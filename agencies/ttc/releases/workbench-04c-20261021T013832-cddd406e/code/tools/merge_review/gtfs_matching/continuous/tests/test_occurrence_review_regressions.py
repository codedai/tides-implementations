"""Independent regressions from occurrence-model review; synthetic fixtures only."""

import copy
from pathlib import Path

import pytest

from tools.merge_review.gtfs_matching.continuous.config import get_config
from tools.merge_review.gtfs_matching.continuous.retrieval import schedule_pool
from tools.merge_review.gtfs_matching.continuous.schedule import (
    build_schedule,
    passage_at,
)
from tools.merge_review.gtfs_matching.continuous.tests.test_continuous_pipeline import (
    apc,
    points,
    run,
    schedule,
)

ENABLED = {"continuous_occurrence_model_enabled": True}


def middle_observations():
    return [row for row in points() if 600 <= row["s_m"] <= 1800]


def occurrence_signature(result):
    return {
        row["trip_id"]: {
            "crossed": row["occurrence_displacement"]["crossed_occurrences"],
            "cost": row["occurrence_displacement"]["displacement_cost"],
            "members": row["occurrence_displacement"]["context"]["member_ids"],
        }
        for row in result["all_candidates"]
    }


@pytest.mark.parametrize(
    "event",
    [
        apc(stop=5),  # APC service coverage legitimately extends past AVL coverage.
        apc(stop=2) | {"interval_start": 2000, "interval_end": 2030},
        apc(stop=2) | {"interval_start": -2000, "interval_end": -1970},
    ],
)
def test_nonoverlapping_apc_does_not_redefine_whole_trip_confusability(event):
    observations = middle_observations()
    baseline = run(rows=observations, config=ENABLED)
    source_event = copy.deepcopy(event)
    compared = run(rows=observations, apc_rows=[event], config=ENABLED)
    assert occurrence_signature(compared) == occurrence_signature(baseline)
    assert event == source_event
    # Stop evidence remains diagnostic; it was not discarded from raw/APC data.
    assert all(row["apc_support"]["rows"] for row in compared["all_candidates"])


@pytest.mark.parametrize("confidence", [0.0, 0.01, 0.49])
def test_low_confidence_apc_cannot_hard_exclude_a_local_competitor(confidence):
    observations = middle_observations()
    baseline = run(rows=observations, config=ENABLED)
    event = apc(stop=2) | {
        "stop_id": "uncertain-unserved-stop",
        "confidence": confidence,
    }
    compared = run(rows=observations, apc_rows=[event], config=ENABLED)
    assert occurrence_signature(compared) == occurrence_signature(baseline)
    for window in compared["retrieval"]["windows"]:
        assert all(
            candidate["observed_stop_ids"] == [] for candidate in window["candidates"]
        )


def test_apc_confidence_threshold_and_strong_stop_contradiction_are_explicit():
    event = apc(stop=2) | {"stop_id": "unserved-stop", "confidence": 0.6}
    rejected = run(rows=middle_observations(), apc_rows=[event], config=ENABLED)
    assert all(
        candidate["occurrence_displacement"]["crossed_occurrences"] is None
        and candidate["occurrence_decision_guard"]["allowed"] is False
        for candidate in rejected["all_candidates"]
    )
    relaxed = run(
        rows=middle_observations(),
        apc_rows=[event],
        config=ENABLED | {"continuous_occurrence_apc_min_confidence": 0.8},
    )
    baseline = run(rows=middle_observations(), config=ENABLED)
    assert occurrence_signature(relaxed) == occurrence_signature(baseline)


def test_movement_with_reliable_endpoints_keeps_continuous_passage_uncertainty():
    trip = schedule()
    built = build_schedule(trip)
    result = run(trips=[trip], config=ENABLED)
    process = result["all_candidates"][0]["whole_alignment"]["delay_process"]
    movements = [row for row in process if row["timepoint_kind"] == "movement"]
    assert movements
    for row in movements:
        expected = passage_at(built, row["s_m"])["uncertainty_s"]
        assert row["schedule_timepoint_reliable"] is True
        assert row["schedule_uncertainty_s"] == pytest.approx(expected)
        assert row["schedule_uncertainty_s"] < 90
        assert row["uncertainty_components"]["schedule_s"] == pytest.approx(expected)


def test_movement_with_explicit_unreliable_endpoint_retains_uncertainty_floor():
    trip = schedule()
    trip["scheduled"][1]["timepoint"] = 0
    result = run(trips=[trip], config=ENABLED)
    process = result["all_candidates"][0]["whole_alignment"]["delay_process"]
    first_segment = [row for row in process if row.get("schedule_segment_id") == 0]
    assert first_segment
    assert all(row["schedule_timepoint_reliable"] is False for row in first_segment)
    assert all(row["schedule_uncertainty_s"] >= 90 for row in first_segment)


def test_previous_next_headways_are_between_scheduled_passages_not_observed_time():
    trips = [schedule("before", -300), schedule("query", 0), schedule("after", 700)]
    pool = schedule_pool(trips, get_config(ENABLED))
    contexts = [pool.confusability(trips[1], (600, 1800), 1200, t) for t in (250, 440)]
    for context in contexts:
        assert context["previous_headway_s"] == 300
        assert context["next_headway_s"] == 700
    assert contexts[0]["previous_headway_s"] != 250 - (-60)


def test_all_simultaneous_ties_and_adjacent_groups_survive_top_k_one():
    trips = [schedule(f"simultaneous-{index}", 0) for index in range(6)]
    trips += [schedule("before", -600), schedule("after", 600)]
    result = run(
        rows=middle_observations(),
        trips=trips,
        config=ENABLED
        | {"continuous_retrieval_top_k": 1, "continuous_retrieval_delta": 0},
    )
    assert {candidate["trip_id"] for candidate in result["all_candidates"]} == {
        trip["trip_id"] for trip in trips
    }
    for window in result["retrieval"]["windows"]:
        if window["status"] == "EVIDENCE":
            assert all(
                candidate["protected_from_top_k"] for candidate in window["candidates"]
            )


def test_ui_prior_mapping_explicitly_recognizes_new_sources():
    static = Path(__file__).resolve().parents[3] / "static"
    app = (static / "app.js").read_text()
    for source, label in (
        (
            "uncalibrated_operational_defaults",
            "Asymmetric operational delay mixture prior (uncalibrated)",
        ),
        (
            "independently_validated_operational_hierarchy",
            "Independently validated hierarchical operational delay prior",
        ),
    ):
        assert f'{source}: "{label}"' in app
