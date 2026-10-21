"""Boundary-spanning dual-role review figure.

Shows the full APC record interval, the current chunk window with its
prev/next context, the reviewer-proposed boundary interval (relative to
chunk start) as an emphasized band, and — when detail-loaded — the
inbound/outbound candidates' scheduled windows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    local_str,
)

if TYPE_CHECKING:
    from ..case_loader import CaseBundleV2

PROPOSED_FILL = "#ffcc66"
PROPOSED_EDGE = "#b26a00"


def _sched_window(obj: object) -> tuple[str, int, int] | None:
    """Alias + scheduled window from a CandidateInfo or CandidateDetail."""
    if obj is None:
        return None
    alias = str(getattr(obj, "alias", "?"))
    start = getattr(obj, "sched_start_epoch", None)
    end = getattr(obj, "sched_end_epoch", None)
    if start is None or end is None:
        metrics = getattr(obj, "metrics", None)
        start = getattr(metrics, "sched_start_epoch", None)
        end = getattr(metrics, "sched_end_epoch", None)
    if start is None or end is None:
        return None
    return alias, int(start), int(end)


def fig_boundary(
    bundle: CaseBundleV2,
    *,
    interval_rel_s: tuple[int, int] | None,
    apc_interval: tuple[int, int] | None = None,
    inbound_detail: object = None,
    outbound_detail: object = None,
) -> go.Figure | None:
    chunk = bundle.chunk
    if chunk is None or chunk.start_epoch is None or chunk.end_epoch is None:
        return None
    proposed = None
    if interval_rel_s is not None:
        proposed = (
            chunk.start_epoch + int(interval_rel_s[0]),
            chunk.start_epoch + int(interval_rel_s[1]),
        )
    extra: list[int] = []
    if proposed is not None:
        extra += list(proposed)
    if apc_interval is not None:
        extra += [int(apc_interval[0]), int(apc_interval[1])]
    axis = TimeAxis(bundle, extra_epochs=extra)
    fig = go.Figure()

    # (row label, start, end, color, hover note, bar width)
    rows: list[tuple[str, int, int, str, str, float]] = []
    if apc_interval is not None:
        s, e = int(apc_interval[0]), int(apc_interval[1])
        rows.append(
            (
                "APC record",
                s,
                e,
                APC_COLOR,
                f"APC record interval: {fmt_duration_s(e - s)}",
                20.0,
            )
        )
    for ci, color, name in [
        (bundle.prev_chunk, PREV_COLOR, "Previous chunk (adjacent context)"),
        (bundle.chunk, CURRENT_COLOR, "Current labeling window"),
        (bundle.next_chunk, NEXT_COLOR, "Next chunk (adjacent context)"),
    ]:
        if ci is not None and ci.start_epoch is not None and ci.end_epoch is not None:
            note = f"{name}: {fmt_duration_s(ci.end_epoch - ci.start_epoch)}"
            rows.append(
                (
                    "AVL chunks",
                    ci.start_epoch,
                    ci.end_epoch,
                    color,
                    note,
                    16.0 if ci is bundle.chunk else 10.0,
                )
            )
    for role, obj in [
        ("inbound", inbound_detail),
        ("outbound", outbound_detail),
    ]:
        win = _sched_window(obj)
        if win is not None:
            alias, s, e = win
            rows.append(
                (
                    f"{role} cand {alias}",
                    s,
                    e,
                    alias_color(alias),
                    f"{role} cand {alias} scheduled window",
                    14.0,
                )
            )

    for label, s, e, color, note, width in rows:
        fig.add_trace(
            go.Scatter(
                x=[axis.x(s), axis.x(e)],
                y=[label, label],
                mode="lines",
                line=dict(color=color, width=width),
                name=note,
                text=[local_str(s), local_str(e)],
                hovertemplate=f"{note}<br>%{{text}}<extra></extra>",
                showlegend=False,
            )
        )

    if proposed is not None:
        fig.add_vrect(
            x0=axis.x(proposed[0]),
            x1=axis.x(proposed[1]),
            fillcolor=PROPOSED_FILL,
            opacity=0.3,
            line_width=2,
            line_color=PROPOSED_EDGE,
            annotation_text="Proposed boundary interval",
            annotation_position="bottom left",
            annotation_font=dict(size=10, color=PROPOSED_EDGE),
        )
        if proposed[0] < chunk.start_epoch or proposed[1] > chunk.end_epoch:
            fig.add_annotation(
                xref="paper",
                yref="paper",
                x=0.5,
                y=1.2,
                showarrow=False,
                text="⚠ Proposed boundary falls outside the displayed chunk",
                font=dict(size=12, color="#c00000"),
            )

    add_current_window(fig, axis, chunk)
    order = list(dict.fromkeys(label for label, *_rest in rows))
    fig.update_layout(
        height=110 + 42 * max(1, len(order)),
        margin=dict(l=10, r=10, t=50, b=10),
        yaxis=dict(categoryorder="array", categoryarray=order),
        xaxis=dict(title=axis.title),
    )
    add_context_note(fig)
    axis.annotate(fig)
    return fig
