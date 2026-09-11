"""Shared landing helpers for delimited AVL/APC source files (plan S1/S2).

Landing preserves source values as strings, assigns the §8 record identity
(``<prefix>-<first 128 bits of file SHA-256>-<1-based data-record ordinal>``),
and attaches the same ``_``-prefixed lineage columns the GTFS stage uses.
Nothing here interprets domain semantics; that is the normalize layer's job.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import polars as pl

#: Lineage columns attached to every landing row, matching the GTFS stage.
SOURCE_LINEAGE_COLUMNS = (
    "_source_type",
    "_source_file",
    "_source_file_sha256",
    "_source_row_number",
    "_ingest_run_id",
    "_record_id",
)

_HASH_CHUNK_BYTES = 4 * 1024 * 1024


class FatalSourceError(RuntimeError):
    """Structural failure that must stop the run (plan §10 outcome 1)."""


@dataclass(frozen=True)
class SourceFile:
    """Identity of one immutable delimited source file."""

    path: Path
    source_type: str
    id_prefix: str
    source_file: str
    sha256: str
    size_bytes: int

    @property
    def record_id_namespace(self) -> str:
        """First 128 bits of the file SHA-256 (plan §8)."""
        return self.sha256[:32]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def inventory_source_file(
    path: Path | str, *, source_type: str, id_prefix: str
) -> SourceFile:
    path = Path(path)
    if not path.is_file():
        raise FatalSourceError(f"{source_type} source file not found: {path}")
    return SourceFile(
        path=path,
        source_type=source_type,
        id_prefix=id_prefix,
        source_file=path.name,
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
    )


def read_header_columns(path: Path) -> list[str]:
    """Read and validate the CSV header (BOM-stripped, no duplicates).

    An empty column name is permitted only in the first position (the APC
    export index); it is reported back as the empty string for the caller to
    rename explicitly.
    """
    with open(path, "rb") as f:
        header = f.readline()
    try:
        line = header.removeprefix(b"\xef\xbb\xbf").decode("utf-8").rstrip("\r\n")
        columns = next(csv.reader(io.StringIO(line)))
    except (UnicodeDecodeError, csv.Error, StopIteration) as exc:
        raise FatalSourceError(f"{path.name}: invalid UTF-8 CSV header") from exc
    columns = [c.strip() for c in columns]
    duplicates = sorted({c for c in columns if c and columns.count(c) > 1})
    if duplicates:
        raise FatalSourceError(
            f"{path.name}: duplicated column name(s) {duplicates} in header"
        )
    unnamed = [i for i, c in enumerate(columns) if not c]
    if unnamed and unnamed != [0]:
        raise FatalSourceError(
            f"{path.name}: unnamed column(s) at position(s) {unnamed}"
        )
    return columns


def require_columns(
    source: SourceFile, columns: list[str], required: tuple[str, ...]
) -> None:
    missing = sorted(set(required) - set(columns))
    if missing:
        raise FatalSourceError(
            f"{source.source_file}: required column(s) missing: {missing}"
        )


def scan_landing(
    source: SourceFile,
    *,
    rename_unnamed_first: str | None = None,
    ingest_run_id: str = "local",
) -> pl.LazyFrame:
    """Lazily scan the source as all-string columns with lineage attached.

    The CSV dialect is pinned: comma-separated, UTF-8 (BOM tolerated), header
    row, empty fields as nulls. The record ordinal is the 1-based data-record
    position under this dialect (plan S1), independent of any export index.
    """
    lf = pl.scan_csv(source.path, infer_schema=False)
    renames = {c: c.strip() for c in lf.collect_schema().names() if c != c.strip()}
    if renames:
        lf = lf.rename(renames)
    names = lf.collect_schema().names()
    if names and names[0] == "" and rename_unnamed_first:
        lf = lf.rename({"": rename_unnamed_first})
    prefix = f"{source.id_prefix}-{source.record_id_namespace}"
    return lf.with_row_index("_source_row_number", offset=1).with_columns(
        pl.col("_source_row_number").cast(pl.Int64),
        pl.lit(source.source_type).alias("_source_type"),
        pl.lit(source.source_file).alias("_source_file"),
        pl.lit(source.sha256).alias("_source_file_sha256"),
        pl.lit(ingest_run_id).alias("_ingest_run_id"),
        pl.concat_str(
            [pl.lit(prefix), pl.col("_source_row_number").cast(pl.String)],
            separator="-",
        ).alias("_record_id"),
    )
