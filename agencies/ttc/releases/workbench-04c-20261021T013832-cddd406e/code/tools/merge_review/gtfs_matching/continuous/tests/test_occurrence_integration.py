"""04c cross-stage regressions: no historical winner or identifier fixtures."""

import copy

import pytest

from tools.merge_review.gtfs_matching.continuous.config import get_config
from tools.merge_review.gtfs_matching.continuous.serialization import validate_result
from tools.merge_review.gtfs_matching.continuous.tests.test_continuous_pipeline import (
    points,
    run,
    schedule,
)

ENABLED = {"continuous_occurrence_model_enabled": True}


def test_new_model_keeps_experiment04_weights_not04b():
    config = get_config(ENABLED)
    assert config["continuous_absolute_delay_weight"] == 1
    assert config["continuous_delay_dynamics_weight"] == 0.5
    assert config["continuous_occurrence_displacement_weight"] == 1


def test_common_pattern_class_no_exact_pattern_identity_bonus():
    first = schedule("first", 0)
    same = schedule("same-time-other-pattern", 0)
    same["pattern_id"] = "isolated-exact-pattern"
    trips = [first, same, schedule("previous", -600), schedule("next", 600)]
    result = run(trips=trips, config=ENABLED)
    rows = {c["trip_id"]: c for c in result["all_candidates"]}
    a, b = rows[first["trip_id"]], rows[same["trip_id"]]
    assert a["score"] == pytest.approx(b["score"], abs=1e-8)
    assert a["delay_prior_source"] != "local_scheduled_headway"
    assert a["operational_context"] == b["operational_context"]
    assert (
        a["occurrence_displacement"]["context"]["member_ids"]
        == b["occurrence_displacement"]["context"]["member_ids"]
    )
    assert validate_result(result)["status"] == "PASS"


def test_near_schedule_and_headway_shift_have_distinct_cost_components():
    result = run(config=ENABLED)
    rows = {c["trip_id"]: c for c in result["all_candidates"]}
    near, shifted = rows["scheduled-A"], rows["scheduled-previous"]
    assert near["score"] < shifted["score"]
    assert shifted["occurrence_displacement"]["crossed_occurrences"] >= 1
    assert shifted["occurrence_decision_guard"]["allowed"] is False
    assert (
        shifted["score_components"]["occurrence_displacement_cost"]
        > near["score_components"]["occurrence_displacement_cost"]
    )
    assert result["candidates"] == []
    assert result["status"] != "MATCHED"
    assert len(near["score_components"]) == 8
    assert near["score"] == pytest.approx(sum(near["score_components"].values()))


def test_legacy_off_still_has_seven_components():
    result = run()
    assert len(result["all_candidates"][0]["score_components"]) == 7
    assert "occurrence_displacement" not in result["all_candidates"][0]


def test_new_model_does_not_mutate_or_use_upstream_schedule_identity():
    rows = points()
    original = copy.deepcopy(rows)
    baseline = run(rows=rows, config=ENABLED)
    assert rows == original
    for row in rows:
        row.update(AvgOTP=-5000, assigned_trip_id="some-vendor-trip", vendor_delay=9000)
    contaminated = run(rows=rows, config=ENABLED)
    assert [c["score"] for c in baseline["all_candidates"]] == [
        c["score"] for c in contaminated["all_candidates"]
    ]


@pytest.mark.parametrize("step", [10, 15, 20, 30])
def test_occurrence_identity_is_not_an_avl_ping_density_vote(step):
    result = run(rows=points(step=step), config=ENABLED)
    assert result["all_candidates"][0]["trip_id"] == "scheduled-A"
    assert result["status"] != "MATCHED"
    assert (
        result["all_candidates"][0]["coverage_metrics"]["projection"]["duration_ratio"]
        == 1
    )


def test_resampling_parameter_does_not_change_new_candidate_ranking():
    signatures = []
    for interval in (5, 10, 15, 20):
        result = run(config={**ENABLED, "score_reference_sample_s": interval})
        signatures.append(
            [
                (r["candidate_id"], r["score"], r["coverage_metrics"])
                for r in result["all_candidates"]
            ]
        )
    assert all(signature == signatures[0] for signature in signatures)


def test_local_window_cache_is_exact_and_never_reuses_candidate_scores():
    trips = [schedule(f"scheduled-{i}", i * 300) for i in range(-3, 4)]
    cached = run(trips=trips, config=ENABLED)
    uncached = run(
        trips=trips, config={**ENABLED, "continuous_local_evidence_cache_size": 0}
    )
    assert cached["all_candidates"] == uncached["all_candidates"]
    assert cached["local_scores"] == uncached["local_scores"]
    assert cached["status"] == uncached["status"]
    assert cached["scorer_fingerprint"] == uncached["scorer_fingerprint"]
