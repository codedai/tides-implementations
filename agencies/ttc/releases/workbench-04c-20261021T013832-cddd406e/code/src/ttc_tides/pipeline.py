"""Provisional diagnostic stage runner: S0 preflight + S1/S2 normalization.

Authorized by ADR 0002 as provisional and non-publishable: outputs carry the
``diagnostic-provisional`` profile marker, claim no TIDES conformance, and may
be invalidated by the merge-plan re-review. No S3+ stage exists here.
"""

from __future__ import annotations

import datetime as dt
import json
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from . import __version__
from .ingest.apc import inventory_apc_file
from .ingest.avl import inventory_avl_file
from .ingest.sources import FatalSourceError, SourceFile
from .normalize.apc import normalize_apc
from .normalize.avl import normalize_avl
from .normalize.common import NormalizedSource

PROFILE = "diagnostic-provisional"
PROFILE_NOTE = (
    "S0-S2 diagnostic output authorized by ADR 0002; non-publishable; "
    "no TIDES conformance is claimed; S3+ is frozen pending re-review"
)

IMMUTABLE_SOURCE_DIR = "database_offline"


@dataclass
class DiagnosticResult:
    out_dir: Path
    manifest: dict[str, Any]
    sources: list[NormalizedSource]


def _git_revision(project_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def check_output_path(out_dir: Path, *, force: bool) -> None:
    resolved = out_dir.resolve()
    for parent in (resolved, *resolved.parents):
        if parent.name == IMMUTABLE_SOURCE_DIR:
            raise FatalSourceError(
                f"refusing to write inside the immutable source-data boundary: "
                f"{out_dir}"
            )
    if resolved.exists() and not force:
        raise FatalSourceError(
            f"refusing to overwrite existing run directory {out_dir}; pass --force"
        )


def _write_partitioned(df: pl.DataFrame, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    df.write_parquet(target, partition_by=["source_calendar_date", "vehicle_bucket"])


def run_diagnostic(
    *,
    avl_path: Path | None,
    apc_path: Path | None,
    out_dir: Path,
    force: bool = False,
    ingest_run_id: str | None = None,
) -> DiagnosticResult:
    """Run S0 preflight plus S1/S2 for the requested sources."""
    if avl_path is None and apc_path is None:
        raise FatalSourceError("nothing to do: pass an AVL and/or APC source file")
    check_output_path(out_dir, force=force)

    started = dt.datetime.now(dt.UTC)
    run_id = ingest_run_id or out_dir.name

    # S0: inventory and hash inputs before any processing.
    inventories: list[SourceFile] = []
    if avl_path is not None:
        inventories.append(inventory_avl_file(avl_path))
    if apc_path is not None:
        inventories.append(inventory_apc_file(apc_path))

    out_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = out_dir / "reports"
    rejections_dir = out_dir / "rejections"
    reports_dir.mkdir(exist_ok=True)
    rejections_dir.mkdir(exist_ok=True)

    results: list[NormalizedSource] = []
    for source in inventories:
        if source.source_type == "avl":
            result = normalize_avl(source, ingest_run_id=run_id)
        else:
            result = normalize_apc(source, ingest_run_id=run_id)
        _write_partitioned(result.accepted, out_dir / "normalized" / source.source_type)
        if result.rejected.height:
            result.rejected.with_columns(
                pl.col("_rejection_reasons").list.join("|")
            ).write_parquet(rejections_dir / f"{source.source_type}.parquet")
        (reports_dir / f"{source.source_type}_quality_report.json").write_text(
            json.dumps(result.report(), indent=2, default=str)
        )
        results.append(result)

    finished = dt.datetime.now(dt.UTC)
    project_root = Path(__file__).resolve().parents[2]
    manifest = {
        "profile": PROFILE,
        "profile_note": PROFILE_NOTE,
        "run_id": run_id,
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "code_version": __version__,
        "git_revision": _git_revision(project_root),
        "python_version": platform.python_version(),
        "polars_version": pl.__version__,
        "platform": platform.platform(),
        "inputs": [
            {
                "source_type": s.source_type,
                "path": str(s.path),
                "sha256": s.sha256,
                "size_bytes": s.size_bytes,
            }
            for s in inventories
        ],
        "configuration": {},
        "sources": {r.source.source_type: r.report() for r in results},
    }
    (out_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str)
    )
    return DiagnosticResult(out_dir=out_dir, manifest=manifest, sources=results)


def main_summary(result: DiagnosticResult) -> str:
    lines = [f"profile: {PROFILE} ({PROFILE_NOTE})"]
    for src in result.sources:
        lines.append(
            f"{src.source.source_type}: {src.landing_rows} landed, "
            f"{src.accepted.height} accepted, {src.rejected.height} rejected; "
            f"flags: { {k: v for k, v in src.flag_counts.items() if v} }"
        )
    lines.append(f"artifacts: {result.out_dir}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    sys.exit("use the ttc-tides CLI")
