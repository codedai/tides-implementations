"""Three locked rounds of assignment over persisted, externally ranked candidates.

Within each round and group, choose a maximum-cardinality strictly monotone
proposal chain. Then minimize the sum of the unchanged 04c candidate scores;
exact ties compare (score, candidate_id, performed_id) in performed order. IDs
are deterministic tie breakers, not numeric quality. No scores or ranks are
recomputed, and no calibration or MATCHED identity decision is implied.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from datetime import date
from fractions import Fraction

VERSION = "experiment05-three-locked-rounds-v1"
MODES = ("global", "early", "middle", "late")
GROUP_FIELDS = ("service_date", "route_id", "direction_id", "gtfs_feed_version")


def _text(value, where, *, numeric=False):
    if numeric and isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{where} must be a nonempty, unpadded identifier")
    return value


def _number(value, where):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        raise ValueError(f"{where} must be a finite number")
    return value


def _group(row, where):
    values = tuple(
        _text(
            row.get(key), f"{where}.{key}", numeric=key in {"route_id", "direction_id"}
        )
        for key in GROUP_FIELDS
    )
    try:
        if date.fromisoformat(values[0]).isoformat() != values[0]:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{where}.service_date must be YYYY-MM-DD") from exc
    return values


def _records(value, name):
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(row, dict) for row in value
    ):
        raise ValueError(f"{name} must be a sequence of object records")
    return value


def _validated_inputs(performed, candidates, scheduled, match_modes):
    if (
        not isinstance(match_modes, (list, tuple))
        or not match_modes
        or any(not isinstance(mode, str) or mode not in MODES for mode in match_modes)
        or len(set(match_modes)) != len(match_modes)
    ):
        raise ValueError("match_modes must contain unique supported decision modes")
    modes = tuple(mode for mode in MODES if mode in match_modes)
    actual, actual_groups = {}, defaultdict(list)
    for row in _records(performed, "performed"):
        identity = _text(row.get("trip_id_performed"), "performed.trip_id_performed")
        if identity in actual:
            raise ValueError(f"Duplicate performed identity: {identity}")
        group = _group(row, identity)
        start = _number(row.get("actual_start_t"), f"{identity}.actual_start_t")
        if row.get("actual_end_t") is not None:
            end = _number(row["actual_end_t"], f"{identity}.actual_end_t")
            if end < start:
                raise ValueError(f"Performed end precedes start: {identity}")
        actual[identity] = {"id": identity, "group": group, "start": start}
        actual_groups[group].append(identity)
    for identities in actual_groups.values():
        identities.sort(key=lambda identity: (actual[identity]["start"], identity))
        for order, identity in enumerate(identities, 1):
            actual[identity]["order"] = order

    schedules, schedule_groups = {}, defaultdict(list)
    for row in _records(scheduled, "scheduled"):
        identity = _text(
            row.get("trip_id_scheduled"), "scheduled.trip_id_scheduled", numeric=True
        )
        group = _group(row, f"schedule:{identity}")
        key = (*group, identity)
        if key in schedules:
            raise ValueError(f"Duplicate authoritative scheduled identity: {key}")
        start = _number(row.get("scheduled_start_s"), f"{key}.scheduled_start_s")
        end = _number(row.get("scheduled_end_s"), f"{key}.scheduled_end_s")
        if start < 0 or end < start:
            raise ValueError(f"Invalid GTFS service-seconds interval: {key}")
        schedules[key] = {"id": identity, "group": group, "start": start, "end": end}
        schedule_groups[group].append(key)
    for keys in schedule_groups.values():
        keys.sort(key=lambda key: (schedules[key]["start"], schedules[key]["id"]))
        for order, key in enumerate(keys, 1):
            schedules[key]["order"] = order

    ranked, seen_candidates = defaultdict(dict), defaultdict(set)
    for row in _records(candidates, "candidates"):
        identity = _text(row.get("trip_id_performed"), "candidate.trip_id_performed")
        if identity not in actual:
            raise ValueError(
                f"Candidate refers to unknown performed identity: {identity}"
            )
        mode = row.get("match_mode")
        if mode not in modes:
            raise ValueError(f"Candidate mode is not requested: {mode}")
        candidate_id = _text(row.get("candidate_id"), "candidate.candidate_id")
        rank = row.get("candidate_rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise ValueError("candidate_rank must be a positive integer")
        cohort = identity, mode
        if rank in ranked[cohort] or candidate_id in seen_candidates[cohort]:
            raise ValueError(f"Duplicate candidate rank or identity: {cohort}")
        score = _number(row.get("score"), f"{candidate_id}.score")
        group = _group(row, candidate_id)
        scheduled_id = row.get("trip_id_scheduled")
        if scheduled_id is not None:
            scheduled_id = _text(
                scheduled_id, "candidate.trip_id_scheduled", numeric=True
            )
        valid = row.get("hard_constraints_valid")
        reasons = row.get("hard_constraint_reasons")
        if (
            not isinstance(valid, bool)
            or not isinstance(reasons, list)
            or any(not isinstance(reason, str) or not reason for reason in reasons)
        ):
            raise ValueError(
                "Candidate hard constraints require a boolean and string list"
            )
        ranked[cohort][rank] = {
            "id": candidate_id,
            "performed_id": identity,
            "mode": mode,
            "rank": rank,
            "score": score,
            "group": group,
            "scheduled_id": scheduled_id,
            "hard_valid": valid,
            "hard_reasons": list(reasons),
        }
        seen_candidates[cohort].add(candidate_id)
    for cohort, rows in ranked.items():
        if sorted(rows) != list(range(1, len(rows) + 1)):
            raise ValueError(f"Candidate ranks must be consecutive from one: {cohort}")
    return modes, actual, actual_groups, schedules, ranked


def _quality(proposal):
    return proposal["score"], proposal["candidate_id"], proposal["trip_id_performed"]


def _chain_key(chain):
    # Exact addition of existing values avoids input-order-dependent rounding.
    return (
        -len(chain),
        sum((Fraction(row["score"]) for row in chain), Fraction()),
        tuple(_quality(row) for row in chain),
    )


def _best_monotone_chain(proposals):
    """Weighted longest increasing subsequence; at most one proposal per actual trip."""
    ordered = sorted(proposals, key=lambda row: row["performed_order"])
    ending, best = [], ()
    for index, row in enumerate(ordered):
        choices = [(row,)]
        for earlier in range(index):
            if ordered[earlier]["scheduled_order"] < row["scheduled_order"]:
                choices.append((*ending[earlier], row))
        chosen = min(choices, key=_chain_key)
        ending.append(chosen)
        if _chain_key(chosen) < _chain_key(best):
            best = chosen
    return best


def _numeric_total(chain):
    total = _chain_key(chain)[1]
    try:
        numeric = float(total)
    except OverflowError:
        numeric = None
    if numeric is not None and not math.isfinite(numeric):
        numeric = None
    return numeric, f"{total.numerator}/{total.denominator}"


def _base(actual, mode):
    return {
        "trip_id_performed": actual["id"],
        **dict(zip(GROUP_FIELDS, actual["group"], strict=True)),
        "match_mode": mode,
        "performed_order": actual["order"],
        "actual_start_t": actual["start"],
    }


def _bounds(actual, locked, ordered_locks):
    index = bisect.bisect_left(ordered_locks, actual["order"])
    lower = locked[ordered_locks[index - 1]] if index else None
    upper = locked[ordered_locks[index]] if index < len(ordered_locks) else None
    return {
        "lower_locked_performed_id": lower["trip_id_performed"] if lower else None,
        "lower_scheduled_order": lower["scheduled_order"] if lower else None,
        "upper_locked_performed_id": upper["trip_id_performed"] if upper else None,
        "upper_scheduled_order": upper["scheduled_order"] if upper else None,
    }


def _proposal(actual, mode, candidate, round_number, schedules, used, bounds):
    reference = schedules.get((*candidate["group"], candidate["scheduled_id"]))
    reference_rejections = []
    if reference is None:
        reference_rejections.append("GTFS_LOOKUP_MISSING")
    reference_rejections.extend(
        f"{field.upper()}_MISMATCH"
        for field, proposed, observed in zip(
            GROUP_FIELDS, candidate["group"], actual["group"], strict=True
        )
        if proposed != observed
    )
    if actual["group"][2].upper() == "UNKNOWN":
        reference_rejections.append("UNKNOWN_PERFORMED_DIRECTION")
    if not candidate["hard_valid"] or candidate["hard_reasons"]:
        reference_rejections.append("CANDIDATE_HARD_CONSTRAINT_FAILED")
    valid_reference = not reference_rejections
    order = reference["order"] if valid_reference else None
    lower, upper = bounds["lower_scheduled_order"], bounds["upper_scheduled_order"]
    lower_pass = order is not None and (lower is None or order > lower)
    upper_pass = order is not None and (upper is None or order < upper)
    used_by = used.get(candidate["scheduled_id"]) if valid_reference else None
    reasons = []
    if not valid_reference:
        reasons.append("INVALID_GTFS_REFERENCE")
    if used_by is not None:
        reasons.append("CANDIDATE_ALREADY_USED")
    if order is not None and not lower_pass:
        reasons.append("VIOLATED_LOWER_ORDER_BOUND")
    if order is not None and not upper_pass:
        reasons.append("VIOLATED_UPPER_ORDER_BOUND")
    return {
        **_base(actual, mode),
        "event": "proposal",
        "round": round_number,
        "candidate_id": candidate["id"],
        "trip_id_scheduled": candidate["scheduled_id"],
        "candidate_rank": candidate["rank"],
        "score": candidate["score"],
        "quality_tuple": [candidate["score"], candidate["id"], actual["id"]],
        "candidate_group": list(candidate["group"]),
        "hard_constraints_valid": candidate["hard_valid"],
        "hard_constraint_reasons": candidate["hard_reasons"],
        "reference_exists": reference is not None,
        "reference_group_matches": candidate["group"] == actual["group"],
        "reference_valid": valid_reference,
        "reference_rejection_reasons": reference_rejections,
        "scheduled_start_s": reference["start"] if reference else None,
        "scheduled_end_s": reference["end"] if reference else None,
        "scheduled_order": order,
        **bounds,
        "lower_bound_passed": lower_pass if order is not None else None,
        "upper_bound_passed": upper_pass if order is not None else None,
        "already_used_by_performed_id": used_by,
        "same_schedule_competitor_ids": [],
        "crossing_selected_performed_ids": [],
        "selected": False,
        "proposal_status": "REJECTED" if reasons else "FEASIBLE",
        "reasons": reasons,
    }


def assign_trips(performed, candidates, scheduled, match_modes):
    """Assign at most ranks 1/2/3 in separate locked rounds for each group/mode.

    Inputs are never modified. Candidate metadata is checked against the
    authoritative scheduled lookup and performed group, rather than supplying
    its own scheduled ordering. Ranks must already be unique and consecutive;
    assignment never sorts candidates into new ranks or applies a score cutoff.
    All output values are JSON-safe, including true null unmatched identities.
    """
    modes, actual, groups, schedules, ranked = _validated_inputs(
        performed, candidates, scheduled, match_modes
    )
    assignments, audit, round_summaries = [], [], []
    for group in sorted(groups):
        identities = groups[group]
        for mode in modes:
            locked, used, failures = {}, {}, defaultdict(list)
            for round_number in (1, 2, 3):
                ordered_locks = sorted(locked)
                proposals = []
                for identity in identities:
                    row = actual[identity]
                    if row["order"] in locked:
                        continue
                    bounds = _bounds(row, locked, ordered_locks)
                    cohort = ranked.get((identity, mode), {})
                    candidate = cohort.get(round_number)
                    if candidate is None:
                        audit.append(
                            {
                                **_base(row, mode),
                                **bounds,
                                "event": "round_no_proposal",
                                "round": round_number,
                                "candidate_rank": round_number,
                                "candidate_id": None,
                                "trip_id_scheduled": None,
                                "selected": False,
                                "proposal_status": "NO_CANDIDATE_AT_RANK",
                                "reasons": [
                                    "TOP3_EXHAUSTED" if cohort else "NO_CANDIDATES"
                                ],
                            }
                        )
                        continue
                    proposal = _proposal(
                        row, mode, candidate, round_number, schedules, used, bounds
                    )
                    audit.append(proposal)
                    proposals.append(proposal)
                feasible = [row for row in proposals if not row["reasons"]]
                competitors = defaultdict(list)
                for row in feasible:
                    competitors[row["scheduled_order"]].append(row["trip_id_performed"])
                for row in feasible:
                    row["same_schedule_competitor_ids"] = [
                        identity
                        for identity in competitors[row["scheduled_order"]]
                        if identity != row["trip_id_performed"]
                    ]
                selected = _best_monotone_chain(feasible)
                selected_ids = {row["trip_id_performed"] for row in selected}
                selected_schedules = {row["scheduled_order"] for row in selected}
                for row in proposals:
                    identity = row["trip_id_performed"]
                    if identity in selected_ids:
                        row.update(selected=True, proposal_status="LOCKED")
                        locked[row["performed_order"]] = row
                        used[row["trip_id_scheduled"]] = identity
                    elif not row["reasons"]:
                        if row["scheduled_order"] in selected_schedules:
                            row["reasons"].append("LOST_SAME_SCHEDULE_CONFLICT")
                        crossing = [
                            chosen["trip_id_performed"]
                            for chosen in selected
                            if (row["performed_order"] - chosen["performed_order"])
                            * (row["scheduled_order"] - chosen["scheduled_order"])
                            < 0
                        ]
                        row["crossing_selected_performed_ids"] = crossing
                        if crossing:
                            row["reasons"].append("CROSSED_CURRENT_ROUND_ASSIGNMENT")
                        if not row["reasons"]:
                            raise RuntimeError(
                                "Maximum-cardinality chain omitted "
                                "an insertable proposal"
                            )
                        row["proposal_status"] = "REJECTED"
                    failures[identity].extend(row["reasons"])
                total, exact = _numeric_total(selected)
                round_summaries.append(
                    {
                        **dict(zip(GROUP_FIELDS, group, strict=True)),
                        "match_mode": mode,
                        "round": round_number,
                        "proposed_count": len(proposals),
                        "feasible_count": len(feasible),
                        "assigned_count": len(selected),
                        "total_existing_score": total,
                        "total_existing_score_exact": exact,
                        "ordered_quality_tuples": [
                            list(_quality(row)) for row in selected
                        ],
                        "locked_performed_ids": [
                            locked[index]["trip_id_performed"]
                            for index in sorted(locked)
                        ],
                    }
                )
            for identity in identities:
                row = actual[identity]
                selected = locked.get(row["order"])
                cohort = ranked.get((identity, mode), {})
                reasons = []
                if selected is None:
                    reasons = ["TOP3_EXHAUSTED" if cohort else "NO_CANDIDATES"]
                    reasons.extend(failures[identity])
                assignments.append(
                    {
                        **_base(row, mode),
                        "assignment_status": "ASSIGNED" if selected else "UNMATCHED",
                        "trip_id_scheduled": selected["trip_id_scheduled"]
                        if selected
                        else None,
                        "candidate_id": selected["candidate_id"] if selected else None,
                        "candidate_rank": selected["candidate_rank"]
                        if selected
                        else None,
                        "score": selected["score"] if selected else None,
                        "assignment_round": selected["round"] if selected else None,
                        "scheduled_order": selected["scheduled_order"]
                        if selected
                        else None,
                        "scheduled_start_s": selected["scheduled_start_s"]
                        if selected
                        else None,
                        "scheduled_end_s": selected["scheduled_end_s"]
                        if selected
                        else None,
                        "unmatched_reasons": list(dict.fromkeys(reasons)),
                        "unmatched_reason": reasons[0] if reasons else None,
                    }
                )
                for rank, candidate in sorted(cohort.items()):
                    if rank <= 3:
                        continue
                    audit.append(
                        {
                            **_base(row, mode),
                            "event": "candidate_retained_for_audit",
                            "round": None,
                            "candidate_rank": rank,
                            "candidate_id": candidate["id"],
                            "trip_id_scheduled": candidate["scheduled_id"],
                            "score": candidate["score"],
                            "selected": False,
                            "proposal_status": "NOT_PROPOSED",
                            "reasons": ["CANDIDATE_RANK_ABOVE_THREE"],
                        }
                    )
    assigned_count = sum(row["assignment_status"] == "ASSIGNED" for row in assignments)
    return {
        "assignments": assignments,
        "audit": audit,
        "summary": {
            "schema_version": VERSION,
            "match_modes": list(modes),
            "group_fields": [*GROUP_FIELDS[:3], "match_mode", GROUP_FIELDS[3]],
            "order_index_base": 1,
            "performed_count": len(actual),
            "assignment_row_count": len(assignments),
            "assigned_count": assigned_count,
            "unmatched_count": len(assignments) - assigned_count,
            "candidate_count": sum(len(rows) for rows in ranked.values()),
            "maximum_candidate_rank_considered": 3,
            "scores_recomputed": False,
            "ranks_recomputed": False,
            "assignment_is_calibrated_trip_identity": False,
            "objective": (
                "Per group/round: maximize cardinality, minimize exact sum of existing "
                "04c scores, then lexicographic performed-order (score, candidate_id, "
                "trip_id_performed); IDs are deterministic ties, not numeric quality."
            ),
            "rounds": round_summaries,
        },
    }
