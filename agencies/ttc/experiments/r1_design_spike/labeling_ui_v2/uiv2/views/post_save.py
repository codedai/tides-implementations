"""Post-save panel: comparison, logged reveals, revisions, adjudication.

Everything on this page exists only after the blind judgment is durably
saved. Reveals are explicit, logged actions; a post-reveal change of mind is
a new record that never overwrites the blind save; adjudication is a third
record that overwrites neither pass.
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from ..case_loader import CaseBundleV2, load_diagnostics
from ..tables import show_table

FIRST_PASS_FIELDS = (
    "raw_primary_label",
    "raw_selected_card",
    "raw_selected_candidate_id",
    "raw_confidence",
    "raw_rationale",
    "raw_notes",
)


def _blind_summary(save: dict[str, Any]) -> list[dict[str, Any]]:
    keys = (
        "case_grain",
        "reviewability",
        "scheduled_judgment",
        "selected_candidate_alias",
        "selected_candidate_key",
        "fragment_role",
        "activity_state",
        "quality_state",
        "grain_confidence",
        "match_confidence",
        "evidence_quality",
    )
    return [
        {"Field": key, "Your blind judgment": save.get(key)}
        for key in keys
        if save.get(key) is not None
    ]


def render_post_save(bundle: CaseBundleV2, review_store, store_ctx) -> None:
    case_id = bundle.case_id
    case_state = review_store.case_state(case_id)
    save = case_state.blind_save or {}
    st.success(
        "Blind judgment saved and immutable. Anything you do below is stored as "
        "a separate, logged record — the blind save itself is never altered."
    )
    show_table(_blind_summary(save))
    comparison = case_state.comparison
    if comparison is not None:
        outcome = comparison.get("outcome")
        st.markdown(f"**First / second pass comparison outcome:** `{outcome}`")
        st.caption(comparison.get("basis", ""))
        if outcome in ("FIRST_SECOND_DISAGREE", "NOT_EVALUABLE_AT_CURRENT_GRAIN"):
            st.warning(
                "This case has entered the adjudication queue. The system does "
                "not automatically favor either pass."
            )
    if bundle.is_child:
        st.caption("Child case: there is no first-pass judgment to compare against.")

    if not bundle.is_child:
        with st.expander("Reveal first-pass judgment (logged)"):
            reveal_key = f"fprevealed::{case_id}"
            if st.button(
                "Reveal this case's first-pass label",
                key=f"fpreveal::{case_id}",
            ):
                try:
                    st.session_state[reveal_key] = review_store.first_pass.for_case(
                        case_id, reviewer_id=review_store.reviewer_id
                    )
                except (KeyError, OSError) as error:
                    st.warning(f"First-pass record unavailable: {error}")
            first = st.session_state.get(reveal_key)
            if first:
                show_table(
                    [
                        {"Field": field, "First pass": first.get(field)}
                        for field in FIRST_PASS_FIELDS
                    ]
                )

    with st.expander("Reveal model diagnostics (logged)"):
        st.caption(
            "For learning and audit only. This reveal is written to the review "
            "history; your saved blind judgment is unaffected."
        )
        if st.button("Reveal diagnostics", key=f"diagreveal::{case_id}"):
            review_store.record_diagnostics_reveal(case_id)
            st.session_state[f"diagrevealed::{case_id}"] = True
        if st.session_state.get(f"diagrevealed::{case_id}"):
            diagnostics = load_diagnostics(store_ctx.artifact_store, bundle)
            anchor = diagnostics.get("anchor_decision")
            if anchor:
                show_table([anchor])
            scores = diagnostics.get("candidate_scores") or []
            if scores:
                show_table(scores, max_rows=20)
            if not anchor and not scores:
                st.info("No diagnostic artifacts available")

    with st.expander("Record a post-reveal change of mind (stored as a new record)"):
        st.caption(
            "If your judgment would differ after seeing the comparison outcome or "
            "the diagnostics, describe the revised judgment here. It is stored as "
            "a post_reveal_revision that only references the blind save record "
            "and never replaces it."
        )
        revised = st.text_area(
            "Revised judgment (free text; structured revisions happen in adjudication)",
            key=f"prtext::{case_id}",
        )
        if st.button("Record this post-reveal note", key=f"prsave::{case_id}"):
            if revised.strip():
                review_store.log_event(
                    {
                        "event": "post_reveal_revision",
                        "case_id": case_id,
                        "reviewer_id": review_store.reviewer_id,
                        "post_reveal_revision_of": save.get("record_id"),
                        "revised_judgment_text": revised.strip(),
                    }
                )
                st.success("Recorded as a separate post-reveal event")
            else:
                st.info("Write the revised judgment first")

    if "REQUIRES_ADJUDICATION" in case_state.states:
        with st.expander(
            "Adjudication (third record; overwrites neither original pass)"
        ):
            st.caption(
                "The final judgment is derived from the resolution you choose: "
                "upholding a pass records that pass's judgment; deferring records "
                "no final judgment."
            )
            resolution = st.selectbox(
                "Resolution",
                [
                    "second_pass_upheld",
                    "first_pass_upheld",
                    "new_judgment",
                    "not_evaluable_at_current_grain",
                    "deferred",
                ],
                key=f"adjres::{case_id}",
            )
            adjudication: dict = {"case_id": case_id, "resolution": resolution}
            if resolution == "new_judgment":
                adjudication["resolved_case_grain"] = st.selectbox(
                    "Resolved case grain",
                    [
                        "single_trip_candidate",
                        "multi_trip_chunk",
                        "local_fragment",
                        "boundary_spanning",
                        "turnaround_or_idle",
                        "data_quality_invalid",
                        "coverage_censored",
                        "unknown",
                    ],
                    key=f"adjgrain::{case_id}",
                )
                adjudication["resolved_scheduled_judgment"] = st.selectbox(
                    "Resolved scheduled-trip judgment",
                    [
                        "not_applicable",
                        "matched",
                        "no_scheduled_match",
                        "ambiguous_candidates",
                        "insufficient_evidence",
                    ],
                    key=f"adjjudg::{case_id}",
                )
                if adjudication["resolved_scheduled_judgment"] == "matched":
                    complete = [
                        c
                        for c in bundle.real_candidates
                        if c.kind == "complete_scheduled_trip"
                    ]
                    alias = st.selectbox(
                        "Resolved candidate (from the frozen candidate universe)",
                        [c.alias for c in complete],
                        key=f"adjalias::{case_id}",
                    )
                    picked = bundle.by_alias(alias)
                    adjudication["resolved_candidate_key"] = (
                        picked.candidate_key if picked else None
                    )
            rationale = st.text_area(
                "Adjudication rationale (required)",
                key=f"adjrat::{case_id}",
            )
            if st.button("Save adjudication", key=f"adjsave::{case_id}"):
                adjudication["resolution_rationale"] = rationale.strip()
                try:
                    review_store.save_adjudication(adjudication)
                except Exception as error:  # noqa: BLE001 - surfaced to user
                    st.error(str(error))
                else:
                    st.success("Adjudication recorded")
                    st.rerun()

    with st.expander("Request an additional child case"):
        task = st.selectbox(
            "Child-case task type",
            [
                "subtrip_scheduled_match",
                "boundary_interval",
                "inbound_context",
                "outbound_context",
                "fragment_role",
                "stitch_decision",
                "source_time_quality",
                "activity_state",
                "sequence_review",
            ],
            key=f"ccr::task::{case_id}",
        )
        detail = st.text_input(
            "What should this child case review?",
            key=f"ccr::detail::{case_id}",
        )
        if st.button("Record child-case request", key=f"ccr::save::{case_id}"):
            if detail.strip():
                review_store.record_child_case_request(
                    bundle.parent_case_id,
                    child_task_type=task,
                    detail=detail.strip(),
                )
                st.success(
                    "Request written to the review history (no child-case label "
                    "was created)"
                )
            else:
                st.info("Describe the request first")

    with st.expander("Raw save record (restricted-local; local only)"):
        st.code(json.dumps(save, indent=2, sort_keys=True, default=str))
