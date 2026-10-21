"""Retire explicitly identified, finished validation copies, never original runs.

Maintenance only: this is not an ingestion or matching stage. Plan and purge are
separate so the caller can inspect the exact inventory before irreversible work.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .cleanup import METADATA, _files, _path, _read, _review, _sha, _signature, _write

SCHEMA = "validation-build-retirement-v1"
ARCHIVE_NAMES = METADATA | {
    "build-inputs.json",
    "build-complete.json",
    "build-failure.json",
    "inventory.json",
}


def _child(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Explicit contained relative path required")
    return _path(root / relative)


def _inventory(build):
    files = _files(build)
    if any(_review(p.relative_to(build)) for p in files):
        raise ValueError(
            "Human review or database present; validation copy not disposable"
        )
    if any(p.stat().st_nlink != 1 for p in files):
        raise ValueError("Hard-linked files are not independent validation copies")
    return {str(p.relative_to(build)): _signature(p) for p in files}


def _active_processes(build):
    """Linux fail-safe check for processes using the selected build's files."""
    proc = Path("/proc")
    if not proc.is_dir():
        raise ValueError("Validation retirement requires Linux process-use checks")
    active = []

    def uses_build(paths):
        return any(
            Path(p.removesuffix(" (deleted)")).is_relative_to(build)
            for p in paths
            if p.startswith("/")
        )

    for process in proc.iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            argv = (process / "cmdline").read_bytes().split(b"\0")
            paths = [os.fsdecode(arg) for arg in argv if arg.startswith(b"/")]
            paths.append(os.readlink(process / "cwd"))
        except OSError:
            continue
        if uses_build(paths):
            active.append(int(process.name))
            continue
        try:
            fds = list((process / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                linked = os.readlink(fd)
            except OSError:
                continue
            if uses_build([linked]):
                active.append(int(process.name))
                break
    return active


def _ended(build, comparison):
    complete, failure = build / "build-complete.json", build / "build-failure.json"
    if complete.exists() == failure.exists():
        raise ValueError("Build needs exactly one completed or failed terminal record")
    terminal = _read(complete if complete.exists() else failure)
    if complete.exists():
        if terminal.get("status") != "PASS" or comparison is None:
            raise ValueError("Successful validation copy requires passing comparison")
        report = _read(comparison)
        if any(
            not isinstance(report.get(key), str) or not Path(report[key]).is_absolute()
            for key in ("rebuilt_run", "source_run")
        ):
            raise ValueError("Comparison needs explicit absolute run paths")
        rebuilt = _path(terminal["run_path"])
        if (
            rebuilt.parent != build / "workspace/runs"
            or not rebuilt.is_dir()
            or terminal.get("run_id") != rebuilt.name
            or report.get("schema_version") != "rebuild-semantic-audit-v1"
            or report.get("status") != "PASS"
            or report.get("semantic_equal") is not True
            or report.get("difference_count") != 0
            or _path(report.get("rebuilt_run", "")) != rebuilt
        ):
            raise ValueError("Comparison does not validate this exact rebuilt run")
        source = _path(report.get("source_run", ""))
        if not source.is_dir() or source.is_relative_to(build):
            raise ValueError("Comparison source must be a preserved external run")
    elif terminal.get("status") != "FAIL":
        raise ValueError("Failure record is not terminal")
    for run in (
        (build / "workspace/runs").iterdir()
        if (build / "workspace/runs").exists()
        else ()
    ):
        if not run.is_dir():
            raise ValueError("Unexpected file in validation run directory")
        finished = run / "completion.json"
        if finished.is_file() and _read(finished).get("validation_ok") is True:
            if (
                _read(run / "run.json").get("status") != "complete"
                or (run / "failure.json").exists()
            ):
                raise ValueError("Run completion and status disagree")
        elif not (run / "failure.json").is_file():
            raise ValueError(f"Possibly active or incomplete run: {run.name}")
    if _active_processes(build):
        raise ValueError("Validation build is still in use by an active process")
    return terminal


def _originals(build, inputs):
    data_root = build.parents[2]
    release_id = inputs.get("release_id", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", release_id):
        raise ValueError("Invalid frozen release identity")
    release = _child(data_root, f"releases/{release_id}/release.json")
    if _sha(release) != inputs.get("release_sha256"):
        raise ValueError("Original release manifest hash changed")
    manifest = _read(release)
    pins = inputs.get("raw_input_sha256")
    if (
        manifest.get("schema") != "frozen-workbench-v1"
        or not pins
        or pins != manifest.get("raw_input_sha256")
    ):
        raise ValueError("Raw input pins differ from frozen release")
    originals = {str(release): _signature(release)}
    for name, digest in pins.items():
        path = _child(data_root, name)
        if path.is_relative_to(build):
            raise ValueError("Original inputs cannot be inside validation copy")
        actual = _signature(path)
        if actual["sha256"] != digest:
            raise ValueError(f"Original input changed: {name}")
        originals[str(path)] = actual
    inventory = build / "inputs/raw_audit/inventory.json"
    if inventory.is_file():
        for spec in _read(inventory).get("sources", {}).values():
            path = _path(spec["path"])
            if (
                path.is_relative_to(build)
                or str(path) not in originals
                or spec.get("sha256") != originals[str(path)]["sha256"]
            ):
                raise ValueError("Raw-audit original CSV provenance is unsafe")
    return originals


def plan_validation_build(
    build, audit_dir, *, expected_build_inputs_sha256, passing_comparison_report=None
):
    build, audit = _path(build), _path(audit_dir)
    if (
        build.parent.name != "workbench-builds"
        or build.parent.parent.name != "artifacts"
        or not re.fullmatch(r"validation-[A-Za-z0-9_.-]+", build.name)
        or not build.is_dir()
    ):
        raise ValueError(
            "Only direct artifacts/workbench-builds/validation-* copies allowed"
        )
    if audit.exists() or audit.is_relative_to(build) or build.is_relative_to(audit):
        raise ValueError("New external, non-overlapping audit directory required")
    if any(
        audit.is_relative_to(build.parents[2] / name)
        for name in ("releases", "database_offline")
    ):
        raise ValueError("Audit cannot be written into protected originals")
    anchor = build / "build-inputs.json"
    if _sha(anchor) != expected_build_inputs_sha256:
        raise ValueError("Explicit build-inputs SHA-256 does not match")
    inputs = _read(anchor)
    if (
        inputs.get("schema") != "frozen-workbench-v1"
        or inputs.get("old_results_used_as_inputs") is not False
    ):
        raise ValueError("Build is not an independently rebuilt frozen validation copy")
    comparison = _path(passing_comparison_report) if passing_comparison_report else None
    if comparison is not None and comparison.is_relative_to(build):
        raise ValueError("Comparison report must survive outside validation copy")
    terminal = _ended(build, comparison)
    if comparison is not None and audit.is_relative_to(
        _path(_read(comparison)["source_run"])
    ):
        raise ValueError("Audit cannot alter the preserved comparison run")
    if any(terminal.get(key) != inputs.get(key) for key in inputs):
        raise ValueError("Terminal record disagrees with build-inputs identity")
    originals = _originals(build, inputs)
    if comparison is not None:
        originals[str(comparison)] = _signature(comparison)
    inventory = _inventory(build)
    directories = [str(p.relative_to(build)) for p in build.rglob("*") if p.is_dir()]
    value = dict(
        schema=SCHEMA,
        build=str(build),
        audit_dir=str(audit),
        build_inputs_sha256=expected_build_inputs_sha256,
        terminal_status=terminal["status"],
        comparison=str(comparison) if comparison else None,
        inventory=inventory,
        directories=sorted(directories),
        originals=originals,
        files=len(inventory),
        bytes=sum(v["size"] for v in inventory.values()),
        allocated_bytes=sum(v["allocated"] for v in inventory.values()),
    )
    audit.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, info in inventory.items():
        if Path(name).name in ARCHIVE_NAMES and info["size"] <= 10 * 1024**2:
            target = audit / "retained-metadata" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with (build / name).open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
            if _sha(target) != info["sha256"]:
                raise ValueError("Metadata archive differs from inventory")
    if comparison is not None:
        with (
            comparison.open("rb") as src,
            (audit / "comparison.json").open("xb") as dst,
        ):
            shutil.copyfileobj(src, dst)
        if _sha(audit / "comparison.json") != originals[str(comparison)]["sha256"]:
            raise ValueError("Comparison archive changed")
    if _inventory(build) != inventory:
        raise ValueError("Validation copy changed while preparing cleanup")
    value["archived_sha256"] = {
        str(p.relative_to(audit)): _sha(p) for p in _files(audit)
    }
    _write(audit / "plan.json", value)
    return audit / "plan.json"


def purge_validation_build(audit_dir, *, expected_plan_sha256):
    audit = _path(audit_dir)
    if _sha(audit / "plan.json") != expected_plan_sha256:
        raise ValueError("Retirement plan changed")
    plan = _read(audit / "plan.json")
    if plan.get("schema") != SCHEMA or plan.get("audit_dir") != str(audit):
        raise ValueError("Wrong retirement plan identity")
    build = _path(plan["build"])
    comparison = _path(plan["comparison"]) if plan["comparison"] else None
    _ended(build, comparison)
    if (
        _inventory(build) != plan["inventory"]
        or sorted(str(p.relative_to(build)) for p in build.rglob("*") if p.is_dir())
        != plan["directories"]
    ):
        raise ValueError("Changed, missing or foreign validation files/directories")
    for path, signature in plan["originals"].items():
        if _signature(Path(path)) != signature:
            raise ValueError("Protected original or comparison report changed")
    for name, digest in plan["archived_sha256"].items():
        if _sha(_child(audit, name)) != digest:
            raise ValueError("Retained metadata archive changed")
    _write(
        audit / "purge-started.json",
        {"status": "PURGING", "plan_sha256": expected_plan_sha256},
    )
    for name, signature in plan["inventory"].items():
        path = _child(build, name)
        if _signature(path) != signature:
            raise ValueError("File changed during purge; remaining files retained")
        path.unlink()
    for name in sorted(
        plan["directories"], key=lambda n: len(Path(n).parts), reverse=True
    ):
        _child(build, name).rmdir()
    build.rmdir()
    for path, signature in plan["originals"].items():
        if _signature(Path(path)) != signature:
            raise ValueError("Protected original changed during purge")
    result = dict(
        status="PURGED",
        build=str(build),
        files=plan["files"],
        bytes=plan["bytes"],
        allocated_bytes=plan["allocated_bytes"],
        originals_unchanged=len(plan["originals"]),
        recovery="Temporary copy deleted; raw originals and small metadata retained",
    )
    _write(audit / "completed.json", result)
    return result


def retire_validation_build(
    build, audit_dir, *, expected_build_inputs_sha256, passing_comparison_report=None
):
    path = plan_validation_build(
        build,
        audit_dir,
        expected_build_inputs_sha256=expected_build_inputs_sha256,
        passing_comparison_report=passing_comparison_report,
    )
    return purge_validation_build(audit_dir, expected_plan_sha256=_sha(path))
