"""Fresh scoring emits usable shadow artifacts without a fictional old run."""

import copy
import json

import pytest

from tools.merge_review.gtfs_matching import METHOD, canonical, feed, runner
from tools.merge_review.tests.test_direction_runner import (
    direction_workspace,  # noqa: F401
)

MODES = ("global", "early", "middle", "late")


def test_fresh_shadow_is_pinned_annotation_only_and_has_no_comparator(tmp_path):
    rows = {
        mode: [
            {
                "case_id": case,
                "feed_version": "synthetic-feed",
                "route_id": "test-route",
                "service_date": "2023-11-06",
                "vehicle_id": "test-vehicle",
                "start_time_s": start,
                "end_time_s": start + 60,
                "status": "PATTERN_ONLY",
                "all_candidates": [],
                "candidates": [],
            }
            for case, start in (("second", 180), ("first", 0))
        ]
        for mode in MODES
    }
    original = copy.deepcopy(rows)
    pins = {}
    runner.write_fresh_continuous_shadow(tmp_path, rows, {}, pins)
    assert rows == original
    assert len(pins) == 12
    for relative, digest in pins.items():
        assert feed.file_sha256(tmp_path / relative) == digest
    for mode in MODES:
        episodes = json.loads((tmp_path / f"shadow/{mode}-episodes.json").read_text())
        report = json.loads((tmp_path / f"shadow/{mode}-report.json").read_text())
        sequence = json.loads(
            (tmp_path / f"shadow/{mode}-vehicle-sequence.json").read_text()
        )
        assert episodes["old"] == []
        assert [row["case_id"] for row in episodes["new"]] == ["first", "second"]
        assert episodes["provenance"]["comparison_status"] == "NO_COMPARATOR"
        assert report["old"]["case_count"] == 0
        assert report["new"]["case_count"] == 2
        assert report["new"]["metrics"]["exact_trip_accuracy"] is None
        assert all(value is None for value in report["comparison"].values())
        assert report["descriptive"]["paired_cases"] == 0
        assert report["descriptive"]["top_rank_changes"] is None
        assert sequence["annotation_only"]
        assert sequence["enabled"]
        assert not sequence["provenance"]["final_decisions_changed_by_sequence"]
    with pytest.raises(FileExistsError):
        runner.write_fresh_continuous_shadow(tmp_path, rows, {}, {})


def test_direct_continuous_run_publishes_all_four_shadow_modes(request, monkeypatch):
    workspace = request.getfixturevalue("direction_workspace")
    direction = workspace.create_run({"method": "direction_destination"})
    raw = workspace.audit / "avl.csv"
    digest = feed.file_sha256(raw)
    points = canonical.geometry(
        [{"lat": 43.65, "lon": -79.4}, {"lat": 43.66, "lon": -79.4}],
        (43.65, -79.4),
    )
    reference = {
        "reference_id": "synthetic-ref",
        "family_id": "synthetic-family",
        "feed_version": "synthetic-feed",
        "route_id": "29",
        "direction_id": "0",
        "direction_label": "NORTH",
        "points": points,
        "origin": (43.65, -79.4),
        "length_m": points[-1]["s_m"],
        "stop_positions": [],
    }
    monkeypatch.setattr(
        feed,
        "load_feed",
        lambda *args, **kwargs: {
            "feed_version": "synthetic-feed",
            "route_id": "29",
            "selection": {},
            "diagnostics": {},
            "archive_sha256": digest,
            "feed_path": str(raw),
            "manifest_path": str(raw),
            "manifest_sha256": digest,
        },
    )
    monkeypatch.setattr(
        canonical,
        "build_references",
        lambda *args, **kwargs: {
            "feed_version": "synthetic-feed",
            "references": [reference],
            "trips": [],
            "validations": [],
            "route_id": "29",
            "diagnostics": {},
            "direction_crosswalk": [],
        },
    )
    record = workspace.create_run(
        {
            "method": METHOD,
            "gtfs": {
                "execution_workers": 1,
                "continuous_matching_enabled": True,
                "matching_partial_modes_enabled": True,
            },
        },
        parent_run_id=direction["run_id"],
    )
    folder = workspace.get_run(record["run_id"])
    manifest = json.loads((folder / "manifest.json").read_text())
    pins = json.loads((folder / "output_sha256.json").read_text())
    assert manifest["fresh_continuous_matching"]
    assert manifest["shadow_comparison_status"] == "NO_COMPARATOR"
    assert record["summary"]["scorer"] == "continuous_passage_v1"
    assert len(record["summary"]["shadow_evaluation_reports"]) == 4
    for mode in MODES:
        episodes = json.loads((folder / f"shadow/{mode}-episodes.json").read_text())
        assert len(episodes["new"]) == record["summary"]["cases"]
        assert episodes["old"] == []
        for suffix in ("episodes", "report", "vehicle-sequence"):
            relative = f"shadow/{mode}-{suffix}.json"
            assert feed.file_sha256(folder / relative) == pins[relative]
