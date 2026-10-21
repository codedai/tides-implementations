"""Experiment 02 persistence, source conservation, and isolated ancestry."""

import json

import polars as pl
import pytest

from experiments.apc_avl_coverage_study.raw_audit import normalize_raw
from tools.merge_review import avl_dedup, debug, direction_trips
from tools.merge_review.tests.test_debug import apc


@pytest.fixture
def direction_workspace(tmp_path):
    audit = tmp_path / "audit"
    audit.mkdir()
    first = {
        "Date_Key": "20231106",
        "Message_DateTime": "2023-11-06 00:00:10.000",
        "Vehicle": "9027",
        "Latitude": "43.65",
        "Longitude": "-79.4",
        "Heading": "0",
        "Route": "29",
        "Run": "1",
        "Destination": "NORTH - 29 DUFFERIN",
        "OffRoute": "false",
        "Delayed": "false",
        "KPH": "10",
        "tatripId": "source-trip",
    }
    rows = [first, dict(first)]
    for seconds, route, destination, vehicle in (
        (20, "29", "NORTH - 29 DUFFERIN", "9027"),
        (40, "29", "SOUTH - 29 DUFFERIN", "9027"),
        (50, "PI", "#N/A", "9027"),
        (55, "PO", "#N/A", "9027"),
        (60, "29", "#N/A", "9027"),
        (70, "PI", "#N/A", "filtered-only"),
    ):
        rows.append(
            {
                **first,
                "Message_DateTime": (
                    f"2023-11-06 00:{seconds // 60:02}:{seconds % 60:02}.000"
                ),
                "Route": route,
                "Destination": destination,
                "Vehicle": vehicle,
            }
        )
    raw = pl.from_dicts(rows)
    csv = audit / "avl.csv"
    raw.write_csv(csv)
    digest = avl_dedup.file_sha256(csv)
    normalize_raw(raw, "avl", digest).write_parquet(audit / "avl.parquet")
    pl.from_dicts(
        [
            apc(1, 15, 45),
            apc(2, 15, 45, direction="SOUTH"),
            apc(3, 5, 20, vehicle="apc-only", branch="PI"),
        ]
    ).write_parquet(audit / "apc.parquet")
    (audit / "inventory.json").write_text(
        json.dumps({"sources": {"avl": {"path": str(csv), "sha256": digest}}})
    )
    return debug.DebugWorkspace(tmp_path / "debug", audit)


def test_direction_config_has_no_hidden_matching_or_segmentation_thresholds():
    assert debug.validate_config({"method": direction_trips.METHOD}) == {
        "method": direction_trips.METHOD,
        "segmentation": {},
        "merge": {},
    }
    for config in (
        {"merge": {"radius_m": 100}},
        {"segmentation": {"gap_seconds": 300}},
        {"segmentation": []},
    ):
        with pytest.raises(ValueError):
            debug.validate_config({"method": direction_trips.METHOD, **config})


def test_direction_run_conserves_sources_filters_only_pi_po_and_preserves_parent(
    direction_workspace, monkeypatch
):
    workspace = direction_workspace
    parent = workspace.create_run({"method": "vehicle_only_dedup"}, label="01b")
    parent_path = workspace.get_run(parent["run_id"])
    original = {
        name: (parent_path / name).read_bytes()
        for name in ("manifest.json", "summary.json", "completion.json")
    }

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Experiment 02 must not run the old segmenter or observation matcher"
        )

    monkeypatch.setattr(debug.segmentation, "segment_vehicle", forbidden)
    monkeypatch.setattr(debug.matcher, "build_index", forbidden)
    monkeypatch.setattr(debug.matcher, "match_direct", forbidden)
    result = workspace.create_run(
        {"method": direction_trips.METHOD}, label="02", parent_run_id=parent["run_id"]
    )
    summary = result["summary"]
    assert summary["source_counts"] == {"avl": 8, "apc": 3}
    assert summary["deduplicated_counts"] == {"avl": 7, "apc": 3}
    assert summary["retained_counts"] == {"avl": 4, "apc": 3}
    assert summary["observations"] == 7
    assert summary["filtered_avl_pi_po"] == 3
    assert summary["removed_avl"] == 1
    assert summary["filter_counts"] == {"PI": 2, "PO": 1}
    assert (
        summary["trip_segmentation_used"] and not summary["observation_matching_used"]
    )
    assert summary["validation_ok"] and not any(summary["violations"].values())
    assert summary["accuracy"] is None and not summary["publishable"]
    folder = workspace.get_run(result["run_id"])
    assert not (folder / "assignments").exists()
    for name, content in original.items():
        assert (parent_path / name).read_bytes() == content
    selection = json.loads((folder / "selection.json").read_text())
    assert len(selection) == 3  # APC-only and all-filtered vehicles remain auditable.
    observations = pl.concat(
        [pl.read_parquet(p) for p in (folder / "observations").glob("*.parquet")],
        how="diagonal_relaxed",
    )
    assert set(observations.filter(pl.col("source") == "apc")["rid"]) == {
        "apc:1",
        "apc:2",
        "apc:3",
    }
    assert not observations.filter(
        (pl.col("source") == "avl") & pl.col("route").is_in(["PI", "PO"])
    ).height
    difference = workspace.compare(parent["run_id"], result["run_id"])
    assert difference["comparison_status"] == "direction_segmentation_comparison"
    assert difference["summary"]["avl_removed_by_pi_po_filter"] == 3
    assert difference["summary"]["unexpected_removed"] == 0
    assert difference["summary"]["unexpected_added"] == 0
    assert difference["summary"]["original_fields_changed"] == 0
    assert (
        difference["summary"]["apc_unchanged"]
        and difference["summary"]["source_lineage_conserved"]
    )
    reverse = workspace.compare(result["run_id"], parent["run_id"])
    assert reverse["summary"]["avl_restored_from_pi_po_filter"] == 3
    assert reverse["summary"]["source_lineage_conserved"]


def test_failed_direction_validation_never_publishes_completed_run(
    direction_workspace, monkeypatch
):
    def invalid(*args, **kwargs):
        return {"violations": {"test_injected_violation": 1}, "examined": {}}

    monkeypatch.setattr(direction_trips, "validate_direction_trips", invalid)
    with pytest.raises(ValueError, match="validation failed"):
        direction_workspace.create_run({"method": direction_trips.METHOD})
    assert not direction_workspace.metadata()["runs"]
    assert len(list((direction_workspace.root / "runs").glob("*/failure.json"))) == 1


def test_direction_same_run_comparison_has_no_fabricated_matching_changes(
    direction_workspace,
):
    result = direction_workspace.create_run({"method": direction_trips.METHOD})
    diff = direction_workspace.compare(result["run_id"], result["run_id"])
    assert diff["summary"]["original_fields_changed"] == 0
    assert diff["summary"]["source_lineage_conserved"]
    assert diff["changes"] == []
    assert diff["accuracy"] is None
