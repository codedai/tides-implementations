"""Common context strip and evidence panels shared by every task page.

Shows operational metadata only: no packet ``summary``, no anchor status,
no model scores, no first-pass fields, and — for parents — no task or
routing vocabulary before the grain decision. The synthetic packet fields
``n_events``/``span_rel_s`` are deliberately absent.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import polars as pl
import streamlit as st

from .. import state
from ..case_loader import CandidateDetail, CaseBundleV2, load_candidate_detail
from ..parsing import fmt_duration_s, fmt_local
from ..plots import (
    fig_boundary,
    fig_map,
    fig_stop_ladder,
    fig_time_progress,
    fig_time_tracks,
    fig_timeline,
    safe_plot,
)

PANELS = ("map", "timeline", "time_progress", "stop_ladder", "time_tracks")

# Display name -> slug recorded in panels_viewed (the stored value does not
# depend on the UI language)
PANEL_SLUGS = {
    "Map": "map",
    "Timeline": "timeline",
    "Progress curve": "time_progress",
    "Stop ladder": "stop_ladder",
    "Time tracks": "time_tracks",
}

NEUTRAL_TASK_LABEL = (
    "Blind review in progress — the task name is revealed after you answer the "
    "grain question"
)


def render_context_strip(bundle: CaseBundleV2, task_label: str) -> None:
    routing = bundle.routing
    st.subheader(f"Case {bundle.case_id}")
    st.caption(f"Current task: {task_label}")
    columns = st.columns(4)
    duration = bundle.chunk.duration_s
    columns[0].metric(
        "Chunk duration", fmt_duration_s(duration) if duration else "none"
    )
    columns[1].metric("AVL pings", routing.get("avl_ping_count") or "none")
    columns[2].metric(
        "APC records",
        routing.get("apc_record_count")
        if routing.get("apc_record_count") is not None
        else "n/a",
        help=str(routing.get("apc_record_count_scope") or ""),
    )
    columns[3].metric("Candidates", routing.get("candidate_count") or 0)
    lines: list[str] = []
    if bundle.avl_route_labels:
        lines.append(f"Route labels observed: {', '.join(bundle.avl_route_labels)}")
    observed = None
    if bundle.apc_events is not None and "source_stop_code" in (
        bundle.apc_events.columns
    ):
        observed = bundle.apc_events["source_stop_code"].drop_nulls().n_unique()
    if observed is not None:
        lines.append(f"Distinct APC stop codes observed nearby: {observed}")
    before = routing.get("context_before_s")
    after = routing.get("context_after_s")
    lines.append(
        "Retrievable context: "
        f"before {fmt_duration_s(before) if before is not None else 'none'} / "
        f"after {fmt_duration_s(after) if after is not None else 'none'}"
        " (before = this chunk's start minus the previous chunk's start; "
        "after = the next chunk's end minus this chunk's end; "
        "'none' means there is no adjacent chunk on that side within the same "
        "run segment)"
    )
    if bundle.vehicle_id is not None:
        lines.append(
            f"Vehicle identification source: {bundle.vehicle_source} "
            "(vehicle ID is kept local only)"
        )
    if bundle.dst_crossing:
        lines.append(
            ":warning: This chunk window spans a clock change — plots switch to a "
            "true elapsed-time axis; wall-clock text is unreliable"
        )
    if routing.get("stratum"):
        lines.append(
            f"Sampling stratum: `{routing['stratum']}` — {_caveat_text(routing)}"
        )
    for note in bundle.availability.values():
        lines.append(f":warning: {note}")
    st.caption("  \n".join(lines))


# The frozen artifact stores the original English caveat text; this is a
# display-layer rewording only and never alters the artifact itself.
_STRATUM_CAVEAT_EN = "Original packet sampling stratum — not a human truth label."
_STRATUM_CAVEAT_DISPLAY = (
    "This is only the original packet's sampling stratum, not a human truth "
    "label; the stratum is determined solely by chunk-duration thresholds and "
    "carries no additional information."
)


def _caveat_text(routing) -> str:
    caveat = routing.get("stratum_caveat")
    if caveat == _STRATUM_CAVEAT_EN:
        return _STRATUM_CAVEAT_DISPLAY
    return str(caveat)


def _window_caption(bundle: CaseBundleV2) -> str:
    start, end = bundle.chunk.start_epoch, bundle.chunk.end_epoch
    if start is None or end is None:
        return "Chunk window unavailable"
    return f"Current labeling window: {fmt_local(start)} → {fmt_local(end)}"


def _without_adjacent(bundle: CaseBundleV2) -> CaseBundleV2:
    """A view of the bundle with all temporally adjacent evidence removed:
    no prev/next chunks and no out-of-window padded APC events."""

    apc = bundle.apc_events
    if (
        apc is not None
        and bundle.chunk.start_epoch is not None
        and bundle.chunk.end_epoch is not None
        and "arrival_epoch_s" in apc.columns
    ):
        apc = apc.filter(
            pl.col("arrival_epoch_s").is_between(
                bundle.chunk.start_epoch, bundle.chunk.end_epoch
            )
        )
    return replace(bundle, prev_chunk=None, next_chunk=None, apc_events=apc)


def render_evidence_panels(
    bundle: CaseBundleV2,
    store_ctx: Any,
    *,
    detail_aliases: list[str],
    boundary_interval: tuple[int, int] | None = None,
) -> dict[str, CandidateDetail]:
    """Render the evidence panels; returns loaded candidate details.

    Panels render one at a time behind a selector (not tabs) so the logged
    ``panels_viewed`` reflects what the reviewer actually opened. The
    adjacent-context toggle governs every panel: off means no prev/next
    chunk traces and no out-of-window APC anywhere, so the logged flag is
    honest in both directions.
    """

    details: dict[str, CandidateDetail] = {}
    for alias in detail_aliases:
        cache_key = f"detail::{bundle.case_id}::{alias}"
        if cache_key not in st.session_state:
            st.session_state[cache_key] = load_candidate_detail(
                store_ctx.artifact_store, bundle, alias
            )
        detail = st.session_state[cache_key]
        if detail is not None:
            details[alias] = detail
    st.caption(_window_caption(bundle))
    show_adjacent = st.toggle(
        "Show adjacent context (previous/next chunk, and APC within ±30 minutes)",
        value=False,
        key=f"adj::{bundle.case_id}",
        help=(
            "Adjacent context is drawn in a muted style, clearly distinguished "
            "from the current labeling window. Turning this on is logged, and "
            "the save form asks again whether your judgment relied on it."
        ),
    )
    if show_adjacent and (
        bundle.prev_chunk is not None
        or bundle.next_chunk is not None
        or bundle.apc_events is not None
    ):
        if not state.adjacent_context_viewed(bundle.case_id):
            store_ctx.review_store.record_interaction(
                bundle.case_id,
                kind="adjacent_context_view",
                detail={"source": "adjacent_context_toggle"},
            )
        state.note_adjacent_context(bundle.case_id)
    shown = bundle if show_adjacent else _without_adjacent(bundle)
    panel = st.radio(
        "Evidence panel",
        ["Map", "Timeline", "Progress curve", "Stop ladder", "Time tracks"],
        horizontal=True,
        key=f"panel::{bundle.case_id}",
        label_visibility="collapsed",
    )
    panel_slug = PANEL_SLUGS[panel]
    if panel_slug not in state.panels_viewed(bundle.case_id):
        store_ctx.review_store.record_interaction(
            bundle.case_id, kind="panel_view", detail={"panel": panel_slug}
        )
    state.note_panel_view(bundle.case_id, panel_slug)
    if panel == "Map":
        safe_plot(
            fig_map,
            shown,
            show_prev=show_adjacent,
            show_next=show_adjacent,
            show_candidate_shapes=details or None,
            empty_msg="No AVL pings available in this window; the map cannot be drawn",
        )
    elif panel == "Timeline":
        extra = (
            [
                {
                    "label": "Proposed boundary",
                    "start_epoch": (bundle.chunk.start_epoch or 0)
                    + boundary_interval[0],
                    "end_epoch": (bundle.chunk.start_epoch or 0) + boundary_interval[1],
                    "kind": "boundary",
                }
            ]
            if boundary_interval and bundle.chunk.start_epoch is not None
            else None
        )
        if not details:
            st.caption(
                "Load detailed candidate evidence above to draw their scheduled "
                "time windows here"
            )
        safe_plot(
            fig_timeline,
            shown,
            candidate_windows=[bundle.by_alias(a) for a in sorted(details)],
            extra_intervals=extra,
            empty_msg="No timeline data available",
        )
    elif panel == "Progress curve":
        reference = None
        if details:
            pick = st.selectbox(
                "Candidate shape to project travel progress onto",
                sorted(details),
                key=f"tp::{bundle.case_id}",
            )
            reference = details.get(pick)
        safe_plot(
            fig_time_progress,
            shown,
            reference,
            empty_msg="No progress data available",
        )
    elif panel == "Stop ladder":
        if details:
            pick = st.selectbox(
                "Candidate to draw the stop ladder for",
                sorted(details),
                key=f"ladder::{bundle.case_id}",
            )
            safe_plot(
                fig_stop_ladder,
                shown,
                details[pick],
                empty_msg="This candidate has no scheduled stops available",
            )
        else:
            st.info("Load a candidate's detailed evidence first to see its stop ladder")
    else:
        safe_plot(
            fig_time_tracks,
            shown,
            empty_msg=(
                "No wall-clock ambiguity in this window — competing time tracks "
                "are meaningful only around the DST fall-back hour"
            ),
        )
    if boundary_interval is not None and bundle.chunk.start_epoch is not None:
        state.note_panel_view(bundle.case_id, "boundary_plot")
        safe_plot(
            fig_boundary,
            shown,
            interval_rel_s=boundary_interval,
            empty_msg="No boundary context available",
        )
    return details
