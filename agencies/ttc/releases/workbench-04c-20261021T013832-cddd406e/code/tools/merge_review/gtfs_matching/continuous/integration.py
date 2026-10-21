"""Continuous occurrence scoring. No sampled GTFS DTW is called here."""

import bisect
import collections
import math
import time
from datetime import datetime, timedelta
from itertools import groupby

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
        or b.get("scope_separator_before")
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


def _point_records(trace, pool, trip, config, occurrence_context=None):
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
        record = {
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
        if config.get("continuous_occurrence_model_enabled"):
            stop_indices = passage.get("stop_indices", [])
            reliable_indices = stop_indices or [
                index
                for index in (
                    passage.get("start_stop_index"),
                    passage.get("end_stop_index"),
                )
                if index is not None
            ]
            record.update(
                scope_separator_before=row.get("scope_separator_before", False),
                stop_id=next(iter(passage.get("stop_ids", [])), None),
                schedule_segment_id=passage.get("segment_index"),
                terminal=bool(stop_indices)
                and (0 in stop_indices or len(schedule["stops"]) - 1 in stop_indices),
                schedule_timepoint_reliable=bool(reliable_indices)
                and all(schedule["stops"][i]["timepoint"] for i in reliable_indices),
            )
            evidence = pool.occurrence_evidence(
                trip, row["s_m"], row["t"], context=occurrence_context
            )
            # Compact point diagnostics; the complete common class is stored once
            # per candidate, never copied to every AVL ping.
            record["occurrence_displacement"] = evidence
            record["occurrence_displacement_cost"] = (
                evidence["displacement_cost"]
                if evidence.get("displacement_cost") is not None
                else config["continuous_occurrence_missing_cost"]
            )
            record["headway_s"] = evidence.get("local_headway_s") or record["headway_s"]
        records.append(record)
        previous = row
    return records, rejected


def _local_evidence(windows, trace, process, config, cache=None):
    # Cache only immutable mathematical results within this performed-mode call.
    # Different support sets retain their own denominators; no score is reused.
    def cached(key, calculate):
        if cache is None:
            return calculate()
        if key not in cache:
            cache[key] = calculate()
            while len(cache) > config["continuous_local_evidence_cache_size"]:
                cache.popitem(last=False)
        cache.move_to_end(key)
        return cache[key]

    raw_by_id = {observation_key(r): r for r in trace}
    by_id = {r["source_id"]: r for r in process}
    output = []
    reference_id = trace[0].get("reference_id") if trace else None
    for window in windows["windows"]:

        def base(window=window):
            raw = [raw_by_id[k] for k in window["observation_ids"] if k in raw_by_id]
            good = [r for r in raw if valid_projection(r, config)]
            progress = _observed_progress(raw, config)
            duration = coverage_metrics(raw, set(), config)["projection"]["duration_s"]
            adequate = (
                len(good) >= config["continuous_min_points"]
                and progress >= config["continuous_min_progress_m"]
                and duration >= config["continuous_min_duration_s"]
            )
            return raw, adequate

        raw, adequate = cached(("base", reference_id, window["id"]), base)
        values = [by_id[k] for k in window["observation_ids"] if k in by_id]
        pairs = [
            (
                r["conditioned_fit_cost"] * config["continuous_conditioned_fit_weight"]
                + r["absolute_delay_level_cost"]
                * config["continuous_absolute_delay_weight"]
                + (
                    r.get("occurrence_displacement_cost", 0.0)
                    * config["continuous_occurrence_displacement_weight"]
                    if config.get("continuous_occurrence_model_enabled")
                    else 0.0
                ),
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
                "coverage_metrics": cached(
                    ("coverage", reference_id, window["id"], frozenset(supported)),
                    lambda raw=raw, supported=supported: coverage_metrics(
                        raw, supported, config
                    ),
                ),
                "supported_point_count": len(supported),
                "estimated_delay_s": _quantile(
                    [(r["delay_s"], r["temporal_support_s"]) for r in values], 0.5
                ),
                "independent_recenter_used": False,
            }
        )
    return output


def score_candidate(
    points, apc, trip, pool, windows, config, operational_context=None, local_cache=None
):
    trace = for_reference(points, trip["reference_id"], include_unprojectable=True)
    occurrence_enabled = config.get("continuous_occurrence_model_enabled", False)
    occurrence_context = None
    if occurrence_enabled:
        valid = [r for r in trace if valid_projection(r, config)]
        if valid:
            weights = temporal_weights(valid, config)
            anchor_t = weighted_quantile(
                [r["t"] for r in valid],
                [weights[observation_key(r)] for r in valid],
            )
            anchor = min(valid, key=lambda r: abs(r["t"] - anchor_t))
            s_range = min(r["s_m"] for r in valid), max(r["s_m"] for r in valid)
            t_range = min(r["t"] for r in valid), max(r["t"] for r in valid)
            stop_rows = sorted(
                (
                    r
                    for r in normalize_apc(apc, trip["reference_id"])
                    if r.get("status") == "EVIDENCE"
                    and r.get("stop_id") is not None
                    and r.get("confidence", 0) > 0
                    and r.get("confidence", 0)
                    >= config.get("continuous_occurrence_apc_min_confidence", 0.5)
                    and s_range[0] <= r["canonical_stop_s"] <= s_range[1]
                    and r["interval_end"] >= t_range[0]
                    and r["interval_start"] <= t_range[1]
                ),
                key=lambda r: r["interval_start"],
            )
            stops = tuple(
                key for key, _ in groupby(str(r["stop_id"]) for r in stop_rows)
            )
            occurrence_context = pool.confusability(
                trip,
                s_range,
                anchor["s_m"],
                anchor["t"],
                observed_stop_ids=stops,
            )
    records, rejected = _point_records(trace, pool, trip, config, occurrence_context)
    fitted = (
        fit_delay(records, config, operational_context=operational_context)
        if occurrence_enabled
        else fit_delay(records, config)
    )
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
    weights_by_component = dict(COMPONENT_WEIGHTS)
    if occurrence_enabled:
        weights_by_component["occurrence_displacement_cost"] = (
            "continuous_occurrence_displacement_weight"
        )
    raw_components = {
        key: fitted.get("components", {}).get(key, 0.0) for key in weights_by_component
    }
    occurrence_summary = {}
    if occurrence_enabled:
        evidence = [r.get("occurrence_displacement", {}) for r in process]
        raw_components["occurrence_displacement_cost"] = _mean(
            [
                (r.get("occurrence_displacement_cost", 0.0), r["normalized_weight"])
                for r in process
            ]
        )
        occurrence_summary = {
            "crossed_occurrences": max(
                (r["crossed_occurrences"] for r in evidence), default=None
            )
            if evidence
            and all(r.get("crossed_occurrences") is not None for r in evidence)
            else None,
            "outside_cell_s": _mean(
                [
                    (
                        r["occurrence_displacement"]["outside_cell_s"],
                        r["normalized_weight"],
                    )
                    for r in process
                    if r.get("occurrence_displacement", {}).get("outside_cell_s")
                    is not None
                ]
            )
            if evidence and all(r.get("outside_cell_s") is not None for r in evidence)
            else None,
            "displacement_cost": raw_components["occurrence_displacement_cost"],
            "aggregation": (
                "duration-weighted cost; conservative maximum crossed occurrence count"
            ),
            "context": occurrence_context,
            "status": "EVIDENCE" if evidence else "NO_EVIDENCE",
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
        for key, weight in weights_by_component.items()
    }
    score = sum(components.values())
    local = _local_evidence(windows, trace, process, config, local_cache)
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
            key: config[value] for key, value in weights_by_component.items()
        },
        **(
            {
                "occurrence_displacement": occurrence_summary,
                "operational_context": operational_context,
                "occurrence_model_enabled": True,
            }
            if occurrence_enabled
            else {}
        ),
        "coverage_metrics": coverage,
        "coverage": metrics[0]["point_ratio"],
        "estimated_delay_s": fitted["delay_median_s"],
        "delay_mad_s": fitted["delay_mad_s"],
        "delay_uncertainty_s": fitted["delay_uncertainty_s"],
        "delay_uncertainty_calibrated": False,
        "delay_prior_source": fitted.get("prior_source"),
        "delay_fit": {k: v for k, v in fitted.items() if k != "rows"},
        **(
            {
                key: fitted.get(key)
                for key in (
                    "operational_disruption_responsibility",
                    "inferred_delay_regime",
                    "delay_regime_is_ground_truth",
                )
            }
            if occurrence_enabled
            else {}
        ),
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
    pool = schedule_pool(trips, c, references=references, service_date=service_date)
    candidates, local = [], {}
    local_cache = (
        collections.OrderedDict()
        if c.get("continuous_occurrence_model_enabled")
        and c["continuous_local_evidence_cache_size"] > 0
        else None
    )
    operational_context = None
    if c.get("continuous_occurrence_model_enabled"):
        good = [p for p in points if valid_projection(p, c)]
        support = temporal_weights(good, c)
        observed_t = weighted_quantile(
            [p["t"] for p in good], [support[observation_key(p)] for p in good]
        )
        by_ref = {ref["reference_id"]: ref for ref in references}
        directions = collections.Counter()
        for p in good:
            direction = by_ref.get(p.get("reference_id"), {}).get("direction_id")
            if direction is not None:
                directions[str(direction)] += support[observation_key(p)]
        # The legacy ingestion axis encodes local wall time as epoch seconds.
        # Read its hour without timezone conversion or altering source timestamps.
        clock = (
            datetime(1970, 1, 1) + timedelta(seconds=observed_t)
            if observed_t is not None
            else None
        )
        feed_ids = {
            str(t["feed_version"]) for t in trips if t.get("feed_version") is not None
        }
        route_ids = {str(t["route_id"]) for t in trips if t.get("route_id") is not None}
        operational_context = {
            "feed_version": next(iter(feed_ids)) if len(feed_ids) == 1 else None,
            "route_id": str(route_id)
            if route_id is not None
            else next(iter(route_ids))
            if len(route_ids) == 1
            else None,
            "direction": directions.most_common(1)[0][0] if directions else None,
            "service_date": service_date
            or (clock.date().isoformat() if clock else None),
            "observed_time_bucket": clock.hour if clock else None,
            "observed_t": observed_t,
            "clock_uncertainty_s": c["continuous_clock_uncertainty_s"],
            "source": (
                "shared performed-evidence temporal support; not candidate schedule"
            ),
        }
        operational_context = {
            key: value
            for key, value in operational_context.items()
            if value is not None
        }
    rescue_context = None
    if c.get("continuous_occurrence_model_enabled"):
        from .operational_prior import rescue_delay_bounds, resolve_prior

        rescue_context = rescue_delay_bounds(
            resolve_prior(c, operational_context),
            probability=c["continuous_operational_prior_rescue_probability"],
        )
    retrieval = retrieve(
        pool,
        points,
        windows,
        c,
        service_date=service_date,
        route_id=route_id,
        apc=apc,
        rescue_context=rescue_context,
    )
    for trip in retrieval["trips"]:
        row, values = score_candidate(
            points, apc, trip, pool, windows, c, operational_context, local_cache
        )
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
        if c.get("continuous_occurrence_model_enabled"):
            row["occurrence_decision_guard"] = calibration.get(
                "occurrence_decision_guards", {}
            ).get(row["candidate_id"])
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
