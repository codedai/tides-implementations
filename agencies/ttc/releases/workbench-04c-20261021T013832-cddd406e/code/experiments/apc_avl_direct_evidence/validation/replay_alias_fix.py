"""Full-coverage composite replay for a narrowly reviewed alias-context fix.

Every raw vehicle and frozen context map is screened. Any valid measurement
whose raw aliases disagree on context triggers all-three-context recomputation
for its entire vehicle. Other assignments are inherited by explicit locator;
they are not represented as fresh executions of the final matcher.
"""

from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import inspect
import json
import os
import resource
import time
from pathlib import Path

import polars as pl

from .. import matcher
from ..evaluate import (
    METHODS,
    combine_metrics,
    dump,
    file_hash,
    load_frozen_segments,
    read_rows,
    report_text,
    save_rows,
    summarize_vehicle,
)


def validate_narrow_change(snapshot, allowed_changed, allowed_new):
    current_code = Path(matcher.__file__).read_text()
    functions = {
        name: hashlib.sha256(inspect.getsource(value).encode()).hexdigest()
        for name, value in vars(matcher).items()
        if inspect.isfunction(value) and value.__module__ == matcher.__name__
    }
    old = snapshot["function_sha256"]
    changed = {name for name in old if functions.get(name) != old[name]}
    added = set(functions) - set(old)
    if not changed <= allowed_changed or added != allowed_new:
        raise ValueError(
            f"Matcher diff exceeds approved alias fix: {changed=}, {added=}"
        )
    if matcher.DEFAULT_CONFIG != snapshot["default_config"]:
        raise ValueError("Configuration changed; bounded reuse is unsafe")

    def remainder(code):
        tree = ast.parse(code)
        tree.body = [
            node
            for node in tree.body
            if not (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in allowed_changed | allowed_new
            )
            and not (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets
                )
            )
        ]
        return ast.dump(tree, include_attributes=False)

    if remainder(snapshot["code"]) != remainder(current_code):
        raise ValueError("Non-approved matcher functions/classes/globals changed")
    return {
        "changed_existing_functions": sorted(changed),
        "new_functions": sorted(added),
        "unchanged_functions": sorted(set(old) - changed),
    }


def screen_alias_context(index, segmentation):
    mapping = segmentation["ping_segment"]
    if set(mapping) != set(index.source_ids):
        raise ValueError("Frozen context is not a complete exact raw AVL partition")
    split = []
    for group in index.groups:
        if not group["valid"]:
            continue
        owners = {mapping.get(rid) for rid in group["rids"]}
        if len(owners) != 1 or None in owners:
            split.append(
                {
                    "measurement_key": group["key"],
                    "source_alias_ids": list(group["rids"]),
                    "segment_ids": sorted(o for o in owners if o is not None),
                }
            )
    return split


def source_locator(path):
    return (
        {"path": str(path.resolve()), "sha256": file_hash(path)}
        if path.exists()
        else {"path": None, "sha256": None}
    )


def screen_motion_counters(path, config):
    """Only the stored point-counter filter changes; geometry remains frozen."""
    if not path.exists():
        return 0
    rows = pl.read_parquet(path, columns=["counter_witnesses"]).to_dicts()
    return sum(
        witness["kind"] == "point"
        and witness["distance_m"]
        <= max(
            config["contradiction_radius_m"],
            config["radius_m"] + config["max_speed_kph"] / 3.6 * witness["gap_s"],
        )
        for row in rows
        for witness in row["counter_witnesses"]
    )


def run(args):
    os.umask(0o077)
    started = time.perf_counter()
    ttc = Path(__file__).resolve().parents[3]
    allowed = (ttc / "artifacts" / "apc-avl-direct-evidence-20260908").resolve()
    out, source = args.out.resolve(), args.source_run.resolve()
    if out == allowed or not out.is_relative_to(allowed):
        raise ValueError("Output must be a new child of the isolated artifact root")
    if out.exists():
        raise FileExistsError("Refusing to overwrite a composite replay")
    prior_completion = json.loads((source / "completion.json").read_text())
    required = (
        "full_fixture",
        "all_implemented_invariants_pass",
        "code_unchanged_during_run",
        "canonical_inputs_unchanged",
        "frozen_inputs_unchanged",
    )
    if not all(prior_completion.get(key) for key in required):
        raise ValueError(
            "Inherited run is not a clean completed full-fixture evaluation"
        )
    prior = json.loads((source / "manifest.json").read_text())
    snapshot = json.loads(args.base_snapshot.read_text())
    if snapshot["matcher_sha256"] != prior["code_sha256"]["matcher.py"]:
        raise ValueError("Original matcher snapshot does not match inherited run")
    proof = validate_narrow_change(
        snapshot, set(args.allow_function_change), set(args.allow_new_function)
    )
    config = json.loads((source / "config.json").read_text())
    if config != matcher.DEFAULT_CONFIG:
        raise ValueError("Composite reuse requires unchanged default configuration")
    audit, baseline = Path(prior["audit"]), Path(prior["baseline"])
    canonical = {
        name: file_hash(audit / name) for name in prior["canonical_input_sha256"]
    }
    if canonical != prior["canonical_input_sha256"]:
        raise ValueError("Canonical sources differ from inherited run")
    frozen_hashes = json.loads((source / "frozen_input_sha256.json").read_text())
    if any(
        file_hash(baseline / name) != digest for name, digest in frozen_hashes.items()
    ):
        raise ValueError("Frozen source contexts or baseline links changed")
    selection = json.loads((source / "selection.json").read_text())
    code_files = [
        Path(matcher.__file__),
        Path(__file__),
        Path(__file__).parents[1] / "evaluate.py",
    ]
    code_hashes = {p.name: file_hash(p) for p in code_files}
    out.mkdir(parents=True, mode=0o700)
    dump(out / "selection.json", selection)
    dump(out / "config.json", config)
    manifest = prior | {
        "version": "alias-context-motion-composite-replay-v1",
        "source_run": str(source),
        "code_sha256": code_hashes,
        "source_matcher_sha256": snapshot["matcher_sha256"],
        "base_snapshot_path": str(args.base_snapshot.resolve()),
        "base_snapshot_sha256": file_hash(args.base_snapshot),
        "reviewed_change_constraints": proof,
        "full_fixture": True,
        "full_input_coverage": True,
        "all_vehicles_executed_with_final_core": False,
        "reuse_basis": "Identical primitive functions/configuration, exact source "
        "hashes, all valid measurement aliases have one frozen owner for each of "
        "three maps, and no persisted point counter is removed by the reviewed "
        "motion allowance: distance <= max(contradiction radius, radius + max speed "
        "in m/s times absolute temporal gap). Changed functions are reviewed, not "
        "automatically proven equivalent for arbitrary code.",
        "assignment_locator": "assignment_locator.json",
    }
    dump(out / "manifest.json", manifest)
    avl, apc = (
        pl.read_parquet(audit / "avl.parquet"),
        pl.read_parquet(audit / "apc.parquet"),
    )
    metrics = collections.defaultdict(list)
    locators, rerun, reused, screenings = [], [], [], []
    deltas = {method: collections.Counter() for method in METHODS}
    for number, item in enumerate(selection, 1):
        vehicle, case = item["raw_vehicle"], item["vehicle_case"]
        pred = (
            pl.col("vehicle").is_null()
            if vehicle is None
            else pl.col("vehicle") == vehicle
        )
        aa = avl.filter(pred).sort("time", "rid", nulls_last=True).to_dicts()
        index = matcher.build_index(aa, config)
        contexts = {
            method: load_frozen_segments(
                baseline / "chunks" / method / f"{case}.parquet"
            )
            for method in METHODS
        }
        split = {
            method: screen_alias_context(index, contexts[method]) for method in METHODS
        }
        motion = {
            method: screen_motion_counters(
                source / "assignments" / method / f"{case}.parquet", config
            )
            for method in METHODS
        }
        should_rerun = any(split.values()) or any(motion.values())
        screenings.append(
            {
                "vehicle_case": case,
                "avl_rows": len(aa),
                "valid_measurements": sum(g["valid"] for g in index.groups),
                "split_measurement_counts": {
                    method: len(values) for method, values in split.items()
                },
                "point_counters_within_motion_allowance": motion,
                "mode": "rerun" if should_rerun else "reused",
            }
        )
        if should_rerun:
            rerun.append(case)
            dump(out / "alias_screening" / f"{case}.json", split)
            pp = apc.filter(pred).sort("arrival", "rid", nulls_last=True).to_dicts()
            prepared = matcher.prepare_candidates(pp, index, config)
        else:
            reused.append(case)
        for method in METHODS:
            if should_rerun:
                assignments = matcher.match_direct(
                    pp, index, contexts[method], config, prepared=prepared
                )
                original_baseline = read_rows(
                    baseline
                    / "assignments"
                    / (method + "__spatiotemporal__nominal")
                    / f"{case}.parquet"
                )
                metric, paired = summarize_vehicle(
                    aa, pp, contexts[method], original_baseline, assignments, config
                )
                target = out
                save_rows(
                    target / "assignments" / method / f"{case}.parquet", assignments
                )
                save_rows(target / "paired" / method / f"{case}.parquet", paired)
                dump(target / "metrics" / method / f"{case}.json", metric)
                old = {
                    r["apc_rid"]: r
                    for r in read_rows(
                        source / "assignments" / method / f"{case}.parquet"
                    )
                }
                for row in assignments:
                    previous = old[row["apc_rid"]]
                    deltas[method]["rerun_apc_rows"] += 1
                    deltas[method]["status_changes"] += (
                        row["status"] != previous["status"]
                    )
                    deltas[method]["context_changes"] += (
                        row["segment_ids"] != previous["segment_ids"]
                        or row["context_status"] != previous["context_status"]
                    )
                    deltas[method]["full_record_changes"] += row != previous
            else:
                target = source
                metric = json.loads(
                    (target / "metrics" / method / f"{case}.json").read_text()
                )
            metrics[method].append(metric)
            entry = {
                "vehicle_case": case,
                "segmentation": method,
                "mode": "rerun" if should_rerun else "reused",
                "apc_rows": metric["apc_rows"],
            }
            for kind, folder, extension in (
                ("assignment", "assignments", "parquet"),
                ("paired", "paired", "parquet"),
                ("metric", "metrics", "json"),
            ):
                ref = source_locator(target / folder / method / f"{case}.{extension}")
                if kind != "metric" and metric["apc_rows"] and ref["path"] is None:
                    raise ValueError("Missing final source disposition artifact")
                entry[kind + "_path"], entry[kind + "_sha256"] = (
                    ref["path"],
                    ref["sha256"],
                )
            locators.append(entry)
        if should_rerun or number % 20 == 0:
            print(
                json.dumps(
                    {
                        "screened_vehicles": number,
                        "total_vehicles": len(selection),
                        "rerun_vehicles": len(rerun),
                        "elapsed_s": round(time.perf_counter() - started, 1),
                    }
                ),
                flush=True,
            )
    summary = {method: combine_metrics(rows) for method, rows in metrics.items()}
    for metric in summary.values():
        supported = sum(metric["new_statuses"].get(s, 0) for s in ("direct", "partial"))
        metric["new_locally_supported_rows"] = supported
        metric["new_local_support_rate_all_rows"] = supported / metric["apc_rows"]
        common = (
            metric["new_common_eligible_direct"] + metric["new_common_eligible_partial"]
        )
        metric["new_local_support_rate_common_eligible"] = (
            common / metric["common_eligible_apc_rows"]
        )
    dump(out / "summary.json", summary)
    dump(out / "assignment_locator.json", locators)
    dump(out / "screening_summary.json", screenings)
    dump(
        out / "delta_from_full01.json",
        {method: dict(counts) for method, counts in deltas.items()},
    )
    manifest.update(rerun_vehicle_cases=rerun, reused_vehicle_cases=reused)
    dump(out / "manifest.json", manifest)
    unchanged = all(file_hash(p) == code_hashes[p.name] for p in code_files)
    unchanged_inputs = canonical == {
        name: file_hash(audit / name) for name in canonical
    }
    unchanged_frozen = all(
        file_hash(baseline / name) == digest for name, digest in frozen_hashes.items()
    )
    unchanged_locators = all(
        entry[kind + "_path"] is None
        or file_hash(Path(entry[kind + "_path"])) == entry[kind + "_sha256"]
        for entry in locators
        for kind in ("assignment", "paired", "metric")
    )
    completion = {
        "wall_seconds": time.perf_counter() - started,
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "completed_vehicles": len(selection),
        "rerun_vehicles": len(rerun),
        "reused_vehicles": len(reused),
        "combinations": len(summary),
        "full_fixture": True,
        "full_input_coverage": True,
        "all_vehicles_executed_with_final_core": False,
        "all_implemented_invariants_pass": all(
            not any(m["invariant_violations"].values()) for m in summary.values()
        ),
        "code_unchanged_during_run": unchanged,
        "canonical_inputs_unchanged": unchanged_inputs,
        "frozen_inputs_unchanged": unchanged_frozen,
        "locator_artifacts_unchanged": unchanged_locators,
        "all_validation_rules_evaluated": False,
        "publishable": False,
        "accuracy": None,
    }
    text = (
        report_text(summary, completion)
        + "\nThis is a composite evaluation: "
        + str(len(rerun))
        + " vehicles were rerun with the final alias-context fix and "
        + str(len(reused))
        + " reused the explicitly located full01 output after all-source context "
        "and point-counter motion screening. See assignment_locator.json and "
        "manifest.json.\n"
    )
    (out / "report.md").write_text(text)
    (out / "report.md").chmod(0o600)
    dump(out / "completion.json", completion)
    if not all(
        completion[key]
        for key in (
            "all_implemented_invariants_pass",
            "code_unchanged_during_run",
            "canonical_inputs_unchanged",
            "frozen_inputs_unchanged",
            "locator_artifacts_unchanged",
        )
    ):
        raise RuntimeError("Composite evaluation integrity check failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--base-snapshot", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--allow-function-change",
        action="append",
        default=["_candidate", "match_direct"],
    )
    parser.add_argument("--allow-new-function", action="append", default=[])
    run(parser.parse_args())
