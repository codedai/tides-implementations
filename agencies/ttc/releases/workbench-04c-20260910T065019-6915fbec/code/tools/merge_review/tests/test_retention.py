"""Detached parent locations preserve original pins, identities and review keys."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.merge_review.data import ReviewData
from tools.merge_review.gtfs_matching.adapter import GTFSReviewData
from tools.merge_review.retention import (
    CONSUMER_FILES,
    build_retention_bundle,
    resolve_direction_parent,
)
from tools.merge_review.server import run_fingerprint


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def frozen(tmp_path):
    workspace = tmp_path / "debug"
    parent = workspace / "runs" / "run-direction-parent"
    consumer = workspace / "runs" / "run-kept-04b"
    fixture = workspace / "fixtures" / "fixture-kept"
    inventory = tmp_path / "raw_audit" / "inventory.json"
    write(inventory, {"sources": {"avl": {"sha256": "a" * 64}}})
    write(fixture / "avl.parquet", ["unchanged original AVL"])
    write(fixture / "apc.parquet", ["unchanged original APC"])
    write(
        fixture / "manifest.json",
        {
            "identity": "original-fixture-identity",
            "source_audit": str(inventory.parent),
            "input_sha256": {"inventory.json": sha(inventory)},
            "fixture_sha256": {
                name: sha(fixture / name) for name in ("avl.parquet", "apc.parquet")
            },
        },
    )
    parent_manifest = {
        "run_id": parent.name,
        "audit": str(fixture),
        "baseline": str(parent),
        "fixture_identity": "original-fixture-identity",
        "debug_date": "2023-11-06",
        "methods": ["direction_candidates"],
    }
    # Import the current known method rather than guessing a future enum alias.
    from tools.merge_review.data import DIRECTION_METHOD

    parent_manifest["methods"] = [DIRECTION_METHOD]
    write(parent / "manifest.json", parent_manifest)
    for name in ("config.json", "summary.json"):
        write(parent / name, {})
    write(parent / "completion.json", {"validation_ok": True})
    write(
        parent / "selection.json",
        [
            {"vehicle_case": "vehicle-a", "raw_vehicle": "9042"},
            {"vehicle_case": "vehicle-b", "raw_vehicle": "9999"},
        ],
    )
    for name in (
        "direction/vehicle-a.json",
        "deduplication/vehicle-a.parquet",
        "deduplication/raw_avl_fields.parquet",
        "observations/vehicle-a.parquet",
        "observations/vehicle-b.parquet",
    ):
        write(parent / name, [name])
    parent_files = (
        "manifest.json",
        "config.json",
        "summary.json",
        "completion.json",
        "selection.json",
        "direction/vehicle-a.json",
        "deduplication/vehicle-a.parquet",
        "deduplication/raw_avl_fields.parquet",
    )
    manifest = {
        "run_id": consumer.name,
        "direction_parent_run_id": parent.name,
        "direction_parent_path": str(parent),
        "parent_sha256": {name: sha(parent / name) for name in parent_files},
        "parent_observations_sha256": {
            "vehicle-a": sha(parent / "observations/vehicle-a.parquet")
        },
    }
    write(consumer / "manifest.json", manifest)
    write(consumer / "config.json", {"gtfs": {"score_delay_prior_weight": 1.0}})
    write(consumer / "completion.json", {"validation_ok": True})
    write(
        consumer / "output_sha256.json",
        {name: sha(consumer / name) for name in ("manifest.json", "config.json")},
    )
    return SimpleNamespace(
        workspace=workspace,
        parent=parent,
        consumer=consumer,
        manifest=manifest,
        fixture=fixture,
        inventory=inventory,
        quarantine=tmp_path / "original-parent-quarantine",
    )


def bundle(frozen):
    result = build_retention_bundle(
        frozen.workspace, [frozen.consumer.name], frozen.parent.name
    )
    frozen.parent.rename(frozen.quarantine)
    return result, Path(result["checkpoint_path"])


def test_build_preserves_every_source_byte_fingerprint_and_extra_observation(frozen):
    original = {
        str(path): sha(path)
        for directory in (frozen.parent, frozen.consumer, frozen.fixture)
        for path in directory.rglob("*")
        if path.is_file()
    }
    fingerprint = run_fingerprint(frozen.consumer)
    result = build_retention_bundle(frozen.workspace, [frozen.consumer], frozen.parent)
    checkpoint = Path(result["checkpoint_path"])
    assert checkpoint.name == frozen.parent.name
    assert result["copied_file_count"] == 10
    assert result["live_external_file_count"] == 4
    assert sha(checkpoint / "observations/vehicle-b.parquet") == sha(
        frozen.parent / "observations/vehicle-b.parquet"
    )
    assert all(sha(Path(path)) == digest for path, digest in original.items())
    assert run_fingerprint(frozen.consumer) == fingerprint
    assert result["registry_sha256"] == sha(Path(result["registry_path"]))
    registry = json.loads(Path(result["registry_path"]).read_text())
    assert registry["historical_manifests_modified"] is False
    assert registry["replay_source_attestation_included"] is False
    with pytest.raises(ValueError, match="already exists"):
        build_retention_bundle(frozen.workspace, [frozen.consumer], frozen.parent)


def test_missing_parent_uses_verified_copy_without_rewriting_manifest(frozen):
    manifest_bytes = (frozen.consumer / "manifest.json").read_bytes()
    result, checkpoint = bundle(frozen)
    assert resolve_direction_parent(frozen.consumer) == checkpoint
    assert (frozen.consumer / "manifest.json").read_bytes() == manifest_bytes
    parent = ReviewData(checkpoint)
    assert parent.run.name == frozen.parent.name
    assert parent.audit == frozen.fixture
    assert parent.metadata()["vehicles"][0]["label"] == "9042"
    assert Path(result["registry_path"]).is_file()


@pytest.mark.parametrize(
    "relative",
    [
        "manifest.json",
        "direction/vehicle-a.json",
        "deduplication/raw_avl_fields.parquet",
        "observations/vehicle-a.parquet",
        "observations/vehicle-b.parquet",
    ],
)
def test_any_retained_file_tampering_is_rejected(frozen, relative):
    _, checkpoint = bundle(frozen)
    (checkpoint / relative).write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        resolve_direction_parent(frozen.consumer)


@pytest.mark.parametrize("name", CONSUMER_FILES)
def test_bound_consumer_identity_cannot_be_changed(frozen, name):
    bundle(frozen)
    (frozen.consumer / name).write_bytes((frozen.consumer / name).read_bytes() + b" ")
    with pytest.raises(ValueError, match="changed"):
        resolve_direction_parent(frozen.consumer, frozen.manifest)


@pytest.mark.parametrize("kind", ["file", "directory", "dangling_original"])
def test_file_and_ancestor_symlinks_never_authorize_a_fallback(frozen, kind):
    _, checkpoint = bundle(frozen)
    if kind == "dangling_original":
        frozen.parent.symlink_to(frozen.workspace / "absent", target_is_directory=True)
    elif kind == "file":
        path = checkpoint / "observations/vehicle-a.parquet"
        path.unlink()
        path.symlink_to(frozen.quarantine / "observations/vehicle-a.parquet")
    else:
        path = checkpoint / "observations"
        path.rename(checkpoint / "original-observations")
        path.symlink_to(checkpoint / "original-observations", target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        resolve_direction_parent(frozen.consumer)


def test_unknown_consumer_and_different_parent_pins_cannot_claim_checkpoint(frozen):
    bundle(frozen)
    other = frozen.workspace / "runs" / "unregistered-consumer"
    other.mkdir()
    write(other / "manifest.json", frozen.manifest)
    with pytest.raises(ValueError, match="Unknown retention consumer"):
        resolve_direction_parent(other)
    modified = copy.deepcopy(frozen.manifest)
    modified["parent_sha256"]["summary.json"] = "f" * 64
    with pytest.raises(ValueError, match="original parent pins"):
        resolve_direction_parent(frozen.consumer, modified)


def test_existing_bad_original_does_not_fall_back(frozen):
    build_retention_bundle(frozen.workspace, [frozen.consumer], frozen.parent)
    (frozen.parent / "summary.json").write_bytes(b"corrupt original")
    with pytest.raises(ValueError, match="changed"):
        resolve_direction_parent(frozen.consumer)


@pytest.mark.parametrize("target", ["fixture", "inventory"])
def test_live_input_changes_cannot_hide_behind_adapter_or_vehicle_cache(frozen, target):
    _, checkpoint = bundle(frozen)
    reader = GTFSReviewData.__new__(GTFSReviewData)
    reader.run = frozen.consumer
    reader.manifest = frozen.manifest
    reader._hash_cache = {}
    reader._verify_parent()
    parent = ReviewData(checkpoint, verify_dependencies=reader._verify_parent)
    parent._cache["vehicle-a"] = {"cached": "raw rows"}
    assert parent._vehicle("vehicle-a") == {"cached": "raw rows"}
    path = frozen.fixture / "avl.parquet" if target == "fixture" else frozen.inventory
    path.write_bytes(b"changed after cache warm-up")
    with pytest.raises(ValueError, match="changed"):
        reader._verify_parent()
    with pytest.raises(ValueError, match="changed"):
        parent._vehicle("vehicle-a")


def test_warm_adapter_parent_is_rebound_after_verified_relocation(frozen):
    build_retention_bundle(frozen.workspace, [frozen.consumer], frozen.parent)
    reader = GTFSReviewData.__new__(GTFSReviewData)
    reader.run = frozen.consumer
    reader.manifest = frozen.manifest
    reader._hash_cache = {}
    reader.parent = ReviewData(frozen.parent, verify_dependencies=reader._verify_parent)
    original_reader = reader.parent
    reader._verify_parent()
    assert reader.parent is original_reader
    frozen.parent.rename(frozen.quarantine)
    reader._verify_parent()
    assert reader.parent is not original_reader
    assert reader.parent.run.name == original_reader.run.name
    assert reader.parent.run != original_reader.run


@pytest.mark.parametrize(
    "relative", ["../outside", "/outside", "checkpoints/../outside"]
)
def test_registry_path_escape_is_rejected(frozen, relative):
    result, _ = bundle(frozen)
    registry_path = Path(result["registry_path"])
    registry = json.loads(registry_path.read_text())
    registry["checkpoint_relative_path"] = relative
    write(registry_path, registry)
    with pytest.raises(ValueError, match="Unsafe retention relative path"):
        resolve_direction_parent(frozen.consumer)


def test_missing_original_without_registry_fails_closed(frozen):
    frozen.parent.rename(frozen.quarantine)
    with pytest.raises(ValueError, match="Cannot read retention dependency"):
        resolve_direction_parent(frozen.consumer)


def test_build_refuses_unverified_consumers_and_leaves_no_registry(frozen):
    (frozen.consumer / "config.json").write_bytes(b"changed config")
    with pytest.raises(ValueError, match="changed"):
        build_retention_bundle(frozen.workspace, [frozen.consumer], frozen.parent)
    assert not (frozen.workspace / "retention/registry.json").exists()


def test_unpinned_parent_vehicle_must_not_disappear_from_checkpoint_inventory(frozen):
    result, _ = bundle(frozen)
    registry_path = Path(result["registry_path"])
    registry = json.loads(registry_path.read_text())
    del registry["dependency_files_sha256"]["observations/vehicle-b.parquet"]
    write(registry_path, registry)
    with pytest.raises(ValueError, match="every parent vehicle"):
        resolve_direction_parent(frozen.consumer)


def test_fixture_pin_inventory_cannot_be_silently_weakened(frozen):
    result, _ = bundle(frozen)
    registry_path = Path(result["registry_path"])
    registry = json.loads(registry_path.read_text())
    del registry["external_files_sha256"][str(frozen.inventory)]
    write(registry_path, registry)
    with pytest.raises(ValueError, match="inventory is incomplete"):
        resolve_direction_parent(frozen.consumer)
