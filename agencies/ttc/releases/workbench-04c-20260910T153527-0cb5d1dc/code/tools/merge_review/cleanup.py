"""Explicit cleanup plans: hash, quarantine, validate, then restore or purge.

No experiment identities are inferred. Callers explicitly protect raw inputs,
frozen releases and retained runs. Review databases are always protected. A
completed checkpoint and a cold-start UI report are required before purging.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
from pathlib import Path

METADATA = {
    "manifest.json",
    "config.json",
    "completion.json",
    "output_sha256.json",
    "selection.json",
    "summary.json",
    "run.json",
    "failure.json",
}
RAW = {"raw", "raw_data", "raw_audit", "database_offline"}


def _path(value):
    path = Path(value).absolute()
    if ".." in path.parts:
        raise ValueError(f"Parent traversal forbidden: {path}")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"Symlink forbidden: {component}")
    return path


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _read(path):
    return json.loads(_path(path).read_text())


def _write(path, value):
    path = _path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def _signature(path):
    path = _path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Ordinary file required: {path}")
    return dict(
        size=info.st_size,
        allocated=info.st_blocks * 512,
        mtime_ns=info.st_mtime_ns,
        inode=info.st_ino,
        device=info.st_dev,
        mode=info.st_mode,
        sha256=_sha(path),
    )


def _files(path):
    path = _path(path)
    if not path.exists():
        raise ValueError(f"Required path missing: {path}")
    if not path.is_file() and not path.is_dir():
        raise ValueError(f"Special file forbidden: {path}")
    candidates = [path] if path.is_file() else sorted(path.rglob("*"))
    files = []
    for candidate in candidates:
        _path(candidate)
        if candidate.is_file():
            files.append(candidate)
        elif not candidate.is_dir():
            raise ValueError(f"Special file forbidden: {candidate}")
    return files


def _review(path):
    return any("review" in p.lower() for p in path.parts[:-1]) or any(
        marker in path.name.lower() for marker in (".sqlite", ".db", "review")
    )


def _snapshot(paths):
    return {str(p): _signature(p) for root in paths for p in _files(root)}


def plan(root, targets, protected_paths, audit_dir):
    """Create a new immutable plan; inputs are explicit paths, not globs."""
    root, audit = _path(root), _path(audit_dir)
    if (
        root in (Path("/"), Path.home(), Path.cwd(), *Path.cwd().parents)
        or len(root.parts) < 3
        or RAW.intersection(root.parts)
        or not root.is_dir()
    ):
        raise ValueError("A dedicated artifact root is required")
    targets = sorted({_path(root / t) for t in targets})
    protected = sorted({_path(p) for p in protected_paths})
    if not targets or not protected or audit.exists():
        raise ValueError("Explicit targets/protection and new audit directory required")
    if any(audit.is_relative_to(p) or p.is_relative_to(audit) for p in protected):
        raise ValueError("Cleanup audit overlaps protected paths")
    for target in targets:
        if target == root or not target.is_relative_to(root):
            raise ValueError(f"Target outside artifact root: {target}")
        if RAW.intersection(target.relative_to(root).parts):
            raise ValueError(f"Raw data cannot be cleaned: {target}")
        if any(target != other and target.is_relative_to(other) for other in targets):
            raise ValueError("Overlapping cleanup targets")
        if audit.is_relative_to(target) or target.is_relative_to(audit):
            raise ValueError("Cleanup audit overlaps targets")
        if any(p.is_relative_to(target) or target.is_relative_to(p) for p in protected):
            raise ValueError(f"Target overlaps protected path: {target}")
    files = [p for target in targets for p in _files(target)]
    if any(RAW.intersection(p.relative_to(root).parts) for p in files):
        raise ValueError("Raw data below target cannot be cleaned")
    reviews = [p for p in _files(root) if _review(p.relative_to(root))]
    protected_files = _snapshot(protected + reviews)
    chosen = [p for p in files if not _review(p.relative_to(root))]
    if not chosen:
        raise ValueError("No disposable files in targets")
    inventory = {str(p.relative_to(root)): _signature(p) for p in chosen}
    directories = sorted(
        {
            str(p.relative_to(root))
            for target in targets
            for p in (target, *target.rglob("*"))
            if p.is_dir()
        }
    )
    value = dict(
        version=1,
        root=str(root),
        audit_dir=str(audit),
        targets=[str(p.relative_to(root)) for p in targets],
        directories=directories,
        inventory=inventory,
        protected_paths=[str(p) for p in protected],
        protected=protected_files,
        bytes=sum(v["size"] for v in inventory.values()),
        allocated_bytes=sum(v["allocated"] for v in inventory.values()),
    )
    _write(audit / "plan.json", value)
    _write(audit / "plan-sha256.json", _sha(audit / "plan.json"))
    return audit / "plan.json"


def _load(audit_dir):
    audit = _path(audit_dir)
    if _sha(audit / "plan.json") != _read(audit / "plan-sha256.json"):
        raise ValueError("Cleanup plan changed")
    value = _read(audit / "plan.json")
    if value["audit_dir"] != str(audit):
        raise ValueError("Cleanup audit moved")
    return audit, value, _path(value["root"])


def _protected(value):
    root = _path(value["root"])
    reviews = [p for p in _files(root) if _review(p.relative_to(root))]
    actual = _snapshot([Path(p) for p in value["protected_paths"]] + reviews)
    if actual != value["protected"]:
        raise ValueError("Protected or review files changed; cleanup stopped")
    return len(actual)


def _quarantine(audit, value):
    directory = audit / "quarantine"
    actual = {str(p.relative_to(directory)): _signature(p) for p in _files(directory)}
    if actual != value["inventory"]:
        raise ValueError("Quarantine contains changed, missing or foreign files")


def stage(audit_dir):
    """Move each inventoried file, preserving review descendants in place."""
    audit, value, root = _load(audit_dir)
    if (audit / "staged.json").exists() or (audit / "quarantine").exists():
        raise ValueError("Plan already staged")
    _protected(value)
    actual = {
        str(p.relative_to(root)): _signature(p)
        for t in value["targets"]
        for p in _files(root / t)
        if not _review(p.relative_to(root))
    }
    if actual != value["inventory"]:
        raise ValueError("Target inventory changed")
    moved = []
    try:
        for name, expected in value["inventory"].items():
            source, destination = _path(root / name), _path(audit / "quarantine" / name)
            if _signature(source) != expected or destination.exists():
                raise ValueError("Target changed during staging")
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            moved.append(name)
        _protected(value)
        # A missing historical run activates the verified retention overlay.
        # Remove only inventoried empty directories; review descendants stay.
        for name in sorted(
            value["directories"], key=lambda n: len(Path(n).parts), reverse=True
        ):
            directory = _path(root / name)
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        _write(
            audit / "staged.json",
            dict(
                status="STAGED_REVERSIBLE",
                plan_sha256=_sha(audit / "plan.json"),
                files=len(moved),
            ),
        )
    except BaseException:
        for name in reversed(moved):
            if (root / name).exists():
                raise ValueError(
                    "Rollback destination occupied; quarantine retained"
                ) from None
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (audit / "quarantine" / name).rename(root / name)
        raise
    return len(moved)


def restore(audit_dir):
    audit, value, root = _load(audit_dir)
    _quarantine(audit, value)
    if any((root / name).exists() for name in value["inventory"]):
        raise ValueError("Restore would overwrite new files")
    for name in value["inventory"]:
        destination = _path(root / name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        (audit / "quarantine" / name).rename(destination)
    _write(
        audit / "restored.json", dict(status="RESTORED", files=len(value["inventory"]))
    )


def record_validation(audit_dir, checkpoint_path, ui_cold_check_path):
    audit, value, _ = _load(audit_dir)
    staged = _read(audit / "staged.json")
    if staged["plan_sha256"] != _sha(audit / "plan.json"):
        raise ValueError("Staged plan identity changed")
    _quarantine(audit, value)
    reports = {}
    for name, path in (
        ("checkpoint", checkpoint_path),
        ("ui_cold_check", ui_cold_check_path),
    ):
        path = _path(path)
        report = _read(path)
        if report.get("status") != "PASS" or (
            name == "ui_cold_check" and report.get("cold_start") is not True
        ):
            raise ValueError(f"Passing {name} report required")
        reports[name] = dict(path=str(path), sha256=_sha(path))
    result = dict(
        status="PASS",
        plan_sha256=_sha(audit / "plan.json"),
        verified_retained_files=_protected(value),
        **reports,
    )
    _write(audit / "validation.json", result)
    return result


def purge(audit_dir):
    """Irreversible: validate everything before the first exact-file unlink."""
    audit, value, root = _load(audit_dir)
    staged = _read(audit / "staged.json")
    if staged.get("status") != "STAGED_REVERSIBLE" or staged.get("plan_sha256") != _sha(
        audit / "plan.json"
    ):
        raise ValueError("An intact staged plan is required")
    validation = _read(audit / "validation.json")
    if validation.get("status") != "PASS" or validation.get("plan_sha256") != _sha(
        audit / "plan.json"
    ):
        raise ValueError("Passing validation bound to this plan required")
    for name in ("checkpoint", "ui_cold_check"):
        report = validation[name]
        if _sha(_path(report["path"])) != report["sha256"]:
            raise ValueError("Validation report changed")
    if _protected(value) != validation["verified_retained_files"]:
        raise ValueError("Protected inventory changed")
    _quarantine(audit, value)
    for name, expected in value["inventory"].items():
        if Path(name).name in METADATA and expected["size"] <= 10 * 1024**2:
            source, destination = (
                audit / "quarantine" / name,
                audit / "retained-metadata" / name,
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst)
            if _sha(destination) != expected["sha256"]:
                raise ValueError("Metadata archive checksum mismatch")
    _write(
        audit / "purge-started.json",
        dict(status="PURGING", files=len(value["inventory"])),
    )
    for name, expected in value["inventory"].items():
        path = _path(audit / "quarantine" / name)
        if _signature(path) != expected:
            raise ValueError("Quarantine changed during purge; remaining files kept")
        path.unlink()
    directories = list((audit / "quarantine").rglob("*")) + [audit / "quarantine"]
    directories += [root / name for name in value["directories"]]
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        directory = _path(directory)
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    _protected(value)
    result = dict(
        status="PURGED",
        files=len(value["inventory"]),
        bytes=value["bytes"],
        allocated_bytes=value["allocated_bytes"],
        recovery="Full files not backed up; selected metadata retained",
    )
    _write(audit / "completed.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("plan", "stage", "restore", "validate", "purge")
    )
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--protect", action="append", default=[], type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--ui-cold-check", type=Path)
    args = parser.parse_args()
    if args.action == "plan":
        if args.root is None:
            parser.error("plan requires --root")
        result = plan(args.root, args.target, args.protect, args.audit)
    elif args.action == "validate":
        if args.checkpoint is None or args.ui_cold_check is None:
            parser.error("validate requires --checkpoint and --ui-cold-check")
        result = record_validation(args.audit, args.checkpoint, args.ui_cold_check)
    else:
        result = {"stage": stage, "restore": restore, "purge": purge}[args.action](
            args.audit
        )
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
