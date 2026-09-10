"""Preselected raw-support masking and perturbation probes, without ground truth.

These probes measure method self-consistency after source observations change.
Recovery against an unmasked algorithm output is not attachment/trip accuracy.
Fixed 2-hour windows censor the surrounding vehicle trajectory; their selected
starts/ends are observation windows and must not be treated as physical termini.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random
import time
from datetime import timedelta
from pathlib import Path

import polars as pl

from .merge import DEFAULTS, build_support, direct_match, extend_sequences
from .raw_audit import wall_time_status
from .segmentation import METHODS, segment_vehicle

MERGE_METHODS = ("time_only", "spatiotemporal", "sequence_extension")
ASSIGNED_STATUSES = {"direct", "inferred", "temporal_only"}
WINDOW_SECONDS = 7200


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False) + "\n")
    path.chmod(0o600)


def _save_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if rows:
        pl.from_dicts(rows, infer_schema_length=None).write_parquet(path)
        path.chmod(0o600)


def select_windows(avl: pl.DataFrame, apc: pl.DataFrame, limit: int = 12) -> list[dict]:
    """Choose raw-eligible fixed windows before evaluating any algorithm output.

    Vehicles are ordered by sha256(str(vehicle))[:16]. Within a vehicle, windows
    are ordered by a hash of vehicle and fixed wall-time window start. Eligibility
    uses only source presence: >=60 AVL rows, >=12 APC rows, each source spanning
    >=90 minutes and the two source envelopes overlapping >=60 minutes.
    Invalid/ambiguous source rows are retained for evaluation once selected.
    """
    summaries = []
    for frame, source, clock in ((avl, "avl", "time"), (apc, "apc", "arrival")):
        summary = (
            frame.filter(pl.col("vehicle").is_not_null() & pl.col(clock).is_not_null())
            .with_columns(pl.col(clock).dt.truncate("2h").alias("window_start"))
            .group_by("vehicle", "window_start")
            .agg(
                pl.len().alias(f"{source}_rows"),
                pl.col(clock).min().alias(f"{source}_first"),
                pl.col(clock).max().alias(f"{source}_last"),
            )
        )
        summaries.append(summary)
    joined = summaries[0].join(summaries[1], on=["vehicle", "window_start"])
    candidates = joined.filter(
        (pl.col("avl_rows") >= 60)
        & (pl.col("apc_rows") >= 12)
        & ((pl.col("avl_last") - pl.col("avl_first")).dt.total_seconds() >= 5400)
        & ((pl.col("apc_last") - pl.col("apc_first")).dt.total_seconds() >= 5400)
        & (
            (
                pl.min_horizontal("avl_last", "apc_last")
                - pl.max_horizontal("avl_first", "apc_first")
            ).dt.total_seconds()
            >= 3600
        )
    ).to_dicts()
    by_vehicle = collections.defaultdict(list)
    for row in candidates:
        row["vehicle_case"] = "V-" + digest(str(row["vehicle"]))
        row["window_end"] = row["window_start"] + timedelta(seconds=WINDOW_SECONDS)
        row["window_hash"] = digest(
            f"{row['vehicle']}|{row['window_start'].isoformat()}"
        )
        row["overlap_envelope_seconds"] = (
            min(row["avl_last"], row["apc_last"])
            - max(row["avl_first"], row["apc_first"])
        ).total_seconds()
        row["apc_prefix_cutoff"] = row["apc_first"] + timedelta(minutes=15)
        row["avl_suffix_cutoff"] = row["avl_last"] - timedelta(minutes=13)
        by_vehicle[row["vehicle"]].append(row)
    chosen = []
    for vehicle in sorted(by_vehicle, key=lambda v: (digest(str(v)), str(v)))[:limit]:
        chosen.append(min(by_vehicle[vehicle], key=lambda r: r["window_hash"]))
    return chosen


def select_edge_windows(
    avl: pl.DataFrame, apc: pl.DataFrame, per_stratum: int = 2
) -> tuple[list[dict], dict]:
    """Select difficult observations by raw features, never algorithm outcomes."""
    pools: dict[str, list[dict]] = {
        "shared_stop_opposite_direction_overlap": [],
        "dst_ambiguous_hour": [],
        "month_end_apc_tail": [],
        "internal_avl_gap_90_to_1800s": [],
    }
    vehicle = pl.col("vehicle").is_not_null()
    aa = avl.filter(vehicle & pl.col("time").is_not_null())
    pp = apc.filter(
        vehicle & pl.col("arrival").is_not_null() & pl.col("departure").is_not_null()
    )
    repeated = (
        pp.filter(pl.col("stop").is_not_null() & pl.col("direction").is_not_null())
        .sort("vehicle", "stop", "arrival", "rid")
        .with_columns(
            *[
                pl.col(field).shift(1).over("vehicle", "stop").alias("prev_" + field)
                for field in ("direction", "arrival", "departure", "rid")
            ]
        )
        .filter(
            (pl.col("direction") != pl.col("prev_direction"))
            & (pl.col("arrival") < pl.col("prev_departure"))
            & (pl.col("departure") > pl.col("prev_arrival"))
        )
    )
    stratum = "shared_stop_opposite_direction_overlap"
    for row in repeated.to_dicts():
        pools[stratum].append(
            {
                "vehicle": row["vehicle"],
                "anchor": row["arrival"],
                "evidence": {
                    "apc_ids": [row["prev_rid"], row["rid"]],
                    "stop": row["stop"],
                    "directions": [row["prev_direction"], row["direction"]],
                    "overlap_seconds": (
                        min(row["departure"], row["prev_departure"])
                        - max(row["arrival"], row["prev_arrival"])
                    ).total_seconds(),
                },
            }
        )
    stratum = "dst_ambiguous_hour"
    for frame, source, fields in (
        (aa, "avl", ("time",)),
        (pp, "apc", ("arrival", "departure")),
    ):
        clocks = pl.concat(
            [
                frame.filter(pl.col("time_ambiguous")).select(
                    "vehicle", "rid", pl.col(field).dt.truncate("1h").alias("hour")
                )
                for field in fields
            ]
        )
        actual_ambiguous_hours = [
            hour
            for hour in clocks["hour"].drop_nulls().unique()
            if wall_time_status(hour) == "ambiguous"
        ]
        ambiguous = (
            clocks.filter(pl.col("hour").is_in(actual_ambiguous_hours))
            .unique(subset=["vehicle", "rid", "hour"])
            .group_by("vehicle", "hour")
            .agg(pl.len().alias("ambiguous_rows"), pl.col("rid").min().alias("rid"))
        )
        for row in ambiguous.to_dicts():
            pools[stratum].append(
                {
                    "vehicle": row["vehicle"],
                    "anchor": row["hour"] + timedelta(minutes=30),
                    "evidence": {
                        "source": source,
                        "ambiguous_rows": row["ambiguous_rows"],
                        "example_rid": row["rid"],
                        "hour": row["hour"],
                    },
                }
            )
    stratum = "month_end_apc_tail"
    if aa.height:
        last_day = aa["time"].max().date()
        tails = (
            aa.group_by("vehicle")
            .agg(pl.col("time").max().alias("last_avl"))
            .join(
                pp.group_by("vehicle").agg(
                    pl.col("departure").max().alias("last_apc_departure")
                ),
                on="vehicle",
            )
            .filter(
                (pl.col("last_avl").dt.date() == last_day)
                & (pl.col("last_apc_departure") > pl.col("last_avl"))
            )
        )
        for row in tails.to_dicts():
            pools[stratum].append(
                {
                    "vehicle": row["vehicle"],
                    "anchor": row["last_avl"],
                    "evidence": {
                        "dataset_final_avl_date": last_day,
                        "last_avl": row["last_avl"],
                        "last_apc_departure": row["last_apc_departure"],
                        "apc_tail_envelope_seconds": (
                            row["last_apc_departure"] - row["last_avl"]
                        ).total_seconds(),
                    },
                }
            )
    stratum = "internal_avl_gap_90_to_1800s"
    instants = (
        aa.group_by("vehicle", "time")
        .agg(
            pl.col("rid").min().alias("rid"),
            pl.any_horizontal("time_ambiguous", "time_nonexistent").any().alias("dst"),
        )
        .sort("vehicle", "time")
        .with_columns(
            *[
                pl.col(field).shift(1).over("vehicle").alias("prev_" + field)
                for field in ("time", "rid", "dst")
            ]
        )
        .with_columns(
            (pl.col("time") - pl.col("prev_time")).dt.total_seconds().alias("gap_s")
        )
        .filter(
            (pl.col("gap_s") > 90)
            & (pl.col("gap_s") <= 1800)
            & ~pl.col("dst")
            & ~pl.col("prev_dst")
        )
    )
    for row in instants.to_dicts():
        pools[stratum].append(
            {
                "vehicle": row["vehicle"],
                "anchor": row["prev_time"] + (row["time"] - row["prev_time"]) / 2,
                "evidence": {
                    "avl_ids": [row["prev_rid"], row["rid"]],
                    "gap_seconds": row["gap_s"],
                    "gap_start": row["prev_time"],
                    "gap_end": row["time"],
                },
            }
        )

    # Exact raw window counts/times are evaluated before algorithm execution.
    selected, status = [], {}
    for stratum, pool in pools.items():
        pool.sort(
            key=lambda r: (
                digest(f"{stratum}|{r['vehicle']}"),
                digest(f"{stratum}|{r['vehicle']}|{r['anchor'].isoformat()}"),
            )
        )
        chosen = []
        seen_vehicles = set()
        for row in pool:
            if row["vehicle"] in seen_vehicles:
                continue
            start, end = (
                row["anchor"] - timedelta(hours=1),
                row["anchor"] + timedelta(hours=1),
            )
            av = aa.filter(
                (pl.col("vehicle") == row["vehicle"])
                & (pl.col("time") >= start)
                & (pl.col("time") < end)
            )
            ap = pp.filter(
                (pl.col("vehicle") == row["vehicle"])
                & (pl.col("arrival") >= start)
                & (pl.col("arrival") < end)
            )
            if not av.height or not ap.height:
                continue
            # A month-end envelope may contain no actual APC event after AVL
            # ends within this 2h context; do not claim that as a tail example.
            if (
                stratum == "month_end_apc_tail"
                and not ap.filter(pl.col("departure") > row["anchor"]).height
            ):
                continue
            item = {
                "selection_stratum": stratum,
                "selection_reason": stratum,
                "raw_feature_evidence": row["evidence"],
                "vehicle": row["vehicle"],
                "vehicle_case": "V-" + digest(str(row["vehicle"])),
                "window_start": start,
                "window_end": end,
                "window_hash": digest(
                    f"{stratum}|{row['vehicle']}|{start.isoformat()}"
                ),
                "avl_rows": av.height,
                "apc_rows": ap.height,
                "avl_first": av["time"].min(),
                "avl_last": av["time"].max(),
                "apc_first": ap["arrival"].min(),
                "apc_last": ap["arrival"].max(),
            }
            item["window_case"] = (
                item["vehicle_case"] + "__" + item["window_hash"] + "__" + stratum
            )
            item["apc_prefix_cutoff"] = item["apc_first"] + timedelta(minutes=15)
            item["avl_suffix_cutoff"] = item["avl_last"] - timedelta(minutes=13)
            item["overlap_envelope_seconds"] = max(
                0,
                (
                    min(item["avl_last"], item["apc_last"])
                    - max(item["avl_first"], item["apc_first"])
                ).total_seconds(),
            )
            chosen.append(item)
            seen_vehicles.add(row["vehicle"])
            if len(chosen) == per_stratum:
                break
        status[stratum] = {
            "raw_candidates": len(pool),
            "requested_windows": per_stratum,
            "selected_windows": len(chosen),
            "not_selected_reason": None
            if len(chosen) == per_stratum
            else (
                "Insufficient distinct vehicles with this raw feature and both "
                "sources present in the centered 2h context."
            ),
        }
        selected.extend(chosen)
    return selected, status


def evaluate_window(avl: list[dict], apc: list[dict], config: dict) -> dict:
    """Evaluate the nine nominal combinations on exactly the supplied source rows."""
    support = build_support(avl, apc)
    result = {}
    for method in METHODS:
        seg = segment_vehicle(avl, apc, method)
        direct = direct_match(apc, support, seg, "spatiotemporal", config)
        for merge in MERGE_METHODS:
            if merge == "time_only":
                assignments = direct_match(apc, support, seg, merge, config)
            elif merge == "spatiotemporal":
                assignments = direct
            else:
                assignments = extend_sequences(apc, direct, seg, config)
            result[f"{method}__{merge}"] = {
                "segmentation": seg,
                "assignments": assignments,
            }
    return result


def _owners(result: dict, origins: dict[str, str] | None = None) -> dict[str, set[str]]:
    origins = origins or {}
    return {
        s["segment_id"]: {origins.get(rid, rid) for rid in s["ping_ids"]}
        for s in result["segmentation"]["segments"]
    }


def owner_partition(
    result: dict, origins: dict[str, str] | None = None
) -> set[frozenset]:
    """Projected owner membership sets; repeated uncertain singletons count once.

    Raw chunk counts remain separate diagnostics, including the extra isolated
    chunks intentionally created for duplicated unresolved DST observations.
    """
    return {frozenset(members) for members in _owners(result, origins).values()}


def _jaccard(a: set[str], b: set[str]) -> float | None:
    union = a | b
    return len(a & b) / len(union) if union else None


def compare_membership(
    before: dict, after: dict, *, origins: dict[str, str] | None = None
) -> tuple[dict, list[dict]]:
    """Compare source memberships, never equality of method-specific chunk IDs.

    The primary Jaccard projects both owners onto the surviving AVL lineage
    universe; the second includes removed source rows and describes lost support.
    Disjoint surviving memberships indicate an observed owner switch, not an
    established error. If no owner evidence survives, comparison abstains.
    """
    old_owners = _owners(before)
    new_owners = _owners(after, origins)
    old_universe = set().union(*old_owners.values()) if old_owners else set()
    new_universe = set().union(*new_owners.values()) if new_owners else set()
    common = old_universe & new_universe
    previous = {r["apc_rid"]: r for r in before["assignments"]}
    counts = collections.Counter()
    transitions = collections.Counter()
    details = []
    for row in sorted(after["assignments"], key=lambda r: r["apc_rid"]):
        old = previous.get(row["apc_rid"])
        if old is None:
            counts["new_apc_not_in_reference"] += 1
            continue
        counts["retained_apc"] += 1
        transitions[f"{old['status']}->{row['status']}"] += 1
        old_assigned = old["status"] in ASSIGNED_STATUSES and bool(old["segment_id"])
        new_assigned = row["status"] in ASSIGNED_STATUSES and bool(row["segment_id"])
        counts["reference_assigned"] += old_assigned
        counts["perturbed_assigned"] += new_assigned
        counts["reference_direct"] += old["status"] == "direct"
        counts["reference_assigned_then_abstained"] += old_assigned and not new_assigned
        counts["reference_unassigned_then_assigned"] += (
            not old_assigned and new_assigned
        )
        counts["direct_to_inferred"] += (
            old["status"] == "direct" and row["status"] == "inferred"
        )
        old_set = old_owners.get(old.get("segment_id"), set())
        new_set = new_owners.get(row.get("segment_id"), set())
        a, b = old_set & common, new_set & common
        comparable = bool(old_assigned and new_assigned and a and b)
        jaccard = _jaccard(a, b) if comparable else None
        full_jaccard = (
            _jaccard(old_set, new_set) if old_assigned and new_assigned else None
        )
        if old_assigned and new_assigned:
            counts["both_assigned"] += 1
            counts["owner_support_not_comparable"] += not comparable
        if comparable:
            counts["comparable_owner_memberships"] += 1
            counts["unchanged_owner_membership"] += a == b
            counts["partially_changed_owner_membership"] += bool(a & b) and a != b
            counts["disjoint_owner_switch"] += not bool(a & b)
            counts["direct_to_inferred_same_membership"] += (
                old["status"] == "direct" and row["status"] == "inferred" and a == b
            )
        details.append(
            {
                "apc_rid": row["apc_rid"],
                "reference_status": old["status"],
                "perturbed_status": row["status"],
                "reference_owner": old["segment_id"],
                "perturbed_owner": row["segment_id"],
                "owner_comparable": comparable,
                "surviving_owner_membership_jaccard": jaccard,
                "full_owner_membership_jaccard": full_jaccard,
                "selected_ping_support_jaccard": _jaccard(
                    set(old.get("ping_ids", [])),
                    {(origins or {}).get(rid, rid) for rid in row.get("ping_ids", [])},
                ),
                "disjoint_owner_switch": comparable and not bool(a & b),
                "reference_reason": old["reason"],
                "perturbed_reason": row["reason"],
            }
        )
    summary = dict(counts)
    summary["status_transitions"] = dict(transitions)
    js = [
        r["surviving_owner_membership_jaccard"]
        for r in details
        if r["surviving_owner_membership_jaccard"] is not None
    ]
    summary["membership_jaccard_sum"] = sum(js)
    summary["membership_jaccard_count"] = len(js)
    summary["mean_surviving_owner_membership_jaccard"] = (
        sum(js) / len(js) if js else None
    )
    summary["reference_chunk_count"] = len(old_owners)
    summary["perturbed_chunk_count"] = len(new_owners)
    return summary, details


def perturbations(avl: list[dict], apc: list[dict], selection: dict) -> dict:
    prefix = selection["apc_prefix_cutoff"]
    suffix = selection["avl_suffix_cutoff"]
    masked_apc = [r for r in apc if r.get("arrival") is None or r["arrival"] >= prefix]
    masked_avl = [r for r in avl if r.get("time") is None or r["time"] <= suffix]
    start = selection["window_start"]
    midpoint = start + timedelta(hours=1)
    variants = {
        "apc_prefix_15m": (avl, masked_apc),
        "avl_suffix_13m": (masked_avl, apc),
        "offset_coverage_15m_13m": (masked_avl, masked_apc),
        "avl_internal_gap_5m": (
            [
                r
                for r in avl
                if r.get("time") is None
                or abs((r["time"] - midpoint).total_seconds()) > 150
            ],
            apc,
        ),
        "no_overlap_45m_separation": (
            [
                r
                for r in avl
                if r.get("time") is not None
                and r["time"] < start + timedelta(minutes=45)
            ],
            [
                r
                for r in apc
                if r.get("arrival") is not None
                and r["arrival"] >= start + timedelta(minutes=90)
            ],
        ),
    }
    for seconds in (-120, -60, 60, 120):
        shifted = [
            r
            | {
                "arrival": r["arrival"] + timedelta(seconds=seconds)
                if r.get("arrival") is not None
                else None,
                "departure": r["departure"] + timedelta(seconds=seconds)
                if r.get("departure") is not None
                else None,
            }
            for r in apc
        ]
        for row in shifted:
            clock_states = {
                wall_time_status(row[field])
                for field in ("arrival", "departure")
                if row.get(field) is not None
            }
            row["time_ambiguous"] = row.get("time_ambiguous", False) or (
                "ambiguous" in clock_states
            )
            row["time_nonexistent"] = row.get("time_nonexistent", False) or (
                "nonexistent" in clock_states
            )
        variants[f"apc_clock_{seconds:+d}s"] = (avl, shifted)
    variants["exact_avl_duplication"] = (
        avl + [r | {"rid": str(r["rid"]) + ":probe_duplicate"} for r in avl],
        apc,
    )
    return variants


def canonical_result(result: dict) -> str:
    """Ignore caller-controlled APC list order; retain every computed decision."""
    normalized = {
        "segmentation": result["segmentation"],
        "assignments": sorted(result["assignments"], key=lambda r: r["apc_rid"]),
    }
    return json.dumps(normalized, sort_keys=True, default=str, allow_nan=False)


def aggregate_summaries(records: list[dict]) -> dict:
    grouped = collections.defaultdict(list)
    for row in records:
        grouped[f"{row['combination']}__{row['probe']}"].append(row)
    result = {}
    for key, group in grouped.items():
        combined = collections.Counter()
        transitions = collections.Counter()
        for row in group:
            for field, value in row.items():
                if field == "status_transitions":
                    transitions.update(value)
                elif isinstance(value, (int, float)) and field != (
                    "mean_surviving_owner_membership_jaccard"
                ):
                    combined[field] += value
        result[key] = dict(combined)
        count = combined["membership_jaccard_count"]
        result[key]["mean_surviving_owner_membership_jaccard"] = (
            combined["membership_jaccard_sum"] / count if count else None
        )
        result[key]["status_transitions"] = dict(transitions)
        result[key]["vehicle_windows"] = len(group)
        result[key]["accuracy"] = None
    return result


def run(args) -> None:
    os.umask(0o077)
    out = args.out.resolve()
    ttc = Path(__file__).resolve().parents[2]
    allowed = (ttc / "artifacts" / "apc-avl-coverage-study-20260908").resolve()
    if not out.is_relative_to(allowed) or out == allowed:
        raise ValueError("Probe outputs require an isolated new study subdirectory")
    if out.exists():
        raise FileExistsError(f"Refusing to overwrite existing probe run: {out}")
    out.mkdir(parents=True, mode=0o700)
    started = time.perf_counter()
    audit = args.audit.resolve()
    # Selection scans only source identities/times before any method evaluation.
    edge_mode = args.selection_mode == "edge"
    avl_columns = ["vehicle", "time"]
    apc_columns = ["vehicle", "arrival"]
    if edge_mode:
        avl_columns += ["rid", "time_ambiguous", "time_nonexistent"]
        apc_columns += [
            "rid",
            "departure",
            "stop",
            "direction",
            "time_ambiguous",
            "time_nonexistent",
        ]
    avl_index = pl.read_parquet(audit / "avl.parquet", columns=avl_columns)
    apc_index = pl.read_parquet(audit / "apc.parquet", columns=apc_columns)
    if edge_mode:
        selected, selection_status = select_edge_windows(
            avl_index, apc_index, args.edge_per_stratum
        )
        _dump(out / "selection_status.json", selection_status)
    else:
        selected = select_windows(avl_index, apc_index, args.vehicles)
    _dump(out / "selection.json", selected)
    del avl_index, apc_index
    study_config = json.loads(Path(__file__).with_name("study_config.json").read_text())
    config = DEFAULTS | study_config["common"] | study_config["regimes"]["nominal"]
    _dump(
        out / "manifest.json",
        {
            "selection_sha256": hashlib.sha256(
                (out / "selection.json").read_bytes()
            ).hexdigest(),
            "code_sha256": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(__file__).parent.glob("*.py"))
            },
            "selected_vehicle_windows": len(selected),
            "requested_vehicles": args.vehicles,
            "selection_mode": args.selection_mode,
            "nominal_config": config,
            "window_seconds": WINDOW_SECONDS,
            "eligibility": (
                "Centered 2h raw-feature windows: shared-stop opposite-direction "
                "overlapping APC intervals; DST ambiguous hour; final-calendar-day "
                "APC tail after AVL; 90–1800s internal AVL gap excluding DST. "
                "Require both sources present; distinct vehicles per stratum. "
                "Selection uses raw fields only, not algorithm outputs."
            )
            if edge_mode
            else (
                "Fixed 2h windows, >=60 AVL/12 APC rows; each source span >=90min; "
                "envelope overlap >=60min. Raw presence only, not matching outcomes."
            ),
            "selection_order": (
                "Within each stratum, sha256(stratum|vehicle)[:16], then "
                "sha256(stratum|vehicle|ISO-anchor)[:16]."
            )
            if edge_mode
            else (
                "Vehicle sha256(str(vehicle))[:16], "
                "then window sha256(vehicle|ISO-start)[:16]."
            ),
            "selection_frozen_before_methods": True,
            "masking": (
                "Remove APC arrivals in first15min of APC observed span; remove AVL "
                "pings in final13min of AVL observed span. Source intervals are "
                "never clipped."
            ),
            "window_censorship": (
                "All records inside each fixed2h window are retained before masking; "
                "surrounding vehicle history is excluded. Window endpoints are "
                "not trip endpoints."
            ),
            "comparison": (
                "Owner source-ping membership Jaccard projected to surviving AVL "
                "lineage; full membership Jaccard also saved. "
                "Algorithm reference is not truth."
            ),
            "thresholds_calibrated": False,
            "independent_ground_truth": False,
            "gtfs_used": False,
            "human_labels_used": False,
            "publishable": False,
        },
    )
    all_summaries, checks = [], []
    for index, selection in enumerate(selected, 1):
        case = selection.get("window_case", selection["vehicle_case"])
        frames = []
        for source, clock in (("avl", "time"), ("apc", "arrival")):
            frames.append(
                pl.scan_parquet(audit / f"{source}.parquet")
                .filter(
                    (pl.col("vehicle") == selection["vehicle"])
                    & (pl.col(clock) >= selection["window_start"])
                    & (pl.col(clock) < selection["window_end"])
                )
                .sort(clock, "rid")
                .collect()
                .to_dicts()
            )
        avl, apc = frames
        reference = evaluate_window(avl, apc, config)
        perm_avl, perm_apc = list(avl), list(apc)
        rng = random.Random(int(selection["window_hash"], 16))
        rng.shuffle(perm_avl)
        rng.shuffle(perm_apc)
        permuted = evaluate_window(perm_avl, perm_apc, config)
        for combination, original in reference.items():
            checks.append(
                {
                    "vehicle_case": case,
                    "combination": combination,
                    "check": "input_permutation",
                    "passed": canonical_result(original)
                    == canonical_result(permuted[combination]),
                }
            )
        for method in ("gap_only", "avl_context"):
            reduced = segment_vehicle(avl, [], method)
            full = reference[f"{method}__spatiotemporal"]["segmentation"]
            checks.append(
                {
                    "vehicle_case": case,
                    "combination": method,
                    "check": "apc_removal_avl_only_segmentation",
                    "passed": full == reduced,
                }
            )
        for probe, (changed_avl, changed_apc) in perturbations(
            avl, apc, selection
        ).items():
            changed = evaluate_window(changed_avl, changed_apc, config)
            origins = (
                {str(r["rid"]) + ":probe_duplicate": str(r["rid"]) for r in avl}
                if probe == "exact_avl_duplication"
                else None
            )
            for combination, result in changed.items():
                summary, details = compare_membership(
                    reference[combination], result, origins=origins
                )
                summary.update(
                    vehicle_case=selection["vehicle_case"],
                    window_case=case,
                    selection_stratum=selection.get("selection_stratum", "general"),
                    combination=combination,
                    probe=probe,
                    reference_avl_rows=len(avl),
                    retained_avl_rows=len(changed_avl),
                    reference_apc_rows=len(apc),
                    retained_apc_rows=len(changed_apc),
                )
                all_summaries.append(summary)
                _save_rows(
                    out / "details" / probe / combination / f"{case}.parquet", details
                )
                if probe == "no_overlap_45m_separation":
                    checks.append(
                        {
                            "vehicle_case": case,
                            "combination": combination,
                            "check": "no_overlap_abstains",
                            "passed": summary.get("perturbed_assigned", 0) == 0,
                        }
                    )
                if probe == "exact_avl_duplication":
                    previous = reference[combination]
                    # Chunk IDs and duplicate row IDs are intentionally ignored.
                    before_owners = owner_partition(previous)
                    after_owners = owner_partition(result, origins)
                    checks.append(
                        {
                            "vehicle_case": case,
                            "combination": combination,
                            "check": "exact_avl_duplication_owner_partition",
                            "passed": before_owners == after_owners,
                        }
                    )
        _dump(out / "checks_progress.json", checks)
        _dump(out / "summaries_progress.json", all_summaries)
        print(
            json.dumps(
                {
                    "completed_windows": index,
                    "total_windows": len(selected),
                    "elapsed_s": round(time.perf_counter() - started, 2),
                }
            ),
            flush=True,
        )
    _dump(out / "probe_summary.json", aggregate_summaries(all_summaries))
    if edge_mode:
        _dump(
            out / "by_stratum_summary.json",
            {
                stratum: aggregate_summaries(
                    [
                        row
                        for row in all_summaries
                        if row["selection_stratum"] == stratum
                    ]
                )
                for stratum in sorted(
                    {row["selection_stratum"] for row in all_summaries}
                )
            },
        )
    _dump(out / "per_window_summary.json", all_summaries)
    _dump(out / "checks.json", checks)
    failed = [r for r in checks if not r["passed"]]
    _dump(
        out / "completion.json",
        {
            "vehicle_windows": len(selected),
            "elapsed_seconds": time.perf_counter() - started,
            "probe_combinations": len(all_summaries),
            "checks": len(checks),
            "failed_checks": failed,
            "accuracy_claimed": False,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--vehicles", type=int, default=12)
    parser.add_argument(
        "--selection-mode", choices=("general", "edge"), default="general"
    )
    parser.add_argument("--edge-per-stratum", type=int, default=2)
    run(parser.parse_args())
