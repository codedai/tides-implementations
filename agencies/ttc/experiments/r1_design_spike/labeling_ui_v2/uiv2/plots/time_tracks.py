"""DST fold-ambiguity view: one set of wall clocks, two consistent readings.

Source epochs were produced by interpreting naive local wall clocks with
fold=0 (the spike's convention, D-16). During a fall-back repeat a naive
time in the repeated hour is ambiguous: it names two instants one hour
apart. This figure shows the chunk's pings on two parallel tracks — track
A keeps the stored fold-0 (earlier, DST) reading, track B shifts each
fold-ambiguous ping by +3600 s to its fold-1 (later, standard-time)
reading. It visualizes that two consistent readings exist; it does NOT
resolve which one is correct.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import plotly.graph_objects as go

from ..parsing import LOCAL_TZ
from .base import ping_frame

if TYPE_CHECKING:
    from ..case_loader import CaseBundleV2

TRACK_A_COLOR = "#2a78d6"
TRACK_B_COLOR = "#eb6834"
UNAMBIGUOUS_COLOR = "#9a9a9a"
AMBIG_FILL = "#eda100"


def _fold_readings(epoch_s: int) -> tuple[datetime, datetime, bool]:
    """(fold-0 dt, fold-1 dt, is_ambiguous) for a stored epoch.

    The naive wall clock is recovered from the stored fold-0 epoch and
    re-interpreted under both folds; it is ambiguous when the two folds
    map it to different UTC offsets (e.g. 01:00–01:59 on the fall-back
    date).
    """
    wall = datetime.fromtimestamp(epoch_s, LOCAL_TZ).replace(tzinfo=None)
    d0 = wall.replace(tzinfo=LOCAL_TZ, fold=0)
    d1 = wall.replace(tzinfo=LOCAL_TZ, fold=1)
    return d0, d1, d0.utcoffset() != d1.utcoffset()


def fig_time_tracks(bundle: CaseBundleV2) -> go.Figure | None:
    """Two-track wall-clock view; None when no ping is fold-ambiguous."""
    cur = ping_frame(bundle.chunk)
    if cur is None:
        return None
    epochs = [int(e) for e in cur["epoch_s"].to_list()]
    origin = bundle.chunk.start_epoch
    if origin is None:
        origin = epochs[0]
    readings = [_fold_readings(e) for e in epochs]
    if not any(amb for _d0, _d1, amb in readings):
        return None

    track_a = "track A · wall clocks read as fold 0 (earlier)"
    track_b = "track B · wall clocks read as fold 1 (later)"
    xs_a: list[float] = []
    xs_b: list[float] = []
    hover_a: list[str] = []
    hover_b: list[str] = []
    colors_a: list[str] = []
    colors_b: list[str] = []
    amb_x: list[float] = []
    for e, (d0, d1, amb) in zip(epochs, readings, strict=True):
        xa = (e - origin) / 60.0
        xb = (e + 3600 - origin) / 60.0 if amb else xa
        xs_a.append(xa)
        xs_b.append(xb)
        tag = "fold-ambiguous" if amb else "unambiguous"
        hover_a.append(f"{d0.strftime('%H:%M:%S %Z')} · {tag}")
        d_b = d1 if amb else d0
        hover_b.append(f"{d_b.strftime('%H:%M:%S %Z')} · {tag}")
        colors_a.append(TRACK_A_COLOR if amb else UNAMBIGUOUS_COLOR)
        colors_b.append(TRACK_B_COLOR if amb else UNAMBIGUOUS_COLOR)
        if amb:
            amb_x.append(xa)

    fig = go.Figure()
    for label, xs, hover, colors in [
        (track_a, xs_a, hover_a, colors_a),
        (track_b, xs_b, hover_b, colors_b),
    ]:
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=[label] * len(xs),
                mode="markers",
                marker=dict(
                    symbol="line-ns-open",
                    size=10,
                    line=dict(width=2, color=colors),
                ),
                name=label,
                text=hover,
                hovertemplate="%{text}<extra></extra>",
                showlegend=False,
            )
        )

    if amb_x:
        fig.add_vrect(
            x0=min(amb_x),
            x1=max(amb_x) + 60.0,
            fillcolor=AMBIG_FILL,
            opacity=0.12,
            line_width=0,
            annotation_text="回拨重叠导致的墙钟歧义",
            annotation_position="top left",
            annotation_font=dict(size=10, color="#8a6d00"),
        )
    fig.add_annotation(
        xref="paper",
        yref="paper",
        x=0,
        y=1.22,
        showarrow=False,
        text=(
            "⚠ two consistent wall-clock readings exist for the highlighted "
            "pings — this view does not resolve which is correct"
        ),
        font=dict(size=11, color="#b26a00"),
    )
    fig.update_layout(
        height=280,
        margin=dict(l=10, r=10, t=60, b=10),
        yaxis=dict(categoryorder="array", categoryarray=[track_b, track_a]),
        xaxis=dict(title="按各时间轨读数计算的、距 chunk 起点的分钟数"),
    )
    return fig
