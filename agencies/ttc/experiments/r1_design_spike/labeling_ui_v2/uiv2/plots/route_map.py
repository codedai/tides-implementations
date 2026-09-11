"""Panel A: offline route map.

Plain lat/lon scatter with a locked aspect ratio — no tiles, works fully
offline. Candidate shapes are drawn only for detail-loaded candidates
(the ``show_candidate_shapes`` mapping of alias → CandidateDetail).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import plotly.graph_objects as go

from .base import (
    APC_COLOR,
    NEXT_COLOR,
    PREV_COLOR,
    alias_color,
    column_list,
    hover_times,
    local_str,
    ping_frame,
)

if TYPE_CHECKING:
    import polars as pl

    from ..case_loader import CaseBundleV2


def fig_map(
    bundle: CaseBundleV2,
    *,
    show_prev: bool = True,
    show_next: bool = True,
    show_candidate_shapes: dict[str, object] | None = None,
    show_stops: bool = True,
    show_apc: bool = True,
    show_terminals: bool = True,
) -> go.Figure | None:
    cur = ping_frame(bundle.chunk)
    details = show_candidate_shapes or {}
    if cur is None and not details:
        return None
    fig = go.Figure()

    for alias in sorted(details):
        _add_candidate_geometry(fig, alias, details[alias], show_stops, show_terminals)

    for flag, ci, color, name in [
        (show_prev, bundle.prev_chunk, PREV_COLOR, "adjacent context (prev)"),
        (show_next, bundle.next_chunk, NEXT_COLOR, "adjacent context (next)"),
    ]:
        p = ping_frame(ci) if flag else None
        if p is not None:
            fig.add_trace(
                go.Scatter(
                    x=p["longitude"],
                    y=p["latitude"],
                    mode="lines",
                    line=dict(color=color, width=1.5, dash="dot"),
                    name=name,
                    text=hover_times(p),
                    hovertemplate="%{text}<extra>" + name + "</extra>",
                )
            )

    if cur is not None:
        _add_current_chunk(fig, cur)

    if show_apc:
        _add_apc(fig, bundle.apc_events)

    lat0 = None
    if cur is not None:
        lat0 = float(cur["latitude"].mean())
    else:
        for alias in sorted(details):
            shape = getattr(details[alias], "shape", None)
            if shape is not None and not shape.is_empty():
                lat0 = float(shape["shape_pt_lat"].mean())
                break
    ratio = 1.0 / max(0.2, math.cos(math.radians(lat0))) if lat0 else 1.35
    fig.update_layout(
        height=520,
        margin=dict(l=10, r=10, t=10, b=10),
        yaxis=dict(scaleanchor="x", scaleratio=ratio, title="纬度"),
        xaxis=dict(title="经度"),
        legend=dict(orientation="h", yanchor="bottom", y=1.01),
        dragmode="pan",
    )
    return fig


def _add_current_chunk(fig: go.Figure, cur: pl.DataFrame) -> None:
    """Current chunk pings colored by time order, with direction cues."""
    n = cur.height
    fig.add_trace(
        go.Scatter(
            x=cur["longitude"],
            y=cur["latitude"],
            mode="lines+markers",
            line=dict(color="#444444", width=1),
            marker=dict(
                size=5,
                color=list(range(n)),
                colorscale="Viridis",
                showscale=True,
                colorbar=dict(
                    title="时间 →",
                    tickvals=[0, n - 1],
                    ticktext=["起", "止"],
                ),
            ),
            name="当前 chunk 的 AVL",
            text=hover_times(cur),
            hovertemplate="%{text}<extra>当前 chunk 的 AVL</extra>",
        )
    )
    ends = [
        (0, "起点", "circle-open", "#00a000"),
        (n - 1, "终点", "square", "#d00000"),
    ]
    for idx, label, symbol, color in ends:
        fig.add_trace(
            go.Scatter(
                x=[cur["longitude"][idx]],
                y=[cur["latitude"][idx]],
                mode="markers+text",
                marker=dict(size=13, symbol=symbol, color=color, line=dict(width=3)),
                text=[label],
                textposition="top center",
                showlegend=False,
                hoverinfo="skip",
            )
        )
    if n >= 2:
        k = min(n - 1, max(1, n // 8))
        fig.add_annotation(
            x=float(cur["longitude"][k]),
            y=float(cur["latitude"][k]),
            ax=float(cur["longitude"][0]),
            ay=float(cur["latitude"][0]),
            xref="x",
            yref="y",
            axref="x",
            ayref="y",
            showarrow=True,
            arrowhead=3,
            arrowwidth=1.5,
            arrowcolor="#00a000",
            text="时间 →",
            font=dict(size=10, color="#00a000"),
        )


def _add_apc(fig: go.Figure, apc_events: pl.DataFrame | None) -> None:
    if apc_events is None or apc_events.is_empty():
        return
    if not {"latitude", "longitude", "arrival_epoch_s"} <= set(apc_events.columns):
        return
    apc = apc_events.drop_nulls(["latitude", "longitude"])
    if apc.is_empty():
        return
    hover = [
        f"{local_str(e)} stop {sc} +{bo or 0}/-{al or 0}"
        for e, sc, bo, al in zip(
            apc["arrival_epoch_s"].to_list(),
            column_list(apc, "source_stop_code", "?"),
            column_list(apc, "boarding"),
            column_list(apc, "alighting"),
            strict=True,
        )
    ]
    fig.add_trace(
        go.Scatter(
            x=apc["longitude"],
            y=apc["latitude"],
            mode="markers",
            marker=dict(size=9, symbol="x-thin", line=dict(width=2, color=APC_COLOR)),
            name="APC 事件（±30 分钟）",
            text=hover,
            hovertemplate="%{text}<extra>APC</extra>",
        )
    )


def _add_candidate_geometry(
    fig: go.Figure,
    alias: str,
    detail: object,
    show_stops: bool,
    show_terminals: bool,
) -> None:
    """Shape polyline, scheduled stops, and terminals for one candidate."""
    color = alias_color(alias)
    shape = getattr(detail, "shape", None)
    if shape is not None and not shape.is_empty():
        fig.add_trace(
            go.Scatter(
                x=shape["shape_pt_lon"],
                y=shape["shape_pt_lat"],
                mode="lines",
                line=dict(color=color, width=3),
                opacity=0.35,
                name=f"候选 {alias} 的线形",
                hoverinfo="skip",
            )
        )
    se = getattr(detail, "sched_events", None)
    if se is None or se.is_empty() or not ({"stop_lat", "stop_lon"} <= set(se.columns)):
        return
    s = se.drop_nulls(["stop_lat", "stop_lon"])
    if s.is_empty():
        return
    if show_stops:
        hover = [
            f"{alias}: seq {q} {nm} ({sc}) sched {local_str(a)}"
            for q, nm, sc, a in zip(
                column_list(s, "stop_sequence", "?"),
                column_list(s, "stop_name", ""),
                column_list(s, "stop_code", "?"),
                column_list(s, "arrival_epoch_s"),
                strict=True,
            )
        ]
        fig.add_trace(
            go.Scatter(
                x=s["stop_lon"],
                y=s["stop_lat"],
                mode="markers",
                marker=dict(size=5, color=color, opacity=0.5),
                name=f"候选 {alias} 的停站",
                text=hover,
                hovertemplate="%{text}<extra></extra>",
            )
        )
    if show_terminals:
        fig.add_trace(
            go.Scatter(
                x=[s["stop_lon"][0], s["stop_lon"][-1]],
                y=[s["stop_lat"][0], s["stop_lat"][-1]],
                mode="markers",
                marker=dict(size=13, symbol="star", color=color, opacity=0.8),
                name=f"候选 {alias} 的终点站",
                showlegend=False,
                hovertemplate=f"terminal (cand {alias})<extra></extra>",
            )
        )
