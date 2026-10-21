"""Constructed contracts for observation evidence, not accuracy ground truth.

These tests deliberately keep trip/chunk ownership separate from local GPS
support. Fixtures are synthetic and do not use historical human labels.
"""

from __future__ import annotations

import copy
import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from experiments.apc_avl_direct_evidence.matcher import (
    build_index,
    match_direct,
    prepare_candidates,
)

BASE = datetime(2024, 1, 1, 13)
METERS_PER_LONGITUDE_DEGREE = 6371008.8 * math.pi / 180 * math.cos(math.radians(43.7))


def ping(rid: str, second: float, meters: float = 0, **extra) -> dict:
    return {
        "rid": rid,
        "vehicle": "synthetic-42",
        "time": BASE + timedelta(seconds=second),
        "lat": 43.7,
        "lon": -79.4 + meters / METERS_PER_LONGITUDE_DEGREE,
        "route": "10",
        "run": "R",
        "destination": "E",
        "source_trip": "source-label",
        "invalid": False,
        "time_ambiguous": False,
        "time_nonexistent": False,
        **extra,
    }


def event(rid: str, second: float, meters: float = 0, **extra) -> dict:
    arrival = BASE + timedelta(seconds=second)
    return {
        "rid": rid,
        "vehicle": "synthetic-42",
        "arrival": arrival,
        "departure": arrival,
        "lat": 43.7,
        "lon": -79.4 + meters / METERS_PER_LONGITUDE_DEGREE,
        "route": "10",
        "direction": "E",
        "branch": "A",
        "stop": rid,
        "boarding": 2,
        "alighting": 1,
        "arrival_load": 10,
        "departure_load": 11,
        "avg_sample": 1,
        "no_trips": 1,
        "grain_uncertain": False,
        "invalid": False,
        "time_ambiguous": False,
        "time_nonexistent": False,
        **extra,
    }


def segmentation(avl: list[dict], mapping: dict | None = None) -> dict:
    mapping = mapping if mapping is not None else {p["rid"]: "chunk" for p in avl}
    groups: dict[str, list[datetime]] = {}
    for row in avl:
        owner = mapping.get(row["rid"])
        if owner is not None and row.get("time") is not None:
            groups.setdefault(owner, []).append(row["time"])
    return {
        "ping_segment": dict(mapping),
        "segments": [
            {"segment_id": owner, "start": min(times), "end": max(times)}
            for owner, times in sorted(groups.items())
        ],
    }


def run(avl, apc, *, mapping=None, config=None, prepared=False):
    index = build_index(avl, config)
    kwargs = {"config": config}
    if prepared:
        kwargs["prepared"] = prepare_candidates(apc, index, config)
    return match_direct(apc, index, segmentation(avl, mapping), **kwargs)


def semantic_signature(row):
    """Ignore display source IDs while retaining physical evidence identity."""
    fields = (
        "status",
        "support_level",
        "episode_count",
        "candidate_count",
        "score",
        "margin",
        "route_state",
        "route_conflict",
        "clock_corrected",
    )
    witnesses = sorted(
        (
            w["kind"],
            tuple(sorted(w["measurement_keys"])),
            w["support_time"],
            w["target"],
            w["target_time"],
            w["gap_s"],
            w["distance_m"],
            w["bracket_gap_s"],
            tuple(sorted(w["route_labels"])),
        )
        for w in row["witnesses"]
    )
    return {"decision": {k: row[k] for k in fields}, "witnesses": witnesses}


def test_exact_observation_is_instant_evidence_not_a_certified_trip():
    avl = [ping("g0", 0), ping("g1", 10), ping("g2", 20)]
    result = run(avl, [event("a", 10)])[0]
    assert result["status"] == "direct"
    assert result["support_level"] == "instant"
    assert result["witnesses"]
    assert result["clock_corrected"] is False
    assert not result.get("trip_id_performed")
    assert not result.get("actual_trip_start")
    assert not result.get("actual_trip_end")


def test_two_interval_endpoints_in_same_episode_are_bounded_evidence():
    avl = [ping(f"g{i}", i * 10) for i in range(7)]
    result = run(avl, [event("a", 0, departure=BASE + timedelta(seconds=60))])[0]
    assert result["status"] == "direct"
    assert result["support_level"] == "bounded_interval"
    assert result["episode_count"] == 1


@pytest.mark.parametrize(
    ("arrival", "departure", "level"),
    [
        (-120, 20, "departure_only"),
        (0, 140, "arrival_only"),
        (-120, 140, "interior_only"),
    ],
)
def test_partial_coverage_does_not_certify_entire_apc_interval(
    arrival, departure, level
):
    avl = [ping("g0", 0), ping("g1", 10), ping("g2", 20)]
    apc = [event("a", arrival, departure=BASE + timedelta(seconds=departure))]
    result = run(avl, apc)[0]
    assert result["status"] == "partial"
    assert result["support_level"] == level
    assert result["witnesses"]


@pytest.mark.parametrize("second", [-10, 30])
def test_close_ping_outside_observed_coverage_is_not_direct(second):
    result = run([ping("g0", 0), ping("g1", 20)], [event("a", second)])[0]
    assert result["status"] not in {"direct", "partial"}
    assert result["support_level"] is None


def test_interior_excursion_prevents_whole_interval_confirmation():
    avl = [ping("g0", 0), ping("g1", 300, 1600), ping("g2", 600)]
    apc = [event("a", 0, departure=BASE + timedelta(seconds=600))]
    result = run(avl, apc)[0]
    assert result["status"] != "direct"
    assert result["support_level"] != "bounded_interval"


def test_two_visits_inside_same_trip_chunk_remain_competing_episodes():
    avl = [
        ping("g0", 0),
        ping("g1", 10),
        ping("g2", 30, 300),
        ping("g3", 50, 300),
        ping("g4", 70),
        ping("g5", 80),
    ]
    apc = [event("a", 0, departure=BASE + timedelta(seconds=80))]
    result = run(avl, apc)[0]
    assert result["episode_count"] >= 2
    assert result["status"] != "direct"


def test_best_old_point_cannot_hide_contemporaneous_distant_gps():
    avl = [ping("g0", 0), ping("g1", 10, 100), ping("g2", 20, 200), ping("g3", 30, 300)]
    result = run(avl, [event("a", 20)])[0]
    assert result["status"] not in {"direct", "partial"}


def test_short_interpolation_has_two_real_brackets_and_is_not_a_new_ping():
    avl = [ping("g0", 0), ping("g1", 60, 220)]
    result = run(avl, [event("a", 30, 110)])[0]
    assert result["status"] == "direct"
    estimates = [w for w in result["witnesses"] if w["kind"] != "point"]
    assert estimates
    for witness in estimates:
        assert set(witness["ping_ids"]) == {"g0", "g1"}
        assert len(witness["measurement_keys"]) == 2
        assert witness["bracket_gap_s"] == 60
        assert witness["distance_m"] == pytest.approx(0, abs=1e-6)


def test_large_gap_cannot_be_hidden_by_midpoint_interpolation():
    avl = [ping("g0", 0), ping("g1", 240, 440)]
    result = run(avl, [event("a", 120, 220)])[0]
    assert result["status"] not in {"direct", "partial"}
    assert not result["witnesses"]


@pytest.mark.parametrize("flag", ["invalid", "time_ambiguous", "time_nonexistent"])
def test_unusable_intervening_instant_cannot_be_skipped_for_interpolation(flag):
    avl = [ping("g0", 0), ping("bad", 30, 110, **{flag: True}), ping("g1", 60, 220)]
    result = run(avl, [event("a", 30, 110)])[0]
    assert result["status"] not in {"direct", "partial"}
    assert all("bad" not in w["ping_ids"] for w in result["witnesses"])


def test_impossible_speed_bracket_cannot_supply_interpolation():
    avl = [ping("g0", 0), ping("g1", 10, 2000)]
    result = run(avl, [event("a", 5, 1000)])[0]
    assert result["status"] not in {"direct", "partial"}
    assert not result["witnesses"]


def test_missing_position_inside_bracket_is_not_silently_skipped():
    avl = [ping("g0", 0), ping("bad", 30, 110, lat=None), ping("g1", 60, 220)]
    result = run(avl, [event("a", 30, 110)])[0]
    assert result["status"] not in {"direct", "partial"}
    assert not result["witnesses"]


def test_equal_timestamp_conflicting_positions_cannot_be_definitive_evidence():
    avl = [ping("g0", 0), ping("g-conflict", 0, 500)]
    result = run(avl, [event("a", 0)])[0]
    assert result["status"] not in {"direct", "partial"}


def test_route_mismatch_is_visible_and_never_silently_concordant():
    avl = [ping("g0", 0, route="20"), ping("g1", 20, route="20")]
    result = run(avl, [event("a", 0, route="10")])[0]
    assert result["route_conflict"] is True
    assert result["route_state"] in {"conflict", "mixed"}


def test_mixed_interpolation_context_cannot_become_unknown_without_warning():
    avl = [ping("g0", 0, route="10"), ping("g1", 60, 220, route="20")]
    result = run(avl, [event("a", 30, 110, route="30")])[0]
    for witness in result["witnesses"]:
        if witness["kind"] != "point":
            assert set(witness["route_labels"]) == {"10", "20"}
    if result["status"] == "direct":
        assert result["route_conflict"] is True
        assert result["route_state"] in {"conflict", "mixed"}


def test_duplicate_measurement_with_conflicting_context_preserves_both_labels():
    avl = [ping("g0", 0, route="10"), ping("g-copy", 0, route="20"), ping("g1", 20)]
    result = run(avl, [event("a", 0)])[0]
    assert result["route_state"] != "concordant"
    for witness in result["witnesses"]:
        if "g0" in witness["ping_ids"] or "g-copy" in witness["ping_ids"]:
            assert set(witness["route_labels"]) == {"10", "20"}


def test_raw_point_support_survives_unresolved_chunk_owner():
    avl = [ping("g0", 0), ping("g1", 10), ping("g2", 20)]
    apc = [event("a", 0, departure=BASE + timedelta(seconds=20))]
    single = run(avl, apc)[0]
    split = run(avl, apc, mapping={"g0": "s0", "g1": "s0", "g2": "s1"})[0]
    assert single["status"] == split["status"] == "direct"
    assert split["support_level"] == "bounded_interval"
    assert split["segment_id"] is None
    assert set(split["segment_ids"]) == {"s0", "s1"}
    assert split["context_status"] == "multiple_segments"


def test_interpolation_across_chunk_boundary_is_not_promoted_to_direct():
    avl = [ping("g0", 0), ping("g1", 60, 220)]
    result = run(avl, [event("a", 30, 110)], mapping={"g0": "s0", "g1": "s1"})[0]
    assert result["status"] not in {"direct", "partial"}


def test_physical_point_evidence_can_exist_without_any_chunk_assignment():
    avl = [ping("g0", 0), ping("g1", 20)]
    result = run(avl, [event("a", 0)], mapping={})[0]
    assert result["status"] == "direct"
    assert result["segment_id"] is None
    assert result["context_status"] in {"unmapped", "no_assignment"}


def test_mixed_vehicle_index_is_rejected():
    with pytest.raises(ValueError):
        build_index([ping("g0", 0), ping("g1", 20, vehicle="another-vehicle")])


def test_apc_vehicle_mismatch_is_retained_as_ineligible():
    result = run([ping("g0", 0)], [event("a", 0, vehicle="another-vehicle")])[0]
    assert result["status"] == "ineligible"
    assert not result["witnesses"]


@pytest.mark.parametrize("flag", ["invalid", "time_ambiguous", "time_nonexistent"])
def test_unresolved_apc_time_is_ineligible_not_clock_corrected(flag):
    result = run([ping("g0", 0)], [event("a", 0, **{flag: True})])[0]
    assert result["status"] == "ineligible"
    assert result["clock_corrected"] is False
    assert not result["witnesses"]


def test_mixed_aware_and_naive_apc_times_retain_invalid_row_without_sorting_crash():
    aware = BASE.replace(tzinfo=UTC)
    apc = [event("naive", 0), event("aware", 0, arrival=aware, departure=aware)]
    result = {r["apc_rid"]: r for r in run([ping("g0", 0)], apc)}
    assert result["naive"]["status"] == "direct"
    assert result["aware"]["status"] == "ineligible"
    assert not result["aware"]["witnesses"]


@pytest.mark.parametrize(
    "extra", [{"lat": None}, {"lon": float("nan")}, {"lat": 0, "lon": 0}]
)
def test_missing_or_outside_local_coordinates_do_not_become_zero_distance(extra):
    result = run([ping("g0", 0)], [event("a", 0, **extra)])[0]
    assert result["status"] not in {"direct", "partial"}


@pytest.mark.parametrize("departure", [None, BASE - timedelta(seconds=1)])
def test_incomplete_or_inverted_interval_is_retained_as_ineligible(departure):
    result = run([ping("g0", 0)], [event("a", 0, departure=departure)])[0]
    assert result["status"] == "ineligible"


def test_passenger_counts_are_not_positive_matching_evidence():
    avl = [ping("g0", 0), ping("g1", 20)]
    first = run(avl, [event("a", 0)])[0]
    second = run(
        avl,
        [
            event(
                "a", 0, boarding=500, alighting=600, arrival_load=999, departure_load=1
            )
        ],
    )[0]
    assert semantic_signature(first) == semantic_signature(second)


def test_grain_uncertainty_is_not_automatically_repaired_by_gps_support():
    result = run([ping("g0", 0)], [event("a", 0, avg_sample=2, grain_uncertain=True)])[
        0
    ]
    assert result["clock_corrected"] is False
    assert not result.get("tides_publishable", False)
    assert not result.get("trip_id_performed")


def test_exact_duplicate_measurements_do_not_increase_physical_support_or_score():
    avl = [ping("g0", 0), ping("g1", 10), ping("g2", 20)]
    apc = [event("a", 10)]
    first = run(avl, apc)[0]
    duplicate_avl = avl + [dict(p, rid=p["rid"] + "-copy") for p in avl]
    second = run(duplicate_avl, apc)[0]
    assert semantic_signature(first) == semantic_signature(second)


def test_input_permutation_preserves_canonical_results():
    avl = [ping("g0", 0), ping("g1", 10, 10), ping("g2", 20, 20)]
    apc = [event("a0", 0), event("a1", 10, 10), event("a2", 20, 20)]
    expected = {r["apc_rid"]: r for r in run(avl, apc)}
    random.Random(20260908).shuffle(avl)
    random.Random(42).shuffle(apc)
    actual = {r["apc_rid"]: r for r in run(avl, apc)}
    assert actual == expected


def test_prepared_candidate_cache_is_semantically_identical():
    avl = [ping("g0", 0), ping("g1", 60, 220), ping("g2", 120, 440)]
    apc = [event("a0", 0), event("a1", 30, 110), event("a2", 120, 440)]
    assert run(avl, apc, prepared=True) == run(avl, apc)


def test_prepared_cache_cannot_bypass_a_stricter_interpolation_limit():
    avl = [ping("g0", 0), ping("g1", 60, 220)]
    apc = [event("a", 30, 110)]
    index = build_index(avl)
    prepared = prepare_candidates(apc, index)
    try:
        result = match_direct(
            apc,
            index,
            segmentation(avl),
            config={"interpolation_gap_s": 30},
            prepared=prepared,
        )[0]
    except ValueError:
        return  # Explicit rejection of incompatible caches is valid.
    assert result["status"] not in {"direct", "partial"}


@pytest.mark.parametrize("changed", [{"vehicle": "another-vehicle"}, {"lon": -79.0}])
def test_prepared_cache_cannot_bypass_changed_apc_identity_or_geometry(changed):
    avl = [ping("g0", 0), ping("g1", 20)]
    apc = [event("a", 10)]
    index = build_index(avl)
    prepared = prepare_candidates(apc, index)
    updated = [dict(apc[0], **changed)]
    try:
        result = match_direct(updated, index, segmentation(avl), prepared=prepared)[0]
    except ValueError:
        return
    assert result["status"] not in {"direct", "partial"}


def test_inputs_are_not_mutated_and_each_apc_has_one_disposition():
    avl = [ping("g0", 0), ping("g1", 20)]
    apc = [event("a0", 0), event("a1", 20), event("bad", 0, invalid=True)]
    original = copy.deepcopy((avl, apc))
    result = run(avl, apc, prepared=True)
    assert (avl, apc) == original
    assert len(result) == len(apc)
    assert {r["apc_rid"] for r in result} == {r["rid"] for r in apc}


def test_no_avl_has_no_observed_or_interpolated_support():
    result = run([], [event("a", 0)])[0]
    assert result["status"] not in {"direct", "partial"}
    assert not result["witnesses"]


def test_large_clock_shift_is_not_automatically_repaired():
    avl = [ping("g0", 0), ping("g1", 20)]
    result = run(avl, [event("a", 3600)])[0]
    assert result["status"] not in {"direct", "partial"}
    assert result["clock_corrected"] is False


def test_opposite_terminal_roles_can_share_physical_support_without_collapsing_apc():
    avl = [ping("g0", 0), ping("g1", 10), ping("g2", 20)]
    apc = [
        event("outgoing", 0, stop="terminal", departure=BASE + timedelta(seconds=20)),
        event(
            "incoming",
            0,
            stop="terminal",
            direction="W",
            departure=BASE + timedelta(seconds=20),
        ),
    ]
    result = run(avl, apc)
    assert len(result) == 2
    assert {r["apc_rid"] for r in result} == {"outgoing", "incoming"}
    assert all(r["overlap_flags"] for r in result)
    assert all(not r.get("trip_id_performed") for r in result)
