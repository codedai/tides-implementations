"""Real partial evaluation, append-only publication, and mode isolation gates."""

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.merge_review.debug import DebugWorkspace, _write
from tools.merge_review.gtfs_matching import (
    METHOD,
    REPRESENTATION,
    adapter,
    config,
    multimode,
    partial,
    runner,
)
from tools.merge_review.gtfs_matching.feed import file_sha256


def snapshot(folder):
    return {
        str(p.relative_to(folder)): p.read_bytes()
        for p in folder.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def source_run(tmp_path, monkeypatch):
    root = tmp_path / "debug"
    source = root / "runs/run-20231106-pinned-global"
    source.mkdir(parents=True)
    workspace = SimpleNamespace(root=root, date="2023-11-06")
    workspace.get_run = lambda run_id: DebugWorkspace.get_run(workspace, run_id)

    class PinnedParent:
        def __init__(self, *args):
            pass

        def _verify_parent(self):
            pass

    monkeypatch.setattr(adapter, "GTFSReviewData", PinnedParent)
    cfg = config.defaults()
    cfg.pop("matching_partial_modes_enabled")
    canonical = {
        "route_id": "29",
        "trips": [],
        "references": [
            {
                "reference_id": "r",
                "family_id": "f",
                "direction_label": "NORTH",
                "length_m": 3000,
                "shape_id": "shape",
            }
        ],
    }
    start = 1699257600
    points = [
        dict(
            source="avl",
            rid=str(i),
            t=start + 10 * i,
            s_m=100 * i,
            reference_id="r",
            family_id="f",
            fragment_id="f0",
            projection_status="ON_REFERENCE",
            quality=1,
            lateral_m=0,
        )
        for i in range(31)
    ]
    projection = {"points": points, "apc_evidence": []}
    modes = partial.evaluate_matching_modes(projection, canonical, cfg)
    case_id = "direction:test:0001"
    summary = dict(
        id=case_id,
        date=workspace.date,
        vehicle="vehicle-test",
        vehicle_label="test",
        direction="NORTH",
        start=start,
        end=start + 300,
        avl_count=31,
        apc_count=0,
        gtfs_status=modes["global"]["result"]["status"],
        gtfs_candidate_count=0,
    )
    artifact = dict(
        case_id=case_id,
        projection=projection,
        apc_evidence=[],
        result=modes["global"]["result"],
        parent_boundary=copy.deepcopy(summary),
        config=cfg,
    )
    runner.write_case(
        source / "cases" / f"{runner.artifact_key(case_id)}.json.gz", artifact
    )
    for relative, value in {
        "config.json": {"method": METHOD, "gtfs": cfg},
        "gtfs/canonical.json": canonical,
        "selection.json": [{"vehicle_case": "vehicle-test", "raw_vehicle": "test"}],
        "catalog/vehicle-test.json": [summary],
        "observations/vehicle-test.json": points,
        "manifest.json": {
            "run_id": source.name,
            "representation": REPRESENTATION,
            "route_scope": ["29"],
            "debug_date": workspace.date,
            "code_sha256": {"projection": "original-code"},
        },
        "summary.json": {
            "cases": 1,
            "elapsed_s": 123,
            "violations": {},
            "status_counts": {summary["gtfs_status"]: 1},
        },
    }.items():
        _write(source / relative, value)
    _write(
        source / "output_sha256.json",
        {name: file_sha256(source / name) for name in snapshot(source)},
    )
    _write(
        source / "completion.json",
        {"debug_date": workspace.date, "validation_ok": True},
    )
    _write(source / "run.json", {"run_id": source.name, "status": "complete"})
    return workspace, source, case_id


def test_append_only_four_mode_run_reuses_global_and_projection(
    source_run, monkeypatch
):
    workspace, source, case_id = source_run
    before = snapshot(source)
    monkeypatch.setattr(
        runner, "evaluate_segment", lambda *a, **k: pytest.fail("Projection rerun")
    )
    record = multimode.create_multimode_run(
        workspace, source.name, "four modes", workers=1
    )
    target = Path(record["path"])
    assert snapshot(source) == before
    relative = f"cases/{runner.artifact_key(case_id)}.json.gz"
    old, new = runner.read_case(source / relative), runner.read_case(target / relative)
    for key in ("result", "projection", "apc_evidence", "parent_boundary"):
        assert new[key] == old[key]
    assert set(new["matching_modes"]) == set(multimode.MODES)
    assert new["matching_modes"]["global"]["result"] == old["result"]
    assert set(record["summary"]["matching_mode_status_counts"]) == set(multimode.MODES)
    for mode in multimode.MODES:
        assert (target / f"overview/{mode}.json").is_file()
    catalog = json.loads((target / "catalog/vehicle-test.json").read_text())
    assert set(catalog[0]["matching_modes"]) == set(multimode.MODES)
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["stage_provenance"]["projection_reused_unchanged"]
    assert not manifest["stage_provenance"]["online_causal_decision"]
    for relative, digest in json.loads(
        (target / "output_sha256.json").read_text()
    ).items():
        assert file_sha256(target / relative) == digest
    assert workspace.get_run(record["run_id"]) == target


def test_source_tamper_aborts_before_creating_run(source_run):
    workspace, source, _ = source_run
    with (source / "config.json").open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="source evidence changed"):
        multimode.create_multimode_run(workspace, source.name, "tampered", workers=1)
    assert list((workspace.root / "runs").iterdir()) == [source]


@pytest.mark.parametrize("corruption", ["global", "scope", "probability", "in_place"])
def test_invalid_mode_evidence_cannot_be_published(source_run, monkeypatch, corruption):
    workspace, source, _ = source_run
    before = snapshot(source)
    evaluate = partial.evaluate_matching_modes

    def corrupt(*args, **kwargs):
        if corruption == "in_place":
            kwargs["global_result"]["status"] = "INVENTED"
        modes = copy.deepcopy(evaluate(*args, **kwargs))
        if corruption == "global":
            modes["global"]["result"]["status"] = "INVENTED"
        elif corruption == "scope":
            modes["early"]["selection"]["selected_avl_ids"].append("avl|outside")
        elif corruption == "probability":
            modes["late"]["result"]["candidates"] = [{"probability": 0.99}]
        return modes

    monkeypatch.setattr(partial, "evaluate_matching_modes", corrupt)
    with pytest.raises(ValueError):
        multimode.create_multimode_run(workspace, source.name, "invalid", workers=1)
    assert snapshot(source) == before
    outputs = [p for p in (workspace.root / "runs").iterdir() if p != source]
    assert len(outputs) == 1
    assert (outputs[0] / "failure.json").is_file()
    assert not (outputs[0] / "completion.json").exists()


@pytest.fixture
def scheduled_source_run(source_run):
    """A pinned real-matcher source with nonzero delay, not mocked score results."""
    workspace, source, case_id = source_run
    relative = f"cases/{runner.artifact_key(case_id)}.json.gz"
    artifact = runner.read_case(source / relative)
    canonical = json.loads((source / "gtfs/canonical.json").read_text())
    cfg = json.loads((source / "config.json").read_text())
    cfg["gtfs"]["score_delay_prior_weight"] = 0.05
    start = artifact["projection"]["points"][0]["t"]
    canonical["trips"] = [
        {
            "trip_id": "scheduled",
            "service_date": workspace.date,
            "feed_version": "synthetic",
            "route_id": "29",
            "reference_id": "r",
            "family_id": "f",
            "pattern_id": "pattern",
            "scheduled": [
                {
                    "t": start - 600 + i * 30,
                    "arrival_t": start - 600 + i * 30,
                    "departure_t": start - 600 + i * 30,
                    "s_m": i * 300,
                }
                for i in range(11)
            ],
        }
    ]
    modes = partial.evaluate_matching_modes(
        artifact["projection"], canonical, cfg["gtfs"]
    )
    artifact.update(result=modes["global"]["result"], config=cfg["gtfs"])
    assert artifact["result"]["all_candidates"]
    (source / relative).unlink()  # Replace this test's own synthetic fixture.
    runner.write_case(source / relative, artifact)
    (source / "gtfs/canonical.json").unlink()
    _write(source / "gtfs/canonical.json", canonical)
    (source / "config.json").unlink()
    _write(source / "config.json", cfg)
    pins = json.loads((source / "output_sha256.json").read_text())
    (source / "output_sha256.json").unlink()
    _write(
        source / "output_sha256.json",
        {name: file_sha256(source / name) for name in pins},
    )
    return workspace, source, case_id


@pytest.mark.parametrize("workers", [1, 2])
def test_scoring_replay_of_multimode_source_recomputes_every_mode(
    scheduled_source_run, monkeypatch, workers
):
    workspace, initial, case_id = scheduled_source_run
    old_record = multimode.create_multimode_run(
        workspace, initial.name, "03c", workers=1
    )
    source = Path(old_record["path"])
    before, initial_before = snapshot(source), snapshot(initial)
    monkeypatch.setattr(
        runner, "evaluate_segment", lambda *a, **k: pytest.fail("Projection rerun")
    )
    record = multimode.create_multimode_run(
        workspace,
        source.name,
        "03d",
        workers=workers,
        scoring_overrides={"score_delay_prior_weight": 1.0},
    )
    target = Path(record["path"])
    assert snapshot(source) == before
    assert snapshot(initial) == initial_before
    relative = f"cases/{runner.artifact_key(case_id)}.json.gz"
    old, new = runner.read_case(source / relative), runner.read_case(target / relative)
    for key in ("projection", "apc_evidence", "parent_boundary"):
        assert new[key] == old[key]
    assert new["config"] == {**old["config"], "score_delay_prior_weight": 1.0}
    canonical = json.loads((target / "gtfs/canonical.json").read_text())
    expected = partial.evaluate_matching_modes(
        new["projection"], canonical, new["config"]
    )
    assert new["matching_modes"] == expected
    assert new["result"] == expected["global"]["result"]
    for mode in multimode.MODES:
        assert (
            new["matching_modes"][mode]["selection"]
            == old["matching_modes"][mode]["selection"]
        )
        current = new["matching_modes"][mode]["result"]
        previous = old["matching_modes"][mode]["result"]
        assert current["all_candidates"] and previous["all_candidates"]
        assert (
            current["all_candidates"][0]["score"]
            > previous["all_candidates"][0]["score"]
        )
        assert record["summary"]["matching_mode_status_counts"][mode] == {
            current["status"]: 1
        }
        overview = json.loads((target / f"overview/{mode}.json").read_text())
        assert overview["matching_mode"] == mode
    assert record["summary"]["status_counts"] == {new["result"]["status"]: 1}
    catalog = json.loads((target / "catalog/vehicle-test.json").read_text())[0]
    for key in (
        "gtfs_status",
        "gtfs_candidate_count",
        "pattern_confidence",
        "trip_confidence",
    ):
        assert catalog[key] == catalog["matching_modes"]["global"][key]
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["matching_scoring_replay"] is True
    assert manifest["scoring_overrides"] == {"score_delay_prior_weight": 1.0}
    assert manifest["source_scoring_values"] == {"score_delay_prior_weight": 0.05}
    assert manifest["partial_source_run_id"] == source.name
    assert manifest["stage_provenance"]["global_result_reused_unchanged"] is False
    assert manifest["stage_provenance"]["matching_modes_recomputed"] == list(
        multimode.MODES
    )
    assert (
        manifest["stage_provenance"]["upstream_stage_provenance"]
        == json.loads((source / "manifest.json").read_text())["stage_provenance"]
    )
    assert "global_result_changed" not in record["summary"]["violations"]
    for name in (
        "gtfs/canonical.json",
        "observations/vehicle-test.json",
        "selection.json",
    ):
        assert (source / name).read_bytes() == (target / name).read_bytes()
    for name, digest in json.loads((target / "output_sha256.json").read_text()).items():
        assert file_sha256(target / name) == digest


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        [],
        {"max_projection_distance_m": 150},
        {"score_delay_prior_weight": True},
        {"score_delay_prior_weight": -0.1},
        {"score_delay_prior_weight": float("nan")},
        {"score_delay_prior_weight": float("inf")},
        {"score_delay_prior_weight": "1.0"},
        {"score_delay_prior_weight": None},
    ],
)
def test_scoring_replay_rejects_invalid_or_non_scoring_overrides(source_run, overrides):
    workspace, source, _ = source_run
    before = snapshot(source)
    with pytest.raises(ValueError):
        multimode.create_multimode_run(
            workspace, source.name, "invalid", workers=1, scoring_overrides=overrides
        )
    assert snapshot(source) == before
    assert list((workspace.root / "runs").iterdir()) == [source]


def test_scoring_replay_rejects_mutation_of_new_global_result(source_run, monkeypatch):
    workspace, source, _ = source_run
    before = snapshot(source)
    evaluate = partial.evaluate_matching_modes

    def corrupt(*args, **kwargs):
        kwargs["global_result"]["status"] = "INVENTED"
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(partial, "evaluate_matching_modes", corrupt)
    with pytest.raises(ValueError, match="mutated the supplied global result"):
        multimode.create_multimode_run(
            workspace,
            source.name,
            "invalid",
            workers=1,
            scoring_overrides={"score_delay_prior_weight": 1.0},
        )
    assert snapshot(source) == before
    outputs = [p for p in (workspace.root / "runs").iterdir() if p != source]
    assert len(outputs) == 1
    assert (outputs[0] / "failure.json").is_file()
    assert not (outputs[0] / "completion.json").exists()


@pytest.fixture
def continuous_source_run(scheduled_source_run):
    workspace, initial, case_id = scheduled_source_run
    record = multimode.create_multimode_run(
        workspace,
        initial.name,
        "continuous control",
        workers=1,
        scoring_overrides={"continuous_matching_enabled": True},
    )
    return workspace, Path(record["path"]), case_id


@pytest.mark.parametrize("workers", [1, 2])
def test_continuous_weights_only_replay_preserves_evidence_and_all_modes(
    continuous_source_run, monkeypatch, workers
):
    workspace, source, case_id = continuous_source_run
    before = snapshot(source)
    monkeypatch.setattr(
        runner, "evaluate_segment", lambda *a, **k: pytest.fail("Projection rerun")
    )
    overrides = {
        "continuous_absolute_delay_weight": 10.0,
        "continuous_delay_dynamics_weight": 0.1,
        "continuous_match_cost_max": 7.3,
        "continuous_local_support_cost_max": 16.5,
        "continuous_calibration_max_score": 7.3,
        "continuous_boundary_penalty": 2.75,
        "continuous_boundary_gain_min": 2.75,
        "continuous_local_missing_cost": 22.0,
        "continuous_apc_support_cost_max": 3.0,
        "sequence_local_score_weight": 6.0 / 14.6,
    }
    record = multimode.create_multimode_run(
        workspace,
        source.name,
        "continuous scoring experiment",
        workers=workers,
        scoring_overrides=overrides,
    )
    target = Path(record["path"])
    assert snapshot(source) == before
    relative = f"cases/{runner.artifact_key(case_id)}.json.gz"
    old, new = runner.read_case(source / relative), runner.read_case(target / relative)
    for key in ("projection", "apc_evidence", "parent_boundary"):
        assert new[key] == old[key]
    assert new["config"] == old["config"] | overrides
    canonical = json.loads((target / "gtfs/canonical.json").read_text())
    expected = partial.evaluate_matching_modes(
        new["projection"], canonical, new["config"]
    )
    for mode in multimode.MODES:
        expected[mode]["result"]["diagnostics"]["runtime_s"] = new["matching_modes"][
            mode
        ]["result"]["diagnostics"]["runtime_s"]
    assert new["matching_modes"] == expected
    assert new["result"] == expected["global"]["result"]
    for mode in multimode.MODES:
        current = new["matching_modes"][mode]["result"]
        previous = old["matching_modes"][mode]["result"]
        assert current["scorer"] == "continuous_passage_v1"
        assert (
            new["matching_modes"][mode]["selection"]
            == old["matching_modes"][mode]["selection"]
        )
        assert current["all_candidates"] and previous["all_candidates"]
        candidate = current["all_candidates"][0]
        assert candidate["score_component_weights"]["absolute_delay_level_cost"] == 10
        assert candidate["score_component_weights"]["delay_dynamics_cost"] == 0.1
        for key, weight in candidate["score_component_weights"].items():
            assert candidate["score_components"][key] == pytest.approx(
                candidate["score_components_unweighted"][key] * weight
            )
        assert (
            candidate["whole_alignment"]["delay_process"]
            == previous["all_candidates"][0]["whole_alignment"]["delay_process"]
        )
        assert (
            current["all_candidates"][0]["score"]
            != previous["all_candidates"][0]["score"]
        )
        assert record["summary"]["matching_mode_status_counts"][mode] == {
            current["status"]: 1
        }
        for suffix in ("episodes", "report", "vehicle-sequence"):
            assert (target / f"shadow/{mode}-{suffix}.json").is_file()
        assert (target / f"overview/{mode}.json").is_file()
        episodes = json.loads((target / f"shadow/{mode}-episodes.json").read_text())
        assert len(episodes["old"]) == len(episodes["new"]) == 1
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["matching_scoring_replay"] is True
    assert manifest["continuous_matching_replay"] is True
    assert manifest["scoring_overrides"] == overrides
    assert manifest["source_scoring_values"] == {
        key: old["config"][key] for key in overrides
    }
    assert manifest["stage_provenance"]["matching_modes_recomputed"] == list(
        multimode.MODES
    )
    assert manifest["stage_provenance"]["global_result_reused_unchanged"] is False
    assert "continuous/delay" in manifest["partial_code_sha256"]
    assert "continuous/integration" in manifest["partial_code_sha256"]
    assert record["summary"]["scorer"] == "continuous_passage_v1"
    assert record["summary"]["shadow_evaluation_reports"] == [
        f"shadow/{mode}-report.json" for mode in multimode.MODES
    ]
    for name in (
        "gtfs/canonical.json",
        "observations/vehicle-test.json",
        "selection.json",
    ):
        assert (source / name).read_bytes() == (target / name).read_bytes()
    for name, digest in json.loads((target / "output_sha256.json").read_text()).items():
        assert file_sha256(target / name) == digest


def test_continuous_source_without_overrides_retains_scorer_identity(
    continuous_source_run,
):
    workspace, source, case_id = continuous_source_run
    before = snapshot(source)
    record = multimode.create_multimode_run(
        workspace, source.name, "continuous partial replay", workers=1
    )
    target = Path(record["path"])
    relative = f"cases/{runner.artifact_key(case_id)}.json.gz"
    old, new = runner.read_case(source / relative), runner.read_case(target / relative)
    assert new["result"] == old["result"]
    assert snapshot(source) == before
    assert record["summary"]["scorer"] == "continuous_passage_v1"
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["continuous_matching_replay"] is True
    assert manifest["matching_scoring_replay"] is False
    assert "continuous/delay" in manifest["partial_code_sha256"]
    for mode in multimode.MODES:
        assert (target / f"shadow/{mode}-report.json").is_file()


def test_continuous_relational_validation_uses_effective_source_config(
    continuous_source_run,
):
    workspace, source, _ = continuous_source_run
    first = multimode.create_multimode_run(
        workspace,
        source.name,
        "larger missing-evidence cost",
        workers=1,
        scoring_overrides={"continuous_local_missing_cost": 22.0},
    )
    # This is valid with the inherited missing cost of 22, but invalid if
    # enabled=True incorrectly validates the partial overrides against defaults.
    second = multimode.create_multimode_run(
        workspace,
        first["run_id"],
        "inherited compatible missing cost",
        workers=1,
        scoring_overrides={
            "continuous_matching_enabled": True,
            "continuous_local_support_cost_max": 16.5,
        },
    )
    assert second["config"]["gtfs"]["continuous_local_missing_cost"] == 22
    assert second["config"]["gtfs"]["continuous_local_support_cost_max"] == 16.5


@pytest.mark.parametrize("enabled_override", [None, True])
def test_continuous_incompatible_local_gates_fail_before_creating_run(
    continuous_source_run, enabled_override
):
    workspace, source, _ = continuous_source_run
    before = snapshot(source)
    existing = set((workspace.root / "runs").iterdir())
    overrides = {"continuous_local_support_cost_max": 16.5}
    if enabled_override is not None:
        overrides["continuous_matching_enabled"] = enabled_override
    with pytest.raises(ValueError, match="Missing local evidence cost must exceed"):
        multimode.create_multimode_run(
            workspace,
            source.name,
            "incompatible local gates",
            workers=1,
            scoring_overrides=overrides,
        )
    assert snapshot(source) == before
    assert set((workspace.root / "runs").iterdir()) == existing


def test_continuous_override_provenance_uses_parameter_defaults(scheduled_source_run):
    workspace, source, _ = scheduled_source_run
    # Simulate a legacy source that predates the continuous configuration fields.
    cfg = json.loads((source / "config.json").read_text())
    for key in multimode.CONTINUOUS_SCORING_OVERRIDE_KEYS:
        cfg["gtfs"].pop(key, None)
    (source / "config.json").unlink()
    _write(source / "config.json", cfg)
    pins = json.loads((source / "output_sha256.json").read_text())
    pins["config.json"] = file_sha256(source / "config.json")
    (source / "output_sha256.json").unlink()
    _write(source / "output_sha256.json", pins)
    before = snapshot(source)
    overrides = {
        "continuous_matching_enabled": True,
        "continuous_absolute_delay_weight": 10.0,
        "continuous_delay_dynamics_weight": 0.1,
        "continuous_match_cost_max": 7.3,
        "continuous_local_support_cost_max": 16.5,
        "continuous_calibration_max_score": 7.3,
        "continuous_local_missing_cost": 22.0,
    }
    record = multimode.create_multimode_run(
        workspace,
        source.name,
        "continuous activation",
        workers=1,
        scoring_overrides=overrides,
    )
    manifest = json.loads((Path(record["path"]) / "manifest.json").read_text())
    assert manifest["source_scoring_values"] == {
        "continuous_matching_enabled": False,
        "continuous_absolute_delay_weight": 1.0,
        "continuous_delay_dynamics_weight": 0.5,
        "continuous_match_cost_max": 3.0,
        "continuous_local_support_cost_max": 3.0,
        "continuous_calibration_max_score": 3.0,
        "continuous_local_missing_cost": 4.0,
    }
    assert snapshot(source) == before


@pytest.mark.parametrize("key", sorted(multimode.CONTINUOUS_SCORING_OVERRIDE_KEYS))
def test_continuous_only_overrides_cannot_silently_apply_to_legacy(source_run, key):
    workspace, source, _ = source_run
    before = snapshot(source)
    with pytest.raises(ValueError, match="require the continuous scorer"):
        multimode.create_multimode_run(
            workspace,
            source.name,
            "wrong scorer",
            workers=1,
            scoring_overrides={key: config.defaults()[key]},
        )
    assert snapshot(source) == before
    assert list((workspace.root / "runs").iterdir()) == [source]


@pytest.mark.parametrize("key", sorted(multimode.CONTINUOUS_SCORING_OVERRIDE_KEYS))
@pytest.mark.parametrize("value", [True, -0.1, float("nan"), float("inf"), "1", None])
def test_continuous_invalid_numeric_overrides_fail_before_creating_run(
    source_run, key, value
):
    workspace, source, _ = source_run
    before = snapshot(source)
    with pytest.raises(ValueError):
        multimode.create_multimode_run(
            workspace,
            source.name,
            "invalid",
            workers=1,
            scoring_overrides={"continuous_matching_enabled": True, key: value},
        )
    assert snapshot(source) == before
    assert list((workspace.root / "runs").iterdir()) == [source]


def test_numeric_replay_cli_passes_only_explicit_overrides(monkeypatch):
    from tools.merge_review import debug

    captured = {}
    overrides = {
        "continuous_absolute_delay_weight": 10.0,
        "continuous_delay_dynamics_weight": 0.1,
        "continuous_match_cost_max": 7.3,
        "continuous_local_support_cost_max": 16.5,
        "continuous_calibration_max_score": 7.3,
        "continuous_boundary_penalty": 2.75,
        "continuous_boundary_gain_min": 2.75,
        "continuous_local_missing_cost": 22.0,
        "continuous_apc_support_cost_max": 3.0,
        "sequence_local_score_weight": 6.0 / 14.6,
    }

    def capture(workspace, source, label, **kwargs):
        captured.update(kwargs)
        return {"source": source, "label": label}

    monkeypatch.setattr(debug, "DebugWorkspace", lambda *args: object())
    monkeypatch.setattr(multimode, "create_multimode_run", capture)
    args = ["multimode", "--source", "source", "--label", "experiment"]
    for key, value in overrides.items():
        args.extend(["--" + key.replace("_", "-"), str(value)])
    monkeypatch.setattr(sys, "argv", args)
    multimode.main()
    assert captured["scoring_overrides"] == overrides
