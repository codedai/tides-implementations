"""Experiment 03 scope, API aliases and safe configuration contracts."""

import copy
from datetime import datetime

import pytest

from tools.merge_review.debug import validate_config
from tools.merge_review.gtfs_matching import METHOD
from tools.merge_review.gtfs_matching.adapter import preserve_source_breaks, public_gtfs
from tools.merge_review.gtfs_matching.runner import scope_vehicle
from tools.merge_review.tests.test_direction_runner import (
    direction_workspace,  # noqa: F401
)


def test_gtfs_config_is_pinned_and_does_not_accept_paths():
    result = validate_config({"method": METHOD})
    assert result["gtfs"]["feed_id"] == "ttc-d092742e6a5b"
    assert result["gtfs"]["feed_allow_candidate"] is True
    assert result["gtfs"]["max_projection_distance_m"] == 100.0
    assert result["gtfs"]["segmentation_joint_gap_s"] == 1800.0
    assert result["segmentation"] == result["merge"] == {}
    for change in (
        {"feed_gtfs_root": "/tmp"},
        {"feed_id": "another"},
        {"window_context_s": [30]},
        {"aggregation_output_max": 4},
        {"score_progress_scale_m": 0},
        {"retrieval_top_k": True},
        {"segmentation_joint_gap_s": 0},
        {"segmentation_joint_gap_s": True},
        {"segmentation_joint_gap_s": float("nan")},
    ):
        with pytest.raises(ValueError):
            validate_config({"method": METHOD, "gtfs": change})
    with pytest.raises(ValueError):
        validate_config({"method": METHOD, "merge": {"radius_m": 100}})


def test_route_29_scope_keeps_parent_boundary_and_splits_omitted_929():
    t = datetime(2023, 11, 6, 13)
    rows = [
        dict(
            source="avl",
            rid=f"v:{i}",
            route=route,
            event_time=t,
            candidate_segment_ids=["direction:v:0000"],
            direction_conflict_ids=[],
        )
        for i, route in enumerate(("29", "929", "29"))
    ]
    original = copy.deepcopy(rows)
    meta = {
        "segments": [
            {
                "id": "direction:v:0000",
                "start": t.isoformat(),
                "end": datetime(2023, 11, 6, 14).isoformat(),
                "direction": "NORTH",
            }
        ]
    }
    selected, excluded, members, catalog = scope_vehicle(rows, meta, "v", "vehicle-v")
    assert rows == original
    assert [r["rid"] for r in selected] == ["v:0", "v:2"]
    assert [r["rid"] for r in excluded] == ["v:1"]
    assert selected[1]["scope_separator_before"]
    assert not selected[0]["scope_separator_before"]
    assert list(members) == ["direction:v:0000"]
    assert catalog[0]["parent_route_929_influence_possible"]
    assert catalog[0]["end"] - catalog[0]["start"] == 3600


def test_apc_only_unresolved_rows_are_retained_not_invented_as_avl():
    rows = [
        dict(
            source="apc",
            rid="p:1",
            route="29",
            event_time=datetime(2023, 11, 6),
            candidate_segment_ids=[],
            direction_conflict_ids=[],
        )
    ]
    selected, _, _, catalog = scope_vehicle(rows, {}, "v", "vehicle-v")
    assert selected == rows
    assert catalog[0]["kind"] == "direction_unresolved"
    assert catalog[0]["avl_count"] == 0
    assert catalog[0]["apc_count"] == 1


def test_route_filter_preserves_existing_parent_visual_breaks():
    from tools.merge_review.data import _decorate_selected_progress

    rows = [
        dict(
            rid=str(i),
            t=100 + 10 * i,
            lat=43.65 + i * 0.0001,
            lon=-79.4,
            route="929" if i == 3 else "29",
            break_before=i == 2,
        )
        for i in range(5)
    ]
    positions = preserve_source_breaks(rows)
    selected = [r for r in rows if r["route"] == "29"]
    _decorate_selected_progress(selected, positions)
    assert selected[0]["break_before"]
    assert not selected[1]["break_before"]
    assert selected[2]["break_before"]  # Parent PI/PO/GPS separator survived.
    assert selected[3]["break_before"]  # Omitted 929 record is a new separator.


def test_public_gtfs_preserves_dwell_and_never_borrows_another_reference_gate():
    artifact = {
        "config": {},
        "projection": {
            "points": [
                {
                    "rid": 2**63 + 1,
                    "t": 100,
                    "s_m": None,
                    "projection_status": "OFF_REFERENCE",
                    "lateral_m": 101,
                }
            ]
        },
        "result": {
            "candidates": [
                {
                    "trip_id": "t",
                    "feed_version": "f",
                    "service_date": "d",
                    "reference_id": "a",
                    "probability": None,
                    "scheduled_trajectory": [
                        {
                            "arrival_t": 100,
                            "departure_t": 130,
                            "s_m": 250,
                            "stop_id": "stop",
                        }
                    ],
                }
            ]
        },
        "apc_evidence": [
            {
                "rid": "apc",
                "arrival": 110,
                "departure": 120,
                "projection_status": "ON_REFERENCE",
                "canonical_stop_intervals": [
                    dict(
                        reference_id=r,
                        stop_id="stop",
                        s_min_m=250,
                        s_max_m=250,
                        ambiguous=False,
                    )
                    for r in ("a", "b")
                ],
                "projection_alternatives": [
                    {"reference_id": "a", "projection_status": "ON_REFERENCE"},
                    {"reference_id": "b", "projection_status": "OFF_REFERENCE"},
                ],
            }
        ],
    }
    canonical = {"references": [], "validations": [], "diagnostics": {}}
    original = copy.deepcopy(artifact)
    result = public_gtfs(artifact, canonical)
    assert artifact == original
    assert [
        (p["t"], p["s"], p["kind"]) for p in result["candidates"][0]["trajectory"]
    ] == [(100, 250, "arrival"), (130, 250, "departure")]
    assert result["projection"]["points"][0]["status"] == "OFF_REFERENCE"
    assert result["apc_evidence"][0]["s"] == 250
    assert result["apc_evidence"][1]["s"] is None
    assert result["apc_evidence"][1]["status"] == "OFF_REFERENCE"
    assert result["candidates"][0]["probability"] is None


def test_runner_persists_scope_and_adapter_is_read_only(request, monkeypatch):
    import json

    from tools.merge_review.gtfs_matching import canonical, feed
    from tools.merge_review.gtfs_matching.adapter import load_review_data

    workspace = request.getfixturevalue("direction_workspace")
    parent = workspace.create_run({"method": "direction_destination"}, label="test 02")
    parent_path = workspace.get_run(parent["run_id"])
    original = {p.name: p.read_bytes() for p in parent_path.glob("*.json")}
    points = canonical.geometry(
        [{"lat": 43.65, "lon": -79.4}, {"lat": 43.66, "lon": -79.4}], (43.65, -79.4)
    )
    ref = dict(
        reference_id="r",
        family_id="f",
        feed_version="test",
        route_id="29",
        direction_label="NORTH",
        points=points,
        origin=(43.65, -79.4),
        length_m=points[-1]["s_m"],
        stop_positions=[],
    )
    source = workspace.audit / "avl.parquet"
    digest = feed.file_sha256(source)
    monkeypatch.setattr(
        feed,
        "load_feed",
        lambda *a, **k: dict(
            feed_version="test",
            route_id="29",
            selection={"provenance_unreviewed": True},
            archive_sha256=digest,
            feed_path=str(source),
            manifest_path=str(source),
            manifest_sha256=digest,
            diagnostics={},
        ),
    )
    monkeypatch.setattr(
        canonical,
        "build_references",
        lambda *a, **k: dict(
            references=[ref],
            trips=[],
            validations=[],
            route_id="29",
            diagnostics={},
            direction_crosswalk=[],
        ),
    )
    created = workspace.create_run(
        {"method": METHOD, "gtfs": {"execution_workers": 1}},
        parent_run_id=parent["run_id"],
    )
    assert created["summary"]["validation_ok"]
    assert created["summary"]["observations"] == 7
    assert not any(created["summary"]["violations"].values())
    folder = workspace.get_run(created["run_id"])
    data = load_review_data(folder)
    assert data.metadata()["route_scope"] == ["29"]
    assert data.metadata()["probability_calibrated"] is False
    for vehicle in data.vehicles:
        for case in data.catalog(METHOD, vehicle):
            detail = data.case(METHOD, vehicle, case["id"])
            assert all(str(r["route"]) == "29" for r in detail["observations"])
            assert detail["case"]["id"] == case["id"]
            assert detail["gtfs"]["result"]["calibration"]["calibrated"] is False
            assert all(r["progress_m"] is None for r in detail["apc"])
            json.dumps(detail, allow_nan=False)
    assert {p.name: p.read_bytes() for p in parent_path.glob("*.json")} == original
    compared = workspace.compare(parent["run_id"], created["run_id"])
    assert compared["accuracy"] is None
    assert not any(compared["summary"]["violations"].values())
    assert all(
        c["avl_count"] + c["apc_count"] == len(c["source_rids"])
        for v in data.vehicles
        for c in data.catalog(METHOD, v)
    )
    from tools.merge_review.gtfs_matching import runner

    original_evaluate = runner.evaluate_segment

    def drop_apc(*args, **kwargs):
        projection, result = original_evaluate(*args, **kwargs)
        projection["apc_evidence"] = projection["apc_evidence"][1:]
        return projection, result

    monkeypatch.setattr(runner, "evaluate_segment", drop_apc)
    with pytest.raises(ValueError, match="invariant failure"):
        workspace.create_run(
            {"method": METHOD, "gtfs": {"execution_workers": 1}},
            parent_run_id=parent["run_id"],
        )
    failures = list((workspace.root / "runs").glob("*/failure.json"))
    assert len(failures) == 1
    assert not (failures[0].parent / "completion.json").exists()
    parent_direction = next((parent_path / "direction").glob("*.json"))
    parent_direction.write_text(parent_direction.read_text() + " ")
    with pytest.raises(ValueError, match="Pinned source"):
        data.metadata()
