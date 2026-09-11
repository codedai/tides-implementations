"""Complete candidate universe with neutral ordering for the v2 blind pass.

The universe repairs the v1 presentation leakage: every hard-valid candidate
row from ``x_complete_schedule_candidates.parquet`` for the case's chunk is
retained (including explicit no-scheduled-trip alternatives and sentinel
contract-unresolved keys, which are flagged, never dropped), the model score
columns are excluded, ordering is schedule-neutral, and display aliases are
assigned only after that ordering.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from .human_label_artifacts import logical_sha256
from .labeling_v2_contracts import (
    NO_SCHEDULED_MATCH_ALIAS,
    NO_SCHEDULED_MATCH_KEY,
    PRESENTATION_VERSION,
    alias_for_index,
)

KIND_COMPLETE = "complete_scheduled_trip"
KIND_NO_TRIP = "no_scheduled_trip_alternative"
KIND_UNRESOLVED = "contract_unresolved"
KIND_NULL_OPTION = "no_scheduled_match_option"

_SENTINEL_PREFIX = "XNULL:"
_SENTINEL_DATE_SORT = "9999-12-31"


def classify_candidate_key(key: str) -> dict[str, Any]:
    """Split a pipe-serialized candidate key and classify its completeness."""

    parts = key.split("|")
    if len(parts) != 5:
        return {
            "candidate_key": key,
            "service_date": None,
            "feed_id": None,
            "route_mapping_id": None,
            "scheduled_trip_id": None,
            "polarity_hypothesis_id": None,
            "explicit_null_polarity": False,
            "candidate_kind": KIND_UNRESOLVED,
            "unresolved_warning": f"key has {len(parts)} fields, expected 5",
        }
    service_date, feed_id, route_mapping_id, trip_id, polarity = parts
    warning = None
    sentinel = any(part.startswith(_SENTINEL_PREFIX) for part in parts)
    valid_date = True
    if service_date and not service_date.startswith(_SENTINEL_PREFIX):
        try:
            date.fromisoformat(service_date)
        except ValueError:
            valid_date = False
    if sentinel:
        kind = KIND_UNRESOLVED
        warning = "contract-unresolved sentinel component; shown with warning"
    elif not valid_date:
        kind = KIND_UNRESOLVED
        warning = "unparseable service date; shown with warning"
    elif service_date and feed_id and route_mapping_id and trip_id:
        kind = KIND_COMPLETE
    elif not any((service_date, feed_id, route_mapping_id, trip_id)):
        kind = KIND_UNRESOLVED
        warning = "key has no identity component; shown with warning"
    else:
        kind = KIND_NO_TRIP
        warning = "explicit no-scheduled-trip alternative"
    return {
        "candidate_key": key,
        "service_date": service_date or None,
        "feed_id": feed_id or None,
        "route_mapping_id": route_mapping_id or None,
        "scheduled_trip_id": trip_id or None,
        "polarity_hypothesis_id": polarity or None,
        "explicit_null_polarity": polarity == "",
        "candidate_kind": kind,
        "unresolved_warning": warning,
    }


def schedule_window_lookup(
    artifacts_root: Path,
    needed: set[tuple[str, str, str]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Scheduled start/end epochs per (service_date, feed_id, trip_id).

    Reads ``gtfs-<date>-<feed>/matched/gtfs/scheduled_stop_events.parquet``.
    The ``arrival_datetime``/``departure_datetime`` columns are tz-aware
    (America/Toronto) and are trusted as-is — re-localizing would shift
    fall-back-night 26:xx trips by an hour.
    """

    windows: dict[tuple[str, str, str], dict[str, Any]] = {}
    by_feed_date: dict[tuple[str, str], set[str]] = {}
    for service_date, feed_id, trip_id in needed:
        by_feed_date.setdefault((service_date, feed_id), set()).add(trip_id)
    for (service_date, feed_id), trip_ids in sorted(
        by_feed_date.items(), key=lambda item: item[0]
    ):
        gtfs_dir = artifacts_root / f"gtfs-{service_date}-{feed_id}"
        events_path = gtfs_dir / "matched" / "gtfs" / "scheduled_stop_events.parquet"
        if not events_path.exists():
            continue
        try:
            frame = (
                pl.scan_parquet(events_path)
                .filter(
                    (pl.col("service_date") == date.fromisoformat(service_date))
                    & pl.col("trip_id").is_in(sorted(trip_ids))
                )
                .group_by("trip_id")
                .agg(
                    pl.col("arrival_datetime").min().dt.epoch("s").alias("start_s"),
                    pl.col("departure_datetime").max().dt.epoch("s").alias("end_s"),
                    pl.len().alias("n_stops"),
                )
                .collect()
            )
        except (OSError, pl.exceptions.PolarsError):
            continue
        for row in frame.iter_rows(named=True):
            windows[(service_date, feed_id, row["trip_id"])] = {
                "sched_start_epoch_s": row["start_s"],
                "sched_end_epoch_s": row["end_s"],
                "sched_n_stops": row["n_stops"],
            }
    return windows


def _sort_key(entry: Mapping[str, Any]) -> tuple:
    service_date = entry["service_date"]
    if service_date is None or str(service_date).startswith(_SENTINEL_PREFIX):
        date_key = _SENTINEL_DATE_SORT
    else:
        date_key = str(service_date)
    start = entry.get("sched_start_epoch_s")
    return (
        date_key,
        entry["feed_id"] or "",
        entry["route_mapping_id"] or "",
        start is None,
        start if start is not None else 0,
        entry["scheduled_trip_id"] or "",
        entry["polarity_hypothesis_id"] or "",
    )


def build_case_universe(
    case_id: str,
    chunk_id: str,
    slice_name: str | None,
    candidate_keys: list[str],
    windows: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Neutrally ordered, aliased universe rows for one case.

    ``candidate_keys`` is the raw (possibly duplicated) key list for the
    chunk; ordering uses only schedule-derived and identity fields.
    """

    entries: list[dict[str, Any]] = []
    for key in sorted(set(candidate_keys)):
        entry = classify_candidate_key(key)
        lookup = (
            entry["service_date"] or "",
            entry["feed_id"] or "",
            entry["scheduled_trip_id"] or "",
        )
        window = windows.get(lookup, {})
        entry["sched_start_epoch_s"] = window.get("sched_start_epoch_s")
        entry["sched_end_epoch_s"] = window.get("sched_end_epoch_s")
        entry["sched_n_stops"] = window.get("sched_n_stops")
        entries.append(entry)
    entries.sort(key=_sort_key)
    universe_hash = logical_sha256([entry["candidate_key"] for entry in entries])
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        rows.append(
            {
                "case_id": case_id,
                "chunk_id": chunk_id,
                "slice": slice_name,
                "display_order": index,
                "candidate_alias": alias_for_index(index),
                **entry,
                "presentation_version": PRESENTATION_VERSION,
                "candidate_universe_hash": universe_hash,
            }
        )
    rows.append(
        {
            "case_id": case_id,
            "chunk_id": chunk_id,
            "slice": slice_name,
            "display_order": len(entries),
            "candidate_alias": NO_SCHEDULED_MATCH_ALIAS,
            "candidate_key": NO_SCHEDULED_MATCH_KEY,
            "service_date": None,
            "feed_id": None,
            "route_mapping_id": None,
            "scheduled_trip_id": None,
            "polarity_hypothesis_id": None,
            "explicit_null_polarity": False,
            "candidate_kind": KIND_NULL_OPTION,
            "unresolved_warning": None,
            "sched_start_epoch_s": None,
            "sched_end_epoch_s": None,
            "sched_n_stops": None,
            "presentation_version": PRESENTATION_VERSION,
            "candidate_universe_hash": universe_hash,
        }
    )
    return rows


def build_universe_manifest(
    restricted_packet: pl.DataFrame,
    schedule_candidates: pl.DataFrame,
    artifacts_root: Path | None,
    *,
    windows: Mapping[tuple[str, str, str], Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Universe rows for every case in the restricted packet, ordered."""

    case_to_chunk = {
        row["case_id"]: row["real_id"]
        for row in restricted_packet.iter_rows(named=True)
    }
    keys_by_chunk: dict[str, list[str]] = {}
    slice_by_chunk: dict[str, str] = {}
    for row in schedule_candidates.iter_rows(named=True):
        keys_by_chunk.setdefault(row["chunk_id"], []).append(row["candidate_key"])
        slice_by_chunk.setdefault(row["chunk_id"], row.get("slice"))
    if windows is None:
        needed: set[tuple[str, str, str]] = set()
        for chunk_id in case_to_chunk.values():
            for key in keys_by_chunk.get(chunk_id, []):
                entry = classify_candidate_key(key)
                if entry["candidate_kind"] == KIND_COMPLETE:
                    needed.add(
                        (
                            entry["service_date"],
                            entry["feed_id"],
                            entry["scheduled_trip_id"],
                        )
                    )
        windows = (
            schedule_window_lookup(artifacts_root, needed)
            if artifacts_root is not None
            else {}
        )
    rows: list[dict[str, Any]] = []
    for case_id in sorted(case_to_chunk):
        chunk_id = case_to_chunk[case_id]
        rows.extend(
            build_case_universe(
                case_id,
                chunk_id,
                slice_by_chunk.get(chunk_id),
                keys_by_chunk.get(chunk_id, []),
                windows,
            )
        )
    return rows


def alias_map_rows(universe_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The immutable alias -> complete-key mapping (display aliases only)."""

    return [
        {
            "case_id": row["case_id"],
            "candidate_alias": row["candidate_alias"],
            "candidate_key": row["candidate_key"],
            "service_date": row["service_date"],
            "feed_id": row["feed_id"],
            "route_mapping_id": row["route_mapping_id"],
            "scheduled_trip_id": row["scheduled_trip_id"],
            "polarity_hypothesis_id": row["polarity_hypothesis_id"],
            "explicit_null_polarity": row["explicit_null_polarity"],
            "candidate_kind": row["candidate_kind"],
            "display_order": row["display_order"],
            "presentation_version": row["presentation_version"],
            "candidate_universe_hash": row["candidate_universe_hash"],
        }
        for row in universe_rows
    ]
