"""CLI: freeze Human Labeling Protocol v2 and initialize run01.

Builds the frozen protocol zone and an honest, empty live label zone. It
never writes a label, never touches first-pass artifacts, never runs
calibration or the factorial.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .labeling_v2_setup import SetupConfig, run_setup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the v2 labeling protocol and initialize the blind "
            "second-pass run directory (no labels are created)."
        )
    )
    parser.add_argument("--audit-dir", required=True, type=Path)
    parser.add_argument("--spike02r-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=None,
        help="root holding gtfs-* dirs; defaults to the spike02r dir's parent",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_setup(
        SetupConfig(
            audit_dir=args.audit_dir,
            spike02r_dir=args.spike02r_dir,
            output_dir=args.output,
            artifacts_root=args.artifacts_root,
        )
    )
    print(
        json.dumps(
            {
                "output": str(result.output_path),
                "cases": result.case_count,
                "child_cases": result.child_case_count,
                "universe_rows": result.universe_row_count,
                "protocol_sha256": result.protocol_sha256,
                "missing_optional_inputs": list(result.missing_optional_inputs),
                "inputs_unchanged": result.inputs_unchanged,
                "labels_created": 0,
                "human_review_completed": False,
                "calibration_run": False,
                "factorial_run": False,
                "architecture_selected": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
