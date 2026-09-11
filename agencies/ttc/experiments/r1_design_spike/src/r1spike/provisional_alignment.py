"""Trip-agnostic APC-to-vehicle-timeline alignment (Spike 02).

Establishes ONLY compatibility evidence between one APC source record
and one provisional vehicle activity run: vehicle hypothesis, wall-time
proximity per fold, bracketing AVL observations, terminal/stop-area
compatibility, route-label compatibility, spatial plausibility, and the
source activity-signal state.

It never states a final segment owner, service date, scheduled trip, or
stop occurrence. Every candidate edge is persisted. The edges are
evidence for boundary refinement only and must not be reused later as
proof that the refined segment was correct (double-counting caveat,
reported in the findings).
"""

from __future__ import annotations

import math

from .candidate_generation import ApcRecord
from .segmentation_baselines import SegPing
from .spike02_contracts import (
    AlignmentEdge,
    ApcBoundarySignal,
    Reason02,
    TerminalCluster,
    VehicleActivityRun,
)
from .temporal_features import interval_gap_s
from .terminal_clusters import point_cluster

EARTH_RADIUS_M = 6_371_008.8


def _dist_m(lat0: float, lon0: float, lat1: float, lon1: float) -> float:
    lat_ref = math.radians((lat0 + lat1) / 2.0)
    dx = EARTH_RADIUS_M * math.cos(lat_ref) * math.radians(lon1 - lon0)
    dy = EARTH_RADIUS_M * math.radians(lat1 - lat0)
    return math.hypot(dx, dy)


def _bracketing(
    pings: list[SegPing], instant_s: int
) -> tuple[SegPing | None, SegPing | None]:
    """Last resolved ping at/before and first resolved ping at/after."""
    prev = None
    nxt = None
    for p in pings:
        if p.instant_s is None:
            continue
        if p.instant_s <= instant_s:
            prev = p
        if p.instant_s >= instant_s and nxt is None:
            nxt = p
    return prev, nxt


def align_apc_records(
    records: list[ApcRecord],
    runs: list[VehicleActivityRun],
    pings_by_id: dict[str, SegPing],
    signals_by_id: dict[str, ApcBoundarySignal],
    clusters: list[TerminalCluster],
    *,
    max_time_gap_s: int,
    interpolation_max_gap_s: int,
) -> list[AlignmentEdge]:
    """All provisional alignment edges, one per (record, run, fold)
    hypothesis within the temporal bound. Deterministic order."""
    runs_by_vehicle: dict[str, list[VehicleActivityRun]] = {}
    for run in runs:
        runs_by_vehicle.setdefault(run.vehicle, []).append(run)
    for vehicle_runs in runs_by_vehicle.values():
        vehicle_runs.sort(key=lambda r: (r.start_instant_s, r.run_id))

    edges: list[AlignmentEdge] = []
    for record in sorted(records, key=lambda r: r.record_id):
        signal = signals_by_id.get(record.record_id)
        vehicle_runs = runs_by_vehicle.get(record.vehicle, [])
        for fold, a0 in record.arrival_candidates:
            dep = [e for f, e in record.departure_candidates if f == fold]
            a1 = max(dep) if dep else a0
            if a1 < a0:
                a1 = a0
            for run in vehicle_runs:
                gap = interval_gap_s(a0, a1, run.start_instant_s, run.end_instant_s)
                if gap > max_time_gap_s:
                    continue
                run_pings = [pings_by_id[rid] for rid in run.record_ids]
                prev, nxt = _bracketing(run_pings, a0)
                reasons: list[str] = []
                ping_to_stop_m: float | None = None
                if prev is None and nxt is None:
                    reasons.append(Reason02.ALIGN_NO_BRACKET)
                elif (
                    prev is not None
                    and nxt is not None
                    and record.latitude is not None
                    and record.longitude is not None
                    and prev.latitude is not None
                    and nxt.latitude is not None
                    and (nxt.instant_s - prev.instant_s) <= interpolation_max_gap_s
                ):
                    span = nxt.instant_s - prev.instant_s
                    frac = 0.0 if span == 0 else (a0 - prev.instant_s) / span
                    lat = prev.latitude + (nxt.latitude - prev.latitude) * frac
                    lon = prev.longitude + (nxt.longitude - prev.longitude) * frac
                    ping_to_stop_m = _dist_m(
                        lat, lon, record.latitude, record.longitude
                    )
                cluster = point_cluster(clusters, record.latitude, record.longitude)
                stop_in_cluster = bool(
                    cluster is not None
                    and record.stop_code is not None
                    and str(record.stop_code) in cluster.member_stop_codes
                )
                route_agrees: bool | None = None
                if record.route_label is not None and run.route_label_counts:
                    route_agrees = record.route_label in run.route_label_counts
                edges.append(
                    AlignmentEdge(
                        apc_source_record_id=record.record_id,
                        run_id=run.run_id,
                        fold=fold,
                        arrival_epoch_s=a0,
                        departure_epoch_s=a1,
                        time_gap_to_run_s=gap,
                        prev_ping_record_id=(prev.record_id if prev else None),
                        next_ping_record_id=(nxt.record_id if nxt else None),
                        prev_gap_s=(a0 - prev.instant_s if prev else None),
                        next_gap_s=(nxt.instant_s - a0 if nxt else None),
                        ping_to_stop_m=ping_to_stop_m,
                        terminal_cluster_id=(cluster.cluster_id if cluster else None),
                        stop_in_cluster=stop_in_cluster,
                        route_label_agrees=route_agrees,
                        direction_label=record.direction_label,
                        signal_state=(signal.state.value if signal else "unknown"),
                        signal_quality=(
                            signal.quality.value if signal else "q0_unusable"
                        ),
                        hard_valid=gap <= max_time_gap_s,
                        reason_codes=tuple(reasons),
                    )
                )
        if not vehicle_runs:
            # Persisted absence: an APC-only vehicle produces no edge but
            # the caller can see that from the empty set; nothing here
            # invents a run.
            continue
    return edges
