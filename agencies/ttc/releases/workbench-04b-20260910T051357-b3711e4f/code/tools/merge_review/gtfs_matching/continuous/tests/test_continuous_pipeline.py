"""Synthetic end-to-end regressions, not operational identity ground truth."""

import copy
import json
from datetime import timedelta

import pytest

from tools.merge_review.gtfs_matching import scoring
from tools.merge_review.gtfs_matching.canonical import build_references
from tools.merge_review.gtfs_matching.continuous import SCORER
from tools.merge_review.gtfs_matching.continuous.clock import (
    gtfs_instant,
    observation_instants,
)
from tools.merge_review.gtfs_matching.continuous.config import get_config
from tools.merge_review.gtfs_matching.continuous.integration import (
    match_candidate_segment as continuous_match,
)
from tools.merge_review.gtfs_matching.continuous.serialization import validate_result
from tools.merge_review.gtfs_matching.integration import match_candidate_segment
from tools.merge_review.gtfs_matching.multimode import validate_modes
from tools.merge_review.gtfs_matching.partial import evaluate_matching_modes
from tools.merge_review.gtfs_matching.projection import project_sequence
from tools.merge_review.tests.test_gtfs_canonical import bundle as shape_bundle
from tools.merge_review.tests.test_gtfs_projection import ping, reference


def schedule(identifier="scheduled-A", offset=0, ref="ref:feed-A:route-A:north"):
    return {
        "trip_id": identifier,
        "service_date": "2019-05-03",
        "feed_version": "feed-A",
        "route_id": "route-A",
        "reference_id": ref,
        "family_id": "family-A",
        "pattern_id": "pattern-A",
        "direction_id": "northbound",
        "scheduled": [
            {
                "stop_id": f"stop-{i}",
                "s_m": i * 600,
                "t": offset + i * 120,
                "arrival_t": offset + i * 120,
                "departure_t": offset + i * 120,
            }
            for i in range(6)
        ],
    }


def points(step=15, delay=30):
    return [
        {
            "source": "avl",
            "rid": f"observation-{i}",
            "source_rid": f"observation-{i}",
            "t": time + delay,
            "s_m": time * 5,
            "lat": 43.65,
            "lon": -79.4,
            "reference_id": "ref:feed-A:route-A:north",
            "family_id": "family-A",
            "fragment_id": "service-fragment",
            "projection_status": "ON_REFERENCE",
            "quality": 0.8,
            "lateral_m": 5,
            "direction_fallback": False,
        }
        for i, time in enumerate(range(0, 601, step))
    ]


def apc(stop=2, rid="apc-0"):
    return {
        "source": "apc",
        "rid": rid,
        "source_rid": rid,
        "t": stop * 120 + 25,
        "interval_start": stop * 120 + 25,
        "interval_end": stop * 120 + 40,
        "stop_id": f"stop-{stop}",
        "s_m": stop * 600,
        "reference_id": "ref:feed-A:route-A:north",
        "projection_status": "STOP_INTERVAL_ONLY",
        "boarding": 3,
        "alighting": 2,
        "quality": 0.6,
        "lat": None,
        "lon": None,
    }


def run(rows=None, trips=None, apc_rows=None, config=None, **kwargs):
    rows = points() if rows is None else rows
    trips = (
        [schedule(), schedule("scheduled-previous", -600)] if trips is None else trips
    )
    return match_candidate_segment(
        {"points": rows},
        apc_rows or [],
        [],
        trips,
        {"continuous_matching_enabled": True, **(config or {})},
        **kwargs,
    )


def signature(result):
    return {
        "status": result["status"],
        "evidence_status": result["evidence_status"],
        "candidates": [
            (
                c["candidate_id"],
                c["score"],
                c["score_components"],
                c["coverage_metrics"],
                c["estimated_delay_s"],
            )
            for c in result["all_candidates"]
        ],
    }


def test_feature_flag_routes_to_continuous_without_calling_legacy_dtw(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Fixed-grid DTW must not execute in continuous mode")

    monkeypatch.setattr(scoring, "constrained_dtw", forbidden)
    result = run()
    assert result["scorer"] == SCORER
    assert result["diagnostics"]["production_dtw_used"] is False
    assert validate_result(result)["status"] == "PASS"


def test_legacy_is_still_default_and_continuous_is_explicit(monkeypatch):
    import tools.merge_review.gtfs_matching.continuous.integration as integration

    def forbidden(*args, **kwargs):
        raise AssertionError("Legacy flag must not call the continuous scorer")

    monkeypatch.setattr(integration, "match_candidate_segment", forbidden)
    result = match_candidate_segment({"points": []}, [], [], [], {})
    assert result.get("scorer") != SCORER
    assert get_config()["continuous_matching_enabled"] is False


def test_no_upstream_schedule_identity_or_otp_feature_leakage():
    original_rows, original_apc = points(), [apc()]
    dirty_rows = [
        dict(r, AvgOTP=9999, delay_flag=True, TripID="vendor-favourite")
        for r in original_rows
    ]
    dirty_apc = [
        dict(
            r,
            AvgOTP=-9999,
            vendor_trip_id="vendor-favourite",
            assigned_schedule_id="some-other-schedule",
        )
        for r in original_apc
    ]
    before = copy.deepcopy((dirty_rows, dirty_apc))
    baseline = run(original_rows, apc_rows=original_apc)
    contaminated = run(dirty_rows, apc_rows=dirty_apc)
    assert signature(baseline) == signature(contaminated)
    assert (dirty_rows, dirty_apc) == before
    assert all(
        "AvgOTP" not in r
        for c in contaminated["all_candidates"]
        for r in c["apc_support"]["rows"]
    )


def test_route_service_date_and_feed_reference_namespaces():
    trips = [
        schedule(),
        dict(schedule("foreign-route"), route_id="route-B"),
        dict(
            schedule("foreign-feed", ref="ref:feed-B:route-A:north"),
            feed_version="feed-B",
        ),
        dict(schedule("previous-service-day"), service_date="2019-05-02"),
    ]
    scoped = run(trips=trips, route_id="route-A", service_date="2019-05-03")
    assert {c["trip_id"] for c in scoped["all_candidates"]} == {"scheduled-A"}
    days = run(trips=trips, route_id="route-A")
    assert {c["trip_id"] for c in days["all_candidates"]} == {
        "scheduled-A",
        "previous-service-day",
    }


def test_strong_direction_prior_is_not_cancelled_by_normalized_quality():
    rows = points()
    opposite = "ref:feed-A:route-A:south"
    for row in rows:
        row["projection_alternatives"] = [
            {
                "reference_id": opposite,
                "s_m": row["s_m"],
                "quality": 0.48,
                "projection_status": "ON_REFERENCE",
                "direction_fallback": True,
                "lateral_m": row["lateral_m"],
                "fragment_id": "opposite-fragment",
            }
        ]
    result = run(rows, [schedule(), schedule("opposite", ref=opposite)])
    costs = {c["trip_id"]: c for c in result["all_candidates"]}
    assert (
        costs["opposite"]["score_components"]["pattern_progress_cost"]
        > costs["scheduled-A"]["score_components"]["pattern_progress_cost"]
    )
    assert result["all_candidates"][0]["trip_id"] == "scheduled-A"


def test_two_second_window_is_no_evidence_not_no_candidate():
    rows = points()[:3]
    for i, row in enumerate(rows):
        row.update(t=i, s_m=i * 25)
    result = run(rows)
    assert result["evidence_status"] == "NO_EVIDENCE"
    assert result["all_candidates"] == []
    assert all(w["status"] == "NO_EVIDENCE" for w in result["retrieval"]["windows"])


def test_adequate_but_outside_schedule_progress_is_no_candidate():
    rows = [dict(row, s_m=row["s_m"] + 10000) for row in points()]
    result = run(rows)
    assert result["evidence_status"] == "NO_CANDIDATE"
    assert result["all_candidates"] == []


def test_three_coverage_equations_and_isolated_failure_do_not_clear_fragment():
    rows = points()
    rows[10].update(s_m=None, projection_status="OFF_REFERENCE", quality=0)
    result = run(rows)
    for candidate in result["all_candidates"]:
        metrics = candidate["coverage_metrics"]
        for unit in ("point_ratio", "interval_ratio", "duration_ratio"):
            assert metrics["projection"][unit] * metrics["schedule"][
                unit
            ] == pytest.approx(metrics["end_to_end"][unit])
        assert metrics["projection"]["point_count"] == len(rows) - 1
        assert metrics["schedule"]["point_ratio"] > 0.9
        process = candidate["whole_alignment"]["delay_process"]
        assert len({r["fragment_id"] for r in process}) == 1
        assert "avl|observation-10" not in {r["source_id"] for r in process}


@pytest.mark.parametrize("step", [5, 10, 15, 20])
def test_obsolete_gtfs_grid_cannot_change_ranking_status_or_coverage(step):
    baseline = run(config={"score_reference_step_s": 10})
    variant = run(config={"score_reference_step_s": step})
    assert signature(baseline) == signature(variant)


@pytest.mark.parametrize("step", [5, 10, 20, 30])
def test_avl_sampling_density_preserves_occurrence_ranking_and_coverage(step):
    baseline, changed = run(), run(points(step=step))
    assert changed["status"] == baseline["status"]
    assert [c["trip_id"] for c in changed["all_candidates"]] == [
        c["trip_id"] for c in baseline["all_candidates"]
    ]
    for a, b in zip(baseline["all_candidates"], changed["all_candidates"], strict=True):
        assert a["score"] == pytest.approx(b["score"], abs=0.02)
        for key in ("projection", "schedule", "end_to_end"):
            assert (
                a["coverage_metrics"][key]["point_ratio"]
                == b["coverage_metrics"][key]["point_ratio"]
            )
            assert (
                a["coverage_metrics"][key]["duration_ratio"]
                == b["coverage_metrics"][key]["duration_ratio"]
            )


def test_all_four_modes_keep_scope_and_continuous_score_components():
    projection = {"points": points(), "apc_evidence": [apc()]}
    before = copy.deepcopy(projection)
    canonical = {
        "references": [],
        "trips": [schedule(), schedule("older", -600)],
        "route_id": "route-A",
    }
    config = {"continuous_matching_enabled": True}
    modes = evaluate_matching_modes(projection, canonical, config)
    assert set(modes) == {"global", "early", "middle", "late"}
    validate_modes(projection, modes["global"]["result"], modes, config)
    assert projection == before
    for entry in modes.values():
        result = entry["result"]
        assert result["scorer"] == SCORER
        assert result["all_candidates"]
        validate_result(result)
        permitted = set(entry["selection"]["selected_avl_ids"])
        assert all(
            r["source_id"] in permitted
            for candidate in result["all_candidates"]
            for r in candidate["whole_alignment"]["delay_process"]
        )


def test_loop_repeated_stop_ids_and_retracing_fail_closed():
    loop = schedule()
    loop["scheduled"][-1]["stop_id"] = loop["scheduled"][0]["stop_id"]
    assert run(trips=[loop])["all_candidates"]
    invalid = copy.deepcopy(loop)
    invalid["scheduled"][3]["s_m"] = 20
    result = run(trips=[invalid])
    assert result["all_candidates"] == []
    assert result["evidence_status"] == "NO_CANDIDATE"


def test_canonical_projection_to_continuous_match_preserves_raw_evidence():
    canonical = build_references(shape_bundle())
    rows = [ping(f"p-{i}", 100 + i * 20, 43.65 + i * 0.002) for i in range(6)]
    before = copy.deepcopy(rows)
    projection = project_sequence(rows, canonical["references"])
    result = continuous_match(
        projection, [], canonical["references"], canonical["trips"], {}
    )
    assert rows == before
    assert result["all_candidates"]
    assert result["all_candidates"][0]["whole_alignment"]["dtw_used"] is False
    assert (
        result["all_candidates"][0]["coverage_metrics"]["projection"]["point_ratio"]
        == 1
    )


def test_off_reference_run_separator_survives_upstream_projection_api():
    raw = [ping(f"p{i}", i * 20, 43.65 + i * 0.0005) for i in range(12)]
    for row in raw[4:8]:
        row["lon"] = -80
    projection = project_sequence(raw, [reference()])
    assert any(
        run["classification"] != "GPS_OUTLIER"
        for run in projection["off_reference_runs"]
    )
    gtfs = schedule(ref="n")
    result = continuous_match(projection, [], [reference()], [gtfs], {})
    assert result["all_candidates"]
    for candidate in result["all_candidates"]:
        process = candidate["whole_alignment"]["delay_process"]
        assert len({r["fragment_id"] for r in process}) >= 2
        assert candidate["coverage_metrics"]["projection"]["point_count"] <= 8


def test_canonical_collinear_shape_vertex_insertion_does_not_change_result():
    original = shape_bundle()
    changed = copy.deepcopy(original)
    for shape in changed["shapes"]:
        first, last = shape["points"]
        shape["points"].insert(
            1,
            {
                "lat": (first["lat"] + last["lat"]) / 2,
                "lon": (first["lon"] + last["lon"]) / 2,
            },
        )
    raw = [ping(f"p{i}", 100 + i * 20, 43.65 + i * 0.002) for i in range(6)]
    results = []
    for source in (original, changed):
        canonical = build_references(source)
        projected = project_sequence(raw, canonical["references"])
        results.append(
            continuous_match(
                projected, [], canonical["references"], canonical["trips"], {}
            )
        )
    assert results[0]["status"] == results[1]["status"]
    assert [c["trip_id"] for c in results[0]["all_candidates"]] == [
        c["trip_id"] for c in results[1]["all_candidates"]
    ]
    assert [c["score"] for c in results[0]["all_candidates"]] == pytest.approx(
        [c["score"] for c in results[1]["all_candidates"]], abs=1e-9
    )


def test_dst_ambiguous_nonexistent_and_gtfs_noon_reference_clocks():
    zone = "America/Toronto"
    fall = observation_instants("2024-11-03T01:30:00", zone)
    assert fall["status"] == "AMBIGUOUS_LOCAL_TIME"
    assert fall["instants"][1] - fall["instants"][0] == timedelta(hours=1)
    assert observation_instants("2024-11-03T01:30:00", zone, fold=1)["instants"] == [
        fall["instants"][1]
    ]
    assert (
        observation_instants("2024-03-10T02:30:00", zone)["status"]
        == "NONEXISTENT_LOCAL_TIME"
    )
    beginning = gtfs_instant("2024-03-10", "00:00:00", zone)
    after_midnight = gtfs_instant("2024-03-10", "25:30:00", zone)
    assert after_midnight - beginning == timedelta(hours=25, minutes=30)


def test_no_calibrator_never_fabricates_candidate_probability():
    result = run()
    assert result["status"] == "PATTERN_ONLY"
    assert result["candidates"] == []
    assert all(c["probability"] is None for c in result["all_candidates"])
    assert "NO_CALIBRATED_SCHEDULED_OCCURRENCE_CREDIBLE_SET" in result["reasons"]
    json.dumps(result, allow_nan=False)


def test_result_json_round_trip_is_equal_including_passage_intervals():
    result = run(apc_rows=[apc()])
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    for candidate in result["all_candidates"]:
        assert all(
            isinstance(row["scheduled_passage_interval"], list)
            for row in candidate["whole_alignment"]["delay_process"]
        )
    assert result["diagnostics"]["serialization_validation"]["status"] == "PASS"


def test_all_four_modes_json_round_trip_preserves_structural_equality():
    projection = {"points": points(), "apc_evidence": [apc()]}
    canonical = {
        "references": [],
        "trips": [schedule(), schedule("older", -600)],
        "route_id": "route-A",
    }
    modes = evaluate_matching_modes(
        projection, canonical, {"continuous_matching_enabled": True}
    )
    assert json.loads(json.dumps(modes, allow_nan=False)) == modes


@pytest.mark.parametrize(
    "config",
    [
        {"continuous_boundary_min_windows": 0},
        {"continuous_event_windows_enabled": True, "continuous_event_radius_s": 0},
    ],
)
def test_zero_boundary_count_or_enabled_event_duration_is_rejected(config):
    with pytest.raises(ValueError):
        get_config(config)


def test_zero_event_radius_is_harmless_when_event_windows_are_disabled():
    config = get_config(
        {"continuous_event_windows_enabled": False, "continuous_event_radius_s": 0}
    )
    result = run(config=config)
    assert result["windows"]["event_windows"] == []


def test_east_west_canonical_labels_require_the_new_mode_flag():
    east = [{"lat": 43.65, "lon": -79.42}, {"lat": 43.65, "lon": -79.40}]
    source = shape_bundle(
        [("east", "increasing", east), ("west", "decreasing", list(reversed(east)))]
    )
    before = copy.deepcopy(source)
    legacy = build_references(source, {"continuous_matching_enabled": False})
    continuous = build_references(source, {"continuous_matching_enabled": True})
    assert {r["direction_label"] for r in legacy["references"]} == {None}
    assert {r["direction_label"] for r in continuous["references"]} == {"EAST", "WEST"}
    assert source == before
    raw = [
        dict(
            ping(f"east-{i}", 100 + i * 20, 43.65),
            lon=-79.42 + i * 0.004,
            direction_label="EAST",
            heading=90,
        )
        for i in range(6)
    ]
    projected = project_sequence(
        raw, continuous["references"], {"continuous_matching_enabled": True}
    )
    assert all(row["direction_label"] == "EAST" for row in projected["points"])
    assert all(not row["direction_fallback"] for row in projected["points"])
    matched = continuous_match(
        projected, [], continuous["references"], continuous["trips"], {}
    )
    assert matched["all_candidates"]
    assert matched["all_candidates"][0]["direction_label"] == "EAST"
    assert (
        matched["all_candidates"][0]["coverage_metrics"]["projection"]["point_ratio"]
        == 1
    )
