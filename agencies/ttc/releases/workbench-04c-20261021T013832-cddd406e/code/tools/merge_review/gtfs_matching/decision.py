"""Conservative final-decision guard over already persisted aggregate evidence.

No projection, alignment, scoring, calibration fitting or threshold tuning occurs
here. Heavy evidence is structurally shared, never modified. The returned decision
fields and candidate annotations are new objects; callers must treat shared evidence
as immutable. This module is intentionally independent of the matching pipeline.
"""

from __future__ import annotations

import copy
import math

VERSION = "structural-competition-guard-v1"
DEFAULTS = {
    "aggregation_match_cost_max": 3.0,
    "aggregation_min_coverage": 0.35,
    "aggregation_ambiguity_margin": 0.1,
    "aggregation_output_max": 3,
    "aggregation_continuous_windows_min": 2,
}


def _number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _at_least(value, threshold):
    return _number(value) and value >= threshold


def _gates(candidate, config):
    threshold = config["aggregation_min_coverage"]
    gates = {
        "absolute_cost_passed": _number(candidate.get("score"))
        and candidate["score"] <= config["aggregation_match_cost_max"],
        "projection_coverage_passed": _at_least(candidate.get("coverage"), threshold),
        "window_coverage_passed": _at_least(
            candidate.get("schedule_supported_window_weight_ratio"), threshold
        ),
        "whole_evidence_passed": candidate.get("whole_alignment", {}).get("status")
        == "EVIDENCE",
        "whole_point_coverage_passed": _at_least(
            candidate.get("schedule_aligned_point_ratio"), threshold
        ),
        "whole_duration_coverage_passed": _at_least(
            candidate.get("schedule_aligned_duration_ratio"), threshold
        ),
        "finite_support_score": _number(candidate.get("support_score"))
        and 0 <= candidate["support_score"] <= 1,
        "finite_aggregate_cost": _number(candidate.get("score"))
        and candidate["score"] >= 0,
    }
    gates["structural_evidence_passed"] = all(
        value for key, value in gates.items() if key != "absolute_cost_passed"
    )
    return gates


def _snapshot(result):
    fields = (
        "status",
        "reported_candidate_id",
        "decision_candidate_id",
        "top1_top2_margin",
        "top1_top2_margin_definition",
        "top1_top2_margin_candidate_ids",
        "estimated_delay",
        "reasons",
        "pattern_confidence",
        "trip_confidence",
        "calibration",
    )
    snapshot = {key: copy.deepcopy(result.get(key)) for key in fields}
    snapshot["candidates"] = [
        {
            key: copy.deepcopy(candidate.get(key))
            for key in (
                "candidate_id",
                "trip_id",
                "service_date",
                "feed_version",
                "score",
                "support_score",
                "probability",
                "calibrated",
            )
        }
        for candidate in result.get("candidates", [])
    ]
    return snapshot


def apply_decision_guard(result, config=None):
    """Return a guarded decision, preserving all original aggregate/local evidence.

    Competitors are gated by projection, window and whole-alignment evidence,
    *not* by absolute aggregate cost. A cost-rejected but structurally supported
    near alternative can veto a claim of unique identity. No previously non-
    MATCHED result can be promoted to MATCHED. Probabilities remain uncalibrated.
    """
    if result.get("scorer") == "continuous_passage_v1":
        # The continuous scorer already applies absolute quality and calibrated
        # credible-set gates. Legacy DTW gates cannot reinterpret those costs.
        return dict(result)
    c = DEFAULTS | (config or {})
    thresholds = {key: c[key] for key in DEFAULTS}
    previous_guard = result.get("diagnostics", {}).get("final_decision_guard")
    if previous_guard and previous_guard.get("version") == VERSION:
        if previous_guard.get("thresholds") != thresholds:
            raise ValueError(
                "Reapply a different guard to the original decision, not a guarded one"
            )
        return dict(result)
    for key in DEFAULTS:
        if not _number(c[key]) or c[key] < 0:
            raise ValueError(f"Invalid decision threshold: {key}")
    cap = min(3, max(0, int(c["aggregation_output_max"])))
    original_status = result.get("status", "UNMATCHED")
    aggregates = result.get("all_candidates", [])
    gated = [(candidate, _gates(candidate, c)) for candidate in aggregates]
    competitors = sorted(
        (
            candidate
            for candidate, gates in gated
            if gates["structural_evidence_passed"]
        ),
        key=lambda candidate: (candidate["score"], str(candidate["candidate_id"])),
    )
    best = competitors[0] if competitors else None
    near = (
        [
            candidate
            for candidate in competitors
            if best["support_score"] - candidate["support_score"]
            <= c["aggregation_ambiguity_margin"]
        ]
        if best
        else []
    )
    gate_by_id = {candidate["candidate_id"]: gates for candidate, gates in gated}
    original_candidates = result.get("candidates", [])
    original_selected_id = (
        original_candidates[0].get("candidate_id")
        if original_candidates
        else result.get("decision_candidate_id")
    )
    status, guard_reasons = original_status, []
    if not best:
        guard_reasons.append("NO_STRUCTURALLY_SUPPORTED_COMPETITOR")
        if original_status == "MATCHED":
            status = "PATTERN_ONLY"
    elif original_status == "MATCHED":
        if not gate_by_id[best["candidate_id"]]["absolute_cost_passed"]:
            status = "PATTERN_ONLY"
            guard_reasons.append("ABSOLUTE_COST_GATE_NOT_MET")
        elif len(near) > 1:
            patterns = {candidate.get("pattern_id") for candidate in near}
            status = (
                "PATTERN_ONLY"
                if len(near) > 3 and len(patterns) == 1 and None not in patterns
                else "AMBIGUOUS"
            )
            guard_reasons.append("NEAR_EQUIVALENT_STRUCTURALLY_SUPPORTED_COMPETITORS")
        elif best["candidate_id"] != original_selected_id:
            status = "AMBIGUOUS"
            guard_reasons.append("ORIGINAL_MATCH_IDENTITY_NOT_RETAINED")
        elif not _at_least(
            best.get("continuous_support_windows"),
            c["aggregation_continuous_windows_min"],
        ):
            status = "PATTERN_ONLY"
            guard_reasons.append("CONTINUOUS_SUPPORT_GATE_NOT_MET")
    if any(
        not gate_by_id[candidate["candidate_id"]]["absolute_cost_passed"]
        for candidate in near[1:]
    ):
        guard_reasons.append("COST_REJECTED_NEAR_COMPETITOR_RETAINED_FOR_UNIQUENESS")
    if original_status != "MATCHED":
        guard_reasons.append("NO_PROMOTION_FROM_PRIOR_NONMATCHED_DECISION")
    shown = []
    for candidate in near[:cap]:
        gates = dict(gate_by_id[candidate["candidate_id"]])
        gates.update(
            absolute_cost_threshold=c["aggregation_match_cost_max"],
            support_delta_to_best=best["support_score"] - candidate["support_score"],
            role="COST_ACCEPTABLE_HYPOTHESIS"
            if gates["absolute_cost_passed"]
            else "COST_REJECTED_NEAR_COMPETITOR",
            candidate_only=True,
        )
        shown.append(
            dict(candidate, decision_gate=gates, probability=None, calibrated=False)
        )
    if status == "MATCHED" and not shown:
        status = "PATTERN_ONLY"
        guard_reasons.append("NO_DISPLAYED_MATCH_CANDIDATE")
    second = competitors[1] if len(competitors) > 1 else None
    margin = best["support_score"] - second["support_score"] if second else None
    prior_reasons = list(result.get("reasons", []))
    if best:
        prior_reasons = [
            reason
            for reason in prior_reasons
            if reason
            not in {
                "WHOLE_ALIGNMENT_OR_SCHEDULE_COVERAGE_NOT_VALIDATED",
                "NO_EVIDENCE_AFTER_REFERENCE_SPECIFIC_QUALITY_THRESHOLDS",
            }
        ]
    output = dict(result)
    output.update(
        status=status,
        candidates=shown,
        reported_candidate_id=best["candidate_id"] if shown else None,
        decision_candidate_id=best["candidate_id"] if shown else None,
        top1_top2_margin=margin,
        top1_top2_margin_candidate_ids=[best["candidate_id"], second["candidate_id"]]
        if second
        else [],
        top1_top2_margin_definition=(
            "Support-score difference between the best and next aggregate with "
            "sufficient projection/window/whole evidence, before absolute-cost "
            "gating and before the display cap. Not calibrated probability."
        ),
        estimated_delay=best.get("estimated_delay_s") if shown else None,
        pattern_confidence={
            "support_score": best.get("support_score") if shown else None,
            "calibrated_probability": None,
            "definition": "Heuristic support only; not calibrated probability",
        },
        trip_confidence={
            "support_score": best.get("support_score") if shown else None,
            "calibrated_probability": None,
            "identity_separated": status == "MATCHED",
            "definition": (
                "Uniqueness checked against all structurally supported "
                "aggregates, including cost-rejected competitors"
            ),
        },
        calibration={
            "calibrated": False,
            "probability": None,
            "status": "UNCALIBRATED_FINAL_DECISION_GUARD",
        },
        reasons=list(dict.fromkeys([*prior_reasons, *guard_reasons])),
    )
    coverage_keys = (
        "valid_projection_point_ratio",
        "valid_projection_duration_ratio",
        "valid_projection_duration_s",
        "valid_projection_progress",
        "off_reference_ratio",
        "longest_valid_contiguous_run",
        "longest_valid_contiguous_run_points",
        "coverage_duration_definition",
        "schedule_aligned_point_ratio",
        "schedule_aligned_duration_ratio",
        "schedule_supported_window_weight_ratio",
    )
    output["evidence_coverage"] = (
        {key: best[key] for key in coverage_keys if key in best}
        if shown
        else copy.deepcopy(result.get("evidence_coverage", {}))
    )
    output["supporting_window_count"] = (
        sum(
            w.get("score") is not None
            for w in result.get("local_scores", {}).get(best["candidate_id"], [])
        )
        if shown
        else 0
    )
    output["diagnostics"] = dict(result.get("diagnostics", {}))
    output["diagnostics"]["final_decision_guard"] = {
        "version": VERSION,
        "thresholds": thresholds,
        "original_decision": _snapshot(result),
        "reasons": guard_reasons,
        "competition_definition": (
            "All aggregates passing projection/window/whole evidence gates; "
            "absolute cost is not a uniqueness filter"
        ),
        "structurally_supported_count": len(competitors),
        "near_equivalent_count": len(near),
        "near_equivalent_candidate_ids": [
            candidate["candidate_id"] for candidate in near
        ],
        "absolute_cost_rejected_near_candidate_ids": [
            candidate["candidate_id"]
            for candidate in near
            if not gate_by_id[candidate["candidate_id"]]["absolute_cost_passed"]
        ],
        "excluded_insufficient_evidence_candidate_ids": [
            candidate["candidate_id"]
            for candidate, gates in gated
            if not gates["structural_evidence_passed"]
        ],
        "display_cap": cap,
        "heavy_evidence_recomputed": False,
        "aggregate_scores_changed": False,
        "prior_nonmatched_promoted": False,
    }
    return output
