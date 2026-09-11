"""Plotly figures for the evidence panels.

All builders take a CaseBundle and return a ``go.Figure`` or ``None`` when
the required inputs are missing; the app shows "not available" for None.
Tiles are never fetched — the map is a plain lat/lon scatter with a
locked aspect ratio, which works fully offline.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import plotly.graph_objects as go
import polars as pl

from . import geometry
from .parsing import LOCAL_TZ, fmt_duration_s
from .resolver import CandidateBundle, CaseBundle, ChunkInfo

LETTER_COLORS = {
    "A": "#1f77b4",
    "B": "#ff7f0e",
    "C": "#2ca02c",
    "D": "#d62728",
    "E": "#9467bd",
}
PREV_COLOR = "#b0b0b0"
NEXT_COLOR = "#7f7f7f"


def _lc(letter: str) -> str:
    return LETTER_COLORS.get(letter, "#8c564b")


def _local_dt(epoch_s) -> datetime | None:
    if epoch_s is None:
        return None
    return datetime.fromtimestamp(epoch_s, LOCAL_TZ).replace(tzinfo=None)


def _local_str(epoch_s) -> str:
    """Local time with tz abbreviation — unambiguous across the DST repeat."""
    if epoch_s is None:
        return "?"
    return datetime.fromtimestamp(epoch_s, LOCAL_TZ).strftime("%H:%M:%S %Z")


class TimeAxis:
    """Maps epochs to plot x values.

    Normally wall-clock local datetimes. When the case's plotted range
    crosses a UTC-offset change (the DST fall-back repeats a wall hour),
    wall-clock positions lie: bars shrink an hour and one-hour gaps render
    as seconds. In that case the axis switches to true elapsed minutes since
    chunk start, with local times carried in hover text instead.
    """

    def __init__(self, b: CaseBundle):
        epochs: list[int] = []
        for ci in (b.prev_chunk, b.chunk, b.next_chunk):
            if ci is not None:
                epochs += [e for e in (ci.start_epoch, ci.end_epoch) if e is not None]
        for c in b.candidates:
            m = c.metrics
            epochs += [
                e for e in (m.sched_start_epoch, m.sched_end_epoch) if e is not None
            ]
        if b.apc_events is not None and not b.apc_events.is_empty():
            arr = b.apc_events["arrival_epoch_s"].drop_nulls()
            if not arr.is_empty():
                epochs += [int(arr.min()), int(arr.max())]
        self.origin = (
            b.chunk.start_epoch
            if b.chunk is not None and b.chunk.start_epoch is not None
            else (min(epochs) if epochs else 0)
        )
        offsets = {
            datetime.fromtimestamp(e, LOCAL_TZ).utcoffset() for e in epochs
        }
        self.elapsed = len(offsets) > 1

    def x(self, epoch_s):
        if epoch_s is None:
            return None
        if self.elapsed:
            return (epoch_s - self.origin) / 60.0
        return _local_dt(epoch_s)

    def xs(self, seq) -> list:
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
                    "⚠ clocks change within this window — axis shows true "
                    "elapsed time; hover for local times"
                ),
                font=dict(size=11, color="#b26a00"),
            )


def _ping_frame(ci: ChunkInfo | None) -> pl.DataFrame | None:
    if ci is None or ci.pings is None or ci.pings.is_empty():
        return None
    need = {"latitude", "longitude", "epoch_s"}
    if not need <= set(ci.pings.columns):
        return None
    p = ci.pings.drop_nulls(["latitude", "longitude"]).sort("epoch_s")
    return p if not p.is_empty() else None


def _hover_times(p: pl.DataFrame) -> list[str]:
    return [_local_str(e) for e in p["epoch_s"].to_list()]


# ---------------------------------------------------------------------------
# Panel A: map


def fig_map(
    b: CaseBundle,
    show_prev: bool = True,
    show_next: bool = True,
    show_candidates: set[str] | None = None,
    show_stops: bool = True,
    show_apc: bool = True,
) -> go.Figure | None:
    cur = _ping_frame(b.chunk)
    if cur is None and not b.candidates:
        return None
    show_candidates = (
        show_candidates
        if show_candidates is not None
        else {c.letter for c in b.candidates}
    )
    fig = go.Figure()

    for c in b.candidates:
        if c.letter not in show_candidates:
            continue
        _add_candidate_geometry(fig, c, show_stops)

    for flag, ci, color, name in [
        (show_prev, b.prev_chunk, PREV_COLOR, "previous chunk"),
        (show_next, b.next_chunk, NEXT_COLOR, "next chunk"),
    ]:
        p = _ping_frame(ci) if flag else None
        if p is not None:
            fig.add_trace(
                go.Scatter(
                    x=p["longitude"],
                    y=p["latitude"],
                    mode="lines",
                    line=dict(color=color, width=1.5, dash="dot"),
                    name=name,
                    text=_hover_times(p),
                    hovertemplate="%{text}<extra>" + name + "</extra>",
                )
            )

    if cur is not None:
        n = cur.height
        fig.add_trace(
            go.Scatter(
                x=cur["longitude"],
                y=cur["latitude"],
                mode="lines+markers",
                line=dict(color="#444", width=1),
                marker=dict(
                    size=5,
                    color=list(range(n)),
                    colorscale="Viridis",
                    showscale=True,
                    colorbar=dict(
                        title="time →", tickvals=[0, n - 1], ticktext=["start", "end"]
                    ),
                ),
                name="chunk AVL",
                text=_hover_times(cur),
                hovertemplate="%{text}<extra>chunk AVL</extra>",
            )
        )
        for idx, label, color in [(0, "start", "#00a000"), (n - 1, "end", "#d00000")]:
            fig.add_trace(
                go.Scatter(
                    x=[cur["longitude"][idx]],
                    y=[cur["latitude"][idx]],
                    mode="markers+text",
                    marker=dict(size=13, symbol="circle-open", color=color,
                                line=dict(width=3)),
                    text=[label],
                    textposition="top center",
                    showlegend=False,
                    hoverinfo="skip",
                )
            )

    if show_apc and b.apc_events is not None and not b.apc_events.is_empty():
        apc = b.apc_events.drop_nulls(["latitude", "longitude"])
        if not apc.is_empty():
            hover = [
                f"{_local_str(e)} stop {sc} +{bo or 0}/-{al or 0}"
                for e, sc, bo, al in zip(
                    apc["arrival_epoch_s"].to_list(),
                    apc["source_stop_code"].to_list(),
                    apc["boarding"].to_list(),
                    apc["alighting"].to_list(),
                )
            ]
            fig.add_trace(
                go.Scatter(
                    x=apc["longitude"],
                    y=apc["latitude"],
                    mode="markers",
                    marker=dict(size=9, symbol="x-thin",
                                line=dict(width=2, color="#e6194b")),
                    name="APC events (±30 min)",
                    text=hover,
                    hovertemplate="%{text}<extra>APC</extra>",
                )
            )

    lat0 = None
    if cur is not None:
        lat0 = float(cur["latitude"].mean())
    elif b.candidates and b.candidates[0].shape is not None:
        lat0 = float(b.candidates[0].shape["shape_pt_lat"].mean())
    ratio = 1.0 / max(0.2, math.cos(math.radians(lat0))) if lat0 else 1.35
    fig.update_layout(
        height=520,
        margin=dict(l=10, r=10, t=10, b=10),
        yaxis=dict(scaleanchor="x", scaleratio=ratio, title="lat"),
        xaxis=dict(title="lon"),
        legend=dict(orientation="h", yanchor="bottom", y=1.01),
        dragmode="pan",
    )
    return fig


def _add_candidate_geometry(
    fig: go.Figure, c: CandidateBundle, show_stops: bool
) -> None:
    color = _lc(c.letter)
    if c.shape is not None and not c.shape.is_empty():
        fig.add_trace(
            go.Scatter(
                x=c.shape["shape_pt_lon"],
                y=c.shape["shape_pt_lat"],
                mode="lines",
                line=dict(color=color, width=3),
                opacity=0.35,
                name=f"candidate {c.letter} shape",
                hoverinfo="skip",
            )
        )
    se = c.sched_events
    if (
        show_stops
        and se is not None
        and not se.is_empty()
        and {"stop_lat", "stop_lon"} <= set(se.columns)
    ):
        s = se.drop_nulls(["stop_lat", "stop_lon"])
        if s.is_empty():
            return
        hover = [
            f"{c.letter}: seq {q} {nm} ({sc}) sched {_local_str(a)}"
            for q, nm, sc, a in zip(
                s["stop_sequence"].to_list(),
                s["stop_name"].to_list(),
                s["stop_code"].to_list(),
                s["arrival_epoch_s"].to_list(),
            )
        ]
        fig.add_trace(
            go.Scatter(
                x=s["stop_lon"],
                y=s["stop_lat"],
                mode="markers",
                marker=dict(size=5, color=color, opacity=0.5),
                name=f"candidate {c.letter} stops",
                text=hover,
                hovertemplate="%{text}<extra></extra>",
            )
        )
        # terminals
        fig.add_trace(
            go.Scatter(
                x=[s["stop_lon"][0], s["stop_lon"][-1]],
                y=[s["stop_lat"][0], s["stop_lat"][-1]],
                mode="markers",
                marker=dict(size=13, symbol="star", color=color, opacity=0.8),
                name=f"candidate {c.letter} terminals",
                showlegend=False,
                hovertemplate=f"terminal ({c.letter})<extra></extra>",
            )
        )


# ---------------------------------------------------------------------------
# Panel B1: context timeline


def fig_timeline(b: CaseBundle) -> go.Figure | None:
    if b.chunk is None or b.chunk.start_epoch is None:
        return None
    axis = TimeAxis(b)
    fig = go.Figure()
    rows: list[tuple[str, int, int, str, str]] = []  # (row label, s, e, color, note)

    # NOTE: chunk roles from x_refined_chunks are model output — diagnostics
    # only, never in this neutral panel
    for ci, color, name in [
        (b.prev_chunk, PREV_COLOR, "prev"),
        (b.chunk, "#333333", "current"),
        (b.next_chunk, NEXT_COLOR, "next"),
    ]:
        if ci is not None and ci.start_epoch is not None and ci.end_epoch is not None:
            note = f"{name}: {fmt_duration_s(ci.end_epoch - ci.start_epoch)}"
            rows.append(("AVL chunks", ci.start_epoch, ci.end_epoch, color, note))

    for c in b.candidates:
        m = c.metrics
        if m.sched_start_epoch is not None and m.sched_end_epoch is not None:
            rows.append(
                (
                    f"cand {c.letter}",
                    m.sched_start_epoch,
                    m.sched_end_epoch,
                    _lc(c.letter),
                    f"candidate {c.letter} scheduled window",
                )
            )

    for label, s, e, color, note in rows:
        fig.add_trace(
            go.Scatter(
                x=[axis.x(s), axis.x(e)],
                y=[label, label],
                mode="lines",
                line=dict(color=color, width=14),
                name=note,
                text=[_local_str(s), _local_str(e)],
                hovertemplate=f"{note}<br>%{{text}}<extra></extra>",
                opacity=0.75 if label != "AVL chunks" else 1.0,
                showlegend=False,
            )
        )

    # gaps between context chunks
    for a, z in [(b.prev_chunk, b.chunk), (b.chunk, b.next_chunk)]:
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
                text=f"gap {fmt_duration_s(z.start_epoch - a.end_epoch)}",
                showarrow=False,
                yshift=18,
                font=dict(size=10, color="#666"),
            )

    if b.apc_events is not None and not b.apc_events.is_empty():
        apc = b.apc_events.drop_nulls(["arrival_epoch_s"])
        arr = apc["arrival_epoch_s"].to_list()
        fig.add_trace(
            go.Scatter(
                x=axis.xs(arr),
                y=["APC"] * apc.height,
                mode="markers",
                marker=dict(symbol="line-ns-open", size=11,
                            line=dict(width=1.5, color="#e6194b")),
                name="APC events",
                text=[
                    f"stop {sc} {_local_str(e)}"
                    for sc, e in zip(apc["source_stop_code"].to_list(), arr)
                ],
                hovertemplate="%{text}<extra>APC</extra>",
                showlegend=False,
            )
        )

    order = ["APC", "AVL chunks"] + [
        f"cand {c.letter}" for c in reversed(b.candidates)
    ]
    fig.update_layout(
        height=90 + 42 * (2 + len(b.candidates)),
        margin=dict(l=10, r=10, t=30, b=10),
        yaxis=dict(categoryorder="array", categoryarray=order),
        xaxis=dict(title=axis.title),
    )
    axis.annotate(fig)
    return fig


# ---------------------------------------------------------------------------
# Panel B2: time–distance


def fig_time_distance(b: CaseBundle, ref_letter: str | None) -> go.Figure | None:
    """x = time, y = arc position along the reference candidate's shape.

    Falls back to cumulative distance from chunk start when no candidate
    shape is available.
    """
    cur = _ping_frame(b.chunk)
    if cur is None:
        return None
    axis = TimeAxis(b)

    ref = next((c for c in b.candidates if c.letter == ref_letter), None)
    if ref is None or ref.shape is None or ref.shape.is_empty():
        ref = next(
            (c for c in b.candidates if c.shape is not None and not c.shape.is_empty()),
            None,
        )

    fig = go.Figure()
    if ref is None:
        # fallback: cumulative along-track distance of the chunk itself
        dist = geometry.cumulative_distance_m(
            cur["latitude"].to_numpy(), cur["longitude"].to_numpy()
        )
        fig.add_trace(
            go.Scatter(
                x=axis.xs(cur["epoch_s"].to_list()),
                y=dist / 1000.0,
                mode="markers",
                marker=dict(size=4, color="#333"),
                name="chunk AVL (cumulative km)",
                text=_hover_times(cur),
                hovertemplate="%{text}<br>%{y:.2f} km<extra></extra>",
            )
        )
        fig.update_layout(
            height=420,
            margin=dict(l=10, r=10, t=30, b=10),
            title="No candidate shape available — cumulative distance fallback",
            xaxis=dict(title=axis.title),
            yaxis=dict(title="km from chunk start"),
        )
        axis.annotate(fig)
        return fig

    ref_lat = float(cur["latitude"].mean())
    sx, sy = geometry.to_xy(
        ref.shape["shape_pt_lat"].to_numpy(),
        ref.shape["shape_pt_lon"].to_numpy(),
        ref_lat,
    )

    def arc_km(p: pl.DataFrame) -> np.ndarray:
        px, py = geometry.to_xy(
            p["latitude"].to_numpy(), p["longitude"].to_numpy(), ref_lat
        )
        arc, _dist = geometry.project_points_onto_polyline(px, py, sx, sy)
        return arc / 1000.0

    for ci, color, name in [
        (b.prev_chunk, PREV_COLOR, "previous chunk"),
        (b.next_chunk, NEXT_COLOR, "next chunk"),
    ]:
        p = _ping_frame(ci)
        if p is not None:
            fig.add_trace(
                go.Scatter(
                    x=axis.xs(p["epoch_s"].to_list()),
                    y=arc_km(p),
                    mode="markers",
                    marker=dict(size=3, color=color),
                    name=name,
                    text=_hover_times(p),
                    hovertemplate="%{text}<extra>" + name + "</extra>",
                )
            )

    fig.add_trace(
        go.Scatter(
            x=axis.xs(cur["epoch_s"].to_list()),
            y=arc_km(cur),
            mode="markers",
            marker=dict(
                size=5, color=list(range(cur.height)), colorscale="Viridis"
            ),
            name="chunk AVL",
            text=_hover_times(cur),
            hovertemplate="%{text}<extra>chunk AVL</extra>",
        )
    )

    for c in b.candidates:
        se = c.sched_events
        if (
            se is None
            or se.is_empty()
            or not ({"stop_lat", "stop_lon"} <= set(se.columns))
        ):
            continue
        s = se.drop_nulls(["stop_lat", "stop_lon", "arrival_epoch_s"])
        if s.is_empty():
            continue
        qx, qy = geometry.to_xy(
            s["stop_lat"].to_numpy(), s["stop_lon"].to_numpy(), ref_lat
        )
        arc, _ = geometry.project_points_onto_polyline(qx, qy, sx, sy)
        arrs = s["arrival_epoch_s"].to_list()
        fig.add_trace(
            go.Scatter(
                x=axis.xs(arrs),
                y=arc / 1000.0,
                mode="lines+markers",
                line=dict(color=_lc(c.letter), width=1.5, dash="dash"),
                marker=dict(size=4, color=_lc(c.letter)),
                opacity=0.7,
                name=f"candidate {c.letter} schedule",
                text=[_local_str(e) for e in arrs],
                hovertemplate="%{text}<extra>cand " + c.letter + "</extra>",
            )
        )

    if b.apc_events is not None and not b.apc_events.is_empty():
        apc = b.apc_events.drop_nulls(["latitude", "longitude", "arrival_epoch_s"])
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
                    marker=dict(size=8, symbol="x-thin",
                                line=dict(width=2, color="#e6194b")),
                    name="APC events",
                    text=[_local_str(e) for e in arrs],
                    hovertemplate="%{text}<extra>APC</extra>",
                )
            )

    fig.update_layout(
        height=460,
        margin=dict(l=10, r=10, t=30, b=10),
        title=f"progress along candidate {ref.letter}'s shape",
        xaxis=dict(title=axis.title),
        yaxis=dict(title=f"km along shape ({ref.letter})"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    axis.annotate(fig)
    return fig


# ---------------------------------------------------------------------------
# Panel C: stop ladder


def fig_stop_ladder(b: CaseBundle, c: CandidateBundle) -> go.Figure | None:
    """Scheduled stops in sequence order vs time; observed AVL pass times and
    APC hits mapped onto them."""
    se = c.sched_events
    if se is None or se.is_empty():
        return None
    axis = TimeAxis(b)
    s = se.sort("stop_sequence")
    n = s.height
    y = list(range(n))
    names = [
        f"{q}· {nm}" for q, nm in zip(s["stop_sequence"].to_list(),
                                      s["stop_name"].to_list())
    ]

    fig = go.Figure()
    sched_arrs = s["arrival_epoch_s"].to_list()
    fig.add_trace(
        go.Scatter(
            x=axis.xs(sched_arrs),
            y=y,
            mode="lines+markers",
            line=dict(color=_lc(c.letter), width=1.5),
            marker=dict(size=5, color=_lc(c.letter)),
            name="scheduled arrival",
            text=[_local_str(e) for e in sched_arrs],
            hovertemplate="%{text}<extra>scheduled</extra>",
        )
    )

    # observed pass time: nearest chunk ping within coverage radius of the stop
    cur = _ping_frame(b.chunk)
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
                if math.sqrt(float(d2[j])) <= 150.0:
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
                        name="AVL passes stop (≤150 m)",
                        text=[_local_str(e) for e in obs_e],
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

    # APC hits by stop code
    if (
        b.apc_events is not None
        and not b.apc_events.is_empty()
        and "stop_code" in s.columns
    ):
        codes = {str(v): i for i, v in enumerate(s["stop_code"].to_list())}
        apc = b.apc_events.with_columns(
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
                    marker=dict(size=11, symbol="circle-open",
                                line=dict(width=2, color="#e6194b")),
                    name="APC at this stop code",
                    text=[_local_str(e) for e in arrs],
                    hovertemplate="%{text}<extra>APC</extra>",
                )
            )

    # chunk window shading
    if b.chunk and b.chunk.start_epoch is not None and b.chunk.end_epoch is not None:
        fig.add_vrect(
            x0=axis.x(b.chunk.start_epoch),
            x1=axis.x(b.chunk.end_epoch),
            fillcolor="#888",
            opacity=0.12,
            line_width=0,
            annotation_text="chunk",
            annotation_position="top left",
        )

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
