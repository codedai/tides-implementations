# ruff: noqa: E501
"""Aggregate insights and audit-only report rendering."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .label_readiness import (
    NEAR_COVERAGE_TOLERANCE_PCT_POINTS,
    NEAR_OVERLAP_TOLERANCE_S,
    candidate_count_summary,
    duration_bucket,
)


def build_audit_summary(
    rows: Sequence[Mapping[str, Any]],
    inventory_rows: Sequence[Mapping[str, Any]],
    *,
    review_history_summary: Mapping[str, Any] | None = None,
    design_contract_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    total = len(rows)
    matches = [row for row in rows if row["original_label"] == "matched"]
    label_counts = _count(rows, "original_label")
    confidence_counts = {
        str(level): sum(row["confidence"] == level for row in rows)
        for level in range(1, 6)
    }
    complete_counts = [int(row["complete_candidate_count"]) for row in rows]
    total_table_counts = [int(row["candidate_table_key_count_total"]) for row in rows]
    flag_counts = Counter(
        reason for row in rows for reason in row["audit_reason_codes"]
    )
    heuristic_counts = Counter(
        heuristic for row in rows for heuristic in row["explicit_reviewer_heuristics"]
    )
    overlap_sign = _advantage_sign_counts(matches, "selected_overlap_advantage_s")
    coverage_sign = _advantage_sign_counts(
        matches, "selected_coverage_advantage_pct_points"
    )
    return {
        "denominator": total,
        "architecture_state": "INCONCLUSIVE — NO REVIEWED HOLDOUT LABELS",
        "packet_role": "pilot/adjudication set; not assumed to be an independent holdout",
        "label_distribution": {
            "primary_labels": label_counts,
            "raw_primary_labels": _count(rows, "raw_primary_label"),
            "confidence": confidence_counts,
            "by_stratum": _cross_count(rows, "original_stratum", "original_label"),
            "by_chunk_duration_bucket": _cross_count_with_bucket(rows),
            "by_packet_fragment_status": _cross_count_fragment(rows),
            "by_audited_fragment_like_status": _cross_count_audited_fragment(rows),
            "notes": {
                "present": sum(bool(row["notes_present"]) for row in rows),
                "absent": sum(not row["notes_present"] for row in rows),
            },
            "rationale": {
                "present": sum(bool(row["rationale_present"]) for row in rows),
                "absent": sum(not row["rationale_present"] for row in rows),
            },
        },
        "consistency": {
            "status_counts": _count(rows, "internal_consistency_status"),
            "reason_code_counts": dict(sorted(flag_counts.items())),
            "notes_primary_conflicts": flag_counts["NOTES_PRIMARY_LABEL_CONFLICT"],
            "high_confidence_matches_without_rationale": flag_counts[
                "HIGH_CONFIDENCE_NO_RATIONALE"
            ],
            "low_confidence_matches": flag_counts["LOW_CONFIDENCE_MATCH"],
            "full_candidate_keys_missing": flag_counts["FULL_CANDIDATE_KEY_MISSING"],
            "adjacent_context_used": flag_counts["ADJACENT_CONTEXT_USED"],
            "timezone_text_inconsistencies": flag_counts[
                "REVIEWER_TIMEZONE_TEXT_INCONSISTENT"
            ],
            "timezone_text_unverifiable": flag_counts[
                "REVIEWER_TIMEZONE_TEXT_UNVERIFIABLE"
            ],
            "diagnostics_revealed_before_first_save": sum(
                row["diagnostics_revealed_before_save"] is True for row in rows
            ),
            "diagnostics_reveal_metadata_conflicts": flag_counts[
                "DIAGNOSTICS_REVEAL_METADATA_CONFLICT"
            ],
        },
        "grain_routing": {
            "case_grain": _count(rows, "case_grain_suggestion"),
            "reviewability": _count(rows, "reviewability_suggestion"),
            "task_routing": _count(rows, "task_routing_suggestion"),
        },
        "candidate_separability": {
            "displayed_candidate_count": _distribution(
                [int(row["candidate_count"]) for row in rows]
            ),
            "displayed_evaluable_candidate_count": _distribution(
                [int(row["displayed_evaluable_candidate_count"]) for row in rows]
            ),
            "complete_candidate_count": candidate_count_summary(complete_counts),
            "candidate_table_key_count_total": candidate_count_summary(
                total_table_counts
            ),
            "matched_selected_overlap_advantage_sign": overlap_sign,
            "matched_selected_coverage_advantage_sign": coverage_sign,
            "matched_selected_exact_ties": sum(
                int(row["selected_exact_tie_count"]) > 0 for row in matches
            ),
            "non_discriminative_without_rationale": flag_counts[
                "VISIBLE_EVIDENCE_NON_DISCRIMINATIVE"
            ],
            "near_tie_definition": {
                "overlap_difference_at_most_s": NEAR_OVERLAP_TOLERANCE_S,
                "coverage_difference_at_most_percentage_points": (
                    NEAR_COVERAGE_TOLERANCE_PCT_POINTS
                ),
            },
            "all_visible_candidates_tied": [
                row["case_id"]
                for row in rows
                if "ALL_VISIBLE_CANDIDATES_TIED" in row["audit_reason_codes"]
            ],
            "scope_note": (
                "Advantages use displayed Markdown metrics only; maps, trajectories, "
                "stop ladders, and residual panels may have supplied other evidence."
            ),
        },
        "human_decision_behavior": {
            "explicit_heuristic_counts": dict(sorted(heuristic_counts.items())),
            "blank_notes_inferred_heuristics": 0,
        },
        "packet_metadata_audit": {
            "n_events_values": _distribution(
                [int(row["original_n_events"]) for row in inventory_rows]
            ),
            "span_rel_s_values": _distribution(
                [int(row["original_span_rel_s"]) for row in inventory_rows]
            ),
            "chunk_duration_s": candidate_count_summary(
                [int(row["chunk_duration_s"]) for row in inventory_rows]
            ),
            "finding": (
                "n_events and span_rel_s describe one synthetic packet event at "
                "relative zero, not the operational chunk."
            ),
            "stratum_finding": (
                "fragment is a duration-derived packet stratum and route_929 is a "
                "hard-coded fallback, not an adjudicated route or grain."
            ),
        },
        "review_history": dict(review_history_summary or {}),
        "design_contract_audit": dict(design_contract_summary or {}),
        "non_claims": {
            "human_labels_overwritten": False,
            "automatic_relabeling": False,
            "accuracy_metric_calculated": False,
            "architecture_selected": False,
            "factorial_run": False,
            "calibration_run": False,
        },
    }


def build_readiness_report(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    classes = Counter(
        readiness_class
        for row in rows
        for readiness_class in row["label_readiness_classes"]
    )
    gate_fields = [
        "single_trip_candidate",
        "reviewable_for_schedule_match",
        "full_candidate_key_resolved",
        "no_unresolved_notes_label_conflict",
        "rationale_present",
        "blind_second_pass_completed",
    ]
    gate_counts = {
        field: sum(bool(row["strict_gate_checks"].get(field)) for row in rows)
        for field in gate_fields
    }
    ready = [row["case_id"] for row in rows if row["calibration_ready"]]
    selected_key_rows = [
        row
        for row in rows
        if row["candidate_key_status"] != "not_applicable_no_selected_candidate"
    ]
    resolved_selected_key_rows = [
        row
        for row in selected_key_rows
        if str(row["candidate_key_status"]).startswith("resolved_complete_key")
    ]
    return {
        "denominator": total,
        "classes_are_nonexclusive": True,
        "class_counts": dict(sorted(classes.items())),
        "class_case_ids": {
            readiness_class: sorted(
                row["case_id"]
                for row in rows
                if readiness_class in row["label_readiness_classes"]
            )
            for readiness_class in sorted(classes)
        },
        "strict_scheduled_calibration_gate": {
            "definition": gate_fields,
            "condition_count_denominator": total,
            "condition_counts_treat_not_applicable_key_as_satisfied": False,
            "condition_counts": gate_counts,
            "selected_candidate_key_resolution": {
                "applicable_count": len(selected_key_rows),
                "resolved_count": len(resolved_selected_key_rows),
                "unresolved_count": len(selected_key_rows)
                - len(resolved_selected_key_rows),
            },
            "calibration_ready_count": len(ready),
            "calibration_ready_case_ids": ready,
        },
        "blind_second_pass": {
            "required_count": classes["blind_second_pass_required"],
            "completed_count": sum(
                bool(row.get("blind_second_pass_completed")) for row in rows
            ),
            "first_pass_labels_must_remain_hidden": True,
        },
        "factorial_status": "BLOCKED",
        "factorial_blockers": [
            "schema and task protocol not frozen",
            "no blind second pass completed",
            "no adjudicated task-specific label artifact",
            "current pilot is not established as the preregistered holdout",
        ],
    }


def proposed_label_schema_v2() -> dict[str, Any]:
    return {
        "schema_version": "human-label-v2-proposal",
        "status": "proposal_only_not_populated",
        "first_pass_immutability": {
            "raw_fields_are_append_only": True,
            "audit_suggestions_must_not_replace_human_fields": True,
        },
        "case_grain": {
            "enum": [
                "single_trip",
                "multiple_trips",
                "local_fragment",
                "boundary_spanning",
                "turnaround_idle",
                "invalid_trajectory",
                "censored_coverage",
                "unknown",
            ]
        },
        "scheduled_judgment": {
            "enum": [
                "matched",
                "no_scheduled_match",
                "ambiguous_candidates",
                "insufficient_evidence",
                "not_applicable",
            ],
            "candidate_key": {
                "service_date": "date",
                "feed_id": "string",
                "route_mapping_id": "string",
                "scheduled_trip_id": "string",
                "polarity_hypothesis_id": "pol_a_pol_b_or_explicit_null",
            },
        },
        "fragment_role": {
            "enum": [
                "trip_core",
                "arrival_fragment",
                "departure_fragment",
                "turnaround",
                "stitch_previous",
                "stitch_next",
                "independent_short_trip",
                "ambiguous",
            ],
            "stitch_previous": "boolean_or_ambiguous",
            "stitch_next": "boolean_or_ambiguous",
        },
        "quality_state": {
            "enum": [
                "valid",
                "simultaneous_positions",
                "unresolved_fold",
                "missing_coverage",
                "trajectory_not_split",
                "spatial_temporal_conflict",
                "other",
            ]
        },
        "activity_state": {
            "enum": [
                "scheduled_service",
                "turnaround",
                "layover",
                "idling",
                "non_revenue",
                "other",
                "not_applicable",
                "unknown",
            ]
        },
        "boundary_detail": {
            "inbound_candidate_key": "complete_key_or_null",
            "outbound_candidate_key": "complete_key_or_null",
            "boundary_stop": "string_or_null",
            "boundary_time_range_rel_s": "integer_pair_or_null",
            "inside_current_chunk": "boolean_or_unknown",
        },
        "segmentation_detail": {
            "subtrip_count": "integer_or_unknown",
            "boundary_ranges_rel_s": "list_of_integer_pairs",
            "child_case_ids": "pseudonymous_ids_after_generation",
        },
        "confidence": {
            "grain_confidence": "ordinal_1_to_5",
            "match_confidence": "ordinal_1_to_5_or_not_applicable",
            "evidence_quality": "ordinal_1_to_5",
        },
        "evidence_exposure": {
            "panels_available": "list",
            "panels_viewed": "interaction_log_or_unknown",
            "diagnostics_revealed_before_save": "boolean",
            "context_before_s": "integer_or_null",
            "context_after_s": "integer_or_null",
            "candidate_shortlist_method": "string",
        },
        "review": {
            "reviewer_id": "pseudonymous_string",
            "pass_number": "integer",
            "blind_to_prior_labels": "boolean",
            "rationale_codes": "list",
            "notes": "verbatim_string",
            "review_timestamp": "timestamp",
        },
        "audit": {
            "suggestions_are_nonbinding": True,
            "reason_codes": "list",
            "adjudication_status": "pending_reviewed_resolved",
        },
    }


def render_findings(
    rows: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    readiness_report: Mapping[str, Any],
    input_hashes: Mapping[str, str],
) -> str:
    total = len(rows)
    label_counts = summary["label_distribution"]["primary_labels"]
    grain = summary["grain_routing"]["case_grain"]
    consistency = summary["consistency"]
    ordered = sorted(
        rows, key=lambda row: (-int(row["review_priority_score"]), row["case_id"])
    )
    pilot_ready = readiness_report["class_counts"].get("schedule_match_pilot_ready", 0)
    second_pass = readiness_report["class_counts"].get("blind_second_pass_required", 0)
    child_needed_ids = {
        row["case_id"]
        for row in rows
        if row["case_grain_suggestion"]
        in {"multi_trip_chunk", "boundary_spanning", "local_fragment"}
    }
    task_specific_ids = {
        row["case_id"]
        for row in rows
        if row["task_routing_suggestion"] != "scheduled_trip_second_pass"
    }
    not_evaluable = readiness_report["class_counts"].get(
        "not_evaluable_from_current_packet", 0
    )
    strict_ready = readiness_report["strict_scheduled_calibration_gate"][
        "calibration_ready_count"
    ]
    schedule_grain_count = grain.get("single_trip_candidate", 0)
    match_count = label_counts.get("matched", 0)
    overlap_sign = summary["candidate_separability"][
        "matched_selected_overlap_advantage_sign"
    ]
    coverage_sign = summary["candidate_separability"][
        "matched_selected_coverage_advantage_sign"
    ]
    candidate_table_counts = summary["candidate_separability"][
        "candidate_table_key_count_total"
    ]
    complete_candidate_counts = summary["candidate_separability"][
        "complete_candidate_count"
    ]
    pilot_ids = readiness_report["class_case_ids"].get("schedule_match_pilot_ready", [])
    diagnostics_unrevealed = sum(
        row["diagnostics_revealed_before_save"] is False for row in rows
    )
    history = summary.get("review_history", {})
    design = summary.get("design_contract_audit", {})
    ui_evidence = design.get("ui_evidence_audit", {})
    fragment_like_count = sum(
        "LOCAL_FRAGMENT_NOT_FULL_TRIP" in row["audit_reason_codes"] for row in rows
    )
    boundary_fragment_count = sum(
        row["case_grain_suggestion"] == "boundary_spanning"
        and "LOCAL_FRAGMENT_NOT_FULL_TRIP" in row["audit_reason_codes"]
        for row in rows
    )
    multi_ids = sorted(
        row["case_id"]
        for row in rows
        if row["case_grain_suggestion"] == "multi_trip_chunk"
    )
    boundary_ids = sorted(
        row["case_id"]
        for row in rows
        if row["case_grain_suggestion"] == "boundary_spanning"
    )
    fragment_ids = sorted(
        row["case_id"]
        for row in rows
        if "LOCAL_FRAGMENT_NOT_FULL_TRIP" in row["audit_reason_codes"]
    )
    quality_ids = sorted(
        row["case_id"]
        for row in rows
        if row["case_grain_suggestion"] == "data_quality_invalid"
    )
    high_no_rationale_ids = sorted(
        row["case_id"]
        for row in rows
        if "HIGH_CONFIDENCE_NO_RATIONALE" in row["audit_reason_codes"]
    )
    lines = [
        "# Human label audit findings",
        "",
        "## 1. Executive result",
        "",
        (
            f"The audit preserved {total}/{total} first-pass judgments. The current "
            f"packet is **not ready for calibration**: {strict_ready}/{total} cases pass the strict gate. "
            f"It supports a blind second pass for {second_pass}/{total} single-trip "
            f"candidates, schema revision, and task-specific adjudication. "
            "R1 remains open and the architecture verdict remains "
            "**INCONCLUSIVE — NO REVIEWED HOLDOUT LABELS**."
        ),
        "",
        "This is an audit flagging exercise, not a replacement-label artifact. The evidence does not support an automatic relabel.",
        "",
        "## 2. Input inventory and preservation",
        "",
        f"The deterministic inventory contains {len(input_hashes)} files:",
        "",
    ]
    lines.extend(
        f"- `{path}` — `{digest}`" for path, digest in sorted(input_hashes.items())
    )
    lines += [
        "",
        (
            "The immutable Parquet uses the earliest append-only save per case and "
            "reviewer. Current-export re-saves are cross-checks, not replacements. "
            "Raw notes remain verbatim only in the restricted artifact."
        ),
        "",
        (
            f"Review history contains {history.get('event_count', 0)} events: "
            f"{history.get('first_pass_label_count', 0)} first saves and "
            f"{history.get('resave_count', 0)} resaves; "
            f"{history.get('resaves_semantically_identical_to_first_pass', 0)} resaves "
            "are semantically identical to their first pass. "
            f"Diagnostic reveal events: {history.get('diagnostic_reveal_event_count', 0)}."
        ),
        "",
        (
            f"The frozen preregistration reports labels_reviewed={str(design.get('labels_reviewed', False)).lower()}, "
            f"a registered label artifact={str(design.get('label_artifact_registered', False)).lower()}, "
            f"and {design.get('holdout_vehicle_day_count', 0)} holdout vehicle-days. "
            f"The Phase B selection has {design.get('phase_b_selected_count', 0)} pseudonymous vehicle-days and "
            f"processed={str(design.get('phase_b_processed', False)).lower()}; overlap with this packet's case IDs is "
            f"{design.get('phase_b_current_packet_case_id_overlap_count', 0)}."
        ),
        "",
        "## 3. Label-distribution findings",
        "",
        _count_sentence(label_counts, total),
        "",
        _count_sentence(
            summary["label_distribution"]["raw_primary_labels"],
            total,
            prefix="Verbatim stored primary labels",
        ),
        "",
        _count_sentence(
            summary["label_distribution"]["confidence"], total, prefix="Confidence"
        ),
        "",
        (
            f"Notes are present for {summary['label_distribution']['notes']['present']}/{total}; "
            f"notes or structured rationale are present for "
            f"{summary['label_distribution']['rationale']['present']}/{total}."
        ),
        "",
        "Duration-bucket and stratum cross-counts are recorded with exact denominators in `label_audit_summary.json`.",
        "",
        "## 4. Internal-consistency findings",
        "",
        (
            f"Explicit notes-primary conflicts occur in {consistency['notes_primary_conflicts']}/{total}; "
            f"high-confidence matches without rationale in "
            f"{consistency['high_confidence_matches_without_rationale']}/"
            f"{match_count} matched cases; and low-confidence matches in "
            f"{consistency['low_confidence_matches']}/{match_count}."
        ),
        "",
        (
            "High-confidence match cases without rationale: "
            + (", ".join(high_no_rationale_ids) if high_no_rationale_ids else "none")
            + "."
        ),
        "",
        (
            f"Complete selected-candidate keys are unresolved in "
            f"{consistency['full_candidate_keys_missing']}/{match_count} "
            "matches. Stored selected keys resolve by complete-key membership; the "
            "display-card-to-key mapping was not independently reconstructed. An empty "
            "fifth field is the contract's explicit null-polarity hypothesis."
        ),
        "",
        (
            "Each summary displayed five cards. The complete table contains "
            f"{candidate_table_counts['min']}–{candidate_table_counts['max']} total keys per case "
            f"(median {candidate_table_counts['median']}) and "
            f"{complete_candidate_counts['min']}–{complete_candidate_counts['max']} full "
            f"scheduled-trip keys (median {complete_candidate_counts['median']}); the remainder "
            "are explicit no-scheduled-trip alternatives."
        ),
        "",
        (
            f"Adjacent context is referenced in {consistency['adjacent_context_used']}/{total} "
            f"cases; timezone text is inconsistent in {consistency['timezone_text_inconsistencies']}/{total} "
            f"and unverifiable in {consistency['timezone_text_unverifiable']}/{total}. "
            f"Diagnostics are recorded as unrevealed for {diagnostics_unrevealed}/{total} first saves, although the five-card shortlist "
            "was model-informed and is therefore weak presentation leakage."
        ),
        "",
        "## 5. Case-grain findings",
        "",
        _count_sentence(grain, total, prefix="Case-grain suggestions"),
        "",
        (
            "The first-pass label is preserved, but the notes indicate that several "
            "parents appear to be routed to a different task grain. Suggestions are "
            "based on explicit notes where available and presentation metadata otherwise."
        ),
        "",
        "### Mandatory case inspection",
        "",
    ]
    lines.extend(_mandatory_case_lines(rows))
    lines += [
        "",
        "## 6. Scheduled-trip labeling findings",
        "",
        (
            f"{schedule_grain_count}/{total} cases are tentative single-trip candidates suitable for a "
            "blind scheduled-trip second pass. "
            + (
                f"Pilot-ready schedule-match cases are {', '.join(pilot_ids)}; they are still not calibration-ready without a blind second pass."
                if pilot_ids
                else "No schedule-match case passes even the pre-second-pass pilot gate."
            )
        ),
        "",
        (
            f"Among {match_count} matches, displayed overlap advantage is positive for "
            f"{overlap_sign['positive']}, zero for {overlap_sign['zero']}, negative for "
            f"{overlap_sign['negative']}, and unavailable for {overlap_sign['unavailable']}. "
            f"Displayed coverage advantage is positive for {coverage_sign['positive']}, "
            f"zero for {coverage_sign['zero']}, negative for {coverage_sign['negative']}, "
            f"and unavailable for {coverage_sign['unavailable']}. These are "
            "Markdown-table findings only; they do not establish that a card choice is incorrect."
        ),
        "",
        (
            f"The deterministic near-tie rule uses at most {NEAR_OVERLAP_TOLERANCE_S} s "
            f"overlap difference and {NEAR_COVERAGE_TOLERANCE_PCT_POINTS:g} percentage point "
            "coverage difference; it is an audit trigger, not a calibration threshold."
        ),
        "",
        "## 7. Segmentation and sequence insights",
        "",
        f"Suggested multi-trip parents ({len(multi_ids)}/{total}): {', '.join(multi_ids) or 'none'}.",
        "",
        (
            "Explicit card references and relative transition offsets are retained only "
            "as child-request context. Card letters remain display labels; every child "
            "case must resolve its own complete key and boundary interval."
        ),
        "",
        "## 8. Boundary-spanning insights",
        "",
        f"Suggested boundary-spanning parents ({len(boundary_ids)}/{total}): {', '.join(boundary_ids) or 'none'}.",
        "",
        (
            "These first-pass labels remain preserved while dual-role boundary evidence is "
            "routed separately. Stops and privacy-safe relative offsets are collected only "
            "when present in notes. No inbound or outbound candidate is invented, and no "
            "record is forced to one natural owner."
        ),
        "",
        "## 9. Fragment insights",
        "",
        (
            f"{grain.get('local_fragment', 0)}/{total} parent cases are suggested as local fragments; "
            f"{fragment_like_count}/{total} carry a conservative fragment-like audit flag, including "
            f"{boundary_fragment_count} boundary parents. Local scheduled context is not the "
            "same claim as complete performed-trip identity. Fragment role and stitch "
            "questions must precede full-trip scheduled judgment."
        ),
        "",
        f"Fragment-like audit flags ({len(fragment_ids)}/{total}): {', '.join(fragment_ids) or 'none'}.",
        "",
        "## 10. Source/time-quality insights",
        "",
        f"Suggested source/time-quality parents ({len(quality_ids)}/{total}): {', '.join(quality_ids) or 'none'}.",
        "",
        (
            "Explicit simultaneous-position, unsplit-trajectory, or DST elapsed-time "
            "evidence is routed to source/time-quality review rather than ordinary "
            "candidate ambiguity. Durations spanning an offset transition must use true "
            "elapsed time; naive Markdown wall-clock subtraction is not valid."
        ),
        "",
        "## 11. Reviewer-behavior insights",
        "",
        _heuristic_sentence(
            summary["human_decision_behavior"]["explicit_heuristic_counts"]
        ),
        "",
        "No heuristic is inferred for a blank note.",
        "",
        "## 12. Label-ontology gaps",
        "",
        (
            "The current primary labels conflate case grain, scheduled judgment, fragment "
            "role, source quality, and activity type. The minimal v2 proposal separates "
            "those dimensions, adds `not_applicable`, records boundary/segmentation detail, "
            "and separates grain confidence, match confidence, and evidence quality."
        ),
        "",
        (
            f"The inspected {design.get('existing_label_schema_version', 'existing')} schema records "
            f"reviewed_by_human={str(design.get('existing_label_schema_reviewed', False)).lower()} "
            "and has separate scheduled, boundary, attachment, and fragment sections, but it lacks "
            "the grain-first, quality, three-confidence, evidence-exposure, and blind-pass contract needed here."
        ),
        "",
        "## 13. UI gaps",
        "",
        (
            "The inspected UI offered map, timeline, time-distance, stop-ladder, and "
            "comparison-table panels. Previous/next chunks were enabled by default on the "
            "map, but panel views were not logged and Markdown summaries omitted adjacent "
            "context. The shortlist used an anchor winner/high qscore before neutral card "
            f"ordering (detected={str(ui_evidence.get('shortlist_uses_anchor_or_qscore', False)).lower()}); "
            "the full candidate list was inside diagnostics."
        ),
        "",
        "The second-pass UI should:",
        "",
        "- ask for case grain before scheduled judgment and disable inapplicable choices;",
        "- show adjacent context consistently across panels and log whether it was viewed, including relative boundary offsets;",
        "- retain five-position keys and name the third component `route_mapping_id`;",
        "- disclose shortlist construction and provide neutral access to every candidate;",
        "- require a rationale for confidence 4–5 and record which evidence panels were viewed;",
        "- support dual-role boundary, multi-subtrip, fragment-stitch, quality, and idle tasks;",
        "- render timezone-aware elapsed time and hide every first-pass field during a blind second pass.",
        "",
        "## 14. Label readiness",
        "",
        (
            f"Pilot-ready: {pilot_ready}/{total}. Blind second pass required: "
            f"{second_pass}/{total}. Child/role-case requests: "
            f"{len(child_needed_ids)}/{total}. Routed outside scheduled-trip second pass: "
            f"{len(task_specific_ids)}/{total}. Not evaluable from the current packet: "
            f"{not_evaluable}/{total}. Strict calibration-ready: {strict_ready}/{total}."
        ),
        "",
        "Readiness classes are nonexclusive and are audit routing, not correctness judgments.",
        "",
        "## 15. Possible next steps",
        "",
        (
            "The recommended immediate step is to freeze the v2 schema and UI contract, "
            f"then conduct a genuinely blind second pass on the {schedule_grain_count} single-trip candidates "
            "while issuing task-specific child requests for the other grains. The smallest "
            "high-value action is adding the grain-first field, explicit context display, "
            "and blind-pass metadata before another label is collected."
        ),
        "",
        (
            "Phase B remains blocked until the protocol is frozen and pilot disagreements "
            "are adjudicated. The factorial remains blocked until task-specific reviewed "
            "labels meet the strict gate, calibration/holdout membership is verified from "
            "the frozen preregistration, thresholds remain untouched until calibration, and "
            "the holdout-opening rule can be honored."
        ),
        "",
        "## 16. Explicit non-claims",
        "",
        "- No human label was overwritten.",
        "- No case was automatically relabeled.",
        "- No accuracy metric was calculated.",
        "- No architecture was selected.",
        "- The factorial was not run.",
        f"- R1 remains open and the current {total} cases are not claimed to be an independent holdout.",
        "",
        "### Highest-priority adjudication queue",
        "",
        "| case | priority | suggested task | reason summary |",
        "|---|---:|---|---|",
    ]
    for row in ordered[:10]:
        reasons = ", ".join(
            reason
            for reason in row["audit_reason_codes"]
            if reason
            not in {
                "MODEL_INFORMED_CANDIDATE_SHORTLIST",
                "PACKET_METADATA_NON_OPERATIONAL",
            }
        )
        lines.append(
            f"| {row['case_id']} | {row['review_priority_score']} | "
            f"{row['task_routing_suggestion']} | {reasons or 'no scored anomaly'} |"
        )
    return "\n".join(lines) + "\n"


def render_possible_next_steps(
    rows: Sequence[Mapping[str, Any]],
    design_contract_summary: Mapping[str, Any] | None = None,
) -> str:
    design = dict(design_contract_summary or {})
    schedule_cases = sorted(
        row["case_id"]
        for row in rows
        if row["case_grain_suggestion"] == "single_trip_candidate"
    )
    priority = sorted(
        rows, key=lambda row: (-int(row["review_priority_score"]), row["case_id"])
    )
    return "\n".join(
        [
            "# Possible next steps",
            "",
            "## Ranked recommendation",
            "",
            "1. **Option C — freeze the label schema and UI contract.** Highest information value per unit effort. Add grain-first routing, task-specific forms, complete-key display semantics, evidence-view logging, rationale requirements, timezone-aware context, and blind-pass metadata.",
            f"2. **Option A — blind second pass on the current schedule-grain subset.** Review only the {len(schedule_cases)} tentative single-trip parents with every first-pass label, confidence, note, reviewer identity, and model diagnostic hidden. Keep candidate presentation neutral and record which panels were viewed.",
            "3. **Option B — issue child-case requests.** Create per-subtrip and boundary requests for multi-trip parents; inbound/outbound dual-role requests for boundary parents; and fragment-role/stitch requests for local fragments. Do not prefill child judgments from parent notes.",
            f"4. **Option D — begin Phase B labeling only after protocol freeze.** The {design.get('phase_b_selected_count', 0)} preselected vehicle-days remain unprocessed. Start only after the revised schema, UI, training examples, adjudication rules, reviewer blinding, and privacy checks pass a pilot rerun.",
            "5. **Option E — keep the frozen factorial blocked.** Do not run it until reviewed task-specific labels satisfy the strict gate and the preregistered calibration/holdout split is verified independently.",
            "",
            "## Option A — blind second pass",
            "",
            f"Value: directly measures first-pass reproducibility on {len(schedule_cases)} schedule-grain cases without conflating other tasks. Cases: {', '.join(schedule_cases)}.",
            "",
            "Required UI changes: hide first-pass fields; ask grain first; store a complete candidate tuple; require rationale for high confidence; expose adjacent context consistently; record panels viewed and diagnostic reveals. Prioritize low-confidence and visible-tie cases. First-pass labels must remain hidden until the second save is immutable.",
            "",
            "## Option B — child cases",
            "",
            "Generate requests, not labels: per-subtrip cases for multi-trip parents; boundary intervals plus per-side roles; inbound/outbound cases for dual-role records; and fragment-role/stitch cases. Parent card letters are contextual display references only.",
            "",
            "## Option C — schema and UI",
            "",
            "Minimum schema: separate grain, scheduled judgment, fragment role/stitching, quality state, activity state, boundary/segmentation detail, three confidence dimensions, evidence exposure, review pass, blinding, and full candidate identity. This is the recommended immediate next step.",
            "",
            "## Option D — Phase B",
            "",
            "Phase B should not begin merely because first-pass pilot labels exist. Freeze the protocol, adjudicate pilot task routing, validate the revised UI with synthetic cases, define reviewer training and disagreements, then confirm the Phase B sample remains untouched.",
            "",
            "## Option E — factorial",
            "",
            "The factorial remains blocked. Before it can run: labels must be task-appropriate and independently reviewed; every scheduled match must carry a resolved complete key and rationale; blind second passes and adjudication must be complete; calibration/holdout membership must match the frozen preregistration; thresholds may be set only on calibration; and holdout may be opened once with no later threshold changes.",
            "",
            "## Smallest action with the highest information value",
            "",
            "Implement and test the grain-first blind-review form on the highest-priority parents before collecting another label. Current priority starts: "
            + ", ".join(
                f"{row['case_id']} ({row['review_priority_score']})"
                for row in priority[:5]
            )
            + ".",
            "",
            "This plan does not authorize calibration, threshold changes, architecture selection, Phase B processing, or the factorial.",
            "",
        ]
    )


def _mandatory_case_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    by_id = {row["case_id"]: row for row in rows}
    lines: list[str] = []
    if row := by_id.get("C-2630793f"):
        cards = "/".join(row["referenced_cards"]) or "unspecified display cards"
        lines.append(
            f"- **C-2630793f:** The first-pass {row['original_label']} label is preserved, "
            f"but the note explicitly indicates a multi-trip {cards} sequence; "
            f"{row['task_routing_suggestion']} is the suggested task."
        )
    if row := by_id.get("C-ba6b0e92"):
        cards = "/".join(row["referenced_cards"]) or "unspecified display cards"
        lines.append(
            f"- **C-ba6b0e92:** The first-pass {row['original_label']} label is preserved, "
            f"but the note explicitly indicates a multi-trip {cards} sequence with "
            "multiple boundaries; segmentation child cases are suggested."
        )
    for case_id in ("C-913f7d4c", "C-c953dac7"):
        if row := by_id.get(case_id):
            timezone_status = (
                "timezone text is inconsistent"
                if "REVIEWER_TIMEZONE_TEXT_INCONSISTENT" in row["audit_reason_codes"]
                else "no timezone-text inconsistency was detected"
            )
            lines.append(
                f"- **{case_id}:** The {row['original_label']} first pass is preserved, "
                f"but the note indicates a dual-role boundary at stop {row['boundary_stop']}; "
                f"its split is {row['boundary_offset_from_chunk_end_s']} s after the displayed chunk, and {timezone_status}."
            )
    if row := by_id.get("C-7d5fcf98"):
        lines.append(
            f"- **C-7d5fcf98:** The {row['original_label']} first pass is preserved, but "
            "the note describes an invalid source/time representation with simultaneous "
            "positions, not ordinary candidate ambiguity."
        )
    if row := by_id.get("C-1b38b137"):
        lines.append(
            f"- **C-1b38b137:** The {row['original_label']} first pass is preserved at "
            f"confidence {row['confidence']} and requires blind review; no confidence value is changed."
        )
    if row := by_id.get("C-7d4b42b8"):
        lines.append(
            f"- **C-7d4b42b8:** All {row['candidate_count']} quantified displayed cards "
            "have identical overlap and coverage, while notes and rationale are empty. "
            "This is a non-discrimination flag, not evidence that the selection is wrong."
        )
    if row := by_id.get("C-ba617064"):
        lines.append(
            f"- **C-ba617064:** The {row['chunk_duration_s']} s fragment has structured "
            "insufficient-context rationale; it is not reviewable as a full trip at the current grain."
        )
    if row := by_id.get("C-88e48154"):
        lines.append(
            f"- **C-88e48154:** The {row['original_label']} first pass is preserved, while "
            "the explicit idling note also routes it to a turnaround/idle/activity task."
        )
    return lines


def _count(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row[field]) for row in rows).items()))


def _cross_count(
    rows: Sequence[Mapping[str, Any]], row_field: str, column_field: str
) -> dict[str, dict[str, int]]:
    result: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        result[str(row[row_field])][str(row[column_field])] += 1
    return {key: dict(sorted(value.items())) for key, value in sorted(result.items())}


def _cross_count_with_bucket(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    result: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        result[duration_bucket(int(row["chunk_duration_s"]))][
            str(row["original_label"])
        ] += 1
    order = ["<10m", "10-<30m", "30-<60m", ">=60m", "unknown"]
    return {key: dict(sorted(result[key].items())) for key in order if key in result}


def _cross_count_fragment(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    result: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        group = "fragment" if row["original_stratum"] == "fragment" else "non_fragment"
        result[group][str(row["original_label"])] += 1
    return {key: dict(sorted(value.items())) for key, value in sorted(result.items())}


def _cross_count_audited_fragment(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    result: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        group = (
            "audit_suggested_fragment_like"
            if "LOCAL_FRAGMENT_NOT_FULL_TRIP" in row["audit_reason_codes"]
            else "no_fragment_audit_flag"
        )
        result[group][str(row["original_label"])] += 1
    return {key: dict(sorted(value.items())) for key, value in sorted(result.items())}


def _distribution(values: Sequence[int]) -> dict[str, int]:
    return dict(
        sorted(
            Counter(str(value) for value in values).items(),
            key=lambda item: int(item[0]),
        )
    )


def _advantage_sign_counts(
    rows: Sequence[Mapping[str, Any]], field: str
) -> dict[str, int]:
    result = Counter()
    for row in rows:
        value = row.get(field)
        if value is None:
            result["unavailable"] += 1
        elif float(value) > 0:
            result["positive"] += 1
        elif float(value) < 0:
            result["negative"] += 1
        else:
            result["zero"] += 1
    return {key: result[key] for key in ("positive", "zero", "negative", "unavailable")}


def _count_sentence(
    counts: Mapping[str, int], total: int, *, prefix: str = "Primary labels"
) -> str:
    body = ", ".join(f"{key}: {value}/{total}" for key, value in counts.items())
    return f"{prefix}: {body}."


def _heuristic_sentence(counts: Mapping[str, int]) -> str:
    if not counts:
        return "No explicit reviewer heuristic was recorded."
    return (
        "Explicit note heuristics: "
        + ", ".join(f"{key} ({value})" for key, value in counts.items())
        + "."
    )
