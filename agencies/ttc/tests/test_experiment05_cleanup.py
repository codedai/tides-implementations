"""Deletion tests use only newly created tiny fixtures, never real run artifacts."""

import csv
import json

import pytest

from tools.experiment05 import cleanup as c


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)
    return path


def sha(path):
    return c._fingerprint(path)["sha256"]


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path / "agency"
    approved = {
        run: (profile, phase, 1) for run, (profile, phase, _) in c.APPROVED_RUNS.items()
    }
    monkeypatch.setattr(c, "APPROVED_RUNS", approved)
    monkeypatch.setattr(c, "_tag_commit", lambda root: "1" * 40)
    raw = put(root / "database_offline/avl/source.csv", "IRREPLACEABLE AVL")
    apc = put(root / "database_offline/apc/source.csv", "IRREPLACEABLE APC")
    sample = "cases/" + "a" * 24 + ".json.gz"
    snapshot = root / c.SNAPSHOT
    pins = {}
    for relative in (
        "run/" + sample,
        "upstream_sample/" + sample,
        "run/gtfs/canonical.json",
        "run/gtfs/feed.json",
        "run/config.json",
        "run/manifest.json",
    ):
        path = put(snapshot / relative, "IMMUTABLE SNAPSHOT " + relative)
        pins[relative] = {"sha256": sha(path), "bytes": path.stat().st_size}
    put(
        snapshot / "snapshot_manifest.json",
        {
            "schema": "pre-experiment-05-snapshot-v1",
            "snapshot_complete": True,
            "files": pins,
            "sample_relative_path": sample,
            "raw_inputs_preserved_in_place": {
                str(path.relative_to(root)): sha(path) for path in (raw, apc)
            },
        },
    )
    targets = []
    for run, (profile, _, _) in approved.items():
        folder = root / c.WORKSPACE / "runs" / run
        outputs = {}
        names = [sample, *(f"overview/{mode}.json" for mode in c.MODES)]
        if profile != "03d":
            names.extend(f"shadow/{mode}-episodes.json" for mode in c.MODES)
        for name in names:
            path = put(folder / name, "REGENERABLE " + run + name)
            outputs[name] = sha(path)
            targets.append(path)
        for name in (
            "config.json",
            "summary.json",
            "gtfs/canonical.json",
            "observations/raw.parquet",
            "catalog/candidates.json",
            "shadow/global-report.json",
            "shadow/global-vehicle-sequence.json",
        ):
            path = put(folder / name, "PRESERVED " + name)
            outputs[name] = sha(path)
        put(folder / "manifest.json", {"run_id": run})
        put(folder / "completion.json", {"validation_ok": True})
        put(folder / "output_sha256.json", outputs)
    review = put(root / c.WORKSPACE / "reviews/notes.sqlite", "HUMAN REVIEW")
    code = put(root / "tools/production.py", "frozen code")
    return dict(
        root=root,
        raw=raw,
        apc=apc,
        snapshot=snapshot,
        sample=sample,
        targets=targets,
        review=review,
        code=code,
    )


def plan(f, **kwargs):
    path = c.plan_cleanup(f["root"], **kwargs)
    return path, sha(path), c._read(path)


def execute(path, digest, phases=("historical", "current_04c")):
    return c.execute_cleanup(path, expected_manifest_sha256=digest, phases=phases)


def test_dry_run_writes_exact_csv_manifest_and_never_deletes(fixture):
    f = fixture
    path, _, value = plan(f)
    assert value["state"] == "DRY_RUN" and value["deleted_paths"] == []
    assert len(value["files"]) == len(f["targets"]) == 32
    assert all(p.exists() for p in f["targets"])
    assert value["before_bytes"] == sum(p.stat().st_size for p in f["targets"])
    with (path.parent / "cleanup_dry_run.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 32
    assert {row["relative_path"] for row in rows} == set(value["files"])
    assert value["snapshot"]["git_commit"] == "1" * 40
    for preserved in (f["raw"], f["apc"], f["review"], f["code"]):
        assert str(preserved.relative_to(f["root"])) in value["preserved_sha256"]
    with pytest.raises(ValueError, match="new directory"):
        c.plan_cleanup(f["root"])


def test_execute_only_planned_files_and_leaves_historical_metadata(fixture):
    f = fixture
    path, digest, value = plan(f)
    result = execute(path, digest)
    assert result["state"] == "COMPLETE"
    assert result["deleted_bytes"] == value["before_bytes"]
    assert set(result["deleted_paths"]) == set(value["files"])
    assert all(not p.exists() for p in f["targets"])
    for relative, fingerprint in value["preserved_sha256"].items():
        assert sha(f["root"] / relative) == fingerprint["sha256"]
    assert c._read(path)["deleted_paths"] == []  # Never mutate the approved plan.
    assert result["historical_full_ui_available"] is False
    assert result["historical_output_pins_rewritten"] is False
    assert result["preserved_files_verified_after"] is True


def test_historical_phase_holds_04c_and_new_plan_can_target_remaining_phase(fixture):
    f = fixture
    path, digest, value = plan(f)
    result = execute(path, digest, ("historical",))
    assert len(result["deleted_paths"]) == 23
    held = [
        f["root"] / relative
        for relative, row in value["files"].items()
        if row["phase"] == "current_04c"
    ]
    assert len(held) == 9 and all(p.exists() for p in held)
    second, pin, value = plan(
        f, report_dir=f["root"] / c.REPORT_ROOT / "second-phase", phases=["current_04c"]
    )
    assert value["file_count"] == 9
    assert len(execute(second, pin, ["current_04c"])["deleted_paths"]) == 9


@pytest.mark.parametrize(
    "kind",
    [
        "target-bytes",
        "target-touch",
        "raw",
        "review",
        "code",
        "source-metadata",
        "snapshot",
        "foreign-snapshot",
        "foreign-run",
        "foreign-protected",
    ],
)
def test_tamper_or_foreign_files_stop_before_any_deletion(fixture, kind):
    f = fixture
    path, digest, _ = plan(f)
    if kind == "target-bytes":
        f["targets"][0].write_text("changed")
    elif kind == "target-touch":
        f["targets"][0].touch()
    elif kind in {"raw", "review", "code"}:
        f[kind].write_text("changed")
    elif kind == "source-metadata":
        put(f["targets"][0].parent.parent / "output_sha256.json", {})
    elif kind == "snapshot":
        put(f["snapshot"] / ("run/" + f["sample"]), "changed")
    elif kind == "foreign-snapshot":
        put(f["snapshot"] / "foreign.txt", "untrusted")
    elif kind == "foreign-run":
        put(f["targets"][0].parent / "human-notes.txt", "do not delete")
    else:
        put(f["review"].parent / "new-review.txt", "new human review")
    with pytest.raises(ValueError):
        execute(path, digest)
    assert all(p.exists() for p in f["targets"])


@pytest.mark.parametrize(
    "kind", ["target", "target-ancestor", "raw", "snapshot", "report"]
)
def test_symlink_aliases_never_authorize_deletion(fixture, kind):
    f = fixture
    path, digest, _ = plan(f)
    target = {
        "target": f["targets"][0],
        "target-ancestor": f["targets"][0].parent,
        "raw": f["raw"],
        "snapshot": f["snapshot"],
        "report": path.parent,
    }[kind]
    moved = target.with_name(target.name + "-original")
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(ValueError, match="Symlink"):
        execute(path, digest)
    assert all(p.exists() for p in f["targets"])


@pytest.mark.parametrize(
    "relative",
    [
        "database_offline/avl/source.csv",
        "../outside.txt",
        "/tmp/outside",
        f"{c.WORKSPACE}/runs/foreign/cases/{'a' * 24}.json.gz",
        f"{c.WORKSPACE}/runs/{next(iter(c.APPROVED_RUNS))}/manifest.json",
    ],
)
def test_forged_plan_cannot_widen_allowlist_even_with_new_hash(fixture, relative):
    f = fixture
    path, _, value = plan(f)
    first = next(iter(value["files"].values()))
    value["files"][relative] = dict(first)
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        execute(path, sha(path))
    assert all(p.exists() for p in f["targets"])


def test_wrong_expected_hash_csv_mutation_missing_phase_and_tag_changes(
    fixture, monkeypatch
):
    f = fixture
    path, digest, _ = plan(f)
    with pytest.raises(ValueError, match="manifest changed"):
        execute(path, "0" * 64)
    with pytest.raises(ValueError, match="phases"):
        execute(path, digest, ())
    monkeypatch.setattr(c, "_tag_commit", lambda root: "2" * 40)
    with pytest.raises(ValueError, match="Git tag"):
        execute(path, digest)
    monkeypatch.setattr(c, "_tag_commit", lambda root: "1" * 40)
    (path.parent / "cleanup_dry_run.csv").write_text("altered approval view")
    with pytest.raises(ValueError, match="CSV"):
        execute(path, digest)
    assert all(p.exists() for p in f["targets"])


def test_interrupted_execution_records_only_actual_deleted_paths(fixture, monkeypatch):
    f = fixture
    path, digest, _ = plan(f)
    original = c._unlink_verified
    count = 0

    def interrupted(target, expected):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("injected I/O failure")
        original(target, expected)

    monkeypatch.setattr(c, "_unlink_verified", interrupted)
    with pytest.raises(ValueError, match="stopped after 1 files"):
        execute(path, digest)
    report = c._read(path.parent / "retirement_manifest.json")
    assert report["state"] == "PARTIAL_FAILURE"
    assert len(report["deleted_paths"]) == 1
    assert sum(p.exists() for p in f["targets"]) == 31
    assert f["raw"].read_text() == "IRREPLACEABLE AVL"


def test_new_plan_refuses_missing_outputs_and_invalid_parent_traversal(fixture):
    f = fixture
    f["targets"][0].unlink()
    with pytest.raises(ValueError, match="inventory"):
        plan(f)
    with pytest.raises(ValueError, match="traversal"):
        c.plan_cleanup(f["root"] / ".." / f["root"].name)


def test_missing_real_git_tag_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Git tag"):
        c._tag_commit(tmp_path)
