"""Read-only hash attestation before retiring historical experiment artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.merge_review.retention import build_retention_bundle
from tools.merge_review.workbench import read, safe_child, sha, write


def attest(runs):
    reports = []
    for run in map(Path, runs):
        manifest = read(run / "manifest.json")
        own = read(run / "output_sha256.json")
        for name, digest in own.items():
            if sha(safe_child(run, name)) != digest:
                raise ValueError(f"Retained output changed: {run.name}/{name}")
        if read(run / "completion.json").get("validation_ok") is not True:
            raise ValueError("Incomplete retained run")
        source = Path(manifest["partial_source_path"])
        pins = manifest["partial_source_sha256"]
        for name, digest in pins.items():
            if sha(safe_child(source, name)) != digest:
                raise ValueError(
                    f"Historical replay input changed: {source.name}/{name}"
                )
        reports.append(
            {
                "run_id": run.name,
                "output_files_verified": len(own),
                "output_inventory_sha256": sha(run / "output_sha256.json"),
                "historical_source": str(source),
                "historical_source_files_verified": len(pins),
                "historical_source_sha256": pins,
                "pre_retention_verification": "PASS",
                "historical_recomputation_performed": False,
                "scope": (
                    "Existing bytes verified; not a substitute "
                    "for a future live replay audit"
                ),
            }
        )
    return {"status": "PASS", "runs": reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run", action="append", required=True)
    parser.add_argument("--dependency", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preflight report already exists")
    workspace = args.workspace.resolve()
    report = attest([workspace / "runs" / run for run in args.run])
    report["retention_bundle"] = build_retention_bundle(
        workspace, args.run, args.dependency
    )
    write(args.output, report)
    print(
        json.dumps(
            {
                "status": "PASS",
                "output": str(args.output),
                "bundle": report["retention_bundle"],
            }
        )
    )


if __name__ == "__main__":
    main()
