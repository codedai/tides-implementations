"""Verified physical relocation of frozen review dependencies.

Historical run manifests and result inventories remain byte-for-byte unchanged.
The registry is an explicit location overlay, not a replacement for their pins
and not an assertion that deleted historical replay inputs still exist.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

VERSION = "merge-review-retention-v1"
REGISTRY = Path("retention/registry.json")
CONSUMER_FILES = (
    "manifest.json",
    "config.json",
    "completion.json",
    "output_sha256.json",
)
_PARENT_FIELDS = (
    "direction_parent_path",
    "direction_parent_run_id",
    "parent_sha256",
    "parent_observations_sha256",
)
_MAX_REPLAY_DEPTH = 16
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _absolute(path):
    path = Path(path)
    if ".." in path.parts:
        raise ValueError("Retention paths must not contain parent traversal")
    return path.absolute()


def _no_symlinks(path):
    """Check lexical ancestors before resolving; reject dangling links as well."""
    path = _absolute(path)
    for candidate in (*reversed(path.parents), path):
        if candidate.is_symlink():
            raise ValueError(f"Retention dependency must not use symlinks: {candidate}")
    return path


def _relative(root, relative):
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or PurePosixPath(relative).is_absolute()
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise ValueError("Unsafe retention relative path")
    path = _no_symlinks(_absolute(root) / relative)
    if not path.is_relative_to(_absolute(root)):
        raise ValueError("Retention dependency escapes its checkpoint")
    return path


def _sha(path):
    path = _no_symlinks(path)
    if not path.is_file():
        raise ValueError(f"Missing retention dependency: {path}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate retention JSON key")
        result[key] = value
    return result


def _read(path):
    path = _no_symlinks(path)
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=_pairs)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read retention dependency: {path}") from error


def _pins(value):
    if not isinstance(value, dict) or not value:
        raise ValueError("Retention requires a nonempty SHA-256 inventory")
    for relative, digest in value.items():
        _relative(Path.cwd(), relative)
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError("Invalid retention SHA-256 digest")
    return value


def _verify(path, digest, verifier=None):
    path = _no_symlinks(path)
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("Invalid retention SHA-256 digest")
    if not path.is_file():
        raise ValueError(f"Missing retention dependency: {path}")
    if verifier is not None:
        verifier(path, digest)
    elif _sha(path) != digest:
        raise ValueError(f"Pinned retention dependency changed: {path}")


def _parent_pins(manifest):
    pins = dict(_pins(manifest.get("parent_sha256")))
    observations = _pins(manifest.get("parent_observations_sha256"))
    for key, digest in observations.items():
        if "/" in key or "\\" in key:
            raise ValueError("Invalid parent observation identity")
        relative = f"observations/{key}.parquet"
        if relative in pins and pins[relative] != digest:
            raise ValueError("Conflicting parent observation pin")
        pins[relative] = digest
    return pins


def _observation_files(parent):
    selection = _read(parent / "selection.json")
    if not isinstance(selection, list) or not selection:
        raise ValueError("Retained parent selection must be a nonempty list")
    identities = []
    for row in selection:
        identity = row.get("vehicle_case") if isinstance(row, dict) else None
        if not isinstance(identity, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", identity
        ):
            raise ValueError("Invalid retained parent vehicle identity")
        identities.append(identity)
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate retained parent vehicle identity")
    return {f"observations/{identity}.parquet" for identity in identities}


def _consumer_binding(run, manifest):
    return {
        "original_path": str(_absolute(run)),
        "files_sha256": {name: _sha(run / name) for name in CONSUMER_FILES},
        "direction_parent_path": manifest["direction_parent_path"],
        "direction_parent_run_id": manifest["direction_parent_run_id"],
        "parent_sha256": manifest["parent_sha256"],
        "parent_observations_sha256": manifest["parent_observations_sha256"],
    }


def _external_files(parent_manifest):
    """Required live fixture inputs; relocation of these is intentionally absent."""
    fixture = _no_symlinks(parent_manifest["audit"])
    fixture_manifest = _read(fixture / "manifest.json")
    if fixture_manifest.get("identity") != parent_manifest.get("fixture_identity"):
        raise ValueError("Retained day fixture identity disagrees with parent")
    fixture_pins = _pins(fixture_manifest.get("fixture_sha256"))
    result = {str(fixture / "manifest.json"): _sha(fixture / "manifest.json")}
    for name in ("avl.parquet", "apc.parquet"):
        digest = fixture_pins.get(name)
        _verify(fixture / name, digest)
        result[str(fixture / name)] = digest
    source = Path(fixture_manifest["source_audit"])
    if not source.is_absolute():
        source = fixture / source
    inventory = _no_symlinks(source / "inventory.json")
    digest = fixture_manifest.get("input_sha256", {}).get("inventory.json")
    _verify(inventory, digest)
    result[str(inventory)] = digest
    return result


def resolve_direction_parent(consumer_run, manifest=None, *, verify_file=None):
    """Resolve and verify original pins, using a registered copy only if absent.

    ``verify_file(path, digest)`` may cache hashes by fresh file stat signatures.
    Path/ancestor safety is checked on every call, independently of that cache.
    Existing bad originals never fall back to a previously valid checkpoint.
    Completed append-only matching replays may inherit an existing registered
    source's relocation, after validating their exact immutable parent pins and
    source metadata inventory. This verifies dependency lineage, not historical
    recomputation of the source's case scores.
    """
    return _resolve_direction_parent(consumer_run, manifest, verify_file, ())


def _completed_replay_run(run, manifest):
    completion = _read(run / "completion.json")
    record = _read(run / "run.json")
    if (
        not isinstance(completion, dict)
        or completion.get("validation_ok") is not True
        or not isinstance(record, dict)
        or record.get("status") != "complete"
        or record.get("run_id") != run.name
        or manifest.get("run_id") != run.name
        or record.get("path") != str(run)
        or (run / "failure.json").exists()
        or (run / "failure.json").is_symlink()
    ):
        raise ValueError("Retention replay lineage requires completed run identities")


def _inherited_replay_parent(run, manifest, verify_file, seen):
    if manifest.get("partial_matching_replay") is not True:
        raise ValueError("Unknown retention consumer")
    if _read(run / "manifest.json") != manifest:
        raise ValueError(
            "Retention replay cached manifest disagrees with live manifest"
        )
    _completed_replay_run(run, manifest)
    value = manifest.get("partial_source_path")
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("Retention replay source requires an absolute sibling path")
    source = _no_symlinks(value)
    if (
        source.parent != run.parent
        or not source.is_dir()
        or source.name != manifest.get("partial_source_run_id")
        or manifest.get("parent_run_id") != source.name
    ):
        raise ValueError("Retention replay source must be an identified sibling run")
    if source in seen:
        raise ValueError("Retention replay dependency cycle")
    pins = _pins(manifest.get("partial_source_sha256"))
    required = {*CONSUMER_FILES, "run.json"}
    if not required <= pins.keys():
        raise ValueError("Retention replay source metadata inventory is incomplete")
    for name in required:
        _verify(source / name, pins[name], verify_file)
    outputs = _pins(_read(source / "output_sha256.json"))
    if not {"manifest.json", "config.json"} <= outputs.keys():
        raise ValueError("Retention replay source output inventory lacks metadata")
    inventory = dict(outputs)
    for name in ("output_sha256.json", "completion.json", "run.json"):
        if name in outputs and outputs[name] != pins[name]:
            raise ValueError(
                "Retention replay source metadata has conflicting output pins"
            )
        inventory[name] = pins[name]
    if inventory != pins:
        raise ValueError("Retention replay source output inventory disagrees with pins")
    # Validate every declared location without claiming to rehash large case
    # payloads. UI result readers separately verify the child artifacts they use.
    for relative in pins:
        _relative(source, relative)
    source_manifest = _read(source / "manifest.json")
    if not isinstance(source_manifest, dict):
        raise ValueError("Invalid retention replay source manifest")
    _completed_replay_run(source, source_manifest)
    if any(manifest.get(key) != source_manifest.get(key) for key in _PARENT_FIELDS):
        raise ValueError("Retention replay changed its inherited direction parent pins")
    return _resolve_direction_parent(source, source_manifest, verify_file, seen)


def _resolve_direction_parent(consumer_run, manifest, verify_file, seen):
    run = _no_symlinks(consumer_run)
    if run in seen:
        raise ValueError("Retention replay dependency cycle")
    if len(seen) >= _MAX_REPLAY_DEPTH:
        raise ValueError("Retention replay dependency depth exceeded")
    seen = (*seen, run)
    manifest = _read(run / "manifest.json") if manifest is None else manifest
    expected = _parent_pins(manifest)
    parent = _no_symlinks(manifest["direction_parent_path"])
    if parent.name != manifest.get("direction_parent_run_id"):
        raise ValueError("Direction parent identity disagrees with original manifest")
    if parent.exists():
        if not parent.is_dir():
            raise ValueError("Existing direction parent is not a directory")
        for relative, digest in expected.items():
            _verify(_relative(parent, relative), digest, verify_file)
        return parent

    if run.parent.name != "runs":
        raise ValueError("Missing parent requires a registered workspace consumer")
    workspace = run.parent.parent
    registry_path = _relative(workspace, REGISTRY.as_posix())
    registry = _read(registry_path)
    if registry.get("version") != VERSION:
        raise ValueError("Unsupported retention registry version")
    consumers = registry.get("consumers")
    if not isinstance(consumers, dict):
        raise ValueError("Invalid retention consumer registry")
    if run.name not in consumers:
        return _inherited_replay_parent(run, manifest, verify_file, seen)
    binding = consumers.get(run.name)
    if not isinstance(binding, dict) or binding.get("original_path") != str(run):
        raise ValueError("Unknown retention consumer")
    if set(binding.get("files_sha256", {})) != set(CONSUMER_FILES):
        raise ValueError("Incomplete retention consumer identity")
    for name, digest in binding["files_sha256"].items():
        _verify(run / name, digest, verify_file)
    # A caller's cached manifest must still describe the bound, live manifest.
    for key in _PARENT_FIELDS:
        if binding.get(key) != manifest.get(key):
            raise ValueError("Retention binding disagrees with original parent pins")
    if (
        registry.get("dependency_original_path") != str(parent)
        or registry.get("dependency_run_id") != parent.name
    ):
        raise ValueError("Retention checkpoint belongs to another direction parent")
    checkpoint = _relative(registry_path.parent, registry["checkpoint_relative_path"])
    if checkpoint.name != parent.name or not checkpoint.is_dir():
        raise ValueError("Invalid retained direction checkpoint")
    pins = _pins(registry.get("dependency_files_sha256"))
    if any(pins.get(relative) != digest for relative, digest in expected.items()):
        raise ValueError("Checkpoint inventory does not preserve original parent pins")
    for relative, digest in pins.items():
        _verify(_relative(checkpoint, relative), digest, verify_file)
    if {key for key in pins if key.startswith("observations/")} != _observation_files(
        checkpoint
    ):
        raise ValueError("Checkpoint must retain observations for every parent vehicle")
    parent_manifest = _read(checkpoint / "manifest.json")
    fixture = _no_symlinks(parent_manifest["audit"])
    external = registry.get("external_files_sha256")
    if not isinstance(external, dict) or not external:
        raise ValueError("Retention registry is missing live fixture pins")
    # Require the original fixture manifest before interpreting its dependencies.
    fixture_manifest_path = fixture / "manifest.json"
    _verify(
        fixture_manifest_path, external.get(str(fixture_manifest_path)), verify_file
    )
    fixture_manifest = _read(fixture_manifest_path)
    source = Path(fixture_manifest["source_audit"])
    if not source.is_absolute():
        source = fixture / source
    required_external = {
        str(fixture_manifest_path),
        str(fixture / "avl.parquet"),
        str(fixture / "apc.parquet"),
        str(_absolute(source / "inventory.json")),
    }
    if set(external) != required_external:
        raise ValueError("Retention live fixture inventory is incomplete or unexpected")
    for path, digest in external.items():
        _verify(path, digest, verify_file)
    if fixture_manifest.get("identity") != parent_manifest.get("fixture_identity"):
        raise ValueError("Retained day fixture identity changed")
    for name in ("avl.parquet", "apc.parquet"):
        if external[str(fixture / name)] != fixture_manifest["fixture_sha256"].get(
            name
        ):
            raise ValueError("Day fixture pin disagrees with its original manifest")
    if external[str(_absolute(source / "inventory.json"))] != fixture_manifest.get(
        "input_sha256", {}
    ).get("inventory.json"):
        raise ValueError("Raw source inventory pin disagrees with fixture provenance")
    return checkpoint


def build_retention_bundle(workspace_root, consumer_runs, dependency_run):
    """Publish a verified shared copy; never edit or remove historical inputs.

    Copies the consumer-pinned parent files and *all* parent observation tables.
    Fixture parquet and raw inventory remain in their existing locations, pinned
    as live dependencies. Publishing an existing registry is intentionally refused.
    The returned registry SHA-256 should also be recorded outside this registry.
    """
    workspace = _no_symlinks(workspace_root)
    if not workspace.is_dir():
        raise ValueError("Retention workspace must already exist")

    def local_run(value):
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = workspace / "runs" / candidate
        candidate = _no_symlinks(candidate)
        if candidate.parent != workspace / "runs" or not candidate.is_dir():
            raise ValueError("Retention runs must be direct workspace/runs children")
        return candidate

    dependency = local_run(dependency_run)
    consumers = [local_run(run) for run in consumer_runs]
    if (
        not consumers
        or len(set(consumers)) != len(consumers)
        or dependency in consumers
    ):
        raise ValueError("Retention needs distinct consumers separate from the parent")
    bindings, pins = {}, {}
    for run in consumers:
        manifest = _read(run / "manifest.json")
        if (
            _absolute(manifest["direction_parent_path"]) != dependency
            or manifest.get("direction_parent_run_id") != dependency.name
        ):
            raise ValueError("Consumer does not use the specified direction parent")
        for relative, digest in _parent_pins(manifest).items():
            if relative in pins and pins[relative] != digest:
                raise ValueError("Consumers disagree on their original parent pins")
            _verify(_relative(dependency, relative), digest)
            pins[relative] = digest
        outputs = _pins(_read(run / "output_sha256.json"))
        for name in ("manifest.json", "config.json"):
            _verify(run / name, outputs.get(name))
        completion = _read(run / "completion.json")
        if completion.get("validation_ok") is not True:
            raise ValueError("Only completed validated consumers may be retained")
        bindings[run.name] = _consumer_binding(run, manifest)
    observation_dir = _no_symlinks(dependency / "observations")
    if not observation_dir.is_dir():
        raise ValueError("Direction parent observations are required")
    for path in sorted(observation_dir.iterdir()):
        _no_symlinks(path)
        if path.suffix != ".parquet" or not path.is_file():
            raise ValueError("Unexpected direction parent observation entry")
        relative = path.relative_to(dependency).as_posix()
        digest = _sha(path)
        if relative in pins and pins[relative] != digest:
            raise ValueError("Original parent observation changed during retention")
        pins[relative] = digest
    if {key for key in pins if key.startswith("observations/")} != _observation_files(
        dependency
    ):
        raise ValueError(
            "Direction parent must contain every selected observation table"
        )
    if "deduplication/raw_avl_fields.parquet" not in pins:
        raise ValueError("Retention must preserve exact AVL raw-field evidence")
    parent_manifest = _read(dependency / "manifest.json")
    external = _external_files(parent_manifest)
    content_id = hashlib.sha256(
        json.dumps({"files": pins, "external": external}, sort_keys=True).encode()
    ).hexdigest()[:24]
    retention = _relative(workspace, "retention")
    registry_path = _relative(workspace, REGISTRY.as_posix())
    if registry_path.exists():
        raise ValueError(
            "Retention registry already exists; do not overwrite provenance"
        )
    retention.mkdir(mode=0o700, exist_ok=True)
    checkpoints = _relative(retention, "checkpoints")
    checkpoints.mkdir(mode=0o700, exist_ok=True)
    target = _relative(checkpoints, content_id)
    if target.exists():
        raise ValueError("Retention checkpoint already exists; inspect before retrying")
    staging = Path(tempfile.mkdtemp(prefix=".building-", dir=checkpoints))
    checkpoint = staging / "runs" / dependency.name
    checkpoint.mkdir(parents=True, mode=0o700)
    for relative, digest in sorted(pins.items()):
        source, destination = (
            _relative(dependency, relative),
            _relative(checkpoint, relative),
        )
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _verify(source, digest)
        shutil.copyfile(source, destination)
        _verify(destination, digest)
    # Recheck after copying: no stale attestation if source changed mid-build.
    for relative, digest in pins.items():
        _verify(_relative(dependency, relative), digest)
    for path, digest in external.items():
        _verify(path, digest)
    for run in consumers:
        if _consumer_binding(run, _read(run / "manifest.json")) != bindings[run.name]:
            raise ValueError("Consumer changed during retention build")
    staging.rename(target)
    checkpoint = target / "runs" / dependency.name
    registry = {
        "version": VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "dependency_run_id": dependency.name,
        "dependency_original_path": str(dependency),
        "checkpoint_relative_path": checkpoint.relative_to(retention).as_posix(),
        "dependency_files_sha256": dict(sorted(pins.items())),
        "external_files_sha256": external,
        "consumers": bindings,
        "historical_manifests_modified": False,
        "historical_results_recomputed": False,
        "replay_source_attestation_included": False,
        "live_fixture_location_preserved": True,
        "builder_sha256": _sha(Path(__file__)),
    }
    encoded = (json.dumps(registry, indent=2, ensure_ascii=False) + "\n").encode()
    # Publish completely or not at all; link is exclusive and cannot overwrite an
    # independently published registry. No historical file is linked or mutated.
    with tempfile.NamedTemporaryFile(
        prefix=".registry-", dir=retention, delete=False
    ) as out:
        temporary = Path(out.name)
        out.write(encoded)
        out.flush()
        os.fsync(out.fileno())
    try:
        os.link(temporary, registry_path)
    finally:
        temporary.unlink()
    return {
        "registry_path": str(registry_path),
        "registry_sha256": _sha(registry_path),
        "checkpoint_path": str(checkpoint),
        "dependency_run_id": dependency.name,
        "consumer_run_ids": list(bindings),
        "copied_file_count": len(pins),
        "copied_bytes": sum(
            (checkpoint / relative).stat().st_size for relative in pins
        ),
        "live_external_file_count": len(external),
    }
