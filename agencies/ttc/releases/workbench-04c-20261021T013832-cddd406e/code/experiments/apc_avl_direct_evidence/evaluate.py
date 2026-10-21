"""Paired, full-fixture evaluation against frozen observation-level links.

This experiment consumes frozen chunks; it never segments vehicles again and
does not use schedules, reviewed labels, or infer actual performed-trip IDs.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import platform
import resource
import time
from datetime import datetime
from pathlib import Path

import polars as pl

METHODS = ("gap_only", "avl_context", "apc_assisted")
VERSION = "direct-evidence-paired-evaluation-v1"


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False) + "\n")
    path.chmod(0o600)


def save_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if rows:
        pl.from_dicts(rows, infer_schema_length=None).write_parquet(path)
        path.chmod(0o600)


def read_rows(path: Path) -> list[dict]:
    return pl.read_parquet(path).to_dicts() if path.exists() else []


def baseline_eligible(row: dict) -> bool:
    """Freeze the first study's temporal/dwell eligibility, not v2 eligibility."""
    arrival, departure = row.get("arrival"), row.get("departure")
    return bool(
        arrival is not None
        and departure is not None
        and not row.get("invalid")
        and not row.get("time_ambiguous")
        and not row.get("time_nonexistent")
        and 0 <= (departure - arrival).total_seconds() <= 1800
    )


def load_frozen_segments(path: Path) -> dict:
    segments = read_rows(path)
    mapping = {}
    for segment in segments:
        for rid in segment["ping_ids"]:
            if rid in mapping:
                raise ValueError("Frozen segmentation has repeated AVL membership")
            mapping[rid] = segment["segment_id"]
    return {"segments": segments, "ping_segment": mapping}


def source_strata(row: dict) -> list[str]:
    tags = ["all"]
    for field in ("grain_uncertain", "time_ambiguous", "time_nonexistent", "invalid"):
        if row.get(field):
            tags.append(field)
    arrival, departure = row.get("arrival"), row.get("departure")
    if arrival is not None and departure is not None:
        duration = (departure - arrival).total_seconds()
        if duration == 0:
            tags.append("zero_dwell")
        elif 0 < duration <= 30:
            tags.append("dwell_1_to_30s")
        elif 30 < duration <= 300:
            tags.append("dwell_31_to_300s")
        elif duration > 300:
            tags.append("dwell_over_300s")
        if arrival.date() != departure.date():
            tags.append("cross_midnight_interval")
    return tags


def transition(old: dict, new: dict) -> str:
    old_direct, new_direct = (
        old.get("status") == "direct",
        new.get("status") == "direct",
    )
    if old_direct and new_direct:
        owners = set(new.get("segment_ids", []))
        if owners == {old.get("segment_id")}:
            return "direct_retained_same_single_chunk"
        if old.get("segment_id") in owners:
            return "direct_old_chunk_in_multiple_contexts"
        if not owners:
            return "direct_new_context_unmapped"
        return "direct_changed_chunk"
    if old_direct:
        return "old_direct_to_" + str(new.get("status"))
    if new_direct:
        return "old_" + str(old.get("status")) + "_to_direct"
    return "neither_direct"


def finite_quantiles(values: list) -> dict:
    clean = sorted(float(v) for v in values if v is not None and math.isfinite(v))
    if not clean:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "n": len(clean),
        **{
            name: clean[min(len(clean) - 1, int((len(clean) - 1) * q))]
            for name, q in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
        },
        "max": clean[-1],
    }


def combine_metrics(rows: list[dict]) -> dict:
    """Combine vehicle metrics without averaging per-vehicle percentages."""
    out = {}
    for row in rows:
        for key, value in row.items():
            if isinstance(value, dict):
                target = out.setdefault(key, collections.Counter())
                target.update(value)
            elif isinstance(value, (int, float)):
                out[key] = out.get(key, 0) + value
    for key, value in tuple(out.items()):
        if isinstance(value, collections.Counter):
            out[key] = dict(value)
    total, common = out.get("apc_rows", 0), out.get("common_eligible_apc_rows", 0)
    for label in ("baseline", "new"):
        count = out.get(label + "_statuses", {}).get("direct", 0)
        out[label + "_direct_rate_all_rows"] = count / total if total else None
        common_count = out.get(label + "_common_eligible_direct", 0)
        out[label + "_direct_rate_common_eligible"] = (
            common_count / common if common else None
        )
    out.update(accuracy=None, actual_trip_accuracy=None, publishable=False)
    return out


def time_coordinate(value) -> float | None:
    if isinstance(value, datetime):
        return (value - datetime(1970, 1, 1)).total_seconds()
    return float(value) if value is not None else None


def coordinate_distance(a: dict, b: dict) -> float | None:
    coords = [a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")]
    if any(v is None or not math.isfinite(v) for v in coords):
        return None
    rad = math.pi / 180
    y = (coords[2] - coords[0]) * rad
    x = (coords[3] - coords[1]) * rad * math.cos((coords[0] + coords[2]) * rad / 2)
    return 6371008.8 * math.hypot(x, y)


def validate_assignments(avl, apc, segmentation, assignments, config) -> dict:
    """Independently recompute source-reference and elementary witness bounds.

    This is not a second implementation of episode discovery: continuity,
    episode uniqueness, and score calibration need independent labels/probes.
    """
    sources = {r["rid"]: r for r in apc}
    pings = {r["rid"]: r for r in avl}
    observed_times = [
        time_coordinate(p["time"])
        for p in avl
        if p.get("time") is not None
        and not p.get("invalid")
        and not p.get("time_ambiguous")
        and not p.get("time_nonexistent")
    ]
    envelope = (min(observed_times), max(observed_times)) if observed_times else None
    mapping = segmentation["ping_segment"]
    owners = {s["segment_id"] for s in segmentation["segments"]}
    violations = collections.Counter(
        apc_count_difference=abs(len(apc) - len(assignments)),
        duplicate_apc_dispositions=len(assignments)
        - len({r["apc_rid"] for r in assignments}),
        missing_apc_dispositions=len(
            set(sources) - {r["apc_rid"] for r in assignments}
        ),
        extra_apc_dispositions=len({r["apc_rid"] for r in assignments} - set(sources)),
    )
    examined = collections.Counter()
    for result in assignments:
        source = sources.get(result["apc_rid"])
        if source is None:
            continue
        if result.get("clock_corrected"):
            violations["unexpected_clock_correction"] += 1
        if result.get("status") == "inferred":
            violations["unexpected_inferred_disposition"] += 1
        supported = result.get("status") in {"direct", "partial"}
        if supported:
            examined["supported_apc"] += 1
            if not baseline_eligible(source):
                violations["supported_apc_outside_common_temporal_eligibility"] += 1
            if not result.get("witnesses") or not result.get("ping_ids"):
                violations["supported_without_witness_lineage"] += 1
            if result.get("support_level") is None:
                violations["supported_without_scope"] += 1
            if result.get("status") == "direct":
                if result.get("support_level") not in {"instant", "bounded_interval"}:
                    violations["direct_with_partial_scope"] += 1
                targets = {w.get("target") for w in result.get("witnesses", [])}
                if result.get("support_level") == "bounded_interval" and not {
                    "arrival",
                    "departure",
                }.issubset(targets):
                    violations["bounded_interval_missing_endpoint_witness"] += 1
                if result.get("support_level") == "instant" and (
                    source.get("arrival") != source.get("departure")
                ):
                    violations["instant_scope_for_nonzero_interval"] += 1
        assigned_owners = set(result.get("segment_ids", []))
        if assigned_owners - owners:
            violations["unknown_segment_context"] += 1
        if result.get("segment_id") is not None and assigned_owners != {
            result["segment_id"]
        }:
            violations["single_owner_disagrees_with_owner_set"] += 1
        all_witness_pings = set()
        for witness in result.get("witnesses", []):
            examined["witnesses"] += 1
            ids = witness.get("ping_ids", [])
            all_witness_pings.update(ids)
            if not ids or any(rid not in pings for rid in ids):
                violations["witness_missing_source_ping"] += 1
                continue
            actual = [pings[rid] for rid in ids]
            if any(p.get("vehicle") != source.get("vehicle") for p in actual):
                violations["witness_vehicle_mismatch"] += 1
            if any(
                p.get("time") is None
                or p.get("invalid")
                or p.get("time_ambiguous")
                or p.get("time_nonexistent")
                for p in actual
            ):
                violations["witness_invalid_or_ambiguous_time"] += 1
                continue
            if any(
                p.get("lat") is None
                or p.get("lon") is None
                or not math.isfinite(p["lat"])
                or not math.isfinite(p["lon"])
                or not -90 <= p["lat"] <= 90
                or not -180 <= p["lon"] <= 180
                or (p["lat"], p["lon"]) == (0, 0)
                for p in actual
            ):
                violations["witness_invalid_source_position"] += 1
                continue
            support_time = time_coordinate(witness.get("support_time"))
            target_time = time_coordinate(witness.get("target_time"))
            if support_time is None or target_time is None:
                violations["witness_missing_temporal_evidence"] += 1
                continue
            target = witness.get("target")
            if target in {"arrival", "departure"} and not math.isclose(
                target_time, time_coordinate(source[target]), abs_tol=1e-6
            ):
                violations["witness_target_not_source_endpoint"] += 1
            if envelope is None or not envelope[0] <= target_time <= envelope[1]:
                violations["witness_target_outside_observed_time_envelope"] += 1
            gap = abs(support_time - target_time)
            if gap > config["time_tolerance_s"] + 1e-6:
                violations["witness_outside_time_tolerance"] += 1
            kind = witness.get("kind")
            if kind == "point":
                # Exact-duplicate source IDs may all be retained as lineage.
                coordinates = {(p["time"], p.get("lat"), p.get("lon")) for p in actual}
                if len(coordinates) != 1:
                    violations["point_witness_combines_distinct_measurements"] += 1
                point = actual[0]
                if abs(time_coordinate(point["time"]) - support_time) > 1e-6:
                    violations["point_support_time_not_observed"] += 1
                position = point
            elif kind == "interpolation":
                examined["interpolation_witnesses"] += 1
                unique = {
                    (time_coordinate(p["time"]), p.get("lat"), p.get("lon"))
                    for p in actual
                }
                nodes = sorted(unique)
                if len(nodes) != 2:
                    violations["interpolation_without_two_distinct_nodes"] += 1
                    continue
                (t0, lat0, lon0), (t1, lat1, lon1) = nodes
                if not t0 < support_time < t1:
                    violations["interpolation_extrapolates_or_uses_endpoint"] += 1
                if t1 - t0 > config["interpolation_gap_s"] + 1e-6:
                    violations["interpolation_bracket_over_limit"] += 1
                witness_owners = {mapping.get(rid) for rid in ids}
                if len(witness_owners) != 1 or None in witness_owners:
                    violations["interpolation_crosses_frozen_chunk"] += 1
                if t1 <= t0:
                    violations["interpolation_nonpositive_span"] += 1
                    continue
                fraction = (support_time - t0) / (t1 - t0)
                position = {
                    "lat": lat0 + fraction * (lat1 - lat0),
                    "lon": lon0 + fraction * (lon1 - lon0),
                }
                travel = coordinate_distance(
                    {"lat": lat0, "lon": lon0}, {"lat": lat1, "lon": lon1}
                )
                if travel / (t1 - t0) * 3.6 > config.get("max_speed_kph", 130) + 1e-6:
                    violations["interpolation_implausible_speed"] += 1
            else:
                violations["unknown_witness_kind"] += 1
                continue
            residual = coordinate_distance(source, position)
            if witness.get("lat") is not None and witness.get("lon") is not None:
                if coordinate_distance(witness, position) > 0.05:
                    violations[
                        "witness_coordinates_disagree_with_source_derivation"
                    ] += 1
            if residual is None or residual > config["radius_m"] + 1e-6:
                violations["witness_outside_spatial_radius"] += 1
            declared_distance = witness.get("distance_m")
            if (
                residual is not None
                and declared_distance is not None
                and not math.isclose(
                    residual, declared_distance, rel_tol=1e-5, abs_tol=0.05
                )
            ):
                violations["declared_spatial_residual_mismatch"] += 1
            if witness.get("gap_s") is not None and not math.isclose(
                gap, witness["gap_s"], abs_tol=1e-6
            ):
                violations["declared_temporal_residual_mismatch"] += 1
        if set(result.get("ping_ids", [])) != all_witness_pings:
            violations["assignment_witness_lineage_mismatch"] += 1
    return {"violations": dict(violations), "examined": dict(examined)}


def summarize_vehicle(avl, apc, segmentation, baseline, assignments, config):
    old = {r["apc_rid"]: r for r in baseline}
    new = {r["apc_rid"]: r for r in assignments}
    expected = {r["rid"] for r in apc}
    if len(old) != len(baseline) or set(old) != expected:
        raise ValueError("Frozen baseline does not conserve the selected APC source")
    if len(new) != len(assignments) or set(new) != expected:
        raise ValueError("New matcher does not conserve the selected APC source")
    counts = {
        "apc_rows": len(apc),
        "avl_rows": len(avl),
        "common_eligible_apc_rows": sum(baseline_eligible(r) for r in apc),
        "baseline_statuses": collections.Counter(r["status"] for r in baseline),
        "new_statuses": collections.Counter(r["status"] for r in assignments),
        "new_support_levels": collections.Counter(
            r.get("support_level") or "none" for r in assignments
        ),
        "new_reasons": collections.Counter(r["reason"] for r in assignments),
        "new_context_statuses": collections.Counter(
            r.get("context_status") or "none" for r in assignments
        ),
        "new_route_states": collections.Counter(
            r.get("route_state") or "none" for r in assignments
        ),
        "paired_transitions": collections.Counter(),
        "paired_statuses": collections.Counter(),
        "stratified_new_statuses": collections.Counter(),
        "stratified_paired_transitions": collections.Counter(),
        "overlap_flags": collections.Counter(),
        "baseline_common_eligible_direct": 0,
        "new_common_eligible_direct": 0,
        "new_common_eligible_partial": 0,
        "newly_ineligible_on_common_input": 0,
        "same_chunk_multi_episode_ambiguities": 0,
        "direct_route_conflicts": 0,
        "partial_route_conflicts": 0,
        "direct_multiple_frozen_chunks": 0,
        "any_candidate_local_support": 0,
        "coverage_start_missing": 0,
        "coverage_end_missing": 0,
        "interval_excursion": 0,
        "endpoint_conflict_rows": 0,
        "accepted_multiple_candidate_episodes": 0,
        "candidate_rows": 0,
    }
    paired = []
    for source in apc:
        a, b = old[source["rid"]], new[source["rid"]]
        category = transition(a, b)
        counts["paired_transitions"][category] += 1
        counts["paired_statuses"][a["status"] + " -> " + b["status"]] += 1
        common = baseline_eligible(source)
        counts["baseline_common_eligible_direct"] += common and a["status"] == "direct"
        counts["new_common_eligible_direct"] += common and b["status"] == "direct"
        counts["new_common_eligible_partial"] += common and b["status"] == "partial"
        counts["newly_ineligible_on_common_input"] += (
            common and b["status"] == "ineligible"
        )
        candidates = b.get("candidates", [])
        counts["candidate_rows"] += len(candidates)
        counts["any_candidate_local_support"] += bool(candidates)
        for flag in (
            "coverage_start_missing",
            "coverage_end_missing",
            "interval_excursion",
        ):
            counts[flag] += bool(b.get(flag))
        counts["endpoint_conflict_rows"] += bool(b.get("endpoint_conflicts"))
        counts["accepted_multiple_candidate_episodes"] += (
            b["status"] in {"direct", "partial"} and len(candidates) > 1
        )
        candidate_contexts = set()
        for candidate in candidates:
            candidate_contexts.update(candidate.get("segment_ids", []))
        counts["same_chunk_multi_episode_ambiguities"] += (
            b["status"] == "ambiguous"
            and b.get("episode_count", 0) > 1
            and len(candidate_contexts) == 1
        )
        counts["direct_route_conflicts"] += b["status"] == "direct" and bool(
            b.get("route_conflict")
        )
        counts["partial_route_conflicts"] += b["status"] == "partial" and bool(
            b.get("route_conflict")
        )
        counts["direct_multiple_frozen_chunks"] += (
            b["status"] == "direct" and len(b.get("segment_ids", [])) > 1
        )
        counts["overlap_flags"].update(b.get("overlap_flags", []))
        for tag in source_strata(source):
            counts["stratified_new_statuses"][tag + " | " + b["status"]] += 1
            counts["stratified_paired_transitions"][tag + " | " + category] += 1
        paired.append(
            {
                "apc_rid": source["rid"],
                "common_eligible": common,
                "baseline_status": a["status"],
                "baseline_reason": a["reason"],
                "baseline_segment_id": a.get("segment_id"),
                "baseline_distance_m": a.get("distance_m"),
                "baseline_gap_s": a.get("gap_s"),
                "baseline_route_conflict": a.get("route_conflict"),
                "new_status": b["status"],
                "new_reason": b["reason"],
                "new_support_level": b.get("support_level"),
                "new_segment_id": b.get("segment_id"),
                "new_segment_ids": b.get("segment_ids", []),
                "new_context_status": b.get("context_status"),
                "new_route_state": b.get("route_state"),
                "new_episode_count": b.get("episode_count", 0),
                "new_candidate_count": b.get("candidate_count", 0),
                "new_distance_m": b.get("distance_m"),
                "new_gap_s": b.get("gap_s"),
                "transition": category,
                "grain_uncertain": bool(source.get("grain_uncertain")),
            }
        )
    checks = validate_assignments(avl, apc, segmentation, assignments, config)
    counts["invariant_violations"] = checks["violations"]
    counts["invariant_examined"] = checks["examined"]
    return counts, paired


def report_text(summary: dict, completion: dict) -> str:
    lines = [
        "# Direct evidence paired evaluation",
        "",
        "All comparisons use the same APC records and the three previously frozen "
        "segmentation maps. No new segmentation, GTFS schedule, reviewed labels, "
        "clock correction, or source data rewriting was used.",
        "",
        "`direct` means instant or bounded-interval support; `partial` means only "
        "arrival, departure, or interior evidence. Neither status certifies a real "
        "performed trip, true dwell, passenger-event grain, or complete visit "
        "sequence.",
        "",
        "| Frozen context | APC rows | Common eligible | Old direct | New direct | "
        "New partial | New ambiguous | New unmatched | New ineligible |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for method, metric in summary.items():
        new = metric["new_statuses"]
        lines.append(
            f"| {method} | {metric['apc_rows']:,} | "
            f"{metric['common_eligible_apc_rows']:,} | "
            f"{metric['baseline_statuses'].get('direct', 0):,} | "
            + " | ".join(
                f"{new.get(status, 0):,}"
                for status in (
                    "direct",
                    "partial",
                    "ambiguous",
                    "unmatched",
                    "ineligible",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Rates are association/support coverage, not accuracy. New direct is "
            "deliberately a narrower statement than old direct; direct-plus-partial "
            "is the more appropriate local-evidence coverage comparison.",
            "",
            "## Evidence and integrity",
            "",
            "`assignments/` retains all returned candidates and chosen witnesses. "
            "`paired/` retains one same-source comparison per APC row. "
            "`summary.json` includes support levels, reasons, context/route states, "
            "overlap warnings, common-denominator rates, and per-rule violations.",
            "",
            f"Wall time: {completion['wall_seconds']:.1f} s; peak RSS: "
            f"{completion['peak_rss_mib']:.1f} MiB. The per-vehicle AVL index and "
            "prepared candidates were reused across all three frozen context maps.",
            "",
            "Independent checks recompute source conservation, source/witness "
            "references, same-vehicle lineage, elementary temporal/spatial residuals, "
            "interpolation bracketing and frozen-context constraints, and declared "
            "endpoint scope. They do not establish true episode identity or calibrated "
            "confidence. Source-clock alignment, route/direction namespaces, "
            "and APC source-grain semantics remain unverified.",
            "",
            "No production TIDES publication or schema-conformance claim is made.",
            "",
        ]
    )
    return "\n".join(lines)


def run(args) -> None:
    from .matcher import DEFAULT_CONFIG, build_index, match_direct, prepare_candidates

    os.umask(0o077)
    started = time.perf_counter()
    ttc = Path(__file__).resolve().parents[2]
    allowed = (ttc / "artifacts" / "apc-avl-direct-evidence-20260908").resolve()
    out = args.out.resolve()
    if out == allowed or not out.is_relative_to(allowed):
        raise ValueError(
            "Outputs must be in a new subdirectory of the isolated artifact root"
        )
    if out.exists():
        raise FileExistsError(f"Refusing to overwrite existing run: {out}")
    if args.limit < 0:
        raise ValueError("--limit must be nonnegative")
    frozen = args.baseline.resolve()
    audit = args.audit.resolve()
    old_completion = json.loads((frozen / "completion.json").read_text())
    if not old_completion.get("full_fixture"):
        raise ValueError("Comparison requires a completed full-fixture frozen baseline")
    old_manifest = json.loads((frozen / "manifest.json").read_text())
    canonical_hashes = {
        name: file_hash(audit / name)
        for name in ("avl.parquet", "apc.parquet", "inventory.json")
    }
    if canonical_hashes != old_manifest["canonical_input_sha256"]:
        raise ValueError("Canonical inputs differ from the frozen experiment")
    config = dict(DEFAULT_CONFIG)
    if args.config is not None:
        config.update(json.loads(args.config.read_text()))
    package = Path(__file__).parent
    code_hashes = {p.name: file_hash(p) for p in sorted(package.glob("*.py"))}
    avl = pl.read_parquet(audit / "avl.parquet")
    apc = pl.read_parquet(audit / "apc.parquet")
    selection = json.loads((frozen / "selection.json").read_text())
    selected = selection[: args.limit] if args.limit else selection
    vehicles = {r["raw_vehicle"] for r in selection}
    source_vehicles = set(avl["vehicle"].to_list()) | set(apc["vehicle"].to_list())
    if vehicles != source_vehicles:
        raise ValueError(
            "Frozen vehicle namespace does not equal the canonical input namespace"
        )
    out.mkdir(parents=True, mode=0o700)
    dump(out / "selection.json", selected)
    dump(out / "config.json", config)
    dump(
        out / "manifest.json",
        {
            "version": VERSION,
            "created_at": datetime.now().isoformat(),
            "canonical_input_sha256": canonical_hashes,
            "code_sha256": code_hashes,
            "baseline_manifest_sha256": file_hash(frozen / "manifest.json"),
            "baseline": str(frozen),
            "audit": str(audit),
            "segmentation_methods": list(METHODS),
            "baseline_merge": "spatiotemporal__nominal",
            "common_eligibility": "Frozen first-study temporal/dwell eligibility: "
            "valid "
            "unambiguous existing arrival/departure, duration 0..1800 seconds. APC "
            "grain flags are retained, not silently removed from this denominator.",
            "full_fixture": not bool(args.limit),
            "source_rows": {"avl": avl.height, "apc": apc.height},
            "selected_vehicles": len(selected),
            "segmentation_recomputed": False,
            "gtfs_used": False,
            "human_labels_used": False,
            "clock_correction": False,
            "thresholds_calibrated": False,
            "publishable": False,
            "accuracy": None,
            "environment": {
                "python": platform.python_version(),
                "polars": pl.__version__,
                "platform": platform.platform(),
            },
        },
    )
    all_metrics = collections.defaultdict(list)
    frozen_input_hashes = {}
    timing = collections.Counter()
    for ordinal, item in enumerate(selected, 1):
        vehicle, case = item["raw_vehicle"], item["vehicle_case"]
        pred = (
            pl.col("vehicle").is_null()
            if vehicle is None
            else pl.col("vehicle") == vehicle
        )
        aa = avl.filter(pred).sort("time", "rid", nulls_last=True).to_dicts()
        pp = apc.filter(pred).sort("arrival", "rid", nulls_last=True).to_dicts()
        began = time.perf_counter()
        index = build_index(aa, config)
        timing["build_index_seconds"] += time.perf_counter() - began
        dump(out / "index_diagnostics" / f"{case}.json", index.diagnostics)
        began = time.perf_counter()
        prepared = prepare_candidates(pp, index, config)
        timing["prepare_candidates_seconds"] += time.perf_counter() - began
        for method in METHODS:
            chunk_path = frozen / "chunks" / method / f"{case}.parquet"
            old_path = (
                frozen
                / "assignments"
                / (method + "__spatiotemporal__nominal")
                / f"{case}.parquet"
            )
            for path in (chunk_path, old_path):
                if path.exists():
                    frozen_input_hashes[str(path.relative_to(frozen))] = file_hash(path)
            segmentation = load_frozen_segments(chunk_path)
            baseline = read_rows(old_path)
            began = time.perf_counter()
            assignments = match_direct(
                pp, index, segmentation, config, prepared=prepared
            )
            timing["match_" + method + "_seconds"] += time.perf_counter() - began
            began = time.perf_counter()
            metric, paired = summarize_vehicle(
                aa, pp, segmentation, baseline, assignments, config
            )
            timing["independent_checks_seconds"] += time.perf_counter() - began
            all_metrics[method].append(metric)
            save_rows(out / "assignments" / method / f"{case}.parquet", assignments)
            save_rows(out / "paired" / method / f"{case}.parquet", paired)
            dump(out / "metrics" / method / f"{case}.json", metric)
        print(
            json.dumps(
                {
                    "completed_vehicles": ordinal,
                    "total_vehicles": len(selected),
                    "avl_rows": len(aa),
                    "apc_rows": len(pp),
                    "elapsed_s": round(time.perf_counter() - started, 1),
                }
            ),
            flush=True,
        )
    summary = {method: combine_metrics(rows) for method, rows in all_metrics.items()}
    for metric in summary.values():
        supported = sum(metric["new_statuses"].get(s, 0) for s in ("direct", "partial"))
        metric["new_locally_supported_rows"] = supported
        metric["new_local_support_rate_all_rows"] = (
            supported / metric["apc_rows"] if metric["apc_rows"] else None
        )
        common_supported = (
            metric["new_common_eligible_direct"] + metric["new_common_eligible_partial"]
        )
        metric["new_local_support_rate_common_eligible"] = (
            common_supported / metric["common_eligible_apc_rows"]
            if metric["common_eligible_apc_rows"]
            else None
        )
    dump(out / "summary.json", summary)
    dump(out / "frozen_input_sha256.json", frozen_input_hashes)
    flat = []
    for method, metric in summary.items():
        flat.append(
            {
                "frozen_segmentation": method,
                "apc_rows": metric["apc_rows"],
                "common_eligible_apc_rows": metric["common_eligible_apc_rows"],
                "baseline_direct": metric["baseline_statuses"].get("direct", 0),
                **{
                    "new_" + status: metric["new_statuses"].get(status, 0)
                    for status in (
                        "direct",
                        "partial",
                        "ambiguous",
                        "unmatched",
                        "ineligible",
                    )
                },
                "new_locally_supported_rows": metric["new_locally_supported_rows"],
                "same_chunk_multi_episode_ambiguities": metric[
                    "same_chunk_multi_episode_ambiguities"
                ],
                "direct_multiple_frozen_chunks": metric[
                    "direct_multiple_frozen_chunks"
                ],
                "new_local_support_rate_all_rows": metric[
                    "new_local_support_rate_all_rows"
                ],
                "invariant_violations": sum(metric["invariant_violations"].values()),
                "accuracy": None,
            }
        )
    pl.from_dicts(flat, infer_schema_length=None).write_csv(
        out / "comparison_summary.csv"
    )
    unchanged_code = code_hashes == {
        p.name: file_hash(p) for p in sorted(package.glob("*.py"))
    }
    unchanged_input = canonical_hashes == {
        name: file_hash(audit / name) for name in canonical_hashes
    }
    unchanged_frozen = all(
        file_hash(frozen / name) == digest
        for name, digest in frozen_input_hashes.items()
    )
    completion = {
        "wall_seconds": time.perf_counter() - started,
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "timing_seconds": dict(timing),
        "completed_vehicles": len(selected),
        "combinations": len(summary),
        "full_fixture": not bool(args.limit),
        "all_implemented_invariants_pass": all(
            not any(m["invariant_violations"].values()) for m in summary.values()
        ),
        "code_unchanged_during_run": unchanged_code,
        "canonical_inputs_unchanged": unchanged_input,
        "frozen_inputs_unchanged": unchanged_frozen,
        "all_validation_rules_evaluated": False,
        "publishable": False,
        "accuracy": None,
    }
    report = out / "report.md"
    report.write_text(report_text(summary, completion))
    report.chmod(0o600)
    dump(out / "completion.json", completion)
    if not (unchanged_code and unchanged_input and unchanged_frozen):
        raise RuntimeError("Inputs or implementation changed during the diagnostic run")
    if not completion["all_implemented_invariants_pass"]:
        raise RuntimeError(
            "Completed diagnostic run contains independently detected violations"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--limit", type=int, default=0, help="Smoke only; zero uses the full fixture"
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
