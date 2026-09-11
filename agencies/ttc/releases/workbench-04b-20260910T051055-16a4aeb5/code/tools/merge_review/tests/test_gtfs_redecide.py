"""Final-decision replay is append-only and cannot silently recompute evidence.

These small gzip fixtures exercise the replay orchestrator, not the matching or
decision rules. The parent-lineage validator and final guard are explicit mocks;
hash checks, gzip round trips, manifests, catalogs and publication gates are real.
"""

import copy
import gzip
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from tools.merge_review import gtfs_matching
from tools.merge_review.debug import DebugWorkspace, _write
from tools.merge_review.gtfs_matching import (
    METHOD,
    REPRESENTATION,
    adapter,
    redecide,
    runner,
)
from tools.merge_review.gtfs_matching.feed import file_sha256
from tools.merge_review.gtfs_matching.runner import artifact_key, read_case, write_case


def read_json(path):
    return json.loads(path.read_text())


def snapshot(folder):
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }


def conservative_guard(result, config):
    updated = copy.deepcopy(result)
    if updated["status"] == "MATCHED":
        updated["status"] = "AMBIGUOUS"
        updated["candidates"] = updated["candidates"][:1]
    updated["reasons"] = ["FINAL_DECISION_GUARD"]
    updated["trip_confidence"]["calibrated_probability"] = None
    return updated


@pytest.fixture
def replay_fixture(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    source = root / "runs" / "run-20231106-heavy-source"
    source.mkdir(parents=True)
    workspace = SimpleNamespace(root=root, date="2023-11-06")
    workspace.get_run = lambda run_id: DebugWorkspace.get_run(workspace, run_id)
    validator_calls = []

    class ParentValidator:
        def __init__(self, folder):
            validator_calls.append(("init", folder))

        def _verify_parent(self):
            validator_calls.append(("verify_parent", source))

    monkeypatch.setattr(adapter, "GTFSReviewData", ParentValidator)
    # Work independently of an in-progress decision implementation. Hashing the
    # existing test file still exercises real decision-stage digest recording.
    decision = ModuleType("tools.merge_review.gtfs_matching.decision")
    decision.__file__ = __file__
    decision.apply_decision_guard = conservative_guard
    monkeypatch.setitem(sys.modules, decision.__name__, decision)
    monkeypatch.setattr(gtfs_matching, "decision", decision, raising=False)

    def forbid_heavy_computation(*args, **kwargs):
        pytest.fail("A final-decision replay must not reevaluate a segment")

    monkeypatch.setattr(runner, "evaluate_segment", forbid_heavy_computation)
    config = {"method": METHOD, "segmentation": {}, "merge": {}, "gtfs": {"k": 3}}
    _write(source / "config.json", config)
    _write(source / "canonical.json", {"references": [{"id": "reference-29"}]})
    _write(
        source / "observations" / "vehicle-test.json",
        [
            {"source": "avl", "rid": "original-avl", "time": 100, "Heading": "#N/A"},
            {"source": "apc", "rid": "original-apc", "arrival": 101},
        ],
    )
    _write(
        source / "manifest.json",
        {
            "run_id": source.name,
            "representation": REPRESENTATION,
            "route_scope": ["29"],
            "code_sha256": {
                "projection.py": "historical-projection",
                "scoring.py": "historical-scoring",
            },
            "direction_parent_run_id": "run-20231106-direction-parent",
            "direction_parent_sha256": {"manifest.json": "pinned-direction-parent"},
        },
    )
    case_files = []
    catalog = []
    for number, status in enumerate(("MATCHED", "PATTERN_ONLY")):
        case_id = f"direction:test:{number:04}"
        candidate = {
            "candidate_id": f"feed|2023-11-06|trip-{number}",
            "reference_id": "reference-29",
            "score": 0.21,
            "probability": None,
            "whole_alignment": {
                "delay_process": [
                    {
                        "source_id": "avl|original-avl",
                        "source_rid": "original-avl",
                        "t": 100,
                        "delay_s": 10,
                        "corrected_t": 90,
                        "fragment_id": "fragment-a",
                    }
                ],
            },
        }
        result = {
            "status": status,
            "candidates": [
                copy.deepcopy(candidate),
                {**copy.deepcopy(candidate), "candidate_id": "alternative"},
            ],
            "pattern_confidence": {
                "support_score": 0.5,
                "calibrated_probability": None,
            },
            "trip_confidence": {"support_score": 0.2, "calibrated_probability": None},
            "all_candidates": [candidate],
            "local_scores": {"candidate": [{"window_id": "w1", "score": 0.123}]},
            "windows": {"windows": [{"id": "w1", "start": 90, "end": 110}]},
            "retrieval": {"candidate_ids": [candidate["candidate_id"]]},
            "whole_trip": {"raw_cost": 0.4321, "diagnostic": [1, 2, 3]},
            "reasons": ["ORIGINAL_DECISION"],
        }
        artifact = {
            "case_id": case_id,
            "config": copy.deepcopy(config["gtfs"]),
            "projection": {
                "points": [
                    {"source": "avl", "rid": "original-avl", "t": 100, "s_m": 42}
                ]
            },
            "observations": [{"source": "apc", "rid": "original-apc", "arrival": 101}],
            "result": result,
        }
        relative = f"cases/{artifact_key(case_id)}.json.gz"
        write_case(source / relative, artifact)
        case_files.append(relative)
        catalog.append(
            {
                "id": case_id,
                "vehicle": "test",
                "start": 90,
                "end": 110,
                "gtfs_status": status,
                "gtfs_candidate_count": 2,
                "pattern_confidence": result["pattern_confidence"],
                "trip_confidence": result["trip_confidence"],
            }
        )
    _write(source / "catalog" / "vehicle-test.json", catalog)
    _write(
        source / "summary.json",
        {
            "status_counts": {"MATCHED": 1, "PATTERN_ONLY": 1},
            "elapsed_s": 4321.5,
            "violations": {"original_field_changes": 0},
            "source_counts": {"avl": 1, "apc": 1},
            "segments": 2,
        },
    )
    pinned = {name: file_sha256(source / name) for name in snapshot(source)}
    _write(source / "output_sha256.json", pinned)
    _write(
        source / "run.json",
        {"run_id": source.name, "config": config, "status": "complete"},
    )
    _write(
        source / "completion.json", {"debug_date": "2023-11-06", "validation_ok": True}
    )
    return SimpleNamespace(
        workspace=workspace,
        source=source,
        decision=decision,
        case_files=case_files,
        validator_calls=validator_calls,
    )


def generated_folders(fixture):
    return [
        p for p in (fixture.workspace.root / "runs").iterdir() if p != fixture.source
    ]


def assert_failed_unpublished(fixture, error):
    generated = generated_folders(fixture)
    assert len(generated) == 1
    assert not (generated[0] / "completion.json").exists()
    assert not (generated[0] / "run.json").exists()
    failure = read_json(generated[0] / "failure.json")
    assert error in failure["error"]
    assert failure["publishable"] is False


def test_replay_appends_new_run_preserving_source_heavy_evidence_and_provenance(
    replay_fixture,
):
    f = replay_fixture
    before = snapshot(f.source)
    progress = []
    record = redecide.create_redecision(
        f.workspace, f.source.name, "Conservative final decision", progress.append
    )
    target = Path(record["path"])
    assert target != f.source and target.parent == f.source.parent
    assert snapshot(f.source) == before
    assert record["label"] == "Conservative final decision"
    assert record["status"] == "complete"
    assert record["parent_run_id"] == f.source.name
    assert record["method"] == METHOD and record["representation"] == REPRESENTATION
    assert f.workspace.get_run(record["run_id"]) == target
    for relative in ("config.json", "canonical.json", "observations/vehicle-test.json"):
        assert (target / relative).read_bytes() == before[relative]
    for relative in f.case_files:
        original, updated = read_case(f.source / relative), read_case(target / relative)
        assert {k: v for k, v in updated.items() if k != "result"} == {
            k: v for k, v in original.items() if k != "result"
        }
        for key in redecide.HEAVY_RESULT_FIELDS:
            assert updated["result"][key] == original["result"][key]
        assert updated["result"] == conservative_guard(
            original["result"], original["config"]
        )
    summary = read_json(target / "summary.json")
    assert summary["status_counts"] == {"AMBIGUOUS": 1, "PATTERN_ONLY": 1}
    assert summary["decision_transitions"] == {
        "MATCHED -> AMBIGUOUS": 1,
        "PATTERN_ONLY -> PATTERN_ONLY": 1,
    }
    assert summary["upstream_processing_elapsed_s"] == 4321.5
    assert summary["source_counts"] == {"avl": 1, "apc": 1}
    assert summary["accuracy"] is None
    assert summary["probability_calibrated"] is summary["publishable"] is False
    assert summary["violations"]["heavy_evidence_changed"] == 0
    catalog = read_json(target / "catalog/vehicle-test.json")
    assert [row["gtfs_status"] for row in catalog] == ["AMBIGUOUS", "PATTERN_ONLY"]
    assert [row["gtfs_candidate_count"] for row in catalog] == [1, 2]
    assert all(
        row["trip_confidence"]["calibrated_probability"] is None for row in catalog
    )
    manifest = read_json(target / "manifest.json")
    original_manifest = read_json(f.source / "manifest.json")
    assert manifest["code_sha256"] == original_manifest["code_sha256"]
    assert (
        manifest["direction_parent_sha256"]
        == original_manifest["direction_parent_sha256"]
    )
    assert manifest["decision_source_run_id"] == f.source.name
    assert manifest["decision_source_path"] == str(f.source)
    assert manifest["decision_replay"] is True
    assert manifest["stage_provenance"] == {
        "upstream_code_sha256": original_manifest["code_sha256"],
        "upstream_evidence_run_id": f.source.name,
        "recomputed_stages": ["final_decision"],
        "heavy_scores_recomputed": False,
    }
    assert manifest["decision_code_sha256"] == {
        "decision.py": file_sha256(Path(f.decision.__file__)),
        "redecide.py": file_sha256(Path(redecide.__file__)),
    }
    for relative, digest in manifest["decision_source_sha256"].items():
        assert file_sha256(f.source / relative) == digest
    for relative, digest in read_json(target / "output_sha256.json").items():
        assert file_sha256(target / relative) == digest
    assert [p["stage"] for p in progress] == ["final_decision_replay"] * 2
    assert [(p["completed"], p["total"]) for p in progress] == [(1, 2), (2, 2)]
    assert [kind for kind, _ in f.validator_calls] == ["init", "init", "verify_parent"]


@pytest.mark.parametrize(
    "relative", ["config.json", "canonical.json", "observations/vehicle-test.json"]
)
def test_tampered_pinned_source_is_rejected_before_creating_run(
    replay_fixture, relative
):
    f = replay_fixture
    with (f.source / relative).open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="source changed"):
        redecide.create_redecision(f.workspace, f.source.name, "Must reject")
    assert generated_folders(f) == []


@pytest.mark.parametrize("key", redecide.HEAVY_RESULT_FIELDS)
@pytest.mark.parametrize("in_place", [False, True])
def test_guard_cannot_change_heavy_fields_even_in_place(replay_fixture, key, in_place):
    f = replay_fixture
    before = snapshot(f.source)

    def corrupt_guard(result, config):
        changed = result if in_place else copy.deepcopy(result)
        changed[key] = {"silently_recomputed": True}
        return changed

    f.decision.apply_decision_guard = corrupt_guard
    with pytest.raises(ValueError, match="heavy matching evidence"):
        redecide.create_redecision(f.workspace, f.source.name, "Must reject")
    assert snapshot(f.source) == before
    assert_failed_unpublished(f, "heavy matching evidence")


def test_guard_cannot_mutate_persisted_config_that_is_not_a_final_decision(
    replay_fixture,
):
    f = replay_fixture

    def corrupt_guard(result, config):
        config["k"] = 999
        return conservative_guard(result, config)

    f.decision.apply_decision_guard = corrupt_guard
    record = redecide.create_redecision(
        f.workspace, f.source.name, "Isolated guard input"
    )
    for relative in f.case_files:
        assert (
            read_case(Path(record["path"]) / relative)["config"]
            == read_case(f.source / relative)["config"]
        )


def test_more_than_three_final_candidates_is_unpublished(replay_fixture):
    f = replay_fixture

    def over_cap(result, config):
        changed = copy.deepcopy(result)
        changed["candidates"] = changed["candidates"] * 2
        return changed

    f.decision.apply_decision_guard = over_cap
    with pytest.raises(ValueError, match="candidate cap"):
        redecide.create_redecision(f.workspace, f.source.name, "Must reject")
    assert_failed_unpublished(f, "candidate cap")


@pytest.mark.parametrize("relative_kind", ["parent", "absolute", "symlink"])
def test_pinned_paths_cannot_escape_source_run(replay_fixture, tmp_path, relative_kind):
    f = replay_fixture
    outside = tmp_path / "private-evidence.json"
    outside.write_text('{"not_in_run": true}')
    if relative_kind == "parent":
        relative = "../../../private-evidence.json"
    elif relative_kind == "absolute":
        relative = str(outside)
    else:
        (f.source / "escaped.json").symlink_to(outside)
        relative = "escaped.json"
    pinned = read_json(f.source / "output_sha256.json")
    pinned[relative] = file_sha256(outside)
    (f.source / "output_sha256.json").write_text(json.dumps(pinned))
    with pytest.raises(ValueError, match="escapes"):
        redecide.create_redecision(f.workspace, f.source.name, "Must reject")
    assert generated_folders(f) == []
    assert outside.read_text() == '{"not_in_run": true}'


@pytest.mark.parametrize(
    "run_id", ["../run-20231106-heavy-source", "/tmp/run-20231106-heavy-source"]
)
def test_source_run_id_cannot_escape_workspace(replay_fixture, run_id):
    with pytest.raises(ValueError, match="Invalid debug run ID"):
        redecide.create_redecision(replay_fixture.workspace, run_id, "Must reject")
    assert generated_folders(replay_fixture) == []


def test_replay_of_a_decision_replay_is_rejected(replay_fixture):
    f = replay_fixture
    first = redecide.create_redecision(f.workspace, f.source.name, "First replay")
    before = snapshot(f.workspace.root)
    with pytest.raises(ValueError, match="not an earlier decision replay"):
        redecide.create_redecision(f.workspace, first["run_id"], "Recursive replay")
    assert snapshot(f.workspace.root) == before


def test_incomplete_or_unvalidated_source_is_rejected(replay_fixture):
    f = replay_fixture
    (f.source / "completion.json").write_text(
        '{"debug_date":"2023-11-06","validation_ok":false}'
    )
    with pytest.raises(ValueError, match="did not complete validation"):
        redecide.create_redecision(f.workspace, f.source.name, "Must reject")
    assert generated_folders(f) == []


def test_source_modified_during_replay_is_never_completed(replay_fixture):
    f = replay_fixture

    def changing_source(result, config):
        with (f.source / "canonical.json").open("ab") as stream:
            stream.write(b" ")
        return conservative_guard(result, config)

    f.decision.apply_decision_guard = changing_source
    with pytest.raises(ValueError, match="changed during replay"):
        redecide.create_redecision(f.workspace, f.source.name, "Must reject")
    assert_failed_unpublished(f, "changed during replay")


@pytest.mark.parametrize("phase", ["initial", "final"])
def test_parent_lineage_must_validate_before_and_after_replay(
    replay_fixture, monkeypatch, phase
):
    f = replay_fixture
    before = snapshot(f.source)

    class RejectParent:
        def __init__(self, folder):
            if phase == "initial":
                raise ValueError("Parent lineage changed")

        def _verify_parent(self):
            raise ValueError("Parent lineage changed")

    monkeypatch.setattr(adapter, "GTFSReviewData", RejectParent)
    with pytest.raises(ValueError, match="Parent lineage changed"):
        redecide.create_redecision(f.workspace, f.source.name, "Must reject")
    assert snapshot(f.source) == before
    if phase == "initial":
        assert generated_folders(f) == []
    else:
        assert_failed_unpublished(f, "Parent lineage changed")


def test_even_hash_pinned_nonmonotone_evidence_cannot_be_published(replay_fixture):
    f = replay_fixture
    relative = f.case_files[0]
    artifact = read_case(f.source / relative)
    artifact["projection"]["points"].append(
        {"source": "avl", "rid": "next-avl", "t": 110}
    )
    artifact["result"]["all_candidates"][0]["whole_alignment"]["delay_process"].append(
        {
            "source_id": "avl|next-avl",
            "source_rid": "next-avl",
            "t": 110,
            "delay_s": 30,
            "corrected_t": 80,
            "fragment_id": "fragment-a",
        }
    )
    (f.source / relative).write_bytes(
        gzip.compress(json.dumps(artifact).encode(), mtime=0)
    )
    pinned = read_json(f.source / "output_sha256.json")
    pinned[relative] = file_sha256(f.source / relative)
    (f.source / "output_sha256.json").write_text(json.dumps(pinned))
    before = snapshot(f.source)
    with pytest.raises(ValueError, match="nonmonotone delay clock"):
        redecide.create_redecision(f.workspace, f.source.name, "Must reject")
    assert snapshot(f.source) == before
    assert_failed_unpublished(f, "nonmonotone delay clock")
