"""Audit tests use tiny synthetic runs, never production evidence or labels."""

import gzip
import hashlib
import json

import pytest

from tools.merge_review.gtfs_matching.continuous.config import get_config
from tools.merge_review.gtfs_matching.continuous.tests.test_continuous_pipeline import (
    apc,
    points,
    schedule,
)
from tools.merge_review.gtfs_matching.partial import evaluate_matching_modes
from tools.merge_review.tests.audit_occurrence_run import MODES, PinnedRun, audit


def write_json(path, value, compress=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, allow_nan=False).encode()
    path.write_bytes(gzip.compress(payload, mtime=0) if compress else payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def runs(tmp_path):
    observations = points(step=30)
    events = [apc(stop=2)]
    projection = {"points": observations, "apc_evidence": events}
    canonical = {
        "references": [],
        "trips": [schedule(), schedule("previous", -600)],
        "route_id": "route-A",
        "feed_version": "feed-A",
    }
    source, target = tmp_path / "source04", tmp_path / "target04c"
    source_pins = None
    for folder, enabled in ((source, False), (target, True)):
        config = get_config(
            {
                "continuous_matching_enabled": True,
                "continuous_occurrence_model_enabled": enabled,
            }
        )
        modes = evaluate_matching_modes(projection, canonical, config)
        artifact = {
            "case_id": "synthetic-episode",
            "projection": projection,
            "apc_evidence": events,
            "parent_boundary": {"reason": "synthetic"},
            "matching_modes": modes,
            "result": modes["global"]["result"],
            "config": config,
        }
        manifest = {
            "debug_date": "2023-11-06",
            "route_scope": ["29"],
            "partial_source_run_id": source.name,
            "partial_source_sha256": source_pins or {},
        }
        pins = {
            "manifest.json": write_json(folder / "manifest.json", manifest),
            "config.json": write_json(folder / "config.json", {"gtfs": config}),
            "gtfs/canonical.json": write_json(
                folder / "gtfs/canonical.json", canonical
            ),
            "cases/synthetic.json.gz": write_json(
                folder / "cases/synthetic.json.gz", artifact, True
            ),
        }
        controls = {
            "completion.json": write_json(
                folder / "completion.json",
                {"validation_ok": True, "available_matching_modes": list(MODES)},
            ),
            "run.json": write_json(folder / "run.json", {"id": folder.name}),
            "output_sha256.json": write_json(folder / "output_sha256.json", pins),
        }
        if not enabled:
            source_pins = pins | controls
    return source, target


def test_full_audit_works_for_all_four_modes_and_reports_no_accuracy(runs):
    source, target = runs
    report = audit(source, target)
    assert report["status"] == "PASS"
    assert report["counts"]["cases_audited"] == 1
    assert report["counts"]["mode_results_audited"] == 4
    assert report["counts"]["candidate_anchor_headways_verified"] > 0
    assert report["counts"]["protected_occurrence_memberships_verified"] > 0
    assert report["source_pins_verified"] == 4
    assert report["source_clocks_unchanged"]
    assert not report["accuracy_claimed"]
    for mode in MODES:
        metrics = report["heldout_evaluation"][mode]["new"]["metrics"]
        assert metrics["exact_trip_accuracy"] is None
        assert metrics["calibration_error"] is None


def test_changed_pinned_artifact_fails(runs):
    source, target = runs
    (target / "cases/synthetic.json.gz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Pinned artifact changed"):
        audit(source, target)


def test_source_symlink_fails(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    elsewhere = tmp_path / "control.json"
    elsewhere.write_text("{}")
    (source / "output_sha256.json").symlink_to(elsewhere)
    with pytest.raises(ValueError, match="Symlink"):
        PinnedRun(source)


def test_rehashed_fabricated_raw_clock_is_not_accepted(runs):
    source, target = runs
    path = target / "cases/synthetic.json.gz"
    artifact = json.loads(gzip.decompress(path.read_bytes()))
    artifact["matching_modes"]["early"]["result"]["all_candidates"][0][
        "whole_alignment"
    ]["delay_process"][0]["t"] += 1
    pins = json.loads((target / "output_sha256.json").read_text())
    pins["cases/synthetic.json.gz"] = write_json(path, artifact, True)
    write_json(target / "output_sha256.json", pins)
    with pytest.raises(ValueError, match="clock"):
        audit(source, target)
