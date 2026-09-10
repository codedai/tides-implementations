"""Compare independent global-only and all-mode builds without loading huge tables.

No run artifacts are rewritten. Column hashes preserve scalar types, signed zero,
nulls, Unicode and maintained row order; nested JSON remains its stored string.
"""

import argparse
import hashlib
import json
import struct
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

from tools.experiment05 import STAGES
from tools.experiment05.io import read_json, sha256
from tools.experiment05.pipeline import verify_completed


def column_hashes(path, mode="global", batch_size=4096):
    reader = pq.ParquetFile(path)
    names = sorted(reader.schema_arrow.names)
    hashes = {name: hashlib.sha256() for name in names}
    count = 0
    for batch in reader.iter_batches(batch_size=batch_size, columns=names):
        if "match_mode" in names:
            batch = batch.filter(pc.equal(batch.column("match_mode"), mode))
        count += batch.num_rows
        for name, digest in hashes.items():
            for value in batch.column(name).to_pylist():
                if value is None:
                    payload = b"N"
                elif isinstance(value, bool):
                    payload = b"B1" if value else b"B0"
                elif isinstance(value, int):
                    payload = b"I" + str(value).encode()
                elif isinstance(value, float):
                    payload = b"F" + struct.pack(">d", value)
                elif isinstance(value, str):
                    payload = b"S" + value.encode("utf-8")
                else:
                    raise TypeError(f"Unsupported stored scalar: {name}/{type(value)}")
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
    return {
        "rows": count,
        "sha256_by_column": {
            name: digest.hexdigest() for name, digest in hashes.items()
        },
    }


def compare_global(all_modes, global_only):
    left, right = Path(all_modes).resolve(), Path(global_only).resolve()
    original_manifest_hashes = [sha256(p / "run_manifest.json") for p in (left, right)]
    a, b = verify_completed(left), verify_completed(right)
    if set(a["match_modes"]) != {"global", "early", "middle", "late"}:
        raise ValueError("First run must contain all four modes")
    if b["match_modes"] != ["global"]:
        raise ValueError("Second run must be global-only")
    checks = []

    def check(name, passed, details=None):
        checks.append({"name": name, "passed": bool(passed), "details": details})

    for key in (
        "service_date",
        "route_id",
        "pipeline_version",
        "config_hash",
        "input_hashes",
        "gtfs_feed_hash",
        "feed_manifest_hash",
        "environment_hash",
    ):
        check(f"same_input_{key}", a[key] == b[key])
    shared = {}
    for stage in STAGES[: STAGES.index("generate_candidates")]:
        first, second = a["stages"][stage], b["stages"][stage]
        shared[stage] = first["output_sha256"]
        check(
            f"shared_{stage}_exact_bytes",
            first["output_sha256"] == second["output_sha256"],
        )
    check(
        "same_scheduled_lookup",
        sha256(left / "scheduled_lookup.parquet")
        == sha256(right / "scheduled_lookup.parquet"),
    )
    table_results = {}
    for table in (
        "candidate_rankings",
        "local_window_evidence",
        "mode_results",
        "assignments",
        "assignment_audit",
    ):
        first = column_hashes(left / f"{table}.parquet")
        second = column_hashes(right / f"{table}.parquet")
        differences = sorted(
            name
            for name in set(first["sha256_by_column"]) | set(second["sha256_by_column"])
            if first["sha256_by_column"].get(name)
            != second["sha256_by_column"].get(name)
        )
        table_results[table] = {"all_mode_global": first, "global_only": second}
        check(
            f"{table}_all_columns_exact",
            first == second,
            {
                "row_counts": [first["rows"], second["rows"]],
                "different_columns": differences,
            },
        )
    first = read_json(left / "assignment_summary.json")
    second = read_json(right / "assignment_summary.json")
    check(
        "assignment_round_summaries_exact",
        [row for row in first["rounds"] if row["match_mode"] == "global"]
        == second["rounds"],
    )
    csv_results = {}
    for base, label in ((left, "all_mode_global"), (right, "global_only")):
        csv_results[label] = {
            path.name: sha256(path)
            for path in sorted((base / "tides/global").glob("*.csv"))
        }
    check(
        "all_global_tides_csv_exact_bytes",
        csv_results["all_mode_global"] == csv_results["global_only"],
    )
    check(
        "no_manifest_mutation",
        original_manifest_hashes
        == [sha256(p / "run_manifest.json") for p in (left, right)],
    )
    return {
        "schema": "experiment05-global-force-comparison-v1",
        "passed": all(row["passed"] for row in checks),
        "created_at": datetime.now(UTC).isoformat(),
        "all_modes_run": str(left),
        "global_only_run": str(right),
        "independent_accuracy_evaluation": False,
        "comparison": (
            "All stored business columns, exact maintained global-subset row order; "
            "type-tagged length-prefixed column SHA-256 over 4096-row batches. "
            "Nested JSON compared verbatim without decoding."
        ),
        "excluded_metadata": [
            "run_manifest run IDs/cache keys/paths/creation and runtime/progress",
            "TIDES completion run_binding (intentionally differs per run)",
            "assignment summary all-mode totals and requested mode list",
        ],
        "checks": checks,
        "shared_stage_pins": shared,
        "business_table_hashes": table_results,
        "global_core_csv_hashes": csv_results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-modes", type=Path, required=True)
    parser.add_argument("--global-only", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error("Report must be new")
    if any(
        args.output.resolve().is_relative_to(path.resolve())
        for path in (
            args.all_modes,
            args.global_only,
        )
    ):
        parser.error("Report must be outside completed run directories")
    report = compare_global(args.all_modes, args.global_only)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "checks": len(report["checks"]),
                "output": str(args.output),
            }
        )
    )
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
