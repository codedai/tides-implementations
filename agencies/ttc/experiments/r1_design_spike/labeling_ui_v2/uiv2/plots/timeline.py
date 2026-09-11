"""Panel B1: context timeline.

Horizontal bars for prev/current/next chunks on one row, APC tick marks,
one row per requested candidate scheduled window, and optional caller-
proposed intervals (multi-trip splits, boundary intervals, seeds) drawn as
emphasized bars.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import plotly.graph_objects as go

from ..parsing import fmt_duration_s
from .base import (
    APC_COLOR,
    CURRENT_COLOR,
    NEXT_COLOR,
    PREV_COLOR,
    TimeAxis,
    add_context_note,
    add_current_window,
    alias_color,
    column_list,
    local_str,
)

if TYPE_CHECKING:
    from ..case_loader import CandidateInfo, CaseBundleV2

#: Interval kinds proposed by the labeling forms (reviewer input, not model
#: output).
INTERVAL_KIND_COLORS = {
    "split": "#d62728",
    "boundary": "#7b3fbf",
    "seed": "#0f766e",
}


def fig_timeline(
    bundle: CaseBundleV2,
    *,
    candidate_windows: list[CandidateInfo] | None = None,
    extra_intervals: list[dict[str, Any]] | None = None,
) -> go.Figure | None:
    chunk = bundle.chunk
    if chunk is None or chunk.start_epoch is None:
        return None
    cands = (
        candidate_windows if candidate_windows is not None else bundle.real_candidates
    )
    cands = [
        c
        for c in cands
        if c.sched_start_epoch is not None and c.sched_end_epoch is not None
    ]
    extras = [
        d
        for d in (extra_intervals or [])
        if d.get("start_epoch") is not None and d.get("end_epoch") is not None
    ]
    axis = TimeAxis(
        bundle,
        extra_epochs=[e for d in extras for e in (d["start_epoch"], d["end_epoch"])],
    )
    fig = go.Figure()

    # (row label, start, end, color, hover note, bar width, opacity)
    rows: list[tuple[str, int, int, str, str, float, float]] = []
    for ci, color, name in [
        (bundle.prev_chunk, PREV_COLOR, "前一个 chunk（相邻上下文）"),
        (bundle.chunk, CURRENT_COLOR, "当前标注窗口"),
        (bundle.next_chunk, NEXT_COLOR, "后一个 chunk（相邻上下文）"),
    ]:
        if ci is not None and ci.start_epoch is not None and ci.end_epoch is not None:
            note = f"{name}: {fmt_duration_s(ci.end_epoch - ci.start_epoch)}"
            width = 16.0 if ci is bundle.chunk else 10.0
            rows.append(
                ("AVL chunks", ci.start_epoch, ci.end_epoch, color, note, width, 1.0)
            )

    for c in cands:
        rows.append(
            (
                f"cand {c.alias}",
                c.sched_start_epoch,
                c.sched_end_epoch,
                alias_color(c.alias),
                f"cand {c.alias} scheduled window",
                14.0,
                0.75,
            )
        )

    extra_labels: list[str] = []
    for d in extras:
        label = str(d.get("label") or "proposed")
        extra_labels.append(label)
        kind = str(d.get("kind") or "")
        rows.append(
            (
                label,
                int(d["start_epoch"]),
                int(d["end_epoch"]),
                INTERVAL_KIND_COLORS.get(kind, "#555555"),
                f"{label} ({kind or 'interval'})",
                18.0,
                0.9,
            )
        )

    for label, s, e, color, note, width, opacity in rows:
        fig.add_trace(
            go.Scatter(
                x=[axis.x(s), axis.x(e)],
                y=[label, label],
                mode="lines",
                line=dict(color=color, width=width),
                name=note,
                text=[local_str(s), local_str(e)],
                hovertemplate=f"{note}<br>%{{text}}<extra></extra>",
                opacity=opacity,
                showlegend=False,
            )
        )

    # gaps between context chunks
    for a, z in [
        (bundle.prev_chunk, bundle.chunk),
        (bundle.chunk, bundle.next_chunk),
    ]:
        if (
            a is not None
            and z is not None
            and a.end_epoch is not None
            and z.start_epoch is not None
            and z.start_epoch > a.end_epoch
        ):
            mid = (a.end_epoch + z.start_epoch) / 2
            fig.add_annotation(
                x=axis.x(mid),
                y="AVL chunks",
                text=f"间隔 {fmt_duration_s(z.start_epoch - a.end_epoch)}",
                showarrow=False,
                yshift=18,
                font=dict(size=10, color="#666666"),
            )

    apc = bundle.apc_events
    if apc is not None and not apc.is_empty() and "arrival_epoch_s" in apc.columns:
        apc = apc.drop_nulls(["arrival_epoch_s"])
        if not apc.is_empty():
            arr = apc["arrival_epoch_s"].to_list()
            fig.add_trace(
                go.Scatter(
                    x=axis.xs(arr),
                    y=["APC"] * apc.height,
                    mode="markers",
                    marker=dict(
                        symbol="line-ns-open",
                        size=11,
                        line=dict(width=1.5, color=APC_COLOR),
                    ),
                    name="APC 事件",
                    text=[
                        f"stop {sc} {local_str(e)}"
                        for sc, e in zip(
                            column_list(apc, "source_stop_code", "?"),
                            arr,
                            strict=True,
                        )
                    ],
                    hovertemplate="%{text}<extra>APC</extra>",
                    showlegend=False,
                )
            )

    order = (
        ["APC", "AVL chunks"]
        + list(dict.fromkeys(extra_labels))
        + [f"cand {c.alias}" for c in reversed(cands)]
    )
    add_current_window(fig, axis, chunk)
    fig.update_layout(
        height=90 + 42 * len(order),
        margin=dict(l=10, r=10, t=40, b=10),
        yaxis=dict(categoryorder="array", categoryarray=order),
        xaxis=dict(title=axis.title),
    )
    add_context_note(fig)
    axis.annotate(fig)
    return fig
