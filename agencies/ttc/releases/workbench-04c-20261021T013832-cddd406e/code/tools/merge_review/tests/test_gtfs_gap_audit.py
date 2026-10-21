"""Independent persisted gap audit rejects plausible but false child catalogs."""

import copy
import gzip
import hashlib
import json
from datetime import UTC, datetime

import pytest

from tools.merge_review.gtfs_matching import audit
from tools.merge_review.tests.test_gtfs_audit import _repin, _write, completed

PARENT = "direction:v:0000"
BASE = {
    "id": PARENT,
    "start": 0.0,
    "end": 5000.0,
    "start_inclusive": True,
    "end_inclusive": False,
    "direction": "NORTH",
    "boundary_reason": "scope_start",
}


def avl(rid, t, **fields):
    return dict(
        source="avl",
        rid=rid,
        vehicle="v",
        time=t,
        candidate_segment_ids=[PARENT],
        **fields,
    )


def apc(rid, arrival, departure, **fields):
    return dict(
        source="apc",
        rid=rid,
        vehicle="v",
        arrival=arrival,
        departure=departure,
        candidate_segment_ids=[PARENT],
        **fields,
    )


def refs(rows):
    return [dict(source=r["source"], rid=r["rid"]) for r in rows]


def unsplit(rows, parent=None):
    return [dict(BASE if parent is None else parent, source_rids=refs(rows))]


def split_fixture():
    rows = [avl("a", 0), avl("b", 1810)]
    gap = dict(
        start=0.0,
        end=1810.0,
        duration_s=1810.0,
        threshold_s=1800.0,
        operator=">",
        left_source_rids=refs(rows[:1]),
        right_source_rids=refs(rows[1:]),
    )
    catalog = []
    for index, row in enumerate(rows):
        catalog.append(
            dict(
                BASE,
                id=f"{PARENT}:gap:{index + 1:04d}",
                parent_case_id=PARENT,
                parent_segment_start=0.0,
                parent_segment_end=5000.0,
                parent_boundary_reason="scope_start",
                parent_boundary_preserved=False,
                gap_split=True,
                gap_unlocated=False,
                gap_fragment_index=index + 1,
                gap_fragment_count=2,
                gap_boundary_before=copy.deepcopy(gap) if index else None,
                gap_boundary_after=None if index else copy.deepcopy(gap),
                start=1810.0 if index else 0.0,
                end=5000.0 if index else 0.0,
                start_inclusive=True,
                end_inclusive=not index,
                boundary_reason="joint_observation_gap" if index else "scope_start",
                source_rids=refs([row]),
                avl_count=1,
                avl_member_count=1,
                apc_count=0,
                source_start=float(row["time"]),
                source_end=float(row["time"]),
            )
        )
    return rows, catalog


def check(rows, catalog, *, parent=None, threshold=1800):
    report = audit._Report(30)
    members = audit._audit_gap_catalog(
        rows,
        {PARENT: BASE if parent is None else parent},
        catalog,
        threshold,
        report,
        "vehicle-v",
    )
    return report.output(), members


def test_independent_valid_split_is_read_only_and_supplies_exact_child_rows():
    rows, catalog = split_fixture()
    before = copy.deepcopy((rows, catalog))
    report, members = check(rows, catalog)
    assert report["validation_ok"], report
    assert list(members.values()) == [[rows[0]], [rows[1]]]
    assert (rows, catalog) == before
    assert report["examined"]["joint_gap_count"] == 1


@pytest.mark.parametrize("duration", [0, 1799, 1800])
def test_gap_threshold_is_strict_and_never_splits_edge_padding(duration):
    rows = [avl("a", 1000), avl("b", 1000 + duration)]
    parent = dict(BASE, start=-10000, end=20000)
    report, _ = check(rows, unsplit(rows, parent), parent=parent)
    assert report["validation_ok"], report


def test_no_fabricated_avl_hold_can_suppress_real_1810_second_gap():
    rows, _ = split_fixture()
    report, _ = check(rows, unsplit(rows))
    assert report["violations"]["joint_gap_child_partition_changed"]


def test_apc_interval_not_just_arrival_bridges_presence():
    rows = [avl("a", 0), apc("dwell", 500, 3300), avl("b", 4500)]
    report, _ = check(rows, unsplit(rows))
    assert report["validation_ok"], report


def test_nested_apc_interval_does_not_move_frontier_backwards():
    rows = [apc("long", 0, 3000), apc("nested", 100, 110), avl("b", 4500)]
    report, _ = check(rows, unsplit(rows))
    assert report["validation_ok"], report


def test_bad_spatial_quality_and_unknown_direction_do_not_erase_timestamp():
    rows = [
        avl("a", 0),
        avl("bridge", 1700, invalid=True, lat=None, direction_label=None),
        avl("b", 3400),
    ]
    report, _ = check(rows, unsplit(rows))
    assert report["validation_ok"], report


@pytest.mark.parametrize("flag", ["time_ambiguous", "time_nonexistent"])
def test_unreliable_timestamp_cannot_bridge_a_gap(flag):
    rows = [avl("a", 0), avl("bridge", 1700, **{flag: True}), avl("b", 3400)]
    report, _ = check(rows, unsplit(rows))
    assert report["violations"]["joint_gap_child_partition_changed"]
    assert report["examined"]["joint_gap_unlocated_observations"] == 1


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        float("nan"),
        float("inf"),
        "garbage",
        datetime(2023, 11, 6, tzinfo=UTC),
    ],
)
def test_nonfinite_or_non_wall_clock_values_do_not_bridge(value):
    rows = [avl("a", 0), avl("bridge", value, event_time=1700), avl("b", 3400)]
    report, _ = check(rows, unsplit(rows))
    assert report["violations"]["joint_gap_child_partition_changed"]


def test_absent_avl_time_can_use_event_alias_but_present_null_cannot():
    rows = [avl("a", 0), avl("bridge", 1700), avl("b", 3400)]
    rows[1]["event_time"] = rows[1].pop("time")
    report, _ = check(rows, unsplit(rows))
    assert report["validation_ok"], report


@pytest.mark.parametrize("arrival,departure", [(1700, None), (None, 1700), (1700, 10)])
def test_invalid_apc_interval_uses_only_genuine_fallback_point(arrival, departure):
    rows = [avl("a", 0), apc("bridge", arrival, departure), avl("b", 3400)]
    report, _ = check(rows, unsplit(rows))
    assert report["validation_ok"], report


@pytest.mark.parametrize("mutation", ["omit", "duplicate", "migrate", "swap"])
def test_malicious_child_source_membership_is_not_accepted_as_ground_truth(mutation):
    rows, catalog = split_fixture()
    if mutation == "omit":
        catalog[1]["source_rids"] = []
    elif mutation == "duplicate":
        catalog[1]["source_rids"] += copy.deepcopy(catalog[1]["source_rids"])
    elif mutation == "migrate":
        catalog[0]["source_rids"] += catalog[1]["source_rids"]
        catalog[1]["source_rids"] = []
    else:
        catalog[0]["source_rids"], catalog[1]["source_rids"] = (
            catalog[1]["source_rids"],
            catalog[0]["source_rids"],
        )
    report, members = check(rows, catalog)
    assert report["violations"]["joint_gap_child_source_membership_changed"]
    assert list(members.values()) == [[rows[0]], [rows[1]]]


def test_fake_split_without_a_real_gap_is_detected():
    rows, catalog = split_fixture()
    rows[1]["time"] = 1800
    report, _ = check(rows, catalog)
    assert report["violations"]["joint_gap_child_partition_changed"]


@pytest.mark.parametrize(
    "field", ["start", "end", "parent_segment_end", "gap_fragment_index"]
)
def test_child_boundary_or_parent_lineage_cannot_drift(field):
    rows, catalog = split_fixture()
    catalog[1][field] += 1
    report, _ = check(rows, catalog)
    assert report["violations"]["joint_gap_child_boundary_changed"]


def test_boundary_witness_omission_is_detected():
    rows, catalog = split_fixture()
    catalog[1]["gap_boundary_before"]["left_source_rids"] = []
    report, _ = check(rows, catalog)
    assert report["violations"]["joint_gap_child_boundary_changed"]


def test_direction_migration_is_detected():
    rows, catalog = split_fixture()
    catalog[1]["direction"] = "SOUTH"
    report, _ = check(rows, catalog)
    assert report["violations"]["joint_gap_child_direction_changed"]


def test_unlocated_rows_require_a_separate_retained_diagnostic_child():
    rows, catalog = split_fixture()
    unknown = avl("unknown", None)
    rows.append(unknown)
    unlocated = dict(
        catalog[0],
        id=f"{PARENT}:gap:unlocated",
        kind="direction_unresolved",
        gap_unlocated=True,
        gap_fragment_index=None,
        gap_boundary_before=None,
        gap_boundary_after=None,
        start=None,
        end=None,
        boundary_reason="unlocated_timestamp",
        source_rids=refs([unknown]),
        source_start=None,
        source_end=None,
    )
    catalog.append(unlocated)
    report, members = check(rows, catalog)
    assert report["validation_ok"], report
    assert members[unlocated["id"]] == [unknown]
    catalog.pop()
    report, _ = check(rows, catalog)
    assert report["violations"]["joint_gap_parent_source_partition_changed"]


def test_parent_boundary_clips_interval_and_respects_exclusive_endpoint():
    rows = [apc("overlap", -20, 10), avl("a", 20), avl("excluded", 5000)]
    parent = dict(BASE, start=0, end=5000)
    gaps, partitions, unlocated = audit._independent_gap_partitions(rows, parent, 1800)
    assert not gaps
    assert partitions == [[rows[0], rows[1]]]
    assert unlocated == [rows[2]]


def test_existing_cross_direction_apc_membership_is_conserved_per_parent():
    other = "direction:v:0001"
    row = apc("shared", 20, 40)
    row["candidate_segment_ids"].append(other)
    parent2 = dict(BASE, id=other, direction="SOUTH")
    catalog = unsplit([row]) + unsplit([row], parent2)
    report = audit._Report(20)
    result = audit._audit_gap_catalog(
        [row], {PARENT: BASE, other: parent2}, catalog, 1800, report, "v"
    )
    assert report.output()["validation_ok"], report.output()
    assert result == {PARENT: [row], other: [row]}


def test_manifest_policy_is_paired_with_saved_threshold_and_old_runs_stay_supported():
    report = audit._Report(20)
    assert audit._audit_gap_policy({}, {}, report) is None
    policy = dict(audit._GAP_POLICY, segmentation_joint_gap_s=1800.0)
    manifest = dict(parent_boundaries_refined=True, joint_gap_segmentation=policy)
    assert (
        audit._audit_gap_policy(manifest, dict(segmentation_joint_gap_s=1800), report)
        == 1800
    )
    assert report.output()["validation_ok"]
    policy["operator"] = ">="
    audit._audit_gap_policy(manifest, dict(segmentation_joint_gap_s=1800), report)
    assert report.violations["joint_gap_policy_changed"]


@pytest.mark.parametrize("threshold", [-1, True, None, float("nan"), float("inf")])
def test_invalid_saved_gap_threshold_cannot_claim_verified_policy(threshold):
    report = audit._Report(20)
    audit._audit_gap_policy({}, dict(segmentation_joint_gap_s=threshold), report)
    assert report.violations["joint_gap_threshold_invalid"]
    assert report.violations["joint_gap_refinement_flag_missing"]


def test_complete_run_checks_refined_policy_without_changing_old_catalog(tmp_path):
    run, _ = completed(tmp_path)
    manifest = json.loads((run / "manifest.json").read_text())
    config = json.loads((run / "config.json").read_text())
    config["gtfs"]["segmentation_joint_gap_s"] = 1800.0
    manifest.update(
        parent_boundaries_refined=True,
        joint_gap_segmentation=dict(audit._GAP_POLICY, segmentation_joint_gap_s=1800.0),
    )
    _write(run / "manifest.json", manifest)
    _write(run / "config.json", config)
    _repin(run, "manifest.json")
    _repin(run, "config.json")
    for path in (run / "cases").iterdir():
        with gzip.open(path, "rt") as stream:
            artifact = json.load(stream)
        artifact["config"]["segmentation_joint_gap_s"] = 1800.0
        _write(path, artifact)
        _repin(run, path.relative_to(run).as_posix())
    report = audit.audit_run(run)
    assert report["validation_ok"], report
    assert report["examined"]["joint_gap_parent_cases"] == 1
    artifact["config"]["segmentation_joint_gap_s"] = 1799.0
    _write(path, artifact)
    _repin(run, path.relative_to(run).as_posix())
    changed = audit.audit_run(run)
    assert changed["violations"]["joint_gap_artifact_threshold_changed"]


def test_updated_output_hashes_do_not_hide_gap_source_omission(tmp_path):
    run, _ = completed(tmp_path)
    manifest = json.loads((run / "manifest.json").read_text())
    config = json.loads((run / "config.json").read_text())
    config["gtfs"]["segmentation_joint_gap_s"] = 1800.0
    manifest.update(
        parent_boundaries_refined=True,
        joint_gap_segmentation=dict(audit._GAP_POLICY, segmentation_joint_gap_s=1800.0),
    )
    for name, value in [("manifest.json", manifest), ("config.json", config)]:
        _write(run / name, value)
        _repin(run, name)
    path = run / "catalog/vehicle-v.json"
    catalog = json.loads(path.read_text())
    catalog[0]["source_rids"].pop()
    _write(path, catalog)
    _repin(run, "catalog/vehicle-v.json")
    report = audit.audit_run(run)
    assert report["violations"]["joint_gap_parent_source_partition_changed"]
    assert report["violations"]["joint_gap_child_source_membership_changed"]
    inventory = json.loads((run / "output_sha256.json").read_text())
    assert (
        inventory["catalog/vehicle-v.json"]
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )
