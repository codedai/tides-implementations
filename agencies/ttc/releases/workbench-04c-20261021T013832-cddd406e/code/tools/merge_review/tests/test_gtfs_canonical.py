"""Geometry family validation and direction crosswalk are evidence based."""

import copy

import pytest

from tools.merge_review.gtfs_matching.canonical import build_references


def bundle(shapes=None):
    points = [{"lat": 43.65, "lon": -79.4}, {"lat": 43.66, "lon": -79.4}]
    shapes = shapes or [("north", "0", points), ("south", "1", points[::-1])]
    stops, trips, geometry = [], [], []
    for name, direction, path in shapes:
        stop_times = []
        for index, point in enumerate([path[0], path[-1]]):
            stop_id = f"{name}-{index}"
            stops.append(dict(point, stop_id=stop_id, stop_code=stop_id))
            stop_times.append(
                dict(
                    stop_id=stop_id,
                    stop_sequence=index,
                    t=100 + index * 100,
                    arrival_t=100 + index * 100,
                    departure_t=100 + index * 100,
                )
            )
        trips.append(
            dict(
                trip_id=name,
                route_id="r",
                direction_id=direction,
                shape_id=name,
                service_id="s",
                feed_version="f",
                service_date="2023-11-06",
                stop_times=stop_times,
            )
        )
        geometry.append(dict(shape_id=name, points=path))
    return dict(
        feed_version="f",
        route_id="r",
        date="2023-11-06",
        reference_trips=copy.deepcopy(trips),
        trips=trips,
        stops=stops,
        shapes=geometry,
    )


def test_direction_ids_are_not_hard_coded_and_trips_project_monotonically():
    original = bundle()
    before = copy.deepcopy(original)
    result = build_references(original)
    assert original == before
    assert {
        (r["direction_id"], r["direction_label"]) for r in result["references"]
    } == {("0", "NORTH"), ("1", "SOUTH")}
    assert all(
        t["scheduled"][1]["s_m"] > t["scheduled"][0]["s_m"] for t in result["trips"]
    )
    assert all(t["service_date"] == "2023-11-06" for t in result["trips"])


def test_short_shape_merges_only_when_covered_monotonically():
    long = [
        {"lat": 43.65, "lon": -79.4},
        {"lat": 43.66, "lon": -79.4},
        {"lat": 43.67, "lon": -79.4},
    ]
    result = build_references(bundle([("long", "0", long), ("short", "0", long[1:])]))
    assert len(result["references"]) == 1
    assert result["validations"][0]["accepted"] is True
    short_trip = next(t for t in result["trips"] if t["trip_id"] == "short")
    assert short_trip["scheduled"][0]["s_m"] > 1000


def test_divergent_branch_and_reverse_shape_not_forced_onto_longest():
    main = [{"lat": 43.65, "lon": -79.4}, {"lat": 43.67, "lon": -79.4}]
    branch = [{"lat": 43.66, "lon": -79.41}, {"lat": 43.67, "lon": -79.41}]
    result = build_references(bundle([("main", "0", main), ("branch", "0", branch)]))
    assert len(result["references"]) == 2
    assert result["validations"][0]["accepted"] is False


def test_loop_ambiguity_does_not_imply_shared_canonical_progress():
    a, b, c = (
        {"lat": 43.65, "lon": -79.4},
        {"lat": 43.66, "lon": -79.4},
        {"lat": 43.67, "lon": -79.4},
    )
    result = build_references(
        bundle([("loop", "0", [a, b, a, b, c]), ("short", "0", [a, b])])
    )
    assert len(result["references"]) == 2
    assert result["validations"][0]["accepted"] is False


def test_route_and_version_contamination_rejected():
    value = bundle()
    value["trips"][0]["feed_version"] = "other"
    with pytest.raises(ValueError, match="namespace"):
        build_references(value)
