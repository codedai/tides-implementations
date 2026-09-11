"""Rejected-candidate views must explain saved evidence without changing decisions."""

import copy
import math

import pytest

from tools.merge_review.gtfs_matching.diagnostic_candidates import (
    build_diagnostic_candidates,
)


def aggregate(trip_id="trip-a", **overrides):
    """A scored, admissible aggregate using the persisted matching-stage schema."""
    candidate = {
        "candidate_id": f"feed-29|2023-11-06|{trip_id}",
        "trip_id": trip_id,
        "service_date": "2023-11-06",
        "feed_version": "feed-29",
        "reference_id": "south-reference",
        "family_id": "south-family",
        "pattern_id": "south-pattern",
        "score": 1.0,
        "support_score": math.exp(-1),
        "coverage": 0.9943,
        "schedule_supported_window_weight_ratio": 0.9212,
        "schedule_aligned_point_ratio": 0.8,
        "schedule_aligned_duration_ratio": 0.8,
        "continuous_support_windows": 10,
        "estimated_delay_s": 480,
        "delay_mad_s": 60,
        "whole_alignment": {"status": "EVIDENCE", "score": 1.0},
        "probability": None,
        "calibrated": False,
        "scheduled_trajectory": [
            {
                "s_m": 100.0,
                "arrival_t": 1_699_298_400.0,
                "departure_t": 1_699_298_430.0,
                "stop_id": "stop-a",
                "stop_sequence": 1,
                "reference_id": "south-reference",
            },
            {
                "s_m": 500.0,
                "arrival_t": 1_699_298_700.0,
                "departure_t": 1_699_298_760.0,
                "stop_id": "stop-b",
                "stop_sequence": 2,
                "reference_id": "south-reference",
            },
        ],
    }
    candidate.update(overrides)
    return candidate


def result(aggregates=(), displayed=()):
    return {
        "status": "PATTERN_ONLY",
        "candidates": list(displayed),
        "all_candidates": list(aggregates),
        "reasons": ["WHOLE_TRIP_ALIGNMENT_NOT_VALIDATED"],
    }


def gate(candidate, metric, operator=None):
    matching = [
        row
        for row in candidate["gate_checks"]
        if row["metric"] == metric and (operator is None or row["operator"] == operator)
    ]
    assert len(matching) == 1, (metric, operator, matching)
    return matching[0]


def test_south_0004_like_coverage_failures_show_three_lowest_cost_in_stable_order():
    # Seven locally scored schedules can all fail whole-trip coverage even when
    # point projection and local-window coverage are strong.
    rows = [
        aggregate(
            trip_id,
            score=score,
            schedule_aligned_point_ratio=0.03125,
            schedule_aligned_duration_ratio=0.02965,
        )
        for trip_id, score in [
            ("trip-g", 2.8),
            ("trip-d", 2.4),
            ("trip-c", 2.1),
            ("trip-b", 2.1),
            ("trip-a", 2.0),
            ("trip-f", 2.6),
            ("trip-e", 2.5),
        ]
    ]
    payload = build_diagnostic_candidates(result(rows))
    assert [c["trip_id"] for c in payload["candidates"]] == [
        "trip-a",
        "trip-b",
        "trip-c",
    ]
    assert [c["diagnostic_rank"] for c in payload["candidates"]] == [1, 2, 3]
    assert payload["summary"]["limit"] == 3
    assert payload["summary"]["total_failed"] == 7
    assert payload["summary"]["already_displayed_failed"] == 0
    assert payload["summary"]["remaining_failed"] == 7
    assert payload["summary"]["shown_count"] == 3
    assert payload["summary"]["ranking"]
    first = payload["candidates"][0]
    assert {g["metric"] for g in first["failed_gate_checks"]} == {
        "schedule_aligned_point_ratio",
        "schedule_aligned_duration_ratio",
    }
    for metric, actual in (
        ("schedule_aligned_point_ratio", 0.03125),
        ("schedule_aligned_duration_ratio", 0.02965),
    ):
        check = gate(first, metric, ">=")
        assert check["actual"] == actual
        assert check["threshold"] == 0.35
        assert check["passed"] is False
        assert check["label"] and check["unit"]
    assert gate(first, "coverage", ">=")["passed"] is True
    assert gate(first, "schedule_supported_window_weight_ratio", ">=")["passed"]


def test_saved_config_thresholds_control_failures_instead_of_current_defaults():
    rows = [
        aggregate("only-custom-cost-fails", score=2.2),
        aggregate("only-custom-coverage-fails", score=1.5, coverage=0.5),
    ]
    saved = {"aggregation_match_cost_max": 2.0, "aggregation_min_coverage": 0.6}
    payload = build_diagnostic_candidates(result(rows), saved)
    by_trip = {c["trip_id"]: c for c in payload["candidates"]}
    assert set(by_trip) == {
        "only-custom-cost-fails",
        "only-custom-coverage-fails",
    }
    check = gate(by_trip["only-custom-cost-fails"], "score", "<=")
    assert check["actual"] == 2.2 and check["threshold"] == 2.0
    assert check["passed"] is False
    check = gate(by_trip["only-custom-coverage-fails"], "coverage", ">=")
    assert check["actual"] == 0.5 and check["threshold"] == 0.6
    assert check["passed"] is False
    # Both are admissible under default gates, so neither should be described
    # as rejected when the saved run actually uses those default thresholds.
    assert build_diagnostic_candidates(result(rows))["candidates"] == []


def test_inclusive_threshold_boundary_is_not_a_failure():
    row = aggregate(
        score=3.0,
        coverage=0.35,
        schedule_supported_window_weight_ratio=0.35,
        schedule_aligned_point_ratio=0.35,
        schedule_aligned_duration_ratio=0.35,
    )
    assert build_diagnostic_candidates(result([row]))["candidates"] == []


def test_final_hypotheses_are_not_duplicated_even_when_absolute_cost_failed():
    displayed = aggregate("already-visible", score=3.1)
    rejected = aggregate("new-diagnostic", schedule_aligned_point_ratio=0.03)
    payload = build_diagnostic_candidates(
        result([displayed, rejected], [copy.deepcopy(displayed)])
    )
    assert [c["trip_id"] for c in payload["candidates"]] == ["new-diagnostic"]
    assert payload["summary"]["total_failed"] == 2
    assert payload["summary"]["already_displayed_failed"] == 1
    assert payload["summary"]["remaining_failed"] == 1
    assert payload["summary"]["shown_count"] == 1


def test_passing_candidates_outside_final_top_three_are_not_mislabeled_rejected():
    rows = [aggregate(f"admissible-{n}", score=0.1 + n * 0.1) for n in range(7)]
    payload = build_diagnostic_candidates(result(rows, rows[:3]))
    assert payload["candidates"] == []
    assert payload["summary"]["total_failed"] == 0
    assert payload["summary"]["shown_count"] == 0


def test_same_trip_id_on_other_service_day_is_not_excluded_as_final_duplicate():
    displayed = aggregate("reused-trip-id", score=3.1)
    previous_day = aggregate(
        "reused-trip-id",
        candidate_id="feed-29|2023-11-05|reused-trip-id",
        service_date="2023-11-05",
        score=3.2,
    )
    other_feed = aggregate(
        "reused-trip-id",
        candidate_id="other-feed|2023-11-06|reused-trip-id",
        feed_version="other-feed",
        score=3.3,
    )
    payload = build_diagnostic_candidates(
        result([displayed, previous_day, other_feed], [displayed])
    )
    assert [c["candidate_id"] for c in payload["candidates"]] == [
        previous_day["candidate_id"],
        other_feed["candidate_id"],
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"candidates": []},
        {"all_candidates": [], "retrieval": {"candidate_ids": ["unscored"]}},
        {
            "all_candidates": [
                {
                    "candidate_id": "retrieved-only",
                    "scheduled_trajectory": [{"s_m": 10, "arrival_t": 100}],
                }
            ]
        },
        {"all_candidates": [aggregate(score=None)]},
    ],
)
def test_missing_aggregate_scores_do_not_fabricate_ranked_candidates(payload):
    assert build_diagnostic_candidates(payload)["candidates"] == []


@pytest.mark.parametrize("score", [math.inf, -math.inf, math.nan, -0.1, True, "1.0"])
def test_invalid_aggregate_costs_do_not_enter_numeric_ranking(score):
    row = aggregate(score=score, schedule_aligned_point_ratio=0.03)
    assert build_diagnostic_candidates(result([row]))["candidates"] == []


@pytest.mark.parametrize("support", [None, -0.1, 1.1])
def test_finite_scored_candidate_with_invalid_support_exposes_failed_support_gate(
    support,
):
    row = aggregate(support_score=support)
    candidates = build_diagnostic_candidates(result([row]))["candidates"]
    assert len(candidates) == 1
    assert any(
        g["metric"] == "support_score" for g in candidates[0]["failed_gate_checks"]
    )


@pytest.mark.parametrize(
    "overrides, expected_metric",
    [
        ({"score": 3.1}, "score"),
        ({"coverage": 0.34}, "coverage"),
        (
            {"schedule_supported_window_weight_ratio": 0.34},
            "schedule_supported_window_weight_ratio",
        ),
        ({"whole_alignment": {"status": "NO_EVIDENCE"}}, "whole_alignment.status"),
        ({"schedule_aligned_point_ratio": 0.34}, "schedule_aligned_point_ratio"),
        ({"schedule_aligned_duration_ratio": 0.34}, "schedule_aligned_duration_ratio"),
    ],
)
def test_each_actual_admissibility_gate_has_an_explicit_failure_record(
    overrides, expected_metric
):
    candidate = build_diagnostic_candidates(result([aggregate(**overrides)]))[
        "candidates"
    ][0]
    failed = candidate["failed_gate_checks"]
    assert any(check["metric"] == expected_metric for check in failed)
    assert all(check["passed"] is False for check in failed)
    for check in failed:
        assert {
            "metric",
            "label",
            "actual",
            "threshold",
            "operator",
            "passed",
            "unit",
        } <= set(check)
        assert check in candidate["gate_checks"]


def test_diagnostic_annotations_are_uncalibrated_and_do_not_mutate_source():
    row = aggregate(
        probability=0.91,
        calibrated=True,
        schedule_aligned_point_ratio=0.03,
    )
    original = result([row])
    config = {"aggregation_min_coverage": 0.35}
    snapshot, saved_config = copy.deepcopy(original), copy.deepcopy(config)
    candidate = build_diagnostic_candidates(original, config)["candidates"][0]
    assert candidate["diagnostic_only"] is True
    assert candidate["probability"] is None
    assert candidate["calibrated"] is False
    assert candidate["score"] == row["score"]
    assert candidate["estimated_delay_s"] == row["estimated_delay_s"]
    assert original == snapshot
    assert config == saved_config
    candidate["trajectory"][0]["s"] = 9_999
    candidate["failed_gate_checks"][0]["actual"] = -1
    assert original == snapshot


def test_schedule_dwell_stop_order_and_reference_preserved_without_delay_shift():
    row = aggregate(schedule_aligned_point_ratio=0.03, estimated_delay_s=900)
    before = copy.deepcopy(row["scheduled_trajectory"])
    candidate = build_diagnostic_candidates(result([row]))["candidates"][0]
    trajectory = candidate["trajectory"]
    assert [(p["t"], p["s"], p["kind"]) for p in trajectory] == [
        (1_699_298_400.0, 100.0, "arrival"),
        (1_699_298_430.0, 100.0, "departure"),
        (1_699_298_700.0, 500.0, "arrival"),
        (1_699_298_760.0, 500.0, "departure"),
    ]
    assert [p["stop_sequence"] for p in trajectory] == [1, 1, 2, 2]
    assert [p["stop_id"] for p in trajectory] == [
        "stop-a",
        "stop-a",
        "stop-b",
        "stop-b",
    ]
    assert {p["reference_id"] for p in trajectory} == {"south-reference"}
    assert row["scheduled_trajectory"] == before
    assert candidate["reference_id"] == "south-reference"


def test_missing_saved_schedule_is_not_fabricated_from_delay_or_observations():
    row = aggregate(schedule_aligned_point_ratio=0.03)
    del row["scheduled_trajectory"]
    original = result([row])
    original["observations"] = [{"t": 100, "s_m": 20, "source": "avl"}]
    candidate = build_diagnostic_candidates(original)["candidates"][0]
    assert candidate["trajectory"] == []


def test_diagnostic_cap_remains_three_despite_many_available_failed_candidates():
    rows = [aggregate(f"failed-{n:03}", score=n + 4.0) for n in reversed(range(100))]
    payload = build_diagnostic_candidates(result(rows), {"aggregation_output_max": 100})
    assert [c["trip_id"] for c in payload["candidates"]] == [
        "failed-000",
        "failed-001",
        "failed-002",
    ]
    assert payload["summary"]["limit"] == payload["summary"]["shown_count"] == 3
