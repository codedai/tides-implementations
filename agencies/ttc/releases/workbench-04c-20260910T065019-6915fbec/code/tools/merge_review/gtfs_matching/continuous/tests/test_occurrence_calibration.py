"""Synthetic software-contract tests, never agency accuracy/calibration evidence."""

import copy
import hashlib
import json

import pytest

from tools.merge_review.gtfs_matching.continuous.calibration import (
    _model_digest,
    calibrate_candidates,
    fit_calibration_model,
    occurrence_decision_guard,
)
from tools.merge_review.gtfs_matching.continuous.evaluation import (
    evaluate_shadow,
    validate_independent_labels,
    validate_training_provenance,
)
from tools.merge_review.gtfs_matching.continuous.train_calibration import (
    build_calibration_report,
    main,
)

CONFIG = {
    "continuous_occurrence_model_enabled": True,
    "continuous_scorer_fingerprint": "synthetic-contract-only",
}


def records():
    rows = []
    for number, (split, n) in enumerate(
        (("train", 100), ("validation", 40), ("test", 20)), 1
    ):
        for index in range(n):
            row = {
                "case_id": f"{split}-{index}",
                "episode_id": f"episode-{split}-{index}",
                "label_id": f"label-{split}-{index}",
                "evidence_id": f"evidence-{split}-{index}",
                "evidence_sha256": "a" * 64,
                "route_id": f"route-{split}",
                "service_date": f"2022-01-0{number}",
                "feed_version": "synthetic-feed",
                "verified": True,
                "independent": True,
                "label_source": "manual_verified",
                "split": split,
                "true_trip_id": "trip-1",
                "scorer_fingerprint": CONFIG["continuous_scorer_fingerprint"],
                "adjacent_trip_ids": ["trip-2"],
                "operating_regime": "ROUTINE",
            }
            row["candidates"] = [
                {
                    **{
                        field: row[field]
                        for field in ("feed_version", "route_id", "service_date")
                    },
                    "candidate_id": f"candidate-{rank}",
                    "trip_id": f"trip-{rank}",
                    "score": score,
                    "quality_eligible": True,
                    "operating_regime": "ROUTINE",
                    "performed_episode_id": row["episode_id"],
                    "occurrence_displacement": {
                        "crossed_occurrences": 0,
                        "outside_cell_s": 0.0,
                    },
                }
                for rank, score in enumerate((0.2, 1.3, 2.4), 1)
            ]
            rows.append(row)
    return rows


@pytest.fixture
def model():
    result = fit_calibration_model(records(), CONFIG)
    assert result["calibrated"]
    return result


def inference_candidates():
    return copy.deepcopy(
        next(row for row in records() if row["split"] == "validation")["candidates"]
    )


def evidence(candidate):
    return {
        **{
            field: candidate[field]
            for field in (
                "trip_id",
                "feed_version",
                "route_id",
                "service_date",
                "performed_episode_id",
            )
        },
        "kind": "verified_dispatch_assignment",
        "verified": True,
        "independent": True,
        "used_for_delay_fit": False,
        "uses_upstream_schedule_assignment": False,
        "evidence_id": "dispatch-source-event",
        "evidence_sha256": "b" * 64,
    }


def test_displaced_perfect_conditioned_fit_cannot_be_matched(model):
    candidates = inference_candidates()
    candidates[0]["conditioned_fit_cost"] = 0.0
    candidates[0]["occurrence_displacement"] = {
        "crossed_occurrences": 1,
        "outside_cell_s": 300,
    }
    before = copy.deepcopy(candidates)
    result = calibrate_candidates(candidates, model, CONFIG)
    assert result["status"] == "PATTERN_ONLY"
    assert result["credible_set"] == []
    assert result["candidate_probabilities"]["candidate-1"] > 0.9
    assert result["reasons"] == [
        "DISPLACED_OCCURRENCE_REQUIRES_INDEPENDENT_CORROBORATION"
    ]
    assert candidates == before


def test_legacy_off_keeps_old_decision_despite_displacement(model):
    candidates = inference_candidates()
    candidates[0]["occurrence_displacement"] = {
        "crossed_occurrences": 9,
        "outside_cell_s": 3000,
    }
    result = calibrate_candidates(
        candidates, model, CONFIG | {"continuous_occurrence_model_enabled": False}
    )
    assert result["status"] == "MATCHED"
    assert "occurrence_decision_guards" not in result


def test_independent_dispatch_identity_may_pass_guard_but_does_not_replace_calibration(
    model,
):
    candidates = inference_candidates()
    candidates[0]["occurrence_displacement"] = {
        "crossed_occurrences": 2,
        "outside_cell_s": 800,
    }
    candidates[0]["occurrence_corroboration"] = evidence(candidates[0])
    assert calibrate_candidates(candidates, model, CONFIG)["status"] == "MATCHED"
    assert calibrate_candidates(candidates, config=CONFIG)["status"] == "PATTERN_ONLY"


@pytest.mark.parametrize(
    "patch",
    [
        {"kind": "APC_stop_time_fit"},
        {"kind": "smooth_delay_process"},
        {"kind": "vehicle_sequence_prior"},
        {"verified": False},
        {"independent": False},
        {"trip_id": "other-trip"},
        {"service_date": "other-day"},
        {"performed_episode_id": "different-vehicle-episode"},
        {"used_for_delay_fit": True},
        {"uses_upstream_schedule_assignment": True},
        {"evidence_id": ""},
        {"evidence_sha256": "not-a-digest"},
    ],
)
def test_weak_or_misattributed_corroboration_never_exempts(patch):
    candidate = inference_candidates()[0]
    candidate["occurrence_displacement"] = {
        "crossed_occurrences": 1,
        "outside_cell_s": 100,
    }
    candidate["occurrence_corroboration"] = evidence(candidate) | patch
    assert not occurrence_decision_guard(candidate)["allowed"]


@pytest.mark.parametrize(
    "displacement",
    [
        None,
        {},
        {"crossed_occurrences": -1, "outside_cell_s": 0},
        {"crossed_occurrences": 0.5, "outside_cell_s": 0},
        {"crossed_occurrences": True, "outside_cell_s": 0},
        {"crossed_occurrences": 0, "outside_cell_s": float("nan")},
        {"crossed_occurrences": 0, "outside_cell_s": 0, "status": "NO_EVIDENCE"},
    ],
)
def test_missing_invalid_displacement_fails_closed(model, displacement):
    candidates = inference_candidates()
    candidates[0]["occurrence_displacement"] = displacement
    result = calibrate_candidates(candidates, model, CONFIG)
    assert result["credible_set"] == []
    assert result["reasons"] == ["MISSING_OR_INVALID_OCCURRENCE_DISPLACEMENT"]


def test_boundary_uncertainty_remains_stronger_than_guard(model):
    candidates = inference_candidates()
    candidates[0]["boundary_uncertain"] = True
    candidates[0]["occurrence_displacement"] = {
        "crossed_occurrences": 1,
        "outside_cell_s": 5,
    }
    assert (
        calibrate_candidates(candidates, model, CONFIG)["status"]
        == "BOUNDARY_UNCERTAIN"
    )


def test_occurrence_model_requires_full_independent_provenance(model):
    model.pop("training_provenance")
    model["model_digest"] = _model_digest(model)
    result = calibrate_candidates(inference_candidates(), model, CONFIG)
    assert result["reasons"] == ["CALIBRATION_INDEPENDENT_SPLIT_PROVENANCE_INVALID"]
    assert not result["calibrated"]


@pytest.mark.parametrize("field", ["route_id", "service_date", "episode_id"])
def test_declared_artifact_cannot_weaken_split_disjointness(model, field):
    provenance = model["training_provenance"]
    provenance["split_groups"]["test"][field][0] = provenance["split_groups"]["train"][
        field
    ][0]
    with pytest.raises(ValueError, match="leakage"):
        validate_training_provenance(provenance)


@pytest.mark.parametrize(
    "field", ["feed_version", "label_id", "evidence_id", "evidence_sha256"]
)
def test_strict_label_training_needs_source_bound_labels(field):
    rows = records()
    rows[0].pop(field)
    with pytest.raises(ValueError, match="provenance|SHA256"):
        fit_calibration_model(rows, CONFIG)


def test_missing_heldout_service_days_does_not_train():
    rows = [row for row in records() if row["split"] != "test"]
    assert (
        fit_calibration_model(rows, CONFIG)["reason"]
        == "MISSING_HELDOUT_ROUTE_SERVICE_DAY_LABELS"
    )


def test_test_outcomes_do_not_change_training_or_validation():
    rows = records()
    first = fit_calibration_model(rows, CONFIG)
    for row in rows:
        if row["split"] == "test":
            row["true_trip_id"] = "trip-2"
    second = fit_calibration_model(rows, CONFIG)
    assert first["bins"] == second["bins"]
    assert first["validation"] == second["validation"]
    assert (
        first["training_provenance"]["labels_sha256"]
        != second["training_provenance"]["labels_sha256"]
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"validated_domains": None},
        {"bins": None},
        {"feature_support": None},
    ],
)
def test_malformed_artifact_is_an_abstention_not_exception(model, patch):
    model.update(patch)
    model["model_digest"] = _model_digest(model)
    result = calibrate_candidates(inference_candidates(), model, CONFIG)
    assert not result["calibrated"]
    assert result["credible_set"] == []


@pytest.mark.parametrize(
    "probabilities",
    [
        [-1, 1, 0.5, 0.5],
        [0.9, 0.9, 0.9, 0.9],
        [0.9, 0.1],
    ],
)
def test_malformed_probability_distribution_cannot_become_calibrated(
    model, probabilities
):
    next(iter(model["bins"].values()))["probabilities"] = probabilities
    model["model_digest"] = _model_digest(model)
    result = calibrate_candidates(inference_candidates(), model, CONFIG)
    assert result["reasons"] == ["CALIBRATION_ARTIFACT_SCHEMA_INVALID"]
    assert result["candidate_probabilities"]["candidate-1"] is None


def test_rehashed_failed_reliability_is_not_usable(model):
    model["validation"]["ece"] = 0.99
    model["model_digest"] = _model_digest(model)
    result = calibrate_candidates(inference_candidates(), model, CONFIG)
    assert not result["calibrated"]
    assert result["reasons"] == ["CALIBRATION_INDEPENDENT_SPLIT_PROVENANCE_INVALID"]


def test_regime_stratified_test_metrics_and_no_label_nulls():
    labels = records()
    row = copy.deepcopy(next(row for row in labels if row["split"] == "test"))
    row.update(
        status="MATCHED", all_candidates=row["candidates"], decision_trip_id="trip-1"
    )
    row["all_candidates"][0].update(probability=0.95, calibrated=True)
    row["all_candidates"][0]["inferred_delay_regime"] = "ROUTINE_LIKE"
    report = evaluate_shadow([row], [row], labels, CONFIG)
    metrics = report["strata"]["operating_regime"]["ROUTINE"]["new"]["metrics"]
    assert metrics["exact_trip_accuracy"] == 1
    assert metrics["adjacent_trip_confusion"] == 0
    assert metrics["candidate_retrieval_recall"] == 1
    assert metrics["top3_coverage"] == 1
    assert metrics["precision_non_abstained"] == 1
    assert metrics["calibration_error"] == pytest.approx(0.05)
    assert report["validation"]["heldout_route_and_service_day"]
    assert "ROUTINE_LIKE" in report["strata"]["inferred_delay_regime"]
    empty = evaluate_shadow([], [row], [], CONFIG)
    assert empty["new"]["metrics"]["exact_trip_accuracy"] is None
    assert empty["new"]["metrics"]["calibration_error"] is None
    assert (
        "MISSING_HELDOUT_ROUTE_SERVICE_DAY_LABELS"
        in empty["validation"]["availability_reasons"]
    )


def test_incomplete_splits_do_not_claim_heldout_metrics():
    labels = [row for row in records() if row["split"] == "test"]
    row = labels[0] | {"status": "MATCHED", "decision_trip_id": "trip-1"}
    report = evaluate_shadow([], [row], labels, CONFIG)
    assert report["new"]["metrics"]["exact_trip_accuracy"] is None
    assert not report["validation"]["heldout_route_and_service_day"]


def test_offline_no_labels_report_and_exclusive_output(tmp_path):
    output = tmp_path / "uncalibrated.json"
    assert main(["--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["status"] == "UNCALIBRATED"
    assert report["heldout_test_reliability"]["ece"] is None
    assert report["activated"] is False
    with pytest.raises(FileExistsError):
        main(["--output", str(output)])


def test_offline_test_reliability_does_not_activate_model():
    report = build_calibration_report(records(), config=CONFIG)
    assert report["status"] == "FITTED_NOT_ACTIVATED"
    assert report["heldout_test_reliability"]["n"] == 20
    assert not report["heldout_test_reliability"][
        "test_used_for_fit_or_model_selection"
    ]
    assert report["model"]["training_n"] == 100
    assert validate_independent_labels(records())["count"] == 160


def test_offline_cli_accepts_persisted_shadow_envelope_without_rewriting(tmp_path):
    row = records()[-1] | {"status": "PATTERN_ONLY"}
    source = tmp_path / "global-episodes.json"
    source.write_text(json.dumps({"old": [], "new": [row]}))
    before = source.read_bytes()
    output = tmp_path / "report.json"
    assert (
        main(
            [
                "--old-results",
                str(source),
                "--new-results",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads(output.read_text())
    assert report["evaluation"]["old"]["case_count"] == 0
    assert report["evaluation"]["new"]["case_count"] == 1
    assert report["input_sha256"][str(source)] == hashlib.sha256(before).hexdigest()
    assert source.read_bytes() == before


@pytest.mark.parametrize("envelope", [{"new": []}, {"old": {}}, {"old": None}])
def test_offline_cli_rejects_missing_or_non_list_requested_side(tmp_path, envelope):
    source = tmp_path / "episodes.json"
    source.write_text(json.dumps(envelope))
    output = tmp_path / "report.json"
    with pytest.raises(ValueError, match="list-valued old side"):
        main(["--old-results", str(source), "--output", str(output)])
    assert not output.exists()
