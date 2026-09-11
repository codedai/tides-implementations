# ruff: noqa: E501
"""Fail-closed, owner-only artifact writer for the human-label audit."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

from .human_label_parser import sha256_file

PRODUCER = "r1spike.run_human_label_audit"
PROFILE = "diagnostic-experimental-r1-human-label-audit-v1"
SCHEMA_VERSION = "human-label-audit-v1"

REQUIRED_ARTIFACTS = {
    "first_pass_labels_v1.parquet",
    "label_case_inventory.csv",
    "label_audit_cases.csv",
    "label_audit_summary.json",
    "label_readiness_report.json",
    "adjudication_queue_v2.csv",
    "child_case_requests.csv",
    "proposed_label_schema_v2.json",
    "human_label_audit_findings.md",
    "possible_next_steps.md",
    "run_manifest.json",
    "input_hashes.json",
    "logical_checksums.json",
}


class AuditArtifactError(RuntimeError):
    """A write would violate containment or reproducibility rules."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def logical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def git_revision(start: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=start,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def source_tree_hash(root: Path) -> tuple[str, dict[str, str]]:
    """Hash experiment source/config/docs using portable sorted paths."""

    suffixes = {".py", ".yaml", ".toml", ".md"}
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if (
            path.is_file()
            and path.suffix in suffixes
            and not {"__pycache__", ".pytest_cache", ".ruff_cache"}.intersection(
                path.parts
            )
        ):
            files[path.relative_to(root).as_posix()] = sha256_file(path)
    return logical_sha256(files), files


class AuditArtifactWriter:
    """Stage a complete audit and atomically publish it to a new directory."""

    def __init__(
        self,
        output_dir: Path,
        *,
        input_hashes: Mapping[str, str],
        source_tree_sha256: str,
        code_revision: str | None,
        generated_at: str,
        forbidden_paths: Iterable[Path],
    ):
        self.output_dir = output_dir.resolve()
        self._validate_target(tuple(path.resolve() for path in forbidden_paths))
        if self.output_dir.exists():
            raise AuditArtifactError(
                f"refusing to overwrite existing audit output: {self.output_dir}"
            )
        self.output_dir.parent.mkdir(parents=True, exist_ok=True)
        temp = tempfile.mkdtemp(
            prefix=f".{self.output_dir.name}.staging-", dir=self.output_dir.parent
        )
        self.staging_dir = Path(temp)
        os.chmod(self.staging_dir, stat.S_IRWXU)
        self.input_hashes = dict(sorted(input_hashes.items()))
        self.source_tree_sha256 = source_tree_sha256
        self.code_revision = code_revision
        self.generated_at = generated_at
        self.artifacts: dict[str, dict[str, Any]] = {}

    def _validate_target(self, forbidden_paths: Sequence[Path]) -> None:
        lowered_parts = {part.lower() for part in self.output_dir.parts}
        if "database_offline" in lowered_parts or "ttc_tides" in lowered_parts:
            raise AuditArtifactError(
                f"audit output cannot be inside source/production paths: {self.output_dir}"
            )
        for source in forbidden_paths:
            if self.output_dir == source or _is_relative_to(self.output_dir, source):
                raise AuditArtifactError(
                    f"audit output cannot be inside an input path: {source}"
                )
            if _is_relative_to(source, self.output_dir):
                raise AuditArtifactError(
                    f"audit output cannot contain an input path: {source}"
                )

    def metadata(
        self,
        artifact: str,
        *,
        grain: str,
        key: Sequence[str],
        contains_human_labels: bool,
        privacy: str,
        logical_checksum: str,
    ) -> dict[str, Any]:
        return {
            "artifact": artifact,
            "profile": PROFILE,
            "row_grain": grain,
            "logical_key": list(key),
            "producer": PRODUCER,
            "input_hashes": self.input_hashes,
            "code_revision": self.code_revision,
            "source_tree_sha256": self.source_tree_sha256,
            "schema_version": SCHEMA_VERSION,
            "generated_time": self.generated_at,
            "generation_time_basis": (
                "latest_first_pass_review_date_at_utc_midnight_for_reproducibility"
            ),
            "generated_time_is_wall_clock": False,
            "contains_human_labels": contains_human_labels,
            "privacy_classification": privacy,
            "logical_sha256": logical_checksum,
            "audit_only": True,
            "human_correctness_claimed": False,
            "calibration_run": False,
            "factorial_run": False,
            "architecture_selected": False,
            "r1_closed": False,
        }

    def write_parquet(
        self,
        rows: Sequence[Mapping[str, Any]],
        name: str,
        *,
        grain: str,
        key: Sequence[str],
        contains_human_labels: bool,
        privacy: str,
    ) -> Path:
        self._check_name(name, ".parquet")
        payload = [dict(row) for row in rows]
        logical = logical_sha256(payload)
        meta = self.metadata(
            name,
            grain=grain,
            key=key,
            contains_human_labels=contains_human_labels,
            privacy=privacy,
            logical_checksum=logical,
        )
        path = self.staging_dir / name
        frame = pl.DataFrame(payload, infer_schema_length=None)
        table = frame.to_arrow()
        arrow_meta = dict(table.schema.metadata or {})
        arrow_meta[b"r1spike.audit.metadata"] = canonical_json_bytes(meta)
        table = table.replace_schema_metadata(arrow_meta)
        pq.write_table(
            table,
            path,
            compression="zstd",
            version="2.6",
            write_statistics=True,
        )
        self._restrict(path)
        self._write_sidecar(path, meta, rows=frame.height, columns=frame.columns)
        self._record(path, meta)
        return path

    def write_csv(
        self,
        rows: Sequence[Mapping[str, Any]],
        name: str,
        *,
        grain: str,
        key: Sequence[str],
        contains_human_labels: bool,
        privacy: str,
        columns: Sequence[str] | None = None,
    ) -> Path:
        self._check_name(name, ".csv")
        payload = [dict(row) for row in rows]
        fieldnames = list(columns or (payload[0].keys() if payload else key))
        logical = logical_sha256(payload)
        meta = self.metadata(
            name,
            grain=grain,
            key=key,
            contains_human_labels=contains_human_labels,
            privacy=privacy,
            logical_checksum=logical,
        )
        path = self.staging_dir / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            for row in payload:
                writer.writerow(
                    {column: _csv_value(row.get(column)) for column in fieldnames}
                )
        self._restrict(path)
        self._write_sidecar(path, meta, rows=len(payload), columns=fieldnames)
        self._record(path, meta)
        return path

    def write_json(
        self,
        payload: Mapping[str, Any],
        name: str,
        *,
        grain: str,
        key: Sequence[str],
        contains_human_labels: bool,
        privacy: str,
    ) -> Path:
        self._check_name(name, ".json")
        logical = logical_sha256(payload)
        meta = self.metadata(
            name,
            grain=grain,
            key=key,
            contains_human_labels=contains_human_labels,
            privacy=privacy,
            logical_checksum=logical,
        )
        merged = {"_artifact_metadata": meta, **dict(payload)}
        path = self.staging_dir / name
        path.write_text(
            json.dumps(
                merged, ensure_ascii=False, indent=2, sort_keys=True, default=str
            )
            + "\n",
            encoding="utf-8",
        )
        self._restrict(path)
        self._record(path, meta)
        return path

    def write_markdown(
        self,
        body: str,
        name: str,
        *,
        grain: str,
        key: Sequence[str],
        contains_human_labels: bool,
        privacy: str,
    ) -> Path:
        self._check_name(name, ".md")
        logical = logical_sha256(body)
        meta = self.metadata(
            name,
            grain=grain,
            key=key,
            contains_human_labels=contains_human_labels,
            privacy=privacy,
            logical_checksum=logical,
        )
        prefix = (
            "<!-- artifact-metadata\n"
            + json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n-->\n\n"
        )
        path = self.staging_dir / name
        path.write_text(prefix + body.rstrip() + "\n", encoding="utf-8")
        self._restrict(path)
        self._record(path, meta)
        return path

    def write_svg(
        self,
        svg_body: str,
        name: str,
        *,
        grain: str,
        key: Sequence[str],
    ) -> Path:
        if not name.startswith("figures/") or not name.endswith(".svg"):
            raise AuditArtifactError(f"invalid figure path: {name}")
        logical = logical_sha256(svg_body)
        meta = self.metadata(
            name,
            grain=grain,
            key=key,
            contains_human_labels=True,
            privacy="repository-safe-derived",
            logical_checksum=logical,
        )
        path = self.staging_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata_element = (
            "<metadata>"
            + html.escape(json.dumps(meta, ensure_ascii=False, sort_keys=True))
            + "</metadata>"
        )
        if "<svg" not in svg_body:
            raise AuditArtifactError(f"figure is not SVG: {name}")
        insertion = svg_body.find(">", svg_body.find("<svg")) + 1
        rendered = svg_body[:insertion] + metadata_element + svg_body[insertion:]
        path.write_text(rendered.rstrip() + "\n", encoding="utf-8")
        self._restrict(path)
        self._write_sidecar(path, meta)
        self._record(path, meta)
        return path

    def finalize(self) -> Path:
        present = {
            path.relative_to(self.staging_dir).as_posix()
            for path in self.staging_dir.rglob("*")
            if path.is_file()
        }
        missing = REQUIRED_ARTIFACTS - present
        if missing:
            raise AuditArtifactError(
                f"audit is incomplete; missing required artifacts: {sorted(missing)}"
            )
        os.replace(self.staging_dir, self.output_dir)
        os.chmod(self.output_dir, stat.S_IRWXU)
        return self.output_dir

    def abort(self) -> None:
        if self.staging_dir.exists():
            shutil.rmtree(self.staging_dir)

    def _write_sidecar(
        self,
        path: Path,
        meta: Mapping[str, Any],
        *,
        rows: int | None = None,
        columns: Sequence[str] | None = None,
    ) -> None:
        sidecar = path.with_name(path.name + ".meta.json")
        payload = {
            **dict(meta),
            "content_sha256": sha256_file(path),
            "rows": rows,
            "columns": list(columns) if columns is not None else None,
            "metadata_document": True,
        }
        sidecar.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._restrict(sidecar)

    def _record(self, path: Path, meta: Mapping[str, Any]) -> None:
        name = path.relative_to(self.staging_dir).as_posix()
        self.artifacts[name] = {
            "sha256": sha256_file(path),
            "logical_sha256": meta["logical_sha256"],
            "grain": meta["row_grain"],
            "key": meta["logical_key"],
            "contains_human_labels": meta["contains_human_labels"],
            "privacy_classification": meta["privacy_classification"],
        }

    @staticmethod
    def _restrict(path: Path) -> None:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    @staticmethod
    def _check_name(name: str, suffix: str) -> None:
        if "/" in name or Path(name).name != name or not name.endswith(suffix):
            raise AuditArtifactError(f"invalid artifact name: {name}")
        allowed = REQUIRED_ARTIFACTS | {
            item + ".meta.json"
            for item in REQUIRED_ARTIFACTS
            if item.endswith((".csv", ".parquet"))
        }
        if name not in allowed:
            raise AuditArtifactError(f"artifact name is not allowlisted: {name}")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple, set)):
        return "|".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return value
