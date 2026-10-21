"""Frozen local workbench releases and a raw-CSV-to-result entry point.

The bootstrap uses only the standard library. Business imports happen after
entering a verified immutable code snapshot, never through historical results.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CODE_ROOT))
sys.path.insert(1, str(CODE_ROOT / "src"))
SCHEMA = "frozen-workbench-v1"
CODE_DIRS = (
    "tools/merge_review",
    "src",
    "experiments/apc_avl_coverage_study",
    "experiments/apc_avl_direct_evidence",
)
SOURCE_FILES = (
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "README.md",
    "docs/frozen-workbench.md",
    "docs/experiment04c-occurrence-plausibility.md",
    "workbench.sh",
)
MODES = ("global", "early", "middle", "late")


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    path.chmod(0o600)


def safe_child(root, relative):
    root, relative = Path(root).absolute(), Path(relative)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(p in (".", "..") for p in relative.parts)
    ):
        raise ValueError("Expected an explicit contained relative path")
    if ".." in root.parts:
        raise ValueError("Frozen root cannot contain parent traversal")
    candidate = root / relative
    for item in (candidate, *candidate.parents):
        if item.is_symlink():
            raise ValueError(f"Symlink is not a frozen input: {item}")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("Frozen path escaped its root")
    return candidate


def environment():
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "packages": {
            dist.metadata["Name"].lower().replace("_", "-"): dist.version
            for dist in importlib.metadata.distributions()
            if dist.metadata.get("Name")
        },
    }


def verify_environment(expected):
    actual = environment()
    if (
        actual["python"] != expected["python"]
        or actual["implementation"] != expected["implementation"]
    ):
        raise ValueError(
            "Python differs from the release; use the pinned Python environment"
        )
    for name, version in expected["packages"].items():
        if actual["packages"].get(name) != version:
            raise ValueError(
                f"Dependency mismatch {name}: need {version}; "
                "restore pinned environment"
            )
    if actual["packages"].keys() != expected["packages"].keys():
        raise ValueError("Additional unpinned packages in the execution environment")


def verify_release(release, expected_digest=None):
    release = Path(release).absolute()
    lock = safe_child(release, "release.json")
    if expected_digest is None:
        expected_digest = read(safe_child(release, "release.sha256.json"))[
            "release.json"
        ]
    if expected_digest is not None and sha(lock) != expected_digest:
        raise ValueError("Release manifest differs from the selected lock")
    manifest = read(lock)
    if manifest.get("schema") != SCHEMA or not manifest.get("code_sha256"):
        raise ValueError("Unknown or empty frozen release")
    code = safe_child(release, "code")
    actual = set()
    for path in code.rglob("*"):
        relative = str(path.relative_to(code))
        path = safe_child(code, relative)
        if path.is_file():
            actual.add(relative)
        elif not path.is_dir():
            raise ValueError(f"Non-regular frozen code entry: {relative}")
    if actual != set(manifest["code_sha256"]):
        raise ValueError("Missing or extra unpinned files in frozen business code")
    if "tools/merge_review/workbench.py" not in actual:
        raise ValueError("Frozen release lacks its verified entry point")
    for relative, digest in manifest["code_sha256"].items():
        if sha(safe_child(release / "code", relative)) != digest:
            raise ValueError(f"Frozen business code changed: {relative}")
    if sha(safe_child(release, "profiles.json")) != manifest["profiles_sha256"]:
        raise ValueError("Frozen experiment settings changed")
    verify_environment(manifest["environment"])
    return manifest


def verify_inputs(data_root, manifest):
    for relative, digest in manifest["raw_input_sha256"].items():
        if sha(safe_child(data_root, relative)) != digest:
            raise ValueError(f"Raw input changed: {relative}")


def freeze(data_root, profile_runs, *, default_profile="04b"):
    """Create append-only code/config/environment/data locks, not result copies."""
    data_root = safe_child(Path(data_root).absolute().parent, Path(data_root).name)
    profiles, source_pins = {}, {}
    for name, folder in profile_runs.items():
        folder = safe_child(Path(folder).absolute().parent, Path(folder).name)
        if (
            name not in {"03d", "04", "04b", "04c"}
            or read(folder / "completion.json").get("validation_ok") is not True
            or read(folder / "run.json").get("status") != "complete"
            or (folder / "failure.json").exists()
        ):
            raise ValueError("Only complete experiments can supply frozen settings")
        config_pin = sha(safe_child(folder, "config.json"))
        if read(folder / "output_sha256.json").get("config.json") != config_pin:
            raise ValueError("Experiment settings differ from completed output pins")
        profiles[name] = read(folder / "config.json")
        source_pins[name] = {
            "run_id": folder.name,
            "config_sha256": config_pin,
        }
    if default_profile not in profiles:
        raise ValueError(f"A current {default_profile} profile is required")
    manifest_path = data_root / "artifacts/feed_manifest.draft.csv"
    input_names = {
        "database_offline/avl/20240401_Vehicle GPS Rt29 RT 929 Nov 2023.csv",
        "database_offline/apc/ttc_apc_data.csv",
        "artifacts/feed_manifest.draft.csv",
    }
    with manifest_path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            if row.get("status") in ("candidate", "approved") and row.get(
                "calendar_min", ""
            ) <= "2023-11-06" <= row.get("calendar_max", ""):
                input_names.add("database_offline/gtfs/" + row["relative_path"])
    raw_pins = {name: sha(safe_child(data_root, name)) for name in sorted(input_names)}
    release_id = (
        f"workbench-{default_profile}-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    release = safe_child(data_root, "releases/" + release_id)
    release.mkdir(parents=True, mode=0o700)
    selected = [CODE_ROOT / name for name in SOURCE_FILES]
    for name in CODE_DIRS:
        for path in (CODE_ROOT / name).rglob("*"):
            if path.is_symlink() and not any(
                part in {".pytest_cache", ".ruff_cache", "node_modules", "__pycache__"}
                for part in path.parts
            ):
                raise ValueError(f"Symlink in business source tree: {path}")
        selected.extend(
            path
            for path in (CODE_ROOT / name).rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix
            in {".py", ".json", ".mjs", ".js", ".css", ".html", ".md", ".yaml", ".yml"}
            and not any(
                part in {".pytest_cache", ".ruff_cache", "node_modules"}
                for part in path.parts
            )
        )
    pins = {}
    for source in sorted(set(selected)):
        relative = str(source.relative_to(CODE_ROOT))
        source = safe_child(CODE_ROOT, relative)
        destination = release / "code" / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, destination)
        destination.chmod(0o444)
        pins[relative] = sha(destination)
        if sha(source) != pins[relative]:
            raise ValueError("Source changed while freezing the release")
    if any(sha(safe_child(CODE_ROOT, name)) != digest for name, digest in pins.items()):
        raise ValueError("Source changed during release creation")
    for name, folder in profile_runs.items():
        if sha(Path(folder) / "config.json") != source_pins[name]["config_sha256"]:
            raise ValueError("Experiment settings changed during release creation")
    write(release / "profiles.json", profiles)
    manifest = {
        "schema": SCHEMA,
        "release_id": release_id,
        "created_at": datetime.now(UTC).isoformat(),
        "date": "2023-11-06",
        "route_scope": ["29"],
        "default_profile": default_profile,
        "source_profiles": source_pins,
        "code_sha256": pins,
        "profiles_sha256": sha(release / "profiles.json"),
        "raw_input_sha256": raw_pins,
        "environment": environment(),
        "business_results_embedded": False,
        "normalization_clock": "naive America/Toronto wall clock, no invented DST fold",
    }
    write(release / "release.json", manifest)
    write(
        release / "release.sha256.json", {"release.json": sha(release / "release.json")}
    )
    verify_release(release, sha(release / "release.json"))
    verify_inputs(data_root, manifest)
    return {
        "release": str(release.relative_to(data_root)),
        "sha256": sha(release / "release.json"),
    }


def validate_completed_run(folder):
    from tools.merge_review.gtfs_matching.adapter import GTFSReviewData

    folder = Path(folder)
    if (
        read(folder / "completion.json").get("validation_ok") is not True
        or read(folder / "run.json").get("status") != "complete"
        or (folder / "failure.json").exists()
    ):
        raise ValueError("Pipeline did not publish a validated run")
    pins = read(folder / "output_sha256.json")
    required = {
        "manifest.json",
        "config.json",
        "selection.json",
        "summary.json",
        "gtfs/canonical.json",
        *(f"overview/{mode}.json" for mode in MODES),
    }
    if (
        not isinstance(pins, dict)
        or not required.issubset(pins)
        or not any(name.startswith("cases/") for name in pins)
    ):
        raise ValueError("Empty or incomplete published result")
    for relative, digest in pins.items():
        if sha(safe_child(folder, relative)) != digest:
            raise ValueError(f"Published output changed: {relative}")
    data = GTFSReviewData(folder)
    if data.available_matching_modes != list(MODES):
        raise ValueError("Four decision modes were not published")
    for mode in MODES:
        overview = data.day_overview(mode)
        if not overview.get("cases"):
            raise ValueError("Published day overview has no performed segments")
    return {"status": "PASS", "output_pins_verified": len(pins), "four_modes": True}


def run_pipeline(data_root, release, output, profile=None, workers=8, progress=None):
    """No old audit, fixture, run, case or projection is read by this pipeline."""
    data_root, release, output = (
        Path(p).absolute() for p in (data_root, release, output)
    )
    manifest = verify_release(release)
    if CODE_ROOT != release / "code":
        raise ValueError("Pipeline execution requires verified frozen business code")
    sys.dont_write_bytecode = True
    profiles = read(release / "profiles.json")
    profile = profile or manifest.get("default_profile", "04b")
    if profile not in profiles:
        raise ValueError(f"Profile not present in frozen release: {profile}")
    config = profiles[profile]
    if (
        isinstance(workers, bool)
        or not isinstance(workers, int)
        or not 1 <= workers <= 8
    ):
        raise ValueError("Workers must be an integer from 1 to 8")
    if output.exists():
        raise ValueError(
            "A raw rebuild requires a new output directory; nothing is overwritten"
        )
    output = safe_child(output.parent.absolute(), output.name)
    if any(
        output.is_relative_to(path)
        for path in (
            CODE_ROOT,
            data_root / "releases",
            data_root / "database_offline",
        )
    ):
        raise ValueError("Build outputs must not be inside code or raw input trees")
    verify_inputs(data_root, manifest)
    os.environ["TIDES_WORKBENCH_DATA_ROOT"] = str(data_root)
    from tools.merge_review.debug import DebugWorkspace
    from tools.merge_review.raw_bootstrap import bootstrap_raw_day

    # Feed paths come from trusted project_root(), not the UI parameter schema.
    # Keep filesystem locations out of DebugWorkspace's validated GTFS config.
    output.mkdir(parents=True, mode=0o700)
    started = time.perf_counter()
    emit = progress or (
        lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)
    )
    provenance = {
        "schema": SCHEMA,
        "release_id": manifest["release_id"],
        "release_sha256": sha(release / "release.json"),
        "profile": profile,
        "date": manifest["date"],
        "route_scope": manifest["route_scope"],
        "raw_input_sha256": manifest["raw_input_sha256"],
        "old_results_used_as_inputs": False,
        "historical_byte_identity_claimed": False,
    }
    write(output / "build-inputs.json", provenance)
    try:
        raw_names = {
            "avl": "database_offline/avl/20240401_Vehicle GPS Rt29 RT 929 Nov 2023.csv",
            "apc": "database_offline/apc/ttc_apc_data.csv",
        }
        emit({"stage": "raw_csv_normalization", "date": manifest["date"]})
        audit = output / "inputs" / "raw_audit"
        bootstrap = bootstrap_raw_day(
            audit,
            data_root / raw_names["avl"],
            data_root / raw_names["apc"],
            date=manifest["date"],
            expected_hashes={
                key: manifest["raw_input_sha256"][name]
                for key, name in raw_names.items()
            },
        )
        if not all(
            bootstrap["source_counts"].get(source, 0) > 0 for source in raw_names
        ):
            raise ValueError("Raw day normalization produced an empty source")
        workspace = DebugWorkspace(output / "workspace", audit, manifest["date"])
        direction = workspace.create_run(
            {"method": "direction_destination", "segmentation": {}, "merge": {}},
            label="Raw preparation · dedup / PI-PO / direction",
            progress=emit,
        )
        config["gtfs"]["execution_workers"] = workers
        result = workspace.create_run(
            config,
            label=f"Frozen {profile} · raw rebuild · {manifest['release_id']}",
            parent_run_id=direction["run_id"],
            progress=emit,
        )
        validation = validate_completed_run(result["path"])
        verify_inputs(data_root, manifest)
        verify_release(release, provenance["release_sha256"])
        completed = {
            **provenance,
            "status": "PASS",
            "run_id": result["run_id"],
            "run_path": result["path"],
            "workspace": str(workspace.root),
            "raw_audit": str(audit),
            "preparation_run_id": direction["run_id"],
            "validation": validation,
            "summary": result["summary"],
            "elapsed_s": round(time.perf_counter() - started, 3),
        }
        write(output / "build-complete.json", completed)
        return completed
    except BaseException as exc:
        write(
            output / "build-failure.json",
            {**provenance, "status": "FAIL", "error": str(exc)},
        )
        raise


def serve_existing(workspace_path, port=8767, state_dir=None):
    """Cold-load completed outputs without creating an old baseline or day cache."""
    from tools.merge_review.debug import DebugWorkspace
    from tools.merge_review.gtfs_matching.adapter import load_review_data
    from tools.merge_review.server import ReviewServer, run_fingerprint
    from tools.merge_review.store import ReviewStore

    root = Path(workspace_path).resolve()
    runs = [
        read(path)
        for path in (root / "runs").glob("*/run.json")
        if (path.parent / "completion.json").is_file()
    ]
    if not runs:
        raise ValueError("No completed run; execute workbench.sh run first")
    latest = max(runs, key=lambda r: (r.get("created_at", ""), r["run_id"]))
    run = root / "runs" / latest["run_id"]
    fixture = Path(read(run / "manifest.json")["audit"])
    fixture_manifest = read(fixture / "manifest.json")
    workspace = DebugWorkspace(root, Path(fixture_manifest["source_audit"]))
    workspace._fixture_valid(fixture, fixture_manifest)
    workspace._fixture = fixture
    data = load_review_data(run)
    state = Path(state_dir) if state_dir else root / "reviews"
    server = ReviewServer(
        ("127.0.0.1", port),
        data,
        ReviewStore(state, run_fingerprint(run)),
        workspace=workspace,
        debug_state_dir=state,
    )
    print(f"Workbench: http://127.0.0.1:{server.server_port} · {run.name}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "serve", "verify", "freeze"),
        default="run",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("TIDES_WORKBENCH_DATA_ROOT", CODE_ROOT)),
    )
    parser.add_argument("--frozen", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--profile", choices=("03d", "04", "04b", "04c"))
    parser.add_argument(
        "--profile-run",
        action="append",
        default=[],
        help="freeze only: NAME=completed run path",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--serve", action="store_true", help="After rebuilding, serve the new workspace"
    )
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    data_root = args.data_root.resolve()
    os.environ["TIDES_WORKBENCH_DATA_ROOT"] = str(data_root)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    if args.command == "freeze":
        result = freeze(
            data_root,
            dict(value.split("=", 1) for value in args.profile_run),
            default_profile=args.profile or "04b",
        )
        print(json.dumps(result, indent=2))
        return
    pointer = read(data_root / "config/workbench-release.json")
    release = safe_child(data_root, pointer["release"])
    manifest = verify_release(release, pointer["sha256"])
    if args.frozen is None:
        script = release / "code/tools/merge_review/workbench.py"
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-I",
                "-B",
                str(script),
                *sys.argv[1:],
                "--frozen",
                str(release),
                "--data-root",
                str(data_root),
            ],
        )
    if args.frozen.resolve() != release.resolve() or CODE_ROOT != release / "code":
        raise ValueError("Execution must use the selected frozen code snapshot")
    if args.command == "verify":
        verify_inputs(data_root, manifest)
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "release_id": manifest["release_id"],
                    "code_files": len(manifest["code_sha256"]),
                    "raw_inputs": len(manifest["raw_input_sha256"]),
                }
            )
        )
    elif args.command == "serve":
        serve_existing(
            args.workspace or data_root / "artifacts/merge-debug-20231106",
            args.port,
            args.state_dir,
        )
    else:
        output = args.output or data_root / "artifacts/workbench-builds" / (
            "build-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        result = run_pipeline(
            data_root, release, output.absolute(), args.profile, args.workers
        )
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        if args.serve:
            serve_existing(result["workspace"], args.port, args.state_dir)


if __name__ == "__main__":
    main()
