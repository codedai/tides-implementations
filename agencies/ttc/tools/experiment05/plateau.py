"""Observation-conditioned plateau diagnostics and conservative right censoring.

No scheduled winner is read. A censored extent means a supported evidence end,
not an asserted physical end of service. Every input AVL row remains returned.
"""

from __future__ import annotations

import collections
import math
import statistics
from bisect import bisect_right

from tools.merge_review.gtfs_matching.canonical import finite, valid_coordinate
from tools.merge_review.gtfs_matching.continuous.retrieval import schedule_pool
from tools.merge_review.gtfs_matching.continuous.schedule import passage_at
from tools.merge_review.gtfs_matching.off_reference import distance

STATES = (
    "NORMAL",
    "STATIONARY_PLATEAU",
    "TERMINAL_LAYOVER",
    "LONG_MIDROUTE_STOP",
    "FROZEN_TELEMETRY",
    "OFF_REFERENCE_MOVING",
    "REFERENCE_MISMATCH",
    "RIGHT_CENSORED_TAIL",
    "RECOVERED_AFTER_PLATEAU",
)
REQUIRED = (
    "atomic_window_s",
    "minimum_window_points",
    "max_adjacent_gap_s",
    "canonical_still_range_m",
    "raw_still_radius_m",
    "raw_moving_distance_m",
    "raw_moving_speed_mps",
    "frozen_coordinate_epsilon_m",
    "frozen_min_points",
    "frozen_speed_conflict_kph",
    "minimum_plateau_s",
    "terminal_radius_m",
    "recovery_lookahead_s",
    "recovery_progress_m",
    "recovery_min_points",
    "censor_min_duration_s",
    "censor_max_duration_s",
    "censor_headway_multiplier",
    "censor_terminal_dwell_allowance_s",
    "counterfactual_advance_m",
    "counterfactual_min_candidates",
    "counterfactual_delay_prior_window_s",
    "counterfactual_delay_growth_s_per_s",
    "counterfactual_rescue_delay_s",
    "apc_meaningful_activity_min",
    "apc_colocation_radius_m",
    "counterfactual_min_prior_points",
    "counterfactual_prior_min_progress_m",
    "counterfactual_delay_mad_max_s",
)


def _configuration(config):
    cfg = config["plateau"]
    for name in REQUIRED:
        if not finite(cfg.get(name)) or cfg[name] < 0:
            raise ValueError(f"Missing or invalid explicit plateau threshold: {name}")
    for name in (
        "atomic_window_s",
        "max_adjacent_gap_s",
        "minimum_plateau_s",
        "censor_min_duration_s",
        "counterfactual_delay_prior_window_s",
    ):
        if cfg[name] <= 0:
            raise ValueError(f"Plateau {name} must be positive")
    for name in (
        "minimum_window_points",
        "recovery_min_points",
        "frozen_min_points",
        "counterfactual_min_prior_points",
        "counterfactual_min_candidates",
    ):
        if cfg[name] < 1 or int(cfg[name]) != cfg[name]:
            raise ValueError(f"Plateau {name} must be a positive integer")
    if cfg["censor_max_duration_s"] < cfg["censor_min_duration_s"]:
        raise ValueError("Plateau adaptive duration bounds are reversed")
    if cfg["counterfactual_delay_growth_s_per_s"] >= 1:
        raise ValueError("Counterfactual delay growth must remain below clock rate")
    return cfg


def _options(row):
    values = {
        p.get("reference_id"): p
        for p in row.get("projection_alternatives", [])
        if p.get("reference_id")
    }
    if row.get("reference_id"):
        values[row["reference_id"]] = row
    return values


def _valid(option, matcher):
    return bool(
        option
        and option.get("projection_status") == "ON_REFERENCE"
        and finite(option.get("s_m"))
        and (
            option.get("quality") is None
            or (
                finite(option["quality"])
                and option["quality"] >= matcher["continuous_min_projection_quality"]
            )
        )
    )


def _trace(rows, reference, matcher):
    return [
        (row, option)
        for row in rows
        if _valid(option := _options(row).get(reference), matcher)
    ]


def _metrics(rows, references, cfg, matcher):
    coordinates = [p for p in rows if valid_coordinate(p.get("lat"), p.get("lon"))]
    center = (
        {
            "lat": statistics.median(p["lat"] for p in coordinates),
            "lon": statistics.median(p["lon"] for p in coordinates),
        }
        if coordinates
        else None
    )
    radius = max((distance(p, center) for p in coordinates), default=None)
    pairs = [(a, b, distance(a, b)) for a, b in zip(rows, rows[1:], strict=False)]
    gaps = [b["t"] - a["t"] for a, b, _ in pairs]
    raw_speeds = [
        d / (b["t"] - a["t"])
        for a, b, d in pairs
        if d is not None and 0 < b["t"] - a["t"] <= cfg["max_adjacent_gap_s"]
    ]
    raw_path = sum(
        d
        for a, b, d in pairs
        if d is not None and b["t"] - a["t"] <= cfg["max_adjacent_gap_s"]
    )
    progress = {}
    for ref in references:
        trace = _trace(rows, ref, matcher)
        values = [option["s_m"] for _, option in trace]
        if values:
            progress[ref] = {
                "count": len(values),
                "range_m": max(values) - min(values),
                "first_s_m": values[0],
                "last_s_m": values[-1],
                "median_s_m": statistics.median(values),
                "monotone": all(
                    b - a >= -cfg["canonical_still_range_m"]
                    for a, b in zip(values, values[1:], strict=False)
                ),
            }
    moving_refs = [
        ref
        for ref, item in progress.items()
        if item["count"] >= cfg["minimum_window_points"]
        and item["monotone"]
        and item["last_s_m"] - item["first_s_m"] > cfg["canonical_still_range_m"]
    ]
    primary = [row["s_m"] for row in rows if _valid(row, matcher)]
    primary_refs = {row.get("reference_id") for row in rows if _valid(row, matcher)}
    primary_flat = not primary or (
        len(primary_refs) == 1
        and max(primary) - min(primary) <= cfg["canonical_still_range_m"]
    )
    speed = max(
        (
            p.get("speed_kph", p.get("speed"))
            for p in rows
            if finite(p.get("speed_kph", p.get("speed")))
        ),
        default=0.0,
    )
    raw_moving = bool(
        radius is not None
        and radius > cfg["raw_still_radius_m"]
        and (
            raw_path >= cfg["raw_moving_distance_m"]
            or max(raw_speeds, default=0) >= cfg["raw_moving_speed_mps"]
        )
    )
    exact_repeat = bool(
        radius is not None
        and len(coordinates) >= cfg["frozen_min_points"]
        and radius <= cfg["frozen_coordinate_epsilon_m"]
    )
    frozen = exact_repeat and speed >= cfg["frozen_speed_conflict_kph"]
    checked = bool(references) and all(
        p.get("all_references_checked") is True
        and set(references) <= _options(p).keys()
        for p in rows
    )
    missing = (
        max(gaps, default=0) > cfg["max_adjacent_gap_s"]
        or any(p.get("time_ambiguous") or p.get("time_nonexistent") for p in rows)
        or any(p.get("scope_separator_before") for p in rows[1:])
    )
    stationary = bool(
        radius is not None
        and len(coordinates) >= cfg["minimum_window_points"]
        and radius <= cfg["raw_still_radius_m"]
        and primary_flat
        and not moving_refs
    )
    if raw_moving and (primary_flat or not progress):
        state = "REFERENCE_MISMATCH" if progress else "OFF_REFERENCE_MOVING"
    elif primary_flat and moving_refs:
        state = "REFERENCE_MISMATCH"
    elif frozen:
        state = "FROZEN_TELEMETRY"
    elif stationary:
        state = "STATIONARY_PLATEAU"
    else:
        state = "NORMAL"
    return {
        "state": state,
        "raw_radius_m": radius,
        "raw_path_m": raw_path,
        "raw_max_speed_mps": max(raw_speeds, default=None),
        "raw_center": center,
        "coordinate_count": len(coordinates),
        "raw_moving": raw_moving,
        "progress": progress,
        "moving_reference_ids": moving_refs,
        "all_references_checked": checked,
        "missing_or_uncertain_observations": missing,
        "stationary": stationary,
        "frozen_telemetry": frozen,
        "exact_coordinate_repeat_without_independent_freeze_proof": exact_repeat
        and not frozen,
    }


def _position(reference, progress):
    points = reference.get("points", [])
    if not points or progress < points[0]["s_m"] or progress > points[-1]["s_m"]:
        return None
    index = max(0, bisect_right([p["s_m"] for p in points], progress) - 1)
    a = points[index]
    if index == len(points) - 1:
        return a
    b = points[index + 1]
    factor = (progress - a["s_m"]) / (b["s_m"] - a["s_m"]) if b["s_m"] > a["s_m"] else 0
    return {key: a[key] + factor * (b[key] - a[key]) for key in ("lat", "lon")}


def _terminals(canonical, matcher):
    markers = collections.defaultdict(dict)
    for ref in canonical.get("references", []):
        if ref.get("points"):
            for p in (ref["points"][0], ref["points"][-1]):
                markers[ref["reference_id"]][p["s_m"]] = "REFERENCE_ENDPOINT"
    pool = schedule_pool(
        canonical.get("trips", []), matcher, references=canonical.get("references", [])
    )
    for key, trip in pool.trips.items():
        stops = trip.get("scheduled", [])
        if (
            trip.get("canonical_valid", True)
            and stops
            and pool.schedules[key]["status"] == "READY"
        ):
            for stop in (stops[0], stops[-1]):
                if finite(stop.get("s_m")):
                    markers[trip["reference_id"]][stop["s_m"]] = (
                        "SCHEDULED_TRIP_ENDPOINT_OR_SHORT_TURN"
                    )
    return markers


def _terminal_context(metrics, references, markers, cfg):
    found = []
    for ref, evidence in metrics["progress"].items():
        for progress, kind in markers[ref].items():
            ds = abs(evidence["median_s_m"] - progress)
            location = _position(references[ref], progress)
            raw_distance = (
                distance(metrics["raw_center"], location)
                if metrics["raw_center"] and location
                else None
            )
            if (
                ds <= cfg["terminal_radius_m"]
                and raw_distance is not None
                and raw_distance <= cfg["terminal_radius_m"]
            ):
                found.append(
                    {
                        "reference_id": ref,
                        "terminal_s_m": progress,
                        "kind": kind,
                        "progress_distance_m": ds,
                        "raw_distance_m": raw_distance,
                    }
                )
    return found


def _apc_activity(apc, start, end, metrics, references, cfg):
    service, unknown, away = [], [], []
    for row in apc:
        a, b = (
            row.get("arrival", row.get("interval_start")),
            row.get("departure", row.get("interval_end")),
        )
        if not finite(a) or not finite(b) or b < a:
            if finite(row.get("t")) and start <= row["t"] <= end:
                unknown.append(row.get("rid"))
            continue
        if a > end or b < start:
            continue
        if row.get("time_ambiguous") or row.get("time_nonexistent"):
            unknown.append(row.get("rid"))
            continue
        counts = [row.get(key) for key in ("boarding", "alighting")]
        if any(not finite(value) or value < 0 for value in counts):
            unknown.append(row.get("rid"))
            continue
        if sum(counts) < cfg["apc_meaningful_activity_min"]:
            continue
        raw_distance = (
            distance(row, metrics["raw_center"]) if metrics["raw_center"] else None
        )
        located = (
            raw_distance is not None and raw_distance <= cfg["apc_colocation_radius_m"]
        )
        # A raw stop ID/code may locate interval evidence; no fake GPS point.
        stop = str(row.get("stop_id", row.get("stop", "")))
        for ref, evidence in metrics["progress"].items():
            matches = [
                p
                for p in references[ref].get("stop_positions", [])
                if stop in {str(p.get("stop_id")), str(p.get("stop_code"))}
                and not p.get("ambiguous")
            ]
            if (
                raw_distance is None
                and matches
                and all(
                    abs(p["s_m"] - evidence["median_s_m"])
                    <= cfg["apc_colocation_radius_m"]
                    for p in matches
                )
            ):
                located = True
        if located:
            service.append(
                {
                    "rid": row.get("rid"),
                    "arrival_t": a,
                    "departure_t": b,
                    "boarding": counts[0],
                    "alighting": counts[1],
                }
            )
        else:
            away.append(row.get("rid"))
    return {
        "colocated_service_intervals": service,
        "unknown_activity_rids": unknown,
        "noncolocated_service_rids": away,
        "last_service_end": max((p["departure_t"] for p in service), default=start),
        "absence_of_activity_is_observational_only": True,
    }


def _recovery(rows, after, anchors, cfg, matcher):
    future = [
        row for row in rows if after < row["t"] <= after + cfg["recovery_lookahead_s"]
    ]
    for ref, anchor in anchors.items():
        trace = _trace(future, ref, matcher)
        run = []
        for row, option in trace:
            if run and (
                row["t"] - run[-1][0]["t"] > cfg["max_adjacent_gap_s"]
                or option["s_m"] < run[-1][1]["s_m"] - cfg["canonical_still_range_m"]
            ):
                run = []
            run.append((row, option))
            if (
                len(run) >= cfg["recovery_min_points"]
                and option["s_m"] - anchor["median_s_m"] >= cfg["recovery_progress_m"]
            ):
                return {
                    "reference_id": ref,
                    "t": row["t"],
                    "rid": row.get("rid"),
                    "progress_m": option["s_m"] - anchor["median_s_m"],
                }
    return None


def _progress_at_time(schedule, timestamp):
    stops = schedule["stops"]
    if timestamp <= stops[0]["arrival_t"]:
        return stops[0]["s_m"]
    if timestamp >= stops[-1]["departure_t"]:
        return stops[-1]["s_m"]  # No extrapolation beyond the actual pattern.
    for row in schedule["movements"]:
        if row["start_t"] <= timestamp <= row["end_t"]:
            fraction = (timestamp - row["start_t"]) / (row["end_t"] - row["start_t"])
            return row["start_s_m"] + fraction * (row["end_s_m"] - row["start_s_m"])
    for row in stops:
        if row["arrival_t"] <= timestamp <= row["departure_t"]:
            return row["s_m"]
    for row in schedule["stationary"]:
        if row["start_t"] <= timestamp <= row["end_t"]:
            return row["start_s_m"]
    return None


def _counterfactual(rows, run, metrics, canonical, cfg, matcher):
    """High recall, pattern-compatible pool; one pre-plateau delay per hypothesis."""
    onset, end = run[0]["t"], run[-1]["t"]
    prior = [
        p
        for p in rows
        if onset - cfg["counterfactual_delay_prior_window_s"] <= p["t"] < onset
    ]
    pool = schedule_pool(
        canonical.get("trips", []), matcher, references=canonical.get("references", [])
    )
    hypotheses, excluded, unresolved, passage_times = [], collections.Counter(), [], []
    traces = {ref: _trace(prior, ref, matcher) for ref in metrics["progress"]}
    for key, trip in pool.trips.items():
        ref = trip.get("reference_id")
        if ref not in metrics["progress"]:
            continue
        schedule = pool.schedules[key]
        if schedule["status"] != "READY":
            excluded["INVALID_SCHEDULE"] += 1
            continue
        anchor = metrics["progress"][ref]["median_s_m"]
        passage = passage_at(schedule, anchor, matcher)
        if passage["status"] != "SUPPORTED" or len(passage["intervals"]) != 1:
            excluded["NO_UNAMBIGUOUS_ANCHOR_PASSAGE"] += 1
            continue
        a, b = passage["intervals"][0]
        if max(a - onset, onset - b, 0) > cfg["counterfactual_rescue_delay_s"]:
            excluded["OUTSIDE_HIGH_RECALL_PASSAGE_BUFFER"] += 1
            continue
        passage_times.append((a + b) / 2)
        # A shape ending here cannot test onward progress. Record the exclusion,
        # and abstain if every plausible hypothesis has this limitation.
        if schedule["s_max"] - anchor <= cfg["canonical_still_range_m"]:
            excluded["PATTERN_ALREADY_FINISHED_AT_ANCHOR_NOT_AN_ADVANCE_TEST"] += 1
            continue
        residuals, support, uncertainties = [], [], []
        for point, option in traces[ref]:
            p = passage_at(schedule, option["s_m"], matcher)
            if p["status"] == "SUPPORTED" and len(p["intervals"]) == 1:
                lo, hi = p["intervals"][0]
                residuals.append((point["t"] - hi, point["t"] - lo))
                support.append((point, option))
                uncertainties.append(p["uncertainty_s"])
        if (
            len(support) < cfg["counterfactual_min_prior_points"]
            or max((p[1]["s_m"] for p in support), default=0)
            - min((p[1]["s_m"] for p in support), default=0)
            < cfg["counterfactual_prior_min_progress_m"]
            or any(
                b[1]["s_m"] < a[1]["s_m"] - cfg["canonical_still_range_m"]
                for a, b in zip(support, support[1:], strict=False)
            )
            or (support and onset - support[-1][0]["t"] > cfg["max_adjacent_gap_s"])
            or any(
                b[0]["t"] - a[0]["t"] > cfg["max_adjacent_gap_s"]
                for a, b in zip(support, support[1:], strict=False)
            )
        ):
            unresolved.append(
                {
                    "candidate_id": key,
                    "reason": "INSUFFICIENT_MONOTONE_PRE_PLATEAU_EVIDENCE",
                }
            )
            continue
        delay = statistics.median((a + b) / 2 for a, b in residuals)
        deviations = [max(a - delay, delay - b, 0) for a, b in residuals]
        mad = statistics.median(deviations)
        if mad > cfg["counterfactual_delay_mad_max_s"]:
            unresolved.append(
                {"candidate_id": key, "reason": "UNSTABLE_PRE_PLATEAU_DELAY"}
            )
            continue
        uncertainty = max(statistics.median(uncertainties), 1.4826 * mad)
        if onset - delay - uncertainty >= schedule["stops"][-1]["departure_t"]:
            excluded["SCHEDULE_ALREADY_FINISHED_NOT_AN_ADVANCE_TEST"] += 1
            continue
        hypotheses.append(
            {
                "candidate_id": key,
                "reference_id": ref,
                "anchor_s_m": anchor,
                "delay_median_s": delay,
                "delay_mad_s": mad,
                "delay_uncertainty_s": uncertainty,
                "prior_points": len(support),
                "schedule": schedule,
            }
        )
    unique = sorted(set(passage_times))
    gaps = [
        b - a
        for a, b in zip(unique, unique[1:], strict=False)
        if b - a >= matcher["continuous_occurrence_min_headway_s"]
    ]
    enough_gaps = len(gaps) >= matcher["continuous_occurrence_min_gap_samples"]
    headway = (
        statistics.median(gaps)
        if enough_gaps
        else matcher["continuous_occurrence_default_headway_s"]
    )
    needed = min(
        cfg["censor_max_duration_s"],
        max(
            cfg["censor_min_duration_s"],
            headway * cfg["censor_headway_multiplier"]
            + cfg["censor_terminal_dwell_allowance_s"],
        ),
    )
    checks, first_pass = [], None
    for point in run:
        if point["t"] - onset < max(needed, cfg["recovery_lookahead_s"]):
            continue
        advances = []
        for hypothesis in hypotheses:
            bounded_delay = (
                hypothesis["delay_median_s"]
                + hypothesis["delay_uncertainty_s"]
                + cfg["counterfactual_delay_growth_s_per_s"] * (point["t"] - onset)
            )
            progress = _progress_at_time(
                hypothesis["schedule"], point["t"] - bounded_delay
            )
            advances.append(
                None if progress is None else progress - hypothesis["anchor_s_m"]
            )
        passed = (
            not unresolved
            and len(advances) >= cfg["counterfactual_min_candidates"]
            and all(
                value is not None and value >= cfg["counterfactual_advance_m"]
                for value in advances
            )
        )
        checks = [
            {
                **{key: value for key, value in h.items() if key != "schedule"},
                "counterfactual_advance_m": advance,
                "delay_process_basis": "pre_plateau_fixed_plus_bounded_growth",
            }
            for h, advance in zip(hypotheses, advances, strict=True)
        ]
        if passed:
            first_pass = point["t"]
            break
    return {
        "passed": first_pass is not None,
        "decision_time": first_pass,
        "adaptive_duration_s": needed,
        "shared_local_headway_s": headway,
        "headway_source": "local_passage_gaps"
        if enough_gaps
        else "configured_fallback",
        "eligible_candidate_count": len(hypotheses),
        "candidates": checks,
        "excluded_counts": dict(excluded),
        "unresolved_candidates": unresolved,
        "final_candidate_assignment_used": False,
        "free_tail_delay_fitted": False,
        "evaluated_until": end,
    }


def assess_trip_extent(points, apc, canonical, config):
    """Return all original rows with additive flags; never create a trip boundary."""
    cfg, matcher = _configuration(config), config["matcher"]
    output = [dict(point) for point in points]
    rows = sorted(
        [p for p in output if finite(p.get("t"))],
        key=lambda p: (p["t"], str(p.get("rid"))),
    )
    for row in output:
        row.setdefault("platform_state", "NORMAL")
        row.setdefault("exclude_from_matching", False)
        row.setdefault("censored_tail", False)
    start, end = (rows[0]["t"], rows[-1]["t"]) if rows else (None, None)
    extent = {
        "raw_segment_start": start,
        "raw_segment_end": end,
        "effective_segment_start": start,
        "effective_segment_end": end,
        "plateau_onset_time": None,
        "censor_decision_time": None,
        "censored_tail_duration_s": 0.0,
        "censor_reason": None,
        "platform_states": ["NORMAL"],
        "diagnostic_flags": [],
        "retained_endpoint_rid": None,
        "retained_apc_rids": [],
        "physical_trip_end_confirmed": False,
    }
    diagnostics = []
    if not rows:
        extent["diagnostic_flags"] = ["NO_TIMESTAMPED_AVL_EVIDENCE"]
        return {"extent": extent, "points": output, "diagnostics": diagnostics}
    references = {ref["reference_id"]: ref for ref in canonical.get("references", [])}
    terminals = _terminals(canonical, matcher)
    windows = collections.defaultdict(list)
    for row in rows:
        windows[math.floor(row["t"] / cfg["atomic_window_s"])].append(row)
    blocks, current = [], []
    for index, selected in sorted(windows.items()):
        metrics = _metrics(selected, references, cfg, matcher)
        informative = (
            len(selected) >= cfg["minimum_window_points"]
            and not metrics["missing_or_uncertain_observations"]
        )
        diagnostics.append(
            {
                "diagnostic_type": "PLATEAU_ATOMIC_WINDOW",
                "start_t": index * cfg["atomic_window_s"],
                "end_t": (index + 1) * cfg["atomic_window_s"],
                "non_overlapping": True,
                "observation_count": len(selected),
                "evidence_status": "EVIDENCE" if informative else "NO_EVIDENCE",
                **metrics,
            }
        )
        candidate = informative and metrics["state"] != "NORMAL"
        connected = (
            not current
            or selected[0]["t"] - current[-1]["t"] <= cfg["max_adjacent_gap_s"]
        )
        if current and (not connected or not candidate):
            # A final incomplete atomic window is not independent evidence, but
            # its observed, co-located rows can extend an already supported run.
            aggregate = _metrics(current + selected, references, cfg, matcher)
            if connected and not informative and aggregate["stationary"]:
                current.extend(selected)
                continue
            blocks.append(current)
            current = []
        if candidate:
            current.extend(selected)
    if current:
        blocks.append(current)
    states, flags = set(), set()
    for previous, following in zip(rows, rows[1:], strict=False):
        if following["t"] - previous["t"] > cfg["max_adjacent_gap_s"]:
            flags.add("MISSING_OR_UNCERTAIN_OBSERVATION_INTERVAL")
            diagnostics.append(
                {
                    "diagnostic_type": "PLATEAU_OBSERVATION_GAP",
                    "start_t": previous["t"],
                    "end_t": following["t"],
                    "reason": "NO_CONTINUOUS_STATIONARITY_PROOF_ACROSS_GAP",
                }
            )
    for run in blocks:
        metrics = _metrics(run, references, cfg, matcher)
        if run[-1]["t"] - run[0]["t"] < cfg["minimum_plateau_s"]:
            continue
        state = metrics["state"]
        terminal = _terminal_context(metrics, references, terminals, cfg)
        activity = _apc_activity(apc, run[0]["t"], end, metrics, references, cfg)
        recovery = _recovery(rows, run[-1]["t"], metrics["progress"], cfg, matcher)
        reasons = []
        if recovery:
            state = "RECOVERED_AFTER_PLATEAU"
            reasons.append("VALID_RECOVERY_RETAINS_ORIGINAL_SEGMENT")
        elif state == "STATIONARY_PLATEAU":
            state = "TERMINAL_LAYOVER" if terminal else "LONG_MIDROUTE_STOP"
        states.add(state)
        if not terminal:
            reasons.append("NO_VALID_TERMINAL_OR_SHORT_TURN_CONTEXT")
        if not metrics["all_references_checked"]:
            reasons.append("ALTERNATIVE_REFERENCES_NOT_FULLY_CHECKED")
        if metrics["missing_or_uncertain_observations"]:
            reasons.append("MISSING_OR_UNCERTAIN_OBSERVATION_INTERVAL")
        if not (metrics["stationary"] or metrics["frozen_telemetry"]):
            reasons.append("RAW_MOVEMENT_OR_ALTERNATIVE_PROGRESS_PREVENTS_CENSOR")
        if run[-1] is not rows[-1]:
            reasons.append("INTERNAL_PLATEAU_IS_NOT_A_TRIP_BOUNDARY")
        if activity["unknown_activity_rids"]:
            reasons.append("APC_ACTIVITY_UNKNOWN")
        if activity["noncolocated_service_rids"]:
            reasons.append("APC_SERVICE_AWAY_FROM_PLATEAU")
        if any(
            p.get("right_boundary_missing") or p.get("observation_gap_after")
            for p in run
        ):
            reasons.append("MISSING_RIGHT_BOUNDARY_CONTEXT")
        idle_start = max(run[0]["t"], activity["last_service_end"])
        idle = [p for p in run if p["t"] >= idle_start]
        counterfactual = None
        if not reasons and idle:
            # Fit on observations preceding the FIRST plateau, never on the
            # stationary tail. APC activity changes the duration, not that fit.
            counterfactual = _counterfactual(
                rows, run, metrics, canonical, cfg, matcher
            )
            duration_needed = max(
                counterfactual["adaptive_duration_s"], cfg["recovery_lookahead_s"]
            )
            if end - idle_start < duration_needed:
                reasons.append("SUSTAINED_IDLE_DURATION_INSUFFICIENT_AFTER_APC_SERVICE")
            if not counterfactual["passed"]:
                reasons.append(
                    "COUNTERFACTUAL_ADVANCE_NOT_PROVEN_FOR_ALL_PLAUSIBLE_CANDIDATES"
                )
        if not reasons and counterfactual and counterfactual["passed"]:
            candidates = [p for p in run if p["t"] <= idle_start and _valid(p, matcher)]
            if not candidates:
                reasons.append("NO_VALID_AVL_ENDPOINT_TO_RETAIN")
            else:
                endpoint = candidates[-1]
                decision = max(
                    counterfactual["decision_time"], idle_start + duration_needed
                )
                decision = next(p["t"] for p in run if p["t"] >= decision)
                for point in run:
                    point["platform_state_before_censor"] = state
                    if point is not endpoint:
                        point.update(
                            exclude_from_matching=True,
                            censored_tail=True,
                            trajectory_included=False,
                        )
                state = "RIGHT_CENSORED_TAIL"
                extent.update(
                    effective_segment_end=idle_start,
                    plateau_onset_time=run[0]["t"],
                    censor_decision_time=decision,
                    censored_tail_duration_s=end - idle_start,
                    censor_reason="SUSTAINED_TERMINAL_PLATEAU_WITH_COUNTERFACTUAL_ADVANCE",
                    retained_endpoint_rid=endpoint.get("rid"),
                    retained_apc_rids=[
                        p["rid"] for p in activity["colocated_service_intervals"]
                    ],
                )
        for point in run:
            point["platform_state"] = state
        states.add(state)
        flags.update(reasons)
        diagnostics.append(
            {
                "diagnostic_type": "PLATEAU_RUN",
                "start_t": run[0]["t"],
                "end_t": run[-1]["t"],
                "state": state,
                "metrics": metrics,
                "terminal_context": terminal,
                "apc_activity": activity,
                "recovery": recovery,
                "counterfactual": counterfactual,
                "abstention_reasons": reasons,
                "idle_start": idle_start,
            }
        )
        if run[-1] is rows[-1] and extent["plateau_onset_time"] is None:
            extent["plateau_onset_time"] = run[0]["t"]
    extent["platform_states"] = sorted(states) if states else ["NORMAL"]
    extent["diagnostic_flags"] = sorted(flags)
    return {"extent": extent, "points": output, "diagnostics": diagnostics}
