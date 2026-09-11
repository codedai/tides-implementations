"""Synthetic mechanics only: these examples are not operational ground truth."""

import copy
import json
import math

import pytest

from tools.merge_review.gtfs_matching.calibration import (
    fit_calibrator,
    predict_probability,
    validate_calibrator,
)
from tools.merge_review.gtfs_matching.integration import (
    aggregate_candidate,
    compare_boundary_models,
    match_candidate_segment,
)
from tools.merge_review.gtfs_matching.retrieval import (
    for_reference,
    retrieve_candidates,
    trip_key,
)
from tools.merge_review.gtfs_matching.scoring import (
    constrained_dtw,
    directed_p95_hausdorff,
    fragments,
    partial_frechet,
    score_whole_with_shared_delay,
    score_window,
    smooth_delay_viterbi,
    weighted_isotonic,
)
from tools.merge_review.gtfs_matching.windows import (
    build_windows,
    observation_key,
    quality,
)


def points(count=25, delay=0, reference="r"):
    return [
        {
            "source": "avl",
            "rid": f"a{i}",
            "source_rid": f"a{i}",
            "t": i * 15 + delay,
            "s_m": i * 150,
            "reference_id": reference,
            "family_id": "f",
            "fragment_id": "f1",
            "projection_status": "ON_REFERENCE",
            "quality": 1,
        }
        for i in range(count)
    ]


def trip(identifier="T", offset=0, ref="r", date="2023-11-06", feed="f"):
    return {
        "trip_id": identifier,
        "service_date": date,
        "feed_version": feed,
        "route_id": "29",
        "reference_id": ref,
        "family_id": "f",
        "pattern_id": "pattern",
        "scheduled": [
            {
                "t": offset + i * 60,
                "arrival_t": offset + i * 60,
                "departure_t": offset + i * 60,
                "s_m": i * 600,
            }
            for i in range(11)
        ],
    }


def test_atomic_windows_disjoint_and_multiscale_source_weights_sum_one():
    rows = points()
    result = build_windows(rows, {"window_event_windows": [{"start": 20, "end": 70}]})
    ownership = [key for w in result["atomic"] for key in w["observation_ids"]]
    assert len(ownership) == len(set(ownership)) == len(rows)
    assert all(w["end"] - w["start"] == 60 for w in result["atomic"])
    assert {w["end"] - w["start"] for w in result["contexts"]} == {300, 600}
    assert all(
        math.isclose(value, 1) for value in result["observation_weights"].values()
    )
    assert result["event_windows"]


def test_empty_atomic_minutes_keep_missing_time_visible():
    rows = points(3)
    rows.append(dict(rows[-1], rid="later", source_rid="later", t=600))
    result = build_windows(rows)
    empty = [w for w in result["atomic"] if not w["observation_ids"]]
    assert empty and all(w["quality"]["status"] == "NO_EVIDENCE" for w in empty)


@pytest.mark.parametrize(
    "rows",
    [
        points(2),
        [dict(r, s_m=0) for r in points()],
        [dict(r, projection_status="OFF_REFERENCE") for r in points()],
        [dict(r, t=None) for r in points()],
    ],
)
def test_insufficient_geometry_is_no_evidence(rows):
    assert quality(rows)["status"] == "NO_EVIDENCE"


def test_retrieval_union_not_three_and_service_feed_identity_preserved():
    rows = points(delay=300)
    trips = [trip(str(i), offset=i * 60) for i in range(12)]
    trips += [
        trip("0", offset=300, date="2023-11-05"),
        trip("0", offset=300, feed="different"),
    ]
    result = retrieve_candidates(
        build_windows(rows), rows, trips, {"retrieval_top_k": 12}
    )
    assert len(result["candidate_ids"]) > 3 and not result["union_truncated"]
    assert len({trip_key(t) for t in trips}) == len(trips)
    assert any(r["service_date"] == "2023-11-05" for r in result["trips"])
    explicit = retrieve_candidates(
        build_windows(rows), rows, trips, service_date="2023-11-06"
    )
    assert all(t["service_date"] == "2023-11-06" for t in explicit["trips"])


def test_per_reference_alternative_is_not_primary_coordinate_reuse():
    rows = points(3)
    rows[0]["projection_alternatives"] = [
        {
            "reference_id": "other",
            "s_m": 999,
            "projection_status": "ON_REFERENCE",
            "quality": 1,
        }
    ]
    selected = for_reference(rows, "other")
    assert len(selected) == 1 and selected[0]["s_m"] == 999
    assert for_reference(rows, "absent") == []
    assert rows[0]["s_m"] == 0


def test_robust_partial_metrics_and_time_band():
    observed = [(10, 100), (20, 200), (30, 300)]
    reference = [(i, i * 10) for i in range(0, 101, 5)]
    assert constrained_dtw(observed, reference) == pytest.approx(0)
    assert (
        partial_frechet(
            [(100, 10), (200, 20)], [(0, 0), (100, 10), (200, 20), (300, 30)]
        )
        == 0
    )
    assert directed_p95_hausdorff([(100, 10), (200, 20)], [(100, 10), (200, 20)]) == 0
    assert (
        constrained_dtw(
            [(1000, 100), (1010, 200)], reference, {"score_time_band_s": 10}
        )
        is None
    )


def test_constrained_dtw_rejects_disallowed_slope_and_reverse_reference():
    assert (
        constrained_dtw(
            [(0, 0), (10, 100)],
            [(0, 0), (10, 100)],
            {"score_warp_min_slope": 1.5, "score_warp_max_slope": 2},
        )
        is None
    )
    assert constrained_dtw([(0, 0), (10, 100)], [(0, 100), (10, 0)]) is None
    with pytest.raises(ValueError, match="at least three"):
        score_window(points(), trip(), 0, {"score_max_points": 1})


def test_off_reference_and_scope_separator_never_bridged():
    rows = points(9)
    rows[3].update(projection_status="OFF_REFERENCE", s_m=1e9)
    rows[6]["scope_separator_before"] = True
    chunks = fragments(rows)
    assert [len(c) for c in chunks] == [3, 2, 3]
    assert not any(any(r["rid"] == "a3" for r in c) for c in chunks)
    result = score_window(rows, trip(), 0)
    assert result["apc_used_as_gps"] is False
    assert len(result["fragments"]) == 3
    assert result["discarded_coverage_penalty"] > 0
    assert all("avl|a3" not in m["sampled_source_ids"] for m in result["fragments"])


def test_viterbi_penalizes_adjacent_delay_jumps():
    rows = [
        {
            "window_id": "a",
            "anchor_t": 0,
            "delay_states": [{"delay_s": 0, "cost": 0}, {"delay_s": 600, "cost": 0.2}],
        },
        {
            "window_id": "b",
            "anchor_t": 60,
            "delay_states": [{"delay_s": 0, "cost": 0.1}, {"delay_s": 600, "cost": 0}],
        },
        {
            "window_id": "c",
            "anchor_t": 120,
            "delay_states": [{"delay_s": 0, "cost": 0}, {"delay_s": 600, "cost": 0.2}],
        },
    ]
    assert set(smooth_delay_viterbi(rows).values()) == {0}


def test_single_vs_two_candidate_boundary_model():
    local = {
        name: [
            {
                "kind": "atomic",
                "start": i * 60,
                "end": (i + 1) * 60,
                "score": 0 if (i < 3) == (name == "first") else 3,
                "weight": 1,
            }
            for i in range(6)
        ]
        for name in ("first", "second")
    }
    result = compare_boundary_models(local)
    assert result["boundary_uncertain"] and result["changepoint_t"] == 180
    assert not result["verified_trip_boundary"]


def test_integration_apc_interval_not_fabricated_avl_and_probabilities_uncalibrated():
    rows = points()
    apc = [
        {
            "source": "apc",
            "rid": "p",
            "t": 100,
            "interval_start": 100,
            "interval_end": 130,
            "projection_status": "STOP_INTERVAL_ONLY",
            "canonical_stop_intervals": [
                {
                    "reference_id": "r",
                    "stop_id": "stop",
                    "s_min_m": 1100,
                    "s_max_m": 1100,
                    "ambiguous": False,
                }
            ],
        }
    ]
    before = copy.deepcopy((rows, apc))
    result = match_candidate_segment({"points": rows}, apc, [], [trip()], route_id="29")
    assert result["all_candidates"] and len(result["candidates"]) <= 3
    candidate = result["all_candidates"][0]
    assert candidate["apc_support"]["count"] == 1
    assert candidate["apc_support"]["rows"][0]["not_a_gps_point"]
    assert candidate["probability"] is None and not candidate["calibrated"]
    assert not result["diagnostics"]["apc_used_as_gps"]
    assert (rows, apc) == before
    json.dumps(result, allow_nan=False)


def test_unprojectable_rows_not_forced_to_matched():
    result = match_candidate_segment(
        {"points": [dict(r, projection_status="OFF_REFERENCE") for r in points()]},
        [],
        [],
        [trip()],
    )
    assert result["evidence_status"] == "NO_EVIDENCE" and result["candidates"] == []
    assert result["status"] == "UNMATCHED"


def test_off_reference_apc_cannot_be_rescued_using_diagnostic_stop_intervals():
    apc = [
        {
            "source": "apc",
            "rid": "off-apc",
            "t": 100,
            "projection_status": "OFF_REFERENCE",
            "interval_start": 100,
            "interval_end": 120,
            "canonical_stop_intervals": [
                {
                    "reference_id": "r",
                    "s_min_m": 1100,
                    "s_max_m": 1100,
                    "ambiguous": False,
                }
            ],
        }
    ]
    result = match_candidate_segment({"points": points()}, apc, [], [trip()])
    assert result["all_candidates"][0]["apc_support"]["count"] == 0


def test_secondary_reference_can_supply_evidence_when_primary_has_none():
    rows = points()
    for row in rows:
        row["projection_alternatives"] = [
            {
                "reference_id": "secondary",
                "family_id": "f",
                "s_m": row["s_m"],
                "projection_status": "ON_REFERENCE",
                "quality": 1,
            }
        ]
        row.update(projection_status="OFF_REFERENCE", s_m=None)
    result = match_candidate_segment({"points": rows}, [], [], [trip(ref="secondary")])
    assert result["evidence_status"] == "EVIDENCE"
    assert result["all_candidates"][0]["reference_id"] == "secondary"
    assert result["evidence_coverage"]["valid_projection_point_ratio"] == 1


def test_projection_lateral_heading_costs_affect_dtw_not_off_reference_rows():
    ideal = points()
    poor = [dict(r, lateral_m=40, heading_error_deg=70) for r in ideal]
    a = score_window(ideal, trip(), 0)
    b = score_window(poor, trip(), 0)
    assert b["score"] > a["score"]
    poor[0].update(
        projection_status="OFF_REFERENCE", lateral_m=1e99, heading_error_deg=1e99
    )
    c = score_window(poor, trip(), 0)
    assert c["score"] < 100


def test_sample_weights_preserve_full_normalized_source_mass():
    rows = points(100)
    for i, row in enumerate(rows):
        row.update(t=i * 5, s_m=i * 50, _evidence_weight=0.1)
    result = score_window(rows, trip(), 0, {"score_max_points": 10})
    assert sum(result["fragments"][0]["sampled_weights"]) == pytest.approx(10)
    assert result["fragments"][0]["weight"] == pytest.approx(10)


def test_adaptive_optional_events_are_visible_without_duplicate_source_weight():
    rows = points()
    rows[10]["scope_separator_before"] = True
    result = match_candidate_segment(
        {"points": rows}, [], [], [trip()], {"window_adaptive_enabled": True}
    )
    assert result["windows"]["event_windows"]
    assert all(
        math.isclose(v, 1) for v in result["windows"]["observation_weights"].values()
    )


def test_calibration_does_not_turn_unverified_synthetic_labels_into_probabilities():
    synthetic = [
        {
            "features": {"support_score": i / 100, "coverage": 1, "margin": 0.2},
            "label": i > 50,
            "group_id": str(i),
            "label_source": "synthetic",
        }
        for i in range(100)
    ]
    model = fit_calibrator(synthetic)
    assert (
        not model["calibrated"]
        and predict_probability(synthetic[0]["features"], model) is None
    )
    with pytest.raises(ValueError, match="verified"):
        fit_calibrator(synthetic, labels_are_verified=True)


def test_heldout_validator_detects_group_leakage_and_reports_metrics():
    model = {
        "feature_names": ["support_score"],
        "means": [0],
        "scales": [1],
        "coefficients": [2],
        "intercept": -1,
        "training_groups": ["train"],
    }
    with pytest.raises(ValueError, match="overlap"):
        validate_calibrator(
            model, [{"features": {"support_score": 1}, "label": 1, "group_id": "train"}]
        )
    result = validate_calibrator(
        model, [{"features": {"support_score": 1}, "label": 1, "group_id": "holdout"}]
    )
    assert result["status"] == "HELD_OUT_VALIDATION" and 0 <= result["brier"] <= 1


def test_fitter_mock_verified_label_boundary_uses_disjoint_groups():
    # Mock the external verified-label contract solely for fitter unit testing.
    # This synthetic model is neither persisted nor used as TTC ground truth.
    supplied = [
        {
            "features": {"support_score": i % 2, "coverage": 0.9, "margin": 0.3},
            "label": i % 2,
            "group_id": f"external-group-{i}",
            "label_source": "manual_verified",
        }
        for i in range(120)
    ]
    model = fit_calibrator(supplied, labels_are_verified=True)
    assert model["labels_verified_by_caller"]
    assert not set(model["training_groups"]) & set(model["validation"]["groups"])
    assert model["validation"]["n"] >= 20
    assert math.isfinite(model["validation"]["log_loss"])


def test_sampling_is_disclosed_and_original_ids_not_mutated():
    rows = points(100)
    for i, row in enumerate(rows):
        row.update(t=i * 5, s_m=i * 50)
    result = score_window(rows, trip(), 0, {"score_max_points": 10})
    assert result["scoring_sampled"]
    assert (
        result["fragments"][0]["scored_points"]
        < result["fragments"][0]["original_points"]
    )
    assert observation_key(rows[-1]) == "avl|a99"


@pytest.mark.parametrize(
    "whole_status,point_ratio,duration_ratio",
    [("NO_EVIDENCE", 0, 0), ("EVIDENCE", 0.1, 1), ("EVIDENCE", 1, 0.1)],
)
def test_good_local_scores_cannot_bypass_failed_whole_validation(
    monkeypatch, whole_status, point_ratio, duration_ratio
):
    monkeypatch.setattr(
        "tools.merge_review.gtfs_matching.integration.score_whole_with_shared_delay",
        lambda *args, **kwargs: {
            "status": whole_status,
            "score": 0 if whole_status == "EVIDENCE" else None,
            "schedule_aligned_point_ratio": point_ratio,
            "schedule_aligned_duration_ratio": duration_ratio,
        },
    )
    result = match_candidate_segment({"points": points()}, [], [], [trip()])
    assert result["all_candidates"]
    assert result["status"] != "MATCHED"
    assert "WHOLE_ALIGNMENT_OR_SCHEDULE_COVERAGE_NOT_VALIDATED" in result["reasons"]


def test_unsuccessful_quality_eligible_windows_pay_weighted_missing_penalty(
    monkeypatch,
):
    monkeypatch.setattr(
        "tools.merge_review.gtfs_matching.integration.score_whole_with_shared_delay",
        lambda *args, **kwargs: {
            "status": "EVIDENCE",
            "score": 0,
            "schedule_aligned_point_ratio": 1,
            "schedule_aligned_duration_ratio": 1,
        },
    )
    local = [
        {
            "window_id": str(i),
            "kind": "atomic",
            "start": i * 60,
            "end": (i + 1) * 60,
            "weight": 1,
            "quality_eligible": True,
            "score": 0 if i == 0 else None,
            "estimated_delay_s": 0,
        }
        for i in range(10)
    ]
    result = aggregate_candidate(local, points(), [], trip())
    assert result["schedule_supported_window_weight_ratio"] == pytest.approx(0.1)
    assert result["schedule_unsupported_valid_window_count"] == 9
    assert result["weighted_mean"] == pytest.approx(3.6)
    assert result["weighted_median"] == result["weighted_p90"] == 4
    assert result["score"] > 3


def supported_window(rows, delay, identifier="manual-window"):
    return {
        "window_id": identifier,
        "score": 0,
        "anchor_t": (rows[0]["t"] + rows[-1]["t"]) / 2,
        "start": rows[0]["t"],
        "end": rows[-1]["t"] + 1,
        "estimated_delay_s": delay,
        "fragments": [{"source_ids": [observation_key(r) for r in rows]}],
    }


def test_shared_delay_process_tracks_drift_and_resets_at_off_reference_gap():
    rows = points(40)
    for i, row in enumerate(rows):
        row["t"] += i * 5 + (600 if i > 19 else 0)
    rows[19].update(projection_status="OFF_REFERENCE", s_m=None)
    local = []
    for chunk_index, chunk in enumerate(fragments(rows)):
        for i in range(0, len(chunk), 3):
            group = chunk[i : i + 3]
            center = group[len(group) // 2]
            local.append(
                supported_window(
                    group, center["t"] - center["s_m"] / 10, f"{chunk_index}:{i}"
                )
            )
    config = {"score_time_band_s": 30}
    fixed = score_window(rows, trip(), 400, config, full_observation_dtw=True)
    assert fixed["status"] == "NO_EVIDENCE"
    result = score_whole_with_shared_delay(rows, trip(), local, config)
    assert result["status"] == "EVIDENCE"
    assert result["schedule_aligned_point_count"] == 39
    assert result["schedule_aligned_point_ratio"] == pytest.approx(39 / 40)
    assert len(result["fragments"]) == 2
    assert all(
        f["dtw_scored_points"] == f["original_points"] for f in result["fragments"]
    )
    process = {r["source_id"]: r for r in result["delay_process"]}
    assert process["avl|a19"]["delay_s"] is None
    assert not process["avl|a19"]["schedule_aligned"]
    assert process["avl|a18"]["fragment_id"] != process["avl|a20"]["fragment_id"]
    assert process["avl|a20"]["delay_s"] > 600
    assert result["schedule_aligned_duration_s"] == pytest.approx(
        (rows[18]["t"] - rows[0]["t"]) + (rows[-1]["t"] - rows[20]["t"])
    )


def test_whole_alignment_is_not_structurally_broken_by_sampling_density():
    outputs = []
    for spacing in [10, 1]:
        rows = points(601 // spacing)
        for i, row in enumerate(rows):
            row.update(t=i * spacing, s_m=i * spacing * 10)
        result = score_whole_with_shared_delay(
            rows, trip(), [supported_window(rows, 0)]
        )
        assert result["status"] == "EVIDENCE"
        assert result["schedule_aligned_point_count"] == len(rows)
        assert (
            result["schedule_aligned_point_ratio"]
            == result["schedule_aligned_duration_ratio"]
            == 1
        )
        assert result["fragments"][0]["dtw_scored_points"] == len(rows)
        outputs.append(result["score"])
    assert outputs[0] == pytest.approx(outputs[1], abs=0.1)


def test_whole_process_does_not_fill_rows_without_successful_window_support():
    rows = points(40)
    result = score_whole_with_shared_delay(
        rows, trip(), [supported_window(rows[:5], 0)]
    )
    assert result["schedule_aligned_point_count"] == 5
    assert result["schedule_aligned_point_ratio"] == pytest.approx(5 / 40)
    assert all(
        r["delay_s"] is None and not r["schedule_aligned"]
        for r in result["delay_process"]
        if r["source_id"] not in {observation_key(p) for p in rows[:5]}
    )


def test_viterbi_hard_constraint_rejects_cheaper_backward_corrected_clock():
    windows = [
        {"window_id": "a", "anchor_t": 0, "delay_states": [{"delay_s": 0, "cost": 0}]},
        {
            "window_id": "b",
            "anchor_t": 10,
            "delay_states": [{"delay_s": 20, "cost": 0}, {"delay_s": 0, "cost": 1}],
        },
        {"window_id": "c", "anchor_t": 20, "delay_states": [{"delay_s": 0, "cost": 0}]},
    ]
    result = smooth_delay_viterbi(windows)
    assert result == {"a": 0, "b": 0, "c": 0}


def test_unreachable_delay_path_is_explicitly_rejected_not_silently_reset():
    windows = [
        {
            "window_id": str(i),
            "anchor_t": i * 10,
            "delay_states": [{"delay_s": delay, "cost": 0}],
        }
        for i, delay in enumerate([0, 20, 30])
    ]
    diagnostics = {}
    result = smooth_delay_viterbi(windows, diagnostics=diagnostics)
    assert "1" not in result
    assert diagnostics["1"]["status"] == "UNREACHABLE_MONOTONE_TRANSITION"
    assert diagnostics["0"]["block_id"] != diagnostics["2"]["block_id"]
    windows[1]["delay_fragment_ids"] = ["different-fragment"]
    result = smooth_delay_viterbi(windows)
    assert result["1"] == 20  # independent source fragment, not a cross-gap path


def test_isotonic_corrected_clock_is_weighted_and_preserves_epoch_precision():
    assert weighted_isotonic([1, 3, 2], [1, 1, 3]) == [1, 2.25, 2.25]
    epoch = 1_699_228_800
    assert (
        weighted_isotonic([epoch + 3, epoch + 1, epoch + 2], [1, 1, 1])
        == [epoch + 2] * 3
    )


def test_adversarial_whole_delay_anchors_do_not_reverse_or_drop_original_points():
    epoch = 1_699_228_800
    rows = points(25)
    for row in rows:
        row["t"] += epoch
    original = copy.deepcopy(rows)
    local = [
        supported_window(rows[:13], 0, "left"),
        supported_window(rows[8:], 240, "right"),
    ]
    result = score_whole_with_shared_delay(rows, trip(offset=epoch), local)
    assert rows == original
    process = result["delay_process"]
    assert len(process) == len(rows)
    assert {p["source_id"] for p in process} == {observation_key(r) for r in rows}
    assert all(p["delay_s"] is not None for p in process)
    assert all(
        b["corrected_t"] >= a["corrected_t"]
        for a, b in zip(process, process[1:], strict=False)
    )
    assert result["delay_constraint_adjusted_point_count"] > 0
    assert result["delay_constraint_max_adjustment_s"] > 0
    assert result["delay_fit_fragments"][0]["status"] == "MONOTONE_CONSTRAINT_ADJUSTED"
    assert not result["delay_fit_fragments"][0]["fallback"]
    assert any(abs(p["raw_delay_s"] - p["delay_s"]) > 0 for p in process)


def test_delay_fit_keeps_unsupported_run_separate_without_cross_gap_knots():
    rows = points(20)
    local = [
        supported_window(rows[:5], 0, "left"),
        supported_window(rows[10:], 500, "right"),
    ]
    result = score_whole_with_shared_delay(rows, trip(), local)
    process = {p["source_id"]: p for p in result["delay_process"]}
    assert len(result["delay_fit_fragments"]) == 2
    assert process["avl|a4"]["fragment_id"] != process["avl|a10"]["fragment_id"]
    assert process["avl|a4"]["delay_s"] == 0
    assert process["avl|a10"]["delay_s"] == 500
    assert all(process[f"avl|a{i}"]["delay_s"] is None for i in range(5, 10))


def test_reported_best_and_margin_use_eligible_display_set_not_global_minimum(
    monkeypatch,
):
    candidates = []
    for name, cost, coverage, delay in [
        ("diagnostic", 1, 0.1, 900),
        ("chosen", 2, 1, 90),
        ("next", 2.5, 1, -300),
    ]:
        candidate = dict(
            trip(name),
            candidate_id=name,
            score=cost,
            support_score=math.exp(-cost),
            coverage=coverage,
            estimated_delay_s=delay,
            schedule_supported_window_weight_ratio=1,
            continuous_support_windows=3,
            schedule_aligned_point_ratio=coverage,
            schedule_aligned_duration_ratio=coverage,
            whole_alignment={
                "status": "NO_EVIDENCE" if coverage < 0.35 else "EVIDENCE"
            },
        )
        candidates.append(candidate)
    monkeypatch.setattr(
        "tools.merge_review.gtfs_matching.integration.retrieve_candidates",
        lambda *args, **kwargs: {"trips": [trip(c["trip_id"]) for c in candidates]},
    )
    monkeypatch.setattr(
        "tools.merge_review.gtfs_matching.integration.evaluate_candidate",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "tools.merge_review.gtfs_matching.integration.aggregate_candidate",
        lambda local, observations, apc, t, config: copy.deepcopy(
            next(c for c in candidates if c["trip_id"] == t["trip_id"])
        ),
    )
    result = match_candidate_segment(
        {"points": points()}, [], [], [trip()], {"aggregation_ambiguity_margin": 0.01}
    )
    assert result["status"] == "MATCHED"
    assert (
        result["candidates"][0]["candidate_id"]
        == result["reported_candidate_id"]
        == result["decision_candidate_id"]
        == "chosen"
    )
    assert result["global_diagnostic_best"]["candidate_id"] == "diagnostic"
    assert result["estimated_delay"] == 90
    assert result["trip_confidence"]["support_score"] == pytest.approx(math.exp(-2))
    assert result["top1_top2_margin"] == pytest.approx(math.exp(-2) - math.exp(-2.5))
    assert result["top1_top2_margin_candidate_ids"] == ["chosen", "next"]
    assert "WHOLE_ALIGNMENT_OR_SCHEDULE_COVERAGE_NOT_VALIDATED" not in result["reasons"]
