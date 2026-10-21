"""Synthetic reproducibility checks; no private CSVs or historical runs needed."""

import copy
import gzip
import json
import subprocess
import sys
from datetime import datetime

import polars as pl
import pytest

from tools.merge_review.gtfs_matching import REPRESENTATION
from tools.merge_review.gtfs_matching.feed import file_sha256
from tools.merge_review.gtfs_matching.runner import artifact_key
from tools.merge_review.rebuild_validation import MODES, audit_rebuild

CASE = "direction:TEST:0000"
VEHICLE = "vehicle-synthetic"
RID = "avl:" + "a" * 64 + ":9001"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def case_path(root):
    return root / "cases" / f"{artifact_key(CASE)}.json.gz"


def read_case(root):
    with gzip.open(case_path(root), "rt") as stream:
        return json.load(stream)


def write_case(root, artifact):
    case_path(root).parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(case_path(root), "wt") as stream:
        json.dump(artifact, stream)


def repin(root):
    pins = {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in root.rglob("*")
        if path.is_file() and path.name not in {"completion.json", "output_sha256.json"}
    }
    write_json(root / "output_sha256.json", pins)


def make_run(root):
    root.mkdir()
    manifest = {
        "run_id": root.name,
        "baseline": str(root),
        "created_at": "different per run",
        "code_sha256": {"module": root.name},
        "debug_date": "2023-11-06",
        "representation": REPRESENTATION,
        "route_scope": ["synthetic-route"],
        "route_id": "synthetic-route",
        "source_counts": {"avl": 1, "apc": 0},
        "available_matching_modes": list(MODES),
        "case_artifact_suffix": ".json.gz",
    }
    config = {
        "method": "gtfs_trip_matching",
        "gtfs": {
            "feed_manifest_path": str(root / "different-location.csv"),
            "continuous_absolute_delay_weight": 10.0,
            "continuous_delay_dynamics_weight": 0.1,
            "execution_workers": 1,
        },
    }
    summary = {
        "cases": 1,
        "avl": 1,
        "apc": 0,
        "vehicles": 1,
        "scorer": "continuous_passage_v1",
        "status_counts": {"PATTERN_ONLY": 1},
        "matching_mode_status_counts": {mode: {"PATTERN_ONLY": 1} for mode in MODES},
        "elapsed_s": 99.0,
    }
    feed = {
        "feed_version": "synthetic-feed",
        "archive_sha256": "b" * 64,
        "route_id": "synthetic-route",
        "date": "2023-11-06",
        "feed_path": str(root / "different-feed.zip"),
    }
    canonical = {
        "feed_version": "synthetic-feed",
        "route_id": "synthetic-route",
        "references": [
            {"reference_id": "ref", "length_m": 1000.0, "points": [{"s_m": 0.0}]}
        ],
        "trips": [{"trip_id": "A", "scheduled": [{"arrival_t": 1699230000.0}]}],
    }
    boundary = {
        "id": CASE,
        "vehicle": VEHICLE,
        "vehicle_label": "TEST",
        "direction": "NORTH",
        "start": 1699230000.0,
        "end": 1699230120.0,
        "source_rids": [{"source": "avl", "rid": RID}],
    }
    point = {
        "rid": RID,
        "t": 1699230000.0,
        "s_m": 10.0,
        "reference_id": "ref",
        "fragment_id": "fragment",
        "projection_status": "ON_REFERENCE",
        "quality": 0.9,
        "projection_distance_m": 5.0,
    }
    candidates = [
        {
            "candidate_id": f"scheduled:{trip}",
            "trip_id": trip,
            "score": score,
            "score_components_unweighted": {"absolute_delay_level_cost": score / 10},
            "score_component_weights": {"absolute_delay_level_cost": 10.0},
            "score_components": {"absolute_delay_level_cost": score},
            "coverage_metrics": {"projection_point_ratio": 1.0},
            "whole_alignment": {
                "delay_process": [{"t": 1699230000.0, "delay_s": 10.0}]
            },
        }
        for trip, score in (("A", 1.0), ("B", 2.0))
    ]
    result = {
        "status": "PATTERN_ONLY",
        "scorer": "continuous_passage_v1",
        "scorer_fingerprint": root.name,
        "all_candidates": candidates,
        "candidates": [],
        "diagnostics": {"runtime_s": 3.0, "production_dtw_used": False},
        "reasons": ["NO_INDEPENDENT_CALIBRATION"],
    }
    artifact = {
        "case_id": CASE,
        "config": config["gtfs"],
        "parent_boundary": boundary,
        "projection": {"points": [point], "apc_evidence": [], "fragments": []},
        "apc_evidence": [],
        "result": result,
        "matching_modes": {
            mode: {
                "result": copy.deepcopy(result),
                "selection": {"mode": mode, "selected_avl_ids": [RID]},
            }
            for mode in MODES
        },
    }
    for relative, value in (
        ("manifest.json", manifest),
        ("config.json", config),
        ("summary.json", summary),
        ("completion.json", {"validation_ok": True}),
        ("selection.json", [{"vehicle_case": VEHICLE, "raw_vehicle": "TEST"}]),
        ("gtfs/feed.json", feed),
        ("gtfs/canonical.json", canonical),
        (f"catalog/{VEHICLE}.json", [boundary]),
        (f"scope/{VEHICLE}.json", {"excluded_source_ids": [], "selected_count": 1}),
    ):
        write_json(root / relative, value)
    observation_path = root / "observations" / f"{VEHICLE}.parquet"
    observation_path.parent.mkdir()
    pl.DataFrame(
        {
            "source": ["avl"],
            "rid": [RID],
            "source_row": pl.Series([9001], dtype=pl.UInt32),
            "payload_hash": pl.Series([2**63 + 17], dtype=pl.UInt64),
            "time": [datetime(2023, 11, 6, 0, 20)],
            "lat": [43.7],
        }
    ).write_parquet(observation_path)
    write_case(root, artifact)
    repin(root)
    return root


@pytest.fixture
def runs(tmp_path):
    return make_run(tmp_path / "retained"), make_run(tmp_path / "rebuilt")


def mutate_case(root, change):
    artifact = read_case(root)
    change(artifact)
    artifact["result"] = copy.deepcopy(artifact["matching_modes"]["global"]["result"])
    write_case(root, artifact)
    repin(root)


def test_matching_business_values_pass_despite_different_locations_and_fingerprints(
    runs,
):
    source, rebuilt = runs
    progress = []
    report = audit_rebuild(source, rebuilt, progress=progress.append)
    assert report["status"] == "PASS"
    assert report["semantic_equal"]
    assert not report["byte_identity_claimed"]
    assert report["difference_count"] == 0
    assert report["checks"] == {
        "vehicles": 1,
        "mode_results": 4,
        "candidate_scores": 8,
        "cases": 1,
    }
    assert report["source_counts"] == {"source": {"avl": 1}, "rebuilt": {"avl": 1}}
    assert report["ignored_field_occurrences"]["scorer_fingerprint"] == 4
    assert progress[-1]["completed"] == 1


@pytest.mark.parametrize(
    "field,value,stage",
    [
        ("s_m", 11.0, "projection"),
        ("quality", 0.3, "projection"),
        ("t", 1699230000.01, "projection"),
        ("projection_status", "OFF_REFERENCE", "projection"),
    ],
)
def test_projection_and_subsecond_source_clock_changes_are_not_ignored(
    runs, field, value, stage
):
    source, rebuilt = runs
    mutate_case(
        rebuilt,
        lambda artifact: artifact["projection"]["points"][0].update({field: value}),
    )
    report = audit_rebuild(source, rebuilt)
    assert report["status"] == "FAIL"
    assert report["differences_by_stage"][stage] > 0


def test_candidate_reordering_is_exact_even_when_score_changes_fit_tolerance(runs):
    source, rebuilt = runs
    mutate_case(
        rebuilt,
        lambda artifact: artifact["matching_modes"]["middle"]["result"][
            "all_candidates"
        ].reverse(),
    )
    report = audit_rebuild(source, rebuilt, absolute_tolerance=1000)
    assert report["status"] == "FAIL"
    assert report["mode_statistics"]["middle"]["top1_changes"] == 1
    assert report["mode_statistics"]["middle"]["candidate_order_changes"] == 1


@pytest.mark.parametrize("mode", MODES)
def test_each_mode_delay_cost_coverage_and_status_are_compared(runs, mode):
    source, rebuilt = runs

    def change(artifact):
        result = artifact["matching_modes"][mode]["result"]
        result["status"] = "AMBIGUOUS"
        candidate = result["all_candidates"][0]
        candidate["score_components"]["absolute_delay_level_cost"] = 3.0
        candidate["coverage_metrics"]["projection_point_ratio"] = 0.5
        candidate["whole_alignment"]["delay_process"][0]["delay_s"] = 100.0

    mutate_case(rebuilt, change)
    report = audit_rebuild(source, rebuilt)
    assert report["status"] == "FAIL"
    assert report["differences_by_stage"]["matching"] == 4
    assert report["mode_statistics"][mode]["rebuilt_status_counts"] == {"AMBIGUOUS": 1}


def test_raw_uint64_payloads_and_original_field_values_are_exact(runs):
    source, rebuilt = runs
    path = rebuilt / "observations" / f"{VEHICLE}.parquet"
    frame = pl.read_parquet(path).with_columns(
        (pl.col("payload_hash") + 1).alias("payload_hash"),
        (pl.col("lat") + 1e-9).alias("lat"),
    )
    frame.write_parquet(path)
    repin(rebuilt)
    report = audit_rebuild(source, rebuilt, absolute_tolerance=1000)
    assert report["status"] == "FAIL"
    assert report["differences_by_stage"]["source_observations"] == 2


def test_changed_config_and_canonical_schedule_are_reported(runs):
    source, rebuilt = runs
    config = json.loads((rebuilt / "config.json").read_text())
    config["gtfs"]["continuous_absolute_delay_weight"] = 9
    write_json(rebuilt / "config.json", config)
    canonical = json.loads((rebuilt / "gtfs/canonical.json").read_text())
    canonical["trips"][0]["scheduled"][0]["arrival_t"] += 1
    write_json(rebuilt / "gtfs/canonical.json", canonical)
    repin(rebuilt)
    report = audit_rebuild(source, rebuilt)
    assert report["differences_by_stage"]["configuration"] == 1
    assert report["differences_by_stage"]["canonical"] == 1


def test_caps_examples_without_stopping_remaining_checks(runs):
    source, rebuilt = runs

    def change(artifact):
        for mode in MODES:
            artifact["matching_modes"][mode]["result"]["status"] = "UNMATCHED"

    mutate_case(rebuilt, change)
    report = audit_rebuild(source, rebuilt, max_differences=1)
    assert report["difference_count"] == 4
    assert len(report["differences"]) == 1
    assert report["difference_examples_truncated"]
    assert report["checks"]["mode_results"] == 4


def test_unpinned_tampering_is_not_presented_as_a_semantic_pass(runs):
    source, rebuilt = runs
    (rebuilt / "summary.json").write_text("{}")
    with pytest.raises(ValueError, match="output pin failed"):
        audit_rebuild(source, rebuilt)


def test_incomplete_run_and_self_comparison_are_rejected(runs):
    source, rebuilt = runs
    with pytest.raises(ValueError, match="distinct directories"):
        audit_rebuild(source, source)
    write_json(rebuilt / "completion.json", {"validation_ok": False})
    with pytest.raises(ValueError, match="completed"):
        audit_rebuild(source, rebuilt)


def test_missing_mode_does_not_count_as_success(runs):
    source, rebuilt = runs
    mutate_case(rebuilt, lambda artifact: artifact["matching_modes"].pop("late"))
    with pytest.raises(ValueError, match="four matching modes"):
        audit_rebuild(source, rebuilt)


def test_cli_writes_new_report_and_refuses_to_write_inside_run(runs, tmp_path):
    source, rebuilt = runs
    output = tmp_path / "semantic-report.json"
    command = [
        sys.executable,
        "-m",
        "tools.merge_review.rebuild_validation",
        "--source",
        str(source),
        "--rebuilt",
        str(rebuilt),
        "--output",
    ]
    run = subprocess.run(
        [*command, str(output)], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(output.read_text())["status"] == "PASS"
    existing = subprocess.run(
        [*command, str(output)], capture_output=True, text=True, check=False
    )
    assert existing.returncode != 0
    unsafe = subprocess.run(
        [*command, str(rebuilt / "bad.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert unsafe.returncode != 0
    assert not (rebuilt / "bad.json").exists()
