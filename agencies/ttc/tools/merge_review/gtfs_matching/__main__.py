"""Run an immutable route-29 experiment from a completed experiment-02 parent."""

import argparse
import json
from pathlib import Path

from tools.merge_review.debug import DebugWorkspace
from tools.merge_review.server import DEFAULT_AUDIT, DEFAULT_DEBUG

from . import METHOD


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--debug-dir", type=Path, default=DEFAULT_DEBUG)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument(
        "--config", type=Path, help="Optional JSON object of GTFS parameters"
    )
    parser.add_argument("--label", default="实验 03 · route 29 · 分层 GTFS 匹配")
    args = parser.parse_args()
    supplied = json.loads(args.config.read_text()) if args.config else {}
    workspace = DebugWorkspace(args.debug_dir, args.audit)
    result = workspace.create_run(
        {"method": METHOD, "gtfs": supplied},
        label=args.label,
        parent_run_id=args.parent,
        progress=lambda p: print(json.dumps(p, ensure_ascii=False), flush=True),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
