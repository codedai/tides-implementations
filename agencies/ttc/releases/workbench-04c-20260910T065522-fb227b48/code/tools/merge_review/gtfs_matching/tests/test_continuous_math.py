"""Route-independent continuous-score fixtures: no production identity rules."""

import copy
import math

import pytest

from tools.merge_review.gtfs_matching.continuous.delay import fit_delay
from tools.merge_review.gtfs_matching.continuous.evidence import (
    coverage_metrics,
    temporal_weights,
)
from tools.merge_review.gtfs_matching.continuous.schedule import (
    build_schedule,
    passage_at,
    residual_at,
)


def trip(offset=0):
    return {
        "trip_id": "fixture-occurrence",
        "scheduled": [
            {"stop_id": "A", "s_m": 0, "arrival_t": offset, "departure_t": offset + 30},
            {
                "stop_id": "B",
                "s_m": 1000,
                "arrival_t": offset + 330,
                "departure_t": offset + 360,
            },
            {
                "stop_id": "A",
                "s_m": 2000,
                "arrival_t": offset + 660,
                "departure_t": offset + 720,
            },
        ],
    }


def observations(step=15, delay=60, offset=0):
    return [
        {
            "source_id": f"avl:{index}",
            "source": "avl",
            "t": offset + time,
            "s_m": time * 5,
            "fragment_id": "service:0",
            "reference_id": "reference",
            "projection_status": "ON_REFERENCE",
            "quality": 1.0,
            "lat": 43.0,
            "lon": -79.0,
            "headway_s": 600,
            "uncertainty_s": 30.0,
            "residual_lower_s": float(delay),
            "residual_upper_s": float(delay),
        }
        for index, time in enumerate(range(0, 601, step))
    ]


def test_direct_movement_and_scheduled_dwell_intervals():
    schedule = build_schedule(trip())
    assert schedule["status"] == "READY"
    assert len(schedule["movements"]) == 2
    assert passage_at(schedule, 500)["intervals"] == [(180.0, 180.0)]
    assert passage_at(schedule, 1000)["intervals"] == [(330.0, 360.0)]
    residual = residual_at(schedule, 1000, 350)
    assert residual["residual_lower_s"] == -10
    assert residual["residual_upper_s"] == 20
    assert passage_at(schedule, -10)["status"] == "OUTSIDE_PATTERN"
    assert passage_at(schedule, 2100)["status"] == "OUTSIDE_PATTERN"


def test_no_required_reference_grid_and_collinear_geometry_invariance():
    original = trip()
    original["shape"] = [[0, 0], [2, 2]]
    modified = copy.deepcopy(original)
    modified["shape"] = [[0, 0], [0.5, 0.5], [1, 1], [1.5, 1.5], [2, 2]]
    assert build_schedule(original) == build_schedule(modified)
    answers = [
        passage_at(build_schedule(original, {"score_reference_step_s": step}), 456)
        for step in (5, 10, 15, 20)
    ]
    assert all(answer == answers[0] for answer in answers)


def test_timepoint_default_and_interpolated_uncertainty():
    schedule = build_schedule(trip())
    assert schedule["stops"][0]["timepoint"] is True
    assert (
        passage_at(schedule, 500)["uncertainty_s"]
        > passage_at(schedule, 0)["uncertainty_s"]
    )
    changed = trip()
    changed["scheduled"][1]["timepoint"] = 0
    uncertain = build_schedule(changed)
    assert (
        passage_at(uncertain, 1000)["uncertainty_s"]
        > passage_at(schedule, 1000)["uncertainty_s"]
    )
    assert (
        passage_at(uncertain, 500)["uncertainty_s"]
        > passage_at(schedule, 500)["uncertainty_s"]
    )


def test_repeated_stop_ids_on_loop_are_distinct_canonical_visits():
    schedule = build_schedule(trip())
    assert passage_at(schedule, 0)["stop_ids"] == ["A"]
    assert passage_at(schedule, 2000)["stop_ids"] == ["A"]
    assert (
        passage_at(schedule, 0)["intervals"] != passage_at(schedule, 2000)["intervals"]
    )


def test_consecutive_equal_progress_stops_are_continuous_stationary_interval():
    value = {
        "scheduled": [
            {"s_m": 0, "arrival_t": 0, "departure_t": 10},
            {"s_m": 0, "arrival_t": 30, "departure_t": 40},
            {"s_m": 100, "arrival_t": 60, "departure_t": 60},
        ]
    }
    schedule = build_schedule(value)
    assert schedule["status"] == "READY"
    assert passage_at(schedule, 0)["intervals"] == [(0, 40)]
    assert passage_at(schedule, 50)["intervals"] == [(50, 50)]


def test_nearby_distinct_progress_visits_preserve_ambiguity():
    value = {
        "scheduled": [
            {"s_m": 0, "arrival_t": 0, "departure_t": 10},
            {"s_m": 0.4, "arrival_t": 30, "departure_t": 40},
            {"s_m": 100, "arrival_t": 60, "departure_t": 60},
        ]
    }
    result = residual_at(build_schedule(value), 0.2, 20)
    assert result["status"] == "AMBIGUOUS_PASSAGE"
    assert len(result["residual_intervals"]) == 2
    assert "residual_lower_s" not in result  # Must not bridge an unsupported time.


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ({"s_m": -100}, "NON_MONOTONE_CANONICAL_PROGRESS"),
        ({"arrival_t": -1}, "NON_MONOTONE_SCHEDULE_TIME"),
        ({"departure_t": 1}, "DEPARTURE_PRECEDES_ARRIVAL"),
        ({"s_m": float("nan")}, "MISSING_OR_NONFINITE_SCHEDULE_COORDINATE"),
    ],
)
def test_bad_schedule_is_diagnostic_not_reordered(mutation, reason):
    value = trip()
    value["scheduled"][1].update(mutation)
    schedule = build_schedule(value)
    assert schedule["status"] == "INVALID_SCHEDULE"
    assert reason in schedule["reasons"]
    assert passage_at(schedule, 400)["status"] == "INVALID_SCHEDULE"


def test_after_midnight_and_clock_origin_invariance():
    reference = passage_at(build_schedule(trip()), 500)["intervals"][0][0]
    for origin in (25 * 3600, 1_699_241_400.0, -86400):
        assert (
            passage_at(build_schedule(trip(origin)), 500)["intervals"][0][0] - origin
            == reference
        )


@pytest.mark.parametrize("delay", [0, 120, -120, 1800, -900])
def test_constant_delay_preserved_with_meaningful_headway_prior(delay):
    result = fit_delay(observations(delay=delay))
    assert result["status"] == "FITTED"
    assert result["delay_median_s"] == pytest.approx(
        delay, abs=max(2, abs(delay) * 0.015)
    )
    assert result["components"]["delay_dynamics_cost"] == pytest.approx(0, abs=1e-8)
    if delay:
        assert result["components"]["absolute_delay_level_cost"] > 0
    assert result["prior_source"] == "local_scheduled_headway"
    assert result["uncertainty_calibrated"] is False


def test_adjacent_identical_patterns_not_free_after_headway_shift():
    near = fit_delay(observations(delay=15))
    previous = fit_delay(observations(delay=615))
    assert near["components"]["conditioned_fit_cost"] < 0.01
    assert previous["components"]["conditioned_fit_cost"] < 0.01
    assert previous["objective"] > near["objective"] + 0.5
    high_frequency = observations(delay=615)
    for row in high_frequency:
        row["headway_s"] = 180
    assert (
        fit_delay(high_frequency)["components"]["absolute_delay_level_cost"]
        > previous["components"]["absolute_delay_level_cost"]
    )


def test_delay_fitting_does_not_read_upstream_schedule_assignments():
    rows = observations()
    polluted = [
        dict(row, AvgOTP=99999, matched_trip_id="preexisting", raw_AvgOTP=-3600)
        for row in rows
    ]
    original, changed = fit_delay(rows), fit_delay(polluted)
    assert original["components"] == changed["components"]
    assert original["delay_median_s"] == changed["delay_median_s"]


def test_delay_uses_dwell_interval_not_midpoint():
    rows = observations()
    for row in rows:
        row.update(residual_lower_s=-15, residual_upper_s=120)
    result = fit_delay(rows)
    assert result["delay_median_s"] == 0
    assert result["objective"] == 0


def test_avl_density_and_temporary_grid_do_not_materially_change_score():
    outputs = []
    for step in (5, 10, 15, 20, 30):
        rows = observations(step=step)
        for row in rows:
            residual = 60 + 0.08 * row["t"]
            row.update(residual_lower_s=residual, residual_upper_s=residual)
        outputs.append(fit_delay(rows, {"score_reference_step_s": step}))
    assert (
        max(row["objective"] for row in outputs)
        - min(row["objective"] for row in outputs)
        < 0.015
    )
    assert (
        max(row["delay_median_s"] for row in outputs)
        - min(row["delay_median_s"] for row in outputs)
        < 3
    )
    assert outputs[0]["rows"][0]["delay_s"] < outputs[0]["rows"][-1]["delay_s"]


def test_equal_time_duplicates_share_temporal_support():
    rows = observations()
    doubled = [
        dict(row, source_id=f"{row['source_id']}:{replica}")
        for row in rows
        for replica in range(2)
    ]
    assert sum(temporal_weights(rows).values()) == 600
    assert sum(temporal_weights(doubled).values()) == 600
    assert fit_delay(rows)["objective"] == pytest.approx(
        fit_delay(doubled)["objective"], abs=1e-5
    )


def test_stationary_delay_dynamics_use_seconds_not_a_per_sample_slack():
    results = []
    for step in (5, 15, 30, 60):
        rows = observations(step=step)
        for row in rows:
            row.update(
                s_m=0, residual_lower_s=row["t"] * 0.7, residual_upper_s=row["t"] * 0.7
            )
        results.append(fit_delay(rows))
    assert (
        max(row["objective"] for row in results)
        - min(row["objective"] for row in results)
        < 0.04
    )
    assert (
        max(row["delay_median_s"] for row in results)
        - min(row["delay_median_s"] for row in results)
        < 0.1
    )


def test_fast_recovery_of_delay_does_not_reverse_corrected_time():
    rows = observations()
    for row in rows:
        residual = 1200 - row["t"] * 2
        row.update(residual_lower_s=residual, residual_upper_s=residual)
    result = fit_delay(rows, {"continuous_delay_smooth_weight": 0.01})
    assert result["components"]["conditioned_fit_cost"] < 0.01
    assert result["components"]["delay_dynamics_cost"] > 1
    assert any(
        second["delay_s"] - first["delay_s"] < -(second["t"] - first["t"])
        for first, second in zip(result["rows"], result["rows"][1:], strict=False)
    )
    assert all(
        second["corrected_t"] >= first["corrected_t"]
        for first, second in zip(result["rows"], result["rows"][1:], strict=False)
    )


def test_sustained_fragment_separator_does_not_generate_delay_transition():
    rows = observations()
    for row in rows:
        if row["t"] >= 300:
            row.update(
                fragment_id="service:1", residual_lower_s=600, residual_upper_s=600
            )
    result = fit_delay(rows)
    assert result["components"]["delay_dynamics_cost"] < 1e-8
    assert result["rows"][19]["delay_s"] < 100
    assert result["rows"][20]["delay_s"] > 590


def test_corrected_time_order_and_physical_delay_slew():
    rows = observations()
    for row in rows:
        residual = 0 if row["t"] < 300 else 1800
        row.update(residual_lower_s=residual, residual_upper_s=residual)
    result = fit_delay(rows)
    for first, second in zip(result["rows"], result["rows"][1:], strict=False):
        assert second["corrected_t"] >= first["corrected_t"]
        assert (
            abs(second["delay_s"] - first["delay_s"])
            <= 0.95 * (second["t"] - first["t"]) + 1e-8
        )
    assert result["components"]["conditioned_fit_cost"] > 1


def test_historical_delay_prior_requires_independent_provenance():
    with pytest.raises(ValueError, match="independent validation"):
        fit_delay(
            observations(),
            {"continuous_delay_historical_prior": {"median_s": 60, "scale_s": 90}},
        )
    historical = {
        "median_s": 1800,
        "scale_s": 120,
        "independently_validated": True,
        "validation_artifact": "held-out-route-date-delay-audit",
    }
    result = fit_delay(
        observations(delay=1800), {"continuous_delay_historical_prior": historical}
    )
    assert result["prior_source"] == "independently_validated_history"
    assert result["objective"] < 1e-7


def test_three_coverage_denominators_and_localized_failure():
    rows = observations(step=20)
    rows[10].update(projection_status="OFF_REFERENCE", s_m=None)
    valid = {
        row["source_id"] for row in rows if row["projection_status"] == "ON_REFERENCE"
    }
    supported = valid - {rows[20]["source_id"]}
    result = coverage_metrics(rows, supported)
    assert result["projection"]["point_ratio"] == pytest.approx(30 / 31)
    assert result["schedule"]["point_ratio"] == pytest.approx(29 / 30)
    assert result["end_to_end"]["point_ratio"] == pytest.approx(29 / 31)
    assert result["projection"]["duration_s"] == 560
    assert result["schedule"]["duration_s"] == 520
    assert result["schedule"]["duration_denominator_s"] == 560
    assert result["end_to_end"]["duration_denominator_s"] == 600
    assert result["end_to_end"]["duration_ratio"] == pytest.approx(
        result["projection"]["duration_ratio"] * result["schedule"]["duration_ratio"]
    )


def test_gap_and_off_reference_run_never_reconnect_edges():
    rows = observations(step=20)
    for row in rows:
        if 200 <= row["t"] < 400:
            row.update(projection_status="OFF_REFERENCE", s_m=None)
        elif row["t"] >= 400:
            row["fragment_id"] = "service:1"
    supported = {
        row["source_id"] for row in rows if row["projection_status"] == "ON_REFERENCE"
    }
    result = coverage_metrics(rows, supported)
    assert result["schedule"]["point_ratio"] == 1
    assert result["end_to_end"]["duration_s"] == 380
    assert result["longest_valid_contiguous_run_s"] == 200
    sparse = [rows[0], rows[-1]]
    sparse_result = coverage_metrics(sparse, {row["source_id"] for row in sparse})
    assert sparse_result["projection"]["duration_s"] == 0
    assert sparse_result["projection"]["duration_denominator_s"] == 600


def test_apc_without_real_coordinates_is_never_a_spatial_point():
    rows = observations()
    apc = {
        "source": "apc",
        "source_id": "apc:1",
        "t": 200,
        "s_m": 1000,
        "projection_status": "ON_REFERENCE",
        "residual_lower_s": 0,
        "residual_upper_s": 30,
    }
    coverage = coverage_metrics(rows + [apc], {row["source_id"] for row in rows})
    assert coverage["all_spatial_point_count"] == len(rows)
    assert len(fit_delay(rows + [apc])["rows"]) == len(rows)


def test_no_evidence_bad_values_and_duplicate_identity():
    assert fit_delay([])["status"] == "NO_EVIDENCE"
    assert coverage_metrics([], [])["end_to_end"]["point_ratio"] == 0
    assert fit_delay([dict(observations()[0], t=math.nan)])["status"] == "NO_EVIDENCE"
    with pytest.raises(ValueError, match="Duplicate"):
        temporal_weights([observations()[0], observations()[0]])
    with pytest.raises(ValueError, match="below one"):
        fit_delay(observations(), {"continuous_delay_max_rate_s_per_s": 1})
