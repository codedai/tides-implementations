"""Frozen release/bootstrap contracts; never freeze or rebuild the real dataset."""

import copy
import json
import sys

import pytest

from tools.merge_review import workbench as w


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value, dict) else value)
    return path


@pytest.fixture
def released(tmp_path, monkeypatch):
    root = tmp_path / "data"
    release = root / "releases/test"
    source = put(
        release / "code/tools/merge_review/workbench.py", "# frozen bootstrap\n"
    )
    settings = put(
        release / "profiles.json", {"04b": {"method": "gtfs_trip_matching", "gtfs": {}}}
    )
    environment = {
        "python": "3.12.9",
        "implementation": "CPython",
        "packages": {"polars": "1.0"},
    }
    monkeypatch.setattr(w, "environment", lambda: copy.deepcopy(environment))
    manifest = {
        "schema": w.SCHEMA,
        "release_id": "test",
        "code_sha256": {"tools/merge_review/workbench.py": w.sha(source)},
        "profiles_sha256": w.sha(settings),
        "environment": environment,
        "date": "2023-11-06",
        "route_scope": ["29"],
        "raw_input_sha256": {},
    }
    for key in (
        "database_offline/avl/20240401_Vehicle GPS Rt29 RT 929 Nov 2023.csv",
        "database_offline/apc/ttc_apc_data.csv",
    ):
        manifest["raw_input_sha256"][key] = w.sha(put(root / key, "raw"))
    put(release / "release.json", manifest)
    put(
        release / "release.sha256.json",
        {"release.json": w.sha(release / "release.json")},
    )
    return root, release, manifest


def test_verifies_complete_release_and_raw_pins(released):
    root, release, manifest = released
    assert w.verify_release(release) == manifest
    w.verify_inputs(root, manifest)


def test_freeze_copies_verified_code_without_result_data(released, monkeypatch):
    root, old_release, _ = released
    source_code = old_release / "code"
    monkeypatch.setattr(w, "CODE_ROOT", source_code)
    monkeypatch.setattr(w, "CODE_DIRS", ())
    monkeypatch.setattr(w, "SOURCE_FILES", ("tools/merge_review/workbench.py",))
    put(
        root / "artifacts/feed_manifest.draft.csv",
        "status,calendar_min,calendar_max,relative_path\n",
    )
    run = root / "saved"
    put(run / "run.json", {"status": "complete"})
    put(run / "completion.json", {"validation_ok": True})
    put(run / "config.json", {"method": "gtfs_trip_matching", "gtfs": {}})
    put(run / "output_sha256.json", {"config.json": w.sha(run / "config.json")})
    put(run / "cases/heavy.json", "must not be copied")
    pointer = w.freeze(root, {"04b": run})
    release = root / pointer["release"]
    manifest = w.verify_release(release, pointer["sha256"])
    assert manifest["business_results_embedded"] is False
    assert set(manifest["code_sha256"]) == {"tools/merge_review/workbench.py"}
    assert len(manifest["raw_input_sha256"]) == 3
    assert not (release / "code/cases").exists()
    put(run / "config.json", {"tampered": True})
    with pytest.raises(ValueError, match="completed output pins"):
        w.freeze(root, {"04b": run})


@pytest.mark.parametrize(
    "name", ["injected.py", "__pycache__/injected.pyc", "native.so", "data.json"]
)
def test_extra_unpinned_code_rejected(released, name):
    _, release, _ = released
    put(release / "code" / name, "unlocked code")
    with pytest.raises(ValueError, match="extra unpinned"):
        w.verify_release(release)


@pytest.mark.parametrize("which", ["code", "profile", "manifest"])
def test_modified_release_bytes_rejected(released, which):
    _, release, _ = released
    path = {
        "code": "code/tools/merge_review/workbench.py",
        "profile": "profiles.json",
        "manifest": "release.json",
    }[which]
    put(release / path, "changed")
    with pytest.raises(ValueError):
        w.verify_release(release)


@pytest.mark.parametrize("which", ["code", "profile", "root_ancestor"])
def test_symlink_in_any_frozen_path_rejected(released, tmp_path, which):
    _, release, _ = released
    if which == "root_ancestor":
        link = tmp_path / "alias"
        link.symlink_to(release.parent, target_is_directory=True)
        release = link / release.name
    else:
        relative = (
            "profiles.json"
            if which == "profile"
            else "code/tools/merge_review/workbench.py"
        )
        path = release / relative
        copy_path = put(tmp_path / "identical-bytes", path.read_text())
        path.unlink()
        path.symlink_to(copy_path)
    with pytest.raises(ValueError, match="Symlink"):
        w.verify_release(release)


@pytest.mark.parametrize(
    "difference", ["python", "implementation", "version", "extra", "missing"]
)
def test_environment_mismatch_rejected(released, monkeypatch, difference):
    _, release, manifest = released
    actual = copy.deepcopy(manifest["environment"])
    if difference in {"python", "implementation"}:
        actual[difference] = "different"
    elif difference == "extra":
        actual["packages"]["unlocked"] = "1"
    elif difference == "missing":
        actual["packages"].pop("polars")
    else:
        actual["packages"]["polars"] = "2"
    monkeypatch.setattr(w, "environment", lambda: actual)
    with pytest.raises(ValueError):
        w.verify_release(release)


def test_raw_input_mutation_rejected(released):
    root, _, manifest = released
    put(root / next(iter(manifest["raw_input_sha256"])), "different CSV")
    with pytest.raises(ValueError, match="Raw input changed"):
        w.verify_inputs(root, manifest)


def test_mutable_checkout_cannot_run_pipeline_api(released):
    root, release, _ = released
    with pytest.raises(ValueError, match="verified frozen business code"):
        w.run_pipeline(root, release, root / "new-build")
    assert not (root / "new-build").exists()


@pytest.mark.parametrize("workers", [0, 9, True, 1.5])
def test_workers_rejected_before_any_output(released, monkeypatch, workers):
    root, release, _ = released
    monkeypatch.setattr(w, "CODE_ROOT", release / "code")
    with pytest.raises(ValueError, match="Workers"):
        w.run_pipeline(root, release, root / "new-build", workers=workers)
    assert not (root / "new-build").exists()


@pytest.mark.parametrize("target", ["existing", "database_offline/new", "releases/new"])
def test_output_cannot_overwrite_or_write_into_inputs(released, monkeypatch, target):
    root, release, _ = released
    monkeypatch.setattr(w, "CODE_ROOT", release / "code")
    if target == "existing":
        (root / target).mkdir()
    with pytest.raises(ValueError):
        w.run_pipeline(root, release, root / target)


def fake_business(monkeypatch):
    from tools.merge_review import debug, raw_bootstrap

    calls = []

    class Workspace:
        def __init__(self, root, audit, date):
            self.root = root
            calls.append(("workspace", root, audit, date))

        def create_run(self, config, **kwargs):
            calls.append(("create", copy.deepcopy(config), kwargs))
            return {
                "run_id": "direction" if len(calls) == 3 else "matched",
                "path": str(self.root / "runs/result"),
                "summary": {"vehicles": 1},
            }

    def bootstrap(*args, **kwargs):
        calls.append(("bootstrap", args, kwargs))
        return {"source_counts": {"avl": 2, "apc": 1}}

    monkeypatch.setattr(debug, "DebugWorkspace", Workspace)
    monkeypatch.setattr(raw_bootstrap, "bootstrap_raw_day", bootstrap)
    monkeypatch.setattr(w, "validate_completed_run", lambda path: {"status": "PASS"})
    return calls


def test_pipeline_uses_raw_bootstrap_direction_then_gtfs_with_correct_api(
    released, monkeypatch
):
    root, release, manifest = released
    monkeypatch.setattr(w, "CODE_ROOT", release / "code")
    calls = fake_business(monkeypatch)
    result = w.run_pipeline(
        root, release, root / "new-build", workers=2, progress=lambda value: None
    )
    assert result["status"] == "PASS"
    assert result["old_results_used_as_inputs"] is False
    assert [call[0] for call in calls] == ["bootstrap", "workspace", "create", "create"]
    assert calls[0][2]["date"] == manifest["date"]
    direction, match = calls[2:]
    assert direction[1]["method"] == "direction_destination"
    assert "parent_run_id" not in direction[2]
    assert match[2]["parent_run_id"] == "direction"
    assert match[1]["gtfs"]["execution_workers"] == 2
    assert "feed_gtfs_root" not in match[1]["gtfs"]
    assert "feed_manifest_path" not in match[1]["gtfs"]
    from tools.merge_review.debug import validate_config

    validate_config(match[1])
    assert json.loads((release / "profiles.json").read_text())["04b"]["gtfs"] == {}


def test_empty_raw_normalization_cannot_publish_success(released, monkeypatch):
    from tools.merge_review import raw_bootstrap

    root, release, _ = released
    monkeypatch.setattr(w, "CODE_ROOT", release / "code")
    fake_business(monkeypatch)
    monkeypatch.setattr(
        raw_bootstrap,
        "bootstrap_raw_day",
        lambda *a, **k: {"source_counts": {"avl": 0, "apc": 1}},
    )
    with pytest.raises(ValueError, match="empty source"):
        w.run_pipeline(root, release, root / "new-build", progress=lambda value: None)
    assert not (root / "new-build/build-complete.json").exists()
    assert (
        json.loads((root / "new-build/build-failure.json").read_text())["status"]
        == "FAIL"
    )


def test_empty_published_result_rejected(tmp_path):
    put(tmp_path / "completion.json", {"validation_ok": True})
    put(tmp_path / "run.json", {"status": "complete"})
    put(tmp_path / "output_sha256.json", {})
    with pytest.raises(ValueError, match="Empty or incomplete"):
        w.validate_completed_run(tmp_path)


def test_cli_execs_verified_snapshot_with_isolated_python(released, monkeypatch):
    root, release, _ = released
    put(
        root / "config/workbench-release.json",
        {"release": "releases/test", "sha256": w.sha(release / "release.json")},
    )
    monkeypatch.setattr(
        sys, "argv", ["workbench.py", "verify", "--data-root", str(root)]
    )
    captured = {}

    def execute(binary, argv):
        captured.update(binary=binary, argv=argv)
        raise SystemExit(0)

    monkeypatch.setattr(w.os, "execv", execute)
    with pytest.raises(SystemExit):
        w.main()
    assert captured["argv"][1:3] == ["-I", "-B"]
    assert captured["argv"][3] == str(release / "code/tools/merge_review/workbench.py")
    assert captured["argv"][-4:] == ["--frozen", str(release), "--data-root", str(root)]
