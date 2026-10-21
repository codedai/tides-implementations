"""GTFS feed manifest and per-date feed selection.

The manifest is the offline analog of WMATA's ``dim_schedule_feeds`` +
``fct_daily_schedule_feed_modes``: it records every archive's identity and an
explicit precedence, and :func:`select_feed` resolves exactly one feed per
service date. Unlike WMATA (which infers validity windows from retrieval
timestamps), this prototype requires a reviewed manifest because the TTC
archives carry no ``feed_info.txt`` and no trustworthy acquisition time —
member modification dates are recorded only as draft evidence.

Selection contract (docs/architecture.md):

1. candidates are feeds whose *expanded services* include the date;
2. feeds marked superseded/invalid are excluded;
3. the highest explicit priority wins;
4. an unresolved tie is a fatal error;
5. the decision and its candidates are recorded.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from ..ingest.gtfs import FatalGtfsError, discover_archives, inventory_archive
from ..normalize.gtfs import normalize_archive
from .schedule import feed_daily_services

#: Only reviewed feeds are selectable unless a caller explicitly opts into a draft.
SELECTABLE_STATUSES = ("approved",)
MANIFEST_STATUSES = ("candidate", "approved", "superseded", "invalid")

MANIFEST_SCHEMA: dict[str, pl.DataType] = {
    "feed_id": pl.String,
    "relative_path": pl.String,
    "archive_sha256": pl.String,
    "content_hash": pl.String,
    "calendar_min": pl.Date,
    "calendar_max": pl.Date,
    "member_date_min": pl.Datetime("us"),
    "member_date_max": pl.Datetime("us"),
    "acquired_at": pl.String,
    "published_at": pl.String,
    "priority": pl.Int64,
    "status": pl.String,
    "notes": pl.String,
}


@dataclass(frozen=True)
class FeedSelection:
    """Outcome of selecting a feed for one service date."""

    service_date: dt.date
    feed_id: str | None
    reason: str
    candidate_feed_ids: tuple[str, ...]

    @property
    def selected(self) -> bool:
        return self.feed_id is not None


def build_manifest_draft(gtfs_dir: Path | str) -> pl.DataFrame:
    """Scan a directory of GTFS archives into a draft manifest.

    Every row is written with ``status = "candidate"`` and a note that the
    proposed priority is a hypothesis derived from member modification dates.
    Review, correction, and promotion to ``approved`` happen in
    ``config/feed_manifest.csv``, never in code.
    """
    rows = []
    seen_content: dict[str, str] = {}
    for path in discover_archives(gtfs_dir):
        archive = inventory_archive(path)
        feed = normalize_archive(
            archive, members=("calendar.txt", "calendar_dates.txt")
        )
        services = feed_daily_services(feed)
        if services.is_empty():
            calendar_min = calendar_max = None
        else:
            calendar_min = services["service_date"].min()
            calendar_max = services["service_date"].max()
        member_min, member_max = archive.member_date_range()

        note = (
            "draft: priority proposed from member modification dates; "
            "confirm provenance before approving"
        )
        if archive.content_hash in seen_content:
            note = (
                f"duplicate content of {seen_content[archive.content_hash]}; "
                "keep only one row when approving"
            )
        else:
            seen_content[archive.content_hash] = path.name

        rows.append(
            {
                "feed_id": archive.feed_id,
                "relative_path": path.name,
                "archive_sha256": archive.archive_sha256,
                "content_hash": archive.content_hash,
                "calendar_min": calendar_min,
                "calendar_max": calendar_max,
                "member_date_min": member_min,
                "member_date_max": member_max,
                "acquired_at": None,
                "published_at": None,
                "priority": 0,
                "status": "candidate",
                "notes": note,
            }
        )

    draft = pl.DataFrame(rows, schema=MANIFEST_SCHEMA)
    # Later member dates => assumed later revision => higher priority.
    return (
        draft.sort("member_date_min", "relative_path")
        .with_columns((pl.int_range(1, draft.height + 1) * 10).alias("priority"))
        .sort("priority")
    )


def write_manifest(manifest: pl.DataFrame, path: Path | str) -> None:
    manifest.write_csv(path)


def load_manifest(path: Path | str) -> pl.DataFrame:
    path = Path(path)
    if not path.is_file():
        raise FatalGtfsError(f"feed manifest not found: {path}")
    try:
        manifest = pl.read_csv(path, schema_overrides=MANIFEST_SCHEMA)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise FatalGtfsError(f"unable to read feed manifest {path}: {exc}") from exc
    missing = [c for c in MANIFEST_SCHEMA if c not in manifest.columns]
    if missing:
        raise FatalGtfsError(f"feed manifest {path} missing column(s): {missing}")
    required_values = (
        "feed_id",
        "relative_path",
        "archive_sha256",
        "content_hash",
        "priority",
        "status",
    )
    for column in required_values:
        if manifest.filter(pl.col(column).is_null()).height:
            raise FatalGtfsError(
                f"feed manifest {path} has missing value(s) in {column}"
            )
    duplicate_ids = (
        manifest.group_by("feed_id").len().filter(pl.col("len") > 1)["feed_id"]
    )
    if len(duplicate_ids):
        raise FatalGtfsError(
            f"feed manifest {path} has duplicate feed_id(s): {sorted(duplicate_ids)}"
        )
    duplicate_paths = (
        manifest.group_by("relative_path")
        .len()
        .filter(pl.col("len") > 1)["relative_path"]
    )
    if len(duplicate_paths):
        raise FatalGtfsError(
            f"feed manifest {path} has duplicate relative_path value(s): "
            f"{sorted(duplicate_paths)}"
        )
    invalid_statuses = sorted(
        set(manifest["status"].to_list()).difference(MANIFEST_STATUSES)
    )
    if invalid_statuses:
        raise FatalGtfsError(
            f"feed manifest {path} has invalid status value(s): {invalid_statuses}"
        )
    for column in ("archive_sha256", "content_hash"):
        invalid_hashes = manifest.filter(
            ~pl.col(column).str.contains(r"(?i)^[0-9a-f]{64}$")
        )
        if invalid_hashes.height:
            raise FatalGtfsError(f"feed manifest {path} has invalid {column} value(s)")
    manifest = manifest.with_columns(
        pl.col("archive_sha256").str.to_lowercase(),
        pl.col("content_hash").str.to_lowercase(),
    )
    incomplete_ranges = manifest.filter(
        pl.col("calendar_min").is_null() != pl.col("calendar_max").is_null()
    )
    if incomplete_ranges.height:
        raise FatalGtfsError(
            f"feed manifest {path} must set both calendar_min and calendar_max, "
            "or neither"
        )
    inverted_ranges = manifest.filter(
        pl.col("calendar_min").is_not_null()
        & pl.col("calendar_max").is_not_null()
        & (pl.col("calendar_min") > pl.col("calendar_max"))
    )
    if inverted_ranges.height:
        raise FatalGtfsError(
            f"feed manifest {path} has calendar_min after calendar_max"
        )
    unsafe_paths = []
    for value in manifest["relative_path"].to_list():
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or not relative.name:
            unsafe_paths.append(value)
    if unsafe_paths:
        raise FatalGtfsError(
            f"feed manifest {path} has unsafe relative_path value(s): "
            f"{sorted(unsafe_paths)}"
        )
    return manifest


def _selection_statuses(allow_candidate: bool) -> tuple[str, ...]:
    return (
        (*SELECTABLE_STATUSES, "candidate") if allow_candidate else SELECTABLE_STATUSES
    )


def resolve_manifest_archive(gtfs_dir: Path | str, relative_path: str) -> Path:
    """Resolve one manifest path without allowing escape via ``..``/symlinks."""
    root = Path(gtfs_dir).resolve()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FatalGtfsError(
            f"manifest relative_path escapes GTFS directory: {relative_path!r}"
        ) from exc
    return path


def manifest_service_coverage(
    manifest: pl.DataFrame,
    gtfs_dir: Path | str,
    *,
    service_dates: tuple[dt.date, ...] | None = None,
    allow_candidate: bool = False,
) -> pl.DataFrame:
    """Expanded (feed_id, service_date) coverage for selectable manifest rows.

    Archives are re-hashed and verified against the manifest's pinned
    ``archive_sha256`` so a silently modified input cannot be selected.
    """
    gtfs_dir = Path(gtfs_dir)
    frames = []
    selectable = manifest.filter(
        pl.col("status").is_in(_selection_statuses(allow_candidate))
    )
    for row in selectable.iter_rows(named=True):
        if (
            service_dates
            and row["calendar_min"] is not None
            and row["calendar_max"] is not None
            and not any(
                row["calendar_min"] <= date <= row["calendar_max"]
                for date in service_dates
            )
        ):
            continue
        path = resolve_manifest_archive(gtfs_dir, row["relative_path"])
        if not path.is_file():
            raise FatalGtfsError(f"manifest archive missing on disk: {path}")
        archive = inventory_archive(
            path,
            compute_content_hash=False,
            feed_id=row["feed_id"],
            known_content_hash=row["content_hash"],
        )
        if archive.archive_sha256 != row["archive_sha256"]:
            raise FatalGtfsError(
                f"{path} SHA-256 does not match the manifest pin for "
                f"{row['feed_id']}; the input changed or the manifest is stale"
            )
        feed = normalize_archive(
            archive, members=("calendar.txt", "calendar_dates.txt")
        )
        services = feed_daily_services(feed)
        if service_dates:
            services = services.filter(
                pl.col("service_date").is_in(list(service_dates))
            )
        frames.append(services)
    if not frames:
        return pl.DataFrame(
            schema={
                "service_id": pl.String,
                "service_date": pl.Date,
                "feed_id": pl.String,
            }
        )
    return pl.concat(frames)


def select_feed(
    manifest: pl.DataFrame,
    coverage: pl.DataFrame,
    service_date: dt.date,
    *,
    allow_candidate: bool = False,
) -> FeedSelection:
    """Select exactly one feed for a service date, or none with a reason."""
    active = (
        coverage.filter(pl.col("service_date") == service_date)
        .select("feed_id")
        .unique()
    )
    candidates = active.join(
        manifest.filter(pl.col("status").is_in(_selection_statuses(allow_candidate))),
        on="feed_id",
        how="inner",
    ).sort(["priority", "feed_id"], descending=[True, False])

    candidate_ids = tuple(candidates["feed_id"].to_list())
    if not candidate_ids:
        return FeedSelection(
            service_date=service_date,
            feed_id=None,
            reason="no_feed_with_active_service",
            candidate_feed_ids=(),
        )

    top_priority = candidates["priority"][0]
    tied = candidates.filter(pl.col("priority") == top_priority)
    if tied.height > 1:
        raise FatalGtfsError(
            f"unresolved feed precedence tie on {service_date}: "
            f"{sorted(tied['feed_id'])} share priority {top_priority}; "
            "fix priorities in the feed manifest"
        )

    return FeedSelection(
        service_date=service_date,
        feed_id=candidate_ids[0],
        reason=(
            f"highest_priority={top_priority} among {len(candidate_ids)} candidate(s)"
        ),
        candidate_feed_ids=candidate_ids,
    )
