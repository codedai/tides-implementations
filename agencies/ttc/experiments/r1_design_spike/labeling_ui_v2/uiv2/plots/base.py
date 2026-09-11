"""Shared plotting primitives for the v2 evidence panels.

Adapted from the frozen v1 UI (``tools/labeling_ui/labeling/plots.py``).
Everything here renders neutral evidence only: candidate identity is an
alias letter, and nothing model-derived is ever drawn.

The CRITICAL visual contract: the current labeling window is always
visually distinct from adjacent context — prev/next traces are muted and
dotted, the current window is shaded via :func:`add_current_window`, and
timeline-style figures carry :func:`add_context_note`.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING

import plotly.graph_objects as go

from ..parsing import LOCAL_TZ

if TYPE_CHECKING:
    import polars as pl

    from ..case_loader import CaseBundleV2, ChunkWindow

PREV_COLOR = "#b0b0b0"
NEXT_COLOR = "#7f7f7f"
CURRENT_COLOR = "#333333"
APC_COLOR = "#e6194b"
WINDOW_FILL = "#888888"

#: Fixed qualitative palette for candidate aliases (v2 has up to 57 aliases,
#: hashed into these 10 slots). Grays are excluded on purpose: gray always
#: means "adjacent context". The alias text in legends and hover carries
#: identity too — color is never the sole channel.
ALIAS_PALETTE = (
    "#2a78d6",
    "#eb6834",
    "#4a3aa7",
    "#eda100",
    "#008300",
    "#e87ba4",
    "#9b5b21",
    "#1baf7a",
    "#e34948",
    "#00a3c4",
)


def alias_color(alias: str) -> str:
    """Deterministic palette color for a candidate alias.

    Uses crc32 (not ``hash``) so the alias→color mapping is stable across
    processes and sessions.
    """
    return ALIAS_PALETTE[zlib.crc32(alias.encode("utf-8")) % len(ALIAS_PALETTE)]


def local_dt(epoch_s: int | float | None) -> datetime | None:
    if epoch_s is None:
        return None
    return datetime.fromtimestamp(epoch_s, LOCAL_TZ).replace(tzinfo=None)


def local_str(epoch_s: int | float | None) -> str:
    """Local time with tz abbreviation — unambiguous across the DST repeat."""
    if epoch_s is None:
        return "?"
    return datetime.fromtimestamp(epoch_s, LOCAL_TZ).strftime("%H:%M:%S %Z")


def ping_frame(ci: ChunkWindow | None) -> pl.DataFrame | None:
    """Time-sorted, position-complete pings for a chunk window, or None."""
    if ci is None or ci.pings is None or ci.pings.is_empty():
        return None
    need = {"latitude", "longitude", "epoch_s"}
    if not need <= set(ci.pings.columns):
        return None
    p = ci.pings.drop_nulls(["latitude", "longitude", "epoch_s"]).sort("epoch_s")
    return p if not p.is_empty() else None


def hover_times(p: pl.DataFrame) -> list[str]:
    return [local_str(e) for e in p["epoch_s"].to_list()]


def column_list(frame: pl.DataFrame, name: str, default: object = None) -> list:
    """Column as a list, or a list of defaults when the column is absent."""
    if name in frame.columns:
        return frame[name].to_list()
    return [default] * frame.height


class TimeAxis:
    """Maps epochs to plot x values.

    Normally wall-clock local datetimes. When the plotted range crosses a
    UTC-offset change (the DST fall-back repeats a wall hour), wall-clock
    positions lie: bars shrink an hour and one-hour gaps render as seconds.
    In that case the axis switches to true elapsed minutes since chunk
    start, with local times carried in hover text instead.

    Epochs are collected from the chunk/prev/next windows, every candidate
    scheduled window in ``bundle.candidates``, the APC extents, and any
    ``extra_epochs`` the figure adds (e.g. proposed intervals).
    """

    def __init__(
        self, b: CaseBundleV2, extra_epochs: Iterable[int | None] | None = None
    ) -> None:
        epochs: list[int] = []
        for ci in (b.prev_chunk, b.chunk, b.next_chunk):
            if ci is not None:
                epochs += [e for e in (ci.start_epoch, ci.end_epoch) if e is not None]
        for c in b.candidates:
            epochs += [
                e for e in (c.sched_start_epoch, c.sched_end_epoch) if e is not None
            ]
        apc = b.apc_events
        if apc is not None and not apc.is_empty() and "arrival_epoch_s" in apc.columns:
            arr = apc["arrival_epoch_s"].drop_nulls()
            if not arr.is_empty():
                epochs += [int(arr.min()), int(arr.max())]
        if extra_epochs is not None:
            epochs += [int(e) for e in extra_epochs if e is not None]
        self.origin = (
            b.chunk.start_epoch
            if b.chunk is not None and b.chunk.start_epoch is not None
            else (min(epochs) if epochs else 0)
        )
        offsets = {datetime.fromtimestamp(e, LOCAL_TZ).utcoffset() for e in epochs}
        self.elapsed = len(offsets) > 1

    def x(self, epoch_s: int | float | None):
        if epoch_s is None:
            return None
        if self.elapsed:
            return (epoch_s - self.origin) / 60.0
        return local_dt(epoch_s)

    def xs(self, seq: Iterable[int | float | None]) -> list:
        return [self.x(e) for e in seq]

    @property
    def title(self) -> str:
        if self.elapsed:
            return "minutes since chunk start (clocks change in this window)"
        return "local time"

    @property
    def hovertime(self) -> str:
        # plotly time formatting only applies to datetime axes
        return "%{x|%H:%M:%S}" if not self.elapsed else "%{x:.1f} min"

    def annotate(self, fig: go.Figure) -> None:
        if self.elapsed:
            fig.add_annotation(
                xref="paper",
                yref="paper",
                x=0,
                y=1.08,
                showarrow=False,
                text=(
                    "⚠ 本窗口内时钟发生变更 —— 横轴显示的是真实经过时间；"
                    "悬停可查看当地时刻"
                ),
                font=dict(size=11, color="#b26a00"),
            )


def add_current_window(
    fig: go.Figure,
    axis: TimeAxis,
    chunk: ChunkWindow | None,
    *,
    label: str = "当前标注窗口",
) -> None:
    """Shade the current labeling window (the critical visual contract)."""
    if chunk is None or chunk.start_epoch is None or chunk.end_epoch is None:
        return
    fig.add_vrect(
        x0=axis.x(chunk.start_epoch),
        x1=axis.x(chunk.end_epoch),
        fillcolor=WINDOW_FILL,
        opacity=0.12,
        line_width=0,
        annotation_text=label,
        annotation_position="top left",
        annotation_font=dict(size=10, color="#444444"),
    )


def add_context_note(fig: go.Figure) -> None:
    """Spell out the current-vs-adjacent convention on timeline figures."""
    fig.add_annotation(
        xref="paper",
        yref="paper",
        x=1.0,
        y=1.14,
        xanchor="right",
        showarrow=False,
        text=("阴影区 = 当前标注窗口 · 灰色/虚线 = 相邻上下文"),
        font=dict(size=10, color="#666666"),
    )


def safe_plot(
    builder: Callable[..., go.Figure | None],
    *args,
    empty_msg: str,
    **kwargs,
) -> None:
    """Call ``builder`` and render the result into streamlit.

    ``None`` → ``st.info(empty_msg)``; an exception → ``st.warning`` with
    the message; otherwise the figure is drawn full-width. Streamlit is
    imported lazily so importing this module never requires it.
    """
    import streamlit as st

    try:
        fig = builder(*args, **kwargs)
    except Exception as exc:
        st.warning(f"could not draw this panel: {exc}")
        return
    if fig is None:
        st.info(empty_msg)
        return
    st.plotly_chart(fig, use_container_width=True)
