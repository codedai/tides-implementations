"""HTTP compaction does not discard observations or hide evidence windows."""

import copy

from tools.merge_review.gtfs_matching.ui_payload import compact_gtfs_detail


def test_compaction_preserves_raw_rejections_windows_and_candidate_trajectories():
    raw = {
        "source": "avl",
        "rid": "v",
        "lat": 43.6,
        "lon": -79.4,
        "source_fields": {"Vehicle": "00123", "Heading": "#N/A"},
    }
    point = {
        **raw,
        "status": "OFF_REFERENCE",
        "s": None,
        "fragment_id": None,
        "projection_alternatives": [{"reference_id": "r", "lateral_m": 150}],
        "rejection_reason": "ALL_REFERENCE_PATHS_FAILED",
    }
    window = {
        "id": "w",
        "start": 60,
        "end": 120,
        "status": "NO_EVIDENCE",
        "quality": {"point_count": 0},
        "observation_ids": ["avl|v"],
        "weights": {"avl|v": 0.1},
        "quality_by_reference": {"r": {"status": "NO_EVIDENCE"}},
    }
    candidate = {
        "candidate_id": "f|d|trip",
        "reference_id": "r",
        "probability": None,
        "trajectory": [
            {"t": 100, "s": 20, "kind": "arrival"},
            {"t": 130, "s": 20, "kind": "departure"},
        ],
    }
    detail = {
        "case": {"id": "case"},
        "provenance": {"run_path": "/tmp/run"},
        "observations": [raw],
        "avl": [raw],
        "apc": [],
        "gtfs": {
            "intermediate_artifact": "/tmp/run/cases/original.json.gz",
            "result": {
                "candidates": [candidate],
                "all_candidates": [candidate],
                "local_scores": {"big": [1] * 100},
                "retrieval": {"trips": [1], "windows": [1]},
            },
            "candidates": [candidate],
            "windows": {"windows": [window], "atomic": [window]},
            "projection": {
                "points": [point],
                "by_reference": {"r": [point]},
                "apc_evidence": [1],
            },
            "apc_evidence": [],
            "references": [],
        },
    }
    original = copy.deepcopy(detail)
    compact = compact_gtfs_detail(detail)
    assert compact["observations"] == original["observations"]
    assert compact["gtfs"]["projection"]["points"][0]["status"] == "OFF_REFERENCE"
    assert compact["gtfs"]["projection"]["points"][0]["projection_alternatives"]
    assert compact["gtfs"]["candidates"] == original["gtfs"]["candidates"]
    windows = compact["gtfs"]["windows"]["windows"]
    assert len(windows) == 1 and windows[0]["id"] == "w"
    assert windows[0]["status"] == "NO_EVIDENCE"
    assert "weights" not in windows[0] and "observation_ids" not in windows[0]
    assert "local_scores" not in compact["gtfs"]["result"]
    assert compact["gtfs"]["result"]["retained_candidate_count"] == 1
    assert compact["gtfs"]["intermediate_artifact"].endswith("original.json.gz")


def test_legacy_payload_is_not_changed():
    detail = {"observations": [{"rid": "v"}]}
    assert compact_gtfs_detail(detail) is detail


def test_http_compaction_exposes_failed_scores_without_promoting_final_candidates():
    rejected = {
        "candidate_id": "feed|2023-11-06|rejected",
        "trip_id": "rejected",
        "score": 2.5,
        "support_score": 0.08,
        "coverage": 0.99,
        "schedule_supported_window_weight_ratio": 0.92,
        "schedule_aligned_point_ratio": 0.03125,
        "schedule_aligned_duration_ratio": 0.02965,
        "whole_alignment": {"status": "EVIDENCE"},
        "reference_id": "south",
        "scheduled_trajectory": [
            {"arrival_t": 100, "departure_t": 130, "s_m": 20, "stop_id": "a"}
        ],
    }
    saved_result = {
        "status": "PATTERN_ONLY",
        "candidates": [],
        "trip_confidence": 0,
        "reasons": ["WHOLE_TRIP_ALIGNMENT_NOT_VALIDATED"],
        "all_candidates": [rejected],
    }
    snapshot = copy.deepcopy(saved_result)
    detail = {
        "case": {"id": "case"},
        "provenance": {"run_path": "/tmp/run"},
        "gtfs": {
            "result": copy.deepcopy(saved_result),
            "candidates": [],
            "config": {"aggregation_min_coverage": 0.4},
        },
    }
    public = compact_gtfs_detail(detail)["gtfs"]
    assert public["candidates"] == public["result"]["candidates"] == []
    for key in ("status", "trip_confidence", "reasons"):
        assert public["result"][key] == saved_result[key]
    assert "all_candidates" not in public["result"]
    assert public["result"]["retained_candidate_count"] == 1
    diagnostic = public["diagnostic_candidates"][0]
    assert diagnostic["trip_id"] == "rejected"
    assert diagnostic["diagnostic_only"] is True
    assert diagnostic["probability"] is None
    assert len(diagnostic["failed_gate_checks"]) == 2
    assert all(c["threshold"] == 0.4 for c in diagnostic["failed_gate_checks"])
    assert [(k["t"], k["s"]) for k in diagnostic["trajectory"]] == [
        (100, 20),
        (130, 20),
    ]
    assert saved_result == snapshot
