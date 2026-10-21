"""Blind second-pass labeling UI — human labeling protocol v2, run01.

Grain-first, task-specific, blind throughout: first-pass labels and model
diagnostics stay hidden until the blind judgment is saved; the candidate
universe is presented complete and in neutral order; every reveal and every
filter is logged. The real enforcement lives in
``r1spike.labeling_v2_store`` — this app is only an honest thin shell on top
of it.

Display labels are UI-only; stored values are always kept as the raw enum
values.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from uiv2.bootstrap import ensure_r1spike_importable  # noqa: E402

ensure_r1spike_importable()

import streamlit as st  # noqa: E402
from r1spike.labeling_v2_store import (  # noqa: E402
    BlindProtocolViolation,
    LabelValidationError,
    ReviewStore,
    derive_timezone_label,
    utc_now_iso,
)

from uiv2 import adapters, state  # noqa: E402
from uiv2.case_loader import CaseBundleV2, build_bundle  # noqa: E402
from uiv2.config import AppConfig, resolve_config  # noqa: E402
from uiv2.tables import show_table  # noqa: E402
from uiv2.views import forms, post_save  # noqa: E402
from uiv2.views.candidates import render_candidate_browser  # noqa: E402
from uiv2.views.common_context import (  # noqa: E402
    NEUTRAL_TASK_LABEL,
    render_context_strip,
    render_evidence_panels,
)

GRAIN_BY_TASK = {
    "scheduled_trip_second_pass": "single_trip_candidate",
    "scheduled_trip_abstention": "single_trip_candidate",
    "segmentation_multi_trip": "multi_trip_chunk",
    "boundary_spanning_dual_role": "boundary_spanning",
    "fragment_role_and_stitching": "local_fragment",
    "insufficient_context": "local_fragment",
    "source_time_quality": "data_quality_invalid",
    "no_match_activity": "turnaround_or_idle",
}

CHILD_GRAIN_BY_TASK = {
    "subtrip_scheduled_match": "single_trip_candidate",
    "boundary_interval": "boundary_spanning",
    "inbound_context": "boundary_spanning",
    "outbound_context": "boundary_spanning",
    "fragment_role": "local_fragment",
    "stitch_decision": "local_fragment",
    "sequence_review": "multi_trip_chunk",
    "source_time_quality": "data_quality_invalid",
    "activity_state": "turnaround_or_idle",
}

TASK_LABELS = {
    "scheduled_trip_second_pass": "Scheduled-trip blind second pass",
    "segmentation_multi_trip": "Multi-trip segmentation review",
    "boundary_spanning_dual_role": "Boundary-spanning dual-role review",
    "fragment_role_and_stitching": "Fragment role and stitching review",
    "insufficient_context": "Insufficient-context fragment review",
    "source_time_quality": "Source data / time quality review",
    "no_match_activity": "No-match activity review",
}


@dataclass
class StoreContext:
    config: AppConfig
    artifact_store: adapters.ArtifactStoreV2
    review_store: ReviewStore


def get_context() -> StoreContext | None:
    if "v2_config" not in st.session_state:
        st.session_state["v2_config"] = resolve_config()
    config: AppConfig = st.session_state["v2_config"]
    for note in config.notes:
        st.warning(note)
    reviewer = st.sidebar.text_input(
        "Reviewer ID (pseudonym)",
        value=config.reviewer,
        key="reviewer_id",
    ).strip()
    if not reviewer:
        st.sidebar.info("Enter a pseudonymous reviewer ID to begin")
        return None
    context_key = f"v2_ctx::{config.run_dir}::{reviewer}"
    if context_key not in st.session_state:
        artifact_store = adapters.discover_v2(
            config.run_dir,
            spike02r_dir=config.spike02r_dir,
            artifacts_root=config.artifacts_root,
            audit_dir=config.audit_dir,
        )
        try:
            review_store = ReviewStore(
                config.run_dir,
                first_pass_path=config.audit_dir / "first_pass_labels_v1.parquet",
                reviewer_id=reviewer,
            )
        except (BlindProtocolViolation, OSError) as error:
            st.error(f"Could not open the run directory: {error}")
            return None
        st.session_state[context_key] = StoreContext(
            config=config,
            artifact_store=artifact_store,
            review_store=review_store,
        )
    return st.session_state[context_key]


def get_bundle(ctx: StoreContext, case_id: str) -> CaseBundleV2:
    cache_key = f"bundle::{case_id}"
    if cache_key not in st.session_state:
        review = ctx.review_store
        root_id = case_id.split("::")[0]
        routing_rows = review.routing.filter(review.routing["case_id"] == root_id)
        routing_row = routing_rows.row(0, named=True)
        child_row = None
        if "::" in case_id:
            child_rows = review.children.filter(
                review.children["child_case_id"] == case_id
            )
            child_row = child_rows.row(0, named=True)
        with st.spinner("Assembling case evidence…"):
            st.session_state[cache_key] = build_bundle(
                ctx.artifact_store,
                case_id,
                routing_row=routing_row,
                child_row=child_row,
            )
    return st.session_state[cache_key]


def _status_icon(case_state) -> str:
    if "ADJUDICATED" in case_state.states:
        return "✓⚖"
    if "REQUIRES_ADJUDICATION" in case_state.states:
        return "⚖"
    if case_state.second_pass_saved:
        return "●"
    return "○"


def render_sidebar(ctx: StoreContext) -> str | None:
    review = ctx.review_store
    states = review.all_states()
    order = review.blind_order["case_order"]
    st.sidebar.markdown("### Blind queue")
    st.sidebar.caption("Fixed seeded order — not grouped by any existing label")
    parents_saved = sum(1 for c in order if states[c].second_pass_saved)
    st.sidebar.progress(parents_saved / len(order))
    st.sidebar.caption(f"Parent cases saved {parents_saved}/{len(order)}")
    options: list[str] = []
    labels: dict[str, str] = {}
    for position, case_id in enumerate(order, start=1):
        icon = _status_icon(states[case_id])
        options.append(case_id)
        labels[case_id] = f"{icon} {position:02d} · {case_id}"
        if states[case_id].second_pass_saved:
            children = review.children.filter(
                review.children["parent_case_id"] == case_id
            )
            for child in children.sort("child_case_id").iter_rows(named=True):
                child_id = child["child_case_id"]
                child_icon = _status_icon(states[child_id])
                options.append(child_id)
                labels[child_id] = f"  {child_icon} └ {child_id.split('::')[1]}"
    selected = st.sidebar.radio(
        "Case",
        options,
        format_func=lambda value: labels[value],
        key="case_select",
        label_visibility="collapsed",
    )

    def _nav(delta: int) -> None:
        current = st.session_state.get("case_select")
        position = options.index(current) if current in options else 0
        position = max(0, min(len(options) - 1, position + delta))
        st.session_state["case_select"] = options[position]

    prev_col, next_col = st.sidebar.columns(2)
    index = options.index(selected) if selected in options else 0
    prev_col.button(
        "◀ Previous", key="nav_prev", disabled=index == 0, on_click=_nav, args=(-1,)
    )
    next_col.button(
        "Next ▶",
        key="nav_next",
        disabled=index >= len(options) - 1,
        on_click=_nav,
        args=(1,),
    )
    st.sidebar.caption(
        "Child cases unlock after their parent's blind save; every case always "
        "keeps the null option and the complete candidate universe"
    )
    return selected


def _task_for(ctx: StoreContext, bundle: CaseBundleV2) -> str:
    if bundle.is_child and bundle.child_row is not None:
        return bundle.child_row["child_task_type"]
    return bundle.routing["initial_task_route"]


def _proposed_boundary_interval(case_id: str) -> tuple[int, int] | None:
    """The boundary form's in-progress interval, so the plots can draw it."""

    low = st.session_state.get(f"bd::lo::{case_id}")
    high = st.session_state.get(f"bd::hi::{case_id}")
    if low is None or high is None or int(high) < int(low):
        return None
    return int(low), int(high)


def _grain_baseline(ctx: StoreContext, bundle: CaseBundleV2) -> str | None:
    task = _task_for(ctx, bundle)
    if bundle.is_child:
        return CHILD_GRAIN_BY_TASK.get(task)
    return GRAIN_BY_TASK.get(task)


def render_task_form(
    bundle: CaseBundleV2,
    grain: str,
    visible: list,
) -> dict | None:
    if grain == "single_trip_candidate":
        return forms.render_scheduled_form(bundle, visible)
    if grain == "multi_trip_chunk":
        return forms.render_multi_trip_form(bundle, visible)
    if grain == "boundary_spanning":
        start = bundle.chunk.start_epoch

        def preview(rel_s: int) -> str | None:
            return derive_timezone_label(start + rel_s) if start is not None else None

        return forms.render_boundary_form(bundle, visible, derived_tz_preview=preview)
    if grain == "local_fragment":
        return forms.render_fragment_form(bundle, visible)
    if grain == "data_quality_invalid":
        return forms.render_quality_form(bundle)
    if grain == "turnaround_or_idle":
        return forms.render_activity_form(bundle)
    st.markdown("### 2 · Minimal judgment")
    st.caption("This grain has no dedicated form; record confidence and rationale.")
    record: dict = {}
    record.update(forms.confidence_block(bundle.case_id, include_match=False))
    record.update(forms.rationale_block(bundle.case_id, for_match=False))
    return record


def render_child_seeds(bundle: CaseBundleV2) -> None:
    child = bundle.child_row or {}
    seeds = {
        key: child.get(key)
        for key in (
            "seed_note_time_references_rel_s",
            "seed_boundary_stop",
            "seed_boundary_offset_from_parent_end_s",
            "seed_display_card_references",
        )
        if child.get(key) not in (None, "")
    }
    if seeds:
        st.info(
            "Unverified, note-derived context (shown only because the parent's "
            "blind save exists; card letters in it refer to the **legacy v1** "
            "display numbering, not the aliases on this page): "
            + "; ".join(f"{key}={value}" for key, value in seeds.items())
        )


def render_case(ctx: StoreContext, case_id: str) -> None:
    review = ctx.review_store
    bundle = get_bundle(ctx, case_id)
    task = _task_for(ctx, bundle)
    case_state = review.case_state(case_id)
    # BLINDING: a parent's routed task name is a deterministic proxy for its
    # first-pass label class — never show it before the reviewer's own grain
    # answer (or the save).
    grain_answered = (
        st.session_state.get(f"grain::{case_id}") is not None
        or case_state.second_pass_saved
    )
    if bundle.is_child:
        task_label = f"Child case · {task}"
    elif grain_answered:
        task_label = TASK_LABELS.get(task, task)
    else:
        task_label = NEUTRAL_TASK_LABEL
    render_context_strip(bundle, task_label)
    if bundle.is_child:
        parent_state = review.case_state(bundle.parent_case_id)
        if not parent_state.second_pass_saved:
            st.warning(
                "This child case unlocks only after the parent's blind save is complete"
            )
            return
        render_child_seeds(bundle)

    started_at = state.note_review_started(case_id, utc_now_iso())
    detail_aliases = st.multiselect(
        "Load detailed candidate evidence (GTFS timetable + shape; heavy, so "
        "pick a few at a time)",
        [c.alias for c in bundle.real_candidates],
        placeholder="Select one or more candidate aliases",
        key=f"details::{case_id}",
        max_selections=6,
    )
    boundary_interval = _proposed_boundary_interval(case_id)
    render_evidence_panels(
        bundle,
        ctx,
        detail_aliases=detail_aliases,
        boundary_interval=boundary_interval,
    )
    browser = render_candidate_browser(bundle, review)

    if case_state.second_pass_saved:
        post_save.render_post_save(bundle, review, ctx)
        return

    if bundle.is_child:
        grain = CHILD_GRAIN_BY_TASK.get(task, "unknown")
        reviewability = None
        st.markdown(
            f"Child-case task grain: `{grain}` (fixed by the child-case manifest; "
            "cannot be changed)"
        )
    else:
        grain, reviewability = forms.render_grain_form(bundle)
        if grain is None:
            st.info(
                "The task form opens only after you answer the grain question above"
            )
            return
        baseline = _grain_baseline(ctx, bundle)
        if baseline is not None and grain != baseline:
            st.caption(
                "Your chosen grain differs from the queue baseline — at save time "
                "this is recorded separately as a routing_change judgment; the "
                "case is not relabeled"
            )
    record = render_task_form(bundle, grain, browser["visible"])
    if record is None:
        return
    adjacent_declared = st.checkbox(
        "My judgment relied on adjacent context (previous/next chunk, or "
        "out-of-window APC)",
        value=state.adjacent_context_viewed(case_id),
        key=f"adjdecl::{case_id}",
    )
    record.update(
        {
            "case_id": case_id,
            "case_grain": grain,
            "reviewability": reviewability
            if not bundle.is_child
            else record.get("reviewability"),
            "child_task_type": task if bundle.is_child else None,
            "adjacent_context_used": bool(
                adjacent_declared or state.adjacent_context_viewed(case_id)
            ),
            "panels_viewed": state.panels_viewed(case_id),
            "candidate_filters_applied": browser["filters"],
            "candidates_hidden_by_filter_at_save": browser["hidden_count"],
            "review_started_at": started_at,
            "blind_review_order_position": bundle.routing.get(
                "blind_review_order_position"
            ),
        }
    )
    st.divider()
    if st.button(
        "💾 Save (blind judgment; immutable)",
        key=f"save::{case_id}",
        type="primary",
    ):
        baseline = _grain_baseline(ctx, bundle)
        try:
            saved = review.save_second_pass(record)
        except (LabelValidationError, BlindProtocolViolation) as error:
            st.error(str(error))
            return
        if not bundle.is_child and baseline is not None and grain != baseline:
            review.record_routing_change(
                case_id,
                from_route=task,
                to_grain=grain,
                reason="reviewer grain-first decision at blind save",
            )
        outcome = (saved.get("comparison") or {}).get("outcome")
        st.success(
            "Blind judgment saved"
            + (f" — comparison with the first pass: {outcome}" if outcome else "")
        )
        st.rerun()


def main() -> None:
    st.set_page_config(page_title="Labeling v2 — blind second pass", layout="wide")
    st.title("scheduled_trip labeling — protocol v2 blind second pass")
    ctx = get_context()
    if ctx is None:
        return
    review = ctx.review_store
    st.caption(
        f"Protocol v{review.protocol_version} · frozen hash "
        f"{review.protocol_sha256[:12]}… · reviewer "
        f"{review.reviewer_id} · first-pass labels stay hidden until each case's "
        "blind save · factorial remains NO-GO"
    )
    case_id = render_sidebar(ctx)
    if case_id is None:
        st.info("Select a case from the blind queue on the left")
        return
    render_case(ctx, case_id)
    with st.expander("Run status (live artifacts)"):
        rows = [
            {
                "Case": row["case_id"],
                "Queue rank": row["queue_rank"],
                "Initial route": row["initial_task_route"],
                "Saved": row["second_pass_saved"],
                "Comparison outcome": row["comparison_outcome"],
                "Requires adjudication": row["requires_adjudication"],
            }
            for row in review.queue_rows()
        ]
        show_table(rows, max_rows=45)


main()
