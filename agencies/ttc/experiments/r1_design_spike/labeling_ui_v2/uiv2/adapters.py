"""Artifact discovery and tolerant loaders for the v2 (blind) labeling UI.

Adapted from the v1 UI (``tools/labeling_ui/labeling/adapters.py``); v1 stays
frozen because the audit hashes it. Every loader returns ``None`` (never
raises) when an artifact is missing or unreadable; callers surface "not
available" instead of crashing. Loaders are cached per-path so repeated
Streamlit reruns stay cheap.

Expected workspace layout (run01 dir required, everything else optional):

    <artifacts>/human-labeling-v2-run01/          protocol_freeze_manifest.json,
                                                  case/candidate manifests,
                                                  blind_review_order.json
    <artifacts>/experimental-r1-spike02r-<run>/   x_*.parquet (+ restricted packet)
    <artifacts>/diagnostic-*/normalized/{avl,apc}/source_calendar_date=YYYY-MM-DD/...
    <artifacts>/gtfs-<service_date>-<feed_id>/{normalized,matched}/gtfs/*.parquet
    <artifacts>/human-label-audit-v1/             audit trail

The v2 UI never reads ``labeling_packet_redacted.csv``: its summary field
embeds an anchor-status word, which would leak the model verdict into the
blind pass. Case text comes from the run01 manifests instead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import polars as pl

log = logging.getLogger(__name__)

LOCAL_TZ_NAME = "America/Toronto"


# ---------------------------------------------------------------------------
# discovery


@dataclass(frozen=True)
class ArtifactStoreV2:
    """Resolved locations of everything the v2 UI can use."""

    run01_dir: Path
    spike02r_dir: Path | None = None
    normalized_dir: Path | None = None
    gtfs_root: Path | None = None
    audit_dir: Path | None = None
    notes: tuple[str, ...] = ()


def discover_v2(
    run01_dir: str | Path,
    *,
    spike02r_dir: str | Path | None = None,
    artifacts_root: str | Path | None = None,
    audit_dir: str | Path | None = None,
) -> ArtifactStoreV2:
    """Locate the v2 artifact directories around the required run01 dir.

    ``artifacts_root`` is the directory that holds the spike02r run dirs,
    ``diagnostic-*`` and ``gtfs-*`` dirs (e.g. ``agencies/ttc/artifacts``);
    it defaults to the parent of ``run01_dir``. Anything missing degrades to
    ``None`` plus a human-readable note.
    """
    run01 = Path(run01_dir).expanduser().resolve()
    notes: list[str] = []

    if not (run01 / "protocol_freeze_manifest.json").exists():
        notes.append(
            "run01 dir has no protocol_freeze_manifest.json: "
            "protocol freeze cannot be verified"
        )

    root = (
        Path(artifacts_root).expanduser().resolve()
        if artifacts_root is not None
        else run01.parent
    )

    spike02r: Path | None
    if spike02r_dir is not None:
        spike02r = Path(spike02r_dir).expanduser().resolve()
    else:
        candidates = [
            d for d in root.glob("experimental-r1-spike02r-*") if _is_spike02r_dir(d)
        ]
        spike02r = max(candidates, key=_mtime_or_zero, default=None)
        if spike02r is not None:
            notes.append(f"spike02r run dir inferred: {spike02r.name}")
        else:
            notes.append(
                "no experimental-r1-spike02r-* dir with "
                "labeling_packet_restricted.parquet: spike02r evidence unavailable"
            )

    normalized = None
    diag = sorted(root.glob("diagnostic-*/normalized"))
    if diag:
        normalized = diag[-1]
    else:
        notes.append("no diagnostic-*/normalized dir: AVL/APC traces unavailable")

    gtfs_root = root if any(root.glob("gtfs-*")) else None
    if gtfs_root is None:
        notes.append("no gtfs-* snapshot dirs: schedule details unavailable")

    audit: Path | None
    if audit_dir is not None:
        audit = Path(audit_dir).expanduser().resolve()
    else:
        default_audit = root / "human-label-audit-v1"
        audit = default_audit if default_audit.exists() else None
        if audit is None:
            notes.append("no human-label-audit-v1 dir: audit trail unavailable")

    return ArtifactStoreV2(run01, spike02r, normalized, gtfs_root, audit, tuple(notes))


def _is_spike02r_dir(d: Path) -> bool:
    try:
        return d.is_dir() and (d / "labeling_packet_restricted.parquet").exists()
    except OSError:  # unreadable dirs during discovery
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
# run01 (protocol v2) manifests


def load_routing(store: ArtifactStoreV2) -> pl.DataFrame | None:
    """Which review lane each frozen case is routed to."""
    return read_parquet(store.run01_dir / "case_routing_manifest.parquet")


def load_universe(store: ArtifactStoreV2) -> pl.DataFrame | None:
    """The frozen candidate universe per case."""
    return read_parquet(store.run01_dir / "candidate_universe_manifest.parquet")


def load_alias_map(store: ArtifactStoreV2) -> pl.DataFrame | None:
    """Neutral candidate aliases -> real candidate keys."""
    return read_parquet(store.run01_dir / "candidate_alias_map.parquet")


def load_children(store: ArtifactStoreV2) -> pl.DataFrame | None:
    """Child cases spawned from parent cases (splits, follow-ups)."""
    return read_parquet(store.run01_dir / "child_case_manifest.parquet")


def load_blind_order(store: ArtifactStoreV2) -> dict | None:
    """The frozen blind presentation order (``blind_review_order.json``)."""
    data = read_json(store.run01_dir / "blind_review_order.json")
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# spike02r run-dir tables


def _spike02r_file(store: ArtifactStoreV2, name: str) -> Path | None:
    if store.spike02r_dir is None:
        return None
    return store.spike02r_dir / name


def load_refined_chunks(store: ArtifactStoreV2) -> pl.DataFrame | None:
    return read_parquet(_spike02r_file(store, "x_refined_chunks.parquet"))


def load_anchor_evidence_edges(store: ArtifactStoreV2) -> pl.DataFrame | None:
    return read_parquet(_spike02r_file(store, "x_anchor_evidence_edges.parquet"))


def load_anchor_candidates(store: ArtifactStoreV2) -> pl.DataFrame | None:
    """MODEL DIAGNOSTICS — DO NOT USE DURING THE BLIND PASS.

    This table carries the model's anchor verdicts and qscores. The app must
    only call this AFTER a logged post-save reveal: never to build the
    neutral case or candidate views, and never before the reviewer's blind
    label has been persisted and the reveal recorded in the audit trail.
    """
    return read_parquet(_spike02r_file(store, "x_anchor_candidates_v2.parquet"))


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


def load_avl_day(store: ArtifactStoreV2, day: date) -> pl.DataFrame | None:
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


def load_apc_day(store: ArtifactStoreV2, day: date) -> pl.DataFrame | None:
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
    store: ArtifactStoreV2, service_date: date | None, feed_id: str | None
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
