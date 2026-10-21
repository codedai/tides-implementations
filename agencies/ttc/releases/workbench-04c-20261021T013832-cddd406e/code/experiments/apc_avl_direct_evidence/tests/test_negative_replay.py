"""Negative replay must accept valid evidence and detect deliberate corruption."""

from __future__ import annotations

import copy
import json
from argparse import Namespace

import polars as pl
import pytest

from experiments.apc_avl_direct_evidence.matcher import DEFAULT_CONFIG
from experiments.apc_avl_direct_evidence.tests.test_matcher import (
    event,
    ping,
    run,
    segmentation,
)
from experiments.apc_avl_direct_evidence.validation.replay_negative import (
    NegativeReplay,
    execute,
    sha256,
)


def fixture(kind="point"):
    avl = (
        [ping("g0", 0), ping("g1", 10, 300), ping("g2", 20, 300)]
        if kind == "point"
        else [ping("g0", 0, 300), ping("g1", 60, 300)]
    )
    apc = [event("a", 10 if kind == "point" else 30)]
    decisions = run(avl, apc)
    return avl, apc, decisions, segmentation(avl)["ping_segment"]


@pytest.mark.parametrize("kind", ["point", "interpolation"])
def test_valid_counter_evidence_replays_without_failure(kind):
    avl, apc, decisions, mapping = fixture(kind)
    assert decisions[0]["counter_witnesses"]
    result = NegativeReplay(avl, apc, DEFAULT_CONFIG).validate(decisions, mapping)
    assert result["counts"]["counter_kind_" + kind] > 0
    assert all(not rule["failures"] for rule in result["rules"].values())


@pytest.mark.parametrize(
    ("kind", "field", "replacement", "rule"),
    [
        ("point", "ping_ids", ["invented-source-row"], "N01_source_identity"),
        ("point", "target_time", 0.0, "N03_endpoint_target"),
        ("point", "distance_m", 0.0, "N04_point_replay"),
        ("point", "lat", 0.0, "N04_point_replay"),
        ("point", "route_labels", [], "N07_lineage_context"),
        ("point", "measurement_keys", ["wrong-key"], "N07_lineage_context"),
        ("interpolation", "bracket_gap_s", 999.0, "N05_interpolation_replay"),
        ("interpolation", "distance_m", 0.0, "N05_interpolation_replay"),
    ],
)
def test_corrupt_counter_evidence_is_detected(kind, field, replacement, rule):
    avl, apc, decisions, mapping = fixture(kind)
    decisions[0]["counter_witnesses"][0][field] = replacement
    result = NegativeReplay(avl, apc, DEFAULT_CONFIG).validate(decisions, mapping)
    assert result["rules"][rule]["failures"] > 0


def test_counter_interpolation_across_frozen_chunks_is_detected():
    avl, apc, decisions, _ = fixture("interpolation")
    result = NegativeReplay(avl, apc, DEFAULT_CONFIG).validate(
        decisions, {"g0": "one", "g1": "two"}
    )
    assert result["rules"]["N06_interpolation_owner"]["failures"] > 0


def test_counter_interpolation_cannot_hide_a_differently_owned_measurement_alias():
    avl, apc, decisions, mapping = fixture("interpolation")
    avl.append(dict(avl[0], rid="g0-alias"))
    mapping["g0-alias"] = "different-owner"
    result = NegativeReplay(avl, apc, DEFAULT_CONFIG).validate(decisions, mapping)
    assert result["rules"]["N06_interpolation_owner"]["failures"] > 0


def test_missing_conflict_flag_and_incorrect_excursion_are_detected():
    avl, apc, decisions, mapping = fixture()
    decisions[0]["endpoint_conflicts"] = []
    decisions[0]["interval_excursion"] = not decisions[0]["interval_excursion"]
    result = NegativeReplay(avl, apc, DEFAULT_CONFIG).validate(decisions, mapping)
    assert result["rules"]["N08_conflict_state"]["failures"] > 0
    assert result["rules"]["N09_interval_excursion"]["failures"] > 0


def test_invalid_raw_timestamp_cannot_support_a_counter_witness():
    avl, apc, decisions, mapping = fixture()
    avl[1]["time_ambiguous"] = True
    result = NegativeReplay(avl, apc, DEFAULT_CONFIG).validate(decisions, mapping)
    assert result["rules"]["N02_source_quality"]["failures"] > 0


def test_replay_does_not_mutate_inputs_or_decisions():
    avl, apc, decisions, mapping = fixture()
    original = copy.deepcopy((avl, apc, decisions, mapping))
    NegativeReplay(avl, apc, DEFAULT_CONFIG).validate(decisions, mapping)
    assert (avl, apc, decisions, mapping) == original


@pytest.mark.parametrize(
    ("seconds", "meters", "conflict"), [(10, 300, False), (5, 400, True)]
)
def test_point_counter_distinguishes_permitted_motion_from_exceeding_envelope(
    seconds, meters, conflict
):
    avl = [
        ping("g0", 0),
        ping("g1", seconds, meters),
        ping("g2", seconds * 2, meters * 2),
    ]
    apc = [event("a", 0)]
    decisions = run(avl, apc)
    assert bool(decisions[0]["counter_witnesses"]) is conflict
    if not conflict:
        assert decisions[0]["status"] == "direct"
    result = NegativeReplay(avl, apc, DEFAULT_CONFIG).validate(
        decisions, segmentation(avl)["ping_segment"]
    )
    assert all(not rule["failures"] for rule in result["rules"].values())


def test_replay_rejects_legacy_counter_explainable_by_permitted_motion():
    avl = [ping("g0", 0), ping("g1", 10, 300), ping("g2", 20, 600)]
    apc = [event("a", 0)]
    decisions = run(avl, apc)
    replay = NegativeReplay(avl, apc, DEFAULT_CONFIG)
    group = replay.by_rid["g1"]
    decisions[0].update(
        status="ambiguous",
        endpoint_conflicts=["arrival"],
        counter_witnesses=[
            {
                "kind": "point",
                "ping_ids": ["g1"],
                "measurement_keys": [group["key"]],
                "support_time": group["t"],
                "target": "arrival",
                "target_time": replay.by_rid["g0"]["t"],
                "gap_s": 10,
                "distance_m": 300,
                "bracket_gap_s": 0,
                "lat": group["lat"],
                "lon": group["lon"],
                "route_labels": ["10"],
            }
        ],
    )
    result = replay.validate(decisions, segmentation(avl)["ping_segment"])
    assert result["rules"]["N04_point_replay"]["failures"] == 1
    assert all(
        not result["rules"][rule]["failures"]
        for rule in result["rules"]
        if rule != "N04_point_replay"
    )


@pytest.mark.parametrize("corruption", [None, "hash", "missing_locator_entry"])
def test_composite_locator_audits_inherited_assignments_and_rejects_bad_inputs(
    tmp_path, corruption
):
    avl, apc, decisions, _ = fixture()
    run_root, audit, baseline = (
        tmp_path / "final",
        tmp_path / "raw",
        tmp_path / "frozen",
    )
    for path in (run_root, audit, baseline):
        path.mkdir()
    pl.from_dicts(avl).write_parquet(audit / "avl.parquet")
    pl.from_dicts(apc).write_parquet(audit / "apc.parquet")
    for name, value in {
        "manifest": {"audit": str(audit), "baseline": str(baseline)},
        "config": DEFAULT_CONFIG,
        "completion": {"all_vehicles_executed_with_final_core": False},
        "selection": [{"raw_vehicle": avl[0]["vehicle"], "vehicle_case": "V-test"}],
    }.items():
        (run_root / f"{name}.json").write_text(json.dumps(value))
    entries = []
    for method in ("gap_only", "avl_context", "apc_assisted"):
        chunk_dir = baseline / "chunks" / method
        chunk_dir.mkdir(parents=True)
        pl.from_dicts(
            [{"segment_id": "chunk", "ping_ids": [r["rid"] for r in avl]}]
        ).write_parquet(chunk_dir / "V-test.parquet")
        assignment = tmp_path / f"inherited-{method}.parquet"
        pl.from_dicts(decisions).write_parquet(assignment)
        entries.append(
            {
                "vehicle_case": "V-test",
                "segmentation": method,
                "mode": "reused",
                "apc_rows": len(apc),
                "assignment_path": str(assignment),
                "assignment_sha256": sha256(assignment),
            }
        )
    if corruption == "hash":
        entries[0]["assignment_sha256"] = "0" * 64
    elif corruption == "missing_locator_entry":
        entries.pop()
    (run_root / "assignment_locator.json").write_text(json.dumps(entries))
    args = Namespace(run=run_root, out=tmp_path / "replay")
    if corruption:
        with pytest.raises(ValueError):
            execute(args)
    else:
        assert execute(args) == 0
        result = json.loads((args.out / "negative_evidence_replay.json").read_text())
        assert result["assignment_source_modes"] == {"reused": 3}
        assert result["all_vehicles_executed_with_final_core"] is False
        assert all(v["counts"]["rows"] == len(apc) for v in result["rules"].values())
