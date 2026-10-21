"""Behavioral fixtures for experimental observed-coverage segmentation."""

from __future__ import annotations

import importlib.util
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "coverage_segmentation", Path(__file__).parents[1] / "segmentation.py"
)
SEGMENTATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEGMENTATION)
segment_vehicle = SEGMENTATION.segment_vehicle
METHODS = SEGMENTATION.METHODS
BASE = datetime(2024, 1, 1, 13)


def ping(index: int, *, seconds: int | None = None, **fields) -> dict:
    return {
        "rid": f"avl-{index:04}",
        "vehicle": "42",
        "time": BASE + timedelta(seconds=index * 60 if seconds is None else seconds),
        "lat": 43.7,
        "lon": -79.4 + index * 0.001,
        "route": "10",
        "run": "R",
        "destination": "E",
        "source_trip": "unreliable",
        "invalid": False,
        "time_ambiguous": False,
        **fields,
    }


def apc(
    index: int,
    *,
    direction: str = "E",
    stop: str | None = None,
    seconds: int | None = None,
    **fields,
) -> dict:
    arrival = BASE + timedelta(seconds=index * 120 if seconds is None else seconds)
    return {
        "rid": f"apc-{index:04}",
        "vehicle": "42",
        "arrival": arrival,
        "departure": arrival + timedelta(seconds=10),
        "route": "10",
        "lat": 43.7,
        "lon": -79.4 + index * 0.002,
        "direction": direction,
        "stop": stop or str(index),
        "grain_uncertain": False,
        "invalid": False,
        "time_ambiguous": False,
        **fields,
    }


def groups(result: dict) -> list[list[str]]:
    return [s["ping_ids"] for s in result["segments"]]


@pytest.mark.parametrize("method", METHODS)
def test_midnight_is_not_a_boundary_and_each_ping_is_retained(method):
    rows = [
        ping(i, time=datetime(2024, 1, 1, 23, 58) + timedelta(minutes=i))
        for i in range(5)
    ]
    result = segment_vehicle(rows, [], method)
    assert groups(result) == [[r["rid"] for r in rows]]
    assert len(result["ping_segment"]) == len(rows)
    assert result["segments"][0]["end"].day == 2


@pytest.mark.parametrize("method", METHODS)
def test_gap_boundary_is_strict_and_source_trip_reuse_does_not_join_it(method):
    rows = [ping(0), ping(1, seconds=1800), ping(2, seconds=3601)]
    result = segment_vehicle(rows, [], method)
    assert groups(result) == [["avl-0000", "avl-0001"], ["avl-0002"]]
    assert result["boundaries"][0]["reasons"] == ["avl_gap"]


@pytest.mark.parametrize("method", METHODS)
def test_raw_trip_id_changes_do_not_define_a_physical_trip(method):
    rows = [ping(i, source_trip=f"source-{i}") for i in range(12)]
    assert len(segment_vehicle(rows, [], method)["segments"]) == 1


def test_context_change_requires_persistence_and_debounces_one_bad_label():
    rows = [ping(i, route="99" if i == 5 else "10") for i in range(15)]
    assert len(segment_vehicle(rows, [], "avl_context")["segments"]) == 1
    for row in rows[8:]:
        row["route"] = "99"
    result = segment_vehicle(rows, [], "avl_context")
    assert [b["after_rid"] for b in result["boundaries"]] == ["avl-0008"]
    assert "sustained_avl_context_change" in result["boundaries"][0]["reasons"]
    assert len(segment_vehicle(rows, [], "gap_only")["segments"]) == 1


def test_destination_text_change_alone_is_not_a_terminal():
    rows = [
        ping(i, destination="new text" if i >= 8 else "old text") for i in range(16)
    ]
    assert len(segment_vehicle(rows, [], "avl_context")["segments"]) == 1


def test_geometry_reversal_makes_one_candidate_terminal_with_source_id_unchanged():
    rows = [ping(i, lon=-79.4 + (i if i < 9 else 18 - i) * 0.002) for i in range(19)]
    result = segment_vehicle(rows, [], "avl_context")
    assert len(result["segments"]) == 2
    assert "observed_geometry_reversal" in result["boundaries"][0]["reasons"]
    assert result["segments"][1]["possible_terminal"]
    assert all(s["experimental_chunk"] for s in result["segments"])


@pytest.mark.parametrize("method", METHODS)
def test_tied_timestamp_input_permutation_does_not_change_membership_or_ids(method):
    rows = [ping(i, seconds=(i // 2) * 60, lon=-79.4) for i in range(20)]
    expected = segment_vehicle(rows, [], method)
    random.Random(2).shuffle(rows)
    assert segment_vehicle(rows, [], method) == expected


@pytest.mark.parametrize("method", METHODS)
def test_dst_ambiguous_pings_are_retained_and_cannot_bridge_normal_segments(method):
    rows = [ping(i, time_ambiguous=i in (4, 5)) for i in range(10)]
    result = segment_vehicle(rows, [], method)
    assert groups(result) == [
        [f"avl-{i:04}" for i in range(4)],
        ["avl-0004"],
        ["avl-0005"],
        [f"avl-{i:04}" for i in range(6, 10)],
    ]
    assert len(result["ping_segment"]) == 10
    assert [s["time_ambiguous"] for s in result["segments"]] == [
        False,
        True,
        True,
        False,
    ]


@pytest.mark.parametrize("method", METHODS)
def test_invalid_avl_with_time_is_a_barrier_and_without_time_is_reported(method):
    rows = [ping(i, invalid=i == 4) for i in range(8)]
    rows.append(ping(9, time=None, invalid=True))
    result = segment_vehicle(rows, [], method)
    assert groups(result) == [
        [f"avl-{i:04}" for i in range(4)],
        [f"avl-{i:04}" for i in range(5, 8)],
    ]
    assert result["excluded_ping_ids"] == ["avl-0004", "avl-0009"]
    assert result["unlocated_invalid_ping_ids"] == ["avl-0009"]
    assert all(s["chronology_unresolved"] for s in result["segments"])


@pytest.mark.parametrize("method", ["gap_only", "avl_context"])
def test_apc_removal_and_passenger_count_changes_cannot_change_avl_only(method):
    rows = [ping(i) for i in range(20)]
    events = [apc(i, direction="E" if i < 4 else "W", boardings=999) for i in range(8)]
    assert segment_vehicle(rows, events, method) == segment_vehicle(rows, [], method)


def test_apc_sustained_direction_change_refines_straight_avl_without_gtfs():
    rows = [ping(i) for i in range(20)]
    events = [apc(i, direction="E" if i < 4 else "W") for i in range(8)]
    result = segment_vehicle(rows, events, "apc_assisted")
    assert len(result["segments"]) == 2
    assert "apc_sustained_direction_change" in result["boundaries"][0]["reasons"]
    assert result["boundaries"][0]["after_rid"] == "avl-0008"
    assert len(segment_vehicle(rows, events, "avl_context")["segments"]) == 1


def test_repeated_stop_reports_cannot_manufacture_sustained_apc_direction():
    rows = [ping(i) for i in range(20)]
    events = [
        apc(i, direction="E" if i < 4 else "W", stop="a" if i < 4 else "b")
        for i in range(8)
    ]
    assert len(segment_vehicle(rows, events, "apc_assisted")["segments"]) == 1


def test_apc_observed_retracing_needs_two_distinct_stops_on_each_side():
    rows = [ping(i) for i in range(20)]
    events = [
        apc(i, direction="", stop=stop)
        for i, stop in enumerate(["a", "b", "c", "b", "a"])
    ]
    result = segment_vehicle(rows, events, "apc_assisted")
    assert len(result["segments"]) == 2
    assert result["boundaries"][0]["after_rid"] == "avl-0006"
    assert "apc_observed_stop_retracing" in result["boundaries"][0]["reasons"]


@pytest.mark.parametrize("flag", ["invalid", "time_ambiguous", "grain_uncertain"])
def test_uncertain_apc_record_breaks_direction_evidence(flag):
    rows = [ping(i) for i in range(20)]
    events = [
        apc(i, direction="E" if i < 4 else "W", **{flag: i == 3}) for i in range(6)
    ]
    assert len(segment_vehicle(rows, events, "apc_assisted")["segments"]) == 1


def test_apc_only_tail_cannot_invent_avl_or_shift_its_coverage():
    rows = [ping(i) for i in range(10)]
    events = [apc(i, direction="E" if i < 6 else "W") for i in range(10)]
    result = segment_vehicle(rows, events, "apc_assisted")
    assert len(result["segments"]) == 1
    assert result["segments"][0]["end"] == rows[-1]["time"]


def test_same_instant_conflicting_positions_are_uncertain_and_deterministic():
    rows = [
        ping(0),
        ping(1, seconds=60, lon=-79.4),
        ping(2, seconds=60, lon=-79.0),
        ping(3),
    ]
    result = segment_vehicle(rows, [], "gap_only")
    assert len(result["segments"]) == 4
    assert result["segments"][1]["chronology_unresolved"]
    assert result["segments"][2]["chronology_unresolved"]
    assert len(result["ping_segment"]) == 4


def test_duplicate_source_lineage_is_rejected():
    with pytest.raises(ValueError, match="unique source-record"):
        segment_vehicle([ping(0), ping(0)], [], "gap_only")


def test_mixed_vehicles_are_rejected_before_any_merge():
    with pytest.raises(ValueError, match="single canonical vehicle"):
        segment_vehicle([ping(0), ping(1, vehicle="different")], [], "gap_only")


def test_empty_and_all_invalid_inputs_have_no_fabricated_chunks():
    assert segment_vehicle([], [], "gap_only")["segments"] == []
    result = segment_vehicle([ping(0, invalid=True)], [], "apc_assisted")
    assert result["segments"] == []
    assert result["excluded_ping_ids"] == ["avl-0000"]


@pytest.mark.parametrize("method", METHODS)
def test_zero_position_is_retained_but_cannot_manufacture_geometry_turns(method):
    rows = [ping(i, **({"lat": 0, "lon": 0} if i == 5 else {})) for i in range(12)]
    result = segment_vehicle(rows, [], method)
    assert len(result["segments"]) == 3
    assert result["segments"][1]["ping_ids"] == ["avl-0005"]
    assert result["segments"][1]["position_uncertain"]
    assert len(result["ping_segment"]) == len(rows)
    assert not any(s["possible_terminal"] for s in result["segments"])


@pytest.mark.parametrize("method", METHODS)
def test_nonexistent_wall_time_is_preserved_as_an_uncertain_singleton(method):
    rows = [ping(i, time_nonexistent=i == 5) for i in range(12)]
    result = segment_vehicle(rows, [], method)
    assert len(result["segments"]) == 3
    assert result["segments"][1]["time_nonexistent"]
    assert result["segments"][1]["chronology_unresolved"]
    assert result["chronology_unresolved"]
    assert len(result["ping_segment"]) == len(rows)
