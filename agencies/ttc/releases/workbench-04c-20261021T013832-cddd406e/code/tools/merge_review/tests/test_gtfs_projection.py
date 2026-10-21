"""Sequence evidence, reverse fallback and lossless rejection diagnostics."""

import copy

import pytest

from tools.merge_review.gtfs_matching.canonical import geometry
from tools.merge_review.gtfs_matching.projection import (
    project_apc_intervals,
    project_sequence,
)


def reference(name="n", reverse=False, lon=-79.4):
    points = [
        {"lat": 43.65, "lon": lon},
        {"lat": 43.66, "lon": lon},
        {"lat": 43.67, "lon": lon},
    ]
    if reverse:
        points.reverse()
    return dict(
        reference_id=name,
        family_id="family:" + name,
        feed_version="f",
        route_id="r",
        direction_label="SOUTH" if reverse else "NORTH",
        direction_id="1" if reverse else "0",
        origin=[43.66, -79.4],
        points=geometry(points, [43.66, -79.4]),
        stop_positions=[
            dict(
                stop_id="gtfs-id",
                stop_code="1652",
                s_m=1000,
                lateral_m=0,
                ambiguous=False,
            )
        ],
    )


def ping(rid, t, lat, **kwargs):
    return dict(
        rid=rid,
        vehicle="v",
        t=t,
        lat=lat,
        lon=-79.4,
        direction_label="NORTH",
        heading=0,
        speed=20,
        **kwargs,
    )


def test_projection_is_lossless_and_sequence_monotone():
    rows = [ping("a", 0, 43.65), ping("b", 60, 43.66), ping("c", 120, 43.67)]
    original = copy.deepcopy(rows)
    result = project_sequence(rows, [reference()])
    assert rows == original
    assert [p["rid"] for p in result["points"]] == ["a", "b", "c"]
    assert all(p["projection_status"] == "ON_REFERENCE" for p in result["points"])
    assert len(result["fragments"]) == 1
    assert (
        result["points"][2]["s_m"]
        > result["points"][1]["s_m"]
        > result["points"][0]["s_m"]
    )


def test_reverse_reference_fallback_overrides_wrong_direction_prior():
    rows = [
        dict(ping("a", 0, 43.67), heading=180),
        dict(ping("b", 60, 43.66), heading=180),
        dict(ping("c", 120, 43.65), heading=180),
    ]
    result = project_sequence(rows, [reference(), reference("s", True)])
    assert all(p["reference_id"] == "s" for p in result["points"])
    assert all(p["direction_fallback"] for p in result["points"])
    assert all(len(p["projection_alternatives"]) == 2 for p in result["points"])


def test_all_references_fail_gate_before_off_reference():
    result = project_sequence(
        [dict(ping("x", 0, 43.66), lon=-79.41)],
        [reference(), reference("other", lon=-79.41)],
    )
    assert result["points"][0]["projection_status"] == "ON_REFERENCE"
    assert result["points"][0]["reference_id"] == "other"
    failed = project_sequence(
        [dict(ping("x", 0, 43.66), lon=-79.5)],
        [reference(), reference("other", lon=-79.41)],
    )
    assert failed["points"][0]["projection_status"] == "OFF_REFERENCE"
    assert failed["points"][0]["lateral_m"] > 100


def test_isolated_jump_retained_and_real_elapsed_time_used():
    rows = [
        ping("a", 0, 43.65),
        dict(ping("bad", 15, 43.655), lon=-80),
        ping("b", 60, 43.66),
    ]
    result = project_sequence(rows, [reference()])
    assert [p["projection_status"] for p in result["points"]] == [
        "ON_REFERENCE",
        "OFF_REFERENCE",
        "ON_REFERENCE",
    ]
    assert len(result["fragments"]) == 1
    assert result["off_reference_runs"][0]["classification"] == "GPS_OUTLIER"
    assert result["points"][1]["source_rid"] == "bad"


def test_scope_separator_forces_fragment_and_dp_restart():
    rows = [
        ping("a", 0, 43.66),
        dict(ping("b", 10, 43.65), scope_separator_before=True),
    ]
    result = project_sequence(rows, [reference()])
    assert len(result["fragments"]) == 2
    assert all(p["projection_status"] == "ON_REFERENCE" for p in result["points"])
    assert result["fragments"][1]["break_reason"] == "scope_separator"


def test_apc_missing_coordinates_keeps_canonical_stop_interval_without_track():
    row = dict(rid="a", arrival=100, departure=130, stop="1652", lat=None, lon=None)
    result = project_apc_intervals([row], [reference()])[0]
    assert result["projection_status"] == "STOP_INTERVAL_ONLY"
    assert result["canonical_stop_intervals"][0]["stop_id"] == "gtfs-id"
    assert result["interval_start"] == 100 and result["interval_end"] == 130
    assert result["trajectory_fabricated"] is False
    assert result["reference_id"] is None


def test_apc_known_stop_does_not_rescue_coordinate_gate_failure():
    row = dict(rid="a", arrival=100, departure=130, stop="1652", lat=43.66, lon=-80)
    result = project_apc_intervals([row], [reference()])[0]
    assert result["projection_status"] == "OFF_REFERENCE"
    assert result["canonical_stop_intervals"]
    assert all(
        a["projection_status"] == "OFF_REFERENCE"
        for a in result["projection_alternatives"]
    )


def test_apc_progress_is_canonical_stop_not_nearest_coordinate():
    row = dict(rid="a", arrival=100, departure=130, stop="1652", lat=43.65, lon=-79.4)
    result = project_apc_intervals([row], [reference()])[0]
    assert result["s_m"] == 1000
    assert result["projection_alternatives"][0]["coordinate_projection_s_m"] == 0


def test_gate_is_applied_after_unconstrained_sequence_projection():
    rows = [
        dict(ping("a", 0, 43.65), lon=-79.4015),
        dict(ping("b", 60, 43.66), lon=-79.4015),
    ]
    result = project_sequence(rows, [reference()])
    assert all(p["projection_status"] == "OFF_REFERENCE" for p in result["points"])
    for point in result["points"]:
        alt = point["projection_alternatives"][0]
        assert alt["ungated_s_m"] is not None
        assert alt["ungated_lateral_m"] > 100
        assert alt["gate_applied_after_sequence"] is True


def test_public_gate_config_controls_both_avl_and_apc():
    row = dict(ping("a", 0, 43.65), lon=-79.4015)
    result = project_sequence([row], [reference()], {"max_projection_distance_m": 150})
    assert result["points"][0]["projection_status"] == "ON_REFERENCE"
    result = project_apc_intervals(
        [
            dict(
                rid="a",
                arrival=0,
                departure=30,
                stop="1652",
                lat=row["lat"],
                lon=row["lon"],
            )
        ],
        [reference()],
        {"max_projection_distance_m": 150},
    )
    assert result[0]["projection_status"] == "ON_REFERENCE"


def test_in_gate_but_sequence_impossible_point_is_off_reference_with_reason():
    rows = [ping("a", 0, 43.65), ping("jump", 10, 43.67), ping("b", 40, 43.66)]
    result = project_sequence(rows, [reference()])
    assert [p["projection_status"] for p in result["points"]] == [
        "ON_REFERENCE",
        "OFF_REFERENCE",
        "ON_REFERENCE",
    ]
    assert result["points"][1]["lateral_m"] == 0
    assert "no_sequence_consistent" in result["points"][1]["rejection_reason"]
    assert result["points"][2]["t"] - result["points"][0]["t"] == 40


def test_alternative_paths_independently_satisfy_speed_and_progress_constraints():
    rows = [ping(str(i), i * 30, 43.65 + i * 0.0007) for i in range(20)]
    rows[7]["lat"] = 43.669
    result = project_sequence(rows, [reference(), reference("s", True)])
    for ref in ("n", "s"):
        previous = None
        for row in result["points"]:
            alt = next(
                a for a in row["projection_alternatives"] if a["reference_id"] == ref
            )
            if alt["projection_status"] != "ON_REFERENCE":
                continue
            if previous is not None and not alt["sequence_restart"]:
                ds = alt["s_m"] - previous["s_m"]
                elapsed = row["t"] - previous["t"]
                assert -35 - 1e-6 <= ds <= 36.1111111111 * elapsed + 20 + 1e-6
            previous = dict(t=row["t"], s_m=alt["s_m"])


def test_apc_duplicate_stop_code_keeps_ambiguity():
    ref = reference()
    ref["stop_positions"].append(
        dict(stop_id="second", stop_code="1652", s_m=1500, lateral_m=0, ambiguous=False)
    )
    result = project_apc_intervals(
        [dict(rid="a", arrival=100, departure=130, stop="1652")], [ref]
    )[0]
    assert all(i["ambiguous"] for i in result["canonical_stop_intervals"])
    assert result["projection_alternatives"][0]["s_m"] is None


@pytest.mark.parametrize("change", [{"feed_version": "other"}, {"route_id": "929"}])
def test_cross_namespace_references_rejected(change):
    with pytest.raises(ValueError, match="mix"):
        project_sequence([], [reference(), dict(reference("s", True), **change)])


def test_missing_invalid_and_duplicate_source_ids():
    result = project_sequence(
        [dict(ping("a", 0, 43.65), invalid=True), dict(ping("b", 1, 43.65), lat=None)],
        [reference()],
    )
    assert all(p["projection_status"] == "UNPROJECTABLE" for p in result["points"])
    with pytest.raises(ValueError, match="Duplicate"):
        project_sequence([ping("a", 0, 43.65), ping("a", 1, 43.65)], [reference()])
