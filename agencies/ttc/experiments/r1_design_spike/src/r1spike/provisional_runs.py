"""Provisional vehicle activity runs (Spike 02, coarse, GTFS-independent).

A provisional activity run is NOT a performed trip and never mints a
trip id. Runs reuse Spike-01's gap-only X4-A segmentation (uncertain
gaps never split; cross-midnight continuity preserved) and add:

- a Spike-02 run identifier (``xar2-<32 hex>``);
- boundary-censoring flags against the slice wall window;
- motion/dwell/progress summaries computed on demand (not stored) by
  the terminal-episode builder.

All subsequent refinement happens INSIDE one provisional run; no
refinement may join unrelated runs across a hard continuity gap.
"""

from __future__ import annotations

import hashlib
import json

from .segmentation_baselines import SegPing, segment_vehicle
from .spike02_contracts import (
    ALGORITHM_VERSION02,
    RUN_ID_VERSION,
    VehicleActivityRun,
)


def _run_id(vehicle: str, record_ids: tuple[str, ...]) -> str:
    key = json.dumps(
        [RUN_ID_VERSION, ALGORITHM_VERSION02, vehicle, list(record_ids)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{RUN_ID_VERSION}-{hashlib.sha256(key.encode()).hexdigest()[:32]}"


def build_activity_runs(
    pings_by_vehicle: dict[str, list[SegPing]],
    *,
    max_continuity_gap_s: int,
    slice_wall_start_epoch_s: int | None = None,
    slice_wall_end_epoch_s: int | None = None,
    censor_margin_s: int = 900,
) -> list[VehicleActivityRun]:
    """Build coarse provisional runs for every vehicle in the slice.

    A run touching the slice wall window within ``censor_margin_s`` is
    flagged boundary-censored on that side: its first/last trip may be
    truncated by the fixture, not by real operations.
    """
    runs: list[VehicleActivityRun] = []
    for vehicle in sorted(pings_by_vehicle):
        pings = pings_by_vehicle[vehicle]
        if not pings:
            continue
        for seg in segment_vehicle(
            pings, variant="x4a", max_continuity_gap_s=max_continuity_gap_s
        ):
            censored_start = (
                slice_wall_start_epoch_s is not None
                and seg.start_instant_s - slice_wall_start_epoch_s <= censor_margin_s
            )
            censored_end = (
                slice_wall_end_epoch_s is not None
                and slice_wall_end_epoch_s - seg.end_instant_s <= censor_margin_s
            )
            runs.append(
                VehicleActivityRun(
                    run_id=_run_id(seg.vehicle, seg.record_ids),
                    vehicle=seg.vehicle,
                    record_ids=seg.record_ids,
                    start_instant_s=seg.start_instant_s,
                    end_instant_s=seg.end_instant_s,
                    time_uncertain=seg.time_uncertain,
                    n_unresolved=seg.n_unresolved,
                    local_dates=seg.local_dates,
                    route_label_counts=dict(seg.route_label_counts),
                    boundary_censored_start=censored_start,
                    boundary_censored_end=censored_end,
                )
            )
    return runs


def ordered_run_pings(
    run: VehicleActivityRun, pings_by_id: dict[str, SegPing]
) -> list[SegPing]:
    """The run's pings in lineage order (already canonically ordered by
    the segmenter; order is part of the run's identity)."""
    return [pings_by_id[rid] for rid in run.record_ids]
