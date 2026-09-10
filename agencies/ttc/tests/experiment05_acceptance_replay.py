"""Read-only completed-run audit; writes only a new, explicitly requested report.

Usage: python -m tests.experiment05_acceptance_replay --run RUN --output REPORT
This is reproducibility/structural acceptance, not independent accuracy labeling.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import frictionless
import polars as pl

from tools.experiment05 import STAGES
from tools.experiment05.assignment import assign_trips
from tools.experiment05.io import read_json, read_rows, sha256
from tools.experiment05.tides import read_completed_tides


def _csv_rows(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _same_rows(actual, expected):
    """Parquet union columns legitimately turn absent record keys into nulls."""
    names = sorted({key for row in [*actual, *expected] for key in row})
    return [{key: row.get(key) for key in names} for row in actual] == [
        {key: row.get(key) for key in names} for row in expected
    ]


def audit_run(run_dir, seeds=(0, 17, 113)):
    folder = Path(run_dir).resolve()
    manifest = read_json(folder / "run_manifest.json")
    if manifest.get("status") != "complete" or not manifest.get("validation_ok"):
        raise ValueError("Only completed validated runs may be audited")
    checks, details = [], {}

    def check(name, passed, detail=None):
        checks.append({"name": name, "passed": bool(passed), "details": detail})

    original_manifest_sha = sha256(folder / "run_manifest.json")
    pins = manifest["output_sha256"]
    check(
        "all_published_output_pins",
        all(sha256(folder / name) == digest for name, digest in pins.items()),
        len(pins),
    )
    check(
        "raw_inputs_unchanged",
        all(
            sha256(path) == manifest["input_hashes"][source]
            for source, path in manifest["input_paths"].items()
        ),
    )
    raw = pl.read_parquet(folder / "stages/raw_observations.parquet")
    final_path = folder / "fused_projected_observations.parquet"
    fields = [
        "rid",
        "raw_payload",
        "source",
        "source_row",
        "source_file_sha256",
        "route_eligible",
        "projection_status",
        "trajectory_included",
        "t",
        "trip_id_performed",
        "duplicate_of",
        "censored_tail",
    ]
    final = pl.read_parquet(final_path, columns=fields)
    check(
        "all_source_identities_conserved",
        raw.height == final.height == raw["rid"].n_unique() == final["rid"].n_unique()
        and set(raw["rid"]) == set(final["rid"]),
    )
    joined = raw.select("rid", "raw").join(
        final.select("rid", "raw_payload"), on="rid", how="inner"
    )
    check(
        "original_payloads_unchanged",
        joined.filter(pl.col("raw") != pl.col("raw_payload")).is_empty(),
    )
    check(
        "source_row_and_file_identity",
        all(
            row["rid"]
            == (f"{row['source']}:{row['source_file_sha256']}:{row['source_row']}")
            and row["source_file_sha256"] == manifest["input_hashes"][row["source"]]
            for row in final.drop("raw_payload").iter_rows(named=True)
        ),
    )
    details["source_counts"] = dict(Counter(final["source"]))
    details["route_eligible_counts"] = dict(
        Counter(final.filter(pl.col("route_eligible"))["source"])
    )
    details["projection_status_counts"] = dict(Counter(final["projection_status"]))
    details["exact_duplicate_rows"] = final["duplicate_of"].is_not_null().sum()
    details["censored_tail_rows"] = final["censored_tail"].fill_null(False).sum()
    expected_pings = set(
        final.filter(
            pl.col("route_eligible")
            & (pl.col("source") == "avl")
            & pl.col("t").is_not_null()
        )["rid"]
    )
    check(
        "apc_never_fabricated_as_trajectory",
        final.filter(
            (pl.col("source") == "apc") & pl.col("trajectory_included")
        ).is_empty(),
    )
    del raw, joined
    segments = read_rows(folder / "performed_trip_segments.parquet")
    rankings = read_rows(folder / "candidate_rankings.parquet")
    scheduled = read_rows(folder / "scheduled_lookup.parquet")
    assignments = read_rows(folder / "assignments.parquet")
    saved_audit = read_rows(folder / "assignment_audit.parquet")
    saved_summary = read_json(folder / "assignment_summary.json")
    ranking_sha = sha256(folder / "candidate_rankings.parquet")
    for seed in seeds:
        rng = random.Random(seed)
        inputs = [list(rows) for rows in (segments, rankings, scheduled)]
        for rows in inputs:
            rng.shuffle(rows)
        replay = assign_trips(*inputs, manifest["match_modes"])
        check(
            f"shuffle_{seed}_assignments",
            _same_rows(replay["assignments"], assignments),
        )
        check(f"shuffle_{seed}_audit", _same_rows(replay["audit"], saved_audit))
        check(f"shuffle_{seed}_summary", replay["summary"] == saved_summary)
    check(
        "candidate_ranking_file_not_changed",
        ranking_sha == sha256(folder / "candidate_rankings.parquet"),
    )
    bundle = read_json(folder / "gtfs/bundle.json")
    check(
        "active_feed_route_and_date",
        str(bundle["route_short_name"]) == manifest["route_id"]
        and bundle["feed_version"] == manifest["gtfs_feed"]["identifier"]
        and all(
            row["service_date"] == manifest["service_date"]
            and str(row["route_id"]) == str(bundle["route_id"])
            for row in bundle["trips"]
        ),
    )
    details["gtfs_feed"] = manifest["gtfs_feed"]
    details["performed_trip_count"] = len(segments)
    details["ranking_rows"] = len(rankings)
    details["modes"] = {}
    for mode in manifest["match_modes"]:
        binding = hashlib.sha256(
            json.dumps(
                {
                    "run_id": manifest["run_id"],
                    "cache_key": manifest["cache_key"],
                    "pipeline_version": manifest["pipeline_version"],
                    "config_hash": manifest["config_hash"],
                    "mode": mode,
                    "upstream": {
                        stage: manifest["stages"][stage]["output_sha256"]
                        for stage in STAGES[: STAGES.index("map_tides")]
                    },
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        target = folder / "tides" / mode
        completed = read_completed_tides(target, mode, expected_binding=binding)
        check(f"{mode}_tides_binding_and_pins", completed["validation"]["valid"])
        package = read_json(target / "datapackage.json")
        for resource in package["resources"]:
            report = frictionless.Resource(
                resource["path"],
                basepath=str(target),
                schema=frictionless.Schema.from_descriptor(
                    read_json(target / resource["schema"])
                ),
            ).validate()
            check(f"{mode}_{resource['name']}_fresh_schema_validation", report.valid)
        exported = _csv_rows(target / "trips_performed.csv")
        selected = [row for row in assignments if row["match_mode"] == mode]
        check(
            f"{mode}_csv_assignments_match",
            {
                (row["service_date"], row["trip_id_performed"]): row[
                    "trip_id_scheduled"
                ]
                for row in selected
            }
            == {
                (row["service_date"], row["trip_id_performed"]): row[
                    "trip_id_scheduled"
                ]
                or None
                for row in exported
            }
            and len(exported) == len(selected),
        )
        pings = _csv_rows(target / "vehicle_locations.csv")
        check(
            f"{mode}_all_real_avl_pings_preserved",
            {row["location_ping_id"] for row in pings} == expected_pings
            and len(pings) == len(expected_pings),
        )
        details["modes"][mode] = {
            "assigned": sum(row["trip_id_scheduled"] is not None for row in selected),
            "unmatched": sum(row["trip_id_scheduled"] is None for row in selected),
            "assignment_rounds": dict(
                Counter(str(row["assignment_round"]) for row in selected)
            ),
            "unmatched_primary_reasons": dict(
                Counter(
                    row["unmatched_reason"]
                    for row in selected
                    if row["unmatched_reason"]
                )
            ),
            "tides": completed["summary"],
        }
    check(
        "run_manifest_not_changed",
        original_manifest_sha == sha256(folder / "run_manifest.json"),
    )
    return {
        "schema": "experiment05-independent-acceptance-v1",
        "passed": all(row["passed"] for row in checks),
        "run_dir": str(folder),
        "service_date": manifest["service_date"],
        "created_at": datetime.now(UTC).isoformat(),
        "independent_accuracy_evaluation": False,
        "notes": "Structural acceptance and replay only; no 04c comparison or labels.",
        "checks": checks,
        "details": details,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error("Report output must be new")
    if args.output.resolve().is_relative_to(args.run.resolve()):
        parser.error("Do not write reports inside a byte-pinned completed run")
    report = audit_run(args.run)
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
