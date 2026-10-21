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
    "gap_only": "Gap-only segmentation",
    "avl_context": "AVL-context segmentation",
    "apc_assisted": "APC-assisted segmentation",
}
MERGE_NAMES = {
    "time_only": "Time-only baseline",
    "spatiotemporal": "Spatiotemporal association",
    "sequence_extension": "Spatiotemporal + sequence extension",
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
    return "undefined" if value is None else f"{value:.2%}"


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
        "# APC–AVL segmentation and merge method comparison (2026-09-08)",
        "",
        f"The current data supports building traceable APC–AVL spatiotemporal associations first, but it is not yet sufficient to certify observed chunks as verified `trip performed` records. Taking AVL-context segmentation with nominal parameters as an example, spatiotemporal association directly links {n(reference_direct['counts']['direct'])} APC rows ({percent(reference_direct['direct_rate_all_apc'])} of all APC), and sequence extension adds a further {n(reference_sequence['counts']['inferred'])} inferred associations; these proportions represent evidence coverage, not accuracy.",
        "",
        f"The full fixture of {n(counts['avl'])} AVL rows and {n(counts['apc'])} APC rows was processed, covering the union of {manifest['selected_vehicles']} source vehicle identifiers, across {len(results)} method/threshold combinations. The objects of analysis are observed chunks and association hypotheses. There are 0 independently reviewed truth labels; association accuracy and trip-boundary accuracy are both undefined.",
        "",
        "This study was conducted independently under the user's new request: it did not read the old blind labels, did not run the factorial frozen under the old protocol, did not use GTFS, and did not generate production TIDES data. Raw files remain local; this document contains only aggregate statistics and no vehicle trajectories, raw identifiers or row-level timestamps.",
        "",
        f"Raw-data issues are covered in the [raw audit](apc-avl-raw-edge-cases-20260908.md), the specification and denominator definitions in the [TIDES evaluation standard](apc-avl-coverage-evaluation-standard.md), and parameters in [study_config.json](../experiments/apc_avl_coverage_study/study_config.json). The access-restricted complete results are in [factorial_summary.csv](../artifacts/apc-avl-coverage-study-20260908/factorial02/factorial_summary.csv) and [{analysis_name}/assessment.json](../artifacts/apc-avl-coverage-study-20260908/{analysis_name}/assessment.json).",
        "",
        "## 1. Nine combinations at nominal thresholds",
        "",
        "Direct: spatiotemporal support from real AVL points or short-interval interpolation features. Inferred: supported by frozen direct anchors, with no GPS points generated. Weak: time-only baseline results, not counted as trusted direct associations. Ambiguous, unmatched and ineligible records are all retained. All row counts use the total raw APC count as the common denominator; the denominator of rows meeting the time/structure eligibility conditions is recorded separately in the complete results.",
        "",
        "| Segmentation | Merge | Direct | Inferred | Weak | Ambiguous | Ineligible | Unmatched | Direct / all APC |",
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
        "## 2. Interpretability of segmentation results",
        "",
        "| Segmentation | Chunks | Median duration (min) | Non-DST median (min) | P95 (min) | < 2 min | > 2 h | Multi-direction chunks |",
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
        "A multi-direction chunk is one observed chunk in which at least two APC source directions each have at least 3 directly associated records. The denominator is all chunks of that segmentation method; the metric flags boundaries that may need further inspection. Source directions have not been authoritatively verified, and APC-assisted segmentation itself uses direction evidence, so this metric carries a self-validation bias and cannot be treated as a true over-merge rate. Shorter chunks or purer directions do not automatically indicate higher quality.",
        "",
        f"All three segmentation methods retain the {n(chunks['gap_only']['time_ambiguous_chunks'])} DST-ambiguous GPS rows as separate uncertain chunks, so the overall median is strongly affected by zero-duration chunks; the non-DST median is listed separately. Retaining position-uncertain points also increases the number of short chunks, so not every short chunk can be interpreted as real operational fragmentation. APC-assisted segmentation still has {n(chunks['apc_assisted']['over_7200s'])} chunks longer than 2 hours and {n(results['apc_assisted__spatiotemporal__nominal']['mixed_source_direction_chunks'])} multi-direction chunks. Combined with the measured ambiguity at shared terminals, this round has not achieved reliable recovery of actual trip boundaries.",
        "",
        "## 3. Trade-offs between strict, nominal and wide parameters",
        "",
        "All numeric thresholds are exploratory configurations. The strict/nominal/wide time tolerances are 30/60/120 s, the spatial radii 50/100/200 m, the maximum interpolation intervals 60/90/180 s, and the sequence-extension horizons 10/15/30 min. Actual acceptance is further constrained by the score and candidate-margin rules; the configured radius is not an automatic acceptance range.",
        "",
        "| Segmentation | Regime | Direct | Added by sequence extension | Ambiguous (sequence extension) | > 100 m / measurable direct | > 60 s / measurable direct |",
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
        "100 m / 60 s are common reference lines across configurations. A wide configuration exceeding these two reference lines does not violate its own 200 m / 120 s rules. Conversely, the time-only baseline cannot be regarded as meeting the common spatial evidence standard even though it produces more associations. Residuals are evidence the algorithm already used, so a smaller residual cannot independently prove an association correct.",
        "",
        "| Nominal combination | Spatial P50/P95 (m) | Temporal gap P95 (s) | Share > 100 m | Share > 60 s |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in nominal:
        d, t = r["sampled_spatial_residual_m"], r["sampled_temporal_gap_s"]
        spatial = "undefined" if d["p50"] is None else f"{d['p50']:.1f}/{d['p95']:.1f}"
        temporal = "undefined" if t["p95"] is None else f"{t['p95']:.1f}"
        rows.append(
            f"| {SEG_NAMES[r['segmentation']]} + {MERGE_NAMES[r['merge']]} | {spatial} | {temporal} | {percent(r['common_100m_excursion_rate'])} | {percent(r['common_60s_excursion_rate'])} |"
        )
    rows += [
        "",
        "## 4. Coverage inconsistency and missing evidence",
        "",
        "| Nominal sequence extension | Chunks with APC lead | Chunks with APC tail | Denominator: chunks with any association | APC tail duration P95 (s) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for method in SEG_NAMES:
        r = results[f"{method}__sequence_extension__nominal"]
        tail = r["apc_trailing_seconds"]["p95"]
        rows.append(
            f"| {SEG_NAMES[method]} | {n(r['apc_leading_assigned_chunks'])} | {n(r['apc_trailing_assigned_chunks'])} | {n(r['coverage_denominator'])} | {tail if tail is not None else 'undefined'} |"
        )
    rows += [
        "",
        "Sequence inference also has a physical-evidence issue that needs separate checking: three distinct stop codes may point to nearby locations. The current kernel checks the distance from the target to its anchors, but does not require physical progression between the three anchors. The following is a 50 m exploratory screen added post hoc in this analysis; it is not a calibrated acceptance threshold.",
        "",
        "| Nominal sequence extension | All inferred rows | Rows with complete anchor coordinates | All three anchors pairwise under 50 m |",
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
        "The following table uses AVL-context segmentation + nominal sequence extension as an example; the categories may overlap and cannot be summed as mutually exclusive totals.",
        "",
        "| Evidence condition | APC denominator | Direct | Inferred | Weak |",
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
        "The distribution of final candidate chunk counts for the nominal combinations is as follows. Sequence inference records the final owner as a single candidate, so this is not the size of the original candidate universe and cannot be used to prove that no trip interpretation was missed.",
        "",
        "| Nominal combination | 0 candidates | 1 candidate | 2 or more candidates | APC denominator |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in nominal:
        cd = r["candidate_count_distribution"]
        rows.append(
            f"| {SEG_NAMES[r['segmentation']]} + {MERGE_NAMES[r['merge']]} | {n(cd.get(0, 0))} | {n(cd.get(1, 0))} | {n(sum(v for k, v in cd.items() if k >= 2))} | {n(r['apc_rows'])} |"
        )
    rows += [
        "",
        "APC rows carrying a grain-uncertainty signal can still form diagnostic spatiotemporal candidates, but this cannot close the APC publication gate. `AvgSample=1` is likewise not contractual proof of a single stop visit. Unmatched rows may result from missing coverage, physical inconsistency, insufficient candidates or cross-boundary ambiguity; they must not be uniformly interpreted as bad data.",
        "",
        "The first and last timestamps of a real point sequence constitute only an observation envelope. All output `actual_trip_start/end` fields remain null. For the AVL 13:00–14:00, APC 13:15–14:13 case, an inferred APC tail may be retained when supported by independent direct anchors and continuity evidence; 13:00–14:13 is not designated as a verified actual trip start and end.",
        "",
        "## 5. Robustness checks and credibility boundaries",
        "",
    ]
    validation = rubric["implementation_verification"]
    if validation["status"] != "not_evaluated":
        probe_count = sum(
            p.get("completion", {}).get("checks", 0)
            for p in rubric["probe_populations"].values()
        )
        rows += [
            f"The project and study implementation tests total {validation['tests']}, with {validation['failures']} failures and {validation['errors']} errors; see the [JUnit artifact](../artifacts/apc-avl-coverage-study-20260908/{validation['filename']}). The general and targeted probes additionally executed {probe_count} scoped checks. Passing these checks means the corresponding implementation behavior was verified; the real boundary limitations and constructed counterexamples described below still hold.",
            "",
        ]
    if probe:
        general = rubric["probe_populations"]["general"]
        rows += [
            f"The general probes selected {general['completion']['vehicle_windows']} fixed 2-hour vehicle windows, sampled by raw-data presence rather than by matching outcome. They cover {len(probe)} segmentation/merge/perturbation configurations and {general['completion']['probe_combinations']} window × configuration comparisons; see the access-restricted [probes01/probe_summary.json](../artifacts/apc-avl-coverage-study-20260908/probes01/probe_summary.json). The masking experiments use the unmasked algorithm output as the reference and count retained ownership, abstentions, direct-to-inferred transitions and disjoint chunk switches. These are stability metrics under missing data and cannot be called true matching accuracy.",
            "",
        ]
        rows += [
            "| Perturbation | Configurations | Retained APC comparisons | Reference-assigned then abstained | Direct to inferred, same membership | Disjoint chunk switches |",
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
            "Comparison counts in the table are repeated across algorithms and cannot be treated as independent sample sizes. Chunks with no surviving GPS support to compare against are listed separately as non-comparable; they must not be assigned a fabricated Jaccard=0 or counted as switches. The complete probe check status is in `checks.json`; any partial perturbation failures must be retained and explained.",
            "",
        ]
        negative = probe["apc_assisted__spatiotemporal__apc_clock_-60s"]
        positive = probe["apc_assisted__spatiotemporal__apc_clock_+60s"]
        negative_two = probe["apc_assisted__spatiotemporal__apc_clock_-120s"]
        positive_two = probe["apc_assisted__spatiotemporal__apc_clock_+120s"]
        rows += [
            f"Clock offset is a clear sensitivity point: in the general windows, the reference direct associations for APC-assisted segmentation + spatiotemporal association number {n(negative['reference_direct'])}; after artificially shifting the APC clock by -60/+60 s, associations drop to {n(negative['perturbed_assigned'])}/{n(positive['perturbed_assigned'])}, and after -120/+120 s to {n(negative_two['perturbed_assigned'])}/{n(positive_two['perturbed_assigned'])}. This experiment does not estimate the real TTC clock offset; it shows that a clock contract and offset diagnosis must precede any increase in association tolerance.",
            "",
        ]
    else:
        rows += [
            "No completed probes01 was found when this report was generated; perturbation recovery and full-fixture rerun determinism are both marked not evaluated, and synthetic tests are not substituted for them.",
            "",
        ]
    edge = rubric["probe_populations"].get("edge", {})
    if edge:
        dst_duplication = edge["by_stratum_summary"]["dst_ambiguous_hour"][
            "apc_assisted__sequence_extension__exact_avl_duplication"
        ]
        rows += [
            "Targeted probes sampled by raw boundary risk were also executed, drawn from shared-terminal overlaps, DST ambiguity, extraction-window ends and internal AVL gaps. They form a different sample population from the general windows above; the final results are stored in [probes-edge02](../artifacts/apc-avl-coverage-study-20260908/probes-edge02/probe_summary.json) and cannot be pooled into a single overall accuracy. The earlier edge01 is retained for diagnostics and is not used as the final statistics.",
            "",
            f"These targeted probes executed {edge['completion'].get('probe_combinations', 'see artifact')} window × configuration comparisons; the number of failed checks is {len(edge['completion'].get('failed_checks', []))}. Complete denominators, stratified results and any failures are retained in their manifest, summary and checks.",
            "",
        ]
        rows += [
            'The following table uses APC-assisted segmentation + nominal sequence extension throughout, with the joint masking "APC prefix 15 min, AVL suffix 13 min" applied. 2 windows were selected for each of the four raw evidence conditions; both the reference and perturbed associations are algorithm references only.',
            "",
            "| Targeted raw evidence | Retained APC denominator | Reference assigned | Assigned after masking | Direct to inferred, same membership | Reference-assigned then abstained |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for stratum, values in edge.get("by_stratum_summary", {}).items():
            m = values["apc_assisted__sequence_extension__offset_coverage_15m_13m"]
            rows.append(
                f"| {stratum} | {n(m['retained_apc'])} | {n(m['reference_assigned'])} | {n(m['perturbed_assigned'])} | {n(m['direct_to_inferred_same_membership'])} | {n(m['reference_assigned_then_abstained'])} |"
            )
        rows += [
            "",
            "In the targeted real samples, two APC rows at the same stop in opposite directions with intervals overlapping by 27 seconds can both be directly associated with the same observed chunk. This shows that direct GPS support can hold while the actual trip boundary remains unresolved. Another shared-terminal sample remains ambiguous/unmatched. Single-source continuation at the end of the month did not recover the reference associations in these masked samples, so not every coverage tail can be treated as a completable same-trip continuation.",
            "",
            f"In that APC-assisted + sequence-extension DST targeted window, duplicating the GPS rows increased the raw chunk total from {n(dst_duplication['reference_chunk_count'])} to {n(dst_duplication['perturbed_chunk_count'])}; the accepted owner relations projected onto the surviving source-record set can remain unchanged. These two metrics describe different objects. The {edge['completion']['checks']} targeted checks cover only explicitly declared rules and do not mean that duplicate data has no effect on the chunk count.",
            "",
        ]
    if rubric["constructed_robustness_counterexamples"]:
        rows += [
            'The core code hashes, statuses and results of the independently constructed cases were verified, and three negative results were actually reproduced; see [segmentation_counterexamples.json](../artifacts/apc-avl-coverage-study-20260908/segmentation_counterexamples.json): duplicating a single GPS measurement can change context segmentation from 1 chunk to 2; duplicating all GPS rows can change a geometric reversal from 2 chunks to 1; and a shared terminal between consecutive roles with overlapping arrival/departure intervals can change the APC direction split from 2 chunks to 1. Even when the duplicate-data probes on the general windows pass, they do not eliminate these counterexamples. They limit the generalization robustness of the segmentation methods and cannot be offset by "no runtime anomalies".',
            "",
        ]
    else:
        rows += [
            "This analysis lacks a verified constructed-counterexample artifact; its results are not evaluated.",
            "",
        ]
    rows += [
        "The kernel computes candidate margin per observed chunk: competition between multiple stops or adjacent trips within the same long chunk is collapsed into one owner. A high direct association rate on long chunks therefore does not mean trip identity is clear. The APC source grain flag includes non-integer count/load signals, and APC-assisted segmentation excludes those records; it can be said that passenger-count values were not used as a positive matching score, but not that the whole method is entirely independent of the count fields.",
        "",
        f"Among the direct associations of nominal AVL-context segmentation + spatiotemporal association, {n(reference_direct['source_route_conflicts'])} APC rows still differ from the source route label of the supporting GPS. Direct association here confirms only local spatiotemporal support, not route or trip semantics.",
        "",
    ]
    fail_count = sum(
        x["status"] == "fail"
        for values in rubric["combination_rules"].values()
        for x in values
    )
    rows += [
        f'The machine-readable [rubric_check_results.json](../artifacts/apc-avl-coverage-study-20260908/{analysis_name}/rubric_check_results.json) records the actual scope, denominator and unchecked portion of every rule. The number of hard-rule failures observed in this analysis is {fail_count} (counted per configuration × rule); partially completed or unexecuted rules are explicitly marked `not_evaluated`, and there is no claim that "all TIDES gates pass".',
        "",
        "The following were verified: a single disposition per APC row, chunk membership for all AVL rows, raw GPS references, no fabricated GPS in inferences, no acceptance of time-uncertain records, and no promotion of outputs to actual trips. Exhaustive enumeration of boundary candidates, independent recomputation of every interpolation residual, and invariance under all input permutations/batchings remain incomplete. The P02–P07 items (time contract, APC grain, event semantics, complete stop order and actual trip identity) remain unresolved; P01 has no production package available for publication validation.",
        "",
        "## 6. Local run feasibility",
        "",
        f"The full 21-combination run took {completion['wall_seconds'] / 60:.1f} minutes, with a peak process RSS of {completion['peak_rss_mib'] / 1024:.2f} GiB and a run directory totaling {output_bytes / (1024**3):.2f} GiB. This is the whole-run peak under a shared process, shared support candidates and segmentation caches; it cannot be apportioned into independent per-algorithm peaks.",
        "",
        "| Nominal combination | Shared candidate build (s) | Segmentation (s) | Association (s) | Assignment files (MiB) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in nominal:
        runtime = r["runtime_seconds"]
        rows.append(
            f"| {SEG_NAMES[r['segmentation']]} + {MERGE_NAMES[r['merge']]} | {runtime['support_seconds']:.1f} | {runtime['segmentation_seconds']:.1f} | {runtime['merge_seconds']:.1f} | {r['assignment_output_bytes'] / 1024**2:.1f} |"
        )
    rows += [
        "",
        "The shared candidate build is repeated on every row but actually executes once per vehicle; each segmentation is likewise reused across merge configurations, so the table columns cannot be summed as total runtime. The association column includes the spatiotemporal matching pre-step; disk write/read and aggregation overheads appear in the wall-clock time. Software and machine information is stored in the manifest; CPU time and per-method independent memory peaks were not measured. The single-month result supports local batch feasibility; it does not yet demonstrate multi-month incremental, parallel or production deployment capability.",
        "",
        "## 7. Conditional recommendations",
        "",
        "The APC–AVL-first route can continue: use spatiotemporal association as the direct evidence layer, preserve independent raw records, and then treat sequence extension as a revisable inference layer. The merge must not repeat-count one APC record across multiple GPS points, nor fill AVL gaps with fabricated observation points.",
        "",
        "The time-only method is suitable as a wide candidate baseline; its higher association count cannot be taken as trusted coverage. Gap-only segmentation is suitable for producing continuous observation chunks; long and multi-direction chunks indicate that it is insufficient to declare a single trip directly. The AVL-context and APC-assisted methods can serve as two complementary sources of boundary candidates: use their measured coverage/abstention/fragmentation trade-offs to select the next review scope, rather than crowning a best method by direction purity or association count.",
        "",
        "Retain the strict/nominal settings as human-review candidates for now, while sampling separately the associations added by the wide setting and all single-source prefixes/suffixes. Same-vehicle different-trip, reversal, internal-gap, clock-offset and aggregated-APC cases need to be labeled separately, followed by independent vehicle-day holdout validation after the rules are frozen. Without these labels, this study can only offer conditional conclusions on evidence consistency and engineering feasibility, not a ranking of the methods by true accuracy.",
        "",
        "This study did not open the old label/holdout artifacts, but it did inspect the entire current raw month. Vehicle-days overlapping this study can no longer be called a raw-data holdout set completely unseen by this round's rule selection. Future independent validation needs a new, pre-declared evaluation design, preferably using fresh source-data windows; the old protocol and old label status are not modified in this round.",
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
