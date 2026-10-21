"""Configuration resolution for the v2 blind labeling UI.

Precedence: CLI args after ``--`` > ``LABELING_V2_*`` environment variables >
repo-layout defaults. The resolved config is pinned into session state by the
app so a regenerated run directory cannot silently swap mid-review.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTS = APP_DIR.parents[2] / "artifacts"
RUN_DIR_NAME = "human-labeling-v2-run01"


@dataclass(frozen=True)
class AppConfig:
    run_dir: Path
    artifacts_root: Path
    audit_dir: Path
    spike02r_dir: Path | None
    reviewer: str
    notes: tuple[str, ...] = ()


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def resolve_config(argv: list[str] | None = None) -> AppConfig:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--artifacts", type=Path, default=None)
    parser.add_argument("--audit-dir", type=Path, default=None)
    parser.add_argument("--spike02r-dir", type=Path, default=None)
    parser.add_argument("--reviewer", type=str, default=None)
    args, _ = parser.parse_known_args(argv)

    notes: list[str] = []
    env_artifacts = _env("LABELING_V2_ARTIFACTS")
    env_run_dir = _env("LABELING_V2_RUN_DIR")
    env_audit = _env("LABELING_V2_AUDIT_DIR")
    artifacts_root = (
        args.artifacts
        or (Path(env_artifacts) if env_artifacts else None)
        or DEFAULT_ARTIFACTS
    ).resolve()
    run_dir = (
        args.run_dir
        or (Path(env_run_dir) if env_run_dir else None)
        or artifacts_root / RUN_DIR_NAME
    ).resolve()
    audit_dir = (
        args.audit_dir
        or (Path(env_audit) if env_audit else None)
        or artifacts_root / "human-label-audit-v1"
    ).resolve()
    spike02r_env = _env("LABELING_V2_SPIKE02R_DIR")
    spike02r_dir = (
        args.spike02r_dir.resolve()
        if args.spike02r_dir
        else Path(spike02r_env).resolve()
        if spike02r_env
        else None
    )
    reviewer = args.reviewer or _env("LABELING_V2_REVIEWER") or ""

    if not run_dir.exists():
        notes.append(
            f"Run directory not found: {run_dir} — generate it from the ttc "
            "root with `uv run python -m r1spike.run_labeling_v2_setup`"
        )
    if not audit_dir.exists():
        notes.append(f"Audit directory not found: {audit_dir}")
    return AppConfig(
        run_dir=run_dir,
        artifacts_root=artifacts_root,
        audit_dir=audit_dir,
        spike02r_dir=spike02r_dir,
        reviewer=reviewer,
        notes=tuple(notes),
    )
