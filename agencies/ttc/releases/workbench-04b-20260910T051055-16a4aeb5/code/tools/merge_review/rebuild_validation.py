"""Compare completed rebuilt and retained runs by business values, read-only.

This is a reproducibility check, not a byte-identity or matching-accuracy claim.
Original observation and scheduled timestamps are always compared. Only named
processing metadata is ignored; cases are loaded one at a time.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import time
from pathlib import Path

import polars as pl

from tools.merge_review.gtfs_matching import REPRESENTATION
from tools.merge_review.gtfs_matching.feed import file_sha256
from tools.merge_review.gtfs_matching.runner import artifact_key, read_case

MODES = ("global", "early", "middle", "late")
PROCESSING_KEYS = frozenset(
    {
        "runtime_s",
        "elapsed_s",
        "upstream_processing_elapsed_s",
        "created_at",
        "created_utc",
        "scorer_fingerprint",
        "code_sha256",
        "partial_code_sha256",
    }
)
CONFIG_LOCATION_KEYS = frozenset(
    {
        "feed_manifest_path",
        "feed_gtfs_root",
        "execution_workers",
        "continuous_scorer_fingerprint",
    }
)
DERIVED_BOUNDARY_KEYS = frozenset(
    {
        "gtfs_status",
        "gtfs_candidate_count",
        "pattern_confidence",
        "trip_confidence",
        "matching_modes",
    }
)
MANIFEST_FIELDS = (
    "debug_date",
    "scope",
    "representation",
    "route_scope",
    "excluded_routes",
    "route_id",
    "gtfs_feed_version",
    "gtfs_archive_sha256",
    "source_counts",
    "avl_deduplication",
    "joint_gap_segmentation",
    "available_matching_modes",
)
SUMMARY_FIELDS = (
    "parent_cases",
    "gap_split_parent_cases",
    "joint_gap_count",
    "gap_unlocated_observations",
    "avl",
    "apc",
    "excluded_observations",
    "cases",
    "projection_points",
    "off_reference_points",
    "observations",
    "vehicles",
    "retained_counts",
    "status_counts",
    "matching_mode_status_counts",
    "joint_gap_segmentation",
    "scorer",
)
FEED_FIELDS = (
    "feed_version",
    "archive_sha256",
    "date",
    "route_id",
    "route_short_name",
    "route",
    "active_service_ids",
    "service_by_day",
    "stop_code_crosswalk",
)


def _read(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _safe_file(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("Run artifact paths must be relative")
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Run artifact path escapes its run")
    return path


def _preview(value):
    if isinstance(value, dict):
        return {"type": "object", "keys": sorted(value)[:15], "length": len(value)}
    if isinstance(value, (list, tuple)):
        return {"type": "array", "length": len(value), "first": str(value[:2])[:250]}
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "…"
    return (
        str(value) if isinstance(value, float) and not math.isfinite(value) else value
    )


class _Comparison:
    def __init__(self, absolute_tolerance, relative_tolerance, max_differences):
        self.absolute_tolerance = absolute_tolerance
        self.relative_tolerance = relative_tolerance
        self.max_differences = max_differences
        self.count = 0
        self.examples = []
        self.by_stage = collections.Counter()
        self.ignored = collections.Counter()

    def difference(self, stage, path, expected, actual, reason="value_changed"):
        self.count += 1
        self.by_stage[stage] += 1
        if len(self.examples) < self.max_differences:
            self.examples.append(
                {
                    "stage": stage,
                    "path": path,
                    "reason": reason,
                    "expected": _preview(expected),
                    "actual": _preview(actual),
                }
            )

    def compare(self, expected, actual, stage, path, *, ignored=PROCESSING_KEYS):
        if isinstance(expected, dict) and isinstance(actual, dict):
            keys = expected.keys() | actual.keys()
            for key in sorted(keys):
                if key in ignored:
                    self.ignored[key] += 1
                elif key not in expected or key not in actual:
                    self.difference(
                        stage,
                        f"{path}/{key}",
                        expected.get(key),
                        actual.get(key),
                        "field_added" if key not in expected else "field_missing",
                    )
                else:
                    self.compare(
                        expected[key],
                        actual[key],
                        stage,
                        f"{path}/{key}",
                        ignored=ignored,
                    )
            return
        if isinstance(expected, (tuple, list)) and isinstance(actual, (tuple, list)):
            if len(expected) != len(actual):
                self.difference(
                    stage, path, len(expected), len(actual), "length_changed"
                )
            for index, (left, right) in enumerate(zip(expected, actual, strict=False)):
                self.compare(left, right, stage, f"{path}/{index}", ignored=ignored)
            return
        # Booleans and integral identifiers are exact, never float-tolerant.
        if isinstance(expected, bool) or isinstance(actual, bool):
            same = type(expected) is type(actual) and expected == actual
        elif isinstance(expected, int) and isinstance(actual, int):
            same = expected == actual
        elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            same = (
                math.isfinite(expected)
                and math.isfinite(actual)
                and math.isclose(
                    expected,
                    actual,
                    rel_tol=self.relative_tolerance,
                    abs_tol=self.absolute_tolerance,
                )
            )
        else:
            same = type(expected) is type(actual) and expected == actual
        if not same:
            self.difference(stage, path, expected, actual)


def _indexed(rows, key, description):
    result = {}
    for row in rows:
        identity = row.get(key)
        if not isinstance(identity, str) or not identity or identity in result:
            raise ValueError(f"Missing or duplicate {description} identity")
        result[identity] = row
    return result


def _verify_run(root):
    completion = _read(root / "completion.json")
    if completion.get("validation_ok") is not True:
        raise ValueError("Semantic audit requires completed, validated runs")
    manifest = _read(root / "manifest.json")
    if manifest.get("representation") != REPRESENTATION:
        raise ValueError("Semantic audit requires a GTFS candidate run")
    if set(manifest.get("available_matching_modes", [])) != set(MODES):
        raise ValueError("Semantic audit requires all four matching modes")
    pins = _read(root / "output_sha256.json")
    for relative, digest in pins.items():
        path = _safe_file(root, relative)
        if file_sha256(path) != digest:
            raise ValueError(f"Stored output pin failed: {root.name}/{relative}")
    required = {
        "manifest.json",
        "config.json",
        "summary.json",
        "selection.json",
        "gtfs/canonical.json",
        "gtfs/feed.json",
    }
    if not required <= pins.keys():
        raise ValueError("Required run control files lack output pins")
    return manifest, pins


def _select(value, fields):
    return {key: value[key] for key in fields if key in value}


def audit_rebuild(
    source_run,
    rebuilt_run,
    *,
    absolute_tolerance=1e-6,
    relative_tolerance=0.0,
    max_differences=100,
    progress=None,
):
    """Return a semantic PASS/FAIL report, without writing either run.

    Absolute tolerance applies to floats; integer source IDs/counts stay exact.
    Relative tolerance defaults to zero so epoch-valued clocks do not gain a
    seconds-wide tolerance. Ranking identities are compared exactly regardless
    of score tolerance. Malformed/incomplete/unpinned inputs raise ValueError.
    """
    for value in (absolute_tolerance, relative_tolerance):
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError(
                "Numeric comparison tolerances must be finite and nonnegative"
            )
    if (
        isinstance(max_differences, bool)
        or not isinstance(max_differences, int)
        or max_differences < 1
    ):
        raise ValueError("max_differences must be a positive integer")
    started = time.perf_counter()
    source, rebuilt = Path(source_run).resolve(), Path(rebuilt_run).resolve()
    if source == rebuilt:
        raise ValueError("Source and rebuilt runs must be distinct directories")
    compare = _Comparison(absolute_tolerance, relative_tolerance, max_differences)
    manifests, pins = {}, {}
    for side, root in (("source", source), ("rebuilt", rebuilt)):
        if progress:
            progress({"stage": "verifying_output_pins", "side": side})
        manifests[side], pins[side] = _verify_run(root)
    compare.compare(
        _select(manifests["source"], MANIFEST_FIELDS),
        _select(manifests["rebuilt"], MANIFEST_FIELDS),
        "scope",
        "manifest",
    )
    for relative, fields, stage in (
        ("summary.json", SUMMARY_FIELDS, "summary"),
        ("gtfs/feed.json", FEED_FIELDS, "feed"),
    ):
        compare.compare(
            _select(_read(source / relative), fields),
            _select(_read(rebuilt / relative), fields),
            stage,
            relative,
        )
    compare.compare(
        _read(source / "config.json"),
        _read(rebuilt / "config.json"),
        "configuration",
        "config",
        ignored=PROCESSING_KEYS | CONFIG_LOCATION_KEYS,
    )
    compare.compare(
        _read(source / "gtfs/canonical.json"),
        _read(rebuilt / "gtfs/canonical.json"),
        "canonical",
        "canonical",
    )
    vehicles = {
        side: _indexed(_read(root / "selection.json"), "vehicle_case", "vehicle")
        for side, root in (("source", source), ("rebuilt", rebuilt))
    }
    if not vehicles["source"] or not vehicles["rebuilt"]:
        raise ValueError("Cannot certify an empty rebuilt experiment")
    compare.compare(
        vehicles["source"], vehicles["rebuilt"], "source_identity", "vehicles"
    )
    source_counts = {side: collections.Counter() for side in vehicles}
    catalogs = {side: {} for side in vehicles}
    for vehicle in sorted(vehicles["source"].keys() | vehicles["rebuilt"].keys()):
        frames = {}
        for side, root in (("source", source), ("rebuilt", rebuilt)):
            if vehicle not in vehicles[side]:
                continue
            for relative in (
                f"observations/{vehicle}.parquet",
                f"catalog/{vehicle}.json",
                f"scope/{vehicle}.json",
            ):
                if relative not in pins[side]:
                    raise ValueError(
                        f"Required vehicle artifact lacks a pin: {relative}"
                    )
            frame = pl.read_parquet(root / "observations" / f"{vehicle}.parquet")
            if not {"source", "rid"} <= set(frame.columns):
                raise ValueError("Observation table lacks source identities")
            if frame.select("source", "rid").n_unique() != frame.height:
                raise ValueError("Observation table duplicates a source identity")
            frames[side] = frame.sort("source", "rid").select(sorted(frame.columns))
            source_counts[side].update(frame["source"].to_list())
            current = _indexed(
                _read(root / "catalog" / f"{vehicle}.json"), "id", "case"
            )
            if catalogs[side].keys() & current.keys():
                raise ValueError("Repeated case identity across vehicles")
            catalogs[side].update(current)
        if set(frames) != {"source", "rebuilt"}:
            continue
        compare.compare(
            {key: str(dtype) for key, dtype in frames["source"].schema.items()},
            {key: str(dtype) for key, dtype in frames["rebuilt"].schema.items()},
            "source_schema",
            vehicle,
        )
        # All original values, including audit-only fields, use exact equality.
        if not frames["source"].equals(frames["rebuilt"]):
            exact = _Comparison(0, 0, max_differences)
            exact.compare(
                frames["source"].to_dicts(),
                frames["rebuilt"].to_dicts(),
                "source_observations",
                vehicle,
                ignored=frozenset(),
            )
            if exact.count:
                compare.count += exact.count
                compare.by_stage.update(exact.by_stage)
                remaining = max(0, max_differences - len(compare.examples))
                compare.examples.extend(exact.examples[:remaining])
            else:
                compare.difference(
                    "source_observations",
                    vehicle,
                    "equal frame",
                    "unequal frame",
                    "typed_frame_changed",
                )
        compare.compare(
            _read(source / "scope" / f"{vehicle}.json"),
            _read(rebuilt / "scope" / f"{vehicle}.json"),
            "segmentation",
            f"scope/{vehicle}",
        )
    if not catalogs["source"] or not catalogs["rebuilt"]:
        raise ValueError("Cannot certify an experiment without performed candidates")
    compare.compare(catalogs["source"], catalogs["rebuilt"], "segmentation", "catalog")
    checks = collections.Counter(vehicles=len(vehicles["rebuilt"]))
    mode_stats = {
        mode: {
            "source_status_counts": collections.Counter(),
            "rebuilt_status_counts": collections.Counter(),
            "top1_changes": 0,
            "candidate_order_changes": 0,
        }
        for mode in MODES
    }
    cases = sorted(catalogs["source"].keys() & catalogs["rebuilt"].keys())
    expected_files = {
        side: {
            f"cases/{artifact_key(case)}{manifests[side].get('case_artifact_suffix', '.json.gz')}"
            for case in catalog
        }
        for side, catalog in catalogs.items()
    }
    for side in pins:
        if expected_files[side] != {
            name for name in pins[side] if name.startswith("cases/")
        }:
            raise ValueError("Pinned case files disagree with catalog membership")
    for index, case in enumerate(cases, 1):
        artifacts = {}
        for side, root in (("source", source), ("rebuilt", rebuilt)):
            suffix = manifests[side].get("case_artifact_suffix", ".json.gz")
            artifacts[side] = read_case(
                root / "cases" / f"{artifact_key(case)}{suffix}"
            )
            if artifacts[side].get("case_id") != case:
                raise ValueError("Case artifact content disagrees with its catalog ID")
            if set(artifacts[side].get("matching_modes", {})) != set(MODES):
                raise ValueError("Case lacks one of the four matching modes")
        left, right = artifacts["source"], artifacts["rebuilt"]
        for field in ("projection", "apc_evidence"):
            compare.compare(left.get(field), right.get(field), field, f"{case}/{field}")
        compare.compare(
            {
                key: value
                for key, value in left["parent_boundary"].items()
                if key not in DERIVED_BOUNDARY_KEYS
            },
            {
                key: value
                for key, value in right["parent_boundary"].items()
                if key not in DERIVED_BOUNDARY_KEYS
            },
            "segmentation",
            f"{case}/parent_boundary",
        )
        compare.compare(
            left["config"],
            right["config"],
            "configuration",
            f"{case}/config",
            ignored=PROCESSING_KEYS | CONFIG_LOCATION_KEYS,
        )
        for mode in MODES:
            before, after = left["matching_modes"][mode], right["matching_modes"][mode]
            compare.compare(
                before["selection"],
                after["selection"],
                "decision_scope",
                f"{case}/{mode}/selection",
            )
            for side, entry in (("source", before), ("rebuilt", after)):
                result = entry["result"]
                mode_stats[mode][f"{side}_status_counts"][result["status"]] += 1
                _indexed(
                    result.get("all_candidates", []),
                    "candidate_id",
                    "scheduled candidate",
                )
            ranking_before = [
                row["candidate_id"]
                for row in before["result"].get("all_candidates", [])
            ]
            ranking_after = [
                row["candidate_id"] for row in after["result"].get("all_candidates", [])
            ]
            mode_stats[mode]["top1_changes"] += ranking_before[:1] != ranking_after[:1]
            mode_stats[mode]["candidate_order_changes"] += (
                ranking_before != ranking_after
            )
            compare.compare(
                before["result"], after["result"], "matching", f"{case}/{mode}/result"
            )
            checks["mode_results"] += 1
            checks["candidate_scores"] += len(ranking_after)
        # The top-level result is a duplicated global presentation, not a fifth mode.
        for side, artifact in artifacts.items():
            if artifact["result"] != artifact["matching_modes"]["global"]["result"]:
                compare.difference(
                    "global_mirror",
                    f"{case}/{side}",
                    "global result mirror",
                    "different top-level result",
                )
        checks["cases"] += 1
        if progress:
            progress(
                {
                    "stage": "comparing_cases",
                    "completed": index,
                    "total": len(cases),
                    "case_id": case,
                    "elapsed_s": round(time.perf_counter() - started, 3),
                }
            )
    return {
        "schema_version": "rebuild-semantic-audit-v1",
        "status": "FAIL" if compare.count else "PASS",
        "semantic_equal": compare.count == 0,
        "byte_identity_claimed": False,
        "source_run": str(source),
        "rebuilt_run": str(rebuilt),
        "checks": dict(checks),
        "verified_output_pins": {side: len(items) for side, items in pins.items()},
        "source_counts": {side: dict(counts) for side, counts in source_counts.items()},
        "mode_statistics": mode_stats,
        "numeric_tolerance": {
            "absolute": absolute_tolerance,
            "relative": relative_tolerance,
            "integers_exact": True,
            "raw_observations_exact": True,
        },
        "difference_count": compare.count,
        "differences_by_stage": dict(compare.by_stage),
        "differences": compare.examples,
        "difference_examples_truncated": compare.count > len(compare.examples),
        "ignored_processing_fields": sorted(PROCESSING_KEYS),
        "ignored_configuration_fields": sorted(CONFIG_LOCATION_KEYS),
        "ignored_field_occurrences": dict(compare.ignored),
        "elapsed_s": round(time.perf_counter() - started, 3),
        "limits": [
            "Original and scheduled observation timestamps are never ignored.",
            "Run paths, creation times, source-code provenance and runtime are not business equality.",
            "Source CSV ingestion independence must be established by the rebuild orchestrator, not this comparison.",
            "Shadow comparator history differs by design and is not compared here.",
            "Historical results are regression references, not independent accuracy labels.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--rebuilt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--absolute-tolerance", type=float, default=1e-6)
    parser.add_argument("--relative-tolerance", type=float, default=0.0)
    parser.add_argument("--max-differences", type=int, default=100)
    args = parser.parse_args()
    output = args.output.resolve()
    if any(
        output.is_relative_to(root.resolve()) for root in (args.source, args.rebuilt)
    ):
        parser.error("Audit report must be outside both immutable run directories")
    if output.exists():
        parser.error("Audit output already exists; use a new report path")
    report = audit_rebuild(
        args.source,
        args.rebuilt,
        absolute_tolerance=args.absolute_tolerance,
        relative_tolerance=args.relative_tolerance,
        max_differences=args.max_differences,
        progress=lambda value: print(json.dumps(value), flush=True),
    )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with output.open("x", encoding="utf-8") as stream:
        output.chmod(0o600)
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(output),
                "difference_count": report["difference_count"],
            }
        ),
        flush=True,
    )
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
