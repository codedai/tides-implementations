"""CLI entry point for the same library used by the on-demand batch website."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import STAGES
from .config import DEFAULT_CONFIG, ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("availability")
    for name in ("init", "run"):
        command = commands.add_parser(name)
        command.add_argument("--service-date", required=True)
        command.add_argument("--route-id", default="29")
        command.add_argument(
            "--match-mode",
            choices=("all", "global", "early", "middle", "late"),
            default="all",
        )
        command.add_argument("--force-rerun", action="store_true")
        command.add_argument("--output-dir", type=Path)
    stage = commands.add_parser("stage")
    stage.add_argument("stage", choices=STAGES)
    stage.add_argument("--run-dir", type=Path, required=True)
    validate = commands.add_parser("verify")
    validate.add_argument("--run-dir", type=Path, required=True)
    web = commands.add_parser("web")
    web.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    if args.command == "web":
        from .web import serve

        serve(port=args.port, root=args.root, config_path=args.config)
        return
    from .pipeline import (
        availability,
        initialize_run,
        run_pipeline,
        run_stage,
        verify_completed,
    )

    if args.command == "availability":
        result = availability(args.root, args.config)
    elif args.command == "stage":
        result = run_stage(args.stage, args.run_dir, args.root)
    elif args.command == "verify":
        result = verify_completed(args.run_dir)
    else:
        fn = initialize_run if args.command == "init" else run_pipeline
        kwargs = dict(
            service_date=args.service_date,
            route_id=args.route_id,
            match_modes=None if args.match_mode == "all" else [args.match_mode],
            force=args.force_rerun,
            root=args.root,
            config_path=args.config,
            output_dir=args.output_dir,
        )
        if args.command == "run":
            kwargs["progress"] = lambda row: print(
                json.dumps({"progress": row}, ensure_ascii=False), flush=True
            )
        result = fn(**kwargs)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
