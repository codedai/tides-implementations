"""Semantic reweight audits, beyond checksums, on small synthetic replay runs."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.merge_review.debug import _write
from tools.merge_review.gtfs_matching import multimode, runner
from tools.merge_review.gtfs_matching.continuous.integration import score_candidate
from tools.merge_review.gtfs_matching.continuous.retrieval import SchedulePool
from tools.merge_review.gtfs_matching.continuous.windows import construct_windows
from tools.merge_review.gtfs_matching.feed import file_sha256
from tools.merge_review.tests.audit_continuous_run import audit
from tools.merge_review.tests.test_gtfs_multimode_run import (
    continuous_source_run as continuous_source_run,
)
from tools.merge_review.tests.test_gtfs_multimode_run import (
    scheduled_source_run as scheduled_source_run,
)
from tools.merge_review.tests.test_gtfs_multimode_run import (
    snapshot,
)
from tools.merge_review.tests.test_gtfs_multimode_run import (
    source_run as source_run,
)


@pytest.fixture
def audit_pair(continuous_source_run):
    workspace, source, case_id = continuous_source_run
    record = multimode.create_multimode_run(
        workspace,
        source.name,
        "synthetic reweight audit",
        workers=1,
        scoring_overrides={
            "continuous_absolute_delay_weight": 10.0,
            "continuous_delay_dynamics_weight": 0.1,
            "continuous_match_cost_max": 7.3,
            "continuous_local_support_cost_max": 16.5,
            "continuous_local_missing_cost": 22.0,
            "continuous_calibration_max_score": 7.3,
        },
    )
    relative = f"cases/{runner.artifact_key(case_id)}.json.gz"
    return source, Path(record["path"]), relative


def replace_case_and_repin(folder, relative, artifact):
    """Change only this test's synthetic fixture, to exercise semantic guards."""
    (folder / relative).unlink()
    runner.write_case(folder / relative, artifact)
    pins = json.loads((folder / "output_sha256.json").read_text())
    pins[relative] = file_sha256(folder / relative)
    (folder / "output_sha256.json").unlink()
    _write(folder / "output_sha256.json", pins)


def recalculate_score(candidate):
    candidate["score_components"] = {
        key: value * candidate["score_component_weights"][key]
        for key, value in candidate["score_components_unweighted"].items()
    }
    candidate["score"] = sum(candidate["score_components"].values())


def test_valid_reweight_audit_is_read_only_and_covers_all_modes(audit_pair):
    source, target, _ = audit_pair
    before = snapshot(source), snapshot(target)
    report = audit(source, target, reweight_only=True)
    assert report["status"] == "PASS"
    assert report["reweight_invariance_verified"] is True
    assert report["accuracy_claimed"] is False
    assert report["case_count"] == 1
    assert report["counts"]["mode_results_audited"] == 4
    assert report["counts"]["unchanged_candidate_fits_verified"] == 4
    assert report["counts"]["candidate_scores_audited"] == 4
    assert set(report["reweight_comparison"]) == set(multimode.MODES)
    assert (snapshot(source), snapshot(target)) == before


def test_reweight_only_cli_writes_a_new_report_without_overwriting(
    audit_pair, tmp_path
):
    source, target, _ = audit_pair
    report_path = tmp_path / "reweight-audit.json"
    command = [
        sys.executable,
        "-m",
        "tools.merge_review.tests.audit_continuous_run",
        "--source",
        str(source),
        "--run",
        str(target),
        "--output",
        str(report_path),
        "--reweight-only",
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, check=True, timeout=30
    )
    report = json.loads(report_path.read_text())
    assert json.loads(completed.stdout) == report
    assert report["reweight_invariance_verified"] is True
    before = report_path.read_bytes()
    repeated = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert repeated.returncode != 0
    assert "FileExistsError" in repeated.stderr
    assert report_path.read_bytes() == before


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("fit_summary", "changed estimated_delay_s"),
        ("fit_objective", "changed delay_fit"),
        ("delay_state", "changed fitted delay states"),
        ("raw_conditioned_cost", "changed raw component conditioned_fit_cost"),
        ("scope", "Decision scope changed"),
        ("retrieval", "Retrieval membership changed"),
        ("trajectory", "changed scheduled_trajectory"),
        ("upstream", "Upstream input mutated"),
    ],
)
def test_reweight_audit_rejects_semantic_mutation_even_after_repinning(
    audit_pair, mutation, reason
):
    source, target, relative = audit_pair
    artifact = runner.read_case(target / relative)
    mode = artifact["matching_modes"]["early"]
    candidate = mode["result"]["all_candidates"][0]
    if mutation == "fit_summary":
        candidate["estimated_delay_s"] += 1
    elif mutation == "fit_objective":
        candidate["delay_fit"]["objective"] += 1
    elif mutation == "delay_state":
        # Preserve source clocks and corrected-time order: the separate
        # reweight-invariance check must still reject this genuine refit.
        for row in candidate["whole_alignment"]["delay_process"]:
            row["delay_s"] += 1
            row["corrected_t"] -= 1
    elif mutation == "raw_conditioned_cost":
        candidate["score_components_unweighted"]["conditioned_fit_cost"] += 1
        recalculate_score(candidate)
    elif mutation == "scope":
        mode["selection"]["selected_avl_ids"] = []
    elif mutation == "retrieval":
        mode["result"]["all_candidates"] = []
    elif mutation == "trajectory":
        candidate["scheduled_trajectory"][0]["arrival_t"] += 1
    elif mutation == "upstream":
        artifact["projection"]["points"][0]["quality"] = 0.9
    replace_case_and_repin(target, relative, artifact)
    with pytest.raises(ValueError, match=reason):
        audit(source, target, reweight_only=True)


def test_reweight_audit_still_rejects_unpinned_changes(audit_pair):
    source, target, _ = audit_pair
    # This deliberately tests checksum failure, without semantic re-pinning.
    (target / "config.json").write_bytes((target / "config.json").read_bytes() + b" ")
    with pytest.raises(ValueError, match="Pinned artifact changed"):
        audit(source, target, reweight_only=True)


@pytest.fixture
def no_fit_audit_pair(audit_pair):
    source, target, relative = audit_pair
    for folder in (source, target):
        artifact = runner.read_case(folder / relative)
        canonical = json.loads((folder / "gtfs/canonical.json").read_text())
        cfg = artifact["config"]
        trip = copy.deepcopy(canonical["trips"][0])
        trip["trip_id"] = "synthetic-no-passage-support"
        # Score an incompatible diagnostic directly: every observed position
        # falls before this schedule, so the real scorer returns NO_EVIDENCE.
        for stop in trip["scheduled"]:
            stop["s_m"] += 10_000
        pool = SchedulePool([trip], cfg)
        for mode in multimode.MODES:
            entry = artifact["matching_modes"][mode]
            selected = set(entry["selection"]["selected_avl_ids"])
            points = [
                point
                for point in artifact["projection"]["points"]
                if f"avl|{point['rid']}" in selected
            ]
            candidate, _ = score_candidate(
                points, [], trip, pool, construct_windows(points, [], cfg), cfg
            )
            assert candidate["whole_alignment"]["delay_process"] == []
            assert candidate["delay_fit"]["status"] == "NO_EVIDENCE"
            entry["result"]["all_candidates"].append(candidate)
            entry["result"]["all_candidates"].sort(
                key=lambda row: (row["score"], row["candidate_id"])
            )
        replace_case_and_repin(folder, relative, artifact)
    return source, target, relative


def test_no_fit_sentinel_may_change_only_with_configured_gate(no_fit_audit_pair):
    source, target, relative = no_fit_audit_pair
    old, new = runner.read_case(source / relative), runner.read_case(target / relative)
    for artifact, expected in ((old, 4.0), (new, 8.3)):
        for mode in multimode.MODES:
            no_fit = next(
                candidate
                for candidate in artifact["matching_modes"][mode]["result"][
                    "all_candidates"
                ]
                if not candidate["whole_alignment"]["delay_process"]
            )
            assert no_fit["score_components_unweighted"][
                "conditioned_fit_cost"
            ] == pytest.approx(expected)
    report = audit(source, target, reweight_only=True)
    assert report["status"] == "PASS"
    assert report["counts"]["unchanged_candidate_fits_verified"] == 8


@pytest.mark.parametrize("mutated_side", ["source", "target"])
def test_no_fit_sentinel_cannot_hide_arbitrary_raw_cost_change(
    no_fit_audit_pair, mutated_side
):
    source, target, relative = no_fit_audit_pair
    folder = source if mutated_side == "source" else target
    artifact = runner.read_case(folder / relative)
    candidate = next(
        row
        for row in artifact["matching_modes"]["early"]["result"]["all_candidates"]
        if not row["whole_alignment"]["delay_process"]
    )
    candidate["score_components_unweighted"]["conditioned_fit_cost"] += 1
    recalculate_score(candidate)
    replace_case_and_repin(folder, relative, artifact)
    with pytest.raises(ValueError, match="sentinel"):
        audit(source, target, reweight_only=True)
