"""Retirement safety using synthetic validation builds only."""

import copy
import json
import os

import pytest

from tools.merge_review import validation_cleanup as v


def put(path, value="derived copy"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value, dict) else value)
    return path


@pytest.fixture
def build_fixture(tmp_path, monkeypatch):
    root = tmp_path / "agency"
    build = root / "artifacts/workbench-builds/validation-test"
    raw = put(root / "database_offline/avl/source.csv", "irreplaceable original")
    source = root / "artifacts/retained-run"
    put(source / "manifest.json", {"original": True})
    release = put(
        root / "releases/release-test/release.json",
        {
            "schema": "frozen-workbench-v1",
            "raw_input_sha256": {"database_offline/avl/source.csv": v._sha(raw)},
        },
    )
    inputs = {
        "schema": "frozen-workbench-v1",
        "old_results_used_as_inputs": False,
        "release_id": "release-test",
        "release_sha256": v._sha(release),
        "raw_input_sha256": {"database_offline/avl/source.csv": v._sha(raw)},
    }
    put(build / "build-inputs.json", inputs)
    put(
        build / "build-failure.json",
        {**inputs, "status": "FAIL", "error": "finished error"},
    )
    run = build / "workspace/runs/run-test"
    put(run / "run.json", {"status": "complete"})
    put(run / "completion.json", {"validation_ok": True})
    put(run / "manifest.json", {"test": True})
    put(run / "cases/data.gz", "large disposable candidate")
    put(build / "inputs/raw_audit/avl.parquet", "normalized raw copy")
    put(
        build / "inputs/raw_audit/inventory.json",
        {
            "sources": {"avl": {"path": str(raw), "sha256": v._sha(raw)}},
        },
    )
    monkeypatch.setattr(v, "_active_processes", lambda build: [])
    return dict(
        root=root,
        build=build,
        raw=raw,
        source=source,
        release=release,
        run=run,
        inputs=inputs,
        audit=root / "artifacts/retirement-audits/test",
    )


def successful(f):
    (f["build"] / "build-failure.json").unlink()
    put(
        f["build"] / "build-complete.json",
        {
            **f["inputs"],
            "status": "PASS",
            "run_path": str(f["run"]),
            "run_id": f["run"].name,
        },
    )
    return put(
        f["root"] / "comparison.json",
        {
            "schema_version": "rebuild-semantic-audit-v1",
            "status": "PASS",
            "semantic_equal": True,
            "difference_count": 0,
            "rebuilt_run": str(f["run"]),
            "source_run": str(f["source"]),
        },
    )


def plan(f, **kwargs):
    return v.plan_validation_build(
        f["build"],
        f["audit"],
        expected_build_inputs_sha256=v._sha(f["build"] / "build-inputs.json"),
        **kwargs,
    )


def test_failed_copy_plan_does_not_delete_and_purge_preserves_originals(build_fixture):
    f = build_fixture
    raw_before = v._signature(f["raw"])
    p = plan(f)
    assert (f["run"] / "cases/data.gz").is_file()
    result = v.purge_validation_build(f["audit"], expected_plan_sha256=v._sha(p))
    assert result["status"] == "PURGED" and not f["build"].exists()
    assert v._signature(f["raw"]) == raw_before
    assert (f["source"] / "manifest.json").is_file()
    assert (f["audit"] / "retained-metadata/build-failure.json").is_file()
    assert (
        f["audit"] / "retained-metadata/workspace/runs/run-test/manifest.json"
    ).is_file()
    assert not (f["audit"] / "retained-metadata/inputs/raw_audit/avl.parquet").exists()


def test_success_copy_requires_and_archives_exact_comparison(build_fixture):
    f = build_fixture
    report = successful(f)
    with pytest.raises(ValueError, match="passing comparison"):
        plan(f)
    result = v.retire_validation_build(
        f["build"],
        f["audit"],
        expected_build_inputs_sha256=v._sha(f["build"] / "build-inputs.json"),
        passing_comparison_report=report,
    )
    assert result["status"] == "PURGED"
    assert (f["audit"] / "comparison.json").read_bytes() == report.read_bytes()


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "FAIL"),
        ("semantic_equal", False),
        ("difference_count", 1),
        ("schema_version", "unknown"),
        ("rebuilt_run", "/tmp/wrong-run"),
        ("source_run", ""),
    ],
)
def test_wrong_comparison_rejected(build_fixture, field, value):
    f = build_fixture
    report = successful(f)
    data = json.loads(report.read_text())
    data[field] = value
    put(report, data)
    with pytest.raises(ValueError):
        plan(f, passing_comparison_report=report)
    assert f["build"].exists() and not f["audit"].exists()


@pytest.mark.parametrize("target", ["root", "wrong-name", "nested", "traversal"])
def test_only_direct_validation_build_scope_allowed(build_fixture, target):
    f = build_fixture
    selected = {
        "root": f["build"].parent,
        "wrong-name": f["build"].parent / "production-run",
        "nested": f["build"] / "validation-nested",
        "traversal": f["build"].parent / "../workbench-builds/validation-test",
    }[target]
    with pytest.raises(ValueError):
        v.plan_validation_build(selected, f["audit"], expected_build_inputs_sha256="x")


def test_input_identity_pin_is_mandatory_and_terminal_identity_matches(build_fixture):
    f = build_fixture
    with pytest.raises(ValueError, match="SHA-256"):
        v.plan_validation_build(
            f["build"], f["audit"], expected_build_inputs_sha256="0" * 64
        )
    failure = json.loads((f["build"] / "build-failure.json").read_text())
    failure["release_id"] = "different-release"
    put(f["build"] / "build-failure.json", failure)
    with pytest.raises(ValueError, match="Terminal record"):
        plan(f)


@pytest.mark.parametrize(
    "mutation", ["review", "symlink", "hardlink", "active-run", "live-process"]
)
def test_non_disposable_or_active_build_rejected(build_fixture, monkeypatch, mutation):
    f = build_fixture
    if mutation == "review":
        put(f["build"] / "workspace/reviews/reviews.sqlite3", "human notes")
    elif mutation == "symlink":
        (f["build"] / "linked.csv").symlink_to(f["raw"])
    elif mutation == "hardlink":
        os.link(f["raw"], f["build"] / "linked.csv")
    elif mutation == "active-run":
        put(f["build"] / "workspace/runs/in-progress/manifest.json", {})
    else:
        monkeypatch.setattr(v, "_active_processes", lambda build: [123])
    with pytest.raises(ValueError):
        plan(f)
    assert f["raw"].read_text() == "irreplaceable original"


@pytest.mark.parametrize(
    "mutation",
    ["modified", "foreign", "foreign-dir", "raw", "archive", "plan", "report"],
)
def test_changes_after_plan_prevent_any_deletion(build_fixture, mutation):
    f = build_fixture
    report = successful(f)
    p = plan(f, passing_comparison_report=report)
    digest = v._sha(p)
    if mutation == "foreign-dir":
        (f["build"] / "unexpected-empty-directory").mkdir()
    else:
        target = {
            "modified": f["run"] / "cases/data.gz",
            "foreign": f["build"] / "new-file",
            "raw": f["raw"],
            "archive": f["audit"] / "retained-metadata/build-inputs.json",
            "plan": p,
            "report": report,
        }[mutation]
        put(target, "changed")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        v.purge_validation_build(f["audit"], expected_plan_sha256=digest)
    assert (f["run"] / "manifest.json").exists()
    assert not (f["audit"] / "purge-started.json").exists()


def test_raw_audit_cannot_disguise_local_copy_as_original(build_fixture):
    f = build_fixture
    local = f["build"] / "inputs/raw_audit/avl.parquet"
    put(
        f["build"] / "inputs/raw_audit/inventory.json",
        {
            "sources": {"avl": {"path": str(local), "sha256": v._sha(local)}},
        },
    )
    with pytest.raises(ValueError, match="original CSV provenance"):
        plan(f)


def test_new_audit_required_and_must_not_touch_protected_trees(build_fixture):
    f = build_fixture
    for target in (
        f["build"] / "audit",
        f["root"] / "releases/audit",
        f["root"] / "database_offline/audit",
    ):
        with pytest.raises(ValueError):
            v.plan_validation_build(
                f["build"],
                target,
                expected_build_inputs_sha256=v._sha(f["build"] / "build-inputs.json"),
            )
    f["audit"].mkdir(parents=True)
    with pytest.raises(ValueError, match="New external"):
        plan(f)


def test_missing_terminal_marker_and_old_result_input_rejected(build_fixture):
    f = build_fixture
    (f["build"] / "build-failure.json").unlink()
    with pytest.raises(ValueError, match="terminal record"):
        plan(f)
    inputs = copy.deepcopy(f["inputs"])
    inputs["old_results_used_as_inputs"] = True
    put(f["build"] / "build-inputs.json", inputs)
    with pytest.raises(ValueError, match="independently rebuilt"):
        plan(f)
