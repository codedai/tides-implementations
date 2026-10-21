"""Agency-independent calibration/evaluation/sequence robustness fixtures.

Synthetic labels test software contracts only; they are never production
accuracy evidence or calibration artifacts for the agency experiment.
"""

import copy
import json

import pytest

from tools.merge_review.gtfs_matching.continuous.calibration import (
    calibrate_candidates,
    fit_calibration_model,
)
from tools.merge_review.gtfs_matching.continuous.evaluation import (
    evaluate_shadow,
    expected_calibration_error,
    load_verified_labels,
    validate_independent_labels,
)
from tools.merge_review.gtfs_matching.continuous.sequence import (
    decode_vehicle_day,
    transition_cost,
)


def candidates():
    return [
        {
            "candidate_id": f"occurrence-{rank}",
            "trip_id": f"trip-{rank}",
            "score": score,
            "pattern_id": "pattern",
            "quality_eligible": True,
            "route_id": "heldout-route",
            "service_date": "2022-02-02",
            "feed_version": "feed-a",
            "estimated_delay_s": 60,
            "coverage_metrics": {
                "projection_coverage": {"observation_ratio": 0.9},
                "schedule_alignment_coverage": {"observation_ratio": 0.9},
            },
        }
        for rank, score in enumerate((0.2, 1.3, 2.4), 1)
    ]


def label(index=0, split="test", true_trip_id="trip-1"):
    return {
        "case_id": f"case-{split}-{index}",
        "route_id": f"{split}-route",
        "service_date": {
            "train": "2022-01-01",
            "validation": "2022-02-02",
            "test": "2022-03-03",
        }[split],
        "episode_id": f"episode-{split}-{index}",
        "feed_version": "feed-a",
        "verified": True,
        "independent": True,
        "label_source": "manual_verified",
        "split": split,
        "true_trip_id": true_trip_id,
    }


def calibration_rows(winner="trip-1"):
    rows = []
    for split, count in (("train", 100), ("validation", 40)):
        for index in range(count):
            row = label(index, split, winner)
            if split == "validation":
                row["route_id"] = "heldout-route"
            row.update(candidates=candidates(), scorer_fingerprint="scorer-v1")
            if winner is None:
                row.pop("true_trip_id")
                row["no_scheduled_trip"] = True
            rows.append(row)
    return rows


@pytest.fixture
def calibrated_model():
    model = fit_calibration_model(
        calibration_rows(), {"continuous_scorer_fingerprint": "scorer-v1"}
    )
    assert model["calibrated"]
    return model


def test_no_labels_never_manufactures_probabilities_or_credible_candidates():
    source = candidates()
    before = copy.deepcopy(source)
    result = calibrate_candidates(source)
    assert result["status"] == "PATTERN_ONLY"
    assert result["credible_set"] == []
    assert all(value is None for value in result["candidate_probabilities"].values())
    assert result["unknown_probability"] is None
    assert source == before
    assert not fit_calibration_model([])["calibrated"]


def test_no_quality_evidence_is_not_pattern_claim():
    source = [{"candidate_id": "x", "score": 1}]
    assert calibrate_candidates(source)["status"] == "UNMATCHED"
    assert calibrate_candidates([])["credible_set"] == []


def test_verified_empirical_model_has_proper_unknown_mass_and_credible_set(
    calibrated_model,
):
    result = calibrate_candidates(
        candidates(), calibrated_model, {"continuous_scorer_fingerprint": "scorer-v1"}
    )
    assert result["status"] == "MATCHED"
    assert result["credible_set"] == ["occurrence-1"]
    assert sum(result["candidate_probabilities"].values()) + result[
        "unknown_probability"
    ] == pytest.approx(1)
    assert 0 < result["unknown_probability"] < 0.1
    assert result["calibrated"]
    assert (
        result["pattern_confidence"] is None
    )  # occurrence calibration is not pattern calibration


def test_unknown_non_candidate_winner_mass_forces_abstention():
    model = fit_calibration_model(
        calibration_rows(None), {"continuous_scorer_fingerprint": "scorer-v1"}
    )
    assert model["calibrated"]
    result = calibrate_candidates(
        candidates(), model, {"continuous_scorer_fingerprint": "scorer-v1"}
    )
    assert result["unknown_probability"] > 0.9
    assert result["credible_set"] == []
    assert result["status"] == "PATTERN_ONLY"


def test_absolute_quality_failure_does_not_renormalize_remaining_candidates(
    calibrated_model,
):
    source = candidates()
    source[0]["quality_eligible"] = False
    result = calibrate_candidates(
        source, calibrated_model, {"continuous_scorer_fingerprint": "scorer-v1"}
    )
    assert result["credible_set"] == []
    assert result["candidate_probabilities"]["occurrence-1"] > 0.9
    assert result["reasons"] == ["CREDIBLE_CANDIDATE_FAILS_ABSOLUTE_QUALITY_GATE"]


def test_output_zero_is_configurable_without_discarding_unknown_mass(calibrated_model):
    result = calibrate_candidates(
        candidates(),
        calibrated_model,
        {
            "continuous_scorer_fingerprint": "scorer-v1",
            "continuous_calibration_output_max": 0,
        },
    )
    assert result["credible_set"] == []
    assert "OUTPUT_LIMIT" in result["reasons"][0]


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("route_id", "never-validated-route"),
        ("service_date", "2030-01-01"),
        ("feed_version", "new-feed"),
    ],
)
def test_unvalidated_domain_cannot_inherit_calibration(
    calibrated_model, field, replacement
):
    source = candidates()
    for candidate in source:
        candidate[field] = replacement
    result = calibrate_candidates(
        source, calibrated_model, {"continuous_scorer_fingerprint": "scorer-v1"}
    )
    assert not result["calibrated"]
    assert result["unknown_probability"] is None
    assert "DOMAIN" in result["reasons"][0]


def test_changed_weights_or_legacy_scorer_cannot_reuse_model(calibrated_model):
    result = calibrate_candidates(
        candidates(),
        calibrated_model,
        {"continuous_scorer_fingerprint": "changed-weights"},
    )
    assert not result["calibrated"]
    assert "FINGERPRINT" in result["reasons"][0]


def test_unverified_model_mutations_and_schema_changes_abstain(calibrated_model):
    model = copy.deepcopy(calibrated_model)
    next(iter(model["bins"].values()))["probabilities"] = [1, 0, 0, 0]
    result = calibrate_candidates(
        candidates(), model, {"continuous_scorer_fingerprint": "scorer-v1"}
    )
    assert "INTEGRITY" in result["reasons"][0]
    model["feature_schema"] = ["legacy_dtw_score"]
    result = calibrate_candidates(
        candidates(), model, {"continuous_scorer_fingerprint": "scorer-v1"}
    )
    assert "SCHEMA" in result["reasons"][0]


def test_feature_extrapolation_is_explicit_abstention(calibrated_model):
    source = candidates()
    source[0]["score"] = -10
    result = calibrate_candidates(
        source, calibrated_model, {"continuous_scorer_fingerprint": "scorer-v1"}
    )
    assert "OUT_OF_SUPPORT" in result["reasons"][0]


def test_boundary_uncertainty_cannot_be_promoted_to_matched(calibrated_model):
    source = candidates()
    source[0]["boundary_uncertain"] = True
    result = calibrate_candidates(
        source, calibrated_model, {"continuous_scorer_fingerprint": "scorer-v1"}
    )
    assert result["status"] == "BOUNDARY_UNCERTAIN"


def test_random_label_model_fails_independent_reliability():
    rows = calibration_rows()
    for row in rows:
        if row["split"] == "validation":
            row["true_trip_id"] = "trip-2"
    model = fit_calibration_model(rows, {"continuous_scorer_fingerprint": "scorer-v1"})
    assert not model["calibrated"]
    assert model["validation"]["ece"] > 0.9


@pytest.mark.parametrize("field", ["route_id", "service_date", "episode_id"])
def test_split_validation_rejects_each_axis_of_operating_episode_leakage(field):
    first, second = label(0, "train"), label(1, "validation")
    second[field] = first[field]
    with pytest.raises(ValueError, match="leakage"):
        validate_independent_labels([first, second])


def test_multiple_fragments_can_share_episode_only_within_one_split():
    first, second = label(0), label(1)
    second["episode_id"] = first["episode_id"]
    assert validate_independent_labels([first, second])["count"] == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"verified": False},
        {"independent": False},
        {"label_source": "matcher_output"},
        {"label_source": "AvgOTP"},
        {"schedule_derived": True},
        {"uses_upstream_schedule_assignment": True},
        {"episode_id": ""},
        {"split": "random-row-split"},
        {"no_scheduled_trip": True},
    ],
)
def test_non_independent_or_ambiguous_labels_rejected(patch):
    with pytest.raises(ValueError):
        validate_independent_labels([label() | patch])


def test_duplicate_conflicting_labels_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        validate_independent_labels([label(), label() | {"true_trip_id": "different"}])


def test_calibration_requires_consistent_explicit_scorer_fingerprint():
    with pytest.raises(ValueError, match="fingerprint"):
        fit_calibration_model(calibration_rows())


def test_calibration_fragments_cannot_multiply_independent_episode_sample_count():
    rows = calibration_rows()
    rows[1]["episode_id"] = rows[0]["episode_id"]
    with pytest.raises(ValueError, match="one representative"):
        fit_calibration_model(rows, {"continuous_scorer_fingerprint": "scorer-v1"})


@pytest.mark.parametrize(
    "patch",
    [
        {"continuous_calibration_prior_count": 0},
        {"continuous_calibration_credible_mass": 1.1},
        {"continuous_calibration_min_validation": 0},
        {"continuous_calibration_score_bins": [2, 1]},
    ],
)
def test_invalid_calibration_thresholds_are_rejected(patch):
    with pytest.raises(ValueError):
        calibrate_candidates(candidates(), config=patch)


def test_finite_candidate_ids_and_scores_are_checked():
    with pytest.raises(ValueError, match="unique"):
        calibrate_candidates([{"candidate_id": "same", "score": 1}] * 2)
    with pytest.raises(ValueError, match="stable"):
        calibrate_candidates([{"score": 1}])


def test_read_only_label_loader_json_and_jsonl(tmp_path):
    for suffix in (".json", ".jsonl"):
        path = tmp_path / f"independent{suffix}"
        content = (
            json.dumps([label()]) if suffix == ".json" else json.dumps(label()) + "\n"
        )
        path.write_text(content)
        before = path.read_bytes()
        assert load_verified_labels(path) == [label()]
        assert path.read_bytes() == before


def shadow_row(truth_label, status="MATCHED", ranked=None):
    return {
        key: truth_label[key]
        for key in ("case_id", "route_id", "service_date", "feed_version")
    } | {
        "status": status,
        "all_candidates": ranked or candidates(),
        "candidates": (ranked or candidates())[:3] if status == "MATCHED" else [],
        "runtime_s": 2,
        "local_headway_s": 180,
        "shape_structure": "branch",
        "valid_projection_point_ratio": 0.8,
    }


def test_unlabeled_shadow_reports_no_invented_accuracy_and_keeps_descriptives():
    old = [shadow_row(label())]
    new = [shadow_row(label(), "PATTERN_ONLY")]
    report = evaluate_shadow(old, new)
    assert report["validation"]["status"] == "NO_INDEPENDENT_LABELS"
    assert report["new"]["metrics"]["exact_trip_accuracy"] is None
    assert (
        report["new"]["metric_details"]["exact_trip_accuracy"]["reason"]
        == "NO_INDEPENDENT_LABELS"
    )
    assert report["new"]["metrics"]["abstention_rate"] == 1
    assert report["new"]["metrics"]["runtime_mean_s"] == 2
    assert report["descriptive"]["status_changes"] == 1
    assert report["descriptive"]["agreement_is_not_accuracy"]


def test_independent_evaluation_metrics_and_strata():
    first = label(0) | {
        "true_pattern_id": "pattern",
        "true_delay_s": 90,
        "adjacent_trip_ids": ["trip-2"],
        "boundary_times_s": [120],
    }
    second = label(1) | {"adjacent_trip_ids": ["trip-2"]}
    good = shadow_row(first)
    good["all_candidates"][0].update(probability=0.8, calibrated=True)
    good["boundary_times_s"] = [120, 121]
    bad_candidates = candidates()[1:] + candidates()[:1]
    bad = shadow_row(second, ranked=bad_candidates)
    report = evaluate_shadow([], [good, bad], [first, second])
    metrics = report["new"]["metrics"]
    assert metrics["candidate_retrieval_recall"] == 1
    assert metrics["exact_trip_accuracy"] == 0.5
    assert metrics["adjacent_trip_confusion"] == 0.5
    assert metrics["top3_coverage"] == 1
    assert metrics["precision_non_abstained"] == 0.5
    assert metrics["pattern_accuracy"] == 1
    assert metrics["delay_mae_s"] == 30
    assert metrics["calibration_error"] == pytest.approx(0.2)
    assert metrics["boundary_precision"] == 0.5
    assert metrics["boundary_recall"] == 1
    assert metrics["boundary_f1"] == pytest.approx(2 / 3)
    assert set(report["strata"]) == {
        "route",
        "headway",
        "shape_structure",
        "data_quality",
    }


def test_training_validation_labels_are_not_final_test_accuracy():
    truth = label(0, "train")
    report = evaluate_shadow([], [shadow_row(truth)], [truth])
    assert report["validation"]["test_label_count"] == 0
    assert report["new"]["metrics"]["exact_trip_accuracy"] is None


def test_no_scheduled_trip_label_penalizes_forced_match_precision():
    truth = label()
    truth.pop("true_trip_id")
    truth["no_scheduled_trip"] = True
    report = evaluate_shadow([], [shadow_row(truth)], [truth])
    assert report["new"]["metrics"]["precision_non_abstained"] == 0
    assert report["new"]["metrics"]["candidate_retrieval_recall"] is None


def test_ece_does_not_accept_scores_as_probabilities():
    assert expected_calibration_error([]) is None
    assert expected_calibration_error([(0.5, 1), (0.5, 0)]) == 0
    with pytest.raises(ValueError):
        expected_calibration_error([(2.0, 1)])


def trip(case="a", start=0, end=600, vehicle="v", day="2022-02-02"):
    return {
        "case_id": case,
        "vehicle_id": vehicle,
        "service_date": day,
        "start_time_s": start,
        "end_time_s": end,
        "start_progress_m": 0,
        "end_progress_m": 1000,
        "candidates": candidates(),
    }


def test_sequence_disabled_is_non_mutating_annotation_only():
    rows = [trip()]
    before = copy.deepcopy(rows)
    result = decode_vehicle_day(rows)
    assert not result["enabled"]
    assert result["best_path"] == []
    assert rows == before


def test_sequence_groups_vehicle_service_days_and_retains_unknown_states():
    rows = [trip(), trip("b", vehicle="other"), trip("c", day="2022-02-03")]
    rows[-1]["candidates"] = []
    before = copy.deepcopy(rows)
    result = decode_vehicle_day(rows, {"sequence_enabled": True})
    assert len(result["groups"]) == 3
    assert len(result["best_path"]) == 3
    assert any(item["candidate_id"] is None for item in result["best_path"])
    assert result["annotation_only"]
    assert result["requires_sequence_recalibration_for_decision_changes"]
    assert rows == before


def test_sequence_real_time_delay_continuity_can_disambiguate_adjacent_occurrences():
    first, second = trip(), trip("b", 700, 1300)
    first["candidates"] = [candidates()[0]]
    second["candidates"] = candidates()[1:]
    second["candidates"][0].update(score=0.1, estimated_delay_s=1800)
    second["candidates"][1].update(score=0.15, estimated_delay_s=60)
    result = decode_vehicle_day([second, first], {"sequence_enabled": True})
    assert [item["candidate_id"] for item in result["best_path"]] == [
        "occurrence-1",
        "occurrence-3",
    ]
    assert all(
        item["calibrated_probability"] is None
        for rows in result["candidate_contributions"].values()
        for item in rows
    )


def test_block_changes_and_vehicle_substitutions_are_soft_not_filters():
    previous, current = candidates()[:2]
    previous.update(block_id="block-a", assigned_vehicle_id="vehicle-a")
    current.update(block_id="block-b", assigned_vehicle_id="vehicle-b")
    score = transition_cost(trip(), previous, trip("b", 700, 1300), current)
    assert 0 < score["components"]["block_continuity_cost"] < 1
    assert 0 < score["components"]["vehicle_substitution_cost"] < 1
    assert score["cost"] < 10


def test_plausible_split_observations_are_not_hard_duplicate_occurrence_failure():
    first, second = trip(), trip("b", 700, 1000)
    second["start_progress_m"] = 1100
    candidate = candidates()[0] | {"reference_id": "canonical"}
    score = transition_cost(first, candidate, second, candidate)
    assert score["components"]["split_or_repeated_occurrence_cost"] == 0.05
    assert "SPLIT_OBSERVATIONS_SAME_OCCURRENCE_HYPOTHESIS" in score["reasons"]


def test_split_progress_never_compared_across_different_canonical_axes():
    first, second = trip(), trip("b", 700, 1000)
    second["start_progress_m"] = 1100
    previous = candidates()[0] | {"reference_id": "one"}
    current = candidates()[0] | {"reference_id": "other"}
    score = transition_cost(first, previous, second, current)
    assert score["components"]["split_or_repeated_occurrence_cost"] == 1.5


def test_terminal_reachability_uses_metres_and_actual_elapsed_seconds():
    previous, current = candidates()[:2]
    previous["terminal_end_lonlat"] = [0, 0]
    current["terminal_start_lonlat"] = [0, 0.1]
    short = transition_cost(trip(), previous, trip("b", 610, 1200), current)
    long = transition_cost(trip(), previous, trip("b", 1600, 2200), current)
    assert short["terminal_distance_m"] == pytest.approx(11119.5, abs=1)
    assert short["components"]["terminal_reachability_cost"] > 0
    assert long["components"]["terminal_reachability_cost"] == 0


def test_missing_trip_hints_are_not_invented_from_long_layovers():
    previous, current = candidates()[:2]
    previous["scheduled_end_time_s"] = 600
    current["scheduled_start_time_s"] = 5000
    without = transition_cost(trip(), previous, trip("b", 5000, 6000), current)
    with_hint = transition_cost(
        trip(),
        previous,
        trip("b", 5000, 6000),
        current | {"missing_scheduled_trip_count": 2},
    )
    assert without["components"]["missing_scheduled_trip_cost"] == 0
    assert with_hint["components"]["missing_scheduled_trip_cost"] == 0.16


def test_sequence_after_midnight_times_stay_ordered_in_real_seconds():
    first, second = (
        trip("a", 25 * 3600, 26 * 3600),
        trip("b", 26 * 3600 + 100, 27 * 3600),
    )
    result = decode_vehicle_day([second, first], {"sequence_enabled": True})
    assert [item["case_id"] for item in result["best_path"]] == ["a", "b"]
    assert result["best_path"][1]["transition"].get("observed_gap_s") == 100


def test_sequence_rejects_duplicate_episode_identity_and_invalid_units():
    with pytest.raises(ValueError, match="Duplicate"):
        decode_vehicle_day([trip(), trip()], {"sequence_enabled": True})
    with pytest.raises(ValueError, match="threshold"):
        decode_vehicle_day(
            [], {"sequence_enabled": True, "sequence_terminal_speed_max_mps": -1}
        )
