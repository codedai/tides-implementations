"""Completed replay lineage may inherit, but never weaken, registered pins."""

import copy
import json
from pathlib import Path

import pytest

from tools.merge_review import retention
from tools.merge_review.tests.test_retention import (  # noqa: F401
    bundle,
    frozen,
    sha,
    write,
)


def read(path):
    return json.loads(path.read_text())


def finish(run, manifest):
    write(run / "manifest.json", manifest)
    write(run / "config.json", {"scorer": "independent-child-configuration"})
    write(run / "completion.json", {"validation_ok": True})
    write(
        run / "run.json", {"run_id": run.name, "path": str(run), "status": "complete"}
    )
    write(
        run / "output_sha256.json",
        {name: sha(run / name) for name in ("manifest.json", "config.json")},
    )


def replay(source, name):
    child = source.parent / name
    manifest = copy.deepcopy(read(source / "manifest.json"))
    pins = read(source / "output_sha256.json")
    pins.update(
        {
            name: sha(source / name)
            for name in ("output_sha256.json", "completion.json", "run.json")
        }
    )
    manifest.update(
        run_id=child.name,
        parent_run_id=source.name,
        partial_matching_replay=True,
        partial_source_path=str(source),
        partial_source_run_id=source.name,
        partial_source_sha256=pins,
    )
    finish(child, manifest)
    return child


@pytest.fixture
def inherited(request):
    fixture = request.getfixturevalue("frozen")
    # Add the actual completed-run metadata used by production replay pinning.
    write(
        fixture.consumer / "run.json",
        {
            "run_id": fixture.consumer.name,
            "path": str(fixture.consumer),
            "status": "complete",
        },
    )
    _, checkpoint = bundle(fixture)
    return fixture, replay(fixture.consumer, "unregistered-replay"), checkpoint


def test_completed_child_and_grandchild_inherit_without_mutating_history(inherited):
    f, child, checkpoint = inherited
    registry = f.workspace / retention.REGISTRY
    before = {
        str(path): sha(path)
        for path in (registry, f.consumer / "manifest.json", child / "manifest.json")
    }
    assert retention.resolve_direction_parent(child) == checkpoint
    grandchild = replay(child, "second-generation-replay")
    assert retention.resolve_direction_parent(grandchild) == checkpoint
    assert all(sha(Path(path)) == digest for path, digest in before.items())


@pytest.mark.parametrize("name", [*retention.CONSUMER_FILES, "run.json"])
def test_source_metadata_tampering_fails_before_inheritance(inherited, name):
    f, child, _ = inherited
    path = f.consumer / name
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="changed"):
        retention.resolve_direction_parent(child)


@pytest.mark.parametrize(
    "change", ["missing-case", "extra-case", "alter-case-pin", "missing-config"]
)
def test_entire_source_inventory_must_match_even_without_rehashing_cases(
    inherited, change
):
    f, _, _ = inherited
    outputs = read(f.consumer / "output_sha256.json")
    outputs["cases/large-fixture.json.gz"] = "a" * 64
    write(f.consumer / "output_sha256.json", outputs)
    # Pin the modified metadata in a fresh registry fixture binding, without
    # production writes. The test ends before registered resolution on mismatch.
    child = replay(f.consumer, "inventory-replay")
    m = read(child / "manifest.json")
    pins = m["partial_source_sha256"]
    if change == "missing-case":
        del pins["cases/large-fixture.json.gz"]
    elif change == "extra-case":
        pins["cases/foreign.json.gz"] = "b" * 64
    elif change == "alter-case-pin":
        pins["cases/large-fixture.json.gz"] = "b" * 64
    else:
        del pins["config.json"]
    write(child / "manifest.json", m)
    with pytest.raises(ValueError, match="inventory"):
        retention.resolve_direction_parent(child)


@pytest.mark.parametrize(
    "field,value",
    [
        ("direction_parent_run_id", "wrong-parent"),
        ("direction_parent_path", "/missing/different-parent"),
        ("parent_sha256", {"manifest.json": "b" * 64}),
        ("parent_observations_sha256", {"vehicle-a": "b" * 64}),
        ("parent_run_id", "wrong-source"),
        ("partial_source_run_id", "wrong-source"),
        ("run_id", "wrong-child"),
        ("partial_matching_replay", "true"),
    ],
)
def test_changed_lineage_identity_or_parent_pins_fail(inherited, field, value):
    _, child, _ = inherited
    m = read(child / "manifest.json")
    m[field] = value
    write(child / "manifest.json", m)
    with pytest.raises(ValueError):
        retention.resolve_direction_parent(child)


@pytest.mark.parametrize(
    "who,field,value",
    [
        ("child", "completion.json", {"validation_ok": False}),
        ("child", "run.json", {"status": "running"}),
        ("source", "completion.json", {"validation_ok": False}),
        ("source", "run.json", {"status": "running"}),
    ],
)
def test_incomplete_runs_never_gain_retention_lineage(inherited, who, field, value):
    f, child, _ = inherited
    target = child if who == "child" else f.consumer
    write(target / field, value)
    if who == "source":
        child = replay(f.consumer, "incomplete-source-replay")
    with pytest.raises(ValueError, match="completed"):
        retention.resolve_direction_parent(child)


def test_cached_child_manifest_is_not_trusted_after_live_change(inherited):
    _, child, _ = inherited
    cached = read(child / "manifest.json")
    changed = copy.deepcopy(cached)
    changed["partial_source_sha256"]["manifest.json"] = "f" * 64
    write(child / "manifest.json", changed)
    with pytest.raises(ValueError, match="cached manifest"):
        retention.resolve_direction_parent(child, cached)


@pytest.mark.parametrize(
    "kind",
    [
        "source-directory",
        "source-metadata",
        "source-case",
        "traversal",
        "foreign-workspace",
    ],
)
def test_source_path_aliases_and_foreign_workspaces_fail(inherited, kind):
    f, child, _ = inherited
    m = read(child / "manifest.json")
    if kind == "source-directory":
        alias = child.parent / "source-alias"
        alias.symlink_to(f.consumer, target_is_directory=True)
        m.update(
            partial_source_path=str(alias),
            partial_source_run_id=alias.name,
            parent_run_id=alias.name,
        )
    elif kind == "source-metadata":
        original = f.consumer / "run.json"
        moved = f.consumer / "original-run.json"
        original.rename(moved)
        original.symlink_to(moved)
    elif kind == "source-case":
        path = f.consumer / "cases" / "alias.json"
        path.parent.mkdir()
        path.symlink_to(f.consumer / "config.json")
        m["partial_source_sha256"]["cases/alias.json"] = sha(f.consumer / "config.json")
    elif kind == "traversal":
        m["partial_source_path"] = str(f.consumer / ".." / f.consumer.name)
    else:
        other = f.workspace.parent / "foreign" / "runs" / f.consumer.name
        other.mkdir(parents=True)
        m["partial_source_path"] = str(other)
    write(child / "manifest.json", m)
    with pytest.raises(ValueError):
        retention.resolve_direction_parent(child)


def test_self_cycle_and_bounded_recursion_fail(inherited, monkeypatch):
    _, child, _ = inherited
    grandchild = replay(child, "grandchild")
    monkeypatch.setattr(retention, "_MAX_REPLAY_DEPTH", 2)
    with pytest.raises(ValueError, match="depth"):
        retention.resolve_direction_parent(grandchild)
    m = read(child / "manifest.json")
    m.update(
        partial_source_path=str(child),
        partial_source_run_id=child.name,
        parent_run_id=child.name,
    )
    write(child / "manifest.json", m)
    with pytest.raises(ValueError, match="cycle"):
        retention.resolve_direction_parent(child)


def test_bad_existing_original_never_uses_valid_inherited_checkpoint(inherited):
    f, child, _ = inherited
    f.quarantine.rename(f.parent)
    (f.parent / "summary.json").write_bytes(b"corrupt original")
    with pytest.raises(ValueError, match="changed"):
        retention.resolve_direction_parent(child)


def test_existing_invalid_registry_binding_cannot_use_replay_escape(inherited):
    f, child, _ = inherited
    registry = f.workspace / retention.REGISTRY
    value = read(registry)
    value["consumers"][child.name] = {"original_path": str(child), "files_sha256": {}}
    write(registry, value)
    with pytest.raises(ValueError, match="Incomplete retention consumer identity"):
        retention.resolve_direction_parent(child)


def test_registered_source_tamper_cannot_be_reauthorized_by_updated_child_pins(
    inherited,
):
    f, _, _ = inherited
    write(f.consumer / "config.json", {"changed": True})
    outputs = read(f.consumer / "output_sha256.json")
    outputs["config.json"] = sha(f.consumer / "config.json")
    write(f.consumer / "output_sha256.json", outputs)
    child = replay(f.consumer, "repinned-tampered-source")
    with pytest.raises(ValueError, match="changed"):
        retention.resolve_direction_parent(child)


def test_lineage_verifies_case_pin_inventory_not_historical_case_recomputation(request):
    f = request.getfixturevalue("frozen")
    write(
        f.consumer / "run.json",
        {"run_id": f.consumer.name, "path": str(f.consumer), "status": "complete"},
    )
    case = f.consumer / "cases" / "example.json"
    write(case, {"source_result": "original"})
    outputs = read(f.consumer / "output_sha256.json")
    outputs["cases/example.json"] = sha(case)
    write(f.consumer / "output_sha256.json", outputs)
    _, checkpoint = bundle(f)
    child = replay(f.consumer, "metadata-lineage-only")
    write(case, {"changed_result": "requires separate result validation"})
    assert retention.resolve_direction_parent(child) == checkpoint
    # Even without case rehashing, aliasing any declared source path is unsafe.
    moved = case.with_name("original.json")
    case.rename(moved)
    case.symlink_to(moved)
    with pytest.raises(ValueError, match="symlinks"):
        retention.resolve_direction_parent(child)
