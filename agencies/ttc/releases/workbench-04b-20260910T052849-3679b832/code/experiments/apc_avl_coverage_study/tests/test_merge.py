"""Constructed behavioral cases; these are not real-world accuracy labels."""

from __future__ import annotations

import copy
import random
from datetime import datetime, timedelta

import pytest

from experiments.apc_avl_coverage_study.merge import (
    DEFAULTS,
    build_support,
    direct_match,
    extend_sequences,
)
from experiments.apc_avl_coverage_study.run_study import aggregate, summarize
from experiments.apc_avl_coverage_study.segmentation import segment_vehicle

BASE = datetime(2024, 1, 1, 13)


def ping(rid: str, second: int, position: float, **extra) -> dict:
    return {
        "rid": rid,
        "vehicle": "42",
        "time": BASE + timedelta(seconds=second),
        "lat": 43.7,
        "lon": -79.4 + position * 0.001,
        "route": "10",
        "run": "R",
        "destination": "E",
        "invalid": False,
        "time_ambiguous": False,
        **extra,
    }


def event(rid: str, second: int, position: float, **extra) -> dict:
    arrival = BASE + timedelta(seconds=second)
    return {
        "rid": rid,
        "vehicle": "42",
        "arrival": arrival,
        "departure": arrival,
        "lat": 43.7,
        "lon": -79.4 + position * 0.001,
        "route": "10",
        "direction": "E",
        "branch": "A",
        "stop": rid,
        "invalid": False,
        "time_ambiguous": False,
        "grain_uncertain": False,
        **extra,
    }


def assign(avl: list[dict], apc: list[dict], *, config=None, method="spatiotemporal"):
    seg = segment_vehicle(avl, apc, "gap_only")
    links = direct_match(apc, build_support(avl, apc), seg, method, config)
    return seg, links


def extended(avl: list[dict], apc: list[dict], *, config=None):
    seg, links = assign(avl, apc, config=config)
    return seg, links, extend_sequences(apc, links, seg, config)


def by_id(links):
    return {link["apc_rid"]: link for link in links}


def coverage_fixture():
    # AVL 13:00--14:00; APC 13:15--14:13. Final three direct anchors
    # stay within the 15-minute horizon even at the end of the APC-only tail.
    avl = [ping(f"g-{minute}", minute * 60, minute) for minute in range(61)]
    apc = [
        event(f"a-{minute}", minute * 60, minute) for minute in (15, 58, 59, 60, 66, 73)
    ]
    return avl, apc


def test_interpolation_is_bounded_feature_with_two_real_source_ids():
    avl = [ping("g0", 0, 0), ping("g1", 60, 2)]
    apc = [event("a", 30, 1)]
    original = copy.deepcopy((avl, apc))
    _, links = assign(avl, apc)
    assert links[0]["status"] == "direct"
    assert links[0]["support_kind"] == "interpolation"
    assert set(links[0]["ping_ids"]) == {"g0", "g1"}
    assert links[0]["distance_m"] == pytest.approx(0, abs=1e-6)
    assert (avl, apc) == original


@pytest.mark.parametrize("second", [-30, 90])
def test_endpoint_proximity_cannot_claim_direct_evidence_outside_coverage(second):
    avl = [ping("g0", 0, 0), ping("g1", 60, 0)]
    _, links = assign(avl, [event("outside", second, 0)])
    assert links[0]["status"] != "direct"
    assert links[0]["segment_id"] is None
    assert not links[0]["ping_ids"]


def test_large_internal_gap_cannot_be_filled_by_interpolation():
    avl = [ping("g0", 0, 0), ping("g1", 180, 4)]
    _, links = assign(avl, [event("a", 90, 2)])
    assert links[0]["status"] != "direct"
    assert not links[0]["ping_ids"]


@pytest.mark.parametrize("flag", ["invalid", "time_ambiguous", "time_nonexistent"])
def test_invalid_or_dst_ambiguous_avl_interrupts_interpolation(flag):
    avl = [
        ping("g0", 0, 0),
        ping("bad", 30, 1, **{flag: True}),
        ping("g1", 60, 2),
    ]
    apc = [event("a", 30, 1)]
    support = build_support(avl, apc)["a"]
    assert all("bad" not in edge["ping_ids"] for edge in support)
    assert all(edge["kind"] != "interpolation" for edge in support)


@pytest.mark.parametrize("flag", ["invalid", "time_ambiguous", "time_nonexistent"])
def test_invalid_or_dst_ambiguous_apc_preserved_without_attachment(flag):
    avl = [ping("g0", 0, 0), ping("g1", 60, 1)]
    _, links = assign(avl, [event("a", 0, 0, **{flag: True})])
    assert len(links) == 1
    assert links[0]["status"] == "ineligible"
    assert links[0]["segment_id"] is None


def test_missing_coordinate_is_unknown_and_not_a_zero_spatial_residual():
    avl = [ping("g0", 0, 0), ping("g1", 60, 1)]
    apc = [event("a", 0, 0, lat=None)]
    _, strict = assign(avl, apc)
    _, temporal = assign(avl, apc, method="time_only")
    assert strict[0]["status"] != "direct"
    assert strict[0]["distance_m"] is None
    assert temporal[0]["status"] == "temporal_only"
    assert temporal[0]["distance_m"] is None


def test_large_clock_offset_is_not_repaired_by_aligning_window_endpoints():
    avl = [ping("g0", 0, 0), ping("g1", 60, 1), ping("g2", 120, 2)]
    apc = [event(f"a{i}", 3600 + i * 60, i) for i in range(3)]
    original = copy.deepcopy(apc)
    _, _, links = extended(avl, apc)
    assert all(link["segment_id"] is None for link in links)
    assert apc == original


def test_cross_midnight_continuity_does_not_invent_a_service_date():
    start = datetime(2024, 1, 1, 23, 59)
    avl = [ping(f"g{i}", 0, i, time=start + timedelta(minutes=i)) for i in range(3)]
    arrival = start + timedelta(seconds=90)
    apc = [event("a", 0, 1.5, arrival=arrival, departure=arrival)]
    seg, links = assign(avl, apc)
    assert len(seg["segments"]) == 1
    assert links[0]["status"] == "direct"
    assert "service_date" not in links[0]


def test_duplicate_gps_payloads_preserve_lineage_without_multiplying_apc():
    avl = [ping("g0", 0, 0), ping("g0-copy", 0, 0), ping("g1", 60, 1)]
    apc = [event("a", 0, 0, boarding=3)]
    seg, links = assign(avl, apc)
    assert len(links) == 1
    assert links[0]["status"] == "direct"
    assert len(links[0]["ping_ids"]) == 1
    assert set(seg["ping_segment"]) == {row["rid"] for row in avl}
    assert apc[0]["boarding"] == 3


def test_conflicting_duplicate_instants_never_supply_interpolation_brackets():
    # Exact equivalent payloads can share one interpolation representative;
    # distinct positions at one unresolved instant cannot supply a bracket.
    avl = [ping("g0", 0, 0), ping("g0-conflict", 0, 4), ping("g1", 60, 2)]
    support = build_support(avl, [event("a", 30, 1)])["a"]
    assert all(edge["kind"] == "point" for edge in support)


def test_conflicting_same_time_gps_cannot_become_definitive_direct_support():
    avl = [ping("g0", 0, 0), ping("g0-conflict", 0, 4)]
    _, links = assign(avl, [event("a", 0, 0)])
    assert links[0]["status"] != "direct"
    assert not links[0]["ping_ids"]


def test_equal_owner_evidence_abstains_independent_of_input_order():
    # Two independent owner hypotheses provide identical evidence. Stable IDs
    # may order their display, but must never resolve which trip is correct.
    apc = [event("a", 30, 0)]
    g0, g1 = ping("g0", 20, 0), ping("g1", 40, 0)
    seg = {
        "segments": [
            {"segment_id": "s0", "start": BASE, "end": BASE + timedelta(seconds=60)},
            {"segment_id": "s1", "start": BASE, "end": BASE + timedelta(seconds=60)},
        ],
        "ping_segment": {"g0": "s0", "g1": "s1"},
    }
    first = direct_match(apc, build_support([g0, g1], apc), seg, "spatiotemporal")
    second = direct_match(apc, build_support([g1, g0], apc), seg, "spatiotemporal")
    assert first == second
    assert first[0]["status"] == "ambiguous"
    assert first[0]["segment_id"] is None


def test_unequal_coverage_retains_tail_as_inference_and_observed_bounds():
    avl, apc = coverage_fixture()
    seg, direct, links = extended(avl, apc)
    lookup = by_id(links)
    assert lookup["a-60"]["status"] == "direct"
    for rid in ("a-66", "a-73"):
        assert lookup[rid]["status"] == "inferred"
        assert not lookup[rid]["ping_ids"]
        assert set(lookup[rid]["anchor_apc_ids"]) == {"a-58", "a-59", "a-60"}
        assert lookup[rid]["segment_id"] == lookup["a-60"]["segment_id"]
    assert all(row["status"] != "inferred" for row in direct)
    assert seg["segments"][0]["start"] == BASE
    assert seg["segments"][0]["end"] == BASE + timedelta(hours=1)
    metrics, coverage = summarize(avl, apc, seg, links, DEFAULTS)
    assert metrics["apc_rows"] == len(apc)
    assert coverage[0]["apc_trailing_seconds"] == 780
    assert coverage[0]["actual_trip_start"] is None
    assert coverage[0]["actual_trip_end"] is None


def test_inferred_tail_records_cannot_become_new_anchors_and_extend_forever():
    avl, apc = coverage_fixture()
    apc.extend([event("a-79", 79 * 60, 79), event("a-85", 85 * 60, 85)])
    _, direct, links = extended(avl, apc)
    lookup = by_id(links)
    assert lookup["a-73"]["status"] == "inferred"
    assert lookup["a-79"]["segment_id"] is None
    assert lookup["a-85"]["segment_id"] is None
    direct_ids = {row["apc_rid"] for row in direct if row["status"] == "direct"}
    assert all(set(row["anchor_apc_ids"]) <= direct_ids for row in links)


def test_tail_horizon_bounds_departure_as_well_as_arrival():
    avl, apc = coverage_fixture()
    apc.append(event("long-tail", 74 * 60, 74, departure=BASE + timedelta(minutes=80)))
    _, _, links = extended(avl, apc)
    assert by_id(links)["long-tail"]["segment_id"] is None


def test_apc_only_prefix_can_extend_without_inventing_avl_start_boundary():
    avl = [ping(f"g{i}", i * 60, i * 2) for i in range(3)]
    apc = [event(f"a{i}", i * 60, i * 2) for i in (-3, -2, -1, 0, 1, 2)]
    seg, _, links = extended(avl, apc)
    lookup = by_id(links)
    assert lookup["a0"]["status"] == "direct"
    for index in (-3, -2, -1):
        assert lookup[f"a{index}"]["status"] == "inferred"
        assert not lookup[f"a{index}"]["ping_ids"]
        assert lookup[f"a{index}"]["segment_id"] == lookup["a0"]["segment_id"]
    assert seg["segments"][0]["start"] == BASE


def test_next_trip_turnaround_in_apc_only_tail_is_not_joined_to_outgoing_trip():
    avl = [ping(f"g{i}", i * 60, i * 2) for i in range(4)]
    apc = [
        event("out-A", 60, 2, stop="A"),
        event("out-B", 120, 4, stop="B"),
        event("out-C", 180, 6, stop="C"),
        event("back-B", 300, 4, stop="B"),
        event("back-A", 420, 2, stop="A"),
    ]
    _, _, links = extended(avl, apc)
    lookup = by_id(links)
    assert all(lookup[f"out-{stop}"]["status"] == "direct" for stop in "ABC")
    assert lookup["back-B"]["segment_id"] is None
    assert lookup["back-A"]["segment_id"] is None


@pytest.mark.parametrize("flag", ["invalid", "time_ambiguous", "grain_uncertain"])
def test_uncertain_intervening_apc_cannot_bridge_anchors_to_tail(flag):
    avl, apc = coverage_fixture()
    apc.insert(-2, event("barrier", 63 * 60, 63, **{flag: True}))
    _, _, links = extended(avl, apc)
    lookup = by_id(links)
    assert lookup["a-66"]["segment_id"] is None
    assert lookup["a-73"]["segment_id"] is None


def test_extension_cannot_cross_a_new_observed_segment_boundary():
    avl = [ping(f"g{i}", i * 60, i * 2) for i in range(5)]
    apc = [event(f"a{i}", i * 60, i * 2) for i in (1, 2, 3)]
    apc.append(event("tail", 300, 10))
    seg, direct = assign(avl, apc)
    original_owner = seg["ping_segment"]["g3"]
    next_owner = "next-segment"
    seg["segments"].append(
        {"segment_id": next_owner, "start": BASE + timedelta(seconds=240)}
    )
    seg["ping_segment"]["g4"] = next_owner
    links = by_id(extend_sequences(apc, direct, seg))
    assert links["a3"]["segment_id"] == original_owner
    assert links["tail"]["segment_id"] is None


def test_spatial_contradiction_is_not_reclassified_as_missing_gps_continuation():
    avl = [ping(f"g{i}", i * 60, i * 2) for i in range(7)]
    apc = [event(f"a{i}", i * 60, i * 2) for i in (1, 2, 3)]
    # During continuous observed AVL coverage, APC suddenly claims a stop
    # 1.6 km away. This is contradictory local support, not an absent GPS tail.
    apc.append(event("wrong-place", 300, 30))
    _, _, links = extended(avl, apc)
    assert by_id(links)["wrong-place"]["segment_id"] is None


def test_ambiguous_intermediate_assignment_blocks_later_inferred_continuation():
    avl, apc = coverage_fixture()
    apc.insert(-2, event("ambiguous", 63 * 60, 63))
    seg, direct = assign(avl, apc)
    ambiguous = by_id(direct)["ambiguous"]
    ambiguous.update(
        status="ambiguous",
        reason="COMPETING_OWNER_EVIDENCE",
        candidate_owners=[direct[0]["segment_id"], "another-owner"],
        candidate_scores=[700, 700],
        candidate_count=2,
    )
    links = by_id(extend_sequences(apc, direct, seg))
    assert links["a-66"]["segment_id"] is None
    assert links["a-73"]["segment_id"] is None


def test_row_and_file_order_permutations_preserve_canonical_decisions():
    avl, apc = coverage_fixture()
    expected_seg, _, expected_links = extended(avl, apc)
    random.Random(123).shuffle(avl)
    random.Random(456).shuffle(apc)
    seg, _, links = extended(avl, apc)
    assert seg == expected_seg
    assert by_id(links) == by_id(expected_links)


def test_all_apc_rows_have_one_disposition_including_invalid_and_no_vehicle_match():
    avl, apc = coverage_fixture()
    apc.extend(
        [
            event("missing-time", 0, 0, arrival=None, departure=None, invalid=True),
            event("bad-interval", 100, 0, departure=BASE),
            event("too-far", 10000, 0),
        ]
    )
    seg, _, links = extended(avl, apc)
    assert len(links) == len(apc)
    assert {link["apc_rid"] for link in links} == {row["rid"] for row in apc}
    metrics, _ = summarize(avl, apc, seg, links, DEFAULTS)
    assert metrics["invariants"]["apc_conservation"] == 0
    assert metrics["invariants"]["duplicate_apc_disposition"] == 0
    totals = aggregate([metrics])
    assert totals["accuracy"] is None
    assert totals["trip_boundary_accuracy"] is None
    assert not totals["tides_publishable"]
    _, no_avl = assign([], apc)
    assert len(no_avl) == len(apc)
    assert all(link["segment_id"] is None for link in no_avl)


def test_an_all_ineligible_population_does_not_produce_a_fake_zero_error_rate():
    apc = [event("bad", 0, 0, invalid=True)]
    seg, links = assign([], apc)
    metrics, _ = summarize([], apc, seg, links, DEFAULTS)
    totals = aggregate([metrics])
    assert totals["assignment_rate_eligible_rows"] is None
    assert totals["accuracy"] is None
