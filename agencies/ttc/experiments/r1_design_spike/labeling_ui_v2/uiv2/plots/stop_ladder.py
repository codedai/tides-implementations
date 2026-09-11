"""Panel C: stop ladder for one detail-loaded candidate.

y = scheduled stop occurrences in stop_sequence order (repeated stop_ids
are never collapsed — each occurrence keeps its own rung), with the
scheduled arrival line, observed AVL pass times (nearest ping within
150 m), missed-stop markers, and APC hits by stop code.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
import plotly.graph_objects as go
import polars as pl

from .. import geometry
from .base import (
    APC_COLOR,
    TimeAxis,
    add_current_window,
    alias_color,
    column_list,
    local_str,
    ping_frame,
)

if TYPE_CHECKING:
    from ..case_loader import CandidateDetail, CaseBundleV2

PASS_RADIUS_M = 150.0


def fig_stop_ladder(bundle: CaseBundleV2, detail: CandidateDetail) -> go.Figure | None:
    se = detail.sched_events if detail is not None else None
    if se is None or se.is_empty():
        return None
    if not {"stop_sequence", "arrival_epoch_s"} <= set(se.columns):
        return None
    axis = TimeAxis(bundle)
    color = alias_color(detail.alias)
    s = se.sort("stop_sequence")
    n = s.height
    y = list(range(n))
    names = [
        f"{q}· {nm if nm is not None else (sid or '')}"
        for q, nm, sid in zip(
            s["stop_sequence"].to_list(),
            column_list(s, "stop_name"),
            column_list(s, "stop_id", ""),
            strict=True,
        )
    ]

    fig = go.Figure()
    sched_arrs = s["arrival_epoch_s"].to_list()
    fig.add_trace(
        go.Scatter(
            x=axis.xs(sched_arrs),
            y=y,
            mode="lines+markers",
            line=dict(color=color, width=1.5),
            marker=dict(size=5, color=color),
            name=f"计划到达（候选 {detail.alias}）",
            text=[local_str(e) for e in sched_arrs],
            hovertemplate="%{text}<extra>scheduled</extra>",
        )
    )

    # observed pass time: nearest chunk ping within the pass radius
    cur = ping_frame(bundle.chunk)
    if cur is not None and {"stop_lat", "stop_lon"} <= set(s.columns):
        st = s.with_row_index("ladder_y").drop_nulls(["stop_lat", "stop_lon"])
        if not st.is_empty():
            ref_lat = float(cur["latitude"].mean())
            px, py = geometry.to_xy(
                cur["latitude"].to_numpy(), cur["longitude"].to_numpy(), ref_lat
            )
            qx, qy = geometry.to_xy(
                st["stop_lat"].to_numpy(), st["stop_lon"].to_numpy(), ref_lat
            )
            epochs = cur["epoch_s"].to_numpy()
            obs_e, obs_y, missed_y = [], [], []
            for i in range(st.height):
                d2 = (px - qx[i]) ** 2 + (py - qy[i]) ** 2
                j = int(np.argmin(d2))
                if math.sqrt(float(d2[j])) <= PASS_RADIUS_M:
                    obs_e.append(int(epochs[j]))
                    obs_y.append(int(st["ladder_y"][i]))
                else:
                    missed_y.append(int(st["ladder_y"][i]))
            if obs_e:
                fig.add_trace(
                    go.Scatter(
                        x=axis.xs(obs_e),
                        y=obs_y,
                        mode="markers",
                        marker=dict(size=7, symbol="circle", color="#00a000"),
                        name="AVL 经过该站（≤150 米）",
                        text=[local_str(e) for e in obs_e],
                        hovertemplate="%{text}<extra>AVL pass</extra>",
                    )
                )
            for yy in missed_y:
                fig.add_annotation(
                    x=1.0,
                    xref="paper",
                    y=yy,
                    text="∅",
                    showarrow=False,
                    font=dict(color="#c00000", size=10),
                )

    # APC hits by stop code (a repeated code maps to its last occurrence)
    apc = bundle.apc_events
    if (
        apc is not None
        and not apc.is_empty()
        and "stop_code" in s.columns
        and {"arrival_epoch_s", "source_stop_code"} <= set(apc.columns)
    ):
        codes = {str(v): i for i, v in enumerate(s["stop_code"].to_list())}
        apc = apc.with_columns(
            pl.col("source_stop_code").cast(pl.String).alias("_code")
        )
        hits = apc.filter(pl.col("_code").is_in(list(codes)))
        if not hits.is_empty():
            arrs = hits["arrival_epoch_s"].to_list()
            fig.add_trace(
                go.Scatter(
                    x=axis.xs(arrs),
                    y=[codes[v] for v in hits["_code"].to_list()],
                    mode="markers",
                    marker=dict(
                        size=11,
                        symbol="circle-open",
                        line=dict(width=2, color=APC_COLOR),
                    ),
                    name="该 stop code 上的 APC 记录",
                    text=[local_str(e) for e in arrs],
                    hovertemplate="%{text}<extra>APC</extra>",
                )
            )

    add_current_window(fig, axis, bundle.chunk)
    fig.update_layout(
        height=max(300, 16 * n + 120),
        margin=dict(l=10, r=30, t=30, b=10),
        yaxis=dict(
            tickvals=y,
            ticktext=names,
            tickfont=dict(size=9),
            autorange="reversed",
        ),
        xaxis=dict(title=axis.title),
        legend=dict(orientation="h", yanchor="bottom", y=1.01),
    )
    axis.annotate(fig)
    return fig
