"""CLI for the non-destructive first-pass human-label audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .human_label_audit import AuditConfig, run_audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preserve and audit first-pass Spike 02R human labels without "
            "relabeling, calibration, threshold changes, or factorial execution."
        )
    )
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--summaries", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--labels",
        type=Path,
        default=None,
        help="current label CSV; defaults to tools/labeling_ui/labels",
    )
    parser.add_argument(
        "--history",
        type=Path,
        default=None,
        help="append-only JSONL review history; defaults beside the UI labels",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_audit(
        AuditConfig(
            cases_path=args.cases,
            summaries_path=args.summaries,
            artifacts_path=args.artifacts,
            output_path=args.output,
            current_labels_path=args.labels,
            review_history_path=args.history,
        )
    )
    print(
        json.dumps(
            {
                "output": str(result.output_path),
                "cases": result.case_count,
                "inputs_unchanged": result.original_input_hashes_unchanged,
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
