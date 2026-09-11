"""Fail-closed artifact writers for the human-labeling v2 run directory.

Two zones:

- the frozen protocol zone, written exactly once by ``labeling_v2_setup``
  (staging directory, atomic publish, allowlisted names, 0600 files);
- the live label zone, initialized empty by setup and updated atomically by
  the review store while the human labels. Live updates can never touch a
  frozen artifact.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

from .human_label_artifacts import canonical_json_bytes, logical_sha256
from .human_label_parser import sha256_file
from .labeling_v2_contracts import (
    FROZEN_ARTIFACTS,
    GENERATION_TIME_BASIS,
    LIVE_ARTIFACTS,
    PRODUCER,
    PROFILE,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
)


class LabelingV2ArtifactError(RuntimeError):
    """A write would violate containment, immutability, or allowlist rules."""


def _restrict_file(path: Path) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _restrict_dir(path: Path) -> None:
    os.chmod(path, stat.S_IRWXU)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def base_metadata(
    artifact: str,
    *,
    grain: str,
    key: Sequence[str],
    contains_human_labels: bool,
    privacy: str,
    logical_checksum: str,
    input_hashes: Mapping[str, str],
    source_tree_sha256: str,
    code_revision: str | None,
    generated_at: str,
    generated_time_is_wall_clock: bool = False,
    generation_time_basis: str = GENERATION_TIME_BASIS,
) -> dict[str, Any]:
    return {
        "artifact": artifact,
        "profile": PROFILE,
        "row_grain": grain,
        "logical_key": list(key),
        "producer": PRODUCER,
        "protocol_version": PROTOCOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "input_hashes": dict(sorted(input_hashes.items())),
        "code_revision": code_revision,
        "source_tree_sha256": source_tree_sha256,
        "generated_time": generated_at,
        "generation_time_basis": generation_time_basis,
        "generated_time_is_wall_clock": generated_time_is_wall_clock,
        "contains_human_labels": contains_human_labels,
        "privacy_classification": privacy,
        "logical_sha256": logical_checksum,
        "human_correctness_claimed": False,
        "calibration_run": False,
        "factorial_run": False,
        "architecture_selected": False,
        "r1_closed": False,
        "packet_is_independent_holdout": False,
    }


def write_parquet_payload(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    meta: Mapping[str, Any],
    *,
    schema: Mapping[str, pl.DataType] | None = None,
) -> tuple[int, list[str]]:
    """Write rows (possibly empty, schema-pinned) with embedded metadata."""

    if rows:
        frame = pl.DataFrame([dict(row) for row in rows], infer_schema_length=None)
        if schema is not None:
            missing = [name for name in schema if name not in frame.columns]
            for name in missing:
                frame = frame.with_columns(pl.lit(None).alias(name))
            frame = frame.select(
                [pl.col(name).cast(dtype) for name, dtype in schema.items()]
            )
    else:
        if schema is None:
            raise LabelingV2ArtifactError(
                f"empty parquet requires an explicit schema: {path.name}"
            )
        frame = pl.DataFrame(schema={name: dtype for name, dtype in schema.items()})
    table = frame.to_arrow()
    arrow_meta = dict(table.schema.metadata or {})
    arrow_meta[b"r1spike.labeling_v2.metadata"] = canonical_json_bytes(meta)
    table = table.replace_schema_metadata(arrow_meta)
    pq.write_table(
        table, path, compression="zstd", version="2.6", write_statistics=True
    )
    _restrict_file(path)
    return frame.height, frame.columns


def write_sidecar(
    path: Path,
    meta: Mapping[str, Any],
    *,
    rows,
    columns,
    content_path: Path | None = None,
) -> Path:
    # content_path lets live rewrites hash the staged temp file: the
    # sidecar must describe the bytes about to land at `path`, not the
    # version being replaced.
    sidecar = path.with_name(path.name + ".meta.json")
    payload = {
        **dict(meta),
        "content_sha256": sha256_file(content_path or path),
        "rows": rows,
        "columns": list(columns) if columns is not None else None,
        "metadata_document": True,
    }
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _restrict_file(sidecar)
    return sidecar


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


def write_csv_payload(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
) -> int:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: _csv_value(row.get(col)) for col in columns})
    _restrict_file(path)
    return len(rows)


class V2RunWriter:
    """Stage the complete run01 directory and atomically publish it."""

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
        self._validate_target(tuple(p.resolve() for p in forbidden_paths))
        if self.output_dir.exists():
            raise LabelingV2ArtifactError(
                f"refusing to overwrite existing run directory: {self.output_dir}"
            )
        self.output_dir.parent.mkdir(parents=True, exist_ok=True)
        temp = tempfile.mkdtemp(
            prefix=f".{self.output_dir.name}.staging-", dir=self.output_dir.parent
        )
        self.staging_dir = Path(temp)
        _restrict_dir(self.staging_dir)
        self.input_hashes = dict(sorted(input_hashes.items()))
        self.source_tree_sha256 = source_tree_sha256
        self.code_revision = code_revision
        self.generated_at = generated_at
        self.artifacts: dict[str, dict[str, Any]] = {}

    def _validate_target(self, forbidden_paths: Sequence[Path]) -> None:
        lowered = {part.lower() for part in self.output_dir.parts}
        if "database_offline" in lowered or "ttc_tides" in lowered:
            raise LabelingV2ArtifactError(
                f"run output cannot live in source/production paths: {self.output_dir}"
            )
        for source in forbidden_paths:
            if self.output_dir == source or _is_relative_to(self.output_dir, source):
                raise LabelingV2ArtifactError(
                    f"run output cannot be inside an input path: {source}"
                )
            if _is_relative_to(source, self.output_dir):
                raise LabelingV2ArtifactError(
                    f"run output cannot contain an input path: {source}"
                )

    def _check_name(self, name: str) -> None:
        if "/" in name or Path(name).name != name:
            raise LabelingV2ArtifactError(f"invalid artifact name: {name}")
        if name not in FROZEN_ARTIFACTS and name not in LIVE_ARTIFACTS:
            raise LabelingV2ArtifactError(f"artifact name is not allowlisted: {name}")

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
        return base_metadata(
            artifact,
            grain=grain,
            key=key,
            contains_human_labels=contains_human_labels,
            privacy=privacy,
            logical_checksum=logical_checksum,
            input_hashes=self.input_hashes,
            source_tree_sha256=self.source_tree_sha256,
            code_revision=self.code_revision,
            generated_at=self.generated_at,
        )

    def write_parquet(
        self,
        rows: Sequence[Mapping[str, Any]],
        name: str,
        *,
        grain: str,
        key: Sequence[str],
        contains_human_labels: bool,
        privacy: str,
        schema: Mapping[str, pl.DataType] | None = None,
    ) -> Path:
        self._check_name(name)
        payload = [dict(row) for row in rows]
        meta = self.metadata(
            name,
            grain=grain,
            key=key,
            contains_human_labels=contains_human_labels,
            privacy=privacy,
            logical_checksum=logical_sha256(payload),
        )
        path = self.staging_dir / name
        n_rows, columns = write_parquet_payload(path, payload, meta, schema=schema)
        write_sidecar(path, meta, rows=n_rows, columns=columns)
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
        columns: Sequence[str],
    ) -> Path:
        self._check_name(name)
        payload = [dict(row) for row in rows]
        meta = self.metadata(
            name,
            grain=grain,
            key=key,
            contains_human_labels=contains_human_labels,
            privacy=privacy,
            logical_checksum=logical_sha256(payload),
        )
        path = self.staging_dir / name
        write_csv_payload(path, payload, columns)
        write_sidecar(path, meta, rows=len(payload), columns=columns)
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
        self._check_name(name)
        meta = self.metadata(
            name,
            grain=grain,
            key=key,
            contains_human_labels=contains_human_labels,
            privacy=privacy,
            logical_checksum=logical_sha256(payload),
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
        _restrict_file(path)
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
        self._check_name(name)
        meta = self.metadata(
            name,
            grain=grain,
            key=key,
            contains_human_labels=contains_human_labels,
            privacy=privacy,
            logical_checksum=logical_sha256(body),
        )
        prefix = (
            "<!-- artifact-metadata\n"
            + json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n-->\n\n"
        )
        path = self.staging_dir / name
        path.write_text(prefix + body.rstrip() + "\n", encoding="utf-8")
        _restrict_file(path)
        self._record(path, meta)
        return path

    def write_jsonl(self, name: str, lines: Sequence[Mapping[str, Any]]) -> Path:
        """Append-only event log; created empty (or seeded) at setup."""

        self._check_name(name)
        path = self.staging_dir / name
        with path.open("w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(
                    json.dumps(line, ensure_ascii=False, sort_keys=True, default=str)
                    + "\n"
                )
        _restrict_file(path)
        meta = self.metadata(
            name,
            grain="one append-only review event per line",
            key=["event_index"],
            contains_human_labels=True,
            privacy="restricted-local",
            logical_checksum=logical_sha256([dict(line) for line in lines]),
        )
        self._record(path, meta)
        return path

    def finalize(self) -> Path:
        present = {
            path.relative_to(self.staging_dir).as_posix()
            for path in self.staging_dir.rglob("*")
            if path.is_file() and not path.name.endswith(".meta.json")
        }
        required = set(FROZEN_ARTIFACTS) | set(LIVE_ARTIFACTS)
        missing = required - present
        if missing:
            raise LabelingV2ArtifactError(
                f"run is incomplete; missing required artifacts: {sorted(missing)}"
            )
        os.replace(self.staging_dir, self.output_dir)
        _restrict_dir(self.output_dir)
        return self.output_dir

    def abort(self) -> None:
        if self.staging_dir.exists():
            shutil.rmtree(self.staging_dir)

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


class LiveArtifactUpdater:
    """Atomic in-place updates to the live label zone of a published run."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir).resolve()
        if not self.run_dir.is_dir():
            raise LabelingV2ArtifactError(f"run directory missing: {self.run_dir}")

    def _check_live(self, name: str) -> Path:
        if name in FROZEN_ARTIFACTS:
            raise LabelingV2ArtifactError(
                f"refusing to modify frozen protocol artifact: {name}"
            )
        if name not in LIVE_ARTIFACTS:
            raise LabelingV2ArtifactError(f"artifact name is not allowlisted: {name}")
        return self.run_dir / name

    def _atomic_write(self, path: Path, write_fn) -> None:
        handle, temp_name = tempfile.mkstemp(dir=self.run_dir, prefix=f".{path.name}.")
        os.close(handle)
        temp = Path(temp_name)
        try:
            write_fn(temp)
            _restrict_file(temp)
            os.replace(temp, path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def append_jsonl(self, name: str, event: Mapping[str, Any]) -> None:
        path = self._check_live(name)
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        _restrict_file(path)

    def rewrite_parquet(
        self,
        name: str,
        rows: Sequence[Mapping[str, Any]],
        meta: Mapping[str, Any],
        *,
        schema: Mapping[str, pl.DataType] | None = None,
    ) -> None:
        path = self._check_live(name)

        def _write(temp: Path) -> None:
            n_rows, columns = write_parquet_payload(temp, rows, meta, schema=schema)
            write_sidecar(path, meta, rows=n_rows, columns=columns, content_path=temp)

        self._atomic_write(path, _write)

    def rewrite_csv(
        self,
        name: str,
        rows: Sequence[Mapping[str, Any]],
        columns: Sequence[str],
        meta: Mapping[str, Any],
    ) -> None:
        path = self._check_live(name)

        def _write(temp: Path) -> None:
            write_csv_payload(temp, [dict(r) for r in rows], columns)
            write_sidecar(
                path, meta, rows=len(rows), columns=columns, content_path=temp
            )

        self._atomic_write(path, _write)

    def rewrite_json(self, name: str, payload: Mapping[str, Any], meta) -> None:
        path = self._check_live(name)
        merged = {"_artifact_metadata": dict(meta), **dict(payload)}

        def _write(temp: Path) -> None:
            temp.write_text(
                json.dumps(
                    merged, ensure_ascii=False, indent=2, sort_keys=True, default=str
                )
                + "\n",
                encoding="utf-8",
            )

        self._atomic_write(path, _write)
