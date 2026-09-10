"""Inspect a completed diagnostic study and write aggregate-only assessments.

This does not rerun inference, read raw CSV data, consume human labels, or emit
TIDES resources. A completion marker is required; partial output is not a result.
"""

# Generated Markdown paragraphs/table rows are intentionally kept intact.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import resource
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import polars as pl

ASSIGN_SCHEMA = {
    "apc_rid": pl.String,
    "segment_id": pl.String,
    "status": pl.String,
    "reason": pl.String,
    "support_kind": pl.String,
    "ping_ids": pl.List(pl.String),
    "distance_m": pl.Float64,
    "gap_s": pl.Float64,
    "score": pl.Float64,
    "margin": pl.Float64,
    "candidate_owners": pl.List(pl.String),
    "candidate_scores": pl.List(pl.Float64),
    "candidate_count": pl.Int64,
    "route_conflict": pl.Boolean,
    "boundary_spanning": pl.Boolean,
    "extension_seconds": pl.Float64,
    "anchor_apc_ids": pl.List(pl.String),
    "grain_uncertain": pl.Boolean,
}
CHUNK_SCHEMA = {
    "segment_id": pl.String,
    "start": pl.Datetime("us"),
    "end": pl.Datetime("us"),
    "experimental_chunk": pl.Boolean,
    "time_ambiguous": pl.Boolean,
    "chronology_unresolved": pl.Boolean,
    "possible_terminal": pl.Boolean,
}
COVERAGE_SCHEMA = {
    "segment_id": pl.String,
    "apc_leading_seconds": pl.Float64,
    "apc_trailing_seconds": pl.Float64,
    "actual_trip_start": pl.Datetime("us"),
    "actual_trip_end": pl.Datetime("us"),
    "bounds_are_observation_envelopes": pl.Boolean,
}
STATUSES = (
    "direct",
    "inferred",
    "temporal_only",
    "ambiguous",
    "ineligible",
    "unmatched",
)
ACCEPTED = ("direct", "inferred", "temporal_only")
SEG_NAMES = {
    "gap_only": "时间缺口分段",
    "avl_context": "AVL 上下文分段",
    "apc_assisted": "APC 辅助分段",
}
MERGE_NAMES = {
    "time_only": "仅时间基线",
    "spatiotemporal": "时空约束关联",
    "sequence_extension": "时空＋序列延续",
}


def read_json(path: Path):
    return json.loads(path.read_text())


def dump(path: Path, value):
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )
    path.chmod(0o600)


def hashed(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_frames(directory: Path, schema: dict) -> pl.DataFrame:
    # Cast inside each file before concatenation: entire-vehicle null columns
    # have Null/List(Null) schemas and are common in the observed fixture.
    frames = []
    for path in sorted(directory.glob("*.parquet")):
        frame = pl.read_parquet(path, columns=list(schema))
        frames.append(frame.select(pl.col(k).cast(v) for k, v in schema.items()))
    return pl.concat(frames) if frames else pl.DataFrame(schema=schema)


def count(frame: pl.DataFrame, expr: pl.Expr) -> int:
    return int(frame.select(expr.fill_null(False).sum()).item())


def distribution(values: pl.Series) -> dict:
    values = values.cast(pl.Float64).drop_nulls()
    if not len(values):
        return {"n": 0, "p50": None, "p90": None, "p95": None, "p99": None, "max": None}
    return {
        "n": len(values),
        **{
            f"p{p}": values.quantile(p / 100, interpolation="nearest")
            for p in (50, 90, 95, 99)
        },
        "max": values.max(),
    }


def rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def bytes_under(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def load_probe(path: Path, manifest: dict) -> dict:
    if not (path / "completion.json").exists():
        return {}
    probe_manifest = read_json(path / "manifest.json")
    for name in ("merge.py", "segmentation.py"):
        if probe_manifest["code_sha256"][name] != manifest["code_sha256"][name]:
            raise RuntimeError(f"Probe uses different core implementation: {name}")
    result = {
        "completion": read_json(path / "completion.json"),
        "manifest": probe_manifest,
        "summary": read_json(path / "probe_summary.json"),
        "checks": read_json(path / "checks.json"),
    }
    for filename in ("by_stratum_summary.json", "selection_status.json"):
        if (path / filename).exists():
            result[filename.removesuffix(".json")] = read_json(path / filename)
    return result


def junit_verification(path: Path) -> dict:
    if not path.exists():
        return {"status": "not_evaluated"}
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {
        name: sum(int(suite.attrib.get(name, 0)) for suite in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }
    return {
        "status": "pass" if not counts["failures"] and not counts["errors"] else "fail",
        **counts,
        "junit_sha256": hashed(path),
        "filename": path.name,
        "scope": "Project and diagnostic implementation tests; not independent TTC accuracy labels.",
    }


def load_counterexamples(path: Path, manifest: dict) -> dict:
    if not path.exists():
        return {}
    evidence = read_json(path)
    for name in ("merge.py", "segmentation.py"):
        if evidence["core_sha256"][name] != manifest["code_sha256"][name]:
            raise RuntimeError(f"Counterexample core mismatch: {name}")
    expected = {
        "duplicate_manufactures_context_persistence": ("robustness_fail", 1, 2),
        "duplicates_suppress_geometric_reversal": ("robustness_fail", 2, 1),
        "overlapping_terminal_roles_suppress_direction_boundary": (
            "terminal_role_representation_gap",
            2,
            1,
        ),
    }
    cases = {case["name"]: case for case in evidence["cases"]}
    for name, expected_values in expected.items():
        case = cases.get(name, {})
        values = tuple(
            case.get(k)
            for k in ("status", "baseline_chunk_count", "perturbed_chunk_count")
        )
        if values != expected_values:
            raise RuntimeError(f"Counterexample narrative not supported: {name}")
    evidence["analysis_verified_statuses_and_core_hashes"] = True
    return evidence


def rule(
    rule_id, evaluated, violations=0, *, missing=0, scope="", reason="", partial=False
):
    status = (
        "fail"
        if violations
        else ("not_evaluated" if partial or not evaluated else "pass")
    )
    return {
        "rule_id": rule_id,
        "status": status,
        "violations": int(violations),
        "evaluated": int(evaluated),
        "not_evaluated": missing,
        "scope": scope,
        "reason": reason,
    }


def conservation_rule(apc_rows, checks, chunk):
    return rule(
        "I01_SOURCE_CONSERVATION",
        apc_rows + chunk["raw_avl_rows"],
        sum(
            checks[k]
            for k in (
                "source_disposition_count_difference",
                "duplicate_apc_dispositions",
                "apc_references_missing_from_source",
            )
        )
        + sum(
            chunk[k]
            for k in (
                "raw_avl_rows_without_chunk",
                "duplicate_source_memberships",
                "invalid_source_references",
            )
        ),
        scope="All canonical APC dispositions and all raw AVL memberships; canonical counts checked against raw audit.",
    )


def invalid_ping_references(frame: pl.DataFrame, source: dict) -> int:
    prefix = f"avl:{source['sha256']}:"
    suffix = pl.col("rid").str.slice(len(prefix))
    ordinal = suffix.cast(pl.Int64, strict=False)
    return count(
        frame,
        ~pl.col("rid").str.starts_with(prefix)
        | ~suffix.str.contains(r"^[1-9][0-9]*$")
        | ordinal.is_null()
        | ~ordinal.is_between(1, source["row_count"]),
    )


def chunk_analysis(run: Path, source: dict) -> dict:
    result = {}
    source_rows = source["row_count"]
    prefix = f"avl:{source['sha256']}:"
    for method in SEG_NAMES:
        frames = read_frames(run / "chunks" / method, CHUNK_SCHEMA)
        durations = (frames["end"] - frames["start"]).dt.total_seconds()
        # File digest + ordinal is an exact source identity; byte ledger avoids
        # retaining millions of Python strings during the independent partition
        # check. Current raw audit reports zero structurally invalid AVL rows.
        seen = bytearray(source_rows + 1)
        duplicate = bad_reference = members = 0
        for path in sorted((run / "chunks" / method).glob("*.parquet")):
            groups = pl.read_parquet(path, columns=["ping_ids"])["ping_ids"]
            for group in groups:
                for rid in group:
                    members += 1
                    if not rid.startswith(prefix):
                        bad_reference += 1
                        continue
                    try:
                        ordinal = int(rid[len(prefix) :])
                    except ValueError:
                        bad_reference += 1
                        continue
                    if not 1 <= ordinal <= source_rows or rid[len(prefix) :] != str(
                        ordinal
                    ):
                        bad_reference += 1
                        continue
                    duplicate += bool(seen[ordinal])
                    seen[ordinal] = 1
        result[method] = {
            "chunks": frames.height,
            "duration_seconds": distribution(durations),
            "duration_seconds_excluding_dst_ambiguous": distribution(
                durations.filter(~frames["time_ambiguous"])
            ),
            "under_120s": int((durations < 120).sum()),
            "over_7200s": int((durations > 7200).sum()),
            "time_ambiguous_chunks": int(frames["time_ambiguous"].sum()),
            "chronology_unresolved_chunks": int(frames["chronology_unresolved"].sum()),
            "possible_terminal_chunks": int(frames["possible_terminal"].sum()),
            "not_labeled_experimental": count(frames, ~pl.col("experimental_chunk")),
            "unique_chunk_ids": frames["segment_id"].n_unique(),
            "source_memberships": members,
            "raw_avl_rows": source_rows,
            "duplicate_source_memberships": duplicate,
            "invalid_source_references": bad_reference,
            "raw_avl_rows_without_chunk": seen.count(0) - 1,
            "partition_check_scope": "Every observed raw AVL row; applicable because raw audit found zero invalid AVL rows.",
        }
    return result


def strata_metrics(frame: pl.DataFrame) -> dict:
    tests = {
        "all_apc": pl.lit(True),
        "dst_ambiguous": pl.col("time_ambiguous"),
        "time_nonexistent": pl.col("time_nonexistent"),
        "source_invalid": pl.col("invalid"),
        "grain_uncertain_signal": pl.col("source_grain_uncertain"),
        "avg_sample_1": pl.col("avg_sample") == 1,
        "avg_sample_gt_1": pl.col("avg_sample") > 1,
        "avg_sample_missing": pl.col("avg_sample").is_null(),
        "zero_dwell": pl.col("arrival") == pl.col("departure"),
        "cross_midnight_interval": pl.col("arrival").dt.date()
        != pl.col("departure").dt.date(),
        "boundary_spanning_interval": pl.col("boundary_spanning"),
        "no_candidate": pl.col("candidate_count") == 0,
        "multiple_candidates": pl.col("candidate_count") > 1,
    }
    output = {}
    for name, expression in tests.items():
        subset = frame.filter(expression.fill_null(False))
        outcomes = collections.Counter(subset["status"].to_list())
        output[name] = {
            "denominator": subset.height,
            "status_counts": dict(outcomes),
            "accepted": sum(outcomes[k] for k in ACCEPTED),
            "direct_rate": rate(outcomes["direct"], subset.height),
        }
    return output


def anchor_geometry(inferred: pl.DataFrame, apc: pl.DataFrame) -> dict:
    anchors = inferred.select("apc_rid", "anchor_apc_ids").explode("anchor_apc_ids")
    positions = (
        anchors.join(
            apc.select("rid", "lat", "lon"),
            left_on="anchor_apc_ids",
            right_on="rid",
            how="left",
        )
        .group_by("apc_rid")
        .agg(pl.struct("lat", "lon").alias("positions"))
    )
    weak = complete = 0
    maximum_distances = []
    for record in positions.to_dicts():
        points = record["positions"]
        if len(points) < 3 or any(p["lat"] is None or p["lon"] is None for p in points):
            continue
        complete += 1
        distances = []
        for i, first in enumerate(points):
            for second in points[i + 1 :]:
                y = math.radians(second["lat"] - first["lat"])
                x = math.radians(second["lon"] - first["lon"]) * math.cos(
                    math.radians((first["lat"] + second["lat"]) / 2)
                )
                distances.append(6371008.8 * math.hypot(x, y))
        maximum = max(distances, default=0)
        weak += maximum < 50
        maximum_distances.append(maximum)
    return {
        "inferred_rows": inferred.height,
        "rows_with_all_anchor_coordinates": complete,
        "all_pairwise_anchor_distances_under_50m": weak,
        "max_pairwise_anchor_distance_m": distribution(
            pl.Series(maximum_distances, dtype=pl.Float64)
        ),
        "criterion": "Posthoc exploratory weakness flag; three distinct stop codes do not prove physical anchor progression.",
    }


def analyze_combination(run, key, baseline, apc, chunks, config, source):
    segmentation, method, regime = key.split("__")
    frame = read_frames(run / "assignments" / key, ASSIGN_SCHEMA)
    cfg = config["common"] | config["regimes"][regime]
    unique_apc = frame["apc_rid"].n_unique()
    # Left join preserves every disposition even if a source reference is bad.
    joined = frame.join(apc, left_on="apc_rid", right_on="rid", how="left")
    source_missing = count(joined, pl.col("source_exists").is_null())
    outcomes = collections.Counter(frame["status"].to_list())
    direct = frame.filter(pl.col("status") == "direct")
    inferred = frame.filter(pl.col("status") == "inferred")
    accepted = frame.filter(pl.col("status").is_in(ACCEPTED))
    sampled = frame.filter(pl.col("status").is_in(["direct", "temporal_only"]))
    measured_space = sampled["distance_m"].drop_nulls()
    measured_time = sampled["gap_s"].drop_nulls()
    candidate_distribution = dict(
        sorted(collections.Counter(frame["candidate_count"].to_list()).items())
    )
    coverage = read_frames(run / "coverage" / key, COVERAGE_SCHEMA)
    ping_refs = accepted.select(
        pl.col("ping_ids").explode(empty_as_null=True).alias("rid")
    ).drop_nulls()
    invalid_ping = invalid_ping_references(ping_refs, source)
    direct_ids = direct.select(pl.col("apc_rid").alias("anchor_id"))
    anchors = inferred.select(
        pl.col("anchor_apc_ids").explode(empty_as_null=True).alias("anchor_id")
    ).drop_nulls()
    invalid_anchors = anchors.join(direct_ids, on="anchor_id", how="anti").height
    ownerless_accepted = count(accepted, pl.col("segment_id").is_null())
    assigned_unaccepted = count(
        frame, ~pl.col("status").is_in(ACCEPTED) & pl.col("segment_id").is_not_null()
    )
    bounds_bad = count(
        direct,
        pl.col("distance_m").is_null()
        | (pl.col("distance_m") > cfg["radius_m"])
        | pl.col("gap_s").is_null()
        | (pl.col("gap_s") > cfg["time_tolerance_s"])
        | pl.col("boundary_spanning"),
    )
    extension_bad = count(
        inferred,
        (pl.col("ping_ids").list.len() != 0)
        | (pl.col("anchor_apc_ids").list.len() < cfg["anchor_distinct_stops"])
        | pl.col("distance_m").is_not_null()
        | pl.col("gap_s").is_not_null()
        | pl.col("extension_seconds").is_null()
        | (pl.col("extension_seconds") > cfg["extension_horizon_s"]),
    )
    accepted_source = joined.filter(pl.col("status").is_in(ACCEPTED))
    bad_time = count(
        accepted_source,
        pl.col("time_ambiguous") | pl.col("time_nonexistent") | pl.col("invalid"),
    )
    endpoint_claims = count(
        coverage,
        pl.col("actual_trip_start").is_not_null()
        | pl.col("actual_trip_end").is_not_null(),
    )
    accepted_low_margin = count(
        direct,
        (pl.col("margin") < cfg["margin_score"])
        | (pl.col("score") < cfg["accept_score"]),
    )
    result = {
        "configuration": key,
        "segmentation": segmentation,
        "merge": method,
        "regime": regime,
        "apc_rows": frame.height,
        "eligible_apc_rows": baseline["apc_eligible_rows"],
        "counts": {status: outcomes[status] for status in STATUSES},
        "direct_rate_all_apc": rate(outcomes["direct"], apc.height),
        "direct_rate_eligible_apc": rate(
            outcomes["direct"], baseline["apc_eligible_rows"]
        ),
        "inferred_rate_all_apc": rate(outcomes["inferred"], apc.height),
        "candidate_count_distribution": candidate_distribution,
        "sampled_spatial_residual_m": distribution(measured_space),
        "sampled_temporal_gap_s": distribution(measured_time),
        "common_100m_excursions": int((measured_space > 100).sum()),
        "common_100m_denominator": len(measured_space),
        "common_100m_excursion_rate": rate(
            int((measured_space > 100).sum()), len(measured_space)
        ),
        "common_60s_excursions": int((measured_time > 60).sum()),
        "common_60s_denominator": len(measured_time),
        "common_60s_excursion_rate": rate(
            int((measured_time > 60).sum()), len(measured_time)
        ),
        "declared_thresholds": cfg,
        "mixed_source_direction_chunks": baseline[
            "chunks_with_3plus_records_each_multiple_directions"
        ],
        "mixed_source_route_chunks": baseline[
            "chunks_with_3plus_records_each_multiple_routes"
        ],
        "mixed_direction_denominator": chunks[segmentation]["chunks"],
        "apc_leading_assigned_chunks": int((coverage["apc_leading_seconds"] > 0).sum()),
        "apc_trailing_assigned_chunks": int(
            (coverage["apc_trailing_seconds"] > 0).sum()
        ),
        "apc_leading_seconds": distribution(
            coverage["apc_leading_seconds"].filter(coverage["apc_leading_seconds"] > 0)
        ),
        "apc_trailing_seconds": distribution(
            coverage["apc_trailing_seconds"].filter(
                coverage["apc_trailing_seconds"] > 0
            )
        ),
        "coverage_denominator": coverage.height,
        "source_route_conflicts": baseline["assigned_source_route_conflicts"],
        "strata": strata_metrics(joined),
        "runtime_seconds": {
            k: baseline[k]
            for k in ("support_seconds", "segmentation_seconds", "merge_seconds")
        },
        "assignment_output_bytes": bytes_under(run / "assignments" / key),
        "independent_reviewed_truth_labels": 0,
        "accuracy": None,
        "trip_boundary_accuracy": None,
        "runner_implemented_invariant_violations": baseline["invariants"],
        "analysis_checks": {
            "source_disposition_count_difference": abs(frame.height - apc.height),
            "duplicate_apc_dispositions": frame.height - unique_apc,
            "apc_references_missing_from_source": source_missing,
            "invalid_observed_ping_references": invalid_ping,
            "inferred_anchors_not_direct": invalid_anchors,
            "direct_outside_declared_bounds_or_boundary": bounds_bad,
            "inference_support_misrepresentation": extension_bad,
            "accepted_invalid_or_ambiguous_time": bad_time,
            "operational_endpoint_claims": endpoint_claims,
            "accepted_without_owner": ownerless_accepted,
            "unaccepted_with_owner": assigned_unaccepted,
            "accepted_direct_below_score_or_margin": accepted_low_margin,
        },
        "inferred_anchor_geometry": anchor_geometry(inferred, apc),
    }
    return result


def build_rubric(results, chunks, hashes_match, probe_checks, counterexamples):
    rules = {}
    for key, result in results.items():
        c = result["analysis_checks"]
        chunk = chunks[result["segmentation"]]
        n, direct, inferred = (
            result["apc_rows"],
            result["counts"]["direct"],
            result["counts"]["inferred"],
        )
        rules[key] = [
            conservation_rule(n, c, chunk),
            rule(
                "I02_IMMUTABLE_LINEAGE",
                n,
                c["apc_references_missing_from_source"]
                + c["invalid_observed_ping_references"]
                + int(not hashes_match),
                partial=True,
                missing=None,
                scope="Canonical-input hashes, APC and observed GPS references checked.",
                reason="Boundary/candidate evidence lineage not exhaustively replayed; raw CSV hashes inherited from audit rather than reread.",
            ),
            rule(
                "I03_UNIQUE_APC_ATTRIBUTION",
                n,
                c["duplicate_apc_dispositions"]
                + c["accepted_without_owner"]
                + c["unaccepted_with_owner"],
                scope="Every APC disposition; no passenger event aggregation was performed.",
            ),
            rule(
                "I04_SEGMENT_PARTITION",
                chunk["source_memberships"],
                chunk["duplicate_source_memberships"]
                + chunk["invalid_source_references"]
                + chunk["raw_avl_rows_without_chunk"],
                partial=True,
                missing=None,
                scope="Full source-membership partition independently checked.",
                reason="Full-fixture batching/ordering invariance not rerun; perturbation probes cover only selected windows.",
            ),
            rule(
                "I05_REAL_GPS_ONLY",
                direct + inferred + result["counts"]["temporal_only"],
                c["invalid_observed_ping_references"]
                + c["inference_support_misrepresentation"],
                scope="All accepted observed GPS references match the raw source digest/ordinal universe; inferred tails contain no ping IDs.",
            ),
            rule(
                "I06_DIRECT_SUPPORT_VALID",
                direct,
                c["direct_outside_declared_bounds_or_boundary"]
                + sum(
                    v
                    for k, v in result[
                        "runner_implemented_invariant_violations"
                    ].items()
                    if k.startswith("direct_")
                    or k == "accepted_boundary_spanning_interval"
                ),
                partial=True,
                missing=None,
                scope="Declared time-space bounds, real references and runner same-segment/no-extrapolation checks.",
                reason="Full independent replay of every interpolation bracket and spatial residual not performed; time-only rows are weak baseline outputs, not accepted direct evidence.",
            ),
            rule(
                "I07_SUPPORT_NOT_CONFIDENCE",
                n,
                c["inference_support_misrepresentation"]
                + c["inferred_anchors_not_direct"]
                + c["accepted_invalid_or_ambiguous_time"],
                scope="All assignments distinguish direct, inferred, temporal-only and abstention; no probability field or fabricated residual on inferred rows.",
            ),
            rule(
                "I08_ABSTENTION_AND_BOUNDARIES",
                n,
                c["accepted_direct_below_score_or_margin"] + c["unaccepted_with_owner"],
                partial=True,
                missing=None,
                scope="Accepted direct margins and unaccepted null owners checked; bounded candidate counts reported.",
                reason="Exhaustiveness of candidate/boundary universe and absence of all possible alternatives cannot be proved from the output alone.",
            ),
            rule(
                "I09_NO_SEMANTIC_PROMOTION",
                n + chunk["chunks"],
                c["operational_endpoint_claims"] + chunk["not_labeled_experimental"],
                scope="Outputs are experimental chunks/attachments; operational endpoints null; no performed trip, service date, GTFS mapping or full visit sequence emitted.",
            ),
            rule(
                "I10_LOGICAL_DETERMINISM",
                0,
                partial=True,
                missing=None,
                scope="Full-fixture determinism not independently rerun.",
                reason="See separately scoped perturbation/permutation probe checks; synthetic tests and selected windows do not establish full-fixture invariance.",
            ),
        ]
    return {
        "rubric_version": "apc-avl-coverage-v1",
        "interpretation": "Partial rules are not_evaluated with explicit completed sub-scope; no global all-rules-pass claim.",
        "combination_rules": rules,
        "probe_checks": probe_checks,
        "constructed_robustness_counterexamples": counterexamples,
        "publication_gates": [
            {
                "rule_id": "P01_SCHEMA_AND_REFERENCES",
                "status": "not_applicable",
                "publication_status": "not_published",
                "reason": "No TIDES package was produced or submitted to conformance validation.",
            },
            {
                "rule_id": "P02_TIME_AND_SERVICE_DATE",
                "status": "not_evaluated",
                "publication_status": "semantic_unresolved",
                "reason": "DST/nonexistent instants abstained; clock basis and operational service date not authoritatively established.",
            },
            {
                "rule_id": "P03_APC_GRAIN",
                "status": "not_evaluated",
                "publication_status": "semantic_unresolved",
                "reason": "No source-owner contract for AvgSample/NoTrips/one-row meaning; applies even to AvgSample=1.",
            },
            {
                "rule_id": "P04_APC_EVENT_SEMANTICS",
                "status": "not_evaluated",
                "publication_status": "semantic_unresolved",
                "reason": "Counts, timestamp proxy, load and door semantics not approved; no passenger events emitted.",
            },
            {
                "rule_id": "P05_ACTUAL_VISIT_ORDER",
                "status": "not_evaluated",
                "publication_status": "semantic_unresolved",
                "reason": "No verified complete trip visit sequence, including truncated prefixes and internal exclusions.",
            },
            {
                "rule_id": "P06_TRIP_IDENTITY_AND_ENDPOINTS",
                "status": "not_evaluated",
                "publication_status": "semantic_unresolved",
                "reason": "Physical chunks are not verified performed trips; observation bounds are not origin/destination events.",
            },
            {
                "rule_id": "P07_SOURCE_IDENTITY_AND_SCHEDULE_FIELDS",
                "status": "not_evaluated",
                "publication_status": "semantic_unresolved",
                "reason": "Exact normalized vehicle string is an experimental identity hypothesis; GTFS mapping deliberately deferred.",
            },
        ],
    }


def n(value):
    return f"{value:,}"


def percent(value):
    return "未定义" if value is None else f"{value:.2%}"


def render_report(
    results,
    chunks,
    completion,
    manifest,
    rubric,
    probe,
    probe_checks,
    output_bytes,
    analysis_name,
):
    counts = manifest["input_rows"]
    nominal = [r for r in results.values() if r["regime"] == "nominal"]
    reference_direct = results["avl_context__spatiotemporal__nominal"]
    reference_sequence = results["avl_context__sequence_extension__nominal"]
    rows = [
        "# APC–AVL 分割与融合方法比较（2026-09-08）",
        "",
        f"当前数据支持先构建可追溯的 APC–AVL 时空关联，但还不足以把观测片段认定为已验证的 `trip performed`。以 AVL 上下文分割＋默认参数为例，时空约束关联直接连接 {n(reference_direct['counts']['direct'])} 条 APC（占全部 APC 的 {percent(reference_direct['direct_rate_all_apc'])}），序列延续再增加 {n(reference_sequence['counts']['inferred'])} 条推断关联；这些比例表示证据覆盖，不表示准确率。",
        "",
        f"已完成全量 {n(counts['avl'])} 行 AVL、{n(counts['apc'])} 行 APC，覆盖 {manifest['selected_vehicles']} 辆源车辆编号并集，共 {len(results)} 个方法/阈值组合。分析对象是观测片段和关联假设。独立审定真值标签为 0，关联准确率和趟次边界准确率均为未定义。",
        "",
        "本次研究依据用户的新请求独立开展，未读取旧盲标标签、未运行旧协议冻结的 factorial、未使用 GTFS，未生成生产 TIDES 数据。原始文件保持本地；本文仅含聚合统计，不含车辆轨迹、原始标识或行级时间。",
        "",
        f"原始数据问题见 [raw 审计](apc-avl-raw-edge-cases-20260908.md)，规范与分母定义见 [TIDES 评价标准](apc-avl-coverage-evaluation-standard.md)，参数见 [study_config.json](../experiments/apc_avl_coverage_study/study_config.json)。受限完整结果见 [factorial_summary.csv](../artifacts/apc-avl-coverage-study-20260908/factorial02/factorial_summary.csv) 和 [{analysis_name}/assessment.json](../artifacts/apc-avl-coverage-study-20260908/{analysis_name}/assessment.json)。",
        "",
        "## 1. 默认阈值下的九种组合",
        "",
        "直接关联：有真实 AVL 点或短间隔插值特征的时空支持。序列推断：有冻结直接锚点支持，未生成 GPS 点。弱关联：仅时间基线的结果，不计为直接可信关联。歧义、未匹配和不合格记录均保留。所有行数以原始 APC 总量为共同分母；符合时间/结构准入条件的分母另记于完整结果。",
        "",
        "| 分割 | 融合 | 直接 | 推断 | 弱关联 | 歧义 | 不合格 | 未匹配 | 直接/全部 APC |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in nominal:
        c = r["counts"]
        cells = [
            SEG_NAMES[r["segmentation"]],
            MERGE_NAMES[r["merge"]],
            *[n(c[s]) for s in STATUSES],
            percent(r["direct_rate_all_apc"]),
        ]
        rows.append("| " + " | ".join(cells) + " |")
    rows += [
        "",
        "## 2. 分割结果的可解释性",
        "",
        "| 分割 | 片段数 | 时长中位数/分钟 | 非DST中位数/分钟 | P95/分钟 | <2 分钟 | >2 小时 | 多方向片段 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for method, c in chunks.items():
        mixed = results[f"{method}__spatiotemporal__nominal"][
            "mixed_source_direction_chunks"
        ]
        rows.append(
            f"| {SEG_NAMES[method]} | {n(c['chunks'])} | {c['duration_seconds']['p50'] / 60:.1f} | {c['duration_seconds_excluding_dst_ambiguous']['p50'] / 60:.1f} | {c['duration_seconds']['p95'] / 60:.1f} | {n(c['under_120s'])} | {n(c['over_7200s'])} | {n(mixed)} |"
        )
    rows += [
        "",
        "多方向片段表示同一观测片段中至少两个 APC 源方向各有至少 3 条直接关联记录。分母是该分割方法的全部片段；它用于提示可能需要进一步检查的边界。源方向未经权威验证，APC 辅助分割也使用了方向证据，因此该指标具有自我验证偏差，不能当作真实误合并率。片段越短或方向越纯均不自动代表质量更高。",
        "",
        f"三种分割均将 {n(chunks['gap_only']['time_ambiguous_chunks'])} 条 DST 歧义 GPS 保留为独立不确定片段，因此全体中位数会被零时长片段显著影响；非DST中位数另列。位置不确定点的保留也会增加短片段，不能将所有短片段解释为真实运行碎片化。APC 辅助分割仍有 {n(chunks['apc_assisted']['over_7200s'])} 个超过2小时的片段、{n(results['apc_assisted__spatiotemporal__nominal']['mixed_source_direction_chunks'])} 个多方向片段。结合共享终点的实测歧义，本轮尚未完成可靠的实际趟次边界恢复。",
        "",
        "## 3. 严格、默认、宽松参数的取舍",
        "",
        "所有数字阈值均为探索配置。严格/默认/宽松的时间容差为 30/60/120 秒，空间半径为 50/100/200 米，最大插值间隔为 60/90/180 秒，序列延续上限为 10/15/30 分钟。实际接受还受分数和候选差距规则约束；配置半径不是自动接受范围。",
        "",
        "| 分割 | 参数 | 直接 | 序列新增推断 | 歧义（序列法） | >100m / 可测直接关联 | >60s / 可测直接关联 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for method in SEG_NAMES:
        for regime in ("strict", "nominal", "wide"):
            direct = results[f"{method}__spatiotemporal__{regime}"]
            seq = results[f"{method}__sequence_extension__{regime}"]
            rows.append(
                f"| {SEG_NAMES[method]} | {regime} | {n(direct['counts']['direct'])} | {n(seq['counts']['inferred'])} | {n(seq['counts']['ambiguous'])} | {n(direct['common_100m_excursions'])}/{n(direct['common_100m_denominator'])} | {n(direct['common_60s_excursions'])}/{n(direct['common_60s_denominator'])} |"
            )
    rows += [
        "",
        "100 米/60 秒是跨配置统一参考线。宽松配置超过这两条参考线不等于违反自己的 200 米/120 秒规则。反过来，仅时间基线即使关联数量更多，也不能视为符合共同空间证据标准。残差是算法使用过的证据，因此较小残差不能独立证明关联正确。",
        "",
        "| 默认组合 | 空间 P50/P95（米） | 时间间隔 P95（秒） | 超过100m比例 | 超过60s比例 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in nominal:
        d, t = r["sampled_spatial_residual_m"], r["sampled_temporal_gap_s"]
        spatial = "未定义" if d["p50"] is None else f"{d['p50']:.1f}/{d['p95']:.1f}"
        temporal = "未定义" if t["p95"] is None else f"{t['p95']:.1f}"
        rows.append(
            f"| {SEG_NAMES[r['segmentation']]}＋{MERGE_NAMES[r['merge']]} | {spatial} | {temporal} | {percent(r['common_100m_excursion_rate'])} | {percent(r['common_60s_excursion_rate'])} |"
        )
    rows += [
        "",
        "## 4. 覆盖不一致与缺失证据",
        "",
        "| 默认序列法 | APC 前伸片段 | APC 尾伸片段 | 实際发生关联的片段分母 | APC 尾伸时长 P95/秒 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for method in SEG_NAMES:
        r = results[f"{method}__sequence_extension__nominal"]
        tail = r["apc_trailing_seconds"]["p95"]
        rows.append(
            f"| {SEG_NAMES[method]} | {n(r['apc_leading_assigned_chunks'])} | {n(r['apc_trailing_assigned_chunks'])} | {n(r['coverage_denominator'])} | {tail if tail is not None else '未定义'} |"
        )
    rows += [
        "",
        "序列推断还有一项需单独检验的物理证据问题：三个不同站码可能指向相近位置。当前内核检验了目标到锚点的距离，但没有要求三个锚点之间形成物理行进。以下为本次事后增加的 50 米探索性筛查，不作为已经校准的接受门槛。",
        "",
        "| 默认序列法 | 全部推断行 | 锚点坐标完整行 | 三锚点两两均不足50米 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for method in SEG_NAMES:
        g = results[f"{method}__sequence_extension__nominal"][
            "inferred_anchor_geometry"
        ]
        rows.append(
            f"| {SEG_NAMES[method]} | {n(g['inferred_rows'])} | {n(g['rows_with_all_anchor_coordinates'])} | {n(g['all_pairwise_anchor_distances_under_50m'])} |"
        )
    ref = results["avl_context__sequence_extension__nominal"]
    rows += [
        "",
        "下表以 AVL 上下文＋默认序列延续为例，各类可能交叉，不能相加当作互斥总数。",
        "",
        "| 证据情形 | APC 分母 | 直接 | 推断 | 弱关联 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for tag in (
        "dst_ambiguous",
        "time_nonexistent",
        "grain_uncertain_signal",
        "avg_sample_1",
        "avg_sample_gt_1",
        "cross_midnight_interval",
        "boundary_spanning_interval",
        "no_candidate",
        "multiple_candidates",
    ):
        s = ref["strata"][tag]
        rows.append(
            f"| {tag} | {n(s['denominator'])} | {n(s['status_counts'].get('direct', 0))} | {n(s['status_counts'].get('inferred', 0))} | {n(s['status_counts'].get('temporal_only', 0))} |"
        )
    rows += [
        "",
        "默认组合的最终候选片段数分布如下。序列推断会把最终 owner 记为单一候选；因此这不是原始候选全集的大小，不能用它证明没有被遗漏的趟次解释。",
        "",
        "| 默认组合 | 0 个候选 | 1 个候选 | 至少2个候选 | APC 分母 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in nominal:
        cd = r["candidate_count_distribution"]
        rows.append(
            f"| {SEG_NAMES[r['segmentation']]}＋{MERGE_NAMES[r['merge']]} | {n(cd.get(0, 0))} | {n(cd.get(1, 0))} | {n(sum(v for k, v in cd.items() if k >= 2))} | {n(r['apc_rows'])} |"
        )
    rows += [
        "",
        "APC 含粒度不确定信号时仍可形成诊断时空候选，但不能关闭 APC 发布门槛。`AvgSample=1` 也不是单次停站的合同证明。未匹配可能来自覆盖缺失、物理不一致、候选不足或跨边界歧义；不得统一解释为错误数据。",
        "",
        "真实点序列的首末时间仅构成观测范围。所有输出的 `actual_trip_start/end` 均保留为空。针对 AVL 13:00–14:00、APC 13:15–14:13，允许在独立直接锚点与连续性证据支持下保留 APC 尾段推断；13:00–14:13 不被命名为已验证的真实趟次起止。",
        "",
        "## 5. 鲁棒性检查与可信度边界",
        "",
    ]
    validation = rubric["implementation_verification"]
    if validation["status"] != "not_evaluated":
        probe_count = sum(
            p.get("completion", {}).get("checks", 0)
            for p in rubric["probe_populations"].values()
        )
        rows += [
            f"项目及研究实现测试共 {validation['tests']} 项，失败 {validation['failures']}、错误 {validation['errors']}，见 [JUnit 制品](../artifacts/apc-avl-coverage-study-20260908/{validation['filename']})。常规与定向探针另执行 {probe_count} 项限定范围检查。通过这些检查说明相应实现行为被验证；下述真实边界局限及构造反例仍然成立。",
            "",
        ]
    if probe:
        general = rubric["probe_populations"]["general"]
        rows += [
            f"常规探针选取 {general['completion']['vehicle_windows']} 个固定 2 小时车辆窗口，按原始数据存在性选样，未按匹配结果选样。已纳入 {len(probe)} 个分割/融合/扰动配置、{general['completion']['probe_combinations']} 次窗口×配置比较，详见受限 [probes01/probe_summary.json](../artifacts/apc-avl-coverage-study-20260908/probes01/probe_summary.json)。掩蔽实验用未掩蔽算法输出作参照；统计原归属保持、弃判、直接转推断和不相交片段切换。这些是缺失条件下的稳定性指标，不能叫作真实匹配准确率。",
            "",
        ]
        rows += [
            "| 扰动 | 组合数 | 保留 APC 比较次数 | 原有关联后弃判 | 直接转同组推断 | 不相交片段切换 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        grouped = collections.defaultdict(collections.Counter)
        for key, value in probe.items():
            if not isinstance(value, dict):
                continue
            kind = key.split("__")[-1]
            grouped[kind]["configs"] += 1
            for field in (
                "retained_apc",
                "reference_assigned_then_abstained",
                "direct_to_inferred_same_membership",
                "disjoint_owner_switch",
            ):
                grouped[kind][field] += value.get(field, 0)
        for kind, v in sorted(grouped.items()):
            rows.append(
                f"| {kind} | {v['configs']} | {n(v['retained_apc'])} | {n(v['reference_assigned_then_abstained'])} | {n(v['direct_to_inferred_same_membership'])} | {n(v['disjoint_owner_switch'])} |"
            )
        rows += [
            "",
            "表中比较次数跨算法重复计数，不能当作独立样本量。无幸存 GPS 支持可比较的片段单列为不可比较，不能给它虚构 Jaccard=0 或判定切换。完整探针检查状态另见 `checks.json`；部分扰动失败必须保留并解释。",
            "",
        ]
        negative = probe["apc_assisted__spatiotemporal__apc_clock_-60s"]
        positive = probe["apc_assisted__spatiotemporal__apc_clock_+60s"]
        negative_two = probe["apc_assisted__spatiotemporal__apc_clock_-120s"]
        positive_two = probe["apc_assisted__spatiotemporal__apc_clock_+120s"]
        rows += [
            f"时钟偏移是明显的敏感点：常规窗口中，APC 辅助＋时空约束的参照直接关联为 {n(negative['reference_direct'])} 条；人为将 APC 时钟移动 −60/+60 秒后，关联降至 {n(negative['perturbed_assigned'])}/{n(positive['perturbed_assigned'])} 条，移动 −120/+120 秒后为 {n(negative_two['perturbed_assigned'])}/{n(positive_two['perturbed_assigned'])} 条。这个实验没有估计 TTC 的真实时钟偏移；它说明时钟合同与偏移诊断必须先于提高关联容差。",
            "",
        ]
    else:
        rows += [
            "本报告生成时未发现已完成的 probes01；扰动恢复与全量重复运行确定性均标为未评估，不用合成测试代替。",
            "",
        ]
    edge = rubric["probe_populations"].get("edge", {})
    if edge:
        dst_duplication = edge["by_stratum_summary"]["dst_ambiguous_hour"][
            "apc_assisted__sequence_extension__exact_avl_duplication"
        ]
        rows += [
            "另外执行了按原始边界风险定向选样的探针，来源是共享终点重叠、DST 歧义、提取窗口末尾和内部 AVL 缺口。它与上述常规窗口是不同样本总体，最终结果保存在 [probes-edge02](../artifacts/apc-avl-coverage-study-20260908/probes-edge02/probe_summary.json)，不能混成一个总体准确率。此前 edge01 保留作诊断，不作为最终统计。",
            "",
            f"该定向探针执行 {edge['completion'].get('probe_combinations', '见制品')} 次窗口×配置比较；检查失败数为 {len(edge['completion'].get('failed_checks', []))}。完整分母、分层结果和任何失败均保留在其 manifest、summary 与 checks 中。",
            "",
        ]
        rows += [
            "下表固定使用 APC 辅助分割＋默认序列延续，并施加“APC 前缀15分钟、AVL 后缀13分钟”联合掩蔽。四种原始证据情形分别选取2个窗口；原先关联与扰动后关联均只是算法参照。",
            "",
            "| 定向原始证据 | 保留APC分母 | 原有关联 | 掩蔽后关联 | 直接转同组推断 | 原有关联后弃判 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for stratum, values in edge.get("by_stratum_summary", {}).items():
            m = values["apc_assisted__sequence_extension__offset_coverage_15m_13m"]
            rows.append(
                f"| {stratum} | {n(m['retained_apc'])} | {n(m['reference_assigned'])} | {n(m['perturbed_assigned'])} | {n(m['direct_to_inferred_same_membership'])} | {n(m['reference_assigned_then_abstained'])} |"
            )
        rows += [
            "",
            "定向真实样本中，同站、相反方向且区间重叠27秒的两条 APC 可同时直接关联到同一观测块。这说明直接 GPS 支持能成立，而实际趟次边界仍未解决。另一个共享终点样本仍为歧义/未匹配。月份末尾单源延续没有在这些掩蔽样本中恢复原关联，不能把所有覆盖尾部都视为可补全的同一趟次。",
            "",
            f"在该次 APC 辅助＋序列法的 DST 定向窗口中，复制 GPS 后原始片段总数从 {n(dst_duplication['reference_chunk_count'])} 增加到 {n(dst_duplication['perturbed_chunk_count'])}；投影到幸存源记录集合后的已接受 owner 关系可以不变。这两个指标描述不同对象。{edge['completion']['checks']} 项定向检查只覆盖明确声明的规则，不等于重复数据对分割数量完全无影响。",
            "",
        ]
    if rubric["constructed_robustness_counterexamples"]:
        rows += [
            "已核验独立构造案例的核心代码哈希、状态与结果，实际复现三个负面结果，见 [segmentation_counterexamples.json](../artifacts/apc-avl-coverage-study-20260908/segmentation_counterexamples.json)：复制一个 GPS 测量可使上下文分割从 1 块变 2 块；复制全部 GPS 行可使几何折返从 2 块变 1 块；前后角色共享终点且到离站区间重叠，可使 APC 方向切分从 2 块变 1 块。常规窗口的重复数据探针即使通过，也不能消除这些反例。它们限制分割方法的泛化鲁棒性，不能用“无运行异常”抵消。",
            "",
        ]
    else:
        rows += ["本次分析缺少经过核验的构造反例制品；其结果未评估。", ""]
    rows += [
        "内核候选差距按观测块计算：同一长块内多个停站或相邻趟次的竞争会被折叠成一个 owner。因此长块的高直接关联率不等于趟次身份明确。APC 源粒度标记包含计数/载荷非整数信号，APC 辅助分割会排除这些记录；可以说未用客流数值作为正向匹配得分，不能说整个方法完全独立于计数字段。",
        "",
        f"默认 AVL 上下文＋时空约束的直接关联中，仍有 {n(reference_direct['source_route_conflicts'])} 条 APC 与支持 GPS 的源线路标签不同。这里的直接关联只确认局部时空支持，不确认线路或趟次语义。",
        "",
    ]
    fail_count = sum(
        x["status"] == "fail"
        for values in rubric["combination_rules"].values()
        for x in values
    )
    rows += [
        f"机器可读 [rubric_check_results.json](../artifacts/apc-avl-coverage-study-20260908/{analysis_name}/rubric_check_results.json) 记录每项规则的实际范围、分母和未检查部分。本次观察到的硬规则失败为 {fail_count} 项（按配置×规则计）；部分完成或未执行的规则明确标为 `not_evaluated`，没有“全部 TIDES 门槛通过”的声明。",
        "",
        "已核对每条 APC 的单一处理结果、全部 AVL 的片段归属、GPS 原始引用、推断不得伪造 GPS、时间不确定记录不得接受，以及输出不提升为实际趟次。完整的边界候选穷举、每个插值残差的独立重算、所有输入排列/分批不变性仍未完成。P02–P07 的时间合同、APC 粒度、事件含义、完整站序和实际趟次身份继续未解决；P01 无生产包可供发布验证。",
        "",
        "## 6. 本地运行可行性",
        "",
        f"完整 21 组合运行耗时 {completion['wall_seconds'] / 60:.1f} 分钟，进程峰值 RSS 为 {completion['peak_rss_mib'] / 1024:.2f} GiB，运行目录总量 {output_bytes / (1024**3):.2f} GiB。这是共享进程、共享支持候选和分割缓存下的整次运行峰值，不能分摊成每个算法的独立峰值。",
        "",
        "| 默认组合 | 共享候选构建/秒 | 分割/秒 | 关联/秒 | 关联文件 MiB |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in nominal:
        runtime = r["runtime_seconds"]
        rows.append(
            f"| {SEG_NAMES[r['segmentation']]}＋{MERGE_NAMES[r['merge']]} | {runtime['support_seconds']:.1f} | {runtime['segmentation_seconds']:.1f} | {runtime['merge_seconds']:.1f} | {r['assignment_output_bytes'] / 1024**2:.1f} |"
        )
    rows += [
        "",
        "共享候选构建在各行重复列示但实际每车只执行一次；同一种分割也由不同融合配置复用，不能把表格各列相加当总耗时。关联列含时空匹配前置步骤，写盘/读取和汇总开销体现在全程耗时。软件和机器信息保存在 manifest；CPU 时间及每方法独立内存峰值未测。单月结果支持本地批处理可行性，尚不能证明多月增量、并行或生产部署能力。",
        "",
        "## 7. 条件性建议",
        "",
        "可以继续 APC–AVL 优先路线：把时空约束关联作为直接证据层，保留独立原始记录，再把序列延续作为可修订的推断层。融合不能把一个 APC 记录按多个 GPS 点重复累计，也不能把 AVL 缺失处补成观测点。",
        "",
        "仅时间方法适合作为宽候选基线；它的较高关联量不能作为可信覆盖。时间缺口分割适合生成连续观测块；长块和多方向块提示它不足以直接声明一趟运行。AVL 上下文与 APC 辅助方法可作为边界候选的两种补充：使用它们的实测覆盖/弃判/碎片化取舍选择下一轮复核范围，不按方向纯度或关联数量直接封为最佳方法。",
        "",
        "先保留严格/默认设置作为人工复核候选，同时把宽松设置新增的关联和所有单源前后缀单列抽样。需要分别标注同车不同趟次、折返、内部缺口、时钟偏移和汇总 APC 情形，冻结规则后再做独立车辆日留出验证。没有这些标签之前，本次只能给出证据一致性与工程可行性的条件结论，不能给出各种方法真实准确率的优劣排序。",
        "",
        "本次没有打开旧标签/留出制品，但已经观察了当前整月 raw 数据。与本次研究重叠的车辆日不能再被称为对本轮规则选择完全未见的原始数据留出集。未来独立验证需要新的、事先声明的评估设计，优先采用新的源数据窗口；旧协议及旧标签状态本次不修改。",
        "",
    ]
    return "\n".join(rows)


def run_analysis(args):
    os.umask(0o077)
    started = time.perf_counter()
    run = args.run.resolve()
    if not (run / "completion.json").exists():
        raise RuntimeError(
            "Completed factorial marker is required; partial analysis is refused"
        )
    completion, manifest = (
        read_json(run / "completion.json"),
        read_json(run / "manifest.json"),
    )
    if (
        not completion.get("full_fixture")
        or completion["completed_vehicles"] != manifest["selected_vehicles"]
    ):
        raise RuntimeError("Full selected fixture has not completed")
    summaries = read_json(run / "factorial_summary.json")
    if len(summaries) != completion["combinations"]:
        raise RuntimeError("Combination count differs from completion marker")
    allowed = run.parent
    out = args.out.resolve()
    if out.parent != allowed or out == run or out.exists():
        raise ValueError("Analysis requires a new sibling output directory")
    audit = args.audit.resolve()
    raw = read_json(audit / "inventory.json")
    hashes = {name: hashed(audit / name) for name in manifest["canonical_input_sha256"]}
    hashes_match = hashes == manifest["canonical_input_sha256"]
    if not hashes_match:
        raise RuntimeError("Canonical source changed since completed run")
    if any(
        raw["sources"][name]["row_count"] != manifest["input_rows"][name]
        for name in ("avl", "apc")
    ):
        raise RuntimeError("Raw audit and completed run source counts disagree")
    apc = pl.read_parquet(
        audit / "apc.parquet",
        columns=[
            "rid",
            "arrival",
            "departure",
            "avg_sample",
            "lat",
            "lon",
            "invalid",
            "grain_uncertain",
            "time_ambiguous",
            "time_nonexistent",
        ],
    )
    apc = apc.rename({"grain_uncertain": "source_grain_uncertain"}).with_columns(
        pl.lit(True).alias("source_exists")
    )
    if (
        apc.height != raw["sources"]["apc"]["row_count"]
        or apc["rid"].n_unique() != apc.height
    ):
        raise RuntimeError("Canonical APC source identity/count integrity failed")
    if raw["sources"]["avl"]["invalid"]["count"]:
        raise RuntimeError(
            "Chunk full-source ledger must first be extended for invalid source rows"
        )
    config = read_json(run / "config.json")
    chunks = chunk_analysis(run, raw["sources"]["avl"])
    results = {}
    for key, summary in sorted(summaries.items()):
        results[key] = analyze_combination(
            run, key, summary, apc, chunks, config, raw["sources"]["avl"]
        )
        print(
            json.dumps({"analyzed": key, "rows": results[key]["apc_rows"]}), flush=True
        )
    general_probe = load_probe(args.probes, manifest) if args.probes else {}
    edge_probe = load_probe(run.parent / "probes-edge02", manifest)
    probe = general_probe.get("summary", {})
    checks = general_probe.get("checks", {})
    counterexample_path = run.parent / "segmentation_counterexamples.json"
    counterexamples = load_counterexamples(counterexample_path, manifest)
    rubric = build_rubric(results, chunks, hashes_match, checks, counterexamples)
    rubric["probe_populations"] = {"general": general_probe, "edge": edge_probe}
    junit_path = run.parent / "verification-tests-final.xml"
    if not junit_path.exists():
        junit_path = run.parent / "verification-tests.xml"
    rubric["implementation_verification"] = junit_verification(junit_path)
    output_bytes = bytes_under(run)
    out.mkdir(mode=0o700)
    assessment = {
        "version": "coverage-results-analysis-v1",
        "run": str(run),
        "completion": completion,
        "canonical_hashes_unchanged": hashes_match,
        "chunk_metrics": chunks,
        "combinations": results,
        "probe_results": probe,
        "probe_checks": checks,
        "probe_populations": {"general": general_probe, "edge": edge_probe},
        "implementation_verification": rubric["implementation_verification"],
        "constructed_robustness_counterexamples": counterexamples,
        "run_output_bytes": output_bytes,
        "independent_reviewed_truth_labels": 0,
        "accuracy": None,
        "publishable": False,
    }
    dump(out / "assessment.json", assessment)
    dump(out / "rubric_check_results.json", rubric)
    (out / "report.md").write_text(
        render_report(
            results,
            chunks,
            completion,
            manifest,
            rubric,
            probe,
            checks,
            output_bytes,
            out.name,
        )
    )
    (out / "report.md").chmod(0o600)
    dump(
        out / "completion.json",
        {
            "full_fixture_analyzed": True,
            "combinations": len(results),
            "wall_seconds": time.perf_counter() - started,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "script_sha256": hashed(Path(__file__)),
            "probes_integrated": bool(probe),
            "publishable": False,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--probes", type=Path)
    run_analysis(parser.parse_args())
