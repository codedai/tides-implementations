"""Full-fixture diagnostic factorial; no GTFS or human label inputs."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import platform
import resource
import time
from datetime import datetime
from pathlib import Path

import polars as pl

from .merge import build_support, direct_match, extend_sequences, usable
from .segmentation import segment_vehicle


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False) + "\n")
    path.chmod(0o600)


def save_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if rows:
        pl.from_dicts(rows, infer_schema_length=None).write_parquet(path)
        path.chmod(0o600)


def summarize(avl, apc, seg, assignments, cfg):
    counts = collections.Counter(r["status"] for r in assignments)
    reasons = collections.Counter(r["reason"] for r in assignments)
    eligible = sum(
        usable(r, "apc")
        and r.get("departure") is not None
        and 0
        <= (r["departure"] - r["arrival"]).total_seconds()
        <= cfg["max_apc_dwell_s"]
        for r in apc
    )
    apc_lookup = {r["rid"]: r for r in apc}
    ping_lookup = {r["rid"]: r for r in avl}
    seg_lookup = {s["segment_id"]: s for s in seg["segments"]}
    violations = collections.Counter()
    violations["apc_conservation"] = abs(len(apc) - len(assignments))
    violations["duplicate_apc_disposition"] = len(assignments) - len(
        {r["apc_rid"] for r in assignments}
    )
    violations["valid_avl_without_chunk"] = sum(
        not r.get("invalid")
        and r.get("time") is not None
        and r["rid"] not in seg["ping_segment"]
        for r in avl
    )
    by_segment = collections.defaultdict(list)
    strata = collections.defaultdict(collections.Counter)
    distances, gaps = [], []
    spatial_over100 = temporal_over60 = route_conflict = 0
    for r in assignments:
        source = apc_lookup[r["apc_rid"]]
        tags = ["all"]
        if source.get("grain_uncertain"):
            tags.append("grain_uncertain")
        if source.get("time_ambiguous"):
            tags.append("dst_ambiguous")
        if source.get("invalid"):
            tags.append("source_invalid")
        if source.get("arrival") and source.get("departure") == source["arrival"]:
            tags.append("zero_dwell")
        if (
            source.get("arrival")
            and source.get("departure")
            and source["arrival"].date() != source["departure"].date()
        ):
            tags.append("cross_midnight_interval")
        for tag in tags:
            strata[tag][r["status"]] += 1
        owner = r["segment_id"]
        if owner:
            if owner not in seg_lookup:
                violations["orphan_segment"] += 1
            else:
                by_segment[owner].append((source, r))
            if not usable(source, "apc"):
                violations["assignment_with_invalid_time"] += 1
            if r["distance_m"] is not None:
                distances.append(r["distance_m"])
                spatial_over100 += r["distance_m"] > 100
            if r["gap_s"] is not None:
                gaps.append(r["gap_s"])
                temporal_over60 += r["gap_s"] > 60
            route_conflict += r["route_conflict"]
        if r["status"] == "direct":
            if not r["ping_ids"] or any(
                pid not in ping_lookup for pid in r["ping_ids"]
            ):
                violations["direct_without_source_ping"] += 1
            if any(seg["ping_segment"].get(pid) != owner for pid in r["ping_ids"]):
                violations["direct_crosses_segment"] += 1
            if (
                r["distance_m"] is None
                or r["distance_m"] > cfg["radius_m"]
                or r["gap_s"] > cfg["time_tolerance_s"]
            ):
                violations["direct_outside_configured_bounds"] += 1
            if r["boundary_spanning"]:
                violations["accepted_boundary_spanning_interval"] += 1
            if owner in seg_lookup and (
                source["arrival"] > seg_lookup[owner]["end"]
                or source["departure"] < seg_lookup[owner]["start"]
            ):
                violations["direct_outside_observed_coverage"] += 1
        if r["status"] == "inferred":
            if r["ping_ids"] or not r["anchor_apc_ids"]:
                violations["inferred_mislabeled_gps_or_no_anchor"] += 1
            if r["extension_seconds"] > cfg["extension_horizon_s"]:
                violations["extension_beyond_anchor_horizon"] += 1
    durations = [(s["end"] - s["start"]).total_seconds() for s in seg["segments"]]
    direction_mixed = route_mixed = leading = trailing = 0
    coverage = []
    for sid, entries in by_segment.items():
        s = seg_lookup[sid]
        starts = [r["arrival"] for r, _ in entries]
        ends = [r["departure"] for r, _ in entries]
        directions = collections.Counter(
            r.get("direction")
            for r, a in entries
            if a["status"] == "direct" and r.get("direction")
        )
        routes = collections.Counter(
            r.get("route")
            for r, a in entries
            if a["status"] == "direct" and r.get("route")
        )
        direction_mixed += sum(n >= 3 for n in directions.values()) > 1
        route_mixed += sum(n >= 3 for n in routes.values()) > 1
        lead, tail = (
            max(0, (s["start"] - min(starts)).total_seconds()),
            max(0, (max(ends) - s["end"]).total_seconds()),
        )
        leading += lead > 0
        trailing += tail > 0
        coverage.append(
            {
                "segment_id": sid,
                "avl_observed_start": s["start"],
                "avl_observed_end": s["end"],
                "apc_assigned_start": min(starts),
                "apc_assigned_end": max(ends),
                "apc_leading_seconds": lead,
                "apc_trailing_seconds": tail,
                "apc_assignment_counts": dict(
                    collections.Counter(a["status"] for _, a in entries)
                ),
                "actual_trip_start": None,
                "actual_trip_end": None,
                "bounds_are_observation_envelopes": True,
            }
        )
    return {
        "apc_rows": len(apc),
        "apc_eligible_rows": eligible,
        "avl_rows": len(avl),
        "chunks": len(seg["segments"]),
        "chunks_over_2h": sum(d > 7200 for d in durations),
        "chunks_under_2min": sum(d < 120 for d in durations),
        "chunks_with_3plus_records_each_multiple_directions": direction_mixed,
        "chunks_with_3plus_records_each_multiple_routes": route_mixed,
        "chunks_with_apc_leading": leading,
        "chunks_with_apc_trailing": trailing,
        "assignment_status": dict(counts),
        "assignment_reasons": dict(reasons),
        "invariants": dict(violations),
        "strata": {k: dict(v) for k, v in strata.items()},
        "assigned_spatial_residual_over_100m": spatial_over100,
        "assigned_temporal_residual_over_60s": temporal_over60,
        "assigned_source_route_conflicts": int(route_conflict),
        "boundary_reasons": dict(
            collections.Counter(
                reason for b in seg["boundaries"] for reason in b["reasons"]
            )
        ),
        "duration_seconds_sum": sum(durations),
        "duration_seconds_max": max(durations, default=0),
    }, coverage


def aggregate(metrics):
    out = {}
    counter_keys = {
        "assignment_status",
        "assignment_reasons",
        "invariants",
        "boundary_reasons",
    }
    for key in metrics[0] if metrics else []:
        if key in counter_keys:
            counter = collections.Counter()
            for m in metrics:
                counter.update(m[key])
            out[key] = dict(counter)
        elif key == "strata":
            strata = collections.defaultdict(collections.Counter)
            for m in metrics:
                for tag, counts in m[key].items():
                    strata[tag].update(counts)
            out[key] = {tag: dict(c) for tag, c in strata.items()}
        elif key == "duration_seconds_max":
            out[key] = max(m[key] for m in metrics)
        else:
            out[key] = sum(m[key] for m in metrics)
    total = out.get("apc_rows", 0)
    eligible = out.get("apc_eligible_rows", 0)
    statuses = out.get("assignment_status", {})
    assigned = sum(statuses.get(k, 0) for k in ("direct", "inferred", "temporal_only"))
    out["assignment_rate_all_rows"] = assigned / total if total else None
    out["assignment_rate_eligible_rows"] = assigned / eligible if eligible else None
    out["accuracy"] = None
    out["trip_boundary_accuracy"] = None
    out["tides_publishable"] = False
    return out


def run(args):
    os.umask(0o077)
    config_path = Path(__file__).with_name("study_config.json")
    config = json.loads(config_path.read_text())
    out = args.out.resolve()
    ttc = Path(__file__).resolve().parents[2]
    allowed = (ttc / "artifacts" / "apc-avl-coverage-study-20260908").resolve()
    if not out.is_relative_to(allowed) or out == allowed:
        raise ValueError(
            "Study outputs must be in a new subdirectory of the isolated artifact root"
        )
    if out.exists():
        raise FileExistsError(f"Refusing to overwrite existing run: {out}")
    out.mkdir(parents=True, mode=0o700)
    started = time.perf_counter()
    avl = pl.read_parquet(args.audit / "avl.parquet")
    apc = pl.read_parquet(args.audit / "apc.parquet")
    vehicles = sorted(
        set(avl["vehicle"].drop_nulls()) | set(apc["vehicle"].drop_nulls()), key=digest
    )
    # Missing identity still gets a conservation disposition, never an inferred ID.
    if avl["vehicle"].null_count() or apc["vehicle"].null_count():
        vehicles.append(None)
    if args.limit:
        vehicles = vehicles[: args.limit]
    combos = [
        (s, m, regime)
        for s in config["segmentation_methods"]
        for m in config["merge_methods"]
        for regime in config["regimes"]
        if m != "time_only" or regime == "nominal"
    ]
    all_metrics = collections.defaultdict(list)
    selection = [
        {"vehicle_case": "V-" + digest(str(v)), "raw_vehicle": v} for v in vehicles
    ]
    dump(out / "selection.json", selection)
    dump(out / "config.json", config)
    code_hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(__file__).parent.glob("*.py"))
    }
    input_hashes = {}
    for name in ("avl.parquet", "apc.parquet", "inventory.json"):
        with (args.audit / name).open("rb") as handle:
            input_hashes[name] = hashlib.file_digest(handle, "sha256").hexdigest()
    dump(
        out / "manifest.json",
        {
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "code_sha256": code_hashes,
            "canonical_input_sha256": input_hashes,
            "raw_source_manifest": json.loads(
                (args.audit / "inventory.json").read_text()
            ).get("sources"),
            "environment": {
                "python": platform.python_version(),
                "polars": pl.__version__,
                "platform": platform.platform(),
                "logical_cpus": os.cpu_count(),
            },
            "raw_audit": str(args.audit.resolve()),
            "input_rows": {"avl": avl.height, "apc": apc.height},
            "selected_vehicles": len(vehicles),
            "full_fixture": not bool(args.limit),
            "gtfs_used": False,
            "human_labels_used": False,
            "publishable": False,
            "tides_conformance_claimed": False,
            "thresholds_calibrated": False,
            "started_at": datetime.now().isoformat(),
            "methods": combos,
        },
    )
    for number, vehicle in enumerate(vehicles, 1):
        case = "V-" + digest(str(vehicle))
        pred = (
            pl.col("vehicle").is_null()
            if vehicle is None
            else pl.col("vehicle") == vehicle
        )
        aa = avl.filter(pred).sort("time", "rid", nulls_last=True).to_dicts()
        pp = apc.filter(pred).sort("arrival", "rid", nulls_last=True).to_dicts()
        support_started = time.perf_counter()
        support = build_support(aa, pp)
        support_s = time.perf_counter() - support_started
        save_rows(
            out / "support_candidates" / f"{case}.parquet",
            [
                dict(apc_rid=rid, **edge)
                for rid, edges in support.items()
                for edge in edges
            ],
        )
        for s in config["segmentation_methods"]:
            segment_started = time.perf_counter()
            seg = segment_vehicle(aa, pp, s)
            segment_s = time.perf_counter() - segment_started
            save_rows(out / "chunks" / s / f"{case}.parquet", seg["segments"])
            save_rows(out / "boundaries" / s / f"{case}.parquet", seg["boundaries"])
            for regime, thresholds in config["regimes"].items():
                cfg = config["common"] | thresholds
                match_started = time.perf_counter()
                direct = direct_match(pp, support, seg, "spatiotemporal", cfg)
                direct_s = time.perf_counter() - match_started
                for method in config["merge_methods"]:
                    if method == "time_only" and regime != "nominal":
                        continue
                    match_started = time.perf_counter()
                    if method == "time_only":
                        assignment = direct_match(pp, support, seg, method, cfg)
                    elif method == "spatiotemporal":
                        assignment = direct
                    else:
                        assignment = extend_sequences(pp, direct, seg, cfg)
                    merge_s = time.perf_counter() - match_started
                    key = f"{s}__{method}__{regime}"
                    metric, coverage = summarize(aa, pp, seg, assignment, cfg)
                    metric.update(
                        support_seconds=support_s,
                        segmentation_seconds=segment_s,
                        merge_seconds=merge_s
                        + (direct_s if method != "time_only" else 0),
                    )
                    all_metrics[key].append(metric)
                    save_rows(out / "assignments" / key / f"{case}.parquet", assignment)
                    save_rows(out / "coverage" / key / f"{case}.parquet", coverage)
                    dump(out / "metrics" / key / f"{case}.json", metric)
        print(
            json.dumps(
                {
                    "completed_vehicles": number,
                    "total_vehicles": len(vehicles),
                    "avl_rows": len(aa),
                    "apc_rows": len(pp),
                    "elapsed_s": round(time.perf_counter() - started, 1),
                }
            ),
            flush=True,
        )
    totals = {k: aggregate(v) for k, v in all_metrics.items()}
    dump(out / "factorial_summary.json", totals)
    flat = []
    for key, m in totals.items():
        s, merge, regime = key.split("__")
        flat.append(
            {
                "segmentation": s,
                "merge": merge,
                "regime": regime,
                **{k: v for k, v in m.items() if not isinstance(v, dict)},
                **{"n_" + k: v for k, v in m["assignment_status"].items()},
                "invariant_violations": sum(m["invariants"].values()),
            }
        )
    pl.from_dicts(flat, infer_schema_length=None).write_csv(
        out / "factorial_summary.csv"
    )
    dump(
        out / "completion.json",
        {
            "wall_seconds": time.perf_counter() - started,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "combinations": len(totals),
            "completed_vehicles": len(vehicles),
            "all_implemented_invariants_pass": all(
                not any(m["invariants"].values()) for m in totals.values()
            ),
            "all_rubric_rules_evaluated": False,
            "full_fixture": not bool(args.limit),
            "publishable": False,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--limit", type=int, default=0, help="Smoke run only; 0 evaluates every vehicle"
    )
    run(parser.parse_args())
