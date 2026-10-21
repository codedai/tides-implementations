"""Optional annotation-only vehicle-service-day scheduled-occurrence decoder.

All transition penalties use actual seconds, metres, headways and occurrence
identity. GTFS blocks, missing trips, extras and substitutions remain soft
hypotheses. This module never overwrites a calibrated per-trip decision: any
eventual sequence-based promotion requires its own independent calibration.
"""

from __future__ import annotations

import math
from collections import defaultdict

DEFAULTS = {
    "sequence_enabled": False,
    "sequence_candidate_limit": 12,
    "sequence_local_score_weight": 1.0,
    "sequence_extra_trip_cost": 1.5,
    "sequence_unknown_transition_cost": 0.15,
    "sequence_delay_change_weight": 0.25,
    "sequence_delay_change_scale_s": 300.0,
    "sequence_delay_rate_s_per_s": 0.1,
    "sequence_schedule_gap_weight": 0.2,
    "sequence_missing_trip_cost": 0.08,
    "sequence_block_change_cost": 0.1,
    "sequence_vehicle_substitution_cost": 0.2,
    "sequence_split_gap_max_s": 1800.0,
    "sequence_split_progress_slack_m": 250.0,
    "sequence_split_observation_cost": 0.05,
    "sequence_repeated_occurrence_cost": 1.5,
    "sequence_terminal_speed_max_mps": 25.0,
    "sequence_terminal_slack_m": 500.0,
    "sequence_terminal_infeasibility_weight": 0.5,
    "sequence_default_headway_s": 600.0,
    "sequence_penalty_cap": 5.0,
}


def _finite(value):
    return (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _number(row, *keys):
    for key in keys:
        if _finite(row.get(key)):
            return float(row[key])
    return None


def _id(candidate):
    return str(candidate.get("candidate_id", candidate.get("trip_id", "")))


def _occurrence(candidate):
    return (
        str(candidate.get("feed_version", "")),
        str(candidate.get("service_date", "")),
        str(candidate.get("trip_id", candidate.get("candidate_id", ""))),
    )


def _huber(value):
    value = abs(value)
    return value * value / 2 if value <= 1 else value - 0.5


def _distance(a, b):
    """Coordinates are [longitude, latitude]; no cross-reference s subtraction."""
    if (
        not isinstance(a, (list, tuple))
        or not isinstance(b, (list, tuple))
        or len(a) != 2
        or len(b) != 2
    ):
        return None
    if not all(_finite(value) for value in [*a, *b]):
        return None
    lon_a, lat_a, lon_b, lat_b = map(math.radians, [*a, *b])
    haversine = (
        math.sin((lat_b - lat_a) / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin((lon_b - lon_a) / 2) ** 2
    )
    return 6371008.8 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, haversine))))


def transition_cost(previous_trip, previous, current_trip, current, config=None):
    """Return independently auditable nonnegative soft transition components."""
    c = DEFAULTS | (config or {})
    components = {
        "unknown_or_extra_cost": 0.0,
        "delay_continuity_cost": 0.0,
        "schedule_gap_cost": 0.0,
        "missing_scheduled_trip_cost": 0.0,
        "block_continuity_cost": 0.0,
        "split_or_repeated_occurrence_cost": 0.0,
        "terminal_reachability_cost": 0.0,
        "vehicle_substitution_cost": 0.0,
    }
    reasons = []
    if previous is None or current is None:
        components["unknown_or_extra_cost"] = c["sequence_unknown_transition_cost"]
        return {
            "cost": sum(components.values()),
            "components": components,
            "reasons": ["UNSCHEDULED_EXTRA_OR_UNKNOWN_STATE"],
        }
    previous_end = _number(previous_trip, "end_time_s", "end_s", "timestamp_end")
    current_start = _number(current_trip, "start_time_s", "start_s", "timestamp_start")
    observed_gap = (
        current_start - previous_end
        if current_start is not None and previous_end is not None
        else None
    )
    gap = max(0.0, observed_gap) if observed_gap is not None else 0.0
    headway = max(
        1.0,
        _number(current, "local_headway_s", "headway_s")
        or _number(current_trip, "local_headway_s")
        or c["sequence_default_headway_s"],
    )
    delay_a = _number(previous, "estimated_delay_s", "delay_median_s")
    delay_b = _number(current, "estimated_delay_s", "delay_median_s")
    if delay_a is not None and delay_b is not None:
        scale = (
            c["sequence_delay_change_scale_s"] + gap * c["sequence_delay_rate_s_per_s"]
        )
        components["delay_continuity_cost"] = c["sequence_delay_change_weight"] * min(
            c["sequence_penalty_cap"], _huber((delay_b - delay_a) / max(1.0, scale))
        )
    scheduled_end = _number(previous, "scheduled_end_time_s", "scheduled_end_s")
    scheduled_start = _number(current, "scheduled_start_time_s", "scheduled_start_s")
    same_occurrence = _occurrence(previous) == _occurrence(current)
    if same_occurrence:
        previous_progress = _number(previous_trip, "end_progress_m")
        current_progress = _number(current_trip, "start_progress_m")
        same_reference = (
            previous.get("reference_id") == current.get("reference_id")
            and previous.get("reference_id") is not None
        )
        progress_continues = (
            same_reference
            and previous_progress is not None
            and current_progress is not None
            and current_progress
            >= previous_progress - c["sequence_split_progress_slack_m"]
        )
        plausible_split = (
            observed_gap is not None
            and 0 <= observed_gap <= c["sequence_split_gap_max_s"]
            and progress_continues
        )
        components["split_or_repeated_occurrence_cost"] = (
            c["sequence_split_observation_cost"]
            if plausible_split
            else c["sequence_repeated_occurrence_cost"]
        )
        reasons.append(
            "SPLIT_OBSERVATIONS_SAME_OCCURRENCE_HYPOTHESIS"
            if plausible_split
            else "REPEATED_OCCURRENCE_SOFT_PENALTY"
        )
    elif (
        scheduled_end is not None
        and scheduled_start is not None
        and observed_gap is not None
    ):
        scheduled_gap = scheduled_start - scheduled_end
        adjusted_gap = scheduled_gap + (delay_b or 0) - (delay_a or 0)
        components["schedule_gap_cost"] = c["sequence_schedule_gap_weight"] * min(
            c["sequence_penalty_cap"], _huber((observed_gap - adjusted_gap) / headway)
        )
        # Count only explicit missing-occurrence hints. A long layover alone
        # cannot identify how many GTFS trips the vehicle should have operated.
        missing_count = _number(current, "missing_scheduled_trip_count")
        if missing_count is not None and missing_count > 0:
            components["missing_scheduled_trip_cost"] = min(
                c["sequence_penalty_cap"],
                missing_count * c["sequence_missing_trip_cost"],
            )
            reasons.append("MISSING_SCHEDULED_OCCURRENCES_SOFT_HINT")
        if scheduled_gap < 0:
            reasons.append("SCHEDULED_OCCURRENCE_OVERLAP_SOFT_PENALTY")
    block_a, block_b = previous.get("block_id"), current.get("block_id")
    if block_a and block_b and block_a != block_b:
        components["block_continuity_cost"] = c["sequence_block_change_cost"]
        reasons.append("GTFS_BLOCK_CHANGE_ALLOWED")
    assigned_a, assigned_b = (
        previous.get("assigned_vehicle_id"),
        current.get("assigned_vehicle_id"),
    )
    if assigned_a and assigned_b and assigned_a != assigned_b:
        components["vehicle_substitution_cost"] = c[
            "sequence_vehicle_substitution_cost"
        ]
        reasons.append("VEHICLE_SUBSTITUTION_SOFT_HYPOTHESIS")
    terminal_distance = _distance(
        previous.get("terminal_end_lonlat"), current.get("terminal_start_lonlat")
    )
    if terminal_distance is not None and observed_gap is not None:
        reachable = (
            gap * c["sequence_terminal_speed_max_mps"] + c["sequence_terminal_slack_m"]
        )
        excess = max(0.0, terminal_distance - reachable)
        components["terminal_reachability_cost"] = c[
            "sequence_terminal_infeasibility_weight"
        ] * min(
            c["sequence_penalty_cap"], excess / max(1.0, c["sequence_terminal_slack_m"])
        )
        if excess:
            reasons.append("TERMINAL_TRAVEL_INFEASIBLE_SOFT_PENALTY")
    return {
        "cost": sum(components.values()),
        "components": components,
        "reasons": reasons,
        "observed_gap_s": observed_gap,
        "local_headway_s": headway,
        "terminal_distance_m": terminal_distance,
    }


def decode_vehicle_day(performed_trips, config=None):
    """Viterbi over candidate occurrences plus an unscheduled/unknown state.

    Input trip fields: case_id, vehicle_id, service_date, start_time_s,
    end_time_s, optional start/end_progress_m and candidates/all_candidates.
    Candidate fields are documented by ``transition_cost``. Missing terminal,
    block or schedule timing metadata are simply absent soft evidence.
    Outputs are annotations; inputs, score components and decisions stay intact.
    """
    c = DEFAULTS | (config or {})
    for key, value in c.items():
        if (
            key in DEFAULTS
            and key != "sequence_enabled"
            and (not _finite(value) or value < 0)
        ):
            raise ValueError(f"Invalid sequence threshold {key}")
    rows = list(performed_trips)
    result = {
        "schema_version": "continuous-vehicle-day-sequence-v1",
        "enabled": bool(c["sequence_enabled"]),
        "annotation_only": True,
        "requires_sequence_recalibration_for_decision_changes": True,
        "best_path": [],
        "candidate_contributions": {},
        "diagnostics": [],
        "groups": [],
    }
    if not c["sequence_enabled"]:
        result["diagnostics"].append("SEQUENCE_DECODER_DISABLED")
        return result
    groups = defaultdict(list)
    for row in rows:
        for key in ("case_id", "vehicle_id", "service_date"):
            if row.get(key) in (None, ""):
                raise ValueError(f"Vehicle-day decoder requires {key}")
        if _number(row, "start_time_s", "start_s", "timestamp_start") is None:
            raise ValueError("Vehicle-day decoder requires finite observed start time")
        groups[
            (
                str(row.get("feed_version", "")),
                str(row["vehicle_id"]),
                str(row["service_date"]),
            )
        ].append(row)
    for group_key, trips in sorted(groups.items()):
        trips = sorted(
            trips,
            key=lambda row: (
                _number(row, "start_time_s", "start_s", "timestamp_start"),
                str(row["case_id"]),
            ),
        )
        if len({str(row["case_id"]) for row in trips}) != len(trips):
            raise ValueError("Duplicate performed case within a vehicle-service-day")
        states, emissions = [], []
        for trip in trips:
            candidates = trip.get("all_candidates", trip.get("candidates", [])) or []
            valid = sorted(
                (
                    item
                    for item in candidates
                    if _id(item) and _finite(item.get("score"))
                ),
                key=lambda item: (item["score"], _id(item)),
            )[: int(c["sequence_candidate_limit"])]
            if len({_id(item) for item in valid}) != len(valid):
                raise ValueError("Duplicate candidate identity within a performed case")
            states.append([*valid, None])
            emissions.append(
                [
                    max(0, item["score"]) * c["sequence_local_score_weight"]
                    for item in valid
                ]
                + [c["sequence_extra_trip_cost"]]
            )
        dp = list(emissions[0])
        history = []
        for index in range(1, len(trips)):
            next_dp, back = [], []
            for current_index, current in enumerate(states[index]):
                transitions = [
                    transition_cost(
                        trips[index - 1], previous, trips[index], current, c
                    )
                    for previous in states[index - 1]
                ]
                chosen = min(
                    range(len(dp)),
                    key=lambda previous_index: (
                        dp[previous_index] + transitions[previous_index]["cost"]
                    ),
                )
                next_dp.append(
                    dp[chosen]
                    + transitions[chosen]["cost"]
                    + emissions[index][current_index]
                )
                back.append(chosen)
            dp = next_dp
            history.append(back)
        last = min(range(len(dp)), key=dp.__getitem__)
        path = [last]
        for back in reversed(history):
            path.append(back[path[-1]])
        path.reverse()
        group_path = []
        for index, (trip, selected) in enumerate(zip(trips, path, strict=True)):
            candidate = states[index][selected]
            previous = states[index - 1][path[index - 1]] if index else None
            current_transition = (
                transition_cost(trips[index - 1], previous, trip, candidate, c)
                if index
                else {"cost": 0.0, "components": {}, "reasons": []}
            )
            entry = {
                "case_id": trip["case_id"],
                "vehicle_id": trip["vehicle_id"],
                "service_date": trip["service_date"],
                "candidate_id": _id(candidate) if candidate else None,
                "state": "SCHEDULED_OCCURRENCE_HYPOTHESIS"
                if candidate
                else "UNSCHEDULED_EXTRA_OR_UNKNOWN",
                "local_score_cost": emissions[index][selected],
                "vehicle_sequence_cost": current_transition["cost"],
                "transition": current_transition,
            }
            group_path.append(entry)
            # Conditional neighbour costs are diagnostics, not independently
            # normalized candidate probabilities and not a marginal posterior.
            key = "|".join((*group_key, str(trip["case_id"])))
            contributions = []
            for alternative in states[index]:
                before = (
                    transition_cost(trips[index - 1], previous, trip, alternative, c)
                    if index
                    else {"cost": 0.0, "components": {}, "reasons": []}
                )
                after = (
                    transition_cost(
                        trip,
                        alternative,
                        trips[index + 1],
                        states[index + 1][path[index + 1]],
                        c,
                    )
                    if index + 1 < len(trips)
                    else {"cost": 0.0, "components": {}, "reasons": []}
                )
                contributions.append(
                    {
                        "candidate_id": _id(alternative) if alternative else None,
                        "vehicle_sequence_cost": before["cost"] + after["cost"],
                        "previous_transition": before,
                        "next_transition": after,
                        "conditioned_on_selected_neighbours": True,
                        "calibrated_probability": None,
                    }
                )
            result["candidate_contributions"][key] = contributions
        result["best_path"].extend(group_path)
        result["groups"].append(
            {
                "feed_version": group_key[0],
                "vehicle_id": group_key[1],
                "service_date": group_key[2],
                "case_count": len(trips),
                "path_cost": min(dp),
            }
        )
    result["diagnostics"].extend(
        [
            "SEQUENCE_PATH_IS_NOT_CALIBRATED_TRIP_IDENTITY",
            "MISSING_FIELDS_PROVIDE_NO_SOFT_EVIDENCE",
            "VEHICLE_SUBSTITUTION_HINTS_REQUIRE_EXTERNAL_PROVENANCE",
        ]
    )
    return result
