"""Whole-candidate reevaluation and explicitly uncalibrated decision reporting."""

from __future__ import annotations

import math
import statistics

from .calibration import predict_probability
from .retrieval import for_reference, passage_times, retrieve_candidates, trip_key
from .scoring import (
    coverage_metrics,
    evaluate_candidate,
    qualified_quality,
    score_whole_with_shared_delay,
)
from .windows import build_windows, observation_key, usable

DEFAULTS = {
    "aggregation_match_cost_max": 3.0,
    "aggregation_min_coverage": 0.35,
    "aggregation_ambiguity_margin": 0.1,
    "aggregation_output_max": 3,
    "aggregation_continuous_windows_min": 2,
    "aggregation_support_cost_max": 2.0,
    "aggregation_delay_mad_scale_s": 120,
    "aggregation_delay_mad_weight": 0.2,
    "aggregation_change_penalty": 0.3,
    "aggregation_change_gain_min": 0.25,
    "aggregation_change_min_windows": 2,
    "aggregation_apc_weight": 0.15,
    "aggregation_whole_alignment_weight": 0.2,
    "aggregation_whole_failure_penalty": 1.0,
    "aggregation_missing_window_penalty": 4.0,
    "aggregation_change_min_coverage": 0.6,
}


def get_config(config=None):
    return DEFAULTS | (config or {})


def weighted_quantile(values, fraction):
    values = sorted((value, weight) for value, weight in values if weight > 0)
    if not values:
        return None
    threshold = sum(weight for _, weight in values) * fraction
    total = 0.0
    for value, weight in values:
        total += weight
        if total >= threshold:
            return value
    return values[-1][0]


def _apc_support(apc, trip, delay):
    result = []
    ref = trip.get("reference_id")
    candidates = for_reference(apc, ref)
    known = {observation_key(row) for row in candidates}
    for original in apc:
        if observation_key(original) in known:
            continue
        if original.get("projection_status") != "STOP_INTERVAL_ONLY":
            continue
        stops = [
            s
            for s in original.get("canonical_stop_intervals", [])
            if s.get("reference_id") == ref
            and not s.get("ambiguous")
            and s.get("s_min_m") is not None
            and s.get("s_max_m") is not None
        ]
        if len(stops) == 1:
            candidates.append(
                dict(
                    original,
                    reference_id=ref,
                    s_m=(stops[0]["s_min_m"] + stops[0]["s_max_m"]) / 2,
                    projection_status="STOP_INTERVAL_ONLY",
                )
            )
    for row in candidates:
        if (
            row.get("projection_status") not in {"ON_REFERENCE", "STOP_INTERVAL_ONLY"}
            or row.get("s_m") is None
        ):
            continue
        left = row.get("interval_start", row.get("arrival", row.get("t")))
        right = row.get("interval_end", row.get("departure", left))
        if left is None and right is None:
            continue
        left = right if left is None else left
        right = left if right is None or right < left else right
        times = passage_times(trip, row["s_m"])
        if not times:
            continue
        distance = min(max(left - (t + delay), 0, (t + delay) - right) for t in times)
        result.append(
            {
                "source_rid": row.get("source_rid", row.get("rid")),
                "interval_start": left,
                "interval_end": right,
                "distance_s": distance,
                "support": math.exp(-distance / 180),
                "not_a_gps_point": True,
            }
        )
    return {
        "count": len(result),
        "support": sum(r["support"] for r in result) / len(result) if result else None,
        "rows": result,
    }


def aggregate_candidate(local, observations, apc, trip, config=None):
    c = get_config(config)
    supported = [w for w in local if w.get("score") is not None and w["weight"] > 0]
    if not supported:
        return None
    valid_windows = [
        w
        for w in local
        if w.get("quality_eligible", w.get("score") is not None) and w["weight"] > 0
    ]
    total_valid_weight = sum(w["weight"] for w in valid_windows)
    supported_weight = sum(w["weight"] for w in supported)
    schedule_window_coverage = (
        supported_weight / total_valid_weight if total_valid_weight else 0
    )
    pairs = [
        (
            w["score"]
            if w.get("score") is not None
            else c["aggregation_missing_window_penalty"],
            w["weight"],
        )
        for w in valid_windows
    ]
    weight = sum(w for _, w in pairs)
    mean = sum(value * w for value, w in pairs) / weight
    median, p90 = weighted_quantile(pairs, 0.5), weighted_quantile(pairs, 0.9)
    delays = [(w["estimated_delay_s"], w["weight"]) for w in supported]
    delay = weighted_quantile(delays, 0.5)
    mad = weighted_quantile([(abs(value - delay), w) for value, w in delays], 0.5)
    trace = for_reference(
        observations, trip.get("reference_id"), include_unprojectable=True
    )
    whole = score_whole_with_shared_delay(trace, trip, local, c)
    raw_count = sum(r.get("source", "avl") == "avl" for r in observations)
    good_count = sum(usable(r, c) for r in trace)
    coverage = good_count / raw_count if raw_count else 0
    atomic = sorted(
        (w for w in local if w["kind"] == "atomic"), key=lambda w: w["start"]
    )
    longest = current = 0
    previous_end = None
    for row in atomic:
        if (
            row.get("score") is not None
            and row["score"] <= c["aggregation_support_cost_max"]
        ):
            current = current + 1 if previous_end == row["start"] else 1
            longest = max(longest, current)
            previous_end = row["end"]
        else:
            current, previous_end = 0, None
    apc_result = _apc_support(apc, trip, delay)
    cost = 0.5 * mean + 0.3 * median + 0.2 * p90
    cost += c["aggregation_delay_mad_weight"] * mad / c["aggregation_delay_mad_scale_s"]
    cost += (1 - coverage) * 2
    # A weak explicitly uncalibrated delay prior prevents equivalent shape-only
    # fits from silently favouring a previous-day trip by lexical ID ordering.
    cost += c.get("score_delay_prior_weight", 0.05) * abs(delay) / 3600
    if whole.get("score") is None:
        cost += c["aggregation_whole_failure_penalty"]
    else:
        cost += c["aggregation_whole_alignment_weight"] * whole["score"]
    if apc_result["support"] is not None:
        cost += c["aggregation_apc_weight"] * (1 - apc_result["support"])
    return {
        "candidate_id": trip_key(trip),
        "trip_id": trip["trip_id"],
        "service_date": trip.get("service_date"),
        "feed_version": trip.get("feed_version"),
        "reference_id": trip.get("reference_id"),
        "family_id": trip.get("family_id"),
        "pattern_id": trip.get("pattern_id"),
        "scheduled_trajectory": trip.get("scheduled", []),
        "score": cost,
        "support_score": math.exp(-max(0, cost)),
        "weighted_mean": mean,
        "weighted_median": median,
        "weighted_p90": p90,
        "estimated_delay_s": delay,
        "delay_mad_s": mad,
        "coverage": coverage,
        "schedule_supported_window_weight_ratio": schedule_window_coverage,
        "schedule_supported_window_weight": supported_weight,
        "schedule_valid_window_weight": total_valid_weight,
        "schedule_unsupported_valid_window_count": sum(
            w.get("score") is None for w in valid_windows
        ),
        "unreachable_delay_window_count": sum(
            w.get("delay_path", {}).get("status") == "UNREACHABLE_MONOTONE_TRANSITION"
            for w in local
        ),
        "schedule_coverage_penalty_definition": (
            "Every quality-eligible window contributes normalized source weight; "
            "unsuccessful alignment receives aggregation_missing_window_penalty "
            "in mean/median/p90 instead of disappearing."
        ),
        "schedule_aligned_point_ratio": whole.get("schedule_aligned_point_ratio", 0),
        "schedule_aligned_duration_ratio": whole.get(
            "schedule_aligned_duration_ratio", 0
        ),
        "continuous_support_windows": longest,
        "apc_support": apc_result,
        "whole_alignment": whole,
        "probability": None,
        "calibrated": False,
        **coverage_metrics(trace, c),
    }


def compare_boundary_models(local_scores, config=None):
    c = get_config(config)
    atomic = {}
    for candidate, windows in local_scores.items():
        for w in windows:
            if w["kind"] == "atomic" and w.get("score") is not None:
                atomic.setdefault((w["start"], w["end"]), {})[candidate] = w
    keys = sorted(atomic)
    candidates = sorted(local_scores)

    def cost(candidate, subset):
        available = [
            atomic[key][candidate] for key in subset if candidate in atomic[key]
        ]
        if len(available) < c["aggregation_change_min_windows"]:
            return math.inf
        total = sum(max(w["weight"] for w in atomic[key].values()) for key in subset)
        supported = sum(w["weight"] for w in available)
        if total <= 0 or supported / total < c["aggregation_change_min_coverage"]:
            return math.inf
        values = available
        if not total:
            return math.inf
        delays = [w.get("estimated_delay_s", 0) for w in values]
        center = statistics.median(delays)
        mad = statistics.median(abs(value - center) for value in delays)
        return (
            (
                sum(w["score"] * w["weight"] for w in values)
                + c["aggregation_missing_window_penalty"] * (total - supported)
            )
            / total
            + c["aggregation_delay_mad_weight"]
            * mad
            / c["aggregation_delay_mad_scale_s"]
            + c.get("score_delay_prior_weight", 0.05) * abs(center) / 3600
        )

    minimum = int(c["aggregation_change_min_windows"])
    if len(keys) < 2 * minimum:
        return {
            "status": "NO_EVIDENCE",
            "reason": "INSUFFICIENT_INDEPENDENT_ATOMIC_WINDOWS",
            "boundary_uncertain": False,
        }
    single = min(
        ((cost(candidate, keys), candidate) for candidate in candidates),
        default=(math.inf, None),
    )
    best = (math.inf, None, None, None)
    for index in range(minimum, len(keys) - minimum + 1):
        left = min(
            (cost(candidate, keys[:index]), candidate) for candidate in candidates
        )
        right = min(
            (cost(candidate, keys[index:]), candidate) for candidate in candidates
        )
        if left[1] == right[1]:
            continue
        value = (left[0] * index + right[0] * (len(keys) - index)) / len(keys) + c[
            "aggregation_change_penalty"
        ]
        if value < best[0]:
            best = value, keys[index][0], left[1], right[1]
    gain = (
        single[0] - best[0]
        if math.isfinite(single[0]) and math.isfinite(best[0])
        else None
    )
    return {
        "status": "COMPARED"
        if math.isfinite(single[0]) or math.isfinite(best[0])
        else "NO_EVIDENCE",
        "reason": None
        if math.isfinite(single[0]) or math.isfinite(best[0])
        else "INSUFFICIENT_COMMON_ATOMIC_COVERAGE",
        "single_cost": single[0] if math.isfinite(single[0]) else None,
        "single_candidate_id": single[1],
        "two_part_cost": best[0] if math.isfinite(best[0]) else None,
        "changepoint_t": best[1],
        "left_candidate_id": best[2],
        "right_candidate_id": best[3],
        "improvement": gain,
        "boundary_uncertain": (
            gain is not None and gain > c["aggregation_change_gain_min"]
        )
        or (not math.isfinite(single[0]) and math.isfinite(best[0])),
        "verified_trip_boundary": False,
    }


def match_candidate_segment(
    projection,
    apc_evidence,
    references,
    trips,
    config=None,
    *,
    service_date=None,
    route_id=None,
    calibrator=None,
):
    if (config or {}).get("continuous_matching_enabled", False):
        from .continuous.integration import match_candidate_segment as continuous_match

        return continuous_match(
            projection,
            apc_evidence,
            references,
            trips,
            config,
            service_date=service_date,
            route_id=route_id,
            calibrator=calibrator,
        )
    c = get_config(config)
    points = (
        projection.get("points", []) if isinstance(projection, dict) else projection
    )
    observations = [
        dict(
            row, t=row.get("t", row.get("event_time")), s_m=row.get("s_m", row.get("s"))
        )
        for row in points
    ]
    apc = list(apc_evidence or [])
    if isinstance(apc_evidence, dict):
        apc = apc_evidence.get("points", apc_evidence.get("observations", []))
    apc_by_id = {
        observation_key(row): row for row in observations if row.get("source") == "apc"
    }
    apc_by_id.update({observation_key(row): row for row in apc})
    apc = list(apc_by_id.values())
    known_ids = {observation_key(row) for row in observations}
    observations.extend(
        dict(
            row,
            source="apc",
            t=row.get("t", row.get("arrival", row.get("interval_start"))),
            s_m=row.get("s_m", row.get("s")),
        )
        for row in apc
        if observation_key(row) not in known_ids
    )
    if c.get("window_adaptive_enabled"):
        radius = c.get("window_event_radius_s", 90)
        events = {
            row["t"]
            for row in observations
            if row.get("t") is not None
            and (
                row.get("scope_separator_before")
                or row.get("source") == "apc"
                or row.get("projection_status") == "OFF_REFERENCE"
            )
        }
        c["window_event_windows"] = list(c.get("window_event_windows", [])) + [
            {
                "start": t - radius,
                "end": t + radius,
                "trigger": "APC_STOP_OR_SCOPE_OR_OFF_REFERENCE",
            }
            for t in sorted(events)
        ]
    windows = build_windows(observations, c)
    by_id = {observation_key(row): row for row in observations}
    refs = sorted({t.get("reference_id") for t in trips if t.get("reference_id")})
    for window in windows["windows"]:
        rows = [by_id[key] for key in window["observation_ids"]]
        window.update(coverage_metrics(rows, c, window=window))
        window["quality_by_reference"] = {
            ref: qualified_quality(
                for_reference(rows, ref, include_unprojectable=True), c, window=window
            )
            for ref in refs
        }
        if window["quality_by_reference"]:
            reference = max(
                window["quality_by_reference"],
                key=lambda ref: (
                    window["quality_by_reference"][ref]["status"] == "EVIDENCE",
                    window["quality_by_reference"][ref]["valid_projection_point_ratio"],
                    window["quality_by_reference"][ref][
                        "valid_projection_duration_ratio"
                    ],
                ),
            )
            window["primary_coverage"] = {
                key: window[key] for key in coverage_metrics(rows, c, window=window)
            }
            window["quality_reference_id"] = reference
            window["quality"] = dict(window["quality_by_reference"][reference])
            window.update(
                {
                    key: value
                    for key, value in window["quality"].items()
                    if key.startswith("valid_projection_")
                    or key in {"off_reference_ratio", "longest_valid_contiguous_run"}
                }
            )
    retrieval = retrieve_candidates(
        windows, observations, trips, c, service_date=service_date, route_id=route_id
    )
    local, aggregates = {}, []
    for trip in retrieval["trips"]:
        key = trip_key(trip)
        local[key] = evaluate_candidate(observations, windows, trip, c)
        summary = aggregate_candidate(local[key], observations, apc, trip, c)
        if summary is not None:
            aggregates.append(summary)
    aggregates.sort(key=lambda row: (row["score"], row["candidate_id"]))
    for index, row in enumerate(aggregates):
        row["margin"] = (
            row["support_score"] - aggregates[index + 1]["support_score"]
            if index + 1 < len(aggregates)
            else None
        )
        row["probability"] = predict_probability(
            {**row, "margin": row["margin"] or 0}, calibrator
        )
        row["calibrated"] = row["probability"] is not None
    boundary = compare_boundary_models(local, c)
    final, status = [], "UNMATCHED"
    eligible = [
        r
        for r in aggregates
        if r["score"] <= c["aggregation_match_cost_max"]
        and r["coverage"] >= c["aggregation_min_coverage"]
        and r["schedule_supported_window_weight_ratio"] >= c["aggregation_min_coverage"]
    ]
    if eligible:
        best = eligible[0]
        alternatives = [
            r
            for r in eligible
            if best["support_score"] - r["support_score"]
            <= c["aggregation_ambiguity_margin"]
        ]
        final = alternatives[: min(3, int(c["aggregation_output_max"]))]
        status = (
            "MATCHED"
            if len(alternatives) == 1
            and best["whole_alignment"]["status"] == "EVIDENCE"
            and best["schedule_aligned_point_ratio"] >= c["aggregation_min_coverage"]
            and best["schedule_aligned_duration_ratio"] >= c["aggregation_min_coverage"]
            and best["continuous_support_windows"]
            >= c["aggregation_continuous_windows_min"]
            else "AMBIGUOUS"
        )
        if (
            len(alternatives) > 3
            and best["pattern_id"] is not None
            and len({r["pattern_id"] for r in alternatives}) == 1
        ):
            status = "PATTERN_ONLY"
        if boundary["boundary_uncertain"]:
            status = "BOUNDARY_UNCERTAIN"
    elif any(
        r.get("reference_id") and r.get("projection_status") == "ON_REFERENCE"
        for r in observations
    ) or any(r.get("projection_status") == "STOP_INTERVAL_ONLY" for r in apc):
        status = "PATTERN_ONLY"
    if boundary["boundary_uncertain"] and aggregates:
        status = "BOUNDARY_UNCERTAIN"
    diagnostic_best = aggregates[0] if aggregates else None
    best = final[0] if final else diagnostic_best
    comparison_set = eligible if final else aggregates
    comparison_others = (
        [row for row in comparison_set if row["candidate_id"] != best["candidate_id"]]
        if best
        else []
    )
    margin = (
        best["support_score"] - comparison_others[0]["support_score"]
        if best and comparison_others
        else None
    )
    reasons = []
    if not aggregates:
        reasons.append("NO_EVIDENCE_AFTER_REFERENCE_SPECIFIC_QUALITY_THRESHOLDS")
    if aggregates and not eligible:
        reasons.append("COST_OR_COVERAGE_THRESHOLD_NOT_MET")
    if aggregates and (
        best["whole_alignment"]["status"] != "EVIDENCE"
        or best["schedule_aligned_point_ratio"] < c["aggregation_min_coverage"]
        or best["schedule_aligned_duration_ratio"] < c["aggregation_min_coverage"]
    ):
        reasons.append("WHOLE_ALIGNMENT_OR_SCHEDULE_COVERAGE_NOT_VALIDATED")
    if status in {"AMBIGUOUS", "PATTERN_ONLY"}:
        reasons.append("TRIP_IDENTITY_NOT_SEPARATED_BY_AVAILABLE_EVIDENCE")
    if boundary["boundary_uncertain"]:
        reasons.append("ONE_CHANGEPOINT_MODEL_OUTPERFORMS_SINGLE_CANDIDATE")
    whole_quality = coverage_metrics(
        for_reference(observations, best["reference_id"], include_unprojectable=True)
        if best
        else observations,
        c,
    )
    return {
        "status": status,
        "evidence_status": "EVIDENCE" if aggregates else "NO_EVIDENCE",
        "candidates": final,
        "decision_candidate_id": best["candidate_id"] if final else None,
        "reported_candidate_id": best["candidate_id"] if best else None,
        "global_diagnostic_best": {
            key: diagnostic_best[key]
            for key in [
                "candidate_id",
                "score",
                "support_score",
                "reference_id",
                "estimated_delay_s",
            ]
        }
        if diagnostic_best
        else None,
        "pattern_confidence": {
            "support_score": best["support_score"] if best else None,
            "calibrated_probability": None,
            "definition": "Heuristic schedule-pattern support, not probability",
        },
        "trip_confidence": {
            "support_score": best["support_score"] if best else None,
            "calibrated_probability": best["probability"] if best else None,
            "identity_separated": status == "MATCHED",
            "definition": "Heuristic support; ambiguous timing cannot "
            "establish trip identity",
        },
        "top1_top2_margin": margin,
        "top1_top2_margin_definition": (
            "Support-score difference between the decision/displayed best and "
            "the next eligible candidate (before display cap); if no final "
            "candidates, diagnostic aggregate ranking only."
        ),
        "top1_top2_margin_candidate_ids": [
            best["candidate_id"],
            comparison_others[0]["candidate_id"],
        ]
        if best and comparison_others
        else [],
        "estimated_delay": best["estimated_delay_s"] if best else None,
        "evidence_coverage": whole_quality,
        "supporting_window_count": sum(
            w.get("score") is not None for w in local.get(best["candidate_id"], [])
        )
        if best
        else 0,
        "reasons": reasons,
        "all_candidates": aggregates,
        "windows": windows,
        "retrieval": retrieval,
        "local_scores": local,
        "whole_trip": {
            "candidate_segment_not_verified_trip": True,
            "boundary_models": boundary,
            "evaluated_candidate_count": len(aggregates),
            **whole_quality,
        },
        "calibration": {
            "calibrated": bool(calibrator and calibrator.get("calibrated")),
            "status": calibrator.get("status")
            if calibrator
            else "UNCALIBRATED_NO_VERIFIED_LABELS",
            "probability": None,
        },
        "diagnostics": {
            "source_observations": len(observations),
            "off_reference_excluded": sum(
                r.get("projection_status") == "OFF_REFERENCE" for r in observations
            ),
            "candidate_union_truncated": False,
            "final_display_cap": 3,
            "apc_used_as_gps": False,
            "vehicle_day_alignment": "not used; candidate-local soft delay model only",
            "metric_definition": "DTW uses Huber progress error with time band "
            "and local warp slopes. Partial Frechet and directed-p95 Hausdorff "
            "use (progress metres, delay-corrected seconds * score_time_metric_mps), "
            "separately per fragment; these are not full-route distances or "
            "calibrated probabilities.",
        },
    }
