"""Fixtures for the v2 UI tests: a hermetic run01 built from frozen inputs."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from uiv2.bootstrap import ensure_r1spike_importable  # noqa: E402

ensure_r1spike_importable()

TTC_ROOT = APP_DIR.parents[2]
ARTIFACTS = TTC_ROOT / "artifacts"
AUDIT_DIR = ARTIFACTS / "human-label-audit-v1"
P4_DIR = ARTIFACTS / "experimental-r1-spike02r-p4"
NORMALIZED = ARTIFACTS / "diagnostic-2026-08-06" / "normalized"

requires_artifacts = pytest.mark.skipif(
    not (AUDIT_DIR.exists() and P4_DIR.exists() and NORMALIZED.exists()),
    reason="frozen audit/p4/normalized artifacts not present",
)


@pytest.fixture(scope="session")
def run01_dir(tmp_path_factory) -> Path:
    if not (AUDIT_DIR.exists() and P4_DIR.exists()):
        pytest.skip("frozen audit/p4 artifacts not present")
    from r1spike.labeling_v2_setup import SetupConfig, run_setup

    output = tmp_path_factory.mktemp("ui-v2") / "run01"
    run_setup(
        SetupConfig(
            audit_dir=AUDIT_DIR,
            spike02r_dir=P4_DIR,
            output_dir=output,
            ttc_root=TTC_ROOT,
        )
    )
    return output


@pytest.fixture()
def fresh_run01(run01_dir: Path, tmp_path: Path) -> Path:
    target = tmp_path / "run01"
    shutil.copytree(run01_dir, target)
    return target


@pytest.fixture(scope="session")
def ui_store(run01_dir: Path):
    from uiv2.adapters import discover_v2

    return discover_v2(run01_dir, artifacts_root=ARTIFACTS, audit_dir=AUDIT_DIR)
