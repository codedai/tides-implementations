"""Unsampled, monotone GTFS stop-to-stop trajectories in physical (s, t) units.

Times are numeric service-clock seconds supplied by the feed adapter. This
module never rebases a service day, wraps a >24-hour time, or samples a grid.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right

DEFAULTS = {
    "continuous_stop_tolerance_m": 0.5,
    "continuous_progress_epsilon_m": 0.001,
    "continuous_timepoint_uncertainty_s": 30.0,
    "continuous_nontimepoint_uncertainty_s": 90.0,
    "continuous_interpolation_uncertainty_s": 30.0,
}


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def get_config(config=None):
    result = DEFAULTS | (config or {})
    for name in DEFAULTS:
        if not _finite(result[name]) or result[name] < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    if result["continuous_timepoint_uncertainty_s"] <= 0:
        raise ValueError("Timepoint uncertainty must be positive")
    if (
        result["continuous_nontimepoint_uncertainty_s"]
        < result["continuous_timepoint_uncertainty_s"]
    ):
        raise ValueError(
            "Non-timepoint uncertainty cannot be smaller than timepoint uncertainty"
        )
    return result


def build_schedule(trip, config=None):
    """Validate ordered stops; do not repair backtracking by sorting on progress.

    Repeated stop IDs are fine when their canonical progress is increasing.
    Consecutive equal-progress stops describe stationary time, not a division
    by zero. A loop whose canonical mapping reverses progress is explicitly
    rejected and needs another reference/hypothesis upstream.
    """
    c = get_config(config)
    output = {
        "status": "READY",
        "trip_id": trip.get("trip_id"),
        "stops": [],
        "movements": [],
        "stationary": [],
        "reasons": [],
        "s_min": None,
        "s_max": None,
        "construction": "continuous_stop_to_stop",
    }
    for index, row in enumerate(trip.get("scheduled", [])):
        s = row.get("s_m", row.get("s"))
        arrival = row.get("arrival_t", row.get("t"))
        departure = row.get("departure_t", row.get("t"))
        if not all(_finite(value) for value in (s, arrival, departure)):
            output["reasons"].append("MISSING_OR_NONFINITE_SCHEDULE_COORDINATE")
            continue
        if departure < arrival:
            output["reasons"].append("DEPARTURE_PRECEDES_ARRIVAL")
        # GTFS timepoint defaults to 1 when the field is omitted/empty.
        timepoint = row.get("timepoint") in (None, "", 1, "1", True)
        output["stops"].append(
            {
                "index": index,
                "stop_id": row.get("stop_id"),
                "stop_sequence": row.get("stop_sequence", index),
                "s_m": float(s),
                "arrival_t": float(arrival),
                "departure_t": float(departure),
                "timepoint": timepoint,
                "uncertainty_s": c["continuous_timepoint_uncertainty_s"]
                if timepoint
                else c["continuous_nontimepoint_uncertainty_s"],
            }
        )
    stops = output["stops"]
    if not stops:
        output["reasons"].append("EMPTY_SCHEDULE")
    for previous, current in zip(stops, stops[1:], strict=False):
        ds = current["s_m"] - previous["s_m"]
        dt = current["arrival_t"] - previous["departure_t"]
        if ds < -c["continuous_progress_epsilon_m"]:
            output["reasons"].append("NON_MONOTONE_CANONICAL_PROGRESS")
        elif ds < 0:
            # Only sub-millimetre floating-point noise may be snapped.
            current["s_m"] = previous["s_m"]
            ds = 0.0
        if dt < 0:
            output["reasons"].append("NON_MONOTONE_SCHEDULE_TIME")
        if ds > 0 and dt <= 0:
            output["reasons"].append("NON_POSITIVE_MOVEMENT_DURATION")
        segment = {
            "start_s_m": previous["s_m"],
            "end_s_m": current["s_m"],
            "start_t": previous["departure_t"],
            "end_t": current["arrival_t"],
            "start_stop_index": previous["index"],
            "end_stop_index": current["index"],
            "start_uncertainty_s": previous["uncertainty_s"],
            "end_uncertainty_s": current["uncertainty_s"],
        }
        if ds > 0 and dt > 0:
            output["movements"].append(segment)
        elif ds == 0 and dt >= 0:
            output["stationary"].append(segment)
    output["reasons"] = sorted(set(output["reasons"]))
    if output["reasons"]:
        output["status"] = "INVALID_SCHEDULE"
    if stops:
        output.update(s_min=stops[0]["s_m"], s_max=stops[-1]["s_m"])
    output["stop_progress"] = [stop["s_m"] for stop in stops]
    output["movement_starts"] = [row["start_s_m"] for row in output["movements"]]
    return output


def passage_at(schedule, s_m, config=None):
    """Return interval-valued scheduled passage via O(log n) progress lookup."""
    c = get_config(config)
    result = {
        "status": "OUTSIDE_PATTERN",
        "intervals": [],
        "uncertainty_s": None,
        "stop_ids": [],
        "reasons": [],
        "s_m": s_m,
    }
    if schedule.get("status") != "READY":
        result.update(status="INVALID_SCHEDULE", reasons=schedule.get("reasons", []))
        return result
    if not _finite(s_m):
        result.update(
            status="INVALID_PROGRESS", reasons=["NONFINITE_OBSERVED_PROGRESS"]
        )
        return result
    epsilon = c["continuous_stop_tolerance_m"]
    if s_m < schedule["s_min"] - epsilon or s_m > schedule["s_max"] + epsilon:
        result["reasons"] = ["OUTSIDE_SCHEDULE_PROGRESS_RANGE"]
        return result
    progress = schedule["stop_progress"]
    lower, upper = (
        bisect_left(progress, s_m - epsilon),
        bisect_right(progress, s_m + epsilon),
    )
    if upper > lower:
        stops = schedule["stops"][lower:upper]
        # Preserve separate visits at nearby but genuinely different progress.
        # Consecutive identical-progress stops cover the intervening dwell too.
        intervals = []
        previous_s = None
        for stop in stops:
            interval = (stop["arrival_t"], stop["departure_t"])
            if intervals and stop["s_m"] == previous_s:
                intervals[-1] = (intervals[-1][0], max(intervals[-1][1], interval[1]))
            else:
                intervals.append(interval)
            previous_s = stop["s_m"]
        # For actual repeated s this is one continuous stationary interval.
        if all(stop["s_m"] == stops[0]["s_m"] for stop in stops):
            intervals = [(stops[0]["arrival_t"], stops[-1]["departure_t"])]
        result.update(
            status="SUPPORTED" if len(intervals) == 1 else "AMBIGUOUS_PASSAGE",
            intervals=intervals,
            kind="dwell",
            stop_ids=[x["stop_id"] for x in stops],
            stop_indices=[x["index"] for x in stops],
            uncertainty_s=max(x["uncertainty_s"] for x in stops),
        )
        if len(intervals) > 1:
            result["reasons"] = ["MULTIPLE_PASSAGES_WITHIN_PROGRESS_TOLERANCE"]
        return result
    index = bisect_right(schedule["movement_starts"], s_m) - 1
    if index < 0:
        return result
    segment = schedule["movements"][index]
    if not segment["start_s_m"] <= s_m <= segment["end_s_m"]:
        return result
    fraction = (s_m - segment["start_s_m"]) / (
        segment["end_s_m"] - segment["start_s_m"]
    )
    time = segment["start_t"] + fraction * (segment["end_t"] - segment["start_t"])
    uncertainty = (1 - fraction) * segment["start_uncertainty_s"] + fraction * segment[
        "end_uncertainty_s"
    ]
    uncertainty += (
        c["continuous_interpolation_uncertainty_s"] * 4 * fraction * (1 - fraction)
    )
    result.update(
        status="SUPPORTED",
        kind="movement",
        intervals=[(time, time)],
        uncertainty_s=uncertainty,
        segment_index=index,
        start_stop_index=segment["start_stop_index"],
        end_stop_index=segment["end_stop_index"],
    )
    return result


def residual_at(schedule, s_m, timestamp, config=None):
    """An observed dwell has r in [t-departure, t-arrival], not a chosen midpoint.

    Multiple possible passages remain explicit. The orchestrator must preserve
    hypotheses or abstain; silently spanning two separate intervals would make
    unscheduled intermediate times appear supported.
    """
    result = passage_at(schedule, s_m, config)
    if not _finite(timestamp):
        return dict(result, status="INVALID_TIME", reasons=["NONFINITE_OBSERVED_TIME"])
    residuals = [
        (timestamp - upper, timestamp - lower) for lower, upper in result["intervals"]
    ]
    result["residual_intervals"] = residuals
    if len(residuals) == 1:
        result["residual_lower_s"], result["residual_upper_s"] = residuals[0]
    return result
