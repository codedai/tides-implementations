"""Neutral, interpretable per-candidate evidence metrics.

Vendored verbatim from the v1 UI (``tools/labeling_ui/labeling/evidence.py``)
for the v2 project; v1 stays frozen because the audit hashes it.

Everything here is recomputed from raw evidence (AVL pings, APC events,
GTFS schedule) — deliberately independent of the spike's qscores, so the
candidate cards describe evidence rather than echo the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl

from . import geometry

STOP_COVERED_RADIUS_M = 150.0
TERMINAL_NEAR_M = 250.0
BOUNDARY_NEAR_S = 120
APC_CANDIDATE_PAD_S = 300


@dataclass
class CandidateMetrics:
    sched_start_epoch: int | None = None
    sched_end_epoch: int | None = None
    sched_duration_s: int | None = None
    overlap_s: int | None = None
    start_residual_s: int | None = None
    end_residual_s: int | None = None
    chunk_containment: float | None = None  # fraction of chunk inside sched window
    n_sched_stops: int | None = None
    stops_covered_frac: float | None = None
    median_dist_to_shape_m: float | None = None
    direction_consistency: float | None = None
    apc_stop_hits: int | None = None
    apc_events_considered: int | None = None
    first_ping_to_first_stop_m: float | None = None
    last_ping_to_last_stop_m: float | None = None
    route_label_match: bool | None = None
    tags: list[str] = field(default_factory=list)


def compute_metrics(
    chunk_start: int | None,
    chunk_end: int | None,
    pings: pl.DataFrame | None,
    avl_route_labels: list[str],
    apc_events: pl.DataFrame | None,
    sched_events: pl.DataFrame | None,
    shape: pl.DataFrame | None,
    route_short_name: str | None,
) -> CandidateMetrics:
    m = CandidateMetrics()

    # ---- temporal fit ------------------------------------------------------
    if sched_events is not None and not sched_events.is_empty():
        m.n_sched_stops = sched_events.height
        starts = sched_events["arrival_epoch_s"].drop_nulls()
        ends = sched_events["departure_epoch_s"].drop_nulls()
        if not starts.is_empty() and not ends.is_empty():
            m.sched_start_epoch = int(starts.min())
            m.sched_end_epoch = int(ends.max())
            m.sched_duration_s = m.sched_end_epoch - m.sched_start_epoch
            if chunk_start is not None and chunk_end is not None:
                m.start_residual_s = chunk_start - m.sched_start_epoch
                m.end_residual_s = chunk_end - m.sched_end_epoch
                lo = max(chunk_start, m.sched_start_epoch)
                hi = min(chunk_end, m.sched_end_epoch)
                m.overlap_s = max(0, hi - lo)
                chunk_dur = max(1, chunk_end - chunk_start)
                m.chunk_containment = m.overlap_s / chunk_dur

    # ---- route label -------------------------------------------------------
    if route_short_name and avl_route_labels:
        m.route_label_match = route_short_name in avl_route_labels

    # ---- spatial fit -------------------------------------------------------
    has_pings = (
        pings is not None
        and not pings.is_empty()
        and {"latitude", "longitude"} <= set(pings.columns)
    )
    if has_pings:
        p = pings.drop_nulls(["latitude", "longitude"])
        has_pings = not p.is_empty()
    if has_pings:
        lat = p["latitude"].to_numpy()
        lon = p["longitude"].to_numpy()
        ref_lat = float(np.nanmean(lat))
        px, py = geometry.to_xy(lat, lon, ref_lat)

        if shape is not None and not shape.is_empty():
            sx, sy = geometry.to_xy(
                shape["shape_pt_lat"].to_numpy(),
                shape["shape_pt_lon"].to_numpy(),
                ref_lat,
            )
            # subsample long inputs to keep the O(P*S) projection cheap
            step_p = max(1, len(px) // 150)
            arc, dist = geometry.project_points_onto_polyline(
                px[::step_p], py[::step_p], sx, sy
            )
            if len(dist) and not np.all(np.isnan(dist)):
                m.median_dist_to_shape_m = float(np.nanmedian(dist))
                m.direction_consistency = geometry.direction_consistency(arc)

        if sched_events is not None and not sched_events.is_empty():
            se = (
                sched_events.drop_nulls(["stop_lat", "stop_lon"])
                if {"stop_lat", "stop_lon"} <= set(sched_events.columns)
                else None
            )
            if se is not None and not se.is_empty():
                qx, qy = geometry.to_xy(
                    se["stop_lat"].to_numpy(), se["stop_lon"].to_numpy(), ref_lat
                )
                d = geometry.min_dist_to_points(qx, qy, px, py)
                if len(d):
                    m.stops_covered_frac = float(np.nanmean(d <= STOP_COVERED_RADIUS_M))
                    m.first_ping_to_first_stop_m = float(
                        np.hypot(qx[0] - px[0], qy[0] - py[0])
                    )
                    m.last_ping_to_last_stop_m = float(
                        np.hypot(qx[-1] - px[-1], qy[-1] - py[-1])
                    )

    # ---- APC evidence ------------------------------------------------------
    if (
        apc_events is not None
        and not apc_events.is_empty()
        and sched_events is not None
        and not sched_events.is_empty()
        and "source_stop_code" in apc_events.columns
        and "stop_code" in sched_events.columns
    ):
        considered = apc_events
        # scope to the candidate's own window (± pad) where known; otherwise
        # same-route candidates all report identical, non-discriminating hits
        if m.sched_start_epoch is not None and m.sched_end_epoch is not None:
            considered = considered.filter(
                pl.col("arrival_epoch_s").is_between(
                    m.sched_start_epoch - APC_CANDIDATE_PAD_S,
                    m.sched_end_epoch + APC_CANDIDATE_PAD_S,
                )
            )
        sched_codes = set(sched_events["stop_code"].drop_nulls().to_list())
        m.apc_events_considered = considered.height
        m.apc_stop_hits = considered.filter(
            pl.col("source_stop_code")
            .cast(pl.String)
            .is_in({str(c) for c in sched_codes})
        ).height

    m.tags = _derive_tags(m, chunk_start, chunk_end)
    return m


def _derive_tags(
    m: CandidateMetrics, chunk_start: int | None, chunk_end: int | None
) -> list[str]:
    tags: list[str] = []
    if (
        m.sched_duration_s
        and chunk_start is not None
        and chunk_end is not None
        and (chunk_end - chunk_start) < 0.5 * m.sched_duration_s
    ):
        tags.append("fragment-like (chunk ≪ scheduled duration)")
    if m.start_residual_s is not None and abs(m.start_residual_s) <= BOUNDARY_NEAR_S:
        tags.append("start boundary-near")
    if m.end_residual_s is not None and abs(m.end_residual_s) <= BOUNDARY_NEAR_S:
        tags.append("end boundary-near")
    if (
        m.first_ping_to_first_stop_m is not None
        and m.first_ping_to_first_stop_m <= TERMINAL_NEAR_M
    ):
        tags.append("starts terminal-near")
    if (
        m.last_ping_to_last_stop_m is not None
        and m.last_ping_to_last_stop_m <= TERMINAL_NEAR_M
    ):
        tags.append("ends terminal-near")
    if m.stops_covered_frac is not None and m.stops_covered_frac < 0.3:
        tags.append("low stop coverage")
    if m.direction_consistency is not None and m.direction_consistency < 0:
        tags.append("direction reversed vs shape")
    if m.route_label_match is False:
        tags.append("AVL route label differs")
    return tags
