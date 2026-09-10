"""Independent 05 projection/attachment/scoring handoff contracts, not a batch E2E."""

import copy

import pytest

from tools.experiment05 import apc, candidates, projection
from tools.experiment05.config import load_config
from tools.merge_review.gtfs_matching.continuous.evidence import (
    coverage_metrics,
    identity,
    valid_projection,
)
from tools.merge_review.gtfs_matching.continuous.tests.test_continuous_pipeline import (
    points,
    schedule,
)
from tools.merge_review.tests.test_gtfs_projection import ping, reference


@pytest.fixture
def config():
    return load_config()[0]


@pytest.fixture
def scored_evidence():
    rows = points(step=30)
    for row in rows:
        row.update(
            t=row["t"] + 1200,
            vehicle_id="synthetic-v",
            vehicle="synthetic-v",
            trip_id_performed="performed",
            route_eligible=True,
            trajectory_included=True,
        )
    trips = [schedule("near", 1200), schedule("earlier", 600), schedule("later", 1800)]
    for trip in trips:
        trip.update(direction_id="0", canonical_valid=True)
        for stop in trip["scheduled"]:
            stop.update(
                arrival_seconds=stop["arrival_t"], departure_seconds=stop["departure_t"]
            )
    canonical = {
        "feed_version": "feed-A",
        "route_id": "route-A",
        "references": [
            {
                "reference_id": rows[0]["reference_id"],
                "family_id": "family-A",
                "direction_id": "0",
                "direction_label": "NORTH",
                "length_m": 3000,
                "canonical_shape_id": "synthetic-shape",
                "points": [],
            }
        ],
        "trips": trips,
    }
    segment = {
        "trip_id_performed": "performed",
        "service_date": "2019-05-03",
        "route_id": "route-A",
        "direction_id": "0",
        "gtfs_feed_version": "feed-A",
        "vehicle_id": "synthetic-v",
        "effective_segment_start": rows[0]["t"],
        "effective_segment_end": rows[-1]["t"],
    }
    return segment, rows, canonical


def test_projection_job_boundaries_are_numerical_not_performed_trip_ids(config):
    rows = []
    for i, (t, direction, eligible) in enumerate(
        [
            (0, "NORTH", True),
            (30, "NORTH", False),
            (60, "NORTH", True),
            (90, "SOUTH", True),
            (4000, "SOUTH", True),
        ]
    ):
        rows.append(
            {
                "rid": str(i),
                "source": "avl",
                "vehicle_id": "v",
                "t": t,
                "direction_label": direction,
                "route_eligible": eligible,
                "raw_payload": {"AvgOTP": 123},
                "audit_avg_otp": 123,
            }
        )
    rows.append({**rows[-1], "rid": "duplicate", "duplicate_of": "4"})
    before = copy.deepcopy(rows)
    jobs = projection.projection_jobs(rows, config)
    assert len(jobs) == 1 and jobs[0][0] == "v"
    selected = jobs[0][1]
    assert [p["rid"] for p in selected] == ["0", "2", "3", "4"]
    assert [p["scope_separator_before"] for p in selected] == [False, True, True, True]
    assert all(
        "trip_id_performed" not in p and "audit_avg_otp" not in p for p in selected
    )
    assert rows == before


def test_real_sequence_projection_retains_rejected_and_restores_full_inventory(config):
    rows = [
        dict(ping("good-a", 0, 43.65), source="avl", vehicle_id="v"),
        dict(ping("bad", 60, 43.66), source="avl", vehicle_id="v", lon=-79.5),
        dict(ping("good-b", 120, 43.67), source="avl", vehicle_id="v"),
    ]
    canonical = {"references": [reference()]}
    result = projection.project_vehicle(rows, canonical, config)
    assert {p["rid"] for p in result["points"]} == {p["rid"] for p in rows}
    failed = next(p for p in result["points"] if p["rid"] == "bad")
    assert failed["projection_status"] == "OFF_REFERENCE"
    assert failed["projection_distance_m"] > 100 and not failed["trajectory_included"]
    timeline = rows + [{"rid": "apc", "source": "apc", "t": 70}]
    combined = projection.combine_projection(timeline, [result])
    assert len(combined) == len(timeline)
    assert combined[-1]["projection_status"] == "PENDING_STOP_INTERVAL"
    assert combined[-1]["trajectory_included"] is False


def test_off_reference_denominator_and_confirmed_censored_tail_are_distinct(
    scored_evidence,
):
    segment, rows, _ = scored_evidence
    rows[5].update(
        projection_status="OFF_REFERENCE",
        trajectory_included=False,
        quality=0,
        s_m=None,
    )
    rows[-1]["censored_tail"] = True
    rows[-2]["exclude_from_matching"] = True
    duplicate = {
        **rows[0],
        "rid": "duplicate",
        "source_rid": "duplicate",
        "duplicate_of": rows[0]["rid"],
    }
    original = copy.deepcopy(rows)
    result = candidates._projection(segment, rows + [duplicate])
    included = result["points"]
    assert len(included) == len(rows) - 2
    assert rows[5]["rid"] in {p["rid"] for p in included}
    assert "duplicate" not in {p["rid"] for p in included}
    metrics = coverage_metrics(
        included, [identity(p) for p in included if valid_projection(p)]
    )
    assert metrics["projection"]["point_ratio"] == pytest.approx(18 / 19)
    assert 0 < metrics["projection"]["duration_ratio"] < 1
    assert metrics["schedule"]["point_ratio"] == 1
    assert rows == original


def test_real_four_mode_scores_fixed_independently_and_do_not_require_calibration(
    scored_evidence, config
):
    segment, rows, canonical = scored_evidence
    before = copy.deepcopy((segment, rows, canonical, config))
    result = candidates.rank_segment(
        segment, rows, canonical, config, ["global", "early", "middle", "late"]
    )
    assert {r["match_mode"] for r in result["mode_results"]} == {
        "global",
        "early",
        "middle",
        "late",
    }
    for mode in ("global", "early", "middle", "late"):
        ranking = [r for r in result["rankings"] if r["match_mode"] == mode]
        assert len(ranking) == 3
        assert [r["candidate_rank"] for r in ranking] == [1, 2, 3]
        assert [r["ordering_tuple"] for r in ranking] == sorted(
            r["ordering_tuple"] for r in ranking
        )
        assert all(not r["score_threshold_applied_to_assignment"] for r in ranking)
        assert all(r["hard_constraints_valid"] for r in ranking)
        assert all(
            "scheduled_trajectory" not in r and "delay_process" not in r
            for r in ranking
        )
        assert all(r["score_components"] and r["coverage_metrics"] for r in ranking)
    assert (segment, rows, canonical, config) == before
    assert all(r["candidate_ranking_fixed"] for r in result["mode_results"])


def test_actual_scoring_preserves_off_reference_coverage_loss(scored_evidence, config):
    segment, rows, canonical = scored_evidence
    rows[5].update(
        projection_status="OFF_REFERENCE",
        quality=0,
        trajectory_included=False,
        s_m=None,
    )
    result = candidates.rank_segment(segment, rows, canonical, config, ["global"])
    assert result["rankings"]
    for candidate in result["rankings"]:
        assert candidate["coverage_metrics"]["projection"][
            "point_ratio"
        ] == pytest.approx(20 / 21)


def test_masked_tail_and_unrelated_trip_do_not_change_actual_rankings(
    scored_evidence, config
):
    segment, rows, canonical = scored_evidence
    original = candidates.rank_segment(segment, rows, canonical, config, ["global"])
    extras = [
        {
            **rows[-1],
            "rid": "tail",
            "source_rid": "tail",
            "t": 99999,
            "s_m": 3000,
            "censored_tail": True,
        },
        {
            **rows[-1],
            "rid": "other",
            "source_rid": "other",
            "trip_id_performed": "other",
        },
    ]
    changed = candidates.rank_segment(
        segment, rows + extras, canonical, config, ["global"]
    )
    assert changed == original


def test_unsorted_upstream_candidate_order_is_rejected(
    scored_evidence, config, monkeypatch
):
    segment, rows, canonical = scored_evidence
    monkeypatch.setattr(candidates, "apply_decision_guard", lambda result, cfg: result)
    monkeypatch.setattr(
        candidates,
        "match_candidate_segment",
        lambda *a, **k: {
            "all_candidates": [
                {"score": 2, "candidate_id": "b"},
                {"score": 1, "candidate_id": "a"},
            ]
        },
    )
    with pytest.raises(ValueError, match="ordering contract"):
        candidates.rank_segment(segment, rows, canonical, config, ["global"])


def test_apc_crossing_confirmed_censor_end_keeps_raw_interval_but_clips_scoring(
    config, monkeypatch
):
    segment = {
        "trip_id_performed": "performed",
        "vehicle_id": "v",
        "service_date": "2024-04-01",
        "effective_segment_start": 0,
        "effective_segment_end": 100,
        "censor_reason": "RIGHT_CENSORED",
    }
    point = {
        "source": "avl",
        "rid": "a",
        "vehicle_id": "v",
        "trip_id_performed": "performed",
        "trajectory_included": True,
        "reference_id": "ref",
        "t": 90,
        "s_m": 50,
        "projected_direction_label": "NORTH",
    }
    event = {
        "source": "apc",
        "rid": "p",
        "vehicle_id": "v",
        "route_eligible": True,
        "arrival": 90,
        "departure": 130,
        "t": 90,
        "direction_label": "NORTH",
        "boarding": 1,
        "alighting": 0,
        "grain_uncertain": False,
        "canonical_stop_intervals": [
            {
                "reference_id": "ref",
                "stop_id": "stop-a",
                "s_min_m": 50,
                "ambiguous": False,
            }
        ],
        "projection_alternatives": [
            {"reference_id": "ref", "projection_status": "STOP_INTERVAL_ONLY"}
        ],
    }
    monkeypatch.setattr(
        apc, "project_apc_intervals", lambda *args: [copy.deepcopy(event)]
    )
    updated, visits, audit = apc.attach_apc([segment], [point, event], {}, config)
    attached = next(row for row in updated if row["rid"] == "p")
    assert attached["apc_attachment_reason"] == "ATTACHED"
    assert attached["arrival"] == 90 and attached["departure"] == 130
    assert attached.get("interval_end") == 100
    assert attached["trajectory_included"] is False
    assert visits[0]["departure_t"] == 130
    assert not visits[0]["passenger_event_semantics_valid"]
    original = copy.deepcopy(updated)
    evidence = candidates._projection(segment, updated)["apc_evidence"][0]
    assert evidence["arrival"] == 90 and evidence["departure"] == 100
    assert evidence["interval_start"] == 90 and evidence["interval_end"] == 100
    assert updated == original
