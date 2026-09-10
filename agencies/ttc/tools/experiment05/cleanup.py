"""Explicit retirement of generated 03d–04c results; dry-run is the default.

This administrative allowlist is not matcher logic. Historical pins, candidate
reports, raw observations, GTFS and source snapshots are never rewritten. Once
retired, full historical UI inspection and live output-pin validation are no
longer available; the saved pins describe the pre-retirement artifacts only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "experiment05-cleanup-v1"
WORKSPACE = "artifacts/merge-debug-20231106"
REPORT_ROOT = "outputs/experiment_05/cleanup"
SNAPSHOT = "releases/pre-experiment-05-20260910"
SNAPSHOT_TAG = "pre-experiment-05-20260910"
APPROVED_RUNS = {
    "run-20231106-20260909T183705-d67ffa30723d": ("03d", "historical", 306),
    "run-20231106-20260910T030622-57e42718b0c1": ("04", "historical", 306),
    "run-20231106-20260910T041452-de4609d0825b": ("04b", "historical", 306),
    "run-20231106-20260910T063603-e4bf6bc60823": ("04c", "current_04c", 306),
}
MODES = ("global", "early", "middle", "late")
CASE = re.compile(r"cases/[0-9a-f]{24}\.json\.gz\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
IGNORED_CACHES = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
}


def _path(value):
    path = Path(value).absolute()
    if ".." in path.parts:
        raise ValueError("Parent traversal is forbidden")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Symlink is forbidden: {part}")
    return path


def _child(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Invalid relative cleanup path")
    if Path(relative).is_absolute() or any(
        p in {"", ".", ".."} for p in relative.split("/")
    ):
        raise ValueError("Unsafe relative cleanup path")
    return _path(root / relative)


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("Duplicate cleanup JSON key")
        result[key] = value
    return result


def _read(path):
    with _path(path).open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=_pairs)


def _write(path, value):
    with _path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def _stat(info):
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Only regular files are permitted")
    return {
        "bytes": info.st_size,
        "inode": info.st_ino,
        "device": info.st_dev,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _fingerprint(path):
    path = _path(path)
    before = _stat(path.lstat())
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if before != _stat(path.lstat()):
        raise ValueError(f"File changed while hashing: {path}")
    return {**before, "sha256": digest}


def _files(path, *, ignore_caches=False):
    path = _path(path)
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise ValueError(f"Required directory is absent: {path}")
    result = []
    for directory, dirs, files in os.walk(path, followlinks=False):
        if ignore_caches:
            dirs[:] = [name for name in dirs if name not in IGNORED_CACHES]
        for name in dirs + files:
            _path(Path(directory) / name)
        for name in files:
            candidate = Path(directory) / name
            _stat(candidate.lstat())
            result.append(candidate)
    return sorted(result)


def _tag_commit(root):
    command = [
        "git",
        "-C",
        str(root),
        "rev-parse",
        "--verify",
        f"refs/tags/{SNAPSHOT_TAG}^{{commit}}",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", result.stdout.strip()):
        raise ValueError("Required pre-experiment-05 Git tag is absent")
    return result.stdout.strip()


def _snapshot(root):
    folder = _child(root, SNAPSHOT)
    path = folder / "snapshot_manifest.json"
    value = _read(path)
    if (
        value.get("schema") != "pre-experiment-05-snapshot-v1"
        or value.get("snapshot_complete") is not True
    ):
        raise ValueError("A completed pre-05 snapshot is required")
    pins = value.get("files")
    raw = value.get("raw_inputs_preserved_in_place")
    if not isinstance(pins, dict) or not pins or not isinstance(raw, dict) or not raw:
        raise ValueError("Snapshot inventories must be nonempty")
    sample = value.get("sample_relative_path")
    if not isinstance(sample, str) or not CASE.fullmatch(sample):
        raise ValueError("Snapshot must contain an identified representative case")
    required = {
        "run/" + sample,
        "upstream_sample/" + sample,
        "run/gtfs/canonical.json",
        "run/gtfs/feed.json",
        "run/config.json",
        "run/manifest.json",
    }
    if not required <= pins.keys():
        raise ValueError("Minimum replay snapshot is incomplete")
    if {str(p.relative_to(folder)) for p in _files(folder)} != {
        *pins,
        "snapshot_manifest.json",
    }:
        raise ValueError("Snapshot contains missing or unpinned files")
    for relative, record in pins.items():
        actual = _fingerprint(_child(folder, relative))
        if actual["sha256"] != record.get("sha256") or actual["bytes"] != record.get(
            "bytes"
        ):
            raise ValueError(f"Snapshot pin changed: {relative}")
    for relative, digest in raw.items():
        if _fingerprint(_child(root, relative))["sha256"] != digest:
            raise ValueError(f"Original raw input changed: {relative}")
    return {
        "path": SNAPSHOT,
        "sha256": _fingerprint(path)["sha256"],
        "git_tag": SNAPSHOT_TAG,
        "git_commit": _tag_commit(root),
        "verified_snapshot_files": len(pins),
        "verified_raw_inputs": raw,
    }


def _kind(relative):
    if CASE.fullmatch(relative):
        return "compressed_case"
    if relative in {f"overview/{mode}.json" for mode in MODES}:
        return "day_overview"
    if relative in {f"shadow/{mode}-episodes.json" for mode in MODES}:
        return "shadow_episode_trace"
    return None


def _run_files(root):
    return {
        str(p.relative_to(root)): p
        for run in APPROVED_RUNS
        for p in _files(_child(root, f"{WORKSPACE}/runs/{run}"))
    }


def _protected_files(root, targets):
    # All non-selected workspace files remain intact, including every review,
    # GTFS reference, raw observation, catalog, report and historical output pin.
    roots = [
        WORKSPACE,
        "database_offline",
        "releases",
        "config",
        "src",
        "tools",
        "tests",
        "experiments",
        "docs",
        "README.md",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "workbench.sh",
        "artifacts/feed_manifest.draft.csv",
        "artifacts/apc-avl-coverage-study-20260908/raw_audit",
    ]
    roots.extend(
        str(p.relative_to(root))
        for p in (root / "artifacts").glob("gtfs-*")
        if p.is_dir()
    )
    roots.extend(
        str(p.relative_to(root)) for p in (root / "artifacts").rglob("reviews")
    )
    paths = set()
    for name in roots:
        path = _child(root, name)
        if path.exists():
            paths.update(_files(path, ignore_caches=True))
    return {
        str(p.relative_to(root)): _fingerprint(p)
        for p in sorted(paths)
        if str(p.relative_to(root)) not in targets
    }


def _approve_record(relative, row):
    parts = relative.split("/")
    prefix = WORKSPACE.split("/") + ["runs"]
    if parts[: len(prefix)] != prefix or len(parts) <= len(prefix) + 1:
        raise ValueError("Cleanup target lies outside approved run roots")
    run = parts[len(prefix)]
    if run not in APPROVED_RUNS:
        raise ValueError("Cleanup target run is not approved")
    profile, phase, _ = APPROVED_RUNS[run]
    kind = _kind("/".join(parts[len(prefix) + 1 :]))
    if kind is None or (profile == "03d" and kind == "shadow_episode_trace"):
        raise ValueError("Cleanup target is not an allowlisted generated output")
    if (row.get("profile"), row.get("phase"), row.get("kind")) != (
        profile,
        phase,
        kind,
    ):
        raise ValueError("Cleanup target classification changed")


def plan_cleanup(project_root, *, report_dir=None, phases=None):
    """Write a new dry-run CSV and hash-bound manifest; never remove any file."""
    root = _path(project_root)
    report = _path(report_dir or root / REPORT_ROOT)
    if not report.is_relative_to(_child(root, REPORT_ROOT)) or report.exists():
        raise ValueError("Reports require a new directory under experiment_05/cleanup")
    snapshot = _snapshot(root)
    phases = set(phases or {row[1] for row in APPROVED_RUNS.values()})
    if not phases or not phases <= {row[1] for row in APPROVED_RUNS.values()}:
        raise ValueError("Unknown cleanup phase")
    all_files = _run_files(root)
    selected, counts = {}, {}
    for run, (profile, phase, case_count) in APPROVED_RUNS.items():
        if phase not in phases:
            continue
        prefix = f"{WORKSPACE}/runs/{run}/"
        outputs = _read(root / prefix / "output_sha256.json")
        manifest = _read(root / prefix / "manifest.json")
        if (
            manifest.get("run_id") != run
            or _read(root / prefix / "completion.json").get("validation_ok") is not True
        ):
            raise ValueError("Only completed identified runs may be retired")
        found = []
        for relative, path in all_files.items():
            if not relative.startswith(prefix) or not (
                kind := _kind(relative[len(prefix) :])
            ):
                continue
            fingerprint = _fingerprint(path)
            if outputs.get(relative[len(prefix) :]) != fingerprint["sha256"]:
                raise ValueError(f"Historical output pin changed: {relative}")
            selected[relative] = {
                **fingerprint,
                "run_id": run,
                "profile": profile,
                "phase": phase,
                "kind": kind,
            }
            _approve_record(relative, selected[relative])
            found.append(kind)
        expected = {
            "compressed_case": case_count,
            "day_overview": 4,
            "shadow_episode_trace": 0 if profile == "03d" else 4,
        }
        if {kind: found.count(kind) for kind in expected} != expected:
            raise ValueError(f"Missing or unexpected generated-output inventory: {run}")
        counts[run] = {
            "profile": profile,
            "phase": phase,
            "files": len(found),
            "bytes": sum(
                row["bytes"] for row in selected.values() if row["run_id"] == run
            ),
        }
    preserved = _protected_files(root, selected)
    report.mkdir(parents=True, mode=0o700)
    csv_path = report / "cleanup_dry_run.csv"
    with csv_path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "relative_path",
                "bytes",
                "sha256",
                "run_id",
                "profile",
                "phase",
                "kind",
                "action",
            ]
        )
        for relative, row in sorted(selected.items()):
            writer.writerow(
                [
                    relative,
                    *(
                        row[key]
                        for key in (
                            "bytes",
                            "sha256",
                            "run_id",
                            "profile",
                            "phase",
                            "kind",
                        )
                    ),
                    "PROPOSED_DELETE_NOT_EXECUTED",
                ]
            )
    value = {
        "schema": SCHEMA,
        "state": "DRY_RUN",
        "created_at": datetime.now(UTC).isoformat(),
        "root": str(root),
        "report_dir": str(report),
        "snapshot": snapshot,
        "approved_run_roots": [f"{WORKSPACE}/runs/{run}" for run in APPROVED_RUNS],
        "phases": sorted(phases),
        "runs": counts,
        "files": selected,
        "before_bytes": sum(row["bytes"] for row in selected.values()),
        "file_count": len(selected),
        "deleted_paths": [],
        "preserved_paths": sorted(preserved),
        "preserved_sha256": preserved,
        "unmodified_generated_dependency_dirs_not_hashed": sorted(IGNORED_CACHES),
        "csv_sha256": _fingerprint(csv_path)["sha256"],
        "effect": (
            "Full retired UI and live full-output-pin checks unavailable; "
            "original pins remain historical evidence."
        ),
        "regeneration": (
            "Verified frozen snapshot + original CSV/GTFS inputs; "
            "no old results used by experiment 05."
        ),
    }
    manifest = report / "cleanup_manifest.json"
    _write(manifest, value)
    _write(
        report / "cleanup_manifest.sha256.json",
        {"sha256": _fingerprint(manifest)["sha256"]},
    )
    return manifest


def _verify_files(root, inventory):
    for relative, expected in inventory.items():
        actual = _fingerprint(_child(root, relative))
        if any(actual[key] != expected[key] for key in actual):
            raise ValueError(f"Planned or protected file changed: {relative}")


def _unlink_verified(path, expected):
    """Traverse with no-follow directory descriptors, then unlink one exact name."""
    path = _path(path)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = following
        with os.fdopen(
            os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor), "rb"
        ) as stream:
            actual = _stat(os.fstat(stream.fileno()))
            actual["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            if any(actual[key] != expected[key] for key in actual):
                raise ValueError(f"File changed immediately before deletion: {path}")
            if _stat(os.fstat(stream.fileno())) != {
                key: expected[key] for key in _stat(os.fstat(stream.fileno()))
            }:
                raise ValueError("File changed during final pre-deletion hash")
            if _stat(
                os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            ) != _stat(os.fstat(stream.fileno())):
                raise ValueError("File replaced before deletion")
            os.unlink(path.name, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def execute_cleanup(manifest_path, *, expected_manifest_sha256, phases):
    """Explicit irreversible execution; caller must arrange old UI retirement."""
    path = _path(manifest_path)
    if not isinstance(expected_manifest_sha256, str) or not SHA.fullmatch(
        expected_manifest_sha256
    ):
        raise ValueError("Explicit expected manifest SHA-256 is required")
    if _fingerprint(path)["sha256"] != expected_manifest_sha256:
        raise ValueError("Cleanup manifest changed")
    value = _read(path)
    root, report = _path(value["root"]), path.parent
    if (
        value.get("schema") != SCHEMA
        or value.get("state") != "DRY_RUN"
        or value.get("deleted_paths") != []
    ):
        raise ValueError("Only an unexecuted dry-run plan may be executed")
    if report != _path(value["report_dir"]) or not report.is_relative_to(
        _child(root, REPORT_ROOT)
    ):
        raise ValueError("Cleanup report is outside the approved report directory")
    if value["approved_run_roots"] != [
        f"{WORKSPACE}/runs/{run}" for run in APPROVED_RUNS
    ]:
        raise ValueError("Approved roots changed")
    if _fingerprint(report / "cleanup_dry_run.csv")["sha256"] != value["csv_sha256"]:
        raise ValueError("Dry-run CSV changed")
    if _snapshot(root) != value["snapshot"]:
        raise ValueError("Snapshot or Git tag identity changed")
    phases = set(phases)
    if not phases or not phases <= set(value["phases"]):
        raise ValueError("Explicit planned execution phases are required")
    for relative, row in value["files"].items():
        _approve_record(relative, row)
    if (
        value["file_count"] != len(value["files"])
        or value["before_bytes"] != sum(row["bytes"] for row in value["files"].values())
        or value["preserved_paths"] != sorted(value["preserved_sha256"])
    ):
        raise ValueError("Cleanup manifest inventory totals are inconsistent")
    _verify_files(root, value["files"])
    _verify_files(root, value["preserved_sha256"])
    if _protected_files(root, value["files"]) != value["preserved_sha256"]:
        raise ValueError("Protected inventory has missing or foreign files")
    if set(_run_files(root)) != set(value["files"]) | {
        p
        for p in value["preserved_paths"]
        if any(p.startswith(f"{WORKSPACE}/runs/{run}/") for run in APPROVED_RUNS)
    }:
        raise ValueError("Approved runs contain missing or foreign files")
    chosen = {p: row for p, row in value["files"].items() if row["phase"] in phases}
    held = {p: row for p, row in value["files"].items() if p not in chosen}
    started = {
        "manifest_sha256": expected_manifest_sha256,
        "phases": sorted(phases),
        "started_at": datetime.now(UTC).isoformat(),
        "selected_file_count": len(chosen),
    }
    if any(
        (report / name).exists()
        for name in (
            "execution_started.json",
            "deletion_journal.jsonl",
            "retirement_manifest.json",
        )
    ):
        raise ValueError("This cleanup plan already has an execution record")
    _write(report / "execution_started.json", started)
    deleted, error = [], None
    try:
        with (report / "deletion_journal.jsonl").open("x", encoding="utf-8") as journal:
            for relative, row in sorted(chosen.items()):
                journal.write(
                    json.dumps({"intent": relative, "sha256": row["sha256"]}) + "\n"
                )
                journal.flush()
                os.fsync(journal.fileno())
                _unlink_verified(_child(root, relative), row)
                deleted.append(relative)
                journal.write(json.dumps({"deleted": relative}) + "\n")
                journal.flush()
        _verify_files(root, value["preserved_sha256"] | held)
        if _protected_files(root, value["files"]) != value["preserved_sha256"]:
            raise ValueError("Protected inventory changed during cleanup")
        if _snapshot(root) != value["snapshot"]:
            raise ValueError("Protected snapshot changed during cleanup")
    except BaseException as exc:
        error = str(exc)
    retirement = {
        **started,
        "schema": SCHEMA,
        "state": "COMPLETE" if error is None else "PARTIAL_FAILURE",
        "finished_at": datetime.now(UTC).isoformat(),
        "deleted_paths": deleted,
        "deleted_bytes": sum(chosen[p]["bytes"] for p in deleted),
        "preserved_files_verified_after": error is None,
        "preserved_inventory_sha256": hashlib.sha256(
            json.dumps(value["preserved_sha256"], sort_keys=True).encode()
        ).hexdigest(),
        "retired_runs": sorted({chosen[p]["run_id"] for p in deleted}),
        "historical_full_ui_available": False,
        "historical_output_pins_rewritten": False,
        "recovery": (
            "No quarantine: regenerate from frozen source and preserved originals; "
            "bit-identical historical replay is not claimed."
        ),
        "error": error,
    }
    _write(report / "retirement_manifest.json", retirement)
    if error is not None:
        raise ValueError(
            f"Cleanup stopped after {len(deleted)} files; "
            f"see retirement manifest: {error}"
        )
    return retirement


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["plan", "execute"], nargs="?", default="plan"
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).absolute().parents[2]
    )
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument(
        "--phase", action="append", choices=["historical", "current_04c"]
    )
    parser.add_argument("--confirm-delete-exact-plan", action="store_true")
    args = parser.parse_args()
    if args.action == "plan":
        result = plan_cleanup(args.root, report_dir=args.report_dir, phases=args.phase)
        print(
            json.dumps(
                {
                    "state": "DRY_RUN",
                    "manifest": str(result),
                    "sha256": _fingerprint(result)["sha256"],
                }
            )
        )
    else:
        if (
            not args.confirm_delete_exact_plan
            or not args.manifest
            or not args.manifest_sha256
            or not args.phase
        ):
            parser.error(
                "Execution requires --manifest, --manifest-sha256, --phase "
                "and --confirm-delete-exact-plan"
            )
        print(
            json.dumps(
                execute_cleanup(
                    args.manifest,
                    expected_manifest_sha256=args.manifest_sha256,
                    phases=args.phase,
                )
            )
        )


if __name__ == "__main__":
    main()
