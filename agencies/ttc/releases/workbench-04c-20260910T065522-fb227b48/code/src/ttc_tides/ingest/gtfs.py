"""GTFS archive inventory and landing reads.

The landing layer reads GTFS ZIP members without changing their meaning:
every value stays a string, empty fields become nulls, and each row carries
the lineage columns required by ``docs/architecture.md``.

Feed identity follows the WMATA reference implementation: the archive is
identified both by the SHA-256 of the exact file on disk and by a
content hash computed over the sorted member names and bytes, so the same
GTFS content re-zipped in a different order or compression level yields the
same ``feed_id``.
"""

from __future__ import annotations

import csv
import hashlib
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import polars as pl

SOURCE_TYPE = "gtfs"

#: Members the prototype requires before a feed can be processed at all.
REQUIRED_MEMBERS = (
    "agency.txt",
    "routes.txt",
    "stops.txt",
    "stop_times.txt",
    "trips.txt",
)

#: GTFS requires at least one of these two service-calendar files.
SERVICE_CALENDAR_MEMBERS = ("calendar.txt", "calendar_dates.txt")

#: Members that are read when present but whose absence is not fatal.
OPTIONAL_MEMBERS = ("shapes.txt", "feed_info.txt")

KNOWN_MEMBERS = REQUIRED_MEMBERS + SERVICE_CALENDAR_MEMBERS + OPTIONAL_MEMBERS

#: Lineage columns attached to every landing row, in output order.
LINEAGE_COLUMNS = (
    "_source_type",
    "_source_file",
    "_source_file_sha256",
    "_source_member",
    "_source_row_number",
    "_ingest_run_id",
    "_record_id",
    "_feed_id",
)

# These values are constant within a member. Dictionary encoding avoids
# repeating hundreds of megabytes of archive metadata on a multi-million-row
# stop_times table while retaining string semantics and Parquet compatibility.
DICTIONARY_LINEAGE_COLUMNS = (
    "_source_type",
    "_source_file",
    "_source_file_sha256",
    "_source_member",
    "_ingest_run_id",
    "_feed_id",
)

_HASH_CHUNK_BYTES = 4 * 1024 * 1024


class FatalGtfsError(RuntimeError):
    """Structural failure that must stop the run (see quality contract)."""


@dataclass(frozen=True)
class GtfsMember:
    """One file inside a GTFS archive."""

    name: str
    size: int
    modified: datetime


@dataclass(frozen=True)
class GtfsArchive:
    """Identity and inventory of one GTFS ZIP archive."""

    path: Path
    source_file: str
    archive_sha256: str
    content_hash: str
    feed_id: str
    members: tuple[GtfsMember, ...]

    @property
    def member_names(self) -> tuple[str, ...]:
        return tuple(m.name for m in self.members)

    def missing_required_members(self) -> tuple[str, ...]:
        present = set(self.member_names)
        missing = [m for m in REQUIRED_MEMBERS if m not in present]
        if not present.intersection(SERVICE_CALENDAR_MEMBERS):
            missing.append("calendar.txt or calendar_dates.txt")
        return tuple(missing)

    def member_date_range(self) -> tuple[datetime, datetime]:
        """Earliest and latest member modification timestamps.

        These are weak provenance proxies only; the feed manifest is the
        authority on precedence.
        """
        dates = [m.modified for m in self.members]
        return min(dates), max(dates)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _content_hash(zip_file: zipfile.ZipFile) -> str:
    """Order-independent hash of member names and bytes."""
    digest = hashlib.sha256()
    for name in sorted(zip_file.namelist()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\x00")
        with zip_file.open(name) as member:
            while chunk := member.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
        digest.update(b"\x00")
    return digest.hexdigest()


def inventory_archive(
    path: Path | str,
    *,
    source_root: Path | str | None = None,
    compute_content_hash: bool = True,
    feed_id: str | None = None,
    known_content_hash: str | None = None,
) -> GtfsArchive:
    """Hash a GTFS archive and inventory its members.

    ``feed_id`` defaults to ``ttc-<content_hash[:12]>``. Pass an explicit
    ``feed_id`` (normally from the reviewed feed manifest) when skipping the
    content hash. ``known_content_hash`` preserves that pinned identity in the
    returned metadata without decompressing every member again.
    """
    path = Path(path)
    if not path.is_file():
        raise FatalGtfsError(f"GTFS archive not found: {path}")
    if source_root is not None:
        try:
            source_file = str(path.resolve().relative_to(Path(source_root).resolve()))
        except ValueError as exc:
            raise FatalGtfsError(
                f"GTFS archive {path} is outside source root {source_root}"
            ) from exc
    else:
        source_file = path.name

    try:
        archive_sha256 = sha256_file(path)
        with zipfile.ZipFile(path) as zf:
            infos = [info for info in zf.infolist() if not info.is_dir()]
            if not infos:
                raise FatalGtfsError(f"{path}: GTFS ZIP contains no files")
            names = [info.filename for info in infos]
            duplicate_names = sorted(
                name for name, count in Counter(names).items() if count > 1
            )
            if duplicate_names:
                raise FatalGtfsError(
                    f"{path}: duplicate member name(s): {duplicate_names}"
                )
            members = tuple(
                GtfsMember(
                    name=info.filename,
                    size=info.file_size,
                    modified=datetime(*info.date_time),
                )
                for info in infos
            )
            content_hash = (
                _content_hash(zf)
                if compute_content_hash
                else (known_content_hash or "")
            )
    except FatalGtfsError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise FatalGtfsError(f"{path}: invalid GTFS ZIP: {exc}") from exc

    if feed_id is None:
        if not content_hash:
            raise ValueError(
                "feed_id must be provided when compute_content_hash is False"
            )
        feed_id = f"ttc-{content_hash[:12]}"

    return GtfsArchive(
        path=path,
        source_file=source_file,
        archive_sha256=archive_sha256,
        content_hash=content_hash,
        feed_id=feed_id,
        members=members,
    )


def landing_record_ids(file_sha256: str, member: str, n_rows: int) -> pl.Series:
    """Create deterministic record IDs without hashing every row in Python.

    The namespace is a 96-bit SHA-256 prefix of the immutable file/member
    identity; the one-based row number is appended by Polars. This keeps the
    IDs reproducible while avoiding millions of Python ``hashlib`` calls for
    ``stop_times.txt``.
    """
    namespace = hashlib.sha256(f"{file_sha256}:{member}".encode()).hexdigest()[:24]
    rows = pl.DataFrame(
        {"row": pl.int_range(1, n_rows + 1, dtype=pl.Int64, eager=True)}
    )
    return rows.select(
        pl.concat_str(
            [pl.lit(f"r1-{namespace}"), pl.col("row").cast(pl.String)],
            separator="-",
        ).alias("_record_id")
    ).to_series()


def _check_header(header: bytes, member: str) -> None:
    try:
        header_line = header.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
        columns = next(csv.reader([header_line]))
    except (UnicodeDecodeError, csv.Error, StopIteration) as exc:
        raise FatalGtfsError(f"{member}: invalid UTF-8 CSV header") from exc
    columns = [c.strip() for c in columns]
    duplicates = sorted({c for c in columns if columns.count(c) > 1})
    if duplicates:
        raise FatalGtfsError(
            f"{member}: duplicated column name(s) {duplicates} in header"
        )
    if any(not c for c in columns):
        raise FatalGtfsError(f"{member}: header contains an unnamed column")


def read_member_landing(
    archive: GtfsArchive,
    member: str,
    *,
    ingest_run_id: str = "local",
) -> pl.DataFrame:
    """Read one GTFS member as an all-string DataFrame with lineage columns.

    Empty CSV fields become nulls. A UTF-8 byte-order mark, when present,
    is stripped from the header without touching data values.
    """
    if member not in archive.member_names:
        raise FatalGtfsError(f"{archive.source_file}: required member {member} missing")

    try:
        with zipfile.ZipFile(archive.path) as zf:
            with zf.open(member) as source:
                _check_header(source.readline(), member)
            # Read from the decompression stream instead of first materializing
            # the complete member as a second Python bytes object.
            with zf.open(member) as source:
                df = pl.read_csv(source, infer_schema=False)
    except FatalGtfsError:
        raise
    except (
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        pl.exceptions.PolarsError,
    ) as exc:
        raise FatalGtfsError(
            f"{archive.source_file}:{member}: unable to read GTFS CSV: {exc}"
        ) from exc

    df = df.rename({c: c.removeprefix("\ufeff").strip() for c in df.columns})
    n_rows = df.height
    return df.with_columns(
        pl.lit(SOURCE_TYPE).cast(pl.Categorical).alias("_source_type"),
        pl.lit(archive.source_file).cast(pl.Categorical).alias("_source_file"),
        pl.lit(archive.archive_sha256)
        .cast(pl.Categorical)
        .alias("_source_file_sha256"),
        pl.lit(member).cast(pl.Categorical).alias("_source_member"),
        pl.int_range(1, n_rows + 1, dtype=pl.Int64).alias("_source_row_number"),
        pl.lit(ingest_run_id).cast(pl.Categorical).alias("_ingest_run_id"),
        landing_record_ids(archive.archive_sha256, member, n_rows),
        pl.lit(archive.feed_id).cast(pl.Categorical).alias("_feed_id"),
    )


def discover_archives(gtfs_dir: Path | str) -> list[Path]:
    """List GTFS ZIP archives in a directory, sorted by name."""
    gtfs_dir = Path(gtfs_dir)
    if not gtfs_dir.is_dir():
        raise FatalGtfsError(f"GTFS directory not found: {gtfs_dir}")
    return sorted(gtfs_dir.glob("*.zip"))
