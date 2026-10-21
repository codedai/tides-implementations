"""Panel B2: time vs progress along a candidate's shape.

Chunk (and adjacent) pings are projected onto the detail-loaded candidate's
shape polyline (arc km) against time, with the scheduled stop trajectory
overlaid. Direction reversals are visible by construction. Falls back to
per-segment cumulative distance when no shape is available.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import plotly.graph_objects as go

from .. import geometry
from .base import (
    APC_COLOR,
    NEXT_COLOR,
    PREV_COLOR,
    TimeAxis,
    add_context_note,
    add_current_window,
    alias_color,
    hover_times,
    local_str,
    ping_frame,
)

if TYPE_CHECKING:
    import numpy as np
    import polars as pl

    from ..case_loader import CandidateDetail, CaseBundleV2

DWELL_MIN_S = 90
DWELL_RADIUS_M = 30.0
MAX_DWELL_BANDS = 40
DWELL_FILL = "#e0b400"


def fig_time_progress(
    bundle: CaseBundleV2, detail: CandidateDetail | None
) -> go.Figure | None:
    cur = ping_frame(bundle.chunk)
    if cur is None:
        return None
    axis = TimeAxis(bundle)
    shape = detail.shape if detail is not None else None
    if shape is not None and (
        shape.is_empty() or not {"shape_pt_lat", "shape_pt_lon"} <= set(shape.columns)
    ):
        shape = None
    if shape is None:
        return _fallback_cumulative(bundle, cur, axis)

    ref_lat = float(cur["latitude"].mean())
    sx, sy = geometry.to_xy(
        shape["shape_pt_lat"].to_numpy(),
        shape["shape_pt_lon"].to_numpy(),
        ref_lat,
    )

    def arc_km(p: pl.DataFrame) -> np.ndarray:
        px, py = geometry.to_xy(
            p["latitude"].to_numpy(), p["longitude"].to_numpy(), ref_lat
        )
        arc, _dist = geometry.project_points_onto_polyline(px, py, sx, sy)
        return arc / 1000.0

    fig = go.Figure()
    for ci, color, name in [
        (bundle.prev_chunk, PREV_COLOR, "prev (adjacent context)"),
        (bundle.next_chunk, NEXT_COLOR, "next (adjacent context)"),
    ]:
        p = ping_frame(ci)
        if p is not None:
            fig.add_trace(
                go.Scatter(
                    x=axis.xs(p["epoch_s"].to_list()),
                    y=arc_km(p),
                    mode="markers",
                    marker=dict(size=3, color=color),
                    name=name,
                    text=hover_times(p),
                    hovertemplate="%{text}<extra>" + name + "</extra>",
                )
            )

    fig.add_trace(
        go.Scatter(
            x=axis.xs(cur["epoch_s"].to_list()),
            y=arc_km(cur),
            mode="markers",
            marker=dict(size=5, color=list(range(cur.height)), colorscale="Viridis"),
            name="Current chunk AVL",
            text=hover_times(cur),
            hovertemplate="%{text}<extra>current chunk AVL</extra>",
        )
    )

    dwells = _dwell_intervals(cur)[:MAX_DWELL_BANDS]
    for s, e in dwells:
        fig.add_vrect(
            x0=axis.x(s),
            x1=axis.x(e),
            fillcolor=DWELL_FILL,
            opacity=0.15,
            line_width=0,
        )
    if dwells:
        fig.add_annotation(
            xref="paper",
            yref="paper",
            x=0,
            y=1.14,
            showarrow=False,
            text="Amber band = dwell (more than 90 s within 30 m)",
            font=dict(size=10, color="#8a6d00"),
        )

    se = detail.sched_events if detail is not None else None
    if (
        se is not None
        and not se.is_empty()
        and {"stop_lat", "stop_lon", "arrival_epoch_s"} <= set(se.columns)
    ):
        s = se.drop_nulls(["stop_lat", "stop_lon", "arrival_epoch_s"])
        if not s.is_empty():
            qx, qy = geometry.to_xy(
                s["stop_lat"].to_numpy(), s["stop_lon"].to_numpy(), ref_lat
            )
            arc, _ = geometry.project_points_onto_polyline(qx, qy, sx, sy)
            arrs = s["arrival_epoch_s"].to_list()
            color = alias_color(detail.alias)
            fig.add_trace(
                go.Scatter(
                    x=axis.xs(arrs),
                    y=arc / 1000.0,
                    mode="lines+markers",
                    line=dict(color=color, width=1.5, dash="dash"),
                    marker=dict(size=4, color=color),
                    opacity=0.7,
                    name=f"Candidate {detail.alias} scheduled times",
                    text=[local_str(e) for e in arrs],
                    hovertemplate=("%{text}<extra>cand " + detail.alias + "</extra>"),
                )
            )

    apc = bundle.apc_events
    if (
        apc is not None
        and not apc.is_empty()
        and {"latitude", "longitude", "arrival_epoch_s"} <= set(apc.columns)
    ):
        apc = apc.drop_nulls(["latitude", "longitude", "arrival_epoch_s"])
        if not apc.is_empty():
            qx, qy = geometry.to_xy(
                apc["latitude"].to_numpy(), apc["longitude"].to_numpy(), ref_lat
            )
            arc, _ = geometry.project_points_onto_polyline(qx, qy, sx, sy)
            arrs = apc["arrival_epoch_s"].to_list()
            fig.add_trace(
                go.Scatter(
                    x=axis.xs(arrs),
                    y=arc / 1000.0,
                    mode="markers",
                    marker=dict(
                        size=8,
                        symbol="x-thin",
                        line=dict(width=2, color=APC_COLOR),
                    ),
                    name="APC events",
                    text=[local_str(e) for e in arrs],
                    hovertemplate="%{text}<extra>APC</extra>",
                )
            )

    add_current_window(fig, axis, bundle.chunk)
    alias = detail.alias
    fig.update_layout(
        height=460,
        margin=dict(l=10, r=10, t=40, b=10),
        title=f"Travel progress along candidate {alias} shape",
        xaxis=dict(title=axis.title),
        yaxis=dict(title=f"Distance along shape, km (candidate {alias})"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    add_context_note(fig)
    axis.annotate(fig)
    return fig


def _fallback_cumulative(
    bundle: CaseBundleV2, cur: pl.DataFrame, axis: TimeAxis
) -> go.Figure:
    """No shape: per-segment cumulative along-track distance vs time."""
    fig = go.Figure()
    for ci, color, name in [
        (bundle.prev_chunk, PREV_COLOR, "prev (adjacent context)"),
        (bundle.next_chunk, NEXT_COLOR, "next (adjacent context)"),
    ]:
        p = ping_frame(ci)
        if p is not None:
            dist = geometry.cumulative_distance_m(
                p["latitude"].to_numpy(), p["longitude"].to_numpy()
            )
            fig.add_trace(
                go.Scatter(
                    x=axis.xs(p["epoch_s"].to_list()),
                    y=dist / 1000.0,
                    mode="markers",
                    marker=dict(size=3, color=color),
                    name=name,
                    text=hover_times(p),
                    hovertemplate="%{text}<extra>" + name + "</extra>",
                )
            )
    dist = geometry.cumulative_distance_m(
        cur["latitude"].to_numpy(), cur["longitude"].to_numpy()
    )
    fig.add_trace(
        go.Scatter(
            x=axis.xs(cur["epoch_s"].to_list()),
            y=dist / 1000.0,
            mode="markers",
            marker=dict(size=5, color=list(range(cur.height)), colorscale="Viridis"),
            name="Current chunk AVL",
            text=hover_times(cur),
            hovertemplate="%{text}<br>%{y:.2f} km<extra></extra>",
        )
    )
    add_current_window(fig, axis, bundle.chunk)
    fig.update_layout(
        height=420,
        margin=dict(l=10, r=10, t=40, b=10),
        title="No candidate shape loaded — showing cumulative distance per segment",
        xaxis=dict(title=axis.title),
        yaxis=dict(title="Distance from segment start, km"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    add_context_note(fig)
    axis.annotate(fig)
    return fig


def _dwell_intervals(cur: pl.DataFrame) -> list[tuple[int, int]]:
    """Epoch spans where the vehicle stays within DWELL_RADIUS_M for more
    than DWELL_MIN_S (near-stationary runs, greedy from each run start)."""
    epochs = cur["epoch_s"].to_list()
    n = len(epochs)
    if n < 2:
        return []
    ref_lat = float(cur["latitude"].mean())
    x, y = geometry.to_xy(
        cur["latitude"].to_numpy(), cur["longitude"].to_numpy(), ref_lat
    )
    out: list[tuple[int, int]] = []
    i = 0
    while i < n:
        j = i
        while (
            j + 1 < n
            and math.hypot(float(x[j + 1] - x[i]), float(y[j + 1] - y[i]))
            < DWELL_RADIUS_M
        ):
            j += 1
        if epochs[j] - epochs[i] > DWELL_MIN_S:
            out.append((int(epochs[i]), int(epochs[j])))
        i = j + 1
    return out
