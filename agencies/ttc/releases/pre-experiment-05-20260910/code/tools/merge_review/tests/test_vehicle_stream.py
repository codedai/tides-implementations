"""UNION ALL retains sources and displays chronology without inferred linkage."""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from tools.merge_review.vehicle_stream import (
    build_vehicle_stream,
    validate_vehicle_stream,
)

T = datetime(2023, 11, 6)


def ping(rid, seconds=0, **fields):
    return {
        "rid": rid,
        "vehicle": "9027",
        "time": T + timedelta(seconds=seconds),
        "source_row": 1,
        "lat": 43.65,
        "lon": -79.4,
        "route": "29",
        **fields,
    }


def apc(rid, seconds=0, **fields):
    return {
        "rid": rid,
        "vehicle": "9027",
        "arrival": T + timedelta(seconds=seconds),
        "departure": T + timedelta(seconds=seconds + 20),
        "source_row": 1,
        "lat": 43.65,
        "lon": -79.4,
        "route": "929",
        "branch": "WIPR",
        "boarding": 2.5,
        "arrival_load": 9.5,
        **fields,
    }


def test_union_all_not_cartesian_or_one_to_one_matching():
    avl = [ping("v2", source_row=2), ping("v1", source_row=1)]
    rows = [apc("a2", source_row=2), apc("a1", source_row=1)]
    originals = copy.deepcopy((avl, rows))
    stream = build_vehicle_stream(avl, rows)
    assert len(stream) == 4
    assert [r["rid"] for r in stream] == ["v1", "v2", "a1", "a2"]
    assert [r["display_order"] for r in stream] == [0, 1, 2, 3]
    assert (avl, rows) == originals
    assert not any(validate_vehicle_stream(avl, rows, stream)["violations"].values())
    assert not any(
        "status" in r or "witnesses" in r or "segment_id" in r for r in stream
    )


def test_duplicate_payloads_and_invalid_observations_are_retained():
    avl = [ping("p1", invalid=True), ping("p2", invalid=True)]
    rows = [apc("a1", invalid=True), apc("a2", invalid=True)]
    result = build_vehicle_stream(avl, rows)
    assert len(result) == 4 and all(r["invalid"] for r in result)
    assert not any(validate_vehicle_stream(avl, rows, result)["violations"].values())


def test_input_permutation_does_not_change_display_order():
    avl = [ping("b", 5), ping("a", 0)]
    rows = [apc("d", 5), apc("c", 0)]
    assert build_vehicle_stream(avl, rows) == build_vehicle_stream(
        avl[::-1], rows[::-1]
    )


def test_source_rid_pair_keeps_identical_rid_labels_from_different_sources():
    result = build_vehicle_stream([ping("same")], [apc("same")])
    assert len(result) == 2
    assert {(r["source"], r["source_rid"]) for r in result} == {
        ("avl", "same"),
        ("apc", "same"),
    }


def test_arrival_fallback_null_last_and_original_times_unchanged():
    avl = [ping("p", 3), ping("unknown", time=None)]
    rows = [
        apc("arrival", 2, departure=T + timedelta(days=1)),
        apc("fallback", arrival=None, departure=T + timedelta(seconds=1)),
        apc("missing", arrival=None, departure=None),
    ]
    result = build_vehicle_stream(avl, rows)
    assert [r["rid"] for r in result] == [
        "fallback",
        "arrival",
        "p",
        "unknown",
        "missing",
    ]
    assert [r["event_time_basis"] for r in result] == [
        "apc.departure_fallback",
        "apc.arrival",
        "avl.time",
        "missing",
        "missing",
    ]
    assert result[1]["departure"] == T + timedelta(days=1)
    assert result[2]["time"] == T + timedelta(seconds=3)
    assert not any(validate_vehicle_stream(avl, rows, result)["violations"].values())


def test_day_membership_uses_arrival_not_cross_midnight_departure():
    rows = [apc("tail", 86395, departure=T + timedelta(days=1, seconds=15))]
    result = build_vehicle_stream([], rows)
    assert not any(
        validate_vehicle_stream([], rows, result, T, T + timedelta(days=1))[
            "violations"
        ].values()
    )
    before = [apc("before", -1, departure=T + timedelta(seconds=5))]
    check = validate_vehicle_stream(
        [], before, build_vehicle_stream([], before), T, T + timedelta(days=1)
    )
    assert check["violations"]["day_membership_violation"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"vehicle": None},
        {"vehicle": ""},
        {"event_time": T},
        {"time": T.replace(tzinfo=UTC)},
        {"time": "2023-11-06"},
        {"source_row": True},
        {"source_row": -1},
        {"source_row": "1"},
    ],
)
def test_unsafe_source_alias_or_time_is_rejected(changes):
    with pytest.raises(ValueError):
        build_vehicle_stream([ping("p", **changes)], [])


def test_multiple_vehicles_and_duplicate_source_ids_are_rejected():
    with pytest.raises(ValueError):
        build_vehicle_stream([ping("p")], [apc("a", vehicle="another")])
    with pytest.raises(ValueError):
        build_vehicle_stream([ping("p"), ping("p")], [])


@pytest.mark.parametrize(
    ("key", "value", "violation"),
    [
        ("boarding", 100, "original_field_changed"),
        ("arrival", T + timedelta(seconds=50), "original_field_changed"),
        ("vehicle", "other", "original_field_changed"),
        ("event_time", T + timedelta(seconds=1), "event_time_changed"),
        ("event_time_basis", "inferred", "event_basis_changed"),
        ("display_order", 5, "ordinal_violation"),
        ("witnesses", [], "fabricated_matching_fields"),
    ],
)
def test_independent_validator_detects_mutation(key, value, violation):
    rows = [apc("a")]
    output = build_vehicle_stream([], rows)
    output[0][key] = value
    assert validate_vehicle_stream([], rows, output)["violations"][violation] >= 1


def test_independent_validator_detects_omission_duplication_and_order():
    avl, rows = [ping("p")], [apc("a", 10)]
    output = build_vehicle_stream(avl, rows)
    assert (
        validate_vehicle_stream(avl, rows, output[:1])["violations"][
            "missing_source_records"
        ]
        == 1
    )
    assert (
        validate_vehicle_stream(avl, rows, output + output[:1])["violations"][
            "duplicate_output_rid"
        ]
        == 1
    )
    assert (
        validate_vehicle_stream(avl, rows, output[::-1])["violations"][
            "display_order_violation"
        ]
        == 1
    )


def test_nan_source_values_are_preserved_without_false_mutation():
    avl = [ping("p", lat=float("nan"), invalid=True)]
    result = build_vehicle_stream(avl, [])
    assert not any(validate_vehicle_stream(avl, [], result)["violations"].values())
