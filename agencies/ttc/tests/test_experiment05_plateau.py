"""Synthetic physical trajectories: no historical scheduled winner fixtures."""

import copy
import math

import pytest

from tools.experiment05.config import load_config
from tools.experiment05.plateau import assess_trip_extent
from tools.merge_review.gtfs_matching.canonical import R

T = 1_700_000_040.0  # Whole UTC minute; neither a routing nor matching rule.


def coordinate(s):
    return {"lat": 43.65 + math.degrees(s / R), "lon": -79.4}


def canonical(*, short_only=False, reverse=False):
    ref = {
        "reference_id": "north-ref",
        "direction_id": "N",
        "length_m": 4000,
        "points": [dict(coordinate(s), s_m=s) for s in (0, 1200, 4000)],
        "stop_positions": [
            {"stop_id": f"stop-{s}", "stop_code": f"code-{s}", "s_m": s}
            for s in (0, 600, 1200, 1800, 2400, 3000, 4000)
        ],
    }
    trips = []
    for index, (limit, offset) in enumerate(
        [(1200, 0)] + ([] if short_only else [(4000, 0), (4000, 600), (4000, 1200)])
    ):
        stops = [s for s in (0, 600, 1200, 1800, 2400, 3000, 4000) if s <= limit]
        trips.append(
            {
                "trip_id": f"hypothesis-{index}",
                "trip_key": f"hypothesis-{index}",
                "reference_id": "north-ref",
                "direction_id": "N",
                "route_id": "generic",
                "feed_version": "test-feed",
                "service_date": "synthetic",
                "canonical_valid": True,
                "scheduled": [
                    {
                        "stop_id": f"stop-{s}",
                        "s_m": s,
                        "arrival_t": T + (s - 1200) / 5 + offset,
                        "departure_t": T + (s - 1200) / 5 + offset,
                    }
                    for s in stops
                ],
            }
        )
    references = [ref]
    if reverse:
        opposite = copy.deepcopy(ref)
        opposite.update(reference_id="south-ref", direction_id="S")
        opposite["points"] = [
            dict(coordinate(4000 - s), s_m=s) for s in (0, 2800, 4000)
        ]
        references.append(opposite)
    return {"references": references, "trips": trips}


def point(t, s, *, raw_s=None, status="ON_REFERENCE", speed=0):
    return {
        "rid": f"avl-{t}",
        "source": "avl",
        "t": T + t,
        **coordinate(s if raw_s is None else raw_s),
        "s_m": s,
        "reference_id": "north-ref",
        "direction_id": "N",
        "quality": 0.9,
        "projection_status": status,
        "speed_kph": speed,
        "all_references_checked": True,
        "projection_alternatives": [],
        "fragment_id": "original-fragment",
        "trajectory_included": status == "ON_REFERENCE",
    }


def rows(duration=1500, *, position=1200, speed=0):
    return [point(t, position + t * 5) for t in range(-240, 0, 30)] + [
        point(t, position, speed=speed) for t in range(0, duration + 1, 30)
    ]


def config(**changes):
    value, _ = load_config()
    value["plateau"].update(changes)
    return value


def assess(observations=None, *, apc=(), bundle=None, cfg=None):
    return assess_trip_extent(
        observations if observations is not None else rows(),
        list(apc),
        bundle if bundle is not None else canonical(),
        cfg if cfg is not None else config(),
    )


def runs(result):
    return [
        row for row in result["diagnostics"] if row["diagnostic_type"] == "PLATEAU_RUN"
    ]


def test_normal_progress_and_short_red_light_do_not_censor():
    normal = assess([point(t, 1200 + t * 5) for t in range(-120, 601, 30)])
    short = assess(rows(120))
    assert (
        normal["extent"]["platform_states"]
        == short["extent"]["platform_states"]
        == ["NORMAL"]
    )
    assert short["extent"]["effective_segment_end"] == T + 120
    assert not any(p["exclude_from_matching"] for p in short["points"])


def test_sustained_terminal_tail_is_backdated_and_keeps_one_avl_endpoint():
    original = rows()
    before = copy.deepcopy(original)
    result = assess(original)
    extent = result["extent"]
    assert original == before
    assert extent["raw_segment_end"] == T + 1500
    assert extent["effective_segment_end"] == extent["plateau_onset_time"] == T
    assert extent["censor_decision_time"] == T + 1200
    assert extent["censored_tail_duration_s"] == 1500
    assert extent["physical_trip_end_confirmed"] is False
    assert "TERMINAL_LAYOVER" in extent["platform_states"]
    assert "RIGHT_CENSORED_TAIL" in extent["platform_states"]
    tail = [p for p in result["points"] if p["t"] >= T]
    assert sum(p["trajectory_included"] for p in tail) == 1
    assert [p["rid"] for p in tail if not p["exclude_from_matching"]] == ["avl-0"]
    assert all(p["censored_tail"] for p in tail[1:])
    assert all(p["fragment_id"] == "original-fragment" for p in result["points"])
    counterfactual = runs(result)[0]["counterfactual"]
    assert counterfactual["eligible_candidate_count"] == 3
    assert not counterfactual["final_candidate_assignment_used"]
    assert not counterfactual["free_tail_delay_fitted"]
    assert all(
        c["counterfactual_advance_m"] >= 250 for c in counterfactual["candidates"]
    )


def test_long_midroute_stop_is_not_automatically_cut_even_without_recovery():
    result = assess(rows(position=2000))
    assert result["extent"]["censor_reason"] is None
    assert "LONG_MIDROUTE_STOP" in result["extent"]["platform_states"]
    assert len(result["points"]) == len(rows())


def test_invalid_short_turn_schedule_cannot_create_terminal_proof():
    bundle = canonical()
    bundle["trips"][0]["scheduled"][-1]["departure_t"] = T - 1000
    result = assess(bundle=bundle)
    assert result["extent"]["censor_reason"] is None
    assert "LONG_MIDROUTE_STOP" in result["extent"]["platform_states"]


def test_internal_long_stop_and_valid_recovery_remain_one_segment():
    observations = rows(1500, position=2000)
    observations += [point(t, 2000 + (t - 1500) * 5) for t in (1530, 1560, 1590, 1620)]
    result = assess(observations)
    assert "RECOVERED_AFTER_PLATEAU" in result["extent"]["platform_states"]
    assert result["extent"]["effective_segment_end"] == T + 1620
    assert not any(p["exclude_from_matching"] for p in result["points"])
    assert runs(result)[0]["recovery"]["progress_m"] >= 150


def test_frozen_coordinates_need_speed_contradiction_not_just_repeated_positions():
    frozen = assess(rows(speed=20))
    assert "FROZEN_TELEMETRY" in frozen["extent"]["platform_states"]
    stationary = assess(rows())
    assert "FROZEN_TELEMETRY" not in stationary["extent"]["platform_states"]
    assert runs(stationary)[0]["metrics"][
        "exact_coordinate_repeat_without_independent_freeze_proof"
    ]


def test_raw_moving_flat_progress_is_reference_failure_never_stationary():
    observations = [point(t, 1200, raw_s=1200 + 2 * t) for t in range(0, 1501, 30)]
    result = assess(observations)
    assert "REFERENCE_MISMATCH" in result["extent"]["platform_states"]
    assert result["extent"]["censor_reason"] is None
    assert all(p["trajectory_included"] for p in result["points"])


def test_all_off_reference_moving_points_remain_in_denominator_inventory():
    observations = [
        point(t, 1200, raw_s=1200 + 2 * t, status="OFF_REFERENCE")
        for t in range(0, 1501, 30)
    ]
    for p in observations:
        p["s_m"] = None
    result = assess(observations)
    assert "OFF_REFERENCE_MOVING" in result["extent"]["platform_states"]
    assert len(result["points"]) == len(observations)
    assert all(
        not p["exclude_from_matching"] and not p["censored_tail"]
        for p in result["points"]
    )
    assert all(not p["trajectory_included"] for p in result["points"])


def test_advancing_alternative_reference_prevents_censoring():
    bundle = canonical()
    other = copy.deepcopy(bundle["references"][0])
    other["reference_id"] = "alternative"
    bundle["references"].append(other)
    observations = [point(t, 1200, raw_s=1200 + t * 2) for t in range(0, 1201, 30)]
    for p in observations:
        p["projection_alternatives"] = [
            {
                "reference_id": "alternative",
                "projection_status": "ON_REFERENCE",
                "quality": 0.9,
                "s_m": 1200 + (p["t"] - T) * 2,
            }
        ]
    result = assess(observations, bundle=bundle)
    assert "REFERENCE_MISMATCH" in result["extent"]["platform_states"]
    assert runs(result)[0]["metrics"]["moving_reference_ids"] == ["alternative"]
    assert result["extent"]["censor_reason"] is None


def test_opposite_direction_recovery_at_terminal_blocks_tail_censor():
    observations = rows(1500)
    for p in observations:
        p["projection_alternatives"] = [
            {
                "reference_id": "south-ref",
                "s_m": 4000 - p["s_m"],
                "projection_status": "ON_REFERENCE" if p["t"] >= T else "OFF_REFERENCE",
                "quality": 0.9,
            }
        ]
    for t in (1530, 1560, 1590, 1620):
        p = point(t, 1200 - (t - 1500) * 5)
        p["projection_alternatives"] = [
            {
                "reference_id": "south-ref",
                "s_m": 4000 - p["s_m"],
                "projection_status": "ON_REFERENCE",
                "quality": 0.9,
            }
        ]
        observations.append(p)
    result = assess(observations, bundle=canonical(reverse=True))
    assert result["extent"]["censor_reason"] is None
    assert "RECOVERED_AFTER_PLATEAU" in result["extent"]["platform_states"]
    assert runs(result)[0]["recovery"]["reference_id"] == "south-ref"


@pytest.mark.parametrize(
    "kind", ["long-gap", "missing-right-boundary", "references-unchecked"]
)
def test_missing_observational_proof_abstains(kind):
    observations = rows()
    if kind == "long-gap":
        observations = [p for p in observations if not T - 100 <= p["t"] <= T + 1000]
    elif kind == "missing-right-boundary":
        observations[-1]["right_boundary_missing"] = True
    else:
        observations[-1]["all_references_checked"] = False
    result = assess(observations)
    assert result["extent"]["censor_reason"] is None
    assert not any(p["exclude_from_matching"] for p in result["points"])


def test_finished_candidates_cannot_be_extrapolated_past_their_end():
    result = assess(bundle=canonical(short_only=True))
    counterfactual = runs(result)[0]["counterfactual"]
    assert counterfactual["eligible_candidate_count"] == 0
    assert (
        counterfactual["excluded_counts"][
            "PATTERN_ALREADY_FINISHED_AT_ANCHOR_NOT_AN_ADVANCE_TEST"
        ]
        == 1
    )
    assert result["extent"]["censor_reason"] is None


def test_one_plausible_long_scheduled_dwell_blocks_unanimous_advance():
    bundle = canonical()
    slow = copy.deepcopy(bundle["trips"][1])
    slow.update(trip_id="scheduled-long-dwell", trip_key="scheduled-long-dwell")
    for p in slow["scheduled"]:
        if p["s_m"] >= 1200:
            p["departure_t"] += 5000
        if p["s_m"] > 1200:
            p["arrival_t"] += 5000
    bundle["trips"].append(slow)
    result = assess(bundle=bundle)
    assert result["extent"]["censor_reason"] is None
    cf = runs(result)[0]["counterfactual"]
    assert any(p["counterfactual_advance_m"] == 0 for p in cf["candidates"])


def test_apc_service_backdates_to_last_colocated_departure_and_keeps_interval():
    interval = {
        "rid": "apc-service",
        "arrival": T + 90,
        "departure": T + 120,
        "boarding": 3,
        "alighting": 1,
        "stop": "code-1200",
    }
    result = assess(apc=[interval])
    assert result["extent"]["effective_segment_end"] == T + 120
    assert result["extent"]["retained_apc_rids"] == ["apc-service"]
    assert result["extent"]["retained_endpoint_rid"] == "avl-120"
    assert result["extent"]["censor_decision_time"] == T + 1320
    assert sum(p["trajectory_included"] for p in result["points"] if p["t"] >= T) == 1
    assert interval["departure"] == T + 120


@pytest.mark.parametrize(
    "kind",
    [
        "ongoing",
        "unknown-counts",
        "elsewhere",
        "conflicting-real-coordinates",
        "ambiguous-time",
    ],
)
def test_service_evidence_blocks_censor_when_not_finished_or_unresolved(kind):
    interval = {
        "rid": "service",
        "arrival": T + 300,
        "departure": T + 330,
        "boarding": 2,
        "alighting": 1,
        "stop": "code-1200",
    }
    if kind == "ongoing":
        interval["departure"] = T + 1600
    elif kind == "unknown-counts":
        interval["boarding"] = None
    elif kind == "elsewhere":
        interval["stop"] = "code-3000"
    elif kind == "conflicting-real-coordinates":
        interval.update(coordinate(3000))
    else:
        interval["time_ambiguous"] = True
    result = assess(apc=[interval])
    assert result["extent"]["censor_reason"] is None


def test_atomic_windows_do_not_double_count_and_no_fake_apc_trajectory():
    result = assess()
    windows = [
        p
        for p in result["diagnostics"]
        if p["diagnostic_type"] == "PLATEAU_ATOMIC_WINDOW"
    ]
    assert sum(p["observation_count"] for p in windows) == len(result["points"])
    assert all(
        a["end_t"] <= b["start_t"] for a, b in zip(windows, windows[1:], strict=False)
    )
    assert all(p["source"] == "avl" for p in result["points"])


def test_all_numerical_thresholds_require_valid_explicit_configuration():
    cfg = config()
    del cfg["plateau"]["counterfactual_min_prior_points"]
    with pytest.raises(ValueError, match="explicit plateau threshold"):
        assess(cfg=cfg)
    with pytest.raises(ValueError, match="clock rate"):
        assess(cfg=config(counterfactual_delay_growth_s_per_s=1))
    assert assess([])["extent"]["effective_segment_end"] is None
