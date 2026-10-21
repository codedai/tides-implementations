"""Conservative off-reference diagnostics; labels are hypotheses, not truth."""

from __future__ import annotations

import math

from .canonical import R, finite, valid_coordinate

DEFAULTS = {
    "off_isolated_max_points": 2,
    "off_isolated_max_s": 30.0,
    "off_sustained_s": 60.0,
    "off_max_plausible_speed_mps": 50.0,
    "off_fragment_gap_s": 120.0,
    "off_terminal_distance_m": 250.0,
    "off_deadhead_min_movement_m": 300.0,
}


def distance(a, b):
    if not valid_coordinate(a.get("lat"), a.get("lon")) or not valid_coordinate(
        b.get("lat"), b.get("lon")
    ):
        return None
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    delta = math.sin((lat2 - lat1) / 2) ** 2
    delta += (
        math.cos(lat1)
        * math.cos(lat2)
        * math.sin(math.radians(b["lon"] - a["lon"]) / 2) ** 2
    )
    return 2 * R * math.asin(min(1.0, math.sqrt(delta)))


def _speed(a, b):
    d = distance(a, b)
    if d is None or not finite(a.get("t")) or not finite(b.get("t")):
        return None
    elapsed = b["t"] - a["t"]
    return d / elapsed if elapsed > 0 else math.inf if d else 0.0


def classify_off_reference(points, config=None, apc=None, refs=None):
    """Classify contiguous rejected runs with raw IDs and explicit evidence.

    Only OFF_REFERENCE means every permitted reference failed the distance
    gate. Missing coordinates and sequence rejection are kept distinct.
    """
    cfg = DEFAULTS | (config or {})
    reference_by_id = {r["reference_id"]: r for r in refs or []}
    for key in DEFAULTS:
        if not finite(cfg[key]) or cfg[key] < 0:
            raise ValueError(f"Invalid {key}")
    runs, index = [], 0
    while index < len(points):
        if points[index].get("projection_status") == "ON_REFERENCE":
            index += 1
            continue
        first = index
        while (
            index < len(points)
            and points[index].get("projection_status") != "ON_REFERENCE"
        ):
            index += 1
        group = points[first:index]
        before = points[first - 1] if first else None
        after = points[index] if index < len(points) else None
        instants = [p["t"] for p in group if finite(p.get("t"))]
        duration = max(instants) - min(instants) if instants else None
        bounded_dt = (
            after["t"] - before["t"]
            if before and after and finite(before.get("t")) and finite(after.get("t"))
            else None
        )
        statuses = sorted({p["projection_status"] for p in group})
        movement = [distance(a, b) for a, b in zip(group, group[1:], strict=False)]
        terminal_distances = []
        for neighbor in (before, after):
            if neighbor is not None:
                reference = reference_by_id.get(neighbor.get("reference_id"))
                if reference is not None and finite(neighbor.get("s_m")):
                    length = reference.get("length_m", reference["points"][-1]["s_m"])
                    terminal_distances.append(
                        min(neighbor["s_m"], max(0, length - neighbor["s_m"]))
                    )
        nearby_apc = [
            row
            for row in apc or []
            if instants
            and finite(row.get("t"))
            and min(instants) <= row["t"] <= max(instants)
        ]
        positive_activity = [
            row
            for row in nearby_apc
            if any(finite(row.get(k)) and row[k] > 0 for k in ("boarding", "alighting"))
        ]
        empty_activity = bool(nearby_apc) and all(
            all(
                finite(row.get(k)) and row[k] == 0
                for k in ("boarding", "alighting", "arrival_load", "departure_load")
            )
            for row in nearby_apc
        )
        evidence = {
            "statuses": statuses,
            "bounded_by_on_reference": bool(before and after),
            "observed_duration_s": duration,
            "neighbor_elapsed_s": bounded_dt,
            "same_rejoining_reference": bool(
                before
                and after
                and before.get("reference_id") == after.get("reference_id")
            ),
            "movement_distance_m": sum(d for d in movement if d is not None),
            "before_s_m": before.get("s_m") if before else None,
            "after_s_m": after.get("s_m") if after else None,
            "neighbor_terminal_distances_m": terminal_distances,
            "apc_activity_rows": [
                {
                    k: row.get(k)
                    for k in (
                        "rid",
                        "boarding",
                        "alighting",
                        "arrival_load",
                        "departure_load",
                    )
                }
                for row in nearby_apc
            ],
            "positive_apc_activity_observations": len(positive_activity),
            "apc_empty_activity_observed": empty_activity,
            "no_apc_is_not_evidence_of_empty_bus": True,
        }
        classification = "UNRESOLVED"
        if statuses == ["OFF_REFERENCE"]:
            chain = ([before] if before else []) + group + ([after] if after else [])
            speeds = [_speed(a, b) for a, b in zip(chain, chain[1:], strict=False)]
            impossible = any(
                s is not None and s > cfg["off_max_plausible_speed_mps"] for s in speeds
            )
            evidence["implausible_neighbor_motion"] = impossible
            direct_speed = _speed(before, after) if before and after else None
            isolated = (
                len(group) <= cfg["off_isolated_max_points"]
                and duration is not None
                and duration <= cfg["off_isolated_max_s"]
            )
            if (
                isolated
                and impossible
                and direct_speed is not None
                and direct_speed <= cfg["off_max_plausible_speed_mps"]
            ):
                classification = "GPS_OUTLIER"
            elif any(
                str(p.get("route") or "").strip().upper() in {"PI", "PO"} for p in group
            ):
                classification = "DEADHEAD_CANDIDATE"
                evidence["explicit_pi_po_route"] = True
            elif (
                empty_activity
                and terminal_distances
                and min(terminal_distances) <= cfg["off_terminal_distance_m"]
                and evidence["movement_distance_m"]
                >= cfg["off_deadhead_min_movement_m"]
                and not impossible
            ):
                classification = "DEADHEAD_CANDIDATE"
            elif not before or not after:
                classification = "BOUNDARY_CANDIDATE"
            elif (
                duration is not None
                and duration >= cfg["off_sustained_s"]
                and not impossible
                and evidence["same_rejoining_reference"]
            ):
                classification = "DETOUR_CANDIDATE"
            else:
                classification = "REFERENCE_MISMATCH"
        split = (
            duration is None
            or duration >= cfg["off_sustained_s"]
            or classification not in {"GPS_OUTLIER"}
        )
        runs.append(
            dict(
                id=f"off:{len(runs):04d}",
                source_rids=[
                    dict(
                        source=p.get("source", "avl"),
                        rid=p.get("source_rid", p.get("rid")),
                    )
                    for p in group
                ],
                start=min(instants) if instants else None,
                end=max(instants) if instants else None,
                start_index=first,
                end_index=index - 1,
                duration_s=duration,
                classification=classification,
                hypothesis_only=True,
                confidence="diagnostic_not_verified",
                evidence=evidence,
                splits_fragment=split,
            )
        )
    return runs


def build_fragments(points, runs, config=None):
    """Accepted sequences separated by sustained rejection, ref change or gap."""
    cfg = DEFAULTS | (config or {})
    blocked = {
        i
        for run in runs
        if run["splits_fragment"]
        for i in range(run["start_index"], run["end_index"] + 1)
    }
    fragments, current, previous = [], None, None
    interrupted = False
    for index, point in enumerate(points):
        point["fragment_id"] = None
        if point.get("projection_status") != "ON_REFERENCE":
            interrupted |= index in blocked
            continue
        elapsed = point["t"] - previous["t"] if previous else None
        different = previous is not None and point.get("reference_id") != previous.get(
            "reference_id"
        )
        gap = elapsed is not None and elapsed > cfg["off_fragment_gap_s"]
        separator = point.get("scope_separator_before") or point.get("sequence_restart")
        if current is None or interrupted or different or gap or separator:
            reason = (
                "sequence_start"
                if current is None
                else "scope_separator"
                if separator
                else "off_reference_run"
                if interrupted
                else "reference_change"
                if different
                else "observation_gap"
            )
            current = dict(
                id=f"fragment:{len(fragments):04d}",
                reference_id=point["reference_id"],
                family_id=point.get("family_id"),
                start=point["t"],
                end=point["t"],
                source_rids=[],
                start_s_m=point["s_m"],
                end_s_m=point["s_m"],
                break_reason=reason,
                interpolated_across_rejection=False,
            )
            fragments.append(current)
        current["source_rids"].append(
            dict(
                source=point.get("source", "avl"),
                rid=point.get("source_rid", point.get("rid")),
            )
        )
        current["end"], current["end_s_m"] = point["t"], point["s_m"]
        point["fragment_id"] = current["id"]
        previous, interrupted = point, False
    return fragments
