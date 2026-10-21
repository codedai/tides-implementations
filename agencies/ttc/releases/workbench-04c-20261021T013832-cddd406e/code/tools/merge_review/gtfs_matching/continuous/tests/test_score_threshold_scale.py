"""Score-scale regressions use synthetic evidence, never historical winners."""

import copy

import pytest

from tools.merge_review.gtfs_matching.continuous import integration
from tools.merge_review.gtfs_matching.continuous.config import get_config
from tools.merge_review.gtfs_matching.continuous.retrieval import SchedulePool
from tools.merge_review.gtfs_matching.continuous.serialization import validate_result
from tools.merge_review.gtfs_matching.continuous.tests.test_continuous_pipeline import (
    apc,
    points,
    run,
    schedule,
)

REWEIGHTED = {
    "continuous_absolute_delay_weight": 10.0,
    "continuous_delay_dynamics_weight": 0.1,
    "continuous_match_cost_max": 7.3,
    "continuous_calibration_max_score": 7.3,
    "continuous_local_support_cost_max": 16.5,
    "continuous_apc_support_cost_max": 3.0,
    "continuous_local_missing_cost": 22.0,
    "continuous_boundary_penalty": 2.75,
    "continuous_boundary_gain_min": 2.75,
}


def atomic(index, score):
    return {
        "window_id": f"atomic-{index}",
        "kind": "atomic",
        "start": index * 60,
        "end": (index + 1) * 60,
        "weight": 1.0,
        "quality_eligible": True,
        "status": "NO_CANDIDATE" if score is None else "EVIDENCE",
        "score": score,
    }


def test_weighted_avl_support_gate_does_not_relax_raw_apc_support():
    event = apc(stop=0)
    # This is a real APC interval within the AVL observation time span, but at
    # the wrong scheduled stop time. Its raw Huber cost is between 3 and 16.5.
    for key in ("t", "interval_start", "interval_end"):
        event[key] += 540
    source = copy.deepcopy(event)
    baseline = run(trips=[schedule()], apc_rows=[event])
    reweighted = run(trips=[schedule()], apc_rows=[event], config=REWEIGHTED)
    for result in (baseline, reweighted):
        support = result["all_candidates"][0]["apc_support"]
        assert support["count"] == 1
        assert 3.0 < support["rows"][0]["cost"] < 16.5
        assert support["supported_stops"] == 0
    assert event == source
    assert (
        baseline["all_candidates"][0]["apc_support"]
        == reweighted["all_candidates"][0]["apc_support"]
    )
    # Demonstrate that only the dedicated APC knob controls this raw-cost gate.
    relaxed_apc = run(
        trips=[schedule()],
        apc_rows=[event],
        config={**REWEIGHTED, "continuous_apc_support_cost_max": 16.5},
    )
    assert relaxed_apc["all_candidates"][0]["apc_support"]["supported_stops"] == 1


@pytest.mark.parametrize("whole_gate", [1.0, 7.3, 100.0])
def test_missing_local_summary_cost_is_independent_of_whole_gate(
    monkeypatch, whole_gate
):
    local = [atomic(0, 2.0), atomic(1, None)]
    monkeypatch.setattr(integration, "_local_evidence", lambda *args: local)
    config = get_config({**REWEIGHTED, "continuous_match_cost_max": whole_gate})
    trip = schedule()
    candidate, returned = integration.score_candidate(
        points(), [], trip, SchedulePool([trip], config), {"windows": []}, config
    )
    assert returned == local
    assert candidate["weighted_mean"] == pytest.approx((2 + 22) / 2)
    assert candidate["trimmed_mean"] == pytest.approx((2 + 22) / 2)
    assert candidate["weighted_p90"] == pytest.approx(22)
    assert candidate["continuous_support_windows"] == 1


@pytest.mark.parametrize("whole_gate", [1.0, 7.3, 100.0])
def test_boundary_uses_local_missing_cost_and_local_scale(whole_gate):
    candidates = [{"candidate_id": "first"}, {"candidate_id": "second"}]
    local = {
        "first": [atomic(i, 0.0 if i < 3 else None) for i in range(6)],
        "second": [atomic(i, None if i < 3 else 0.0) for i in range(6)],
    }
    config = get_config({**REWEIGHTED, "continuous_match_cost_max": whole_gate})
    result = integration.boundary_diagnostic(candidates, local, config)
    assert result["single_candidate_cost"] == pytest.approx(11)
    assert result["split_cost"] == pytest.approx(2.75)
    assert result["gain"] == pytest.approx(8.25)
    assert result["change_time"] == 180
    assert result["left_candidate_id"] == "first"
    assert result["right_candidate_id"] == "second"
    assert result["boundary_uncertain"] is True


def test_final_reweighting_preserves_shared_delay_raw_costs_and_coverage():
    rows = points()
    # A smooth, nonconstant delay exercises both the level and dynamics terms.
    for index, row in enumerate(rows):
        row["t"] += index * 0.6
    original = copy.deepcopy(rows)
    baseline, reweighted = run(rows), run(rows, config=REWEIGHTED)
    before = {c["candidate_id"]: c for c in baseline["all_candidates"]}
    after = {c["candidate_id"]: c for c in reweighted["all_candidates"]}
    assert before.keys() == after.keys()
    assert before
    for identity, old in before.items():
        new = after[identity]
        raw = old["score_components_unweighted"]
        assert raw["absolute_delay_level_cost"] > 0
        assert raw["delay_dynamics_cost"] > 0
        for key in (
            "score_components_unweighted",
            "delay_fit",
            "estimated_delay_s",
            "delay_mad_s",
            "delay_uncertainty_s",
            "coverage_metrics",
            "whole_alignment",
        ):
            assert new[key] == old[key], key
        assert new["score_components"]["absolute_delay_level_cost"] == pytest.approx(
            10 * raw["absolute_delay_level_cost"]
        )
        assert new["score_components"]["delay_dynamics_cost"] == pytest.approx(
            0.1 * raw["delay_dynamics_cost"]
        )
        assert new["score"] == pytest.approx(
            old["score"]
            + 9 * raw["absolute_delay_level_cost"]
            - 0.4 * raw["delay_dynamics_cost"]
        )
    assert rows == original
    config = get_config(REWEIGHTED)
    assert config["continuous_delay_prior_weight"] == 1.0
    assert config["continuous_delay_smooth_weight"] == 0.3
    assert baseline["scorer_fingerprint"] != reweighted["scorer_fingerprint"]
    for result in (baseline, reweighted):
        assert result["status"] == "PATTERN_ONLY"
        assert result["candidates"] == []
        assert all(c["probability"] is None for c in result["all_candidates"])
        assert "NO_CALIBRATED_SCHEDULED_OCCURRENCE_CREDIBLE_SET" in result["reasons"]
        assert validate_result(result)["status"] == "PASS"


@pytest.mark.parametrize(
    "key", ["continuous_apc_support_cost_max", "continuous_local_missing_cost"]
)
@pytest.mark.parametrize("value", [-1.0, float("inf"), float("nan"), True])
def test_independent_cost_limits_reject_invalid_configuration(key, value):
    with pytest.raises(ValueError):
        get_config({key: value})


def test_default_independent_limits_preserve_existing_experiment_scale():
    config = get_config()
    assert config["continuous_apc_support_cost_max"] == 3.0
    assert config["continuous_local_missing_cost"] == 4.0
    assert config["continuous_local_support_cost_max"] == 3.0
    assert config["continuous_match_cost_max"] == 3.0


@pytest.mark.parametrize("missing_cost", [0.0, 4.0, 16.5])
def test_missing_local_evidence_cannot_cost_less_than_an_accepted_window(
    missing_cost,
):
    with pytest.raises(ValueError):
        get_config({**REWEIGHTED, "continuous_local_missing_cost": missing_cost})


@pytest.mark.parametrize("local_gate,expected_support", [(3.0, 0), (16.5, 2)])
def test_local_support_count_uses_its_own_weighted_gate(
    monkeypatch, local_gate, expected_support
):
    local = [atomic(0, 10.0), atomic(1, 10.0)]
    monkeypatch.setattr(integration, "_local_evidence", lambda *args: local)
    config = get_config(
        {**REWEIGHTED, "continuous_local_support_cost_max": local_gate}
    )
    trip = schedule()
    candidate, _ = integration.score_candidate(
        points(), [], trip, SchedulePool([trip], config), {"windows": []}, config
    )
    assert candidate["weighted_mean"] == 10.0
    assert candidate["continuous_support_windows"] == expected_support
