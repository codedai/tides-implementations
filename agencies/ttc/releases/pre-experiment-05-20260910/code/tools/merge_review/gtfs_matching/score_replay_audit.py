"""Independent, read-only validation of a four-mode scoring replay.

Projection and selection must be unchanged, but delay paths, local alignment,
candidate costs and final decisions may change. This auditor does not call the
matcher, aggregator, decision guard or overview builder. Aggregate arithmetic is
recomputed from persisted local evidence; alignment distances and accuracy are
not independently recomputed.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .partial_audit import (
    MODES,
    Report,
    _candidate_key,
    _finite,
    _path,
    _read,
    _sha,
    audit_modes,
    audit_overview_case,
)

VERSION = "independent-four-mode-score-replay-v1"
WEIGHT_KEY = "score_delay_prior_weight"


def _equal_number(actual, expected):
    return (
        _finite(actual)
        and _finite(expected)
        and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)
    )


def _quantile(pairs, fraction):
    rows = sorted((value, weight) for value, weight in pairs if weight > 0)
    threshold = sum(weight for _, weight in rows) * fraction
    accumulated = 0.0
    for value, weight in rows:
        accumulated += weight
        if accumulated >= threshold:
            return value
    return rows[-1][0] if rows else None


def score_terms(candidate, config):
    whole = candidate.get("whole_alignment", {}).get("score")
    apc = candidate.get("apc_support", {}).get("support")
    return {
        "local_windows": 0.5 * candidate["weighted_mean"]
        + 0.3 * candidate["weighted_median"]
        + 0.2 * candidate["weighted_p90"],
        "delay_mad": config.get("aggregation_delay_mad_weight", 0.2)
        * candidate["delay_mad_s"]
        / config.get("aggregation_delay_mad_scale_s", 120),
        "discarded_coverage": 2 * (1 - candidate["coverage"]),
        "absolute_delay": config.get(WEIGHT_KEY, 0.05)
        * abs(candidate["estimated_delay_s"])
        / 3600,
        "whole_alignment": config.get("aggregation_whole_failure_penalty", 1.0)
        if whole is None
        else config.get("aggregation_whole_alignment_weight", 0.2) * whole,
        "apc": 0.0
        if apc is None
        else config.get("aggregation_apc_weight", 0.15) * (1 - apc),
    }


def gate_values(candidate, config):
    minimum = config.get("aggregation_min_coverage", 0.35)
    score, support = candidate.get("score"), candidate.get("support_score")
    gates = {
        "absolute_cost_passed": _finite(score)
        and score <= config.get("aggregation_match_cost_max", 3.0),
        "projection_coverage_passed": _finite(candidate.get("coverage"))
        and candidate["coverage"] >= minimum,
        "window_coverage_passed": _finite(
            candidate.get("schedule_supported_window_weight_ratio")
        )
        and candidate["schedule_supported_window_weight_ratio"] >= minimum,
        "whole_evidence_passed": candidate.get("whole_alignment", {}).get("status")
        == "EVIDENCE",
        "whole_point_coverage_passed": _finite(
            candidate.get("schedule_aligned_point_ratio")
        )
        and candidate["schedule_aligned_point_ratio"] >= minimum,
        "whole_duration_coverage_passed": _finite(
            candidate.get("schedule_aligned_duration_ratio")
        )
        and candidate["schedule_aligned_duration_ratio"] >= minimum,
        "finite_support_score": _finite(support) and 0 <= support <= 1,
        "finite_aggregate_cost": _finite(score) and score >= 0,
    }
    gates["structural_evidence_passed"] = all(
        value for key, value in gates.items() if key != "absolute_cost_passed"
    )
    return gates


def audit_candidate(candidate, local, config, report, where):
    supported = [
        row for row in local if row.get("score") is not None and row["weight"] > 0
    ]
    eligible = [
        row
        for row in local
        if row.get("quality_eligible", row.get("score") is not None)
        and row["weight"] > 0
    ]
    report.check(bool(supported) and bool(eligible), "aggregate_without_windows", where)
    if not supported or not eligible:
        return
    pairs = [
        (
            row["score"]
            if row.get("score") is not None
            else config.get("aggregation_missing_window_penalty", 4.0),
            row["weight"],
        )
        for row in eligible
    ]
    weight = sum(value for _, value in pairs)
    delays = [(row["estimated_delay_s"], row["weight"]) for row in supported]
    delay = _quantile(delays, 0.5)
    expected = {
        "weighted_mean": sum(value * weight for value, weight in pairs) / weight,
        "weighted_median": _quantile(pairs, 0.5),
        "weighted_p90": _quantile(pairs, 0.9),
        "estimated_delay_s": delay,
        "delay_mad_s": _quantile([(abs(d - delay), w) for d, w in delays], 0.5),
        "schedule_supported_window_weight_ratio": sum(
            row["weight"] for row in supported
        )
        / weight,
    }
    for key, value in expected.items():
        report.check(
            _equal_number(candidate.get(key), value),
            "aggregate_statistic_wrong",
            f"{where}:{key}",
        )
    try:
        cost = sum(score_terms(candidate, config).values())
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        report.check(False, "invalid_aggregate_formula_inputs", where)
        return
    report.check(
        _equal_number(candidate.get("score"), cost), "aggregate_cost_wrong", where
    )
    report.check(
        _equal_number(candidate.get("support_score"), math.exp(-max(0, cost))),
        "aggregate_support_wrong",
        where,
    )
    report.check(
        candidate.get("probability") is None and candidate.get("calibrated") is False,
        "aggregate_falsely_calibrated",
        where,
    )
    report.examined["aggregate_candidates"] += 1


def audit_decision(result, config, report, where):
    aggregates = result.get("all_candidates", [])
    by_id = {_candidate_key(row): row for row in aggregates}
    report.check(len(by_id) == len(aggregates), "duplicate_aggregate_identity", where)
    ordered = sorted(aggregates, key=lambda row: (row["score"], _candidate_key(row)))
    report.check(aggregates == ordered, "aggregate_rank_wrong", where)
    competitors = [
        row for row in ordered if gate_values(row, config)["structural_evidence_passed"]
    ]
    near = (
        [
            row
            for row in competitors
            if competitors[0]["support_score"] - row["support_score"]
            <= config.get("aggregation_ambiguity_margin", 0.1)
        ]
        if competitors
        else []
    )
    shown = result.get("candidates", [])
    cap = min(3, max(0, int(config.get("aggregation_output_max", 3))))
    report.check(
        [_candidate_key(row) for row in shown]
        == [_candidate_key(row) for row in near[:cap]],
        "displayed_rank_or_competitors_wrong",
        where,
    )
    for row in shown:
        original = by_id.get(_candidate_key(row))
        report.check(
            original is not None
            and {k: v for k, v in row.items() if k != "decision_gate"} == original,
            "displayed_candidate_evidence_changed",
            where,
        )
        for key, expected in gate_values(row, config).items():
            report.check(
                row.get("decision_gate", {}).get(key) is expected,
                "displayed_gate_wrong",
                f"{where}:{_candidate_key(row)}:{key}",
            )
    margin = (
        competitors[0]["support_score"] - competitors[1]["support_score"]
        if len(competitors) > 1
        else None
    )
    report.check(
        result.get("top1_top2_margin") is None
        if margin is None
        else _equal_number(result.get("top1_top2_margin"), margin),
        "decision_margin_wrong",
        where,
    )
    expected_id = _candidate_key(competitors[0]) if shown else None
    for field in ("reported_candidate_id", "decision_candidate_id"):
        report.check(result.get(field) == expected_id, "decision_identity_stale", where)
    if result.get("status") == "MATCHED":
        report.check(
            len(near) == 1
            and all(gate_values(near[0], config).values())
            and near[0].get("continuous_support_windows", 0)
            >= config.get("aggregation_continuous_windows_min", 2),
            "matched_without_required_evidence",
            where,
        )
    for candidate in aggregates:
        identity = _candidate_key(candidate)
        audit_candidate(
            candidate,
            result.get("local_scores", {}).get(identity, []),
            config,
            report,
            f"{where}:{identity}",
        )


def audit_case(old, artifact, config, report):
    where = artifact["case_id"]
    ignored = {"config", "matching_modes", "result"}
    report.check(
        {k: v for k, v in old.items() if k not in ignored}
        == {k: v for k, v in artifact.items() if k not in ignored},
        "upstream_case_evidence_changed",
        where,
    )
    report.check(artifact.get("config") == config, "case_config_stale", where)
    report.check(
        artifact.get("result")
        == artifact.get("matching_modes", {}).get("global", {}).get("result"),
        "top_level_global_result_stale",
        where,
    )
    for mode in MODES:
        previous = old.get("matching_modes", {}).get(mode, {})
        current = artifact.get("matching_modes", {}).get(mode, {})
        location = f"{where}:{mode}"
        report.check(
            previous.get("selection") == current.get("selection"),
            "decision_scope_changed",
            location,
        )
        result = current.get("result", {})
        audit_decision(result, config, report, location)
        report.distributions[f"{mode}_status_transition"][
            f"{previous.get('result', {}).get('status')}->{result.get('status')}"
        ] += 1
        before = previous.get("result", {}).get("candidates", [])
        after = result.get("candidates", [])
        if (before[0].get("candidate_id") if before else None) != (
            after[0].get("candidate_id") if after else None
        ):
            report.examined[f"{mode}_top_candidate_changed"] += 1
    audit_modes(artifact, config, report, where)
    report.examined["cases"] += 1


def audit_catalog(record, artifact, report):
    where = artifact["case_id"]
    global_result = artifact["result"]
    for key, expected in (
        ("gtfs_status", global_result.get("status", "UNMATCHED")),
        ("gtfs_candidate_count", len(global_result.get("candidates", []))),
        ("pattern_confidence", global_result.get("pattern_confidence")),
        ("trip_confidence", global_result.get("trip_confidence")),
    ):
        report.check(
            record.get(key) == expected, "catalog_global_stale", f"{where}:{key}"
        )
    for mode, entry in artifact["matching_modes"].items():
        result, selection = entry["result"], entry["selection"]
        expected = {
            "status": result.get("status", "UNMATCHED"),
            "candidate_count": len(result.get("candidates", [])),
            "gtfs_status": result.get("status", "UNMATCHED"),
            "gtfs_candidate_count": len(result.get("candidates", [])),
            "pattern_confidence": result.get("pattern_confidence"),
            "trip_confidence": result.get("trip_confidence"),
            "selection_status": selection.get("status"),
            "effective_progress_m": selection.get("effective_progress_m"),
        }
        report.check(
            record.get("matching_modes", {}).get(mode) == expected,
            "catalog_mode_stale",
            f"{where}:{mode}",
        )


def audit_run(newpath):
    root = Path(newpath).resolve()
    report = Report()
    manifest = _read(root / "manifest.json")
    source = Path(manifest["partial_source_path"]).resolve()
    outputs = _read(root / "output_sha256.json")
    source_outputs = _read(source / "output_sha256.json")
    report.check(
        _read(root / "completion.json").get("validation_ok") is True,
        "run_not_complete",
        root.name,
    )
    report.check(
        manifest.get("matching_scoring_replay") is True,
        "not_scoring_replay",
        root.name,
    )
    provenance = manifest.get("stage_provenance", {})
    report.check(
        provenance.get("global_result_reused_unchanged") is False
        and provenance.get("projection_reused_unchanged") is True,
        "misleading_stage_provenance",
        root.name,
    )
    start_hashes = {}
    for folder, pins, kind in (
        (root, outputs, "new_output_hash"),
        (source, manifest["partial_source_sha256"], "source_pin_hash"),
    ):
        for relative, digest in pins.items():
            path = _path(folder, relative)
            actual = _sha(path) if path.is_file() else None
            report.check(actual == digest, kind, str(path))
            start_hashes[str(path)] = actual
            report.examined[kind] += 1
    for name, digest in manifest.get("partial_code_sha256", {}).items():
        path = Path(__file__).with_name(name + ".py")
        report.check(
            path.is_file() and _sha(path) == digest, "execution_code_changed", name
        )
    for relative, digest in source_outputs.items():
        if relative.startswith(("cases/", "catalog/", "overview/")) or relative in {
            "manifest.json",
            "config.json",
            "summary.json",
            "completion.json",
            "run.json",
            "output_sha256.json",
        }:
            continue
        report.check(
            outputs.get(relative) == digest, "copied_raw_or_gtfs_changed", relative
        )
        report.examined["unchanged_raw_or_gtfs_files"] += 1
    config_bundle = _read(root / "config.json")
    old_bundle = _read(source / "config.json")
    config, previous_config = config_bundle["gtfs"], old_bundle["gtfs"]
    report.check(
        manifest.get("scoring_overrides") == {WEIGHT_KEY: config.get(WEIGHT_KEY)},
        "scoring_override_provenance_wrong",
        root.name,
    )
    report.check(
        {k: v for k, v in config_bundle.items() if k != "gtfs"}
        == {k: v for k, v in old_bundle.items() if k != "gtfs"}
        and {k: v for k, v in config.items() if k != WEIGHT_KEY}
        == {k: v for k, v in previous_config.items() if k != WEIGHT_KEY},
        "unrelated_configuration_changed",
        root.name,
    )
    report.check(
        _finite(config.get(WEIGHT_KEY))
        and config[WEIGHT_KEY] > previous_config.get(WEIGHT_KEY, 0.05),
        "delay_weight_not_increased",
        root.name,
    )
    case_files = {name for name in outputs if name.startswith("cases/")}
    report.check(
        case_files == {name for name in source_outputs if name.startswith("cases/")},
        "case_set_changed",
        root.name,
    )
    canonical = _read(root / "gtfs/canonical.json")
    overviews = {mode: _read(root / f"overview/{mode}.json") for mode in MODES}
    overview_rows = {
        mode: {row["id"]: row for row in value.get("cases", [])}
        for mode, value in overviews.items()
    }
    catalogs = [
        row
        for name in outputs
        if name.startswith("catalog/")
        for row in _read(root / name)
    ]
    by_id = {row["id"]: row for row in catalogs}
    report.check(
        len(by_id) == len(catalogs) == len(case_files), "catalog_case_count", root.name
    )
    case_ids = set()
    for relative in sorted(case_files):
        old, artifact = _read(source / relative), _read(root / relative)
        case_id = artifact["case_id"]
        case_ids.add(case_id)
        audit_case(old, artifact, config, report)
        audit_catalog(by_id.get(case_id, {}), artifact, report)
        for mode in MODES:
            record = overview_rows[mode].get(case_id)
            report.check(
                record is not None, "overview_case_missing", f"{case_id}:{mode}"
            )
            if record is not None:
                audit_overview_case(record, artifact, canonical, mode, config, report)
    summary = _read(root / "summary.json")
    report.check(
        summary.get("status_counts") == dict(report.distributions["global_status"]),
        "summary_global_status_stale",
        root.name,
    )
    for mode in MODES:
        expected = dict(report.distributions[f"{mode}_status"])
        report.check(
            summary.get("matching_mode_status_counts", {}).get(mode) == expected,
            "summary_mode_status_stale",
            mode,
        )
        report.check(
            set(overview_rows[mode]) == case_ids
            and len(overviews[mode].get("cases", [])) == len(case_ids),
            "overview_case_identity_mismatch",
            mode,
        )
        report.check(
            overviews[mode].get("summary", {}).get("statuses") == expected,
            "overview_summary_status_stale",
            mode,
        )
        report.check(
            overviews[mode].get("summary", {}).get("case_count") == len(case_ids)
            and overviews[mode].get("schedule_time_basis")
            == "original_scheduled_wall_clock"
            and overviews[mode].get("matching_recomputed") is False,
            "overview_summary_or_time_policy_wrong",
            mode,
        )
    for path, digest in start_hashes.items():
        report.check(
            Path(path).is_file() and _sha(path) == digest,
            "file_changed_during_audit",
            path,
        )
    return report.output() | {
        "audit_version": VERSION,
        "audit_sha256": _sha(__file__),
        "run_id": root.name,
        "source_run_id": source.name,
        "source_delay_prior_weight": previous_config.get(WEIGHT_KEY, 0.05),
        "delay_prior_weight": config.get(WEIGHT_KEY),
        "scores_recomputed": True,
        "score_validation_scope": (
            "aggregate arithmetic and local weighted statistics only"
        ),
        "alignment_distances_recomputed": False,
        "raw_projection_selection_preservation_assessed": True,
        "global_result_preservation_expected": False,
        "scope": "Independent persisted invariants; no matching-accuracy claim",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit_run(args.run)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        destination = args.output.resolve()
        source = Path(
            _read(args.run / "manifest.json")["partial_source_path"]
        ).resolve()
        if destination.is_relative_to(args.run.resolve()) or destination.is_relative_to(
            source
        ):
            parser.error("Audit report must be outside completed runs")
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(payload + "\n")
    print(payload)
    raise SystemExit(0 if result["validation_ok"] else 1)


if __name__ == "__main__":
    main()
