"""Audit context ambiguity hidden by exact-measurement representative IDs."""

from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path

import polars as pl

from ..evaluate import METHODS, dump, file_hash, read_rows


def audit_vehicle(avl, assignments, mapping):
    """Replay accepted, candidate, and counter-witness source alias contexts."""
    instants = collections.defaultdict(list)
    for row in avl:
        if row.get("time") is not None:
            instants[row["time"]].append(row)
    aliases = {}
    counters = collections.Counter()
    for rows in instants.values():
        if len(rows) < 2:
            continue
        if len({(r.get("lat"), r.get("lon")) for r in rows}) != 1:
            continue
        if any(
            r.get("invalid")
            or r.get("time_ambiguous")
            or r.get("time_nonexistent")
            or r.get("lat") is None
            or r.get("lon") is None
            or not -90 <= r["lat"] <= 90
            or not -180 <= r["lon"] <= 180
            or (r["lat"], r["lon"]) == (0, 0)
            for r in rows
        ):
            continue
        owners = {mapping.get(r["rid"]) for r in rows}
        counters["eligible_duplicated_measurements"] += 1
        if len(owners) > 1:
            counters["duplicated_measurements_with_multiple_frozen_contexts"] += 1
            evidence = {
                "source_alias_ids": sorted(r["rid"] for r in rows),
                "all_alias_segment_ids": sorted(o for o in owners if o is not None),
                "some_alias_unmapped": None in owners,
            }
            for row in rows:
                aliases[row["rid"]] = evidence
    affected = []

    def validate_positive_record(record, scope):
        witnesses = record.get("witnesses", [])
        if not witnesses:
            return
        expected = set()
        for witness in witnesses:
            owners = set()
            for rid in witness["ping_ids"]:
                if rid in aliases:
                    owners.update(aliases[rid]["all_alias_segment_ids"])
                    if aliases[rid]["some_alias_unmapped"]:
                        owners.add(None)
                else:
                    owners.add(mapping.get(rid))
            expected.update(owners)
            counters[scope + "_alias_owner_witness_checks"] += 1
            if witness["kind"] == "interpolation" and (
                len(owners) != 1 or None in owners
            ):
                counters[scope + "_interpolation_alias_owner_violations"] += 1
        declared = set(record.get("segment_ids", []))
        if declared != expected - {None}:
            counters[scope + "_declared_alias_owner_set_violations"] += 1
        context = (
            "unmapped"
            if None in expected
            else "single_segment"
            if len(expected) == 1
            else "multiple_segments"
        )
        if record.get("context_status") != context:
            counters[scope + "_declared_alias_context_status_violations"] += 1

    for result in assignments:
        counters["assignments_examined"] += 1
        if result["status"] in {"direct", "partial"}:
            validate_positive_record(result, "accepted")
        for candidate in result.get("candidates", []):
            validate_positive_record(candidate, "candidate")
        problematic = []
        scopes = [
            (
                "candidate",
                [
                    w
                    for c in result.get("candidates", [])
                    for w in c.get("witnesses", [])
                ],
            ),
            ("counter", result.get("counter_witnesses", [])),
        ]
        if result["status"] in {"direct", "partial"}:
            scopes.append(("accepted", result.get("witnesses", [])))
        for scope, witnesses in scopes:
            counters[scope + "_assignments_examined"] += 1
            scope_affected = False
            for witness in witnesses:
                counters[scope + "_witnesses_examined"] += 1
                implicated = {
                    tuple(aliases[rid]["source_alias_ids"]): aliases[rid]
                    for rid in witness["ping_ids"]
                    if rid in aliases
                }
                if not implicated:
                    continue
                scope_affected = True
                counters[scope + "_witnesses_with_split_context_aliases"] += 1
                if witness["kind"] == "interpolation":
                    counters[scope + "_interpolations_with_split_context_aliases"] += 1
                problematic.append(
                    {
                        "scope": scope,
                        "kind": witness["kind"],
                        "chosen_ping_ids": witness["ping_ids"],
                        "target": witness["target"],
                        "aliases": list(implicated.values()),
                    }
                )
            if scope_affected:
                counters[scope + "_assignments_with_split_context_aliases"] += 1
        if problematic:
            counters["any_scope_assignments_with_split_context_aliases"] += 1
            affected.append(
                {
                    "apc_rid": result["apc_rid"],
                    "status": result["status"],
                    "support_level": result["support_level"],
                    "declared_segment_ids": result["segment_ids"],
                    "declared_context_status": result["context_status"],
                    "problematic_witnesses": problematic,
                }
            )
    return dict(counters), affected


def run(args):
    os.umask(0o077)
    base = Path(__file__).resolve().parents[3]
    allowed = (base / "artifacts" / "apc-avl-direct-evidence-20260908").resolve()
    out = args.out.resolve()
    if out == allowed or not out.is_relative_to(allowed):
        raise ValueError("Output must be a new child of the isolated artifact root")
    if out.exists():
        raise FileExistsError("Refusing to overwrite an audit")
    run_path = args.run.resolve()
    complete = (run_path / "completion.json").exists()
    if not complete and not args.allow_incomplete:
        raise ValueError("Full run is incomplete; explicit snapshot mode required")
    manifest = json.loads((run_path / "manifest.json").read_text())
    baseline = Path(manifest["baseline"])
    audit = Path(manifest["audit"])
    selected = json.loads((run_path / "selection.json").read_text())
    completed = {p.stem for p in (run_path / "metrics" / "apc_assisted").glob("*.json")}
    selected = [row for row in selected if row["vehicle_case"] in completed]
    split_screen = None
    if args.split_screen is not None:
        split_screen = json.loads(args.split_screen.read_text())
        if not split_screen.get("full_fixture") or split_screen.get("vehicles") != 157:
            raise ValueError(
                "Bounded alias replay needs the complete source-context screen"
            )
        scope_cases = {
            row["vehicle_case"]
            for row in split_screen["vehicle_metrics"]
            if any(row["split_measurements"].values())
        }
        if not scope_cases <= {row["vehicle_case"] for row in selected}:
            raise ValueError(
                "Some alias-affected vehicles lack completed matcher output"
            )
        selected = [row for row in selected if row["vehicle_case"] in scope_cases]
    out.mkdir(parents=True, mode=0o700)
    columns = [
        "rid",
        "vehicle",
        "time",
        "lat",
        "lon",
        "invalid",
        "time_ambiguous",
        "time_nonexistent",
    ]
    avl = pl.read_parquet(audit / "avl.parquet", columns=columns)
    summary = {method: collections.Counter() for method in METHODS}
    affected_vehicles = {method: [] for method in METHODS}
    split_measurement_vehicles = {method: [] for method in METHODS}
    input_hashes = {}
    for item in selected:
        vehicle, case = item["raw_vehicle"], item["vehicle_case"]
        pred = (
            pl.col("vehicle").is_null()
            if vehicle is None
            else pl.col("vehicle") == vehicle
        )
        aa = avl.filter(pred).to_dicts()
        for method in METHODS:
            path = baseline / "chunks" / method / f"{case}.parquet"
            chunks = (
                pl.read_parquet(path, columns=["segment_id", "ping_ids"]).to_dicts()
                if path.exists()
                else []
            )
            mapping = {rid: s["segment_id"] for s in chunks for rid in s["ping_ids"]}
            assignments = read_rows(
                run_path / "assignments" / method / f"{case}.parquet"
            )
            for input_path in (
                path,
                run_path / "assignments" / method / f"{case}.parquet",
            ):
                if input_path.exists():
                    input_hashes[str(input_path.resolve())] = file_hash(input_path)
            metric, affected = audit_vehicle(aa, assignments, mapping)
            summary[method].update(metric)
            dump(out / "metrics" / method / f"{case}.json", metric)
            if metric.get("duplicated_measurements_with_multiple_frozen_contexts"):
                split_measurement_vehicles[method].append(case)
            if affected:
                affected_vehicles[method].append(case)
                dump(out / "affected" / method / f"{case}.json", affected)
    result = {
        "version": "duplicate-context-replay-v3",
        "source_run": str(run_path),
        "completed_run_at_start": complete,
        "selected_completed_vehicles": len(selected),
        "full_fixture": complete and manifest["full_fixture"] and split_screen is None,
        "source_run_full_fixture": complete and manifest["full_fixture"],
        "all_alias_split_vehicles_included": split_screen is not None,
        "split_screen_path": str(args.split_screen.resolve())
        if args.split_screen
        else None,
        "split_screen_sha256": file_hash(args.split_screen)
        if args.split_screen
        else None,
        "matcher_sha256": manifest["code_sha256"]["matcher.py"],
        "audit_code_sha256": file_hash(Path(__file__)),
        "summary": {method: dict(counter) for method, counter in summary.items()},
        "affected_vehicle_cases": affected_vehicles,
        "split_measurement_vehicle_cases": split_measurement_vehicles,
        "implemented_alias_owner_violations": sum(
            value
            for counts in summary.values()
            for key, value in counts.items()
            if key.endswith("_violations")
        ),
        "inputs_unchanged": all(
            file_hash(Path(path)) == digest for path, digest in input_hashes.items()
        ),
        "claim_scope": "All persisted accepted, candidate, and counter witnesses; "
        "identical time/position "
        "measurement aliases with valid source times and coordinates. Context "
        "ambiguity is not automatically a false physical observation match. "
        "Positive accepted/candidate interpolation and declared owner metadata "
        "are checked using all measurement aliases, including source aliases "
        "omitted from the representative witness.",
    }
    dump(out / "input_sha256.json", input_hashes)
    dump(out / "assessment.json", result)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--split-screen", type=Path)
    run(parser.parse_args())
