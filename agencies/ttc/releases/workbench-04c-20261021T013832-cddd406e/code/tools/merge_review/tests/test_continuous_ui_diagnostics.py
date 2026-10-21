"""Continuous diagnostics must not turn numerical fit into verified identity."""

import copy

import pytest

from tools.merge_review.gtfs_matching.adapter import public_gtfs
from tools.merge_review.gtfs_matching.diagnostic_candidates import (
    build_diagnostic_candidates,
)
from tools.merge_review.gtfs_matching.overview import (
    select_overview_candidate,
    visual_fragments,
)
from tools.merge_review.tests.test_gtfs_overview import fixture, point

COMPONENTS = (
    "absolute_delay_level_cost",
    "conditioned_fit_cost",
    "delay_dynamics_cost",
    "APC_stop_time_cost",
    "projection_and_coverage_cost",
    "pattern_progress_cost",
    "vehicle_sequence_cost",
)


def candidate(name="scheduled-a", score=1.0):
    coverage = {
        key: {"point_ratio": ratio, "duration_ratio": ratio}
        for key, ratio in (("projection", 0.9), ("schedule", 0.8), ("end_to_end", 0.72))
    }
    return {
        "candidate_id": name,
        "trip_id": name,
        "reference_id": "canonical",
        "scorer": "continuous_passage_v1",
        "score": score,
        "score_components": dict.fromkeys(COMPONENTS, 0.1),
        "score_components_unweighted": dict.fromkeys(COMPONENTS, 0.2),
        "score_component_weights": dict.fromkeys(COMPONENTS, 0.5),
        "coverage_metrics": coverage,
        "quality_eligible": True,
        "quality_gates": {
            "projection_coverage": True,
            "schedule_coverage": True,
            "end_to_end_coverage": True,
            "adequate_observations": True,
            "absolute_quality": True,
        },
        "probability": None,
        "calibrated": False,
        "estimated_delay_s": 60,
        "delay_uncertainty_s": 30,
        "delay_prior_source": "local_scheduled_headway",
        "whole_alignment": {
            "status": "EVIDENCE",
            "dtw_used": False,
            "coverage_metrics": coverage,
            "delay_process": [{"headway_s": 300}, {"headway_s": 600}],
        },
        "apc_support": {"count": 4, "support": 0.9, "supported_stops": 3},
        "scheduled_trajectory": [
            {"s_m": 0, "arrival_t": 100, "departure_t": 120},
            {"s_m": 1000, "arrival_t": 200, "departure_t": 200},
        ],
    }


def result(rows):
    return {
        "scorer": "continuous_passage_v1",
        "all_candidates": rows,
        "candidates": [],
        "status": "PATTERN_ONLY",
        "calibration": {"calibrated": False, "credible_set": []},
    }


def test_uncalibrated_numerical_pass_candidates_remain_top_three_diagnostics():
    source = result([candidate(str(index), index / 10) for index in reversed(range(5))])
    before = copy.deepcopy(source)
    payload = build_diagnostic_candidates(source)
    assert [row["trip_id"] for row in payload["candidates"]] == ["0", "1", "2"]
    assert payload["summary"]["total_failed"] == 5
    assert all(
        [gate["metric"] for gate in row["failed_gate_checks"]]
        == ["calibrated_credible_set"]
        for row in payload["candidates"]
    )
    assert source == before


def test_continuous_diagnostics_preserve_components_three_coverages_and_delay():
    stored = candidate()
    row = build_diagnostic_candidates(result([stored]))["candidates"][0]
    assert row["score_components"] == stored["score_components"]
    assert row["score_components_unweighted"] == stored["score_components_unweighted"]
    assert row["score_component_weights"] == stored["score_component_weights"]
    assert row["coverage_metrics"] == stored["coverage_metrics"]
    assert row["delay_uncertainty_s"] == 30
    assert row["headway_summary_s"] == {"min": 300, "median": 450, "max": 600}
    assert row["apc_support"]["supported_stops"] == 3
    assert "delay_process" not in row["whole_alignment"]
    assert row["whole_alignment"]["dtw_used"] is False
    assert row["probability"] is None


def test_saved_continuous_threshold_used_not_legacy_threshold():
    source = result([candidate(score=2)])
    row = build_diagnostic_candidates(
        source, {"continuous_match_cost_max": 1.5, "aggregation_match_cost_max": 10}
    )["candidates"][0]
    score_gate = next(
        gate for gate in row["failed_gate_checks"] if gate["metric"] == "score"
    )
    assert score_gate["threshold"] == 1.5


def test_overview_uses_diagnostic_one_for_uncalibrated_numerical_pass():
    source = result([candidate("best", 0.2), candidate("second", 0.4)])
    selected = select_overview_candidate(source)
    assert selected["trip_id"] == "best"
    assert selected["source"] == "fallback"
    assert selected["diagnostic_only"]
    assert selected["probability"] is None
    assert not selected["trip_identity_confirmed"]


def test_future_calibrated_credible_candidate_keeps_actual_probability():
    first, second = candidate("first"), candidate("second", 2)
    first.update(probability=0.95, calibrated=True)
    source = result([first, second])
    source.update(
        status="MATCHED",
        candidates=[first],
        calibration={"calibrated": True, "credible_set": ["first"]},
    )
    selected = select_overview_candidate(source)
    assert selected["source"] == "accepted"
    assert selected["calibrated"]
    assert selected["probability"] == 0.95
    assert not selected["trip_identity_confirmed"]
    assert build_diagnostic_candidates(source)["candidates"][0]["trip_id"] == "second"


def test_calibrated_probability_alone_cannot_bypass_credible_set_gate():
    first = candidate()
    first.update(probability=0.99, calibrated=True)
    source = result([first])
    source["calibration"]["calibrated"] = True
    selected = select_overview_candidate(source)
    assert selected["source"] == "fallback"
    assert selected["probability"] == 0.99  # retain evidence, not acceptance
    assert not selected["trip_identity_confirmed"]


def test_nonfinite_continuous_cost_is_not_manufactured_as_diagnostic():
    source = result([candidate(score=float("nan"))])
    payload = build_diagnostic_candidates(source)
    assert payload["candidates"] == []
    assert payload["summary"]["unranked_failed"] == 1
    assert select_overview_candidate(source) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope_separator_before", True),
        ("fragment_id", "other-fragment"),
        ("reference_id", "other-reference"),
    ],
)
def test_continuous_visual_fragments_preserve_physical_separators(field, value):
    rows = [point("a", 0, 0), point("b", 10, 100, **{field: value})]
    before = copy.deepcopy(rows)
    assert len(visual_fragments(rows, {"continuous_matching_enabled": True})) == 2
    assert rows == before


def test_continuous_visual_gap_is_not_legacy_dtw_gap():
    rows = [point("a", 0, 0), point("b", 120, 100)]
    assert len(visual_fragments(rows, {"score_max_gap_s": 300})) == 1
    assert (
        len(
            visual_fragments(
                rows,
                {
                    "continuous_matching_enabled": True,
                    "continuous_coverage_max_gap_s": 90,
                    "score_max_gap_s": 300,
                },
            )
        )
        == 2
    )


def test_continuous_visual_off_reference_does_not_reconnect():
    rows = [
        point("a", 0, 0),
        point("rejected", 10, 100, projection_status="OFF_REFERENCE"),
        point("b", 20, 200),
    ]
    fragments = visual_fragments(rows, {"continuous_matching_enabled": True})
    assert [[row["rid"] for row in group] for group in fragments] == [["a"], ["b"]]


def test_public_adapter_uses_continuous_fragment_connectivity_without_mutation():
    artifact, canonical, *_ = fixture()
    artifact["config"] = {
        "continuous_matching_enabled": True,
        "continuous_coverage_max_gap_s": 5,
    }
    before = copy.deepcopy(artifact)
    public = public_gtfs(artifact, canonical)
    trace = public["projection"]["by_reference"]["south-a"]
    assert len({row["fragment_id"] for row in trace}) == 3
    assert artifact == before
