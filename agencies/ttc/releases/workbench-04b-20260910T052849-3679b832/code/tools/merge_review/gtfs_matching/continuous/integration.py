"""Continuous occurrence scoring. No sampled GTFS DTW is called here."""

import bisect
import collections
import math
import time

from ..retrieval import for_reference, trip_key
from ..windows import observation_key
from . import SCORER
from .apc import normalize_apc
from .config import get_config
from .delay import fit_delay, huber, weighted_quantile
from .evidence import coverage_metrics, temporal_weights, valid_projection
from .retrieval import retrieve, schedule_pool
from .schedule import passage_at, residual_at
from .windows import construct_windows

COMPONENT_WEIGHTS = {
    "absolute_delay_level_cost": "continuous_absolute_delay_weight",
    "conditioned_fit_cost": "continuous_conditioned_fit_weight",
    "delay_dynamics_cost": "continuous_delay_dynamics_weight",
    "APC_stop_time_cost": "continuous_apc_weight",
    "projection_and_coverage_cost": "continuous_projection_coverage_weight",
    "pattern_progress_cost": "continuous_pattern_progress_weight",
    "vehicle_sequence_cost": "continuous_vehicle_sequence_weight",
}


def _mean(values):
    pairs = [(v, w) for v, w in values if w > 0 and math.isfinite(v)]
    return sum(v * w for v, w in pairs) / sum(w for _, w in pairs) if pairs else 0.0


def _quantile(pairs, fraction):
    return weighted_quantile([v for v, _ in pairs], [w for _, w in pairs], fraction)


def _trimmed_mean(pairs, fraction):
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    total = sum(w for _, w in pairs)
    lower, upper, position = total * fraction, total * (1 - fraction), 0.0
    retained = []
    for value, weight in pairs:
        overlap = max(0.0, min(position + weight, upper) - max(position, lower))
        retained.append((value, overlap))
        position += weight
    return _mean(retained)


def _observed_progress(rows, config):
    """Add within-fragment progress, never subtract unrelated canonical axes."""
    groups = collections.defaultdict(list)
    for row in rows:
        if valid_projection(row, config):
            groups[row.get("reference_id"), row.get("fragment_id")].append(row)
    progress = 0.0
    for group in groups.values():
        ordered = sorted(group, key=lambda r: r["t"])
        for a, b in zip(ordered, ordered[1:], strict=False):
            if 0 < b["t"] - a["t"] <= config[
                "continuous_coverage_max_gap_s"
            ] and not b.get("scope_separator_before"):
                progress += max(0.0, b["s_m"] - a["s_m"])
    return progress


def _delay_at(process, timestamp, config):
    if not process:
        return None
    i = bisect.bisect_left([p["t"] for p in process], timestamp)
    if i < len(process) and process[i]["t"] == timestamp:
        return process[i]["delay_s"]
    if i == 0 or i == len(process):
        row = process[0 if i == 0 else -1]
        return (
            row["delay_s"]
            if abs(timestamp - row["t"]) <= config["continuous_coverage_max_gap_s"]
            else None
        )
    a, b = process[i - 1], process[i]
    if (
        a["fragment_id"] != b["fragment_id"]
        or b["t"] - a["t"] > config["continuous_coverage_max_gap_s"]
    ):
        return None
    if b["t"] == a["t"]:
        return b["delay_s"]
    f = (timestamp - a["t"]) / (b["t"] - a["t"])
    return a["delay_s"] * (1 - f) + b["delay_s"] * f


def _apc_evidence(apc, schedule, process, reference_id, config):
    rows, fit, missing = [], [], []
    for row in normalize_apc(apc, reference_id):
        if row["status"] != "EVIDENCE":
            rows.append(row)
            continue
        confidence = row["confidence"]
        passage = passage_at(schedule, row["canonical_stop_s"], config)
        # Traversing a stop's location is not evidence that this trip serves it.
        served = (
            passage.get("kind") == "dwell"
            and passage["status"] == "SUPPORTED"
            and row.get("stop_id") is not None
            and str(row["stop_id"]) in {str(s) for s in passage.get("stop_ids", [])}
        )
        missing.append((float(not served), confidence))
        if not served:
            rows.append(
                dict(
                    row,
                    status="NO_SCHEDULE_STOP",
                    reasons=["APC_STOP_NOT_SERVED_BY_PATTERN"],
                )
            )
            continue
        t = (row["interval_start"] + row["interval_end"]) / 2
        delay = _delay_at(process, t, config)
        if delay is None:
            rows.append(
                dict(
                    row,
                    status="NO_DELAY_SUPPORT",
                    reasons=["APC_NOT_BRIDGED_ACROSS_GPS_FRAGMENT"],
                )
            )
            continue
        lo, hi = passage["intervals"][0]
        distance = max(
            row["interval_start"] - (hi + delay),
            (lo + delay) - row["interval_end"],
            0.0,
        )
        raw_distance = max(row["interval_start"] - hi, lo - row["interval_end"], 0.0)
        cost = huber(distance / config["continuous_apc_time_scale_s"])
        fit.append((cost, confidence))
        rows.append(
            dict(
                row,
                shared_delay_s=delay,
                raw_schedule_interval=[lo, hi],
                raw_interval_distance_s=raw_distance,
                shifted_interval_distance_s=distance,
                cost=cost,
                support=math.exp(-min(cost, 700)),
                reasons=[],
            )
        )
    discriminative = max((v * w for v, w in missing), default=0.0)
    return {
        "rows": rows,
        "count": len(fit),
        "supported_stops": len(
            {
                r.get("canonical_stop_s")
                for r in rows
                if r.get("cost") is not None
                and r["cost"] <= config["continuous_apc_support_cost_max"]
            }
        ),
        "support": math.exp(-min(_mean(fit), 700)) if fit else None,
        "cost": _mean(fit),
        "discriminative_stop_mismatch": discriminative,
        "pattern_stop_cost": config["continuous_pattern_stop_penalty"] * _mean(missing)
        + config["continuous_pattern_discriminative_stop_weight"] * discriminative,
        "schedule_derived_features_used": False,
    }


def _point_records(trace, pool, trip, config):
    schedule = pool.schedules[trip_key(trip)]
    records, rejected = [], []
    previous, fragment = None, -1
    for row in sorted(
        trace, key=lambda r: (r.get("t") is None, r.get("t") or 0, observation_key(r))
    ):
        key = observation_key(row)
        if not valid_projection(row, config):
            rejected.append(
                {
                    "source_id": key,
                    "reason": "INVALID_PROJECTION_NOT_SCHEDULE_DISTANCE",
                    "projection_status": row.get("projection_status"),
                }
            )
            continue
        passage = residual_at(schedule, row["s_m"], row["t"], config)
        if passage["status"] != "SUPPORTED":
            rejected.append(
                {
                    "source_id": key,
                    "reason": passage["status"],
                    "passage_intervals": passage.get("intervals", []),
                }
            )
            continue
        if (
            previous is None
            or row.get("scope_separator_before")
            or row.get("fragment_id") != previous.get("fragment_id")
            or row["t"] - previous["t"] > config["continuous_coverage_max_gap_s"]
            or row["s_m"]
            < previous["s_m"] - config.get("score_backtrack_tolerance_m", 25)
        ):
            fragment += 1
        records.append(
            {
                "source_id": key,
                "source_rid": row.get("source_rid", row.get("rid")),
                "source": "avl",
                "t": row["t"],
                "s_m": row["s_m"],
                "reference_id": trip["reference_id"],
                "fragment_id": f"continuous_fragment:{fragment}",
                "quality": row.get("quality", 1),
                "weight": row.get("quality", 1),
                "projection_distance_m": row.get("lateral_m"),
                "heading_error_deg": row.get("heading_error_deg"),
                "residual_lower_s": passage["residual_lower_s"],
                "residual_upper_s": passage["residual_upper_s"],
                "scheduled_passage_interval": passage["intervals"][0],
                "uncertainty_s": passage["uncertainty_s"],
                "headway_s": pool.headway(trip, row["s_m"]),
                "timepoint_kind": passage.get("kind"),
            }
        )
        previous = row
    return records, rejected


def _local_evidence(windows, trace, process, config):
    raw_by_id = {observation_key(r): r for r in trace}
    by_id = {r["source_id"]: r for r in process}
    output = []
    for window in windows["windows"]:
        raw = [raw_by_id[k] for k in window["observation_ids"] if k in raw_by_id]
        values = [by_id[k] for k in window["observation_ids"] if k in by_id]
        good = [r for r in raw if valid_projection(r, config)]
        progress = _observed_progress(raw, config)
        duration = coverage_metrics(raw, set(), config)["projection"]["duration_s"]
        adequate = (
            len(good) >= config["continuous_min_points"]
            and progress >= config["continuous_min_progress_m"]
            and duration >= config["continuous_min_duration_s"]
        )
        pairs = [
            (
                r["conditioned_fit_cost"] * config["continuous_conditioned_fit_weight"]
                + r["absolute_delay_level_cost"]
                * config["continuous_absolute_delay_weight"],
                r["temporal_support_s"] * r.get("quality", 1),
            )
            for r in values
        ]
        score = _mean(pairs) if adequate and values else None
        supported = {r["source_id"] for r in values if r.get("schedule_aligned")}
        output.append(
            {
                "window_id": window["id"],
                "kind": window["kind"],
                "start": window["start"],
                "end": window["end"],
                "weight": sum(w for _, w in pairs),
                "status": "NO_EVIDENCE"
                if not adequate
                else "EVIDENCE"
                if supported
                else "NO_CANDIDATE",
                "score": score,
                "quality_eligible": adequate,
                "coverage_metrics": coverage_metrics(raw, supported, config),
                "supported_point_count": len(supported),
                "estimated_delay_s": _quantile(
                    [(r["delay_s"], r["temporal_support_s"]) for r in values], 0.5
                ),
                "independent_recenter_used": False,
            }
        )
    return output


def score_candidate(points, apc, trip, pool, windows, config):
    trace = for_reference(points, trip["reference_id"], include_unprojectable=True)
    records, rejected = _point_records(trace, pool, trip, config)
    fitted = fit_delay(records, config)
    process = fitted["rows"]
    for row in process:
        row["schedule_aligned"] = (
            abs(row["conditioned_residual_s"])
            <= config["continuous_alignment_sigma_max"] * row["uncertainty_s"]
        )
        row["status"] = (
            "SUPPORTED" if row["schedule_aligned"] else "CONDITIONED_RESIDUAL_OUTLIER"
        )
    supported = {r["source_id"] for r in process if r["schedule_aligned"]}
    coverage = coverage_metrics(trace, supported, config)
    metrics = coverage["projection"], coverage["schedule"], coverage["end_to_end"]
    spatial_weights = temporal_weights(
        [r for r in trace if valid_projection(r, config)], config
    )
    projection_error = _mean(
        [
            (
                huber(
                    (r.get("lateral_m") or 0)
                    / max(config.get("max_projection_distance_m", 100), 1)
                ),
                spatial_weights.get(observation_key(r), 0),
            )
            for r in trace
            if valid_projection(r, config)
        ]
    )
    heading_cost = _mean(
        [
            (
                huber(
                    r["heading_error_deg"]
                    / config["continuous_pattern_heading_scale_deg"]
                ),
                spatial_weights.get(observation_key(r), 0),
            )
            for r in trace
            if valid_projection(r, config) and r.get("heading_error_deg") is not None
        ]
    )
    direction_prior = config["continuous_direction_prior_cost"] * _mean(
        [
            (
                float(bool(r.get("direction_fallback"))),
                spatial_weights.get(observation_key(r), 0),
            )
            for r in trace
            if valid_projection(r, config)
        ]
    )
    good = sorted(
        (r for r in trace if valid_projection(r, config)), key=lambda r: r["t"]
    )
    backtrack = sum(
        max(0.0, a["s_m"] - b["s_m"])
        for a, b in zip(good, good[1:], strict=False)
        if a.get("fragment_id") == b.get("fragment_id")
        and 0 < b["t"] - a["t"] <= config["continuous_coverage_max_gap_s"]
    )
    order_cost = huber(backtrack / config["continuous_backtrack_scale_m"])
    apc_support = _apc_evidence(
        apc, pool.schedules[trip_key(trip)], process, trip["reference_id"], config
    )
    raw_components = {
        key: fitted.get("components", {}).get(key, 0.0) for key in COMPONENT_WEIGHTS
    }
    raw_components.update(
        APC_stop_time_cost=apc_support["cost"],
        projection_and_coverage_cost=projection_error
        + (1 - metrics[0]["duration_ratio"])
        + (1 - metrics[1]["duration_ratio"]),
        pattern_progress_cost=heading_cost
        + direction_prior
        + order_cost
        + apc_support["pattern_stop_cost"],
        vehicle_sequence_cost=0.0,
    )
    if not process:
        raw_components["conditioned_fit_cost"] = config["continuous_match_cost_max"] + 1
    components = {
        key: raw_components[key] * config[weight]
        for key, weight in COMPONENT_WEIGHTS.items()
    }
    score = sum(components.values())
    local = _local_evidence(windows, trace, process, config)
    atomic = [w for w in local if w["kind"] == "atomic" and w["quality_eligible"]]
    pairs = [
        (
            w["score"]
            if w["score"] is not None
            else config["continuous_local_missing_cost"],
            max(w["weight"], 1),
        )
        for w in atomic
    ]
    longest = count = 0
    previous_end = None
    for w in [w for w in local if w["kind"] == "atomic"]:
        if (
            w["status"] == "EVIDENCE"
            and w["score"] is not None
            and w["score"] <= config["continuous_local_support_cost_max"]
        ):
            count = count + 1 if previous_end == w["start"] else 1
            longest = max(longest, count)
            previous_end = w["end"]
        else:
            count, previous_end = 0, None
    informative = sum(max(w["weight"], 1) for w in atomic)
    window_support = (
        sum(max(w["weight"], 1) for w in atomic if w["status"] == "EVIDENCE")
        / informative
        if informative
        else 0
    )
    progress = _observed_progress(trace, config)
    sufficient = (
        metrics[0]["point_count"] >= config["continuous_min_points"]
        and metrics[0]["duration_s"] >= config["continuous_min_duration_s"]
        and progress >= config["continuous_min_progress_m"]
    )
    gates = {
        "projection_coverage": metrics[0]["duration_ratio"]
        >= config["continuous_min_projection_ratio"],
        "schedule_coverage": metrics[1]["duration_ratio"]
        >= config["continuous_min_schedule_ratio"],
        "end_to_end_coverage": metrics[2]["duration_ratio"]
        >= config["continuous_min_end_to_end_ratio"],
        "adequate_observations": sufficient,
        "absolute_quality": score <= config["continuous_match_cost_max"],
    }
    reasons = [
        key.upper() + "_GATE_FAILED" for key, passed in gates.items() if not passed
    ]
    reasons.extend(fitted.get("diagnostics", []))
    alias = {
        "valid_projection_point_ratio": metrics[0]["point_ratio"],
        "valid_projection_duration_ratio": metrics[0]["duration_ratio"],
        "valid_projection_duration_s": metrics[0]["duration_s"],
        "valid_projection_progress": progress,
        "off_reference_ratio": sum(
            r.get("projection_status") == "OFF_REFERENCE" for r in trace
        )
        / len(trace)
        if trace
        else 0,
        "longest_valid_contiguous_run": coverage["longest_valid_contiguous_run_s"],
        "schedule_aligned_point_ratio": metrics[2]["point_ratio"],
        "schedule_aligned_duration_ratio": metrics[2]["duration_ratio"],
    }
    whole = {
        "status": "EVIDENCE" if supported else "NO_EVIDENCE",
        "score": components["conditioned_fit_cost"],
        "delay_process": process,
        "fragments": [],
        "schedule_aligned_point_count": len(supported),
        "schedule_aligned_duration_s": metrics[2]["duration_s"],
        "coverage_metrics": coverage,
        "continuous_passage_evaluated": True,
        "dtw_used": False,
        **alias,
    }
    return {
        "candidate_id": trip_key(trip),
        **{
            k: trip.get(k)
            for k in (
                "trip_id",
                "service_date",
                "feed_version",
                "route_id",
                "route_short_name",
                "direction_id",
                "direction_label",
                "reference_id",
                "family_id",
                "pattern_id",
                "shape_id",
                "block_id",
                "trip_headsign",
            )
        },
        "scorer": SCORER,
        "pattern_diagnostics": {
            "heading_cost": heading_cost,
            "direction_prior_cost": direction_prior,
            "backtrack_m": backtrack,
            "order_cost": order_cost,
            "stop_pattern_cost": apc_support["pattern_stop_cost"],
        },
        "scheduled_trajectory": trip.get("scheduled", []),
        "score": score,
        "support_score": math.exp(-min(score, 700)),
        "score_components": components,
        "score_components_unweighted": raw_components,
        "score_component_weights": {
            key: config[value] for key, value in COMPONENT_WEIGHTS.items()
        },
        "coverage_metrics": coverage,
        "coverage": metrics[0]["point_ratio"],
        "estimated_delay_s": fitted["delay_median_s"],
        "delay_mad_s": fitted["delay_mad_s"],
        "delay_uncertainty_s": fitted["delay_uncertainty_s"],
        "delay_uncertainty_calibrated": False,
        "delay_prior_source": fitted.get("prior_source"),
        "delay_fit": {k: v for k, v in fitted.items() if k != "rows"},
        "apc_support": apc_support,
        "whole_alignment": whole,
        "continuous_support_windows": longest,
        "weighted_mean": _mean(pairs),
        "trimmed_mean": _trimmed_mean(pairs, config["continuous_trim_fraction"]),
        "weighted_median": _quantile(pairs, 0.5),
        "weighted_p90": _quantile(pairs, 0.9),
        "schedule_supported_window_weight_ratio": window_support,
        "quality_eligible": all(gates.values()),
        "pattern_evidence_eligible": sufficient and gates["projection_coverage"],
        "quality_gates": gates,
        "reasons": reasons,
        "projection_or_passage_rejections": rejected,
        "probability": None,
        "calibrated": False,
        **alias,
    }, local


def boundary_diagnostic(candidates, local, config):
    atoms = sorted(
        {
            (w["start"], w["end"])
            for values in local.values()
            for w in values
            if w["kind"] == "atomic" and w["quality_eligible"]
        }
    )
    minimum = config["continuous_boundary_min_windows"]
    if len(atoms) < minimum * 2 or len(candidates) < 2:
        return {
            "boundary_uncertain": False,
            "status": "NO_EVIDENCE",
            "reason": "INSUFFICIENT_INDEPENDENT_BOUNDARY_EVIDENCE",
        }
    costs = {
        c["candidate_id"]: {
            (w["start"], w["end"]): w["score"]
            for w in local[c["candidate_id"]]
            if w["kind"] == "atomic" and w["score"] is not None
        }
        for c in candidates
    }

    def cost(key, subset):
        return sum(
            costs[key].get(t, config["continuous_local_missing_cost"]) for t in subset
        ) / len(subset)

    single = min((cost(key, atoms), key) for key in costs)
    split = (math.inf, None, None, None)
    for index in range(minimum, len(atoms) - minimum + 1):
        left = min((cost(key, atoms[:index]), key) for key in costs)
        right = min((cost(key, atoms[index:]), key) for key in costs)
        if left[1] == right[1]:
            continue
        value = (left[0] * index + right[0] * (len(atoms) - index)) / len(
            atoms
        ) + config["continuous_boundary_penalty"]
        if value < split[0]:
            split = value, atoms[index][0], left[1], right[1]
    gain = single[0] - split[0] if math.isfinite(split[0]) else 0
    return {
        "status": "EVIDENCE",
        "single_candidate_cost": single[0],
        "split_cost": split[0] if math.isfinite(split[0]) else None,
        "change_time": split[1],
        "left_candidate_id": split[2],
        "right_candidate_id": split[3],
        "gain": gain,
        "boundary_uncertain": gain >= config["continuous_boundary_gain_min"],
        "definition": (
            "one change point using existing shared candidate delay fits; "
            "no local recentring; hypothesis, not confirmed boundary"
        ),
    }


def match_candidate_segment(
    projection,
    apc,
    references,
    trips,
    config=None,
    *,
    service_date=None,
    route_id=None,
    calibrator=None,
):
    from .calibration import calibrate_candidates
    from .serialization import json_ready, scorer_fingerprint, validate_result

    started = time.perf_counter()
    c = get_config(config)
    c["continuous_scorer_fingerprint"] = scorer_fingerprint(c)
    points = list(
        projection.get("points", projection)
        if isinstance(projection, dict)
        else projection
    )
    windows = construct_windows(points, apc, c)
    pool = schedule_pool(trips, c)
    retrieval = retrieve(
        pool, points, windows, c, service_date=service_date, route_id=route_id
    )
    candidates, local = [], {}
    for trip in retrieval["trips"]:
        row, values = score_candidate(points, apc, trip, pool, windows, c)
        candidates.append(row)
        local[row["candidate_id"]] = values
    candidates.sort(key=lambda row: (row["score"], row["candidate_id"]))
    best = candidates[0] if candidates else None
    eligible = [row for row in candidates if row["quality_eligible"]]
    pattern = any(row["pattern_evidence_eligible"] for row in candidates)
    by_reference = collections.defaultdict(list)
    for row in points:
        if valid_projection(row, c):
            by_reference[row.get("reference_id")].append(row)
    sufficient = any(
        len(rows) >= c["continuous_min_points"]
        and sum(temporal_weights(rows, c).values()) >= c["continuous_min_duration_s"]
        and _observed_progress(rows, c) >= c["continuous_min_progress_m"]
        for rows in by_reference.values()
    )
    evidence_status = (
        "NO_EVIDENCE" if not sufficient else "EVIDENCE" if eligible else "NO_CANDIDATE"
    )
    calibration = calibrate_candidates(candidates, calibrator, c)
    probabilities = calibration.get("candidate_probabilities", {})
    for row in candidates:
        row["probability"] = probabilities.get(row["candidate_id"])
        row["calibrated"] = row["probability"] is not None
    final_ids = set(calibration.get("credible_set", []))
    final = [
        row
        for row in candidates
        if row["candidate_id"] in final_ids and row["quality_eligible"]
    ]
    boundary = boundary_diagnostic(candidates, local, c)
    if boundary.get("change_time") is not None:
        nearby = [
            event
            for event in windows["event_windows"]
            if event["start"] <= boundary["change_time"] < event["end"]
        ]
        corroboration = sorted(
            {
                reason
                for event in nearby
                for reason in event.get("event_reasons", [])
                if reason != "APC_STOP"
            }
        )
        boundary["corroborating_events"] = corroboration
        boundary["statistical_split_supported"] = boundary["boundary_uncertain"]
        boundary["boundary_uncertain"] = boundary["boundary_uncertain"] and bool(
            corroboration
        )
    status = (
        calibration.get("status", "PATTERN_ONLY")
        if final
        else "PATTERN_ONLY"
        if pattern
        else "UNMATCHED"
    )
    if not sufficient:
        status = "UNMATCHED"
    if boundary["boundary_uncertain"] and sufficient:
        status = "BOUNDARY_UNCERTAIN"
    margin = candidates[1]["score"] - best["score"] if len(candidates) > 1 else None
    reasons = [evidence_status] if evidence_status != "EVIDENCE" else []
    reasons.extend(calibration.get("reasons", []))
    if not final:
        reasons.append("NO_CALIBRATED_SCHEDULED_OCCURRENCE_CREDIBLE_SET")
    if boundary["boundary_uncertain"]:
        reasons.append("SINGLE_VERSUS_TWO_OCCURRENCE_BOUNDARY_UNCERTAIN")
    coverage = best["coverage_metrics"] if best else coverage_metrics(points, set(), c)
    result = {
        "scorer": SCORER,
        "scorer_fingerprint": c["continuous_scorer_fingerprint"],
        "status": status,
        "evidence_status": evidence_status,
        "candidates": final[:3],
        "all_candidates": candidates,
        "reported_candidate_id": final[0]["candidate_id"] if final else None,
        "decision_candidate_id": final[0]["candidate_id"] if final else None,
        "global_diagnostic_best": {
            k: best[k]
            for k in ("candidate_id", "score", "reference_id", "estimated_delay_s")
        }
        if best
        else None,
        "pattern_confidence": {
            "support_score": 1 / (1 + best["score_components"]["pattern_progress_cost"])
            if best and pattern
            else None,
            "calibrated_probability": None,
            "definition": (
                "heuristic geometric/ordered-stop pattern evidence, "
                "not calibrated occurrence probability"
            ),
        },
        "trip_confidence": {
            "support_score": None,
            "calibrated_probability": final[0]["probability"] if final else None,
            "identity_separated": status == "MATCHED",
        },
        "top1_top2_margin": margin,
        "top1_top2_margin_definition": (
            "runner-up minus best continuous cost; "
            "uncalibrated, not probability difference"
        ),
        "estimated_delay": best["estimated_delay_s"] if best else None,
        "delay_uncertainty_s": best["delay_uncertainty_s"] if best else None,
        "evidence_coverage": {
            **coverage,
            **(
                {
                    k: best[k]
                    for k in (
                        "valid_projection_point_ratio",
                        "valid_projection_duration_ratio",
                        "valid_projection_progress",
                        "off_reference_ratio",
                        "longest_valid_contiguous_run",
                    )
                }
                if best
                else {}
            ),
        },
        "supporting_window_count": sum(
            w["status"] == "EVIDENCE" for w in local.get(best["candidate_id"], [])
        )
        if best
        else 0,
        "reasons": list(dict.fromkeys(reasons)),
        "windows": windows,
        "retrieval": retrieval,
        "local_scores": local,
        "whole_trip": {
            "boundary_models": boundary,
            "coverage_metrics": coverage,
            "candidate_segment_not_verified_trip": True,
        },
        "calibration": calibration,
        "diagnostics": {
            "source_observations": len(points),
            "candidate_union_truncated": False,
            "schedule_resampling_used": False,
            "production_dtw_used": False,
            "apc_used_as_gps": False,
            "upstream_schedule_derived_features_used": False,
            "window_votes_used": False,
            "shared_delay_scope": (
                "one performed trip and candidate, physical fragments"
            ),
            "runtime_s": time.perf_counter() - started,
            "vehicle_day_alignment": "optional separate sequence shadow annotations",
            "raw_source_clock_modified": False,
        },
    }
    result["diagnostics"]["serialization_validation"] = validate_result(result)
    return json_ready(result)
