"""Refined-segment adapters (Spike 02).

Converts frozen ``RefinedSegment`` objects into Spike-01 ``Segment``
objects so the existing candidate-generation and staged-resolution
machinery (which is variant-agnostic) can run over refined segments
without modifying any Spike-01 module. The refined segment keeps its
Spike-02 identity (``xrs2-*``); nothing here mints a performed-trip id.
"""

from __future__ import annotations

from .segmentation_baselines import Segment, SegPing
from .spike02_contracts import (
    RefinedSegment,
    RefinementResult,
    SegmentTypeHypothesis,
    VehicleActivityRun,
)


def to_spike01_segments(
    refined: list[RefinedSegment],
    pings_by_id: dict[str, SegPing],
) -> list[Segment]:
    """One Spike-01 ``Segment`` per refined segment, same id/lineage."""
    segments: list[Segment] = []
    for seg in refined:
        vehicle = pings_by_id[seg.record_ids[0]].vehicle
        segments.append(
            Segment(
                segment_id=seg.refined_segment_id,
                variant=f"xrf2-{seg.variant}",
                vehicle=vehicle,
                record_ids=seg.record_ids,
                start_instant_s=seg.start_instant_s,
                end_instant_s=seg.end_instant_s,
                time_uncertain=seg.time_uncertain,
                n_unresolved=seg.n_unresolved,
                local_dates=seg.local_dates,
                route_label_counts=dict(seg.route_label_counts),
                boundary_reasons=seg.reason_codes,
            )
        )
    return segments


def runs_as_refined(
    runs: list[VehicleActivityRun],
    variant: str = "v0_unrefined",
) -> list[RefinedSegment]:
    """The no-refinement comparator: each provisional run as one
    unsplit 'refined' segment (used to measure refinement deltas)."""
    out: list[RefinedSegment] = []
    for run in runs:
        out.append(
            RefinedSegment(
                refined_segment_id=run.run_id,
                run_id=run.run_id,
                variant=variant,
                record_ids=run.record_ids,
                start_instant_s=run.start_instant_s,
                end_instant_s=run.end_instant_s,
                segment_type=SegmentTypeHypothesis.UNRESOLVED,
                boundary_id_before=None,
                boundary_id_after=None,
                time_uncertain=run.time_uncertain,
                n_unresolved=run.n_unresolved,
                local_dates=run.local_dates,
                route_label_counts=dict(run.route_label_counts),
                censored_start=run.boundary_censored_start,
                censored_end=run.boundary_censored_end,
            )
        )
    return out


def membership_rows(results: list[RefinementResult]) -> list[dict]:
    """One row per (run, ping) with its exclusive post-refinement state."""
    rows: list[dict] = []
    for res in results:
        owner_by_record: dict[str, str] = {}
        for seg in res.refined_segments:
            for rid in seg.record_ids:
                owner_by_record[rid] = seg.refined_segment_id
        for ta in res.turnarounds:
            for rid in ta.record_ids:
                owner_by_record[rid] = ta.turnaround_id
        for record_id, state in sorted(res.ping_states.items()):
            rows.append(
                {
                    "run_id": res.run_id,
                    "refinement_variant": res.variant,
                    "avl_record_id": record_id,
                    "state": state,
                    "owner_id": owner_by_record.get(record_id),
                }
            )
    return rows
