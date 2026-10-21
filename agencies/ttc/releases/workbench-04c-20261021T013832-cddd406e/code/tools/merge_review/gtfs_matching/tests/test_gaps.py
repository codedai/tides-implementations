"""Exact joint-presence segmentation and per-parent conservation contracts."""

import json
import math
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from tools.merge_review.gtfs_matching.gaps import (
    policy,
    split_direction_gaps,
    validate_gap_split,
)


def avl(rid, instant, **extra):
    return dict(source="avl", rid=rid, time=instant, **extra)


def apc(rid, arrival, departure, **extra):
    return dict(source="apc", rid=rid, arrival=arrival, departure=departure, **extra)


def parent(identifier="direction:1:0001", start=0, end=20000, **extra):
    return dict(
        id=identifier,
        start=start,
        end=end,
        direction="NORTH",
        kind="direction_segment",
        boundary_reason="definite_direction_reversal",
        start_inclusive=True,
        end_inclusive=False,
        **extra,
    )


def run(rows, configuration=None, item=None):
    item = item or parent()
    return split_direction_gaps({item["id"]: rows}, [item], configuration)


@pytest.mark.parametrize("duration, expected", [(1799, 1), (1800, 1), (1800.001, 2)])
def test_strict_threshold(duration, expected):
    _, catalog, audit = run([avl("a", 0), avl("b", duration)])
    assert len(catalog) == expected
    assert audit["validation"]["valid"]
    assert audit["gap_count"] == expected - 1
    if expected == 2:
        gap = catalog[0]["gap_boundary_after"]
        assert gap == catalog[1]["gap_boundary_before"]
        assert gap["duration_s"] == duration
        assert gap["operator"] == ">"
        assert gap["left_source_rids"] == [dict(source="avl", rid="a")]
        assert gap["right_source_rids"] == [dict(source="avl", rid="b")]


def test_multiple_gaps_and_closed_internal_endpoints():
    rows = [avl("a", 100), avl("b", 200), avl("c", 2201), avl("d", 5202)]
    members, catalog, audit = run(rows)
    assert [item["id"] for item in catalog] == [
        f"direction:1:0001:gap:{i:04d}" for i in (1, 2, 3)
    ]
    assert [(item["start"], item["end"]) for item in catalog] == [
        (0, 200),
        (2201, 2201),
        (5202, 20000),
    ]
    assert [item["end_inclusive"] for item in catalog] == [True, True, False]
    assert all(item["start_inclusive"] for item in catalog)
    assert catalog[1]["boundary_reason"] == "joint_observation_gap"
    assert catalog[0]["boundary_reason"] == "definite_direction_reversal"
    assert all(
        item["parent_boundary_reason"] == "definite_direction_reversal"
        for item in catalog
    )
    assert all(item["parent_case_id"] == "direction:1:0001" for item in catalog)
    assert all(item["gap_fragment_count"] == 3 for item in catalog)
    assert sum(map(len, members.values())) == len(rows)
    assert audit["split_parent_cases"] == 1
    assert audit["gap_count"] == 2
    assert audit["conservation_violations"] == {}


def test_unchanged_case_identity_no_leading_or_trailing_split():
    original = parent()
    rows = [avl("one", 8000)]
    members, catalog, audit = run(rows, item=original)
    assert catalog == [original]
    assert members == {original["id"]: rows}
    assert not audit["split_parent_cases"]


@pytest.mark.parametrize(
    "bridges",
    [
        [apc("bridge", 0, 4000)],
        [apc("bridge", 1600, 2500)],
        [apc("one", 1000, 2000), apc("two", 1900, 3500)],
        [apc("one", 1000, 2000), apc("two", 2000, 3500)],
        [apc("one", 500, 3500), apc("nested", 1000, 1100)],
    ],
)
def test_apc_intervals_bridge_gap_and_overlaps_union(bridges):
    _, catalog, audit = run([avl("left", 0), *bridges, avl("right", 4000)])
    assert len(catalog) == 1
    assert audit["validation"]["valid"]


def test_apc_departure_is_boundary_not_arrival():
    _, catalog, audit = run([apc("stop", 0, 1000), avl("a", 3000)])
    assert audit["gap_count"] == 1
    assert catalog[0]["end"] == 1000
    assert catalog[0]["source_end"] == 1000
    assert catalog[0]["gap_boundary_after"]["duration_s"] == 2000


@pytest.mark.parametrize(
    "arrival, departure, start, gap",
    [
        (100, 0, 100, 1900),
        (100, None, 100, 1900),
        (None, 100, 100, 1900),
        (math.nan, 100, 100, 1900),
        (100, math.inf, 100, 1900),
    ],
)
def test_invalid_apc_endpoints_are_points_not_invented_intervals(
    arrival, departure, start, gap
):
    _, catalog, audit = run([apc("stop", arrival, departure), avl("a", 2000)])
    assert audit["gap_count"] == 1
    assert catalog[0]["source_start"] == start
    assert catalog[0]["source_end"] == start
    assert catalog[0]["gap_boundary_after"]["duration_s"] == gap


def test_avl_no_hold_and_event_alias_only_when_field_absent():
    alias = dict(source="avl", rid="alias", event_time=2000)
    rows = [avl("left", 0), alias]
    _, _, audit = run(rows)
    assert audit["gaps"][0]["duration_s"] == 2000
    rows.append(avl("missing", None, event_time=1000))
    members, catalog, audit = run(rows)
    assert audit["gap_count"] == 1
    assert audit["unlocated_count"] == 1
    assert members[catalog[-1]["id"]] == [rows[-1]]
    assert catalog[-1]["gap_unlocated"]
    assert catalog[-1]["kind"] == "direction_unresolved"
    assert catalog[-1]["start"] is catalog[-1]["end"] is None


@pytest.mark.parametrize(
    "extra",
    [
        dict(invalid=True),
        dict(lat=math.nan, lon=0),
        dict(direction_label=None),
        dict(direction_label="SOUTH", direction_conflict_ids=["conflict:1"]),
    ],
)
def test_bad_spatial_or_conflicting_direction_rows_still_bridge(extra):
    _, catalog, audit = run(
        [avl("left", 0), avl("bridge", 1500, **extra), avl("right", 3000)]
    )
    assert len(catalog) == 1
    assert audit["validation"]["valid"]


@pytest.mark.parametrize("bad", [None, math.nan, math.inf, -math.inf, True, "garbage"])
def test_missing_nonfinite_and_non_timestamps_do_not_bridge(bad):
    _, catalog, audit = run([avl("left", 0), avl("bad", bad), avl("right", 3000)])
    assert len(catalog) == 3
    assert audit["unlocated_count"] == 1
    assert audit["validation"]["valid"]


@pytest.mark.parametrize("flag", ["time_ambiguous", "time_nonexistent"])
def test_explicit_bad_time_does_not_bridge_even_with_finite_value(flag):
    _, catalog, audit = run(
        [avl("left", 0), avl("bad", 1500, **{flag: True}), avl("right", 3000)]
    )
    assert len(catalog) == 3
    assert audit["gap_count"] == 1
    assert audit["unlocated_count"] == 1


def test_unresolvable_parent_without_usable_rows_is_untouched():
    p = parent(start=None, end=None)
    p["kind"] = "direction_unresolved"
    rows = [avl("none", None), apc("empty", None, None)]
    members, catalog, audit = run(rows, item=p)
    assert catalog == [p]
    assert members[p["id"]] == rows
    assert audit["unlocated_count"] == 0
    assert audit["validation"]["valid"]


def test_parent_clipping_prevents_adjacent_parent_evidence_from_bridging():
    rows = [apc("before", -5000, -1000), avl("left", 0), avl("right", 3000)]
    members, catalog, audit = run(rows, item=parent(end=4000))
    assert audit["gap_count"] == 1
    assert audit["unlocated_count"] == 1
    assert members[catalog[-1]["id"]] == [rows[0]]
    clipped = [apc("interval", -1000, 1000), avl("right", 3000)]
    _, catalog, _ = run(clipped, item=parent(end=4000))
    assert catalog[0]["source_start"] == 0
    assert catalog[0]["source_end"] == 1000


def test_midnight_and_multiday_naive_clocks_keep_real_elapsed_time():
    start = datetime(2023, 11, 6, 23, 45)
    end = start + timedelta(days=2)
    rows = [
        avl("left", start),
        apc("next", (start + timedelta(hours=1)).isoformat(), None),
    ]
    _, catalog, audit = run(rows, item=parent(start=start, end=end))
    assert audit["gaps"][0]["duration_s"] == 3600
    assert catalog[0]["start"] == start
    assert catalog[-1]["end"] == end


def test_timezone_aware_clock_does_not_guess_local_time():
    rows = [
        avl("a", 0),
        avl("aware", datetime(2023, 11, 6, tzinfo=UTC)),
        avl("b", 3000),
    ]
    _, _, audit = run(rows)
    assert audit["unlocated_count"] == 1


def test_equal_time_records_keep_all_boundary_provenance_and_direction():
    rows = [
        avl("left", 0),
        apc("left-stop", 0, 0),
        avl("right", 3000),
        apc("right-stop", 3000, 3000),
    ]
    _, catalog, audit = run(rows)
    assert len(catalog[0]["gap_boundary_after"]["left_source_rids"]) == 2
    assert len(catalog[0]["gap_boundary_after"]["right_source_rids"]) == 2
    assert all(item["direction"] == "NORTH" for item in catalog)
    assert audit["validation"]["valid"]


def test_child_counts_and_conflicts_describe_actual_members():
    rows = [
        avl("left", 0, direction_conflict_ids=["conflict:left"]),
        apc("right", 3000, 3100, direction_conflict_ids=["conflict:right"]),
    ]
    _, catalog, _ = run(rows)
    assert [item["avl_count"] for item in catalog] == [1, 0]
    assert [item["apc_count"] for item in catalog] == [0, 1]
    assert catalog[0]["conflict_ids"] == ["conflict:left"]
    assert catalog[1]["conflict_ids"] == ["conflict:right"]
    assert catalog[1]["apc_rids"] == ["right"]


def test_no_mutation_and_cross_parent_shared_apc_provenance():
    shared = apc("shared", 3000, 3200, candidate_segment_ids=["p1", "p2"])
    members = {"p1": [avl("a", 0), shared], "p2": [shared, avl("b", 6000)]}
    catalog = [parent("p1"), parent("p2")]
    before = deepcopy((members, catalog))
    new_members, new_catalog, audit = split_direction_gaps(members, catalog)
    assert (members, catalog) == before
    assert audit["parent_cases"] == 2
    assert audit["output_cases"] == 4
    assert audit["validation"]["valid"]
    assert (
        sum(row["rid"] == "shared" for rows in new_members.values() for row in rows)
        == 2
    )
    new_members[new_catalog[0]["id"]][0]["changed"] = True
    assert (members, catalog) == before


@pytest.mark.parametrize("threshold", [True, -1, math.nan, math.inf, "1800"])
def test_invalid_threshold_rejected(threshold):
    with pytest.raises(ValueError):
        policy(dict(segmentation_joint_gap_s=threshold))


def test_configuration_can_change_threshold_without_changing_default():
    _, catalog, _ = run(
        [avl("a", 0), avl("b", 1000)], dict(segmentation_joint_gap_s=900)
    )
    assert len(catalog) == 2
    assert policy()["segmentation_joint_gap_s"] == 1800


def test_validator_accepts_json_roundtrip_and_extra_matching_fields():
    p = parent()
    members = {p["id"]: [avl("a", 0), avl("b", 3000)]}
    new_members, catalog, _ = split_direction_gaps(members, [p])
    catalog = json.loads(json.dumps(catalog))
    for item in catalog:
        item.update(gtfs_status="PATTERN_ONLY", gtfs_candidate_count=0, confidence=None)
    assert validate_gap_split(members, [p], new_members, catalog)["valid"]


@pytest.mark.parametrize(
    "mutation, code",
    [
        (lambda m, c: m[c[0]["id"]].append(m[c[1]["id"]][0]), "source_conservation"),
        (
            lambda m, c: c[0]["gap_boundary_after"].update(duration_s=1800),
            "boundary_evidence",
        ),
        (lambda m, c: c[0].update(end=3000), "fragment_bounds"),
        (lambda m, c: c[0].update(end_inclusive=False), "internal_end_inclusivity"),
        (lambda m, c: c[0].update(direction="SOUTH"), "parent_direction_changed"),
        (lambda m, c: m[c[0]["id"]][0].update(time=1), "source_mutation_or_migration"),
    ],
)
def test_validator_detects_corruption(mutation, code):
    p = parent()
    members = {p["id"]: [avl("a", 0), avl("b", 3000)]}
    new_members, catalog, _ = split_direction_gaps(members, [p])
    mutation(new_members, catalog)
    validation = validate_gap_split(members, [p], new_members, catalog)
    assert not validation["valid"]
    assert validation["violations"][code] > 0


def test_validator_does_not_accept_shrunk_bounds_hiding_an_unsplit_gap():
    p = parent()
    members = {p["id"]: [avl("a", 0), avl("b", 3000)]}
    fake = deepcopy(p)
    fake["end"] = 100
    validation = validate_gap_split(members, [p], deepcopy(members), [fake])
    assert validation["violations"]["fragment_count"]
    assert validation["violations"]["unsplit_internal_gap"]


def test_mismatched_catalog_cannot_silently_drop_sources():
    with pytest.raises(ValueError):
        split_direction_gaps({"orphan": [avl("a", 0)]}, [parent()])
