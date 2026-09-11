"""Robustness-harness tests, separate from method accuracy or ground truth."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

import pytest

from experiments.apc_avl_direct_evidence.probes import (
    compare_links,
    evaluate,
    normalized_decisions,
    project_frozen_segmentation,
    read_frozen_selections,
    refine_segments,
    replay_witnesses,
    synthetic_cases,
)


def test_projection_freezes_owners_but_does_not_keep_removed_source_support():
    base = datetime(2024, 1, 1)
    original = {
        "segments": [
            {
                "segment_id": "one",
                "ping_ids": ["a", "b", "c"],
                "start": base,
                "end": base + timedelta(minutes=2),
            }
        ],
        "ping_segment": {"a": "one", "b": "one", "c": "one"},
    }
    retained = [
        {"rid": "b", "time": base + timedelta(minutes=1)},
        {"rid": "copy", "time": base + timedelta(minutes=1)},
    ]
    projected = project_frozen_segmentation(original, retained, {"copy": "b"})
    assert projected["ping_segment"] == {"b": "one", "copy": "one"}
    assert projected["segments"][0]["start"] == retained[0]["time"]
    assert projected["segments"][0]["end"] == retained[0]["time"]
    assert original["segments"][0]["ping_ids"] == ["a", "b", "c"]


def test_frozen_selection_reads_datetime_without_changing_selection(tmp_path):
    path = tmp_path / "selection.json"
    row = {
        "vehicle": "v",
        "vehicle_case": "V-hash",
        "window_hash": "abc",
        "window_start": "2024-01-01 00:00:00",
        "window_end": "2024-01-01 02:00:00",
    }
    path.write_text(json.dumps([row]))
    before = path.read_bytes()
    selected, hashes = read_frozen_selections([path, path])
    assert len(selected) == 1
    assert selected[0]["window_start"] == datetime(2024, 1, 1)
    assert hashes[0]["sha256"] == hashes[1]["sha256"]
    assert path.read_bytes() == before


def test_compare_never_labels_algorithm_retention_as_accuracy():
    old = [
        {"apc_rid": "a", "status": "direct", "segment_id": "x"},
        {"apc_rid": "b", "status": "direct", "segment_id": "x"},
    ]
    new = [
        {"apc_rid": "a", "status": "ambiguous", "segment_id": None},
        {"apc_rid": "b", "status": "direct", "segment_id": "y"},
    ]
    summary = compare_links(old, new)
    assert summary["direct_to_abstention"] == 1
    assert summary["both_direct_owner_switch"] == 1
    assert summary["independent_accuracy"] is None


def test_normalized_decisions_project_duplicate_ids_and_ignore_input_order():
    before = [
        {"apc_rid": "a", "status": "direct", "ping_ids": ["p"]},
        {"apc_rid": "b", "status": "unmatched", "ping_ids": []},
    ]
    after = [dict(before[1]), dict(before[0], ping_ids=["p", "duplicate"])]
    assert normalized_decisions(before) == normalized_decisions(
        after, {"duplicate": "p"}
    )


def test_synthetic_cases_include_within_owner_visit_competition():
    cases = synthetic_cases()
    competing = cases["same_chunk_repeated_near_stop_episodes"]
    assert len(competing["segmentation"]["segments"]) == 1
    assert len(cases["same_stop_opposite_direction_overlap"]["apc"]) == 2
    assert len(cases) == 6


def test_replay_catches_fabricated_point_position_from_real_source_ref():
    case = synthetic_cases()["long_dwell_continuously_near"]
    ping = case["avl"][0]
    event = case["apc"][0]
    witness = {
        "kind": "point",
        "ping_ids": [ping["rid"]],
        "support_time": ping["time"],
        "target": "arrival",
        "target_time": event["arrival"],
        "gap_s": 0,
        "distance_m": 0,
        "lat": ping["lat"] + 0.1,
        "lon": ping["lon"],
    }
    replay = replay_witnesses(
        case["avl"],
        case["apc"],
        [{"apc_rid": event["rid"], "status": "partial", "witnesses": [witness]}],
    )
    assert not replay["passed"]
    assert replay["failures"] == [{"apc_rid": event["rid"], "check": "stored_position"}]


def test_replay_accepts_only_bounded_real_interpolation_sources():
    case = synthetic_cases()["short_interpolation_without_point_inside_radius"]
    event = case["apc"][0]
    witness = {
        "kind": "interpolation",
        "ping_ids": [r["rid"] for r in case["avl"]],
        "support_time": event["arrival"],
        "target": "arrival",
        "target_time": event["arrival"],
        "gap_s": 0,
        "distance_m": 0,
        "bracket_gap_s": 60,
        "lat": event["lat"],
        "lon": event["lon"],
    }
    links = [{"apc_rid": event["rid"], "status": "direct", "witnesses": [witness]}]
    assert replay_witnesses(case["avl"], case["apc"], links)["passed"]
    witness["bracket_gap_s"] = 1
    assert not replay_witnesses(case["avl"], case["apc"], links)["passed"]


def test_owner_refinement_keeps_every_source_record_and_physical_instant():
    case = synthetic_cases()["long_dwell_continuously_near"]
    refined = refine_segments(case["segmentation"], case["avl"])
    assert len(refined["segments"]) == len(case["avl"])
    assert set(refined["ping_segment"]) == set(case["segmentation"]["ping_segment"])
    assert all(row["start"] == row["end"] for row in refined["segments"])


@pytest.mark.parametrize(
    ("case_name", "status", "support_level"),
    [
        ("long_dwell_single_accidental_near_ping", "ambiguous", None),
        ("long_dwell_continuously_near", "direct", "bounded_interval"),
        ("same_chunk_repeated_near_stop_episodes", "ambiguous", None),
        ("short_interpolation_without_point_inside_radius", "direct", "instant"),
        ("internal_missing_gps_not_a_fabricated_observation", "unmatched", None),
    ],
)
def test_constructed_support_contract_does_not_relabel_reference_as_truth(
    case_name, status, support_level
):
    case = synthetic_cases()[case_name]
    outputs = evaluate(case["avl"], case["apc"], case["segmentation"])
    assert outputs["v2"][0]["status"] == status
    assert outputs["v2"][0]["support_level"] == support_level
    assert replay_witnesses(case["avl"], case["apc"], outputs["v2"])["passed"]


def test_shared_terminal_keeps_local_evidence_without_certifying_visit_roles():
    case = synthetic_cases()["same_stop_opposite_direction_overlap"]
    links = evaluate(case["avl"], case["apc"], case["segmentation"])["v2"]
    assert len(links) == len(case["apc"]) == 2
    for link in links:
        assert link["status"] == "direct"
        assert "same_stop_opposite_direction_overlap" in link["overlap_flags"]
        assert not link["trip_identity_verified"]


@pytest.mark.parametrize("case_name", sorted(synthetic_cases()))
def test_constructed_v2_decisions_ignore_permutation_and_duplicate_measurement_votes(
    case_name,
):
    case = synthetic_cases()[case_name]
    reference = evaluate(case["avl"], case["apc"], case["segmentation"])["v2"]
    origins = {row["rid"] + ":duplicate": row["rid"] for row in case["avl"]}
    avl = case["avl"] + [
        dict(row, rid=row["rid"] + ":duplicate") for row in case["avl"]
    ]
    apc = list(case["apc"])
    rng = random.Random(42)
    rng.shuffle(avl)
    rng.shuffle(apc)
    projected = project_frozen_segmentation(case["segmentation"], avl, origins)
    changed = evaluate(avl, apc, projected)["v2"]
    assert normalized_decisions(reference) == normalized_decisions(changed, origins)


def test_real_point_evidence_survives_pure_owner_refinement():
    case = synthetic_cases()["long_dwell_continuously_near"]
    original = evaluate(case["avl"], case["apc"], case["segmentation"])["v2"][0]
    refined = refine_segments(case["segmentation"], case["avl"])
    changed = evaluate(case["avl"], case["apc"], refined)["v2"][0]
    assert original["status"] == changed["status"] == "direct"
    assert original["support_level"] == changed["support_level"] == "bounded_interval"
    assert original["ping_ids"] == changed["ping_ids"]
    assert changed["segment_id"] is None
    assert changed["context_status"] == "multiple_segments"


def test_owner_refinement_may_remove_derived_cross_owner_interpolation():
    case = synthetic_cases()["short_interpolation_without_point_inside_radius"]
    original = evaluate(case["avl"], case["apc"], case["segmentation"])["v2"][0]
    refined = refine_segments(case["segmentation"], case["avl"])
    changed = evaluate(case["avl"], case["apc"], refined)["v2"][0]
    assert original["status"] == "direct"
    assert original["support_kind"] == "interpolation"
    assert changed["status"] == "unmatched"
