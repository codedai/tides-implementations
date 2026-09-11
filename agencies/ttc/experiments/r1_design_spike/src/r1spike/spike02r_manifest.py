"""Spike-02R reproducibility: content hashes, source-tree hash, manifest,
and the guarded, permission-restricted run directory.

Spec section 2C: filename+size fingerprints are NOT acceptable — every
input and output records a real content SHA-256.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import polars as pl

from .spike02r_contracts import (
    ALGORITHM_VERSION02R,
    ASSUMPTIONS02R,
    MARKERS02R,
    UNRESOLVED02R,
)

RUN_DIR_PREFIX02R = "experimental-r1-spike02r-"
IMMUTABLE_SOURCE_DIR = "database_offline"
FORBIDDEN_NAME_TOKENS = (
    "vehicles",
    "vehicle_locations",
    "trips_performed",
    "trip_id_performed",
    "stop_visits",
    "passenger_events",
    "datapackage",
    "tides",
)
ALLOWED_JSON_NAMES02R = {
    "authorization_snapshot.json",
    "run_manifest.json",
    "source_tree_manifest.json",
    "input_content_hashes.json",
    "output_content_hashes.json",
    "experiment_preregistration.json",
    "comparator_integrity_report.json",
    "evidence_reuse_report.json",
    "dual_margin_report.json",
    "unique_survivor_report.json",
    "fragment_report.json",
    "factorial_metrics.json",
    "determinism_report.json",
    "performance_report.json",
    "logical_checksums.json",
    "label_schema.json",
    "sampling_plan.json",
}


class Guard02RError(RuntimeError):
    pass


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def content_hashes(root: Path, *, suffixes: tuple[str, ...] | None = None) -> dict:
    """Real content SHA-256 for every file under ``root`` (sorted)."""
    out: dict[str, str] = {}
    if root.is_file():
        return {root.name: sha256_file(root)}
    for p in sorted(root.rglob("*")):
        if p.is_file() and (suffixes is None or p.suffix in suffixes):
            out[str(p.relative_to(root))] = sha256_file(p)
    return out


def source_tree_hash(paths: list[Path]) -> dict:
    """Content hash over the experiment source tree (tracked or not),
    plus git revision and clean/dirty status."""
    files: dict[str, str] = {}
    for base in paths:
        if base.is_file():
            files[str(base)] = sha256_file(base)
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix in {".py", ".yaml", ".toml", ".md"}:
                files[str(p)] = sha256_file(p)
    combined = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()

    def _git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", *args], capture_output=True, text=True, check=True
            )
            return out.stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    status = _git("status", "--porcelain")
    return {
        "combined_sha256": combined,
        "n_files": len(files),
        "files": files,
        "git_revision": _git("rev-parse", "HEAD"),
        "git_dirty": bool(status) if status is not None else None,
    }


def environment_snapshot(argv: list[str], lock_path: Path | None) -> dict:
    import importlib.metadata as md
    import zoneinfo

    tzdata_version = None
    try:
        tzdata_version = md.version("tzdata")
    except md.PackageNotFoundError:
        tzpath = zoneinfo.TZPATH
        tzdata_version = f"system:{tzpath[0] if tzpath else 'unknown'}"
    packages = {}
    for name in ("polars", "pyyaml", "pytest", "ruff"):
        try:
            packages[name] = md.version(name)
        except md.PackageNotFoundError:
            packages[name] = None
    relevant_env = {
        k: os.environ.get(k)
        for k in ("PYTHONPATH", "TZ", "POLARS_MAX_THREADS")
        if os.environ.get(k) is not None
    }
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "packages": packages,
        "tzdata_version": tzdata_version,
        "dependency_lock_sha256": (
            sha256_file(lock_path) if lock_path and lock_path.exists() else None
        ),
        "argv": argv,
        "environment_variables": relevant_env,
    }


def _check_name(name: str) -> None:
    lowered = name.lower()
    for token in FORBIDDEN_NAME_TOKENS:
        if token in lowered:
            raise Guard02RError(f"refusing TIDES-shaped artifact name {name!r}")
    if name.endswith((".parquet", ".csv")):
        ok_prefixes = ("x_", "crossfit_", "labeling_packet_")
        if not name.startswith(ok_prefixes):
            raise Guard02RError(
                f"refusing {name!r}: tabular artifacts need an experimental "
                f"prefix {ok_prefixes}"
            )
    elif name.endswith(".json"):
        if name not in ALLOWED_JSON_NAMES02R and not name.endswith(".meta.json"):
            raise Guard02RError(f"refusing non-allowlisted report {name!r}")
    elif name.endswith(".md"):
        if name != "r1-spike02r-findings-full.md":
            raise Guard02RError(f"refusing markdown {name!r}")
    else:
        raise Guard02RError(f"unsupported artifact type {name!r}")


def check_run_dir02r(run_dir: Path) -> Path:
    resolved = run_dir.resolve()
    if IMMUTABLE_SOURCE_DIR in resolved.parts:
        raise Guard02RError(f"refusing write inside immutable source: {resolved}")
    if not resolved.name.startswith(RUN_DIR_PREFIX02R):
        raise Guard02RError(f"run dir must start with {RUN_DIR_PREFIX02R}")
    if resolved.parent.name != "artifacts":
        raise Guard02RError("run dir must sit directly under artifacts/")
    return resolved


def _restrict(path: Path) -> None:
    if path.is_dir():
        os.chmod(path, stat.S_IRWXU)
    else:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


class Spike02RRun:
    """Guarded run directory: owner-only permissions, content-hash
    bookkeeping, mandatory markers on every artifact."""

    def __init__(
        self,
        run_dir: Path,
        *,
        config_hash: str,
        source_tree: dict,
        input_hashes: dict[str, Any],
        baseline_hashes: dict[str, Any],
        environment: dict,
        experimental_parameters: dict | None = None,
    ):
        self.run_dir = check_run_dir02r(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        _restrict(self.run_dir)
        self.config_hash = config_hash
        self.source_tree = source_tree
        self.input_hashes = input_hashes
        self.baseline_hashes = baseline_hashes
        self.environment = environment
        self.experimental_parameters = experimental_parameters or {}
        self.artifacts: dict[str, dict] = {}
        self.stage_timings_s: dict[str, float] = {}
        self.started_utc = dt.datetime.now(dt.UTC).isoformat()

    def _markers(self, privacy: str) -> dict:
        return {
            **MARKERS02R,
            "experimental_algorithm_version": ALGORITHM_VERSION02R,
            "config_hash": self.config_hash,
            "source_tree_sha256": self.source_tree["combined_sha256"],
            "git_revision": self.source_tree.get("git_revision"),
            "git_dirty": self.source_tree.get("git_dirty"),
            "assumptions": ASSUMPTIONS02R,
            "unresolved_design_contracts": UNRESOLVED02R,
            "experimental_parameters": self.experimental_parameters,
            "privacy_classification": privacy,
        }

    def write_parquet(
        self,
        df: pl.DataFrame,
        name: str,
        *,
        grain: str,
        key: list[str],
        producer: str,
        privacy: str = "restricted-local",
        lineage: list[str] | None = None,
    ) -> Path:
        _check_name(name)
        path = self.run_dir / name
        df.write_parquet(path)
        _restrict(path)
        digest = sha256_file(path)
        meta = {
            **self._markers(privacy),
            "artifact": name,
            "row_grain": grain,
            "logical_key": key,
            "producer": producer,
            "rows": df.height,
            "columns": df.columns,
            "content_sha256": digest,
            "lineage": lineage or [],
        }
        meta_path = self.run_dir / f"{name}.meta.json"
        meta_path.write_text(json.dumps(meta, indent=2, default=str) + "\n")
        _restrict(meta_path)
        self.artifacts[name] = {"rows": df.height, "sha256": digest}
        return path

    def write_json(
        self, payload: dict, name: str, *, privacy: str = "restricted-local"
    ) -> Path:
        _check_name(name)
        path = self.run_dir / name
        merged = {**payload, **self._markers(privacy)}
        path.write_text(json.dumps(merged, indent=2, default=str) + "\n")
        _restrict(path)
        self.artifacts[name] = {"sha256": sha256_file(path)}
        return path

    def write_text(self, text: str, name: str) -> Path:
        _check_name(name)
        path = self.run_dir / name
        path.write_text(text)
        _restrict(path)
        self.artifacts[name] = {"sha256": sha256_file(path)}
        return path

    def finalize(self, extra: dict) -> Path:
        output_hashes = {
            n: info.get("sha256") for n, info in sorted(self.artifacts.items())
        }
        self.write_json({"outputs": output_hashes}, "output_content_hashes.json")
        payload = {
            "run_id": self.run_dir.name,
            "started_utc": self.started_utc,
            "finished_utc": dt.datetime.now(dt.UTC).isoformat(),
            "environment": self.environment,
            "input_content_hashes": self.input_hashes,
            "baseline_hashes": self.baseline_hashes,
            "stage_timings_s": {
                k: round(v, 3) for k, v in self.stage_timings_s.items()
            },
            "artifacts": self.artifacts,
            **extra,
        }
        return self.write_json(payload, "run_manifest.json")
