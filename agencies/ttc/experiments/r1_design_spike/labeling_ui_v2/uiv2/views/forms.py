"""Grain-first form, plus the six task-specific judgment forms.

Contract points enforced here (the store re-validates all of them at save
time):

- the grain question comes first, has no preselected answer, and no audit
  suggestion is shown before it is answered;
- the null option for the scheduled trip is always visible; a match is never
  forced at any point;
- confidence 4-5 requires both a structured rationale and a free-text
  rationale;
- the timezone label is derived from the timestamp; text typed by the
  reviewer is never trusted;
- a proposed split that falls outside the displayed window gets a prominent
  warning.

Display labels are UI-only, and **stored values are always kept as the raw
enum values**: every select/radio ``options`` list holds the enum values that
go into the store; human-readable labels appear only in the ``format_func``
display layer.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ..case_loader import CaseBundleV2
from ..parsing import fmt_duration_s
from .candidates import alias_selector

GRAIN_OPTIONS: list[tuple[str, str]] = [
    ("single_trip_candidate", "A reviewable, trip-scale chunk"),
    ("multi_trip_chunk", "Contains multiple trips — needs splitting"),
    ("local_fragment", "Local fragment (not a complete trip)"),
    ("boundary_spanning", "Boundary-spanning APC / event record"),
    ("turnaround_or_idle", "Turnaround / idle / non-revenue activity"),
    (
        "data_quality_invalid",
        "Source data, time, or trajectory representation is invalid",
    ),
    ("coverage_censored", "Insufficient context / coverage censored"),
    ("unknown", "Cannot determine"),
]

REVIEWABILITY_BY_GRAIN: dict[str, list[str]] = {
    "single_trip_candidate": [
        "reviewable_for_schedule_match",
        "insufficient_context",
    ],
    "multi_trip_chunk": ["requires_segmentation_adjudication"],
    "boundary_spanning": ["requires_boundary_adjudication"],
    "local_fragment": [
        "not_reviewable_at_current_grain",
        "insufficient_context",
    ],
    "turnaround_or_idle": ["not_reviewable_at_current_grain"],
    "data_quality_invalid": ["invalid_source_state"],
    "coverage_censored": ["insufficient_context"],
    "unknown": ["insufficient_context", "not_reviewable_at_current_grain"],
}

REVIEWABILITY_LABELS = {
    "reviewable_for_schedule_match": "Reviewable for scheduled-trip match",
    "insufficient_context": "Insufficient context; cannot review",
    "requires_segmentation_adjudication": "Requires segmentation adjudication",
    "requires_boundary_adjudication": "Requires boundary adjudication",
    "not_reviewable_at_current_grain": "Not reviewable at the current grain",
    "invalid_source_state": "Invalid source state",
}

EVIDENCE_FAMILY_LABELS = {
    "interval_time_fit": "Time interval / start / end fit",
    "stop_sequence_fit": "Stop sequence fit",
    "spatial_shape_fit": "Spatial shape fit",
    "terminal_behavior": "Terminal behavior",
    "apc_signal": "APC boarding/alighting signal",
    "direction_or_polarity": "Direction / polarity",
    "schedule_headway_separation": "Headway separation from competing scheduled trips",
    "adjacent_context": "Adjacent context",
    "data_quality_signal": "Data quality signal",
}

TRISTATE_OPTIONS = ["true", "false", "ambiguous", "not_applicable"]

TRISTATE_LABELS = {
    "true": "Yes",
    "false": "No",
    "ambiguous": "Cannot determine",
    "not_applicable": "Not applicable",
}

SCHEDULED_JUDGMENT_OPTIONS = [
    "matched",
    "no_scheduled_match",
    "ambiguous_candidates",
    "insufficient_evidence",
]

SCHEDULED_JUDGMENT_LABELS = {
    "matched": "Matched to a scheduled trip",
    "no_scheduled_match": "No corresponding scheduled trip (null option)",
    "ambiguous_candidates": "Cannot distinguish between candidates",
    "insufficient_evidence": "Insufficient evidence to judge",
}

ACTIVITY_OPTIONS = [
    "revenue_trip_candidate",
    "terminal_turnaround",
    "idling",
    "deadhead_or_nonrevenue_unconfirmed",
    "pullin_or_pullout_unconfirmed",
    "coverage_censored",
    "unknown",
]

ACTIVITY_LABELS = {
    "revenue_trip_candidate": "Suspected revenue trip",
    "terminal_turnaround": "Terminal turnaround",
    "idling": "Idling",
    "deadhead_or_nonrevenue_unconfirmed": "Deadhead / non-revenue (unconfirmed)",
    "pullin_or_pullout_unconfirmed": "Pull-in / pull-out (unconfirmed)",
    "coverage_censored": "Coverage censored",
    "unknown": "Cannot determine",
}

QUALITY_FLAG_OPTIONS = [
    "simultaneous_positions",
    "impossible_speed",
    "unresolved_dst_fold",
    "trajectory_not_split",
    "missing_avl_coverage",
    "spatial_temporal_conflict",
    "candidate_universe_incomplete",
    "timezone_text_inconsistent",
    "other",
]

QUALITY_FLAG_LABELS = {
    "simultaneous_positions": "Multiple positions at the same instant",
    "impossible_speed": "Impossible speed",
    "unresolved_dst_fold": "Unresolved DST fall-back overlap hour",
    "trajectory_not_split": "Trajectory should have been split but was not",
    "missing_avl_coverage": "Missing AVL coverage",
    "spatial_temporal_conflict": "Spatial and temporal information conflict",
    "candidate_universe_incomplete": "Candidate universe incomplete",
    "timezone_text_inconsistent": "Timezone text inconsistent",
    "other": "Other",
}

FRAGMENT_ROLE_OPTIONS = [
    "trip_core",
    "arrival_fragment",
    "departure_fragment",
    "turnaround",
    "stitch_previous",
    "stitch_next",
    "independent_short_trip",
    "ambiguous_fragment",
]

FRAGMENT_ROLE_LABELS = {
    "trip_core": "Trip core (this is actually the core of a complete trip)",
    "arrival_fragment": "Arrival fragment (the tail of a trip)",
    "departure_fragment": "Departure fragment (the head of a trip)",
    "turnaround": "Turnaround",
    "stitch_previous": "Should be stitched to the previous object",
    "stitch_next": "Should be stitched to the next object",
    "independent_short_trip": "Independent short trip",
    "ambiguous_fragment": "Fragment whose role cannot be determined",
}

ARRIVAL_ROLE_LABELS = {
    "unresolved": "Unresolved",
    "trip_end": "End of a trip",
    "terminal_arrival": "Terminal arrival",
}

DEPARTURE_ROLE_LABELS = {
    "unresolved": "Unresolved",
    "trip_start": "Start of a trip",
    "terminal_departure": "Terminal departure",
}

COMPLETE_TRIP_LABELS = {
    "not_applicable": "Not applicable (this is not a complete trip)",
    "insufficient_evidence": "Insufficient evidence",
    "ambiguous_candidates": "Cannot distinguish between candidates",
}


def _labeler(mapping: dict[str, str]):
    """Display-layer labels; unlisted values are shown as-is, and stored
    values are never changed."""

    return lambda value: mapping.get(value, value)


def render_grain_form(bundle: CaseBundleV2) -> tuple[str | None, str | None]:
    """The mandatory first question. Returns (grain, reviewability)."""

    st.markdown("### 1 · What does this object represent?")
    st.caption(
        "Answer using only the evidence above. No suggestion is shown before you "
        "answer; whichever queue this case was originally routed to, you may "
        "choose any grain."
    )
    labels = [label for _, label in GRAIN_OPTIONS]
    picked = st.radio(
        "Case grain",
        labels,
        index=None,
        key=f"grain::{bundle.case_id}",
    )
    if picked is None:
        return None, None
    grain = next(value for value, label in GRAIN_OPTIONS if label == picked)
    choices = REVIEWABILITY_BY_GRAIN[grain]
    reviewability = st.selectbox(
        "Reviewability at this grain",
        choices,
        format_func=_labeler(REVIEWABILITY_LABELS),
        key=f"reviewability::{bundle.case_id}",
    )
    return grain, reviewability


def confidence_block(
    case_id: str, *, include_match: bool, match_label: str = "Match confidence"
) -> dict[str, Any]:
    columns = st.columns(3 if include_match else 2)
    grain_confidence = columns[0].select_slider(
        "Grain confidence",
        options=[1, 2, 3, 4, 5],
        value=3,
        key=f"gc::{case_id}",
    )
    evidence_quality = columns[1].select_slider(
        "Evidence quality",
        options=[1, 2, 3, 4, 5],
        value=3,
        key=f"eq::{case_id}",
    )
    result: dict[str, Any] = {
        "grain_confidence": grain_confidence,
        "evidence_quality": evidence_quality,
    }
    if include_match:
        result["match_confidence"] = columns[2].select_slider(
            match_label,
            options=[1, 2, 3, 4, 5],
            value=3,
            key=f"mc::{case_id}",
        )
    return result


def rationale_block(case_id: str, *, for_match: bool) -> dict[str, Any]:
    flags = st.multiselect(
        "Structured rationale — which evidence families support this judgment? "
        "(required at confidence 4-5"
        + ("; a match normally requires two evidence families)" if for_match else ")"),
        list(EVIDENCE_FAMILY_LABELS),
        format_func=_labeler(EVIDENCE_FAMILY_LABELS),
        placeholder="Select the evidence families that support your judgment",
        key=f"flags::{case_id}",
    )
    single = False
    single_family = None
    if for_match:
        single = st.checkbox(
            "Single decisive anchor — only one evidence family, but it is "
            "sufficient on its own",
            key=f"single::{case_id}",
        )
        if single:
            single_family = st.selectbox(
                "Which evidence family is decisive?",
                list(EVIDENCE_FAMILY_LABELS),
                format_func=_labeler(EVIDENCE_FAMILY_LABELS),
                key=f"singlefam::{case_id}",
            )
    rationale_text = st.text_area(
        "Free-text rationale (required at confidence 4-5)",
        key=f"rat::{case_id}",
        height=80,
    )
    notes = st.text_area("Notes (optional)", key=f"notes::{case_id}", height=80)
    result: dict[str, Any] = {
        "evidence_flags": list(flags) + (["single_decisive_anchor"] if single else []),
        "rationale_text": rationale_text,
        "notes": notes,
    }
    if single_family:
        result["single_decisive_anchor_family"] = single_family
    return result


def render_scheduled_form(
    bundle: CaseBundleV2, visible: list, *, key_prefix: str = ""
) -> dict[str, Any] | None:
    case_id = bundle.case_id
    prefix = key_prefix or "sched"
    st.markdown("### 2 · Scheduled-trip judgment")
    judgment = st.radio(
        "Judgment",
        SCHEDULED_JUDGMENT_OPTIONS,
        index=None,
        format_func=_labeler(SCHEDULED_JUDGMENT_LABELS),
        key=f"{prefix}::judgment::{case_id}",
        help=(
            "The null option (no corresponding scheduled trip) is always "
            "available. A match is never required, even when only one "
            "candidate is displayed."
        ),
    )
    alias = None
    if judgment == "matched":
        alias = alias_selector(
            bundle,
            visible,
            key=f"{prefix}::alias::{case_id}",
            label="Matched candidate (choose from the candidate universe above)",
            allow_none=True,
            none_label="— select a candidate —",
        )
        if alias is None:
            st.info("Select the matched candidate before saving")
    if judgment is None:
        return None
    record: dict[str, Any] = {"scheduled_judgment": judgment}
    if judgment == "matched":
        if alias is None:
            return None
        record["selected_candidate_alias"] = alias
    record.update(confidence_block(case_id, include_match=judgment == "matched"))
    record.update(rationale_block(case_id, for_match=judgment == "matched"))
    return record


def render_multi_trip_form(
    bundle: CaseBundleV2, visible: list
) -> dict[str, Any] | None:
    case_id = bundle.case_id
    st.markdown("### 2 · Multi-trip segmentation")
    st.caption(
        "Describe only the subtrips you can defend. Boundaries are entered as "
        "**second intervals relative to the chunk start**; an exact instant is "
        "not required. Each subtrip has its own child case for separate review; "
        "no earlier notes are copied here."
    )
    duration = bundle.chunk.duration_s or 0
    count = int(
        st.number_input(
            "Number of operating subtrips",
            min_value=1,
            max_value=8,
            value=2,
            key=f"mt::count::{case_id}",
        )
    )
    sequence: list[str | None] = []
    for index in range(count):
        alias = alias_selector(
            bundle,
            visible,
            key=f"mt::sub{index}::{case_id}",
            label=f"Candidate for subtrip {index + 1} (or choose none / unresolved)",
        )
        sequence.append(bundle.by_alias(alias).candidate_key if alias else None)
    intervals: list[list[int]] = []
    for index in range(max(0, count - 1)):
        columns = st.columns(2)
        low = int(
            columns[0].number_input(
                f"Boundary {index + 1} — earliest (seconds after chunk start)",
                min_value=-86400,
                max_value=2 * 86400,
                value=0,
                key=f"mt::blo{index}::{case_id}",
            )
        )
        high = int(
            columns[1].number_input(
                f"Boundary {index + 1} — latest (seconds after chunk start)",
                min_value=-86400,
                max_value=2 * 86400,
                value=min(duration, max(0, low)),
                key=f"mt::bhi{index}::{case_id}",
            )
        )
        if high < low:
            st.error("Boundary interval ends before it starts")
            return None
        if low < 0 or (duration and high > duration):
            st.warning(
                f"Boundary interval {index + 1} falls outside the displayed chunk "
                f"(0 … {fmt_duration_s(duration)}) — the save will carry an "
                "explicit out-of-window warning"
            )
        intervals.append([low, high])
    unresolved = int(
        st.number_input(
            "Number of subsegments that cannot be attributed",
            min_value=0,
            max_value=8,
            value=0,
            key=f"mt::unres::{case_id}",
        )
    )
    split_confidence = st.select_slider(
        "Split confidence",
        options=[1, 2, 3, 4, 5],
        value=3,
        key=f"mt::sc::{case_id}",
    )
    record: dict[str, Any] = {
        "subtrip_count": count,
        "ordered_candidate_sequence": sequence,
        "split_intervals_rel_s": intervals,
        "split_confidence": split_confidence,
        "unresolved_subsegments": unresolved,
    }
    record.update(confidence_block(case_id, include_match=False))
    record.update(rationale_block(case_id, for_match=False))
    return record


def render_boundary_form(
    bundle: CaseBundleV2, visible: list, *, derived_tz_preview
) -> dict[str, Any] | None:
    case_id = bundle.case_id
    st.markdown("### 2 · Boundary-spanning dual-role judgment")
    st.caption(
        "The inbound side and the outbound side are independent: the two sides "
        "may select different candidates, and either side may remain unresolved. "
        "This record is never forced onto a single unique owner."
    )
    inbound = alias_selector(
        bundle,
        visible,
        key=f"bd::in::{case_id}",
        label="Inbound-side (arrival direction) candidate",
    )
    outbound = alias_selector(
        bundle,
        visible,
        key=f"bd::out::{case_id}",
        label="Outbound-side (departure direction) candidate",
    )
    stop_hypothesis = st.text_input(
        "Boundary stop hypothesis (stop code; optional)",
        key=f"bd::stop::{case_id}",
    )
    duration = bundle.chunk.duration_s or 0
    columns = st.columns(2)
    low = int(
        columns[0].number_input(
            "Boundary interval — earliest (seconds after chunk start)",
            min_value=-86400,
            max_value=2 * 86400,
            value=0,
            key=f"bd::lo::{case_id}",
        )
    )
    high = int(
        columns[1].number_input(
            "Boundary interval — latest (seconds after chunk start)",
            min_value=-86400,
            max_value=2 * 86400,
            value=duration,
            key=f"bd::hi::{case_id}",
        )
    )
    if high < low:
        st.error("Boundary interval ends before it starts")
        return None
    if low < 0 or (duration and high > duration):
        st.warning(
            "The proposed boundary falls **outside** the displayed chunk "
            f"(0 … {fmt_duration_s(duration)}); the save will carry an explicit "
            "out-of-window warning — check the adjacent context first"
        )
    tz_label = derived_tz_preview((low + high) // 2)
    if tz_label:
        st.caption(
            f"Derived timezone at this boundary: **{tz_label}** "
            "(derived from the timestamp — reviewer-typed EDT/EST text is never "
            "trusted)"
        )
    columns = st.columns(3)
    arrival_role = columns[0].selectbox(
        "Arrival-side role",
        ["unresolved", "trip_end", "terminal_arrival"],
        format_func=_labeler(ARRIVAL_ROLE_LABELS),
        key=f"bd::ar::{case_id}",
    )
    departure_role = columns[1].selectbox(
        "Departure-side role",
        ["unresolved", "trip_start", "terminal_departure"],
        format_func=_labeler(DEPARTURE_ROLE_LABELS),
        key=f"bd::dr::{case_id}",
    )
    dual_role = columns[2].checkbox(
        "This record needs dual attribution",
        value=True,
        key=f"bd::dual::{case_id}",
    )
    record: dict[str, Any] = {
        "inbound_candidate_key": (
            bundle.by_alias(inbound).candidate_key if inbound else None
        ),
        "outbound_candidate_key": (
            bundle.by_alias(outbound).candidate_key if outbound else None
        ),
        "boundary_stop_hypothesis": stop_hypothesis or None,
        "boundary_time_interval_rel_s": [low, high],
        "arrival_role": arrival_role,
        "departure_role": departure_role,
        "dual_role": dual_role,
    }
    record.update(confidence_block(case_id, include_match=False))
    record.update(rationale_block(case_id, for_match=False))
    return record


def render_fragment_form(bundle: CaseBundleV2, visible: list) -> dict[str, Any] | None:
    case_id = bundle.case_id
    st.markdown("### 2 · Fragment role and stitching")
    st.caption(
        "Local scheduled-trip context is **not** executed-trip identity: pointing "
        "out which scheduled trip's timetable window this fragment falls within "
        "does not mean the fragment is that complete trip."
    )
    role = st.radio(
        "Fragment role",
        FRAGMENT_ROLE_OPTIONS,
        index=None,
        format_func=_labeler(FRAGMENT_ROLE_LABELS),
        key=f"fr::role::{case_id}",
    )
    if role is None:
        return None
    columns = st.columns(2)
    stitch_previous = columns[0].selectbox(
        "Stitch to the previous object?",
        TRISTATE_OPTIONS,
        index=3,
        format_func=_labeler(TRISTATE_LABELS),
        key=f"fr::sp::{case_id}",
    )
    stitch_next = columns[1].selectbox(
        "Stitch to the next object?",
        TRISTATE_OPTIONS,
        index=3,
        format_func=_labeler(TRISTATE_LABELS),
        key=f"fr::sn::{case_id}",
    )
    local_alias = alias_selector(
        bundle,
        visible,
        key=f"fr::local::{case_id}",
        label=(
            "Local scheduled **context** candidate (optional; this is not a trip match)"
        ),
    )
    complete_trip = st.selectbox(
        "Judgment on 'complete executed trip' at the current grain",
        ["not_applicable", "insufficient_evidence", "ambiguous_candidates"],
        format_func=_labeler(COMPLETE_TRIP_LABELS),
        key=f"fr::ct::{case_id}",
        help=(
            "A fragment cannot be promoted to a complete-trip match here; if you "
            "are confident it is actually a complete trip, go back up and change "
            "the case grain."
        ),
    )
    fragment_confidence = st.select_slider(
        "Fragment role confidence",
        options=[1, 2, 3, 4, 5],
        value=3,
        key=f"fr::fc::{case_id}",
    )
    record: dict[str, Any] = {
        "fragment_role": role,
        "stitch_previous": stitch_previous,
        "stitch_next": stitch_next,
        "local_scheduled_context_candidate_key": (
            bundle.by_alias(local_alias).candidate_key if local_alias else None
        ),
        "complete_trip_judgment": complete_trip,
        "fragment_confidence": fragment_confidence,
    }
    record.update(confidence_block(case_id, include_match=False))
    record.update(rationale_block(case_id, for_match=False))
    return record


def render_quality_form(bundle: CaseBundleV2) -> dict[str, Any] | None:
    case_id = bundle.case_id
    st.markdown("### 2 · Source data / time quality judgment")
    st.caption(
        "When the trajectory representation itself is invalid, you are not asked "
        "to choose a scheduled trip — a quality problem is not ambiguity between "
        "candidates."
    )
    flags = st.multiselect(
        "Quality flags (all that apply)",
        QUALITY_FLAG_OPTIONS,
        format_func=_labeler(QUALITY_FLAG_LABELS),
        placeholder="Select all applicable quality flags",
        key=f"q::flags::{case_id}",
    )
    if not flags:
        st.info("Select at least one quality flag")
        return None
    dominant = st.selectbox(
        "Dominant quality state",
        flags,
        format_func=_labeler(QUALITY_FLAG_LABELS),
        key=f"q::state::{case_id}",
    )
    applicable = st.checkbox(
        "A scheduled-trip judgment still applies despite the problems above",
        value=False,
        key=f"q::app::{case_id}",
    )
    record: dict[str, Any] = {
        "quality_state": dominant,
        "quality_flags": flags,
        "scheduled_match_applicable": applicable,
    }
    record.update(confidence_block(case_id, include_match=False))
    record.update(rationale_block(case_id, for_match=False))
    return record


def render_activity_form(bundle: CaseBundleV2) -> dict[str, Any] | None:
    case_id = bundle.case_id
    st.markdown("### 2 · Activity state judgment")
    st.caption(
        "A long dwell does not automatically mean a layover, pull-in/pull-out, or "
        "deadhead — these semantics are not yet settled (open decision D-11); "
        "the *_unconfirmed states record the hypothesis without claiming it holds."
    )
    activity = st.radio(
        "Activity state",
        ACTIVITY_OPTIONS,
        index=None,
        format_func=_labeler(ACTIVITY_LABELS),
        key=f"act::state::{case_id}",
    )
    if activity is None:
        return None
    applicable = st.checkbox(
        "A scheduled-trip judgment applies to this object",
        value=False,
        key=f"act::app::{case_id}",
    )
    no_match_confidence = st.select_slider(
        "Confidence in 'no scheduled-trip match'",
        options=[1, 2, 3, 4, 5],
        value=3,
        key=f"act::nm::{case_id}",
    )
    record: dict[str, Any] = {
        "activity_state": activity,
        "scheduled_match_applicable": applicable,
        "match_confidence": no_match_confidence,
    }
    record.update(confidence_block(case_id, include_match=False))
    record.update(rationale_block(case_id, for_match=False))
    return record
