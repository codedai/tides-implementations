"""Daily overlays are compact stored evidence, not new inferred trajectories."""

import copy
from pathlib import Path

import pytest

from tools.merge_review.gtfs_matching.adapter import GTFSReviewData, public_gtfs
from tools.merge_review.gtfs_matching.overview import (
    build_day_overview,
    mode_entry,
    overview_case,
    select_overview_candidate,
)


def candidate(name="trip", score=1, **extra):
    return {
        "candidate_id": f"feed|2023-11-06|{name}",
        "trip_id": name,
        "reference_id": "south-a",
        "score": score,
        "support_score": 0.7,
        "coverage": 0.9,
        "schedule_supported_window_weight_ratio": 0.9,
        "schedule_aligned_point_ratio": 0.9,
        "schedule_aligned_duration_ratio": 0.9,
        "whole_alignment": {"status": "EVIDENCE"},
        "estimated_delay_s": 600,
        "scheduled_trajectory": [
            {"arrival_t": 100, "departure_t": 110, "s_m": 0, "stop_id": "a"},
            {"arrival_t": 200, "departure_t": 230, "s_m": 1000, "stop_id": "b"},
        ],
        **extra,
    }


def point(rid, t, s, ref="south-a", **extra):
    return {
        "source": "avl",
        "rid": rid,
        "t": t,
        "s_m": s,
        "reference_id": ref,
        "fragment_id": "service",
        "quality": 1,
        "projection_status": "ON_REFERENCE",
        **extra,
    }


def fixture():
    good = candidate()
    points = [point("a", 100, 0), point("b", 110, 100), point("c", 120, 200)]
    result = {
        "status": "AMBIGUOUS",
        "candidates": [good],
        "all_candidates": [good],
        "windows": {"windows": [{"id": "global-window"}]},
    }
    artifact = {
        "config": {},
        "projection": {"points": points},
        "apc_evidence": [],
        "result": result,
        "matching_modes": {
            "global": {"result": result, "selection": {"mode": "global"}},
            "early": {
                "result": {
                    "status": "PATTERN_ONLY",
                    "candidates": [],
                    "all_candidates": [candidate("early-failed", score=4)],
                    "windows": {"windows": [{"id": "early-window"}]},
                },
                "selection": {
                    "mode": "early",
                    "selected_avl_ids": ["avl|a"],
                    "selected_valid_avl_ids": ["avl|a"],
                    "selected_apc_ids": ["apc|p"],
                    "time_ranges": [{"start": 100, "end": 110, "end_inclusive": False}],
                },
            },
        },
    }
    canonical = {
        "references": [
            {
                "reference_id": "south-a",
                "direction_label": "SOUTH",
                "length_m": 1000,
                "canonical_shape_id": "a",
            },
            {
                "reference_id": "south-b",
                "direction_label": "SOUTH",
                "length_m": 1200,
                "canonical_shape_id": "b",
            },
            {
                "reference_id": "north",
                "direction_label": "NORTH",
                "length_m": 1300,
                "canonical_shape_id": "c",
            },
        ],
        "validations": [],
        "diagnostics": [],
    }
    summary = {"id": "case", "direction": "SOUTH", "start": 100, "end": 200}
    return artifact, canonical, summary


def test_lowest_cost_actual_all_gates_pass_wins_even_outside_formal_cap():
    failed = candidate("formal-failed", score=0.1, coverage=0.1)
    valid = candidate("valid-outside-cap", score=2)
    another = candidate("valid-slower", score=2.5)
    result = {
        "status": "AMBIGUOUS",
        "candidates": [failed],
        "all_candidates": [failed, another, valid],
    }
    snapshot = copy.deepcopy(result)
    chosen = select_overview_candidate(result)
    assert chosen["trip_id"] == "valid-outside-cap"
    assert chosen["source"] == "accepted"
    assert chosen["failed_gate_checks"] == []
    assert chosen["trip_identity_confirmed"] is False
    assert chosen["probability"] is None
    assert [k["t"] for k in chosen["trajectory"]] == [100, 110, 200, 230]
    assert chosen["estimated_delay_s"] == 600  # The delay never shifts these knots.
    assert result == snapshot


def test_real_gate_failure_is_fallback_even_if_already_in_formal_list():
    first = candidate("formal-rejected", 4)
    second = candidate("other-rejected", 5)
    chosen = select_overview_candidate(
        {
            "status": "PATTERN_ONLY",
            "candidates": [first],
            "all_candidates": [second, first],
        }
    )
    assert chosen["trip_id"] == "formal-rejected"
    assert chosen["source"] == "fallback"
    assert chosen["diagnostic_only"] is True
    assert [check["metric"] for check in chosen["failed_gate_checks"]] == ["score"]


def test_custom_saved_gates_and_nonfinite_scores_fail_closed():
    assert select_overview_candidate({"all_candidates": []}) is None
    assert (
        select_overview_candidate({"all_candidates": [candidate(score=float("nan"))]})
        is None
    )
    chosen = select_overview_candidate(
        {"all_candidates": [candidate(score=2)]}, {"aggregation_match_cost_max": 1.5}
    )
    assert chosen["source"] == "fallback"
    assert chosen["failed_gate_checks"][0]["threshold"] == 1.5


def test_partial_result_and_windows_are_selected_but_projection_remains_full():
    artifact, canonical, _ = fixture()
    snapshot = copy.deepcopy(artifact)
    detail = public_gtfs(artifact, canonical, "early")
    assert detail["matching_mode"] == "early"
    assert detail["available_matching_modes"] == ["global", "early"]
    assert detail["result"]["status"] == "PATTERN_ONLY"
    assert detail["windows"]["windows"][0]["id"] == "early-window"
    assert [p["rid"] for p in detail["projection"]["points"]] == ["a", "b", "c"]
    assert detail["decision_selection"]["selected_valid_avl_ids"] == ["avl|a"]
    assert artifact == snapshot


@pytest.mark.parametrize("mode", ["middle", "late", "unknown", "", None])
def test_uncomputed_or_invalid_mode_is_never_silently_global(mode):
    artifact, _, _ = fixture()
    with pytest.raises(ValueError):
        mode_entry(artifact, mode)


def test_legacy_global_is_preserved_and_partial_fails_explicitly():
    artifact, _, _ = fixture()
    del artifact["matching_modes"]
    assert mode_entry(artifact)["result"] is artifact["result"]
    assert mode_entry(artifact)["selection"]["status"] == "LEGACY_GLOBAL"
    with pytest.raises(ValueError, match="historical version"):
        mode_entry(artifact, "early")


def test_overview_never_connects_reference_switch_rejections_or_time_gap():
    artifact, canonical, summary = fixture()
    artifact["result"].update(candidates=[], all_candidates=[])
    artifact["projection"]["points"] = [
        point("a", 100, 0),
        point("b", 110, 100),
        point("bad", 120, None, projection_status="OFF_REFERENCE"),
        point("c", 130, 200),
        point("d", 140, 300, ref="south-b"),
        point("e", 300, 400, ref="south-b"),
        point("f", 310, 500, ref="south-b", scope_separator_before=True),
    ]
    record = overview_case(artifact, summary, "v", "9028", canonical)
    assert [[p[2] for p in group["points"]] for group in record["fragments"]] == [
        ["a", "b"],
        ["c"],
        ["d"],
        ["e"],
        ["f"],
    ]
    assert record["candidate"] is None
    assert record["candidate_source"] == "none"
    assert record["omitted_valid_primary_point_count"] == 0
    assert record["off_reference_count"] == 1


def test_candidate_reference_alternatives_and_unprojectable_context_are_kept():
    artifact, canonical, summary = fixture()
    artifact["projection"]["points"] = [
        point(
            "a",
            100,
            50,
            ref="south-b",
            projection_alternatives=[
                {
                    "reference_id": "south-a",
                    "s_m": 100,
                    "projection_status": "ON_REFERENCE",
                    "quality": 1,
                }
            ],
        ),
        point(
            "b",
            110,
            60,
            ref="south-b",
            projection_alternatives=[
                {
                    "reference_id": "south-a",
                    "s_m": None,
                    "projection_status": "OFF_REFERENCE",
                    "quality": 0,
                }
            ],
        ),
        point(
            "c",
            120,
            70,
            ref="south-b",
            projection_alternatives=[
                {
                    "reference_id": "south-a",
                    "s_m": 200,
                    "projection_status": "ON_REFERENCE",
                    "quality": 1,
                }
            ],
        ),
    ]
    record = overview_case(artifact, summary, "v", "9028", canonical)
    assert [f["reference_id"] for f in record["fragments"]] == [
        "south-a",
        "south-a",
        "south-b",
    ]
    assert record["fragments"][-1]["context_only"]
    assert record["fragments"][-1]["points"][0][2] == "b"
    assert record["displayed_projection_point_count"] == 3
    assert record["omitted_valid_primary_point_count"] == 0


def test_apc_intervals_are_not_points_and_selected_span_is_clipped():
    artifact, canonical, summary = fixture()
    artifact["apc_evidence"] = [
        {
            "source": "apc",
            "rid": "p",
            "arrival": 105,
            "departure": 115,
            "projection_status": "STOP_INTERVAL_ONLY",
            "boarding": 2,
            "alighting": 1,
            "canonical_stop_intervals": [
                {"reference_id": "south-a", "s_min_m": 100, "s_max_m": 100}
            ],
            "projection_alternatives": [
                {"reference_id": "south-a", "projection_status": "STOP_INTERVAL_ONLY"}
            ],
        }
    ]
    snapshot = copy.deepcopy(artifact)
    record = overview_case(artifact, summary, "v", "9028", canonical, "early")
    assert [p[3] for p in record["fragments"][0]["points"]] == [True, False, False]
    apc = record["apc_intervals"][0]
    assert (apc["start"], apc["end"], apc["s"]) == (105, 115, 100)
    assert apc["selected_intervals"] == [{"start": 105, "end": 110}]
    assert apc["trajectory_fabricated"] is False
    assert apc["boarding"] == 2
    assert all(p[2] != "p" for f in record["fragments"] for p in f["points"])
    assert artifact == snapshot


def test_empty_projection_apc_case_is_not_omitted_from_day():
    artifact, canonical, summary = fixture()
    artifact["projection"]["points"] = []
    artifact["result"].update(candidates=[], all_candidates=[], status="PATTERN_ONLY")
    record = overview_case(artifact, summary, "v", "9028", canonical)
    day = build_day_overview(
        [record],
        canonical,
        run_id="run",
        day="2023-11-06",
        matching_mode="global",
        available_matching_modes=["global"],
    )
    assert day["summary"]["case_count"] == 1
    assert day["summary"]["candidate_sources"] == {"none": 1}
    assert len(day["reference_facets"]) == 3
    assert [f["shape_id"] for f in day["reference_facets"]] == ["a", "b", "c"]
    assert day["cases"][0]["fragments"] == []


def test_saved_overview_is_hash_checked_cached_and_never_loads_raw_detail(monkeypatch):
    from tools.merge_review.gtfs_matching import adapter

    data = GTFSReviewData.__new__(GTFSReviewData)
    data.run = Path("/tmp/overview-fixture")
    data.manifest = {"available_matching_modes": ["global", "early"]}
    data.output_hashes = {"overview/early.json": "expected"}
    data._overview_cache = {}
    data._verify_parent = lambda: None
    checks, reads = [], []
    data._verify_file = lambda path, digest: checks.append((path, digest))
    payload = {"run_id": "overview-fixture", "matching_mode": "early", "cases": []}
    monkeypatch.setattr(
        adapter, "read", lambda path: reads.append(path) or copy.deepcopy(payload)
    )
    first = data.day_overview("early")
    first["cases"].append("client mutation")
    assert data.day_overview("early")["cases"] == []
    assert len(reads) == 1 and len(checks) == 2
    with pytest.raises(ValueError):
        data.day_overview("late")


def test_catalog_mode_summaries_do_not_change_saved_global_rows():
    data = GTFSReviewData.__new__(GTFSReviewData)
    data.manifest = {"available_matching_modes": ["global", "early"]}
    data.vehicles = {"v": "9028"}
    data._verify_parent = lambda: None
    data._catalog = {
        "v": [
            {
                "id": "case",
                "gtfs_status": "MATCHED",
                "matching_modes": {
                    "early": {"gtfs_status": "PATTERN_ONLY", "gtfs_candidate_count": 0}
                },
            }
        ]
    }
    from tools.merge_review.gtfs_matching import METHOD

    assert data.catalog(METHOD, "v", "early")[0]["gtfs_status"] == "PATTERN_ONLY"
    assert data.catalog(METHOD, "v")[0]["gtfs_status"] == "MATCHED"
    assert data._catalog["v"][0]["gtfs_status"] == "MATCHED"
