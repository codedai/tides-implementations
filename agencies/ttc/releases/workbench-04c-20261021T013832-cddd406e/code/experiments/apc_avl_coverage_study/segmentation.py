"""Schedule-independent *experimental chunks*, never certified performed trips.

The input is one vehicle's raw, canonical source observations. Naive datetimes
must already have a common clock basis; repeated DST-hour observations must be
flagged ``time_ambiguous`` by the reader. Such observations are preserved as
isolated uncertain chunks. Invalid observations with a known time interrupt
continuity. An invalid observation without a time cannot be placed in the
chronology: its ID is returned separately, requiring downstream abstention from
claims of complete/verified chronology.

APC can refine the ``apc_assisted`` boundaries inside observed AVL coverage; it
cannot manufacture AVL coverage at either end. Passenger counts, GTFS, and raw
source-trip labels are never used as segmentation evidence. All numeric defaults
are experimental starting settings, not TIDES requirements or calibrated values.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left
from datetime import datetime
from typing import Any

ALGORITHM_VERSION = "coverage-segmentation-v1"
METHODS = ("gap_only", "avl_context", "apc_assisted")
# Same deliberately broad diagnostic envelope as raw_audit.py; an agency prior,
# not a TIDES rule and not a worldwide coordinate-validity definition.
TTC_DIAGNOSTIC_ENVELOPE = (43.0, 45.0, -80.6, -78.5)
DEFAULT_CONFIG: dict[str, float | int] = {
    "gap_seconds": 1800,
    "context_min_points": 3,
    "context_min_seconds": 30,
    "boundary_cooldown_seconds": 300,
    "reversal_window_points": 3,
    "reversal_min_displacement_m": 150,
    "reversal_angle_degrees": 135,
    "reversal_min_straightness": 0.8,
    "apc_min_direction_points": 2,
    "apc_anchor_max_gap_seconds": 300,
    "same_time_conflict_distance_m": 100,
}


def _label(record: dict, field: str) -> str:
    value = record.get(field)
    return "" if value is None else str(value).strip()


def _vector(a: dict, b: dict) -> tuple[float, float] | None:
    try:
        lat_a, lat_b = float(a["lat"]), float(b["lat"])
        lon_a, lon_b = float(a["lon"]), float(b["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (lat_a, lat_b, lon_a, lon_b)):
        return None
    if max(abs(lat_a), abs(lat_b)) > 90 or max(abs(lon_a), abs(lon_b)) > 180:
        return None
    return (
        (lon_b - lon_a) * 111_320 * math.cos(math.radians((lat_a + lat_b) / 2)),
        (lat_b - lat_a) * 111_320,
    )


def _distance(a: dict, b: dict) -> float | None:
    vector = _vector(a, b)
    return None if vector is None else math.hypot(*vector)


def _position_uncertain(record: dict) -> bool:
    if _vector(record, record) is None:
        return True
    lat_min, lat_max, lon_min, lon_max = TTC_DIAGNOSTIC_ENVELOPE
    return not (
        lat_min <= float(record["lat"]) <= lat_max
        and lon_min <= float(record["lon"]) <= lon_max
    )


def _runs(records: list[dict], field: str) -> list[tuple[int, int, str]]:
    result = []
    start = 0
    while start < len(records):
        label = _label(records[start], field)
        end = start + 1
        while end < len(records) and _label(records[end], field) == label:
            end += 1
        result.append((start, end, label))
        start = end
    return result


def _context_candidates(pings: list[dict], cfg: dict) -> dict[int, list[dict]]:
    candidates: dict[int, list[dict]] = {}
    minimum = int(cfg["context_min_points"])
    for field in ("route", "run", "destination"):
        runs = _runs(pings, field)
        stable = None
        for c, d, new in runs:
            if not new:
                stable = None
                continue
            after_span = (pings[d - 1]["time"] - pings[c]["time"]).total_seconds()
            if d - c < minimum or after_span < cfg["context_min_seconds"]:
                continue
            if stable is not None:
                a, b, old = stable
                if old != new:
                    candidates.setdefault(c, []).append(
                        {
                            "field": field,
                            "before": old,
                            "after": new,
                            "before_points": b - a,
                            "after_points": d - c,
                            "before_start_rid": str(pings[a]["rid"]),
                            "before_end_rid": str(pings[b - 1]["rid"]),
                            "after_start_rid": str(pings[c]["rid"]),
                            "after_end_rid": str(pings[d - 1]["rid"]),
                        }
                    )
            stable = (c, d, new)
    return candidates


def _reversal(pings: list[dict], index: int, cfg: dict) -> dict | None:
    window = int(cfg["reversal_window_points"])
    if index < window or index + window > len(pings):
        return None
    incoming = pings[index - window : index]
    outgoing = pings[index : index + window]
    vectors = (_vector(incoming[0], incoming[-1]), _vector(outgoing[0], outgoing[-1]))
    if any(v is None for v in vectors):
        return None
    first, second = vectors
    lengths = (math.hypot(*first), math.hypot(*second))
    if min(lengths) < cfg["reversal_min_displacement_m"]:
        return None
    cosine = sum(x * y for x, y in zip(first, second, strict=True)) / math.prod(lengths)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
    if angle < cfg["reversal_angle_degrees"]:
        return None
    for group, displacement in zip((incoming, outgoing), lengths, strict=True):
        legs = [_distance(a, b) for a, b in zip(group, group[1:], strict=False)]
        if any(value is None for value in legs):
            return None
        path_length = sum(legs)
        if (
            path_length <= 0
            or displacement / path_length < cfg["reversal_min_straightness"]
        ):
            return None
    return {
        "angle_degrees": round(angle, 2),
        "incoming_displacement_m": round(lengths[0], 2),
        "outgoing_displacement_m": round(lengths[1], 2),
        "avl_ids": [str(p["rid"]) for p in incoming + outgoing],
    }


def _apc_candidates(pings: list[dict], apc: list[dict], cfg: dict) -> dict:
    """Collect directional changes and two-stop observed retracing, in O(A log N)."""
    # Invalid, uncertain-grain, and ambiguous records interrupt APC evidence.
    # They are not silently filtered into an apparently consecutive stop sequence.
    ordered = sorted(
        (r for r in apc if isinstance(r.get("arrival"), datetime)),
        key=lambda r: (r["arrival"], str(r["rid"])),
    )
    blocks: list[list[dict]] = []
    block: list[dict] = []
    for record in ordered:
        bad = any(
            record.get(f, False)
            for f in (
                "invalid",
                "time_ambiguous",
                "time_nonexistent",
                "grain_uncertain",
            )
        ) or _position_uncertain(record)
        if (
            block
            and (record["arrival"] - block[-1]["arrival"]).total_seconds()
            > cfg["gap_seconds"]
        ):
            blocks.append(block)
            block = []
        if bad:
            if block:
                blocks.append(block)
                block = []
            continue
        block.append(record)
    if block:
        blocks.append(block)

    evidence: list[tuple[datetime, dict]] = []
    minimum = int(cfg["apc_min_direction_points"])
    for block in blocks:
        # Consecutive repeated stop reports are one sequence observation. Keep
        # the earliest report, so duplication cannot manufacture persistence.
        stops: list[dict] = []
        for record in block:
            same = (
                stops
                and _label(record, "stop")
                and all(
                    _label(record, field) == _label(stops[-1], field)
                    for field in ("stop", "route", "direction")
                )
            )
            if not same:
                stops.append(record)
        direction_runs = _runs(stops, "direction")
        for before, after in zip(direction_runs, direction_runs[1:], strict=False):
            a, b, old = before
            c, d, new = after
            if not old or not new or min(b - a, d - c) < minimum:
                continue
            route_before = _label(stops[b - 1], "route")
            route_after = _label(stops[c], "route")
            if not route_before or route_before != route_after:
                continue
            evidence.append(
                (
                    stops[c]["arrival"],
                    {
                        "kind": "apc_sustained_direction_change",
                        "before": old,
                        "after": new,
                        "route": route_after,
                        "apc_ids": [
                            str(r["rid"])
                            for r in stops[b - minimum : b] + stops[c : c + minimum]
                        ],
                        "earliest_boundary": stops[b - 1].get("departure")
                        or stops[b - 1]["arrival"],
                        "latest_boundary": stops[c]["arrival"],
                    },
                )
            )
        for index in range(2, len(stops) - 2):
            group = stops[index - 2 : index + 3]
            labels = [_label(r, "stop") for r in group]
            routes = {_label(r, "route") for r in group}
            # A,B,C,B,A is evidence of retracing; A,A or A,B,A alone is not.
            if (
                not all(labels)
                or len(routes) != 1
                or "" in routes
                or len(set(labels[:3])) != 3
                or labels[0] != labels[4]
                or labels[1] != labels[3]
            ):
                continue
            evidence.append(
                (
                    group[3]["arrival"],
                    {
                        "kind": "apc_observed_stop_retracing",
                        "stop_sequence": labels,
                        "apc_ids": [r["rid"] for r in group],
                        "earliest_boundary": group[2].get("departure")
                        or group[2]["arrival"],
                        "latest_boundary": group[3]["arrival"],
                    },
                )
            )

    times = [p["time"] for p in pings]
    candidates: dict[int, list[dict]] = {}
    for instant, item in evidence:
        index = bisect_left(times, instant)
        # No APC-only tail can create or extend an AVL chunk.
        if not 0 < index < len(times):
            continue
        before_gap = (instant - times[index - 1]).total_seconds()
        after_gap = (times[index] - instant).total_seconds()
        if max(before_gap, after_gap) > cfg["apc_anchor_max_gap_seconds"]:
            continue
        candidates.setdefault(index, []).append(item)
    return candidates


def segment_vehicle(
    avl: list[dict], apc: list[dict], method: str, config: dict | None = None
) -> dict[str, Any]:
    """Partition each valid AVL row exactly once into deterministic chunks.

    ``gap_only`` uses temporal discontinuities and source-quality barriers.
    ``avl_context`` adds persistent route/run changes and observed geometric
    reversals; a destination change alone is retained only as supporting evidence.
    ``apc_assisted`` additionally uses persistent APC direction changes or observed
    stop retracing. All three methods retain uncertainty and source lineage.

    Sorting is O(N log N + A log A); subsequent scans are linear for fixed-size
    reversal windows. ``ping_ids``, start/end, and ``ping_segment`` refer strictly
    to observed AVL; merging must separately preserve APC coverage and lineage.
    """
    if method not in METHODS:
        raise ValueError(f"Unknown method {method!r}; expected one of {METHODS}")
    cfg = DEFAULT_CONFIG | (config or {})
    unknown = set(cfg) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"Unknown segmentation configuration keys: {sorted(unknown)}")
    if any(
        not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
        for v in cfg.values()
    ):
        raise ValueError("Configuration values must be finite non-negative numbers")
    for key in (
        "context_min_points",
        "reversal_window_points",
        "apc_min_direction_points",
    ):
        if cfg[key] < 2 or int(cfg[key]) != cfg[key]:
            raise ValueError(f"{key} must be an integer >= 2")

    ids = [str(r["rid"]) for r in avl]
    if len(set(ids)) != len(ids):
        raise ValueError("AVL rid must be a unique source-record identifier")
    vehicles = {_label(r, "vehicle") for r in avl + apc}
    if len(vehicles) > 1:
        raise ValueError("segment_vehicle requires a single canonical vehicle")
    for record in avl:
        instant = record.get("time")
        if isinstance(instant, datetime) and instant.tzinfo is not None:
            raise ValueError(
                "Canonical AVL times must be naive on a common clock basis"
            )

    excluded = sorted(
        str(r["rid"])
        for r in avl
        if r.get("invalid") or not isinstance(r.get("time"), datetime)
    )
    unlocated = sorted(
        str(r["rid"]) for r in avl if not isinstance(r.get("time"), datetime)
    )
    ordered = sorted(
        (r for r in avl if isinstance(r.get("time"), datetime)),
        key=lambda r: (r["time"], str(r["rid"])),
    )
    pings: list[dict] = []
    hard_cuts: dict[int, list[str]] = {}
    pending_invalid = False
    for ping in ordered:
        if ping.get("invalid"):
            pending_invalid = True
            continue
        reasons = []
        if pending_invalid:
            reasons.append("invalid_avl_barrier")
        pending_invalid = False
        if ping.get("time_ambiguous") or (pings and pings[-1].get("time_ambiguous")):
            reasons.append("ambiguous_time_barrier")
        if ping.get("time_nonexistent") or (
            pings and pings[-1].get("time_nonexistent")
        ):
            reasons.append("nonexistent_time_barrier")
        if _position_uncertain(ping) or (pings and _position_uncertain(pings[-1])):
            reasons.append("uncertain_position_barrier")
        if (
            pings
            and (ping["time"] - pings[-1]["time"]).total_seconds() > cfg["gap_seconds"]
        ):
            reasons.append("avl_gap")
        if reasons:
            hard_cuts[len(pings)] = reasons
        pings.append(ping)

    # Contradictory positions at one instant cannot establish a travel order.
    start = 0
    while start < len(pings):
        end = start + 1
        while end < len(pings) and pings[end]["time"] == pings[start]["time"]:
            end += 1
        if end - start > 1:
            distances = [_distance(pings[start], p) for p in pings[start + 1 : end]]
            if any(
                d is not None and d > cfg["same_time_conflict_distance_m"]
                for d in distances
            ):
                for index in range(start, min(end + 1, len(pings))):
                    hard_cuts.setdefault(index, []).append(
                        "same_time_position_conflict"
                    )
        start = end

    result: dict[str, Any] = {
        "method": method,
        "algorithm_version": ALGORITHM_VERSION,
        "segments": [],
        "ping_segment": {},
        "boundaries": [],
        "excluded_ping_ids": excluded,
        "unlocated_invalid_ping_ids": unlocated,
        "chronology_unresolved": bool(unlocated)
        or any(p.get("time_ambiguous", False) for p in pings)
        or any(p.get("time_nonexistent", False) for p in pings)
        or any("same_time_position_conflict" in rs for rs in hard_cuts.values()),
        "config": cfg,
    }
    if not pings:
        return result

    # Evidence is evaluated independently within each uninterrupted AVL block.
    # This prevents context persistence/reversals from borrowing across outages,
    # invalid rows, or either side of an unresolved DST observation.
    cuts = {0: {"reasons": ["first_observation"], "evidence": []}}
    for index, reasons in hard_cuts.items():
        cuts[index] = {"reasons": sorted(set(reasons)), "evidence": []}
    block_starts = sorted(set([0, *hard_cuts, len(pings)]))
    if method != "gap_only":
        # APC candidate collection is once per vehicle; the local anchor and
        # hard-block containment checks below prevent crossing an AVL barrier.
        apc_cuts = _apc_candidates(pings, apc, cfg) if method == "apc_assisted" else {}
        for lo, hi in zip(block_starts, block_starts[1:], strict=False):
            block = pings[lo:hi]
            contexts = _context_candidates(block, cfg)
            last_boundary_time = block[0]["time"]
            for local in range(1, len(block)):
                index = lo + local
                reasons: list[str] = []
                evidence: list[dict] = []
                context = contexts.get(local, [])
                if any(e["field"] in ("route", "run") for e in context):
                    reasons.append("sustained_avl_context_change")
                    evidence.extend(context)
                geometric = _reversal(block, local, cfg)
                if geometric:
                    reasons.append("observed_geometry_reversal")
                    evidence.append({"kind": "avl_geometry", **geometric})
                    evidence.extend(e for e in context if e not in evidence)
                for item in apc_cuts.get(index, []):
                    earliest = item["earliest_boundary"]
                    if (
                        block[0]["time"]
                        <= earliest
                        <= item["latest_boundary"]
                        <= block[-1]["time"]
                    ):
                        reasons.append(item["kind"])
                        evidence.append(item)
                if not reasons:
                    continue
                elapsed = (block[local]["time"] - last_boundary_time).total_seconds()
                if elapsed < cfg["boundary_cooldown_seconds"]:
                    continue
                cuts[index] = {"reasons": sorted(set(reasons)), "evidence": evidence}
                last_boundary_time = block[local]["time"]

    starts = sorted(cuts)
    vehicle = next(iter(vehicles), "")
    config_id = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
    for begin, end in zip(starts, starts[1:] + [len(pings)], strict=True):
        members = pings[begin:end]
        ping_ids = [str(p["rid"]) for p in members]
        payload = json.dumps(
            [ALGORITHM_VERSION, method, config_id, vehicle, ping_ids],
            separators=(",", ":"),
        ).encode()
        segment_id = "chunk-v1-" + hashlib.sha256(payload).hexdigest()[:24]
        cut = cuts[begin]
        ambiguous = any(p.get("time_ambiguous", False) for p in members)
        nonexistent = any(p.get("time_nonexistent", False) for p in members)
        position_uncertain = any(_position_uncertain(p) for p in members)
        conflict = "same_time_position_conflict" in cut["reasons"]
        segment = {
            "segment_id": segment_id,
            "vehicle": vehicle,
            "ping_ids": ping_ids,
            "start": members[0]["time"],
            "end": members[-1]["time"],
            "boundary_before": cut["reasons"],
            "observed_direction_evidence": cut["evidence"],
            "possible_terminal": any(
                "reversal" in r or "retracing" in r or "direction_change" in r
                for r in cut["reasons"]
            ),
            "time_ambiguous": ambiguous,
            "time_nonexistent": nonexistent,
            "position_uncertain": position_uncertain,
            "chronology_unresolved": bool(unlocated)
            or ambiguous
            or nonexistent
            or conflict,
            "experimental_chunk": True,
        }
        result["segments"].append(segment)
        result["ping_segment"].update(dict.fromkeys(ping_ids, segment_id))
        if begin:
            result["boundaries"].append(
                {
                    "at": members[0]["time"],
                    "before_rid": str(pings[begin - 1]["rid"]),
                    "after_rid": ping_ids[0],
                    "reasons": cut["reasons"],
                    "evidence": cut["evidence"],
                }
            )
    return result
