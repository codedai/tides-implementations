"""Pure final-decision counterexamples; no synthetic operational ground truth."""

import copy
import math

import pytest

from tools.merge_review.gtfs_matching.decision import apply_decision_guard


def candidate(name, score, *, coverage=1, whole="EVIDENCE", pattern="p"):
    return {
        "candidate_id": name,
        "trip_id": name,
        "feed_version": "feed",
        "service_date": "2023-11-06",
        "reference_id": "reference",
        "pattern_id": pattern,
        "score": score,
        "support_score": math.exp(-score),
        "coverage": coverage,
        "schedule_supported_window_weight_ratio": 1,
        "schedule_aligned_point_ratio": 1,
        "schedule_aligned_duration_ratio": 1,
        "whole_alignment": {"status": whole, "score": 1, "delay_process": []},
        "continuous_support_windows": 20,
        "estimated_delay_s": 60,
        "probability": None,
        "calibrated": False,
        "valid_projection_point_ratio": coverage,
    }


def result(candidates, status="MATCHED"):
    return {
        "status": status,
        "candidates": [candidates[0]] if candidates else [],
        "all_candidates": candidates,
        "reported_candidate_id": candidates[0]["candidate_id"] if candidates else None,
        "decision_candidate_id": candidates[0]["candidate_id"] if candidates else None,
        "top1_top2_margin": None,
        "reasons": [],
        "diagnostics": {"source_observations": 200},
        "local_scores": {c["candidate_id"]: [{"score": 1}] for c in candidates},
        "retrieval": {"candidate_ids": [c["candidate_id"] for c in candidates]},
        "windows": {"windows": [{"id": "atomic", "source_ids": ["a", "b"]}]},
    }


def test_cost_threshold_cannot_erase_near_competitor_and_manufacture_uniqueness():
    source = result([candidate("first", 2.98), candidate("second", 3.06)])
    before = copy.deepcopy(source)
    guarded = apply_decision_guard(source)
    assert source == before
    assert guarded["status"] == "AMBIGUOUS"
    assert [c["candidate_id"] for c in guarded["candidates"]] == ["first", "second"]
    assert guarded["candidates"][0]["decision_gate"]["absolute_cost_passed"]
    assert not guarded["candidates"][1]["decision_gate"]["absolute_cost_passed"]
    assert (
        guarded["candidates"][1]["decision_gate"]["role"]
        == "COST_REJECTED_NEAR_COMPETITOR"
    )
    assert guarded["top1_top2_margin"] == pytest.approx(
        math.exp(-2.98) - math.exp(-3.06)
    )
    assert guarded["top1_top2_margin_candidate_ids"] == ["first", "second"]
    assert guarded["all_candidates"] == before["all_candidates"]
    assert guarded["local_scores"] == before["local_scores"]
    assert guarded["retrieval"] == before["retrieval"]
    assert guarded["windows"] == before["windows"]
    audit = guarded["diagnostics"]["final_decision_guard"]
    assert audit["original_decision"]["status"] == "MATCHED"
    assert audit["absolute_cost_rejected_near_candidate_ids"] == ["second"]
    assert not audit["aggregate_scores_changed"]
    assert not audit["heavy_evidence_recomputed"]


def test_well_separated_match_stays_matched_without_threshold_tuning():
    guarded = apply_decision_guard(
        result([candidate("clear", 0.2), candidate("far", 4)])
    )
    assert guarded["status"] == "MATCHED"
    assert len(guarded["candidates"]) == 1
    assert guarded["top1_top2_margin"] > 0.1


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("coverage", 0.1),
        ("schedule_supported_window_weight_ratio", 0.1),
        ("schedule_aligned_point_ratio", 0.1),
        ("schedule_aligned_duration_ratio", 0.1),
        ("whole_alignment", {"status": "NO_EVIDENCE"}),
    ],
)
def test_insufficient_evidence_competitor_does_not_create_ambiguity(field, bad_value):
    rival = candidate("insufficient", 3.06)
    rival[field] = bad_value
    guarded = apply_decision_guard(result([candidate("first", 2.98), rival]))
    assert guarded["status"] == "MATCHED"
    assert len(guarded["candidates"]) == 1
    assert guarded["top1_top2_margin"] is None
    assert guarded["diagnostics"]["final_decision_guard"][
        "excluded_insufficient_evidence_candidate_ids"
    ] == ["insufficient"]


@pytest.mark.parametrize(
    "status", ["AMBIGUOUS", "PATTERN_ONLY", "UNMATCHED", "BOUNDARY_UNCERTAIN"]
)
def test_prior_nonmatched_status_is_never_promoted(status):
    guarded = apply_decision_guard(result([candidate("clear", 0.2)], status=status))
    assert guarded["status"] == status
    assert not guarded["trip_confidence"]["identity_separated"]


def test_same_pattern_near_competitors_remain_inspectable_with_maximum_three():
    source = result([candidate(str(i), 2.98 + i * 0.03) for i in range(8)])
    guarded = apply_decision_guard(source)
    assert guarded["status"] == "PATTERN_ONLY"
    assert len(guarded["candidates"]) == 3
    assert len(guarded["all_candidates"]) == 8
    assert guarded["diagnostics"]["final_decision_guard"]["near_equivalent_count"] == 8


def test_decision_guard_never_fabricates_or_inherits_calibrated_probability():
    source = result([candidate("first", 0.2)])
    source["candidates"][0].update(probability=0.99, calibrated=True)
    before = copy.deepcopy(source)
    guarded = apply_decision_guard(source)
    assert guarded["candidates"][0]["probability"] is None
    assert guarded["trip_confidence"]["calibrated_probability"] is None
    assert guarded["calibration"]["probability"] is None
    assert not guarded["calibration"]["calibrated"]
    assert source == before
    assert guarded["all_candidates"] == before["all_candidates"]
    assert (
        guarded["diagnostics"]["final_decision_guard"]["original_decision"][
            "candidates"
        ][0]["probability"]
        == 0.99
    )


def test_guard_is_idempotent_but_different_thresholds_require_original_result():
    guarded = apply_decision_guard(
        result([candidate("first", 2.98), candidate("second", 3.06)])
    )
    assert apply_decision_guard(guarded) == guarded
    with pytest.raises(ValueError, match="original decision"):
        apply_decision_guard(guarded, {"aggregation_ambiguity_margin": 0.2})


def test_no_structural_candidate_cannot_retain_matched_status():
    source = result([candidate("bad", 1, whole="NO_EVIDENCE")])
    source["evidence_coverage"] = {
        "valid_projection_point_ratio": 0.5,
        "valid_projection_duration_ratio": 0.4,
        "valid_projection_progress": 1000,
        "off_reference_ratio": 0.5,
        "longest_valid_contiguous_run": 120,
    }
    guarded = apply_decision_guard(source)
    assert guarded["status"] == "PATTERN_ONLY"
    assert guarded["candidates"] == []
    assert guarded["reported_candidate_id"] is None
    assert guarded["estimated_delay"] is None
    assert guarded["evidence_coverage"] == source["evidence_coverage"]
    assert guarded["evidence_coverage"] is not source["evidence_coverage"]
