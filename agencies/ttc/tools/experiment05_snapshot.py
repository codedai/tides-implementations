"""Append-only pre-experiment snapshot; never edit an input or historical run."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from tools.merge_review.workbench import environment, read, sha, verify_release, write


def snapshot(root, run, destination, sample):
    root, run, destination = map(lambda p: Path(p).resolve(), (root, run, destination))
    if destination.exists():
        raise ValueError("Snapshot destination must not exist")
    pointer = read(root / "config/workbench-release.json")
    release = root / pointer["release"]
    lock = verify_release(release, pointer["sha256"])
    if read(run / "completion.json").get("validation_ok") is not True:
        raise ValueError("Snapshot source is not a validated complete run")
    pinned = read(run / "output_sha256.json")
    sample_path = Path(sample)
    if sample_path.is_absolute() or ".." in sample_path.parts:
        raise ValueError("Sample must be an explicit relative case path")
    destination.mkdir(parents=True)
    copied = {}

    def keep(source, relative, expected=None):
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Not a regular snapshot source: {source}")
        digest = sha(source)
        if expected is not None and digest != expected:
            raise ValueError(f"Snapshot source hash mismatch: {source}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if sha(target) != digest or sha(source) != digest:
            raise ValueError(f"Snapshot copy mismatch: {source}")
        copied[str(relative)] = {"sha256": digest, "source": str(source),
                                 "bytes": target.stat().st_size}

    for name, digest in lock["code_sha256"].items():
        keep(release / "code" / name, Path("code") / name, digest)
    for name in ("release.json", "release.sha256.json", "profiles.json"):
        keep(release / name, Path("frozen_release") / name)
    keep(root / "config/workbench-release.json", "frozen_release/pointer.json")
    for source in sorted((root / "tests").rglob("*")):
        if source.is_file() and source.suffix in {".py", ".md", ".json", ".csv"}:
            keep(source, Path("code/tests") / source.relative_to(root / "tests"))
    for source in sorted(run.glob("*.json")):
        keep(source, Path("run") / source.name, pinned.get(source.name))
    for source in sorted((run / "gtfs").glob("*.json")):
        relative = source.relative_to(run)
        keep(source, Path("run") / relative, pinned[str(relative)])
    keep(run / sample_path, Path("run") / sample_path, pinned[str(sample_path)])
    source_meta = read(run / "manifest.json")
    parent_name = source_meta.get("partial_source_path")
    if parent_name:
        parent = Path(parent_name)
        if parent.is_dir() and (parent / sample_path).is_file():
            parent_pins = read(parent / "output_sha256.json")
            keep(parent / sample_path, Path("upstream_sample") / sample_path,
                 parent_pins[str(sample_path)])
            for source in sorted(parent.glob("*.json")):
                keep(source, Path("upstream_sample") / source.name,
                     parent_pins.get(source.name))
    reports = root / "artifacts/merge-debug-20231106/experiment04c-validation"
    # This is an archival tool for an explicitly supplied historic run, not a
    # production date selector. The existing report location is provenance only.
    for name in ("summary.md", "full-run-audit.json", "python-tests-final.xml"):
        if (reports / name).is_file():
            keep(reports / name, Path("reports") / name)
    for name in ("artifacts/feed_manifest.draft.csv", "uv.lock", ".python-version"):
        keep(root / name, Path("input_metadata") / Path(name).name)
    manifest = {
        "schema": "pre-experiment-05-snapshot-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "repository_parent_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "frozen_release": pointer,
        "final_config_sha256": sha(run / "config.json"),
        "environment": environment(),
        "raw_inputs_preserved_in_place": lock["raw_input_sha256"],
        "files": copied,
        "sample_relative_path": str(sample_path),
        "source_run": str(run),
        "reproduction": "Frozen code + profiles + original raw inputs; full raw rebuild. "
                        "Only representative heavy cases are embedded. Full historical "
                        "replay requires upstream files that may later be retired.",
        "snapshot_complete": True,
    }
    write(destination / "snapshot_manifest.json", manifest)
    return {"path": str(destination), "files": len(copied),
            "bytes": sum(item["bytes"] for item in copied.values()),
            "manifest_sha256": sha(destination / "snapshot_manifest.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--sample", required=True)
    args = parser.parse_args()
    print(json.dumps(snapshot(args.root, args.run, args.destination, args.sample), indent=2))


if __name__ == "__main__":
    main()
