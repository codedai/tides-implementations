"""Artifact discovery and tolerant loaders.

Every loader returns ``None`` (never raises) when an artifact is missing or
unreadable; callers surface "not available" instead of crashing. Loaders are
cached per-path so repeated Streamlit reruns stay cheap.

Expected workspace layout (all optional except the cases CSV):

    <artifacts>/experimental-r1-spike02r-<run>/   "run dir": labeling packet
        labeling_packet_redacted.csv               + x_*.parquet + *.json
    <artifacts>/diagnostic-*/normalized/{avl,apc}/source_calendar_date=YYYY-MM-DD/...
    <artifacts>/gtfs-<service_date>-<feed_id>/{normalized,matched}/gtfs/*.parquet
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

import polars as pl

log = logging.getLogger(__name__)

LOCAL_TZ_NAME = "America/Toronto"


# ---------------------------------------------------------------------------
# discovery


@dataclass(frozen=True)
class ArtifactStore:
    """Resolved locations of everything the UI can use."""

    cases_csv: Path
    run_dir: Path | None = None
    normalized_dir: Path | None = None
    gtfs_root: Path | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def discover(cases_csv: str | Path, artifacts_root: str | Path | None) -> ArtifactStore:
    """Locate optional artifact directories near the cases CSV.

    ``artifacts_root`` is the directory that holds run dirs, ``diagnostic-*``
    and ``gtfs-*`` dirs (e.g. ``agencies/ttc/artifacts``). When omitted, the
    parent chain of the cases CSV is searched.
    """
    cases_csv = Path(cases_csv).expanduser().resolve()
    notes: list[str] = []

    run_dir = None
    if _is_run_dir(cases_csv.parent):
        run_dir = cases_csv.parent

    root = Path(artifacts_root).expanduser().resolve() if artifacts_root else None
    if root is None:
        # walk up a little from the CSV looking for gtfs-*/diagnostic-* dirs;
        # bounded so we never wander into unrelated system directories
        for parent in list(cases_csv.parents)[:3]:
            try:
                if any(
                    p.is_dir() for p in parent.glob("gtfs-*")
                ) or any(p.is_dir() for p in parent.glob("diagnostic-*")):
                    root = parent
                    break
            except OSError:
                continue
    if root is None:
        notes.append("no artifacts root found; only the cases CSV will be shown")
        return ArtifactStore(cases_csv, run_dir, None, None, tuple(notes))

    if run_dir is None:
        # any run dir under the root that carries this packet
        candidates = [d for d in root.glob("*") if _is_run_dir(d)]
        run_dir = max(candidates, key=_mtime_or_zero, default=None)
        if run_dir is not None:
            notes.append(f"run dir inferred: {run_dir.name}")

    normalized = None
    diag = sorted(root.glob("diagnostic-*/normalized"))
    if diag:
        normalized = diag[-1]
    else:
        notes.append("no diagnostic-*/normalized dir: AVL/APC traces unavailable")

    gtfs_root = root if any(root.glob("gtfs-*")) else None
    if gtfs_root is None:
        notes.append("no gtfs-* snapshot dirs: schedule details unavailable")

    return ArtifactStore(cases_csv, run_dir, normalized, gtfs_root, tuple(notes))


def _is_run_dir(d: Path) -> bool:
    try:
        return d.is_dir() and (
            (d / "labeling_packet_redacted.csv").exists()
            or (d / "labeling_packet_restricted.parquet").exists()
        )
    except OSError:  # unreadable system dirs during upward discovery
        return False


def _mtime_or_zero(d: Path) -> float:
    try:
        return d.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# generic guarded readers
#
# each cache key includes a cheap freshness stamp so a regenerated artifact
# is picked up without restarting the app.


def _stamp(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _tree_stamp(part: Path) -> int:
    try:
        return sum(_stamp(p) for p in part.glob("**/*.parquet"))
    except OSError:
        return 0


@lru_cache(maxsize=256)
def _read_parquet(path_str: str, stamp: int) -> pl.DataFrame | None:
    try:
        return pl.read_parquet(path_str)
    except Exception as exc:  # missing, corrupt, permission — degrade
        log.warning("could not read %s: %s", path_str, exc)
        return None


def read_parquet(path: Path | None) -> pl.DataFrame | None:
    if path is None:
        return None
    return _read_parquet(str(path), _stamp(path))


def read_json(path: Path | None) -> dict | list | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        log.warning("could not read %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# cases + run-dir tables


def load_cases(store: ArtifactStore) -> pl.DataFrame | None:
    """The redacted cases CSV, in original row order (``case_order``)."""
    try:
        df = pl.read_csv(store.cases_csv, infer_schema_length=1000)
    except Exception as exc:
        log.error("cannot read cases CSV %s: %s", store.cases_csv, exc)
        return None
    if "case_id" not in df.columns:
        log.error("cases CSV has no case_id column")
        return None
    for col, dtype in [
        ("stratum", pl.String),
        ("kind", pl.String),
        ("n_events", pl.Int64),
        ("span_rel_s", pl.Int64),
        ("summary", pl.String),
    ]:
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(col))
    # CSV inference may type these numerically (an all-digit case_id, an
    # empty summary column) — string ops downstream must stay safe
    df = df.with_columns(
        pl.col("case_id").cast(pl.String),
        pl.col("stratum").cast(pl.String),
        pl.col("kind").cast(pl.String),
        pl.col("summary").cast(pl.String),
        pl.col("n_events").cast(pl.Int64, strict=False),
        pl.col("span_rel_s").cast(pl.Int64, strict=False),
    )
    dups = df.filter(pl.col("case_id").is_duplicated())
    if not dups.is_empty():
        log.warning(
            "cases CSV has %d duplicated case_id rows; keeping first of each",
            dups.height,
        )
        df = df.unique(subset=["case_id"], keep="first", maintain_order=True)
    return df.with_row_index("case_order")


def _run_file(store: ArtifactStore, name: str) -> Path | None:
    if store.run_dir is None:
        return None
    return store.run_dir / name


def load_restricted_packet(store: ArtifactStore) -> pl.DataFrame | None:
    """case_id -> real chunk id + case epoch (restricted-local artifact)."""
    return read_parquet(_run_file(store, "labeling_packet_restricted.parquet"))


def load_refined_chunks(store: ArtifactStore) -> pl.DataFrame | None:
    return read_parquet(_run_file(store, "x_refined_chunks.parquet"))


def load_anchor_candidates(store: ArtifactStore) -> pl.DataFrame | None:
    return read_parquet(_run_file(store, "x_anchor_candidates_v2.parquet"))


def load_schedule_candidates(store: ArtifactStore) -> pl.DataFrame | None:
    return read_parquet(_run_file(store, "x_complete_schedule_candidates.parquet"))


def load_anchor_evidence_edges(store: ArtifactStore) -> pl.DataFrame | None:
    return read_parquet(_run_file(store, "x_anchor_evidence_edges.parquet"))


def load_fragment_roles(store: ArtifactStore) -> pl.DataFrame | None:
    return read_parquet(_run_file(store, "x_fragment_roles.parquet"))


def load_run_report(store: ArtifactStore, name: str) -> dict | list | None:
    """Any of the run-level JSON reports (unique_survivor_report.json, ...)."""
    return read_json(_run_file(store, name))


# ---------------------------------------------------------------------------
# normalized AVL / APC (hive-partitioned by source_calendar_date)


def _epoch_expr(df: pl.DataFrame, col: str) -> pl.Expr:
    """Datetime column -> epoch seconds.

    Naive columns are interpreted as America/Toronto wall time (the spike's
    convention): DST fall-back ambiguity resolves to the earlier instant and
    spring-forward nonexistent times become null. Columns that already carry
    a time zone are trusted as-is — re-localizing them would silently shift
    post-transition times (e.g. GTFS "26:xx" stops on the fall-back night).
    """
    dtype = df.schema[col]
    if getattr(dtype, "time_zone", None):
        return pl.col(col).dt.epoch("s")
    return (
        pl.col(col)
        .dt.replace_time_zone(LOCAL_TZ_NAME, ambiguous="earliest", non_existent="null")
        .dt.epoch("s")
    )


def load_avl_day(store: ArtifactStore, day: date) -> pl.DataFrame | None:
    """All vehicles' AVL pings for one local calendar date, with ``epoch_s``."""
    if store.normalized_dir is None:
        return None
    part = store.normalized_dir / "avl" / f"source_calendar_date={day.isoformat()}"
    if not part.exists():
        return None
    return _load_avl_partition(str(part), _tree_stamp(part))


@lru_cache(maxsize=32)
def _load_avl_partition(part_str: str, stamp: int) -> pl.DataFrame | None:
    try:
        df = pl.read_parquet(f"{part_str}/**/*.parquet")
        return df.with_columns(_epoch_expr(df, "event_local_time").alias("epoch_s"))
    except Exception as exc:
        log.warning("could not read AVL partition %s: %s", part_str, exc)
        return None


def load_apc_day(store: ArtifactStore, day: date) -> pl.DataFrame | None:
    """All vehicles' APC stop events for one local date, with arrival/departure
    epochs."""
    if store.normalized_dir is None:
        return None
    part = store.normalized_dir / "apc" / f"source_calendar_date={day.isoformat()}"
    if not part.exists():
        return None
    return _load_apc_partition(str(part), _tree_stamp(part))


@lru_cache(maxsize=32)
def _load_apc_partition(part_str: str, stamp: int) -> pl.DataFrame | None:
    try:
        df = pl.read_parquet(f"{part_str}/**/*.parquet")
        return df.with_columns(
            _epoch_expr(df, "arrival_local_time").alias("arrival_epoch_s"),
            _epoch_expr(df, "departure_local_time").alias("departure_epoch_s"),
        )
    except Exception as exc:
        log.warning("could not read APC partition %s: %s", part_str, exc)
        return None


# ---------------------------------------------------------------------------
# GTFS snapshots


def gtfs_dir_for(
    store: ArtifactStore, service_date: date | None, feed_id: str | None
) -> Path | None:
    """``gtfs-<service_date>-<feed_id>`` under the artifacts root."""
    if store.gtfs_root is None or service_date is None or feed_id is None:
        return None
    d = store.gtfs_root / f"gtfs-{service_date.isoformat()}-{feed_id}"
    return d if d.is_dir() else None


def load_scheduled_trips(gtfs_dir: Path | None) -> pl.DataFrame | None:
    if gtfs_dir is None:
        return None
    return read_parquet(gtfs_dir / "matched" / "gtfs" / "scheduled_trips.parquet")


@lru_cache(maxsize=16)
def _load_stop_events_all(path_str: str, stamp: int) -> pl.DataFrame | None:
    df = _read_parquet(path_str, stamp)
    if df is None:
        return None
    try:
        return df.with_columns(
            _epoch_expr(df, "arrival_datetime").alias("arrival_epoch_s"),
            _epoch_expr(df, "departure_datetime").alias("departure_epoch_s"),
        )
    except Exception as exc:  # schema drift — degrade like the other loaders
        log.warning("could not derive epochs from %s: %s", path_str, exc)
        return None


def load_stop_events_for_trip(
    gtfs_dir: Path | None, service_date: date | None, trip_id: str | None
) -> pl.DataFrame | None:
    """Scheduled stop events (with epochs) for one trip on one service date,
    ordered by stop_sequence."""
    if gtfs_dir is None or trip_id is None:
        return None
    path = gtfs_dir / "matched" / "gtfs" / "scheduled_stop_events.parquet"
    all_events = _load_stop_events_all(str(path), _stamp(path))
    if all_events is None:
        return None
    try:
        out = all_events.filter(pl.col("trip_id") == trip_id)
        if service_date is not None and "service_date" in out.columns:
            out = out.filter(pl.col("service_date") == service_date)
        if out.is_empty():
            return None
        return out.sort("stop_sequence")
    except Exception as exc:
        log.warning("could not slice stop events from %s: %s", path, exc)
        return None


def load_shape(gtfs_dir: Path | None, shape_id: str | None) -> pl.DataFrame | None:
    """Shape points for one shape_id, ordered by point sequence."""
    if gtfs_dir is None or shape_id is None:
        return None
    shapes = read_parquet(gtfs_dir / "normalized" / "gtfs" / "shapes.parquet")
    if shapes is None:
        return None
    try:
        out = shapes.filter(pl.col("shape_id") == shape_id)
        if out.is_empty():
            return None
        return out.sort("shape_pt_sequence")
    except Exception as exc:
        log.warning("could not slice shape %s: %s", shape_id, exc)
        return None


def load_stops(gtfs_dir: Path | None) -> pl.DataFrame | None:
    if gtfs_dir is None:
        return None
    return read_parquet(gtfs_dir / "normalized" / "gtfs" / "stops.parquet")


def clear_caches() -> None:
    """Mainly for tests."""
    _read_parquet.cache_clear()
    _load_avl_partition.cache_clear()
    _load_apc_partition.cache_clear()
    _load_stop_events_all.cache_clear()
