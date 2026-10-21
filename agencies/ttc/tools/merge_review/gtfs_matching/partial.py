"""Independent decisions on chronological thirds of safely projected travel.

The partition is an experimental *decision scope*, not a new trip boundary or
an online forecast. Projection has already used the complete source segment.
Raw observations and the supplied global decision are never modified.
"""

from __future__ import annotations

import bisect
import collections
import copy
import math

from .decision import apply_decision_guard
from .integration import match_candidate_segment
from .retrieval import for_reference
from .scoring import qualified_quality
from .windows import get_config, observation_key, usable

MODES = ("global", "early", "middle", "late")
LABELS = {"global": "Global", "early": "Early", "middle": "Middle", "late": "Late"}
BASIS = "valid_projected_travel_distance"
VERSION = "projected-distance-thirds-v1"


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _times(row):
    return [
        row[key]
        for key in ("t", "interval_start", "interval_end")
        if _finite(row.get(key))
    ]


def _distance_profile(rows, config):
    """Never turn rejection gaps, reference changes, or jitter into distance."""
    cumulative, total, previous, high_water = [], 0.0, None, None
    exclusions = collections.Counter()
    references = {}
    increments = {}
    for row in rows:
        distance = 0.0
        if not usable(row, config):
            exclusions["INVALID_PROJECTION"] += 1
            previous, high_water = None, None
        else:
            reference_key = (row.get("reference_id"), row.get("fragment_id"))
            summary = references.setdefault(
                reference_key,
                {
                    "reference_id": reference_key[0],
                    "fragment_id": reference_key[1],
                    "accepted_point_count": 0,
                    "effective_progress_m": 0.0,
                },
            )
            summary["accepted_point_count"] += 1
            reason = None
            if previous is None:
                reason = "VALID_RUN_START"
            elif row.get("reference_id") != previous.get("reference_id"):
                reason = "REFERENCE_CHANGE"
            elif row.get("fragment_id") != previous.get("fragment_id"):
                reason = "FRAGMENT_CHANGE"
            elif row.get("scope_separator_before") or row.get("sequence_restart"):
                reason = "EXPLICIT_SEPARATOR"
            elif not 0 < row["t"] - previous["t"] <= config["window_max_gap_s"]:
                reason = "TIME_GAP_OR_NONINCREASING_TIME"
            if reason:
                exclusions[reason] += 1
                high_water = row["s_m"]
            else:
                distance = max(0.0, row["s_m"] - high_water)
                high_water = max(high_water, row["s_m"])
            total += distance
            summary["effective_progress_m"] += distance
            previous = row
        cumulative.append(total)
        increments[observation_key(row)] = distance
    return {
        "cumulative": cumulative,
        "total": total,
        "increments": increments,
        "references": list(references.values()),
        "excluded_edges": dict(exclusions),
    }


def _in_range(t, selected_range):
    return (
        _finite(t)
        and selected_range["start"] <= t
        and (
            t <= selected_range["end"]
            if selected_range["end_inclusive"]
            else t < selected_range["end"]
        )
    )


def _clip_apc(row, selected_range):
    """Clip evidence clocks only; do not invent points or prorate passenger counts."""
    start, end = row.get("interval_start"), row.get("interval_end")
    interval_valid = _finite(start) and _finite(end) and start <= end
    if interval_valid:
        left, right = (
            max(start, selected_range["start"]),
            min(end, selected_range["end"]),
        )
        if left > right or (left == right and not _in_range(left, selected_range)):
            return None
        result = copy.deepcopy(row)
        result.update(
            t=left,
            interval_start=left,
            interval_end=right,
            arrival=left,
            departure=right,
        )
        result["decision_interval_clip"] = {
            "original_start": start,
            "original_end": end,
            "clipped": left != start or right != end,
            "overlap_fraction": (right - left) / (end - start) if end > start else 1.0,
            "passenger_counts_prorated": False,
        }
        return result
    # Malformed intervals remain diagnostics but cannot leak an endpoint from
    # another third into APC support. Their finite anchor is the only evidence.
    t = row.get("t")
    if not _in_range(t, selected_range):
        return None
    result = copy.deepcopy(row)
    result.update(t=t, interval_start=t, interval_end=t, arrival=t, departure=t)
    result["decision_interval_clip"] = {
        "original_start": start,
        "original_end": end,
        "clipped": True,
        "overlap_fraction": None,
        "passenger_counts_prorated": False,
        "reason": "INVALID_INTERVAL_FINITE_ANCHOR_ONLY",
    }
    return result


def partition_projection(projection, config=None):
    """Return auditable global/third slices without touching source projection.

    Bounds snap forward to real AVL observation times. Ties belong to the later
    third; therefore sparse observations can leave a third empty. No synthesized
    boundary point is inserted. Rejected points inside each range are retained
    for the existing discarded-coverage gates.
    """
    c = get_config(config)
    original = list(projection.get("points", []))
    if len({observation_key(row) for row in original}) != len(original):
        raise ValueError("Duplicate AVL source identity in partial matching input")
    rows = sorted(
        (row for row in original if _finite(row.get("t"))),
        key=lambda row: (row["t"], observation_key(row)),
    )
    untimed = [observation_key(row) for row in original if not _finite(row.get("t"))]
    apc = list(projection.get("apc_evidence", []))
    all_times = [t for row in [*rows, *apc] for t in _times(row)]
    bounds = (min(all_times), max(all_times)) if all_times else (None, None)
    profile = _distance_profile(rows, c)
    total = profile["total"]
    boundaries = []
    if total > 0:
        for fraction in (1 / 3, 2 / 3):
            target = total * fraction
            index = min(
                bisect.bisect_left(profile["cumulative"], target), len(rows) - 1
            )
            boundaries.append(
                {
                    "fraction": fraction,
                    "cumulative_progress_m": target,
                    "snapped_cumulative_progress_m": profile["cumulative"][index],
                    "t": rows[index]["t"],
                    "source_id": observation_key(rows[index]),
                }
            )
    output = {}
    valid_count = sum(usable(row, c) for row in rows)
    references = sorted(
        {row.get("reference_id") for row in rows if row.get("reference_id")}
        | {
            a["reference_id"]
            for row in rows
            for a in row.get("projection_alternatives", [])
            if a.get("reference_id")
        }
    )
    for mode_index, mode in enumerate(MODES):
        if mode == "global":
            ranges = (
                [{"start": bounds[0], "end": bounds[1], "end_inclusive": True}]
                if all_times
                else []
            )
            selected_rows, selected_apc = original, apc
            selected_projection = projection
            target_range = [0.0, total]
        elif total <= 0:
            ranges, selected_rows, selected_apc = [], [], []
            selected_projection = {"points": [], "apc_evidence": []}
            target_range = [0.0, 0.0]
        else:
            third = mode_index - 1
            edges = [bounds[0], boundaries[0]["t"], boundaries[1]["t"], bounds[1]]
            selected_range = {
                "start": edges[third],
                "end": edges[third + 1],
                "end_inclusive": third == 2,
            }
            ranges = [selected_range]
            selected_rows = [
                copy.deepcopy(row)
                for row in rows
                if _in_range(row["t"], selected_range)
            ]
            selected_apc = [
                clipped
                for row in apc
                if (clipped := _clip_apc(row, selected_range)) is not None
            ]
            selected_projection = {
                "points": selected_rows,
                "apc_evidence": selected_apc,
            }
            target_range = [total * third / 3, total * (third + 1) / 3]
        selected_valid = [row for row in selected_rows if usable(row, c)]
        selected_profile = _distance_profile(
            sorted(
                (row for row in selected_rows if _finite(row.get("t"))),
                key=lambda row: (row["t"], observation_key(row)),
            ),
            c,
        )
        qualities = [
            qualified_quality(
                for_reference(selected_rows, ref, include_unprojectable=True), c
            )
            for ref in references
        ]
        sufficient = any(q["status"] == "EVIDENCE" for q in qualities)
        reason = (
            None
            if sufficient
            else (
                "NO_POSITIVE_VALID_PROJECTED_TRAVEL"
                if total <= 0 and mode != "global"
                else "PARTIAL_SELECTION_BELOW_EXISTING_EVIDENCE_GATES"
                if mode != "global"
                else "GLOBAL_SELECTION_BELOW_EXISTING_EVIDENCE_GATES"
            )
        )
        selection = {
            "version": VERSION,
            "mode": mode,
            "label": LABELS[mode],
            "basis": BASIS,
            "status": "EVIDENCE" if sufficient else "NO_EVIDENCE",
            "reason": reason,
            "selected_avl_ids": [observation_key(row) for row in selected_rows],
            "selected_valid_avl_ids": [observation_key(row) for row in selected_valid],
            "selected_apc_ids": [observation_key(row) for row in selected_apc],
            "apc_interval_clips": [
                {
                    "source_id": observation_key(row),
                    "interval_start": row.get("interval_start"),
                    "interval_end": row.get("interval_end"),
                    **row["decision_interval_clip"],
                }
                for row in selected_apc
                if row.get("decision_interval_clip")
            ],
            "time_ranges": ranges,
            "boundaries": copy.deepcopy(boundaries),
            "target_progress_range_m": target_range,
            "effective_progress_m": selected_profile["total"],
            "total_effective_progress_m": total,
            "retained_point_fraction": len(selected_valid) / valid_count
            if valid_count
            else 0.0,
            "retained_progress_fraction": selected_profile["total"] / total
            if total
            else 0.0,
            "selected_avl_count": len(selected_rows),
            "selected_apc_count": len(selected_apc),
            "selected_valid_avl_count": len(selected_valid),
            "original_avl_count": len(original),
            "partition_references": copy.deepcopy(profile["references"]),
            "excluded_length_edges": dict(profile["excluded_edges"]),
            "unassigned_untimed_avl_ids": untimed if mode != "global" else [],
            "invalid_projection_retained_in_denominator": True,
            "boundary_rule": (
                "snap_forward_to_real_avl_time; boundary belongs to later third"
            ),
            "distance_rule": (
                "forward high-water progress within adjacent valid same-reference "
                "fragments; no gap/rejection jumps"
            ),
            "projection_uses_full_segment": True,
            "online_causal_decision": False,
            "candidate_reference_locked_by_partition": False,
            "raw_observations_modified": False,
        }
        output[mode] = {"projection": selected_projection, "selection": selection}
    return output


def evaluate_matching_modes(projection, canonical, config=None, global_result=None):
    """Re-retrieve and re-score each third; no global shortlist or delay reuse."""
    output = {}
    for mode, part in partition_projection(projection, config).items():
        if mode == "global" and global_result is not None:
            result = global_result
        else:
            selected = part["projection"]
            result = apply_decision_guard(
                match_candidate_segment(
                    selected,
                    selected.get("apc_evidence", []),
                    canonical["references"],
                    canonical["trips"],
                    config,
                    service_date=None,
                    route_id=canonical["route_id"],
                ),
                config,
            )
            if mode != "global":
                result["diagnostics"] = dict(
                    result.get("diagnostics", {}),
                    decision_mode=mode,
                    matching_scope="selected projected-distance third only",
                    global_candidates_reused=False,
                    global_delay_reused=False,
                    projection_uses_full_segment=True,
                )
                if part["selection"]["reason"]:
                    result["reasons"] = list(
                        dict.fromkeys(
                            [*result.get("reasons", []), part["selection"]["reason"]]
                        )
                    )
        output[mode] = {"result": result, "selection": part["selection"]}
    return output


def validate_matching_modes(projection, modes, config=None):
    """Fail publication if mode scopes overlap, leak sources, or fake confidence."""
    if set(modes) != set(MODES):
        raise ValueError("Four matching modes are required")
    expected = partition_projection(projection, config)
    ownership = collections.Counter()
    for mode in MODES:
        result, selection = modes[mode]["result"], modes[mode]["selection"]
        if selection != expected[mode]["selection"]:
            raise ValueError(f"Invalid or stale {mode} selection metadata")
        if mode != "global":
            ownership.update(selection["selected_avl_ids"])
        allowed = set(selection["selected_avl_ids"] + selection["selected_apc_ids"])
        for window in result.get("windows", {}).get("windows", []):
            if not set(window["observation_ids"]) <= allowed:
                raise ValueError(f"Out-of-scope observation in {mode} window")
        if len(result.get("candidates", [])) > 3:
            raise ValueError(f"Display cap exceeded in {mode}")
        if any(
            row.get("probability") is not None
            for row in result.get("all_candidates", []) + result.get("candidates", [])
        ) and not (
            result.get("scorer") == "continuous_passage_v1"
            and result.get("calibration", {}).get("calibrated") is True
        ):
            raise ValueError(f"Uncalibrated probability in {mode}")
    if any(count != 1 for count in ownership.values()):
        raise ValueError("AVL observation belongs to multiple partial modes")
    if expected["global"]["selection"]["total_effective_progress_m"] > 0:
        timed = {
            observation_key(row)
            for row in projection.get("points", [])
            if _finite(row.get("t"))
        }
        if set(ownership) != timed:
            raise ValueError("Timed AVL missing from partial-mode partition")
    return {
        "status": "PASS",
        "mode_count": len(MODES),
        "partitioned_avl_count": len(ownership),
        "global_result_recomputed": False,
        "source_scope_validated": True,
        "partial_avl_ownership_disjoint": True,
    }
