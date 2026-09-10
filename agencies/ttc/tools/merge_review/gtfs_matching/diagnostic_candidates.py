"""Read-only presentation of scored hypotheses that failed recorded run gates.

This does not change the final decision or repeat projection/alignment. Only
already scored aggregates can be ranked; retrieved-only trips get no fake cost.
"""

from __future__ import annotations

import copy
import math
import statistics

from .decision import DEFAULTS, _gates

LIMIT = 3
DISPLAY_FIELDS = {
    "candidate_id",
    "trip_id",
    "feed_version",
    "service_date",
    "reference_id",
    "family_id",
    "pattern_id",
    "route_id",
    "route_short_name",
    "trip_headsign",
    "headsign",
    "score",
    "support_score",
    "weighted_mean",
    "weighted_median",
    "weighted_p90",
    "estimated_delay_s",
    "delay_mad_s",
    "coverage",
    "continuous_support_windows",
    "schedule_supported_window_weight_ratio",
    "schedule_supported_window_weight",
    "schedule_valid_window_weight",
    "schedule_unsupported_valid_window_count",
    "schedule_aligned_point_ratio",
    "schedule_aligned_duration_ratio",
    "valid_projection_point_ratio",
    "valid_projection_duration_ratio",
    "valid_projection_duration_s",
    "valid_projection_progress",
    "off_reference_ratio",
    "longest_valid_contiguous_run",
    "longest_valid_contiguous_run_points",
    "coverage_duration_definition",
    "scheduled_trajectory",
    "scorer",
    "score_components",
    "score_components_unweighted",
    "score_component_weights",
    "pattern_diagnostics",
    "coverage_metrics",
    "delay_uncertainty_s",
    "delay_uncertainty_calibrated",
    "delay_prior_source",
    "occurrence_model_enabled",
    "occurrence_displacement",
    "occurrence_decision_guard",
    "operational_context",
    "quality_eligible",
    "quality_gates",
    "probability",
    "calibrated",
}


def is_continuous(candidate):
    return candidate.get("scorer") == "continuous_passage_v1"


def continuous_gate_checks(candidate, config, result=None):
    """Recorded physical-evidence gates plus calibrated-set membership.

    The point ratios are displayed separately, not reinterpreted as legacy DTW
    coverage. Numerical passage-score acceptance alone cannot establish identity.
    """
    from .continuous.config import get_config

    c = get_config(config)
    quality = candidate.get("quality_gates", {})
    coverage = candidate.get("coverage_metrics", {})
    checks = []
    for metric, label, actual, threshold, operator, unit, passed in (
        (
            "score",
            "连续时空综合成本",
            candidate.get("score"),
            c["continuous_match_cost_max"],
            "<=",
            "score",
            finite(candidate.get("score"))
            and 0 <= candidate["score"] <= c["continuous_match_cost_max"],
        ),
        *(
            (
                f"coverage_metrics.{key}.duration_ratio",
                label,
                coverage.get(key, {}).get("duration_ratio"),
                c[threshold],
                ">=",
                "fraction",
                quality.get(gate) is True,
            )
            for key, label, threshold, gate in (
                (
                    "projection",
                    "投影覆盖率（相邻区间时长）",
                    "continuous_min_projection_ratio",
                    "projection_coverage",
                ),
                (
                    "schedule",
                    "计划对齐覆盖率（相邻区间时长）",
                    "continuous_min_schedule_ratio",
                    "schedule_coverage",
                ),
                (
                    "end_to_end",
                    "端到端覆盖率（相邻区间时长）",
                    "continuous_min_end_to_end_ratio",
                    "end_to_end_coverage",
                ),
            )
        ),
        (
            "quality_gates.adequate_observations",
            "有效点数、持续时间和进度证据",
            quality.get("adequate_observations"),
            True,
            "==",
            "status",
            quality.get("adequate_observations") is True,
        ),
    ):
        checks.append(
            dict(
                metric=metric,
                label=label,
                actual=actual,
                threshold=threshold,
                operator=operator,
                unit=unit,
                passed=bool(passed),
            )
        )
    calibration = (result or {}).get("calibration", {})
    credible = set(calibration.get("credible_set", []))
    probability = candidate.get("probability")
    verified_set = (
        calibration.get("calibrated") is True
        and candidate.get("calibrated") is True
        and finite(probability)
        and 0 <= probability <= 1
        and identity(candidate) in credible
    )
    checks.append(
        dict(
            metric="calibrated_credible_set",
            label="独立校准的班次可信集",
            actual=verified_set,
            threshold=True,
            operator="==",
            unit="status",
            passed=verified_set,
        )
    )
    return checks


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def identity(row):
    if row.get("candidate_id") is not None:
        return str(row["candidate_id"])
    keys = ("feed_version", "service_date", "trip_id")
    if all(row.get(key) is not None for key in keys):
        return "|".join(str(row[key]) for key in keys)
    return None


def scheduled_knots(candidate):
    """Same wall-clock and canonical-axis aliases as final-candidate charts."""
    knots = []
    for original in candidate.get("scheduled_trajectory", []):
        row = copy.deepcopy(original)
        s = row.get("s_m", row.get("s"))
        if "arrival_t" in row or "departure_t" in row:
            for kind, key in (("arrival", "arrival_t"), ("departure", "departure_t")):
                if row.get(key) is not None:
                    knots.append({**row, "t": row[key], "s": s, "kind": kind})
        elif row.get("t") is not None:
            knots.append({**row, "s": s})
    return sorted(knots, key=lambda row: (row["t"], row.get("stop_sequence", 0)))


def gate_checks(candidate, config, result=None):
    if is_continuous(candidate):
        return continuous_gate_checks(candidate, config, result)
    gates = _gates(candidate, config)
    coverage = config["aggregation_min_coverage"]
    specifications = (
        (
            "score",
            "综合成本",
            candidate.get("score"),
            config["aggregation_match_cost_max"],
            "<=",
            "score",
            "absolute_cost_passed",
        ),
        (
            "coverage",
            "有效投影点比例",
            candidate.get("coverage"),
            coverage,
            ">=",
            "fraction",
            "projection_coverage_passed",
        ),
        (
            "schedule_supported_window_weight_ratio",
            "窗口支持权重比例",
            candidate.get("schedule_supported_window_weight_ratio"),
            coverage,
            ">=",
            "fraction",
            "window_coverage_passed",
        ),
        (
            "whole_alignment.status",
            "整段对齐状态",
            candidate.get("whole_alignment", {}).get("status"),
            "EVIDENCE",
            "==",
            "status",
            "whole_evidence_passed",
        ),
        (
            "schedule_aligned_point_ratio",
            "整段对齐点覆盖率",
            candidate.get("schedule_aligned_point_ratio"),
            coverage,
            ">=",
            "fraction",
            "whole_point_coverage_passed",
        ),
        (
            "schedule_aligned_duration_ratio",
            "整段对齐时长覆盖率",
            candidate.get("schedule_aligned_duration_ratio"),
            coverage,
            ">=",
            "fraction",
            "whole_duration_coverage_passed",
        ),
        (
            "support_score",
            "支持分须为 0–1 的有限数值",
            candidate.get("support_score"),
            [0, 1],
            "in",
            "score",
            "finite_support_score",
        ),
        (
            "finite_aggregate_cost",
            "成本须为非负有限数值",
            gates["finite_aggregate_cost"],
            True,
            "==",
            "status",
            "finite_aggregate_cost",
        ),
    )
    return [
        dict(
            metric=metric,
            label=label,
            actual=actual,
            threshold=threshold,
            operator=operator,
            unit=unit,
            passed=bool(gates[key]),
        )
        for metric, label, actual, threshold, operator, unit, key in specifications
    ]


def build_diagnostic_candidates(result, config=None):
    """Separate up to three failed hypotheses without mutating final candidates."""
    c = DEFAULTS | (config or {})
    displayed = {identity(row) for row in result.get("candidates", [])}
    ranked, seen = [], set()
    total_failed = already_displayed = unranked = 0
    for original in result.get("all_candidates", []):
        candidate_id = identity(original)
        if candidate_id is None or candidate_id in seen:
            continue
        seen.add(candidate_id)
        checks = gate_checks(original, c, result)
        failed = [check for check in checks if not check["passed"]]
        if not failed:
            continue  # A display cap or ambiguity is not itself a failed gate.
        total_failed += 1
        if candidate_id in displayed:
            already_displayed += 1
            continue
        score = original.get("score")
        if not finite(score) or score < 0:
            unranked += 1
            continue
        ranked.append((score, candidate_id, original, checks, failed))
    ranked.sort(key=lambda item: (item[0], item[1]))
    candidates = []
    for rank, (_, candidate_id, original, checks, failed) in enumerate(
        ranked[:LIMIT], 1
    ):
        row = {
            key: copy.deepcopy(value)
            for key, value in original.items()
            if key in DISPLAY_FIELDS
        }
        # Large source-by-source alignment traces remain in the verified log.
        row["whole_alignment"] = {
            key: copy.deepcopy(value)
            for key, value in original.get("whole_alignment", {}).items()
            if key
            in {
                "status",
                "score",
                "reasons",
                "schedule_aligned_point_ratio",
                "schedule_aligned_duration_ratio",
                "coverage_metrics",
                "continuous_passage_evaluated",
                "dtw_used",
            }
        }
        row["apc_support"] = {
            key: copy.deepcopy(value)
            for key, value in original.get("apc_support", {}).items()
            if key in {"count", "support", "supported_stops"}
        }
        row.update(
            candidate_id=candidate_id,
            diagnostic_only=True,
            diagnostic_rank=rank,
            probability=original.get("probability")
            if is_continuous(original)
            else None,
            calibrated=original.get("calibrated") is True
            if is_continuous(original)
            else False,
            gate_checks=checks,
            failed_gate_checks=failed,
            trajectory=scheduled_knots(original),
            diagnostic_ranking_basis="stored_aggregate_cost_ascending",
        )
        if is_continuous(original):
            # Keep the compact fitted-prior/dynamics summary, not thousands of
            # per-observation state rows, in diagnostic candidate cards.
            row["delay_model_diagnostics"] = {
                key: copy.deepcopy(value)
                for key, value in original.get("delay_fit", {}).items()
                if key
                not in {"rows", "excluded", "states", "atomic_states", "dynamics_rows"}
            }
            headways = [
                point["headway_s"]
                for point in original.get("whole_alignment", {}).get(
                    "delay_process", []
                )
                if finite(point.get("headway_s")) and point["headway_s"] > 0
            ]
            row["headway_summary_s"] = (
                {
                    "min": min(headways),
                    "median": statistics.median(headways),
                    "max": max(headways),
                }
                if headways
                else None
            )
        candidates.append(row)
    return {
        "candidates": candidates,
        "summary": {
            "limit": LIMIT,
            "total_failed": total_failed,
            "already_displayed_failed": already_displayed,
            "remaining_failed": len(ranked),
            "unranked_failed": unranked,
            "shown_count": len(candidates),
            "ranking": "stored_aggregate_cost_ascending",
            "matching_recomputed": False,
            "formal_decision_changed": False,
            "gate_source": "saved_run_configuration",
            "note": (
                "Only scored gate-failed hypotheses not already displayed as "
                "final candidates; no synthetic score for retrieved-only trips."
            ),
        },
    }
