"""Guarded artifact output for the spike. Fails closed on publication.

Every write goes through :func:`SpikeRun.write_parquet` /
:func:`SpikeRun.write_json`, which refuse:

- any output outside an ``artifacts/experimental-r1-spike-*`` run
  directory;
- any path containing a ``database_offline`` component (immutable source
  boundary, same rule as the production pipeline);
- any file whose name resembles a TIDES resource or data package
  (``vehicles``, ``vehicle_locations``, ``trips_performed``,
  ``stop_visits``, ``passenger_events``, ``datapackage``) — the spike
  must be structurally unable to emit a TIDES-shaped artifact;
- any artifact name not starting with the experimental ``x_`` prefix
  (JSON reports and the manifest excepted via an explicit allowlist).

Every artifact gets a sidecar ``<name>.meta.json`` carrying the
experimental profile markers, hashes, grain, key, and assumptions.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import polars as pl

from . import (
    ALGORITHM_VERSION,
    AUTHORIZATION_BASIS,
    PROFILE,
    PUBLISHABLE,
    TIDES_CONFORMANCE_CLAIMED,
)

RUN_DIR_PREFIX = "experimental-r1-spike-"
IMMUTABLE_SOURCE_DIR = "database_offline"

FORBIDDEN_NAME_TOKENS = (
    "vehicles",
    "vehicle_locations",
    "trips_performed",
    "stop_visits",
    "passenger_events",
    "datapackage",
    "tides",
)

ALLOWED_JSON_NAMES = {
    "run_manifest.json",
    "experimental_score_contract.json",
    "segment_variant_report.json",
    "metrics.json",
    "determinism_report.json",
    "performance_report.json",
    "stage1_policy_report.json",
    "window_order_report.json",
    "baseline_report.json",
    "sensitivity_report.json",
}


class PublicationGuardError(RuntimeError):
    """A code path attempted a write the spike must never perform."""


def _check_name(name: str) -> None:
    lowered = name.lower()
    for token in FORBIDDEN_NAME_TOKENS:
        if token in lowered:
            raise PublicationGuardError(
                f"refusing to write {name!r}: name matches a TIDES "
                "resource/publication token; this spike is non-publishable"
            )
    if name.endswith(".parquet") or name.endswith(".csv"):
        if not name.startswith("x_"):
            raise PublicationGuardError(
                f"refusing to write {name!r}: spike tabular artifacts "
                "must carry the experimental x_ prefix"
            )
    elif name.endswith(".json"):
        if name not in ALLOWED_JSON_NAMES and not name.endswith(".meta.json"):
            raise PublicationGuardError(
                f"refusing to write {name!r}: not an allowlisted spike report"
            )
    else:
        raise PublicationGuardError(
            f"refusing to write {name!r}: unsupported artifact type"
        )


def check_run_dir(run_dir: Path) -> Path:
    resolved = run_dir.resolve()
    for part in resolved.parts:
        if part == IMMUTABLE_SOURCE_DIR:
            raise PublicationGuardError(
                "refusing to write inside the immutable source-data "
                f"boundary: {resolved}"
            )
    if not resolved.name.startswith(RUN_DIR_PREFIX):
        raise PublicationGuardError(
            f"spike outputs must live under an {RUN_DIR_PREFIX}* run "
            f"directory, got {resolved}"
        )
    if resolved.parent.name != "artifacts":
        raise PublicationGuardError(
            f"spike run directory must sit directly under artifacts/, got {resolved}"
        )
    return resolved


def _git_revision(start: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=start,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


class SpikeRun:
    """One guarded spike run directory with manifest bookkeeping."""

    def __init__(
        self,
        run_dir: Path,
        *,
        config_hash: str,
        contract_hash: str,
        input_hashes: dict[str, str],
        assumptions: list[str],
        unresolved_design_contracts: list[str],
    ):
        self.run_dir = check_run_dir(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.config_hash = config_hash
        self.contract_hash = contract_hash
        self.input_hashes = input_hashes
        self.assumptions = assumptions
        self.unresolved_design_contracts = unresolved_design_contracts
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.started_utc = dt.datetime.now(dt.UTC).isoformat()

    def _markers(self) -> dict[str, Any]:
        return {
            "profile": PROFILE,
            "publishable": PUBLISHABLE,
            "tides_conformance_claimed": TIDES_CONFORMANCE_CLAIMED,
            "authorization_basis": AUTHORIZATION_BASIS,
            "experimental_algorithm_version": ALGORITHM_VERSION,
            "config_hash": self.config_hash,
            "score_contract_hash": self.contract_hash,
            "input_hashes": self.input_hashes,
            "assumptions": self.assumptions,
            "unresolved_design_contracts": self.unresolved_design_contracts,
            "code_revision": _git_revision(self.run_dir.parent),
        }

    def write_parquet(
        self,
        df: pl.DataFrame,
        name: str,
        *,
        grain: str,
        key: list[str],
        schema_version: int = 1,
    ) -> Path:
        _check_name(name)
        path = self.run_dir / name
        df.write_parquet(path)
        meta = {
            **self._markers(),
            "artifact": name,
            "row_grain": grain,
            "key": key,
            "schema_version": schema_version,
            "rows": df.height,
            "columns": df.columns,
        }
        meta_path = self.run_dir / f"{name}.meta.json"
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        self.artifacts[name] = {"rows": df.height, "grain": grain}
        return path

    def write_json(self, payload: dict[str, Any], name: str) -> Path:
        _check_name(name)
        path = self.run_dir / name
        merged = {**self._markers(), **payload}
        path.write_text(
            json.dumps(merged, indent=2, default=str) + "\n", encoding="utf-8"
        )
        self.artifacts[name] = {"kind": "json"}
        return path

    def write_manifest(self, extra: dict[str, Any]) -> Path:
        payload = {
            "run_id": self.run_dir.name,
            "started_utc": self.started_utc,
            "finished_utc": dt.datetime.now(dt.UTC).isoformat(),
            "artifacts": self.artifacts,
            **extra,
        }
        return self.write_json(payload, "run_manifest.json")


def hash_config_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
