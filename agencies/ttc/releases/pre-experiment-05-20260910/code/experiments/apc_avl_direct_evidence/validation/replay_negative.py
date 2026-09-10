"""Independently replay persisted negative evidence against canonical inputs.

This validator imports neither the matcher nor its evaluator. Passing these
checks establishes bounded evidence integrity, not matching accuracy or APC
clock/record-grain semantics. No raw records or frozen chunks are rewritten.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import polars as pl

METHODS = ("gap_only", "avl_context", "apc_assisted")
RULES = {
    "N01_source_identity": "Counter IDs exist, are unique and match the APC vehicle.",
    "N02_source_quality": "Source instants pass time/position/duplicate/spike checks.",
    "N03_endpoint_target": "Target label/time is the source APC arrival or departure.",
    "N04_point_replay": "Point geometry, time and counter-evidence bounds replay.",
    "N05_interpolation_replay": "Derived position has adjacent time/speed-bounded GPS.",
    "N06_interpolation_owner": "Counter brackets have the same non-null frozen chunk.",
    "N07_lineage_context": "Source measurement keys and route labels are preserved.",
    "N08_conflict_state": "Conflict labels equal counter targets and force abstention.",
    "N09_interval_excursion": "Interior-excursion flags replay from raw chronology.",
}


def wall_seconds(value):
    return (value - datetime(1970, 1, 1)).total_seconds()


def valid_time(row, field):
    value = row.get(field)
    return (
        isinstance(value, datetime)
        and value.tzinfo is None
        and not any(
            row.get(k) for k in ("invalid", "time_ambiguous", "time_nonexistent")
        )
    )


def valid_position(row):
    a, b = row.get("lat"), row.get("lon")
    return (
        a is not None
        and b is not None
        and math.isfinite(a)
        and math.isfinite(b)
        and -90 <= a <= 90
        and -180 <= b <= 180
        and (a, b) != (0, 0)
    )


def separation(a, b):
    lat0, lat1 = math.radians(a["lat"]), math.radians(b["lat"])
    lon_difference = math.radians(b["lon"] - a["lon"])
    return 6371008.8 * math.sqrt(
        (lat1 - lat0) ** 2 + (lon_difference * math.cos((lat0 + lat1) / 2)) ** 2
    )


def close(a, b, tolerance=1e-5):
    return (
        a is not None
        and b is not None
        and math.isfinite(a)
        and math.isfinite(b)
        and abs(a - b) <= tolerance
    )


class NegativeReplay:
    """One vehicle's raw chronology, reusable across frozen chunk variants."""

    def __init__(self, avl, apc, config):
        self.config = config
        self.avl = {r["rid"]: r for r in avl}
        self.apc = {r["rid"]: r for r in apc}
        grouped = defaultdict(list)
        for row in avl:
            t = row.get("time")
            if isinstance(t, datetime) and t.tzinfo is None:
                grouped[wall_seconds(t)].append(row)
        self.groups = []
        self.by_rid = {}
        for t, rows in sorted(grouped.items()):
            positions = {(r.get("lat"), r.get("lon")) for r in rows}
            sample = min(rows, key=lambda r: r["rid"])
            key = hashlib.sha256(
                repr(
                    (sample.get("vehicle"), t, sample.get("lat"), sample.get("lon"))
                ).encode()
            ).hexdigest()[:24]
            group = {
                "t": t,
                "lat": sample.get("lat"),
                "lon": sample.get("lon"),
                "key": key,
                "rids": tuple(sorted(r["rid"] for r in rows)),
                "valid": len(positions) == 1
                and all(valid_time(r, "time") and valid_position(r) for r in rows),
                "routes": sorted({str(r["route"]) for r in rows if r.get("route")}),
            }
            self.groups.append(group)
            for row in rows:
                self.by_rid[row["rid"]] = group
        spikes = []
        for i in range(1, len(self.groups) - 1):
            a, b, c = self.groups[i - 1 : i + 2]
            dt0, dt1 = b["t"] - a["t"], c["t"] - b["t"]
            if not all(g["valid"] for g in (a, b, c)):
                continue
            if max(dt0, dt1) > config["interpolation_gap_s"]:
                continue
            if (
                separation(a, b) * 3.6 / dt0 > config["max_speed_kph"]
                and separation(b, c) * 3.6 / dt1 > config["max_speed_kph"]
                and separation(a, c) <= config["radius_m"]
            ):
                spikes.append(i)
        for i in spikes:
            self.groups[i]["valid"] = False
        self.times = [g["t"] for g in self.groups]
        self.excursion_cache = {}

    def excursion(self, row):
        if row["rid"] in self.excursion_cache:
            return self.excursion_cache[row["rid"]]
        expected = False
        if (
            valid_time(row, "arrival")
            and valid_time(row, "departure")
            and valid_position(row)
        ):
            a, d = wall_seconds(row["arrival"]), wall_seconds(row["departure"])
            if 0 <= d - a <= self.config["max_apc_dwell_s"]:
                left, right = (
                    bisect.bisect_left(self.times, a),
                    bisect.bisect_right(self.times, d),
                )
                expected = any(
                    g["valid"]
                    and separation(row, g) > self.config["contradiction_radius_m"]
                    for g in self.groups[left:right]
                )
        self.excursion_cache[row["rid"]] = expected
        return expected

    def validate(self, assignments, mapping):
        checks = {name: {"checked": 0, "failures": 0} for name in RULES}
        counts = Counter(rows=len(assignments))

        def check(name, condition):
            checks[name]["checked"] += 1
            checks[name]["failures"] += not bool(condition)

        for decision in assignments:
            row = self.apc[decision["apc_rid"]]
            counters = decision.get("counter_witnesses") or []
            labels = sorted({w.get("target") for w in counters})
            check(
                "N08_conflict_state",
                labels == sorted(decision.get("endpoint_conflicts") or [])
                and (not labels or decision["status"] == "ambiguous"),
            )
            counts["rows_with_counter_witnesses"] += bool(counters)
            counts["counter_witnesses"] += len(counters)
            counts["interval_excursion_rows"] += bool(
                decision.get("interval_excursion")
            )
            if decision.get("reason") == "CANDIDATE_SEARCH_OVERFLOW":
                counts["excursion_overflow_not_evaluated"] += 1
            else:
                check(
                    "N09_interval_excursion",
                    bool(decision.get("interval_excursion")) == self.excursion(row),
                )
            for witness in counters:
                kind = witness.get("kind")
                counts["counter_kind_" + str(kind)] += 1
                ids = witness.get("ping_ids") or []
                n = 1 if kind == "point" else 2 if kind == "interpolation" else 0
                identity_ok = (
                    n > 0
                    and len(ids) == len(set(ids)) == n
                    and all(
                        pid in self.avl
                        and self.avl[pid].get("vehicle") == row.get("vehicle")
                        for pid in ids
                    )
                )
                check("N01_source_identity", identity_ok)
                if not identity_ok:
                    continue
                groups = [self.by_rid.get(pid) for pid in ids]
                quality_ok = all(g is not None and g["valid"] for g in groups)
                check("N02_source_quality", quality_ok)
                label = witness.get("target")
                target_ok = label in {"arrival", "departure"} and valid_time(row, label)
                target = wall_seconds(row[label]) if target_ok else None
                target_ok = target_ok and close(witness.get("target_time"), target)
                check("N03_endpoint_target", target_ok)
                if not quality_ok or not target_ok:
                    continue
                keys = [g["key"] for g in groups]
                routes = sorted({r for g in groups for r in g["routes"]})
                check(
                    "N07_lineage_context",
                    list(witness.get("measurement_keys") or []) == keys
                    and list(witness.get("route_labels") or []) == routes,
                )
                if kind == "point":
                    g = groups[0]
                    gap, distance = abs(g["t"] - target), separation(row, g)
                    motion_envelope = max(
                        self.config["contradiction_radius_m"],
                        self.config["radius_m"]
                        + self.config["max_speed_kph"] / 3.6 * gap,
                    )
                    check(
                        "N04_point_replay",
                        close(witness.get("lat"), g["lat"], 1e-10)
                        and close(witness.get("lon"), g["lon"], 1e-10)
                        and close(witness.get("support_time"), g["t"])
                        and close(witness.get("gap_s"), gap)
                        and close(witness.get("bracket_gap_s"), 0)
                        and close(witness.get("distance_m"), distance)
                        and gap <= self.config["contemporaneous_s"]
                        and distance > motion_envelope,
                    )
                else:
                    p, q = groups
                    span = q["t"] - p["t"]
                    idx = bisect.bisect_left(self.times, target)
                    adjacent = 0 < idx < len(self.times) and self.times[
                        idx - 1 : idx + 1
                    ] == [p["t"], q["t"]]
                    bounded = (
                        p["t"] < target < q["t"]
                        and span <= self.config["interpolation_gap_s"]
                    )
                    geometric_ok = False
                    if bounded:
                        f = (target - p["t"]) / span
                        predicted = {
                            k: p[k] + f * (q[k] - p[k]) for k in ("lat", "lon")
                        }
                        distance = separation(row, predicted)
                        geometric_ok = (
                            close(witness.get("lat"), predicted["lat"], 1e-10)
                            and close(witness.get("lon"), predicted["lon"], 1e-10)
                            and close(witness.get("support_time"), target)
                            and close(witness.get("gap_s"), 0)
                            and close(witness.get("bracket_gap_s"), span)
                            and close(witness.get("distance_m"), distance)
                            and distance > self.config["contradiction_radius_m"]
                            and separation(p, q) * 3.6 / span
                            <= self.config["max_speed_kph"]
                        )
                    check(
                        "N05_interpolation_replay",
                        adjacent and bounded and geometric_ok,
                    )
                    owners = [
                        mapping.get(pid) for group in groups for pid in group["rids"]
                    ]
                    check(
                        "N06_interpolation_owner",
                        None not in owners and len(set(owners)) == 1,
                    )
        return {"counts": dict(counts), "rules": checks}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def execute(args):
    started = time.monotonic()
    root, out = args.run.resolve(), args.out.resolve()
    if not (root / "completion.json").exists():
        raise ValueError("Negative replay requires a completed direct-evidence run")
    if out.exists():
        raise ValueError("Refusing to overwrite an existing replay artifact directory")
    os.umask(0o077)
    out.mkdir(parents=True, mode=0o700)
    manifest = json.loads((root / "manifest.json").read_text())
    completion = json.loads((root / "completion.json").read_text())
    config = json.loads((root / "config.json").read_text())
    audit, baseline = Path(manifest["audit"]), Path(manifest["baseline"])
    selected = json.loads((root / "selection.json").read_text())
    locator_path = root / "assignment_locator.json"
    locator = None
    if locator_path.exists():
        entries = json.loads(locator_path.read_text())
        locator = {(e["vehicle_case"], e["segmentation"]): e for e in entries}
        expected = {(e["vehicle_case"], m) for e in selected for m in METHODS}
        if len(entries) != len(locator) or set(locator) != expected:
            raise ValueError("Composite assignment locator is not complete and unique")
    totals = {
        method: {"counts": Counter(), "rules": {k: Counter() for k in RULES}}
        for method in METHODS
    }
    failure_groups = []
    assignment_modes = Counter()
    avl_columns = [
        "rid",
        "vehicle",
        "time",
        "lat",
        "lon",
        "route",
        "invalid",
        "time_ambiguous",
        "time_nonexistent",
    ]
    apc_columns = [
        "rid",
        "vehicle",
        "arrival",
        "departure",
        "lat",
        "lon",
        "invalid",
        "time_ambiguous",
        "time_nonexistent",
    ]
    outcome_columns = [
        "apc_rid",
        "status",
        "reason",
        "counter_witnesses",
        "endpoint_conflicts",
        "interval_excursion",
    ]
    for position, entry in enumerate(selected, 1):
        vehicle, case = entry["raw_vehicle"], entry["vehicle_case"]
        avl = (
            pl.scan_parquet(audit / "avl.parquet")
            .filter(pl.col("vehicle") == vehicle)
            .select(avl_columns)
            .collect()
            .to_dicts()
        )
        apc = (
            pl.scan_parquet(audit / "apc.parquet")
            .filter(pl.col("vehicle") == vehicle)
            .select(apc_columns)
            .collect()
            .to_dicts()
        )
        replay = NegativeReplay(avl, apc, config)
        for method in METHODS:
            if locator is not None:
                located = locator[(case, method)]
                if located["mode"] not in {"reused", "rerun"}:
                    raise ValueError("Unknown composite assignment source mode")
                assignment_modes[located["mode"]] += 1
                assignment_path = (
                    Path(located["assignment_path"])
                    if located["assignment_path"] is not None
                    else None
                )
                if located["apc_rows"] != len(apc):
                    raise ValueError("Composite locator APC denominator mismatch")
                if assignment_path is None:
                    if apc or located["assignment_sha256"] is not None:
                        raise ValueError("Null assignment is only valid for no APC")
                elif sha256(assignment_path) != located["assignment_sha256"]:
                    raise ValueError("Composite assignment content hash mismatch")
            else:
                assignment_modes["standalone"] += 1
                assignment_path = root / "assignments" / method / f"{case}.parquet"
            decisions = (
                pl.read_parquet(assignment_path, columns=outcome_columns).to_dicts()
                if assignment_path is not None and assignment_path.exists()
                else []
            )
            if len(decisions) != len(apc) or {r["apc_rid"] for r in decisions} != {
                r["rid"] for r in apc
            }:
                raise ValueError("Assignment population does not match canonical APC")
            chunk_path = baseline / "chunks" / method / f"{case}.parquet"
            chunks = (
                pl.read_parquet(
                    chunk_path, columns=["segment_id", "ping_ids"]
                ).to_dicts()
                if chunk_path.exists()
                else []
            )
            mapping = {
                rid: chunk["segment_id"]
                for chunk in chunks
                for rid in chunk["ping_ids"]
            }
            result = replay.validate(decisions, mapping)
            failed = {
                name: value["failures"]
                for name, value in result["rules"].items()
                if value["failures"]
            }
            if failed:
                failure_groups.append(
                    {"vehicle_case": case, "method": method, "failures": failed}
                )
            totals[method]["counts"].update(result["counts"])
            for name, value in result["rules"].items():
                totals[method]["rules"][name].update(value)
        if position % 20 == 0 or position == len(selected):
            print(
                json.dumps(
                    {
                        "completed_vehicles": position,
                        "vehicles": len(selected),
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                    }
                ),
                flush=True,
            )
    for method in METHODS:
        for rule, value in totals[method]["rules"].items():
            value["status"] = (
                "not_evaluated"
                if not value["checked"]
                else "fail"
                if value["failures"]
                else "pass"
            )
            value["description"] = RULES[rule]
    report = {
        "version": "independent-negative-evidence-replay-v1",
        "completed": True,
        "vehicles": len(selected),
        "rules": totals,
        "failure_groups": failure_groups,
        "assignment_source_modes": dict(assignment_modes),
        "all_vehicles_executed_with_final_core": completion.get(
            "all_vehicles_executed_with_final_core"
        ),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "independent_accuracy": None,
        "counter_semantics": (
            "Experimental temporal-neighborhood conflicts under uncorrected "
            "clocks, not proof of APC error or physical impossibility."
        ),
        "scope": (
            "All persisted endpoint counter-witnesses in all three frozen "
            "segmentation contexts; all stored endpoint-conflict and "
            "interval-excursion flags, except explicit overflow skips."
        ),
        "not_evaluated": [
            "Completeness of omitted counter-witnesses and candidate episodes.",
            "Clock alignment, vehicle crosswalk, APC grain and stop coordinates.",
            "Phase-aware movement envelopes for nonzero point/event time gaps.",
            "Physical path between brackets beyond the linear model and speed bound.",
            "Actual trip identity, matching accuracy and TIDES publication readiness.",
        ],
        "input_run_manifest_sha256": sha256(root / "manifest.json"),
        "input_run_completion_sha256": sha256(root / "completion.json"),
        "input_assignment_locator_sha256": (
            sha256(locator_path) if locator is not None else None
        ),
        "validator_sha256": sha256(Path(__file__)),
    }
    (out / "negative_evidence_replay.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    failures = sum(
        rule["failures"]
        for method in totals.values()
        for rule in method["rules"].values()
    )
    print(
        json.dumps(
            {
                "completed": True,
                "rule_failures": failures,
                "elapsed_seconds": report["elapsed_seconds"],
            }
        ),
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    raise SystemExit(execute(parser.parse_args()))
