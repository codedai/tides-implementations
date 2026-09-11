"""Guarded artifact output for Spike 02. Fails closed on publication.

Same fail-closed posture as Spike 01's ``artifacts_io`` (which is left
untouched so the frozen ``r1c`` baseline can be re-run), with Spike-02
run directories (``artifacts/experimental-r1-spike02-*``), Spike-02
profile markers, and the Spike-02 report allowlist.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import polars as pl

from .spike02_contracts import (
    ALGORITHM_VERSION02,
    AUTHORIZATION_BASIS02,
    NORMATIVE_CONTRACT_CLAIMED02,
    PRODUCTION_MODULES_MODIFIED02,
    PROFILE02,
    PUBLISHABLE02,
    R1_CLOSED02,
    TIDES_CONFORMANCE_CLAIMED02,
)

RUN_DIR_PREFIX02 = "experimental-r1-spike02-"
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

ALLOWED_JSON_NAMES02 = {
    "run_manifest.json",
    "experiment_matrix.json",
    "variant_metrics.json",
    "threshold_sensitivity.json",
    "ablation_report.json",
    "determinism_report.json",
    "performance_report.json",
    "logical_checksums.json",
    "baseline_reproduction.json",
    "wilson_case_report.json",
    "edge_case_packet.json",
    "spike02_score_contract.json",
}


class PublicationGuardError02(RuntimeError):
    """A code path attempted a write Spike 02 must never perform."""


def _check_name(name: str) -> None:
    lowered = name.lower()
    for token in FORBIDDEN_NAME_TOKENS:
        if token in lowered:
            raise PublicationGuardError02(
                f"refusing to write {name!r}: name matches a TIDES "
                "resource/publication token; Spike 02 is non-publishable"
            )
    if name.endswith(".parquet") or name.endswith(".csv"):
        if not name.startswith("x_"):
            raise PublicationGuardError02(
                f"refusing to write {name!r}: Spike-02 tabular artifacts "
                "must carry the experimental x_ prefix"
            )
    elif name.endswith(".json"):
        if name not in ALLOWED_JSON_NAMES02 and not name.endswith(".meta.json"):
            raise PublicationGuardError02(
                f"refusing to write {name!r}: not an allowlisted Spike-02 report"
            )
    else:
        raise PublicationGuardError02(
            f"refusing to write {name!r}: unsupported artifact type"
        )


def check_run_dir02(run_dir: Path) -> Path:
    resolved = run_dir.resolve()
    for part in resolved.parts:
        if part == IMMUTABLE_SOURCE_DIR:
            raise PublicationGuardError02(
                "refusing to write inside the immutable source-data "
                f"boundary: {resolved}"
            )
    if not resolved.name.startswith(RUN_DIR_PREFIX02):
        raise PublicationGuardError02(
            f"Spike-02 outputs must live under an {RUN_DIR_PREFIX02}* run "
            f"directory, got {resolved}"
        )
    if resolved.parent.name != "artifacts":
        raise PublicationGuardError02(
            f"Spike-02 run directory must sit directly under artifacts/, got {resolved}"
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


class Spike02Run:
    """One guarded Spike-02 run directory with manifest bookkeeping."""

    def __init__(
        self,
        run_dir: Path,
        *,
        config_hash: str,
        contract_hash: str,
        input_hashes: dict[str, str],
        assumptions: list[str],
        unresolved_design_contracts: list[str],
        experimental_thresholds: dict[str, Any] | None = None,
    ):
        self.run_dir = check_run_dir02(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.config_hash = config_hash
        self.contract_hash = contract_hash
        self.input_hashes = input_hashes
        self.assumptions = assumptions
        self.unresolved_design_contracts = unresolved_design_contracts
        self.experimental_thresholds = experimental_thresholds or {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.started_utc = dt.datetime.now(dt.UTC).isoformat()

    def _markers(self) -> dict[str, Any]:
        return {
            "profile": PROFILE02,
            "publishable": PUBLISHABLE02,
            "tides_conformance_claimed": TIDES_CONFORMANCE_CLAIMED02,
            "normative_contract_claimed": NORMATIVE_CONTRACT_CLAIMED02,
            "r1_closed": R1_CLOSED02,
            "production_modules_modified": PRODUCTION_MODULES_MODIFIED02,
            "authorization_basis": AUTHORIZATION_BASIS02,
            "experimental_algorithm_version": ALGORITHM_VERSION02,
            "config_hash": self.config_hash,
            "score_contract_hash": self.contract_hash,
            "input_hashes": self.input_hashes,
            "assumptions": self.assumptions,
            "experimental_thresholds": self.experimental_thresholds,
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
        producer: str,
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
            "producer": producer,
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
        # Markers merge LAST so no payload key can shadow a safety marker.
        merged = {**payload, **self._markers()}
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
