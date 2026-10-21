"""Compare temporal, spatial and bounded sequence attachment hypotheses.

All coordinates and event counts remain source values. No trip identity,
GPS observation, service date, or complete visit sequence is manufactured.
Similarity thresholds are experimental, not calibrated probabilities.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from datetime import datetime

DEFAULTS = {
    "time_tolerance_s": 60,
    "radius_m": 100,
    "interpolation_gap_s": 90,
    "extension_horizon_s": 900,
    "nearest_baseline_tolerance_s": 300,
    "max_apc_dwell_s": 1800,
    "chain_gap_s": 600,
    "chain_speed_max_kph": 130,
    "anchor_distinct_stops": 3,
    "accept_score": 650,
    "null_score": 550,
    "margin_score": 60,
}


def distance_m(a: dict, b: dict) -> float | None:
    coords = [a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")]
    if any(x is None or not math.isfinite(x) for x in coords):
        return None
    lat0, lon0, lat1, lon1 = coords
    rad = math.pi / 180
    y = (lat1 - lat0) * rad
    x = (lon1 - lon0) * rad * math.cos((lat0 + lat1) / 2 * rad)
    return 6371008.8 * math.hypot(x, y)


def seconds(t: datetime) -> float:
    # Coordinate on the naive wall-time axis, NOT a resolved UTC timestamp.
    return (t - datetime(1970, 1, 1)).total_seconds()


def usable(row: dict, source: str) -> bool:
    t = row.get("time" if source == "avl" else "arrival")
    return (
        t is not None
        and not row.get("invalid")
        and not row.get("time_ambiguous")
        and not row.get("time_nonexistent")
    )


def build_support(avl: list[dict], apc: list[dict]) -> dict[str, list[dict]]:
    """Segment-independent point/short-interpolation candidates, once per vehicle.

    Prefilter includes all configured regimes. Incompatible equal-time GPS
    observations remain distinct point candidates. Interpolation is forbidden
    across duplicate instants or intervening invalid/ambiguous records.
    """
    located = sorted(
        [r for r in avl if r.get("time") is not None],
        key=lambda r: (r["time"], r["rid"]),
    )
    times = [seconds(p["time"]) for p in located]
    # Exact repeated measurements are not independent evidence. Keep all raw
    # identities in source/chunk tables, but one deterministic representative
    # per equal-time equal-coordinate/context measurement for geometry.
    signatures = defaultdict(set)
    bad_instants = set()
    for t, p in zip(times, located, strict=True):
        signatures[t].add((p.get("lat"), p.get("lon")))
        if not usable(p, "avl"):
            bad_instants.add(t)
    conflicts = {t for t, coords in signatures.items() if len(coords) > 1}
    supports = {}
    for row in apc:
        out = []
        supports[row["rid"]] = out
        if not usable(row, "apc") or row.get("departure") is None:
            continue
        a, d = seconds(row["arrival"]), seconds(row["departure"])
        if d < a or d - a > DEFAULTS["max_apc_dwell_s"]:
            continue
        left, right = (
            bisect.bisect_left(times, a - 300),
            bisect.bisect_right(times, d + 300),
        )
        seen = set()
        for i in range(left, right):
            p = located[i]
            identity = (times[i], p.get("lat"), p.get("lon"), p.get("route"))
            if not usable(p, "avl") or times[i] in conflicts or identity in seen:
                continue
            seen.add(identity)
            gap = max(0.0, a - times[i], times[i] - d)
            residual = distance_m(row, p)
            out.append(
                {
                    "ping_ids": [p["rid"]],
                    "kind": "point",
                    "gap_s": gap,
                    "distance_m": residual,
                    "interpolation_gap_s": 0,
                    "route": p.get("route"),
                    "support_time": times[i],
                    "duplicate_instant": False,
                }
            )
        for target in sorted({a, d, (a + d) / 2}):
            i = bisect.bisect_left(times, target)
            if i == 0 or i == len(times) or times[i] == target:
                continue
            p, q = located[i - 1], located[i]
            span = times[i] - times[i - 1]
            if (
                span > 180
                or span <= 0
                or times[i - 1] in conflicts
                or times[i] in conflicts
                or times[i - 1] in bad_instants
                or times[i] in bad_instants
                or not usable(p, "avl")
                or not usable(q, "avl")
                or distance_m(p, q) is None
                or distance_m(p, q) / span * 3.6 > 130
            ):
                continue
            f = (target - times[i - 1]) / span
            position = {
                "lat": p["lat"] + (q["lat"] - p["lat"]) * f,
                "lon": p["lon"] + (q["lon"] - p["lon"]) * f,
            }
            out.append(
                {
                    "ping_ids": [p["rid"], q["rid"]],
                    "kind": "interpolation",
                    "gap_s": 0.0,
                    "distance_m": distance_m(row, position),
                    "interpolation_gap_s": span,
                    "route": p.get("route")
                    if p.get("route") == q.get("route")
                    else None,
                    "support_time": target,
                    "duplicate_instant": False,
                }
            )
    return supports


def _blank(row: dict) -> dict:
    return {
        "apc_rid": row["rid"],
        "segment_id": None,
        "status": "unmatched",
        "reason": "NO_LOCAL_SUPPORT",
        "support_kind": None,
        "ping_ids": [],
        "distance_m": None,
        "gap_s": None,
        "score": None,
        "margin": None,
        "candidate_owners": [],
        "candidate_scores": [],
        "candidate_count": 0,
        "route_conflict": False,
        "boundary_spanning": False,
        "extension_seconds": None,
        "anchor_apc_ids": [],
        "grain_uncertain": bool(row.get("grain_uncertain")),
    }


def direct_match(
    apc: list[dict],
    support: dict,
    segmentation: dict,
    method: str,
    config: dict | None = None,
) -> list[dict]:
    cfg = DEFAULTS | (config or {})
    mapping = segmentation["ping_segment"]
    segments = {s["segment_id"]: s for s in segmentation["segments"]}
    # First observation is a coverage edge, not evidence of a trip boundary.
    boundaries = sorted(seconds(s["start"]) for s in segmentation["segments"])[1:]
    result = []
    for row in apc:
        r = _blank(row)
        result.append(r)
        if not usable(row, "apc") or row.get("departure") is None:
            r.update(status="ineligible", reason="INVALID_OR_AMBIGUOUS_TIME")
            continue
        a, d = seconds(row["arrival"]), seconds(row["departure"])
        if d < a or d - a > cfg["max_apc_dwell_s"]:
            r.update(status="ineligible", reason="INVERTED_OR_LONG_INTERVAL")
            continue
        r["boundary_spanning"] = bisect.bisect_right(
            boundaries, a
        ) < bisect.bisect_right(boundaries, d)
        candidates: dict[str, tuple] = {}
        local_position_evidence = False
        for edge in support.get(row["rid"], []):
            owners = {mapping.get(pid) for pid in edge["ping_ids"]}
            if len(owners) != 1 or None in owners:
                continue
            owner = next(iter(owners))
            if method == "time_only":
                if (
                    edge["kind"] != "point"
                    or edge["gap_s"] > cfg["nearest_baseline_tolerance_s"]
                ):
                    continue
                # Baseline chooses time only; distance is evaluation evidence.
                score = 1000 - edge["gap_s"]
            else:
                segment = segments[owner]
                # A nearby endpoint is not synchronous coverage outside the
                # source span. Such records can only be continuation candidates.
                if (
                    row["arrival"] > segment["end"]
                    or row["departure"] < segment["start"]
                ):
                    continue
                dist = edge["distance_m"]
                if edge["gap_s"] <= cfg["time_tolerance_s"] and dist is not None:
                    local_position_evidence = True
                if (
                    dist is None
                    or dist > cfg["radius_m"]
                    or edge["gap_s"] > cfg["time_tolerance_s"]
                    or edge["interpolation_gap_s"] > cfg["interpolation_gap_s"]
                ):
                    continue
                score = round(
                    1000
                    - 400 * dist / cfg["radius_m"]
                    - 200 * edge["gap_s"] / cfg["time_tolerance_s"]
                    - 100 * edge["interpolation_gap_s"] / cfg["interpolation_gap_s"]
                )
            key = (score, -edge["gap_s"], edge["kind"] == "point")
            if owner not in candidates or key > candidates[owner][0]:
                candidates[owner] = (key, edge)
        ordered = sorted(candidates, key=lambda k: (-candidates[k][0][0], k))
        r["candidate_owners"] = ordered
        r["candidate_scores"] = [candidates[k][0][0] for k in ordered]
        r["candidate_count"] = len(ordered)
        if not ordered:
            if local_position_evidence:
                r["reason"] = "LOCAL_GPS_CONTRADICTION_OR_INSUFFICIENT_SCORE"
            continue
        best = ordered[0]
        score, edge = candidates[best][0][0], candidates[best][1]
        rival = candidates[ordered[1]][0][0] if len(ordered) > 1 else cfg["null_score"]
        margin = score - max(rival, cfg["null_score"])
        r.update(
            score=score,
            margin=margin,
            distance_m=edge["distance_m"],
            gap_s=edge["gap_s"],
        )
        if len(ordered) > 1 and score == candidates[ordered[1]][0][0]:
            r.update(status="ambiguous", reason="EQUAL_OWNER_EVIDENCE")
        elif method != "time_only" and r["boundary_spanning"]:
            r.update(status="ambiguous", reason="INTERVAL_SPANS_CHUNK_BOUNDARY")
        elif method != "time_only" and (
            score < cfg["accept_score"] or margin < cfg["margin_score"]
        ):
            r.update(status="ambiguous", reason="LOW_SCORE_OR_MARGIN")
        else:
            r.update(
                segment_id=best,
                status="temporal_only" if method == "time_only" else "direct",
                reason="TEMPORAL_BASELINE"
                if method == "time_only"
                else "LOCAL_SPATIOTEMPORAL_SUPPORT",
                support_kind=edge["kind"],
                ping_ids=edge["ping_ids"],
                route_conflict=bool(
                    row.get("route")
                    and edge.get("route")
                    and row["route"] != edge["route"]
                ),
            )
    return result


def apc_chains(apc: list[dict], cfg: dict) -> list[list[dict]]:
    """Source structural continuity only; source route/direction are hypotheses."""
    ordered = sorted(apc, key=lambda r: (r.get("arrival") or datetime.min, r["rid"]))
    chains, chain = [], []
    for row in ordered:
        valid = (
            usable(row, "apc")
            and not row.get("grain_uncertain")
            and row.get("departure") is not None
            and row["departure"] >= row["arrival"]
            and (row["departure"] - row["arrival"]).total_seconds()
            <= cfg["max_apc_dwell_s"]
        )
        split = not valid
        if chain and valid:
            prev = chain[-1]
            delta = (row["arrival"] - prev["departure"]).total_seconds()
            dist = distance_m(prev, row)
            split = (
                delta < 0
                or delta > cfg["chain_gap_s"]
                or dist is None
                or dist > 100 + cfg["chain_speed_max_kph"] / 3.6 * max(0, delta)
                or any(
                    row.get(k) != prev.get(k) for k in ("route", "direction", "branch")
                )
            )
            # Revisiting an earlier distinct stop may be a return trip, loop,
            # or duplicate export; all require extra evidence before extension.
            if row.get("stop") != prev.get("stop") and any(
                old.get("stop") == row.get("stop") for old in chain[:-1]
            ):
                split = True
        if split and chain:
            chains.append(chain)
            chain = []
        if valid:
            chain.append(row)
    if chain:
        chains.append(chain)
    return chains


def extend_sequences(
    apc: list[dict], direct: list[dict], segmentation: dict, config: dict | None = None
) -> list[dict]:
    """Bounded, revisionable *inferences*; never relabel missing GPS as direct.

    Only frozen direct anchors contribute. Newly inferred records cannot
    become anchors, preventing unlimited chaining. Competing owners and
    unresolved local support prevent inference. Boundary crossings abstain.
    """
    cfg = DEFAULTS | (config or {})
    result = [dict(r) for r in direct]
    by_id = {r["apc_rid"]: r for r in result}
    direct_by_id = {r["apc_rid"]: r for r in direct if r["status"] == "direct"}
    cuts = sorted(seconds(s["start"]) for s in segmentation["segments"])[1:]
    # Unresolved/contradictory direct evidence is a barrier, not an invisible
    # bridge between a confident anchor and the target tail record.
    chain_input = []
    for source in apc:
        decision = by_id[source["rid"]]
        if (
            decision["status"] in ("ambiguous", "ineligible")
            or decision["reason"] == "LOCAL_GPS_CONTRADICTION_OR_INSUFFICIENT_SCORE"
        ):
            source = dict(source, invalid=True)
        chain_input.append(source)
    for chain in apc_chains(chain_input, cfg):
        anchors = [
            (i, row, direct_by_id[row["rid"]])
            for i, row in enumerate(chain)
            if row["rid"] in direct_by_id and not row.get("grain_uncertain")
        ]
        for row in chain:
            r = by_id[row["rid"]]
            if (
                r["status"] != "unmatched"
                or row.get("grain_uncertain")
                or r["boundary_spanning"]
            ):
                continue
            t = seconds(row["arrival"])
            candidates = defaultdict(list)
            for j, anchor, assignment in anchors:
                at = seconds(anchor["arrival"])
                dt = max(abs(t - at), abs(seconds(row["departure"]) - at))
                if dt > cfg["extension_horizon_s"]:
                    continue
                lo, hi = sorted((t, at))
                # At cut itself is deliberately treated conservatively.
                if bisect.bisect_right(cuts, lo) < bisect.bisect_right(cuts, hi):
                    continue
                candidates[assignment["segment_id"]].append((dt, anchor, j))
            viable = {
                owner: aa
                for owner, aa in candidates.items()
                if len({x[1].get("stop") for x in aa if x[1].get("stop")})
                >= cfg["anchor_distinct_stops"]
            }
            if len(viable) != 1:
                if viable:
                    r.update(status="ambiguous", reason="MULTIPLE_SEQUENCE_OWNERS")
                continue
            owner, aa = next(iter(viable.items()))
            # A rival with fewer anchors remains a rival; absence isn't proof.
            if any(other != owner for other in candidates):
                r.update(status="ambiguous", reason="COMPETING_SEQUENCE_EVIDENCE")
                continue
            aa.sort(key=lambda x: (x[0], x[1]["rid"]))
            used, seen = [], set()
            for delta, anchor, _ in aa:
                if anchor.get("stop") not in seen:
                    seen.add(anchor.get("stop"))
                    used.append((delta, anchor))
                if len(used) == cfg["anchor_distinct_stops"]:
                    break
            # Require actual spatial progression across distinct anchor stops.
            if max(distance_m(row, x[1]) or 0 for x in used) < 50:
                continue
            r.update(
                segment_id=owner,
                status="inferred",
                reason="BOUNDED_APC_SEQUENCE_CONTINUATION",
                support_kind="apc_sequence",
                extension_seconds=used[0][0],
                anchor_apc_ids=[x[1]["rid"] for x in used],
                candidate_owners=[owner],
                candidate_scores=[],
                candidate_count=1,
            )
    return result
