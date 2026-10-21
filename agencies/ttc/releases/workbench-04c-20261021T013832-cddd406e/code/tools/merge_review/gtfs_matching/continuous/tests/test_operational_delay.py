"""General physical-unit regression tests; no historical winner or route IDs."""

from __future__ import annotations

import copy
import math

import pytest

from tools.merge_review.gtfs_matching.continuous.delay import (
    _fit_legacy_delay,
    fit_delay,
)
from tools.merge_review.gtfs_matching.continuous.operational_prior import (
    prior_terms,
    rescue_delay_bounds,
    resolve_prior,
)

ENABLED = {"continuous_occurrence_model_enabled": True}


def observations(step=10, *, delay=300.0, noise=0.0, headway=600.0, end=600):
    rows = []
    for index, timestamp in enumerate(range(0, end + 1, step)):
        residual = delay(timestamp) if callable(delay) else delay
        residual += noise * (-1 if index % 2 == 0 else 1)
        rows.append(
            {
                "source_id": f"observation:{timestamp}",
                "source": "avl",
                "t": float(timestamp),
                "s_m": timestamp * 8.0,
                "residual_lower_s": residual,
                "residual_upper_s": residual,
                "uncertainty_s": 30.0,
                "headway_s": headway,
                "fragment_id": "service-fragment",
                "reference_id": "canonical-pattern",
                "projection_distance_m": 0.0,
                "quality": 1.0,
                "timepoint_kind": "movement",
            }
        )
    return rows


def artifact():
    params = {
        key: value
        for key, value in resolve_prior().items()
        if key.endswith("scale_s") or key in {"centre_s", "disruption_probability"}
    }
    contexts = [
        {},
        {"feed_version": "feed-A"},
        {"feed_version": "feed-A", "route_id": "route-A"},
        {"feed_version": "feed-A", "route_id": "route-A", "direction": "outbound"},
        {
            "feed_version": "feed-A",
            "route_id": "route-A",
            "direction": "outbound",
            "observed_time_bucket": 25,
        },
    ]
    return {
        "schema_version": "operational-delay-prior-v1",
        "independently_validated": True,
        "validation_artifact": "independent-heldout-metrics.json",
        "training_provenance": {
            "labels_sha256": "ab" * 32,
            "independent_verified": True,
            "split_policy": "route_id AND service_date AND episode_id disjoint",
            "split_counts": {name: 100 for name in ("train", "validation", "test")},
            "split_groups": {
                name: {
                    field: [f"{field}:{name}"]
                    for field in ("route_id", "service_date", "episode_id")
                }
                for name in ("train", "validation", "test")
            },
        },
        "records": [
            dict(
                params,
                context=context,
                sample_count=100,
                centre_s=10.0 * depth,
                record_id=f"level:{depth}",
            )
            for depth, context in enumerate(contexts)
        ],
    }


def test_legacy_flag_off_is_exact_and_does_not_read_new_context():
    rows = observations(noise=15)
    original = _fit_legacy_delay(rows)
    assert fit_delay(rows) == original
    assert (
        fit_delay(
            rows,
            {"continuous_occurrence_model_enabled": False},
            operational_context="not-an-object",
        )
        == original
    )


def test_candidate_own_headway_cannot_rescale_delay_or_change_fit():
    context = {
        "feed_version": "shared-feed",
        "route_id": "shared-route",
        "direction": "outbound",
        "observed_time_bucket": 8,
        "local_headway_s": 420,
    }
    short = fit_delay(
        observations(headway=600, delay=900), ENABLED, operational_context=context
    )
    long = fit_delay(
        observations(headway=3600, delay=900), ENABLED, operational_context=context
    )
    assert short["components"] == long["components"]
    assert short["objective"] == long["objective"]
    assert short["delay_median_s"] == long["delay_median_s"]
    assert short["dynamics_diagnostics"] == long["dynamics_diagnostics"]
    assert short["prior_provenance"] == long["prior_provenance"]
    assert short["prior_provenance"]["candidate_headway_used"] is False
    assert [row["delay_s"] for row in short["rows"]] == [
        row["delay_s"] for row in long["rows"]
    ]


def test_asymmetric_early_late_and_unbounded_nonzero_large_delay_penalty():
    prior = resolve_prior()
    assert prior_terms(0, prior)["cost"] == 0
    assert prior_terms(-600, prior)["cost"] > prior_terms(600, prior)["cost"] > 0
    costs = [
        prior_terms(value, prior)["cost"] for value in (600, 1800, 3600, 7200, 20000)
    ]
    assert all(math.isfinite(value) for value in costs)
    assert costs == sorted(costs)
    assert len(set(costs)) == len(costs)
    assert (
        prior_terms(7200, prior)["disruption_responsibility"]
        > prior_terms(600, prior)["disruption_responsibility"]
    )


@pytest.mark.parametrize("delay", [-7200.0, -600.0, 300.0, 3600.0, 10000.0])
def test_mixture_irls_gradient_is_gradient_of_reported_prior_objective(delay):
    prior = resolve_prior()
    epsilon = 0.01
    gradient = (
        prior_terms(delay + epsilon, prior)["cost"]
        - prior_terms(delay - epsilon, prior)["cost"]
    ) / (2 * epsilon)
    assert prior_terms(delay, prior)["gradient"] == pytest.approx(
        gradient, rel=1e-6, abs=1e-10
    )


def test_large_real_delay_remains_fittable_but_is_not_free():
    result = fit_delay(observations(delay=3600), ENABLED)
    assert result["status"] == "FITTED"
    assert result["delay_median_s"] == pytest.approx(3600, abs=5)
    assert result["components"]["conditioned_fit_cost"] < 0.01
    assert result["components"]["absolute_delay_level_cost"] > 3
    assert result["uncertainty_calibrated"] is False
    assert result["inferred_delay_regime"] == "DISRUPTION_LIKE"
    assert result["operational_disruption_responsibility"] > 0.5
    assert result["delay_regime_is_ground_truth"] is False


def test_prior_is_in_solver_not_only_added_to_output():
    rows = observations(delay=200)
    for row in rows:
        row["uncertainty_s"] = 120.0
    unregularized = fit_delay(rows, ENABLED | {"continuous_delay_prior_weight": 0})
    regularized = fit_delay(rows, ENABLED | {"continuous_delay_prior_weight": 10})
    assert unregularized["delay_median_s"] == pytest.approx(200, abs=0.01)
    assert regularized["delay_median_s"] < unregularized["delay_median_s"] - 50
    parts = regularized["components"]
    assert regularized["objective"] == pytest.approx(
        parts["conditioned_fit_cost"]
        + 10 * parts["absolute_delay_level_cost"]
        + 0.3 * parts["delay_dynamics_cost"]
    )


@pytest.mark.parametrize("step", [1, 5, 10, 20, 30])
def test_ping_density_and_short_scale_noise_do_not_dominate_dynamics(step):
    # Alternating index noise has different physical periods at each density;
    # this is a robustness test, not an identical-signal quadrature equality.
    # At 20s pings its 40s cycle aliases weakly onto 60s knots (under 0.5s fitted
    # delay change), so material physical tolerances are used deliberately.
    baseline = fit_delay(observations(step=10, noise=10), ENABLED)
    result = fit_delay(observations(step=step, noise=10), ENABLED)
    assert result["components"] == pytest.approx(baseline["components"], abs=0.002)
    assert result["delay_median_s"] == pytest.approx(
        baseline["delay_median_s"], abs=2.0
    )
    assert result["components"]["delay_dynamics_cost"] < 0.002
    diagnostic = result["dynamics_diagnostics"]
    assert diagnostic["per_observation_derivatives_used"] is False
    assert diagnostic["state_count"] == 11
    assert diagnostic["interval_count"] == 10
    assert diagnostic["linked_duration_s"] == 600
    assert sum(row["normalized_weight"] for row in result["rows"]) == pytest.approx(1)
    assert len(result["rows"]) == len(observations(step=step))


def test_sampling_change_preserves_shared_smooth_delay_trend():
    first = fit_delay(observations(step=5, delay=lambda t: 120 + t * 0.15), ENABLED)
    second = fit_delay(observations(step=30, delay=lambda t: 120 + t * 0.15), ENABLED)
    assert first["components"] == pytest.approx(second["components"], abs=0.01)
    first_by_t = {row["t"]: row["delay_s"] for row in first["rows"]}
    assert max(abs(row["delay_s"] - first_by_t[row["t"]]) for row in second["rows"]) < 1


def test_long_gps_gap_is_separator_not_a_delay_derivative():
    rows = observations(end=60, delay=0)
    tail = observations(end=60, delay=1800)
    for row in tail:
        row["t"] += 660
        row["s_m"] += 2000
        row["source_id"] += ":tail"
    result = fit_delay(rows + tail, ENABLED)
    diagnostics = result["dynamics_diagnostics"]
    assert diagnostics["fragment_count"] == 2
    assert diagnostics["linked_duration_s"] == 120
    assert diagnostics["separators"][0]["reason"] == "GPS_GAP"
    assert all(
        not (row["start_t"] < 660 and row["end_t"] > 60)
        for row in diagnostics["intervals"]
    )
    assert result["components"]["delay_dynamics_cost"] < 0.01
    assert result["rows"][-1]["delay_s"] > 1790
    assert result["rows"][len(rows)]["uncertainty_components"]["gap_s"] > 0


@pytest.mark.parametrize(
    "separator", ["scope_separator_before", "fragment_id", "reference_id"]
)
def test_explicit_off_reference_fragment_and_scope_separators_not_bridged(separator):
    rows = observations(end=120)
    split = len(rows) // 2
    for index, row in enumerate(rows):
        if index >= split:
            row["residual_lower_s"] = row["residual_upper_s"] = 1800
            if separator != "scope_separator_before":
                row[separator] = "second-fragment"
    if separator == "scope_separator_before":
        rows[split][separator] = True
    result = fit_delay(rows, ENABLED)
    diagnostics = result["dynamics_diagnostics"]
    assert diagnostics["fragment_count"] == 2
    assert len(diagnostics["separators"]) == 1
    assert "DELAY_DYNAMICS_NOT_BRIDGED_ACROSS_SEPARATORS" in result["diagnostics"]
    assert (
        result["rows"][split]["delay_s"] - result["rows"][split - 1]["delay_s"] > 1400
    )


def test_combined_uncertainty_and_stop_segment_events_are_reported():
    rows = observations(end=180)
    for row in rows:
        row["projection_distance_m"] = 80.0
        row["schedule_timepoint_reliable"] = False
        row["schedule_segment_id"] = "first" if row["t"] < 90 else "second"
    rows[9].update(stop_id="terminal-stop", terminal=True, timepoint_kind="dwell")
    result = fit_delay(rows, ENABLED, operational_context={"clock_uncertainty_s": 20})
    terminal = result["rows"][9]
    assert terminal["uncertainty_s"] > 100
    assert terminal["schedule_uncertainty_s"] == 90
    assert terminal["uncertainty_components"] == {
        "schedule_s": 90,
        "projection_s": 40,
        "clock_s": 20,
        "stop_s": 10,
        "terminal_s": 30,
        "gap_s": 0,
    }
    assert any(
        "STOP_SEGMENT_OR_TERMINAL_EVENT" in row["reasons"]
        for row in result["dynamics_diagnostics"]["states"]
    )


def test_corrected_t_monotone_under_physically_incompatible_residual_jumps():
    rows = observations(step=1, end=180, delay=lambda t: 0 if t < 90 else 1800)
    result = fit_delay(rows, ENABLED)
    assert "PHYSICAL_DELAY_RATE_CONSTRAINT_ACTIVE" in result["diagnostics"]
    assert all(
        b["corrected_t"] > a["corrected_t"]
        for a, b in zip(result["rows"], result["rows"][1:], strict=False)
    )
    assert result["components"]["conditioned_fit_cost"] > 1


def test_dwell_interval_is_not_converted_to_an_arbitrary_timestamp():
    rows = observations(end=120, delay=0)
    for row in rows:
        row.update(residual_lower_s=-20, residual_upper_s=50, timepoint_kind="dwell")
    result = fit_delay(rows, ENABLED)
    assert result["delay_median_s"] == pytest.approx(0, abs=1e-6)
    assert result["components"]["conditioned_fit_cost"] == 0
    assert result["components"]["absolute_delay_level_cost"] == 0


def test_apc_and_invalid_rows_never_become_delay_trajectory_points():
    rows = observations(end=60)
    rows += [
        dict(rows[0], source_id="apc-event", source="apc"),
        dict(rows[0], source_id="bad", t=None),
    ]
    result = fit_delay(rows, ENABLED)
    assert result["fit_count"] == 7
    assert {row["reason"] for row in result["excluded"]} == {
        "APC_IS_INTERVAL_EVIDENCE_NOT_AN_AVL_TRAJECTORY_POINT",
        "INVALID_RESIDUAL_INTERVAL",
    }
    assert fit_delay([], ENABLED)["status"] == "NO_EVIDENCE"


def test_hierarchy_selects_observed_after_midnight_hour_and_keeps_provenance():
    history = artifact()
    config = {"continuous_operational_prior_artifact": history}
    context = {
        "feed_version": "feed-A",
        "route_id": "route-A",
        "direction": "outbound",
        "observed_time_bucket": 25,
    }
    selected = resolve_prior(config, context)
    assert selected["centre_s"] == 40
    assert selected["selected_context"] == context
    assert selected["calibrated"] is True
    assert selected["validation_artifact"] == history["validation_artifact"]
    assert len(selected["artifact_sha256"]) == 64
    assert (
        resolve_prior(config, context | {"observed_time_bucket": 10})["centre_s"] == 30
    )
    assert (
        resolve_prior(config, context | {"feed_version": "other-feed"})["centre_s"] == 0
    )
    assert (
        resolve_prior(
            config, context | {"scheduled_hour": 10, "candidate_headway_s": 3600}
        )
        == selected
    )


def test_historical_prior_actually_moves_the_shared_fitted_process():
    history = artifact()
    history["records"][0]["centre_s"] = 100.0
    rows = observations(delay=150)
    for row in rows:
        row["uncertainty_s"] = 120.0
    defaults = fit_delay(rows, ENABLED | {"continuous_delay_prior_weight": 10})
    fitted = fit_delay(
        rows,
        ENABLED
        | {
            "continuous_delay_prior_weight": 10,
            "continuous_operational_prior_artifact": history,
        },
    )
    assert fitted["delay_median_s"] > defaults["delay_median_s"] + 40
    assert fitted["prior_provenance"]["calibrated"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        "unverified",
        "missing_provenance",
        "leakage",
        "duplicate",
        "candidate_context",
        "invalid_scale",
        "empty_count",
    ],
)
def test_invalid_or_leaky_history_fails_closed(mutation):
    history = artifact()
    if mutation == "unverified":
        history["independently_validated"] = False
    elif mutation == "missing_provenance":
        history.pop("training_provenance")
    elif mutation == "leakage":
        history["training_provenance"]["split_groups"]["test"]["route_id"] = [
            "route_id:train"
        ]
    elif mutation == "duplicate":
        history["records"].append(copy.deepcopy(history["records"][0]))
    elif mutation == "candidate_context":
        history["records"][0]["context"] = {"trip_id": "forbidden-candidate-identity"}
    elif mutation == "invalid_scale":
        history["records"][0]["routine_early_scale_s"] = 0
    else:
        history["records"][0]["sample_count"] = 0
    with pytest.raises(ValueError):
        resolve_prior({"continuous_operational_prior_artifact": history})


def test_rescue_bounds_are_asymmetric_finite_and_honest_about_defaults():
    prior = resolve_prior()
    bounds = rescue_delay_bounds(prior)
    assert bounds["lower_s"] < 0 < bounds["upper_s"]
    assert bounds["upper_s"] > abs(bounds["lower_s"])
    assert bounds["calibrated"] is False
    assert bounds["source"] == "uncalibrated_operational_defaults"
    history = resolve_prior({"continuous_operational_prior_artifact": artifact()})
    assert rescue_delay_bounds(history)["calibrated"] is True


def test_duplicate_observation_identity_is_not_counted_twice():
    rows = observations(end=60)
    with pytest.raises(ValueError, match="Duplicate source identity"):
        fit_delay(rows + [rows[0]], ENABLED)


def test_isolated_noisy_ping_does_not_determine_episode_delay():
    rows = observations()
    clean = fit_delay(rows, ENABLED)
    rows[len(rows) // 2]["residual_lower_s"] = 600
    rows[len(rows) // 2]["residual_upper_s"] = 600
    noisy = fit_delay(rows, ENABLED)
    assert noisy["delay_median_s"] == pytest.approx(clean["delay_median_s"], abs=2)
    assert noisy["components"]["delay_dynamics_cost"] < 0.01
    # The noisy ping and its genuine fit residual remain visible for coverage.
    assert len(noisy["rows"]) == len(rows)
    assert abs(noisy["rows"][len(rows) // 2]["conditioned_residual_s"]) > 250


def test_atomic_duration_changes_state_granularity_in_physical_seconds():
    result = fit_delay(
        observations(), ENABLED | {"continuous_delay_atomic_duration_s": 120}
    )
    diagnostics = result["dynamics_diagnostics"]
    assert diagnostics["state_count"] == 6
    assert diagnostics["interval_count"] == 5
    assert diagnostics["linked_duration_s"] == 600
    assert len(result["rows"]) == 61


@pytest.mark.parametrize(
    "config",
    [
        {"continuous_delay_atomic_duration_s": 0},
        {"continuous_delay_projection_speed_floor_mps": 0},
        {"continuous_operational_disruption_probability": 1},
        {"continuous_operational_prior_rescue_probability": 0},
        {"continuous_occurrence_model_enabled": "true"},
    ],
)
def test_invalid_operational_physical_configuration_rejected(config):
    with pytest.raises(ValueError):
        fit_delay(observations(), ENABLED | config)
