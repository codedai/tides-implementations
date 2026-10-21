"""Local APC–AVL evidence, without promoting support into a performed trip.

Episodes are observed near-stop portions of the deduplicated AVL chronology,
not trips or verified stop visits. Interpolated witnesses remain derived
positions referencing two actual measurements. Scores rank experimental
evidence and are not probabilities. Source wall clocks are never corrected.
"""

from __future__ import annotations

import bisect
import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

DEFAULT_CONFIG = {
    "time_tolerance_s": 60.0,
    "radius_m": 100.0,
    "interpolation_gap_s": 90.0,
    "episode_gap_s": 90.0,
    "contradiction_radius_m": 200.0,
    "contemporaneous_s": 10.0,
    "max_speed_kph": 130.0,
    "max_apc_dwell_s": 1800.0,
    "accept_score": 650.0,
    "null_score": 550.0,
    "margin_score": 60.0,
    "max_episodes": 64,
    "max_measurements_per_record": 4096,
    "route_policy": "flag",
}
VERSION = "direct-evidence-v2"


def _config(config):
    cfg = DEFAULT_CONFIG | (config or {})
    for key in (
        "time_tolerance_s",
        "radius_m",
        "interpolation_gap_s",
        "episode_gap_s",
        "contradiction_radius_m",
        "max_speed_kph",
        "max_apc_dwell_s",
    ):
        if not math.isfinite(cfg[key]) or cfg[key] <= 0:
            raise ValueError(f"Positive finite configuration required: {key}")
    if cfg["route_policy"] not in ("flag", "abstain_conflict"):
        raise ValueError("Unknown route policy")
    return cfg


def seconds(value):
    return (value - datetime(1970, 1, 1)).total_seconds()


def distance_m(a, b):
    coords = (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon"))
    if any(v is None or not math.isfinite(v) for v in coords):
        return None
    lat0, lon0, lat1, lon1 = coords
    r = math.pi / 180
    return 6371008.8 * math.hypot(
        (lat1 - lat0) * r,
        (lon1 - lon0) * r * math.cos((lat0 + lat1) * r / 2),
    )


def _valid_position(row):
    lat, lon = row.get("lat"), row.get("lon")
    return (
        lat is not None
        and lon is not None
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
        and (lat, lon) != (0, 0)
    )


def _valid_time(row, field):
    value = row.get(field)
    return (
        isinstance(value, datetime)
        and value.tzinfo is None
        and not any(
            row.get(k) for k in ("invalid", "time_ambiguous", "time_nonexistent")
        )
    )


def _key(value):
    return hashlib.sha256(repr(value).encode()).hexdigest()[:24]


@dataclass
class AVLIndex:
    vehicle: str | None
    groups: list[dict]
    times: list[float]
    source_ids: frozenset[str]
    coverage_start: float | None
    coverage_end: float | None
    config: dict
    diagnostics: dict


class PreparedCandidates(dict):
    """Cache is bound to its source index, relevant APC inputs and configuration."""

    def __init__(self, index, apc, config):
        super().__init__()
        self.index = index
        self.config = dict(config)
        self.fingerprints = _fingerprints(apc)


def _fingerprints(apc):
    fields = (
        "vehicle",
        "arrival",
        "departure",
        "lat",
        "lon",
        "route",
        "stop",
        "direction",
        "branch",
        "invalid",
        "time_ambiguous",
        "time_nonexistent",
    )
    return {row["rid"]: tuple(row.get(k) for k in fields) for row in apc}


def build_index(avl, config=None):
    """Index one vehicle; duplicate payloads never add measurement votes."""
    cfg = _config(config)
    vehicles = {r.get("vehicle") for r in avl}
    if len(vehicles) > 1 or (avl and None in vehicles):
        raise ValueError("build_index requires one non-null source vehicle")
    vehicle = next(iter(vehicles), None)
    ids = [r["rid"] for r in avl]
    if len(set(ids)) != len(ids):
        raise ValueError("Repeated AVL source row identity")
    by_time = defaultdict(list)
    unlocated = 0
    for row in avl:
        t = row.get("time")
        if isinstance(t, datetime) and t.tzinfo is None:
            by_time[seconds(t)].append(row)
        else:
            unlocated += 1
    groups = []
    for t, rows in sorted(by_time.items()):
        positions = {(r.get("lat"), r.get("lon")) for r in rows}
        valid = len(positions) == 1 and all(
            _valid_time(r, "time") and _valid_position(r) for r in rows
        )
        representative = min(rows, key=lambda r: r["rid"])
        groups.append(
            {
                "t": t,
                "lat": representative.get("lat"),
                "lon": representative.get("lon"),
                "valid": valid,
                "rid": representative["rid"],
                "rids": tuple(sorted(r["rid"] for r in rows)),
                "routes": tuple(
                    sorted({str(r["route"]) for r in rows if r.get("route")})
                ),
                "key": _key(
                    (vehicle, t, representative.get("lat"), representative.get("lon"))
                ),
                "position_conflict": len(positions) > 1,
                "isolated_spike": False,
            }
        )
    # A two-sided implausible out-and-back is a quality barrier, not a trip cut.
    spikes = []
    for i in range(1, len(groups) - 1):
        p, q, r = groups[i - 1 : i + 2]
        dt0, dt1 = q["t"] - p["t"], r["t"] - q["t"]
        if (
            not all(g["valid"] for g in (p, q, r))
            or max(dt0, dt1) > cfg["interpolation_gap_s"]
        ):
            continue
        if (
            distance_m(p, q) / dt0 * 3.6 > cfg["max_speed_kph"]
            and distance_m(q, r) / dt1 * 3.6 > cfg["max_speed_kph"]
            and distance_m(p, r) <= cfg["radius_m"]
        ):
            spikes.append(i)
    for i in spikes:
        groups[i]["valid"] = False
        groups[i]["isolated_spike"] = True
    covered = [g["t"] for g in groups if g["valid"]]
    return AVLIndex(
        vehicle,
        groups,
        [g["t"] for g in groups],
        frozenset(ids),
        min(covered) if covered else None,
        max(covered) if covered else None,
        cfg,
        {
            "raw_rows": len(avl),
            "distinct_instants": len(groups),
            "duplicate_instant_extra_rows": len(avl) - unlocated - len(groups),
            "unlocated_time_rows": unlocated,
            "conflicting_instants": sum(g["position_conflict"] for g in groups),
            "isolated_spikes": len(spikes),
            "invalid_instants": sum(not g["valid"] for g in groups),
        },
    )


def _point(group, target, label, distance):
    return {
        "kind": "point",
        "ping_ids": [group["rid"]],
        "measurement_keys": [group["key"]],
        "support_time": group["t"],
        "target_time": target,
        "target": label,
        "gap_s": abs(group["t"] - target),
        "distance_m": distance,
        "bracket_gap_s": 0.0,
        "lat": group["lat"],
        "lon": group["lon"],
        "route_labels": list(group["routes"]),
    }


def _interpolation(index, row, target, label, cfg):
    i = bisect.bisect_left(index.times, target)
    if i == 0 or i == len(index.times) or index.times[i] == target:
        return None
    p, q = index.groups[i - 1 : i + 1]
    span = q["t"] - p["t"]
    if (
        not p["valid"]
        or not q["valid"]
        or span > cfg["interpolation_gap_s"]
        or distance_m(p, q) / span * 3.6 > cfg["max_speed_kph"]
    ):
        return None
    fraction = (target - p["t"]) / span
    position = {
        "lat": p["lat"] + fraction * (q["lat"] - p["lat"]),
        "lon": p["lon"] + fraction * (q["lon"] - p["lon"]),
    }
    return (
        i - 1,
        i,
        {
            "kind": "interpolation",
            "ping_ids": [p["rid"], q["rid"]],
            "measurement_keys": [p["key"], q["key"]],
            "support_time": target,
            "target_time": target,
            "target": label,
            "gap_s": 0.0,
            "distance_m": distance_m(position, row),
            "bracket_gap_s": span,
            **position,
            "route_labels": sorted(set(p["routes"]) | set(q["routes"])),
        },
    )


def _overlap_flags(apc):
    flags = {r["rid"]: set() for r in apc}
    active = []
    timed = [
        r for r in apc if _valid_time(r, "arrival") and _valid_time(r, "departure")
    ]
    for row in sorted(timed, key=lambda r: (r["arrival"], r["rid"])):
        a, d = row["arrival"], row["departure"]
        active = [old for old in active if old["departure"] >= a]
        for old in active:
            if old.get("vehicle") != row.get("vehicle"):
                continue
            coincident = (a, d) == (old["arrival"], old["departure"])
            if not coincident and min(d, old["departure"]) <= max(a, old["arrival"]):
                continue
            same = row.get("stop") is not None and row.get("stop") == old.get("stop")
            opposite = (
                row.get("direction") is not None
                and old.get("direction") is not None
                and row["direction"] != old["direction"]
            )
            name = (
                "same_stop_opposite_direction_overlap"
                if same and opposite
                else "same_stop_overlap"
                if same
                else "different_stop_overlap"
            )
            flags[row["rid"]].add(name)
            flags[old["rid"]].add(name)
            if coincident:
                flags[row["rid"]].add("coincident_record_interval")
                flags[old["rid"]].add("coincident_record_interval")
        active.append(row)
    return flags


def prepare_candidates(apc, index, config=None):
    """Build local episodes/witness alternatives once, independently of chunks."""
    cfg = _config(config or index.config)
    if len({r["rid"] for r in apc}) != len(apc):
        raise ValueError("Repeated APC source row identity")
    overlaps = _overlap_flags(apc)
    prepared = PreparedCandidates(index, apc, cfg)
    pad = max(cfg["time_tolerance_s"], cfg["interpolation_gap_s"])
    for row in apc:
        record = {
            "apc_rid": row["rid"],
            "invalid_reason": None,
            "episodes": [],
            "overlap_flags": sorted(overlaps[row["rid"]]),
            "endpoint_conflicts": [],
            "interval_excursion": False,
            "coverage_start_missing": False,
            "coverage_end_missing": False,
            "search_overflow": False,
            "counter_witnesses": [],
        }
        prepared[row["rid"]] = record
        if not _valid_time(row, "arrival") or not _valid_time(row, "departure"):
            record["invalid_reason"] = "INVALID_OR_UNRESOLVED_TIME"
            continue
        if not _valid_position(row):
            record["invalid_reason"] = "MISSING_OR_INVALID_APC_POSITION"
            continue
        if row.get("vehicle") is None or (
            index.vehicle is not None and row["vehicle"] != index.vehicle
        ):
            record["invalid_reason"] = "VEHICLE_IDENTITY_MISMATCH"
            continue
        a, d = seconds(row["arrival"]), seconds(row["departure"])
        if not 0 <= d - a <= cfg["max_apc_dwell_s"]:
            record["invalid_reason"] = "INVERTED_OR_LONG_INTERVAL"
            continue
        record.update(arrival=a, departure=d)
        if index.coverage_start is None:
            record.update(coverage_start_missing=True, coverage_end_missing=True)
            continue

        def covered(t):
            return index.coverage_start <= t <= index.coverage_end

        record["coverage_start_missing"] = not covered(a)
        record["coverage_end_missing"] = not covered(d)
        left = bisect.bisect_left(index.times, a - pad)
        right = bisect.bisect_right(index.times, d + pad)
        if right - left > cfg["max_measurements_per_record"]:
            record["search_overflow"] = True
            continue
        distances = {i: distance_m(row, index.groups[i]) for i in range(left, right)}
        barrier_prefix = [0]
        for i in range(left, right):
            barrier_prefix.append(
                barrier_prefix[-1]
                + int(
                    not index.groups[i]["valid"]
                    or distances[i] is None
                    or distances[i] > cfg["radius_m"]
                )
            )
        episodes, current, group_episode = [], None, {}
        for i in range(left, right):
            g, dist = index.groups[i], distances[i]
            if g["valid"] and dist is not None:
                if a <= g["t"] <= d and dist > cfg["contradiction_radius_m"]:
                    record["interval_excursion"] = True
                for label, target in (("arrival", a), ("departure", d)):
                    if (
                        abs(g["t"] - target) <= cfg["contemporaneous_s"]
                        and dist > cfg["contradiction_radius_m"]
                    ):
                        record["endpoint_conflicts"].append(label)
                        record["counter_witnesses"].append(
                            _point(g, target, label, dist)
                        )
            if not g["valid"] or dist is None or dist > cfg["radius_m"]:
                current = None
                continue
            if current is None or g["t"] - current["end"] > cfg["episode_gap_s"]:
                current = {
                    "start": g["t"],
                    "end": g["t"],
                    "groups": [],
                    "witnesses": [],
                }
                episodes.append(current)
            current["end"] = g["t"]
            current["groups"].append(i)
            group_episode[i] = current
            for label, target in (("arrival", a), ("departure", d)):
                if covered(target) and abs(g["t"] - target) <= cfg["time_tolerance_s"]:
                    lo = bisect.bisect_left(index.times, min(g["t"], target)) - left
                    hi = bisect.bisect_right(index.times, max(g["t"], target)) - left
                    # A near point cannot bridge over an observed departure or
                    # a timestamped invalid/conflicting measurement.
                    if barrier_prefix[hi] == barrier_prefix[lo]:
                        current["witnesses"].append(_point(g, target, label, dist))
            if a < g["t"] < d:
                current["witnesses"].append(_point(g, g["t"], "interior", dist))
        derived_episodes = {}
        targets = (
            [("arrival", a)]
            if a == d
            else [("arrival", a), ("departure", d), ("interior", (a + d) / 2)]
        )
        for label, target in targets:
            edge = _interpolation(index, row, target, label, cfg)
            if edge is None:
                continue
            p, q, witness = edge
            if (
                label != "interior"
                and witness["distance_m"] > cfg["contradiction_radius_m"]
            ):
                record["counter_witnesses"].append(witness)
            if witness["distance_m"] > cfg["radius_m"]:
                continue
            ep = group_episode.get(p) or group_episode.get(q)
            if ep is None:
                ep = derived_episodes.setdefault(
                    (p, q),
                    {
                        "start": index.times[p],
                        "end": index.times[q],
                        "groups": [],
                        "witnesses": [],
                    },
                )
            ep["witnesses"].append(witness)
        episodes += list(derived_episodes.values())
        for ep in episodes:
            if not ep["witnesses"]:
                continue
            keys = sorted({k for w in ep["witnesses"] for k in w["measurement_keys"]})
            ep["candidate_id"] = "episode-v2-" + _key((row.get("vehicle"), tuple(keys)))
            ep["measurement_count"] = len(keys)
            ep["max_internal_gap_s"] = max(
                (
                    index.times[y] - index.times[x]
                    for x, y in zip(ep["groups"], ep["groups"][1:], strict=False)
                ),
                default=0.0,
            )
            record["episodes"].append(ep)
        record["endpoint_conflicts"] = sorted(set(record["endpoint_conflicts"]))
        record["search_overflow"] = len(record["episodes"]) > cfg["max_episodes"]
    return prepared


def _witness_score(w, cfg):
    return round(
        1000
        - 300 * w["distance_m"] / cfg["radius_m"]
        - 200 * w["gap_s"] / cfg["time_tolerance_s"]
        - 100 * w["bracket_gap_s"] / cfg["interpolation_gap_s"],
        6,
    )


def _route_state(row, witnesses):
    routes = sorted({r for w in witnesses for r in w["route_labels"]})
    if not routes or not row.get("route"):
        return "unknown"
    if len(routes) > 1:
        return "mixed"
    return "concordant" if routes[0] == str(row["route"]) else "conflict"


def _witness_owners(ping_ids, contexts):
    """All aliases of a physical measurement contribute possible contexts."""
    return {owner for pid in ping_ids for owner in contexts.get(pid, {None})}


def _valid_counter(witness, contexts, cfg):
    if witness["kind"] == "point":
        # A later distant observation does not contradict an earlier stop if
        # physically permitted travel can explain that distance. This is a
        # conservative configured motion envelope, not a measured clock shift.
        allowance = cfg["radius_m"] + cfg["max_speed_kph"] / 3.6 * witness["gap_s"]
        return witness["distance_m"] > max(cfg["contradiction_radius_m"], allowance)
    owners = _witness_owners(witness["ping_ids"], contexts)
    return len(owners) == 1 and None not in owners


def _candidate(ep, row, record, mapping, cfg, contexts):
    witnesses = []
    for w in ep["witnesses"]:
        if w["kind"] == "interpolation":
            owners = _witness_owners(w["ping_ids"], contexts)
            if len(owners) != 1 or None in owners:
                continue
        witnesses.append(w)
    if not witnesses:
        return None
    selected = {}
    for w in witnesses:
        rank = (
            _witness_score(w, cfg),
            -w["gap_s"],
            w["kind"] == "point",
            tuple(w["measurement_keys"]),
        )
        label = w["target"]
        if label not in selected or rank > selected[label][0]:
            selected[label] = (rank, w)
    qualified = {k: v for k, v in selected.items() if v[0][0] >= cfg["accept_score"]}
    active = qualified or selected
    chosen = [active[k][1] for k in ("arrival", "departure", "interior") if k in active]
    keys = {k for w in witnesses for k in w["measurement_keys"]}
    instant = record["arrival"] == record["departure"]
    full = (
        not instant
        and "arrival" in qualified
        and "departure" in qualified
        and len(keys) >= 2
        and not record["interval_excursion"]
    )
    if instant and "arrival" in active:
        level, status = "instant", "direct"
        chosen = [active["arrival"][1]]
    elif full:
        level, status = "bounded_interval", "direct"
        # Include a distinct real measurement if the best endpoint witnesses
        # happen to use the same point despite other observed support.
        chosen_keys = {k for w in chosen for k in w["measurement_keys"]}
        if len(chosen_keys) < 2:
            alternative = next(
                w for w in witnesses if set(w["measurement_keys"]) - chosen_keys
            )
            chosen.append(alternative)
    else:
        level, status = (
            ("arrival_only", "partial")
            if "arrival" in active
            else ("departure_only", "partial")
            if "departure" in active
            else ("interior_only", "partial")
        )
    if status == "direct":
        required = [w for w in chosen if w["target"] != "interior"]
    else:
        target = level.removesuffix("_only")
        required = [active[target][1]]
        # The partial label and its score describe the SAME supported anchor.
        chosen = required
    score = min(_witness_score(w, cfg) for w in required)
    pids = sorted({pid for w in chosen for pid in w["ping_ids"]})
    possible_owners = _witness_owners(pids, contexts)
    owners = sorted(possible_owners - {None})
    context = (
        "unmapped"
        if None in possible_owners
        else "single_segment"
        if len(owners) == 1
        else "multiple_segments"
    )
    route_state = _route_state(row, chosen)
    return {
        "candidate_id": ep["candidate_id"],
        "status": status,
        "support_level": level,
        "score": score,
        "distance_m": max(w["distance_m"] for w in required),
        "gap_s": max(w["gap_s"] for w in required),
        "start": ep["start"],
        "end": ep["end"],
        "measurement_count": len(keys),
        "max_internal_gap_s": ep["max_internal_gap_s"],
        "ping_ids": pids,
        "witnesses": chosen,
        "segment_ids": owners,
        "segment_id": owners[0] if context == "single_segment" else None,
        "context_status": context,
        "route_state": route_state,
        "route_conflict": route_state in ("conflict", "mixed"),
    }


def match_direct(apc, index, segmentation, config=None, prepared=None):
    """Select local evidence; performed-trip identity is explicitly unresolved."""
    cfg = _config(config or index.config)
    if prepared is not None and (
        not isinstance(prepared, PreparedCandidates)
        or prepared.index is not index
        or prepared.config != cfg
        or prepared.fingerprints != _fingerprints(apc)
        or len(apc) != len({r["rid"] for r in apc})
    ):
        raise ValueError("Prepared candidates do not match source/index/configuration")
    records = prepare_candidates(apc, index, cfg) if prepared is None else prepared
    mapping = segmentation["ping_segment"]
    contexts = {
        group["rid"]: {mapping.get(rid) for rid in group["rids"]}
        for group in index.groups
    }
    result = []
    for row in apc:
        record = records[row["rid"]]
        counter_witnesses = [
            w for w in record["counter_witnesses"] if _valid_counter(w, contexts, cfg)
        ]
        endpoint_conflicts = sorted({w["target"] for w in counter_witnesses})
        decision = {
            "apc_rid": row["rid"],
            "vehicle": row.get("vehicle"),
            "status": "unmatched",
            "reason": "NO_LOCAL_SUPPORT",
            "support_level": None,
            "support_kind": None,
            "segment_id": None,
            "segment_ids": [],
            "context_status": "no_assignment",
            "ping_ids": [],
            "witnesses": [],
            "score": None,
            "margin": None,
            "distance_m": None,
            "gap_s": None,
            "candidate_id": None,
            "candidate_count": 0,
            "episode_count": 0,
            "candidate_owners": [],
            "candidates": [],
            "route_state": "unknown",
            "route_conflict": False,
            "grain_uncertain": bool(row.get("grain_uncertain")),
            "overlap_flags": record["overlap_flags"],
            "clock_corrected": False,
            "has_local_support": False,
            "endpoint_conflicts": endpoint_conflicts,
            "counter_witnesses": counter_witnesses,
            "interval_excursion": record["interval_excursion"],
            "coverage_start_missing": record["coverage_start_missing"],
            "coverage_end_missing": record["coverage_end_missing"],
            "trip_identity_verified": False,
        }
        result.append(decision)
        if record["invalid_reason"]:
            decision.update(status="ineligible", reason=record["invalid_reason"])
            continue
        if record["search_overflow"]:
            decision.update(status="ambiguous", reason="CANDIDATE_SEARCH_OVERFLOW")
            continue
        candidates = [
            c
            for ep in record["episodes"]
            if (c := _candidate(ep, row, record, mapping, cfg, contexts)) is not None
        ]
        candidates.sort(key=lambda c: (-c["score"], c["candidate_id"]))
        decision.update(
            candidates=candidates,
            candidate_count=len(candidates),
            episode_count=len(candidates),
            candidate_owners=sorted({s for c in candidates for s in c["segment_ids"]}),
            has_local_support=bool(candidates),
        )
        if not candidates:
            if endpoint_conflicts:
                decision.update(
                    status="ambiguous", reason="CONTEMPORANEOUS_GPS_CONTRADICTION"
                )
            continue
        best = candidates[0]
        rival = max(
            cfg["null_score"],
            candidates[1]["score"] if len(candidates) > 1 else cfg["null_score"],
        )
        margin = best["score"] - rival
        decision.update(score=best["score"], margin=margin)
        if endpoint_conflicts:
            decision.update(
                status="ambiguous", reason="CONTEMPORANEOUS_GPS_CONTRADICTION"
            )
        elif best["score"] < cfg["accept_score"] or margin < cfg["margin_score"]:
            decision.update(
                status="ambiguous", reason="COMPETING_EPISODES_OR_INSUFFICIENT_SUPPORT"
            )
        elif cfg["route_policy"] == "abstain_conflict" and best["route_conflict"]:
            decision.update(status="ambiguous", reason="SOURCE_CONTEXT_CONFLICT")
        else:
            decision.update(
                {
                    k: best[k]
                    for k in (
                        "status",
                        "support_level",
                        "segment_id",
                        "segment_ids",
                        "context_status",
                        "ping_ids",
                        "witnesses",
                        "distance_m",
                        "gap_s",
                        "candidate_id",
                        "route_state",
                        "route_conflict",
                    )
                }
            )
            kinds = sorted({w["kind"] for w in best["witnesses"]})
            decision["support_kind"] = kinds[0] if len(kinds) == 1 else "mixed"
            decision["reason"] = (
                "OBSERVED_INTERVAL_EXCURSION_PARTIAL_ONLY"
                if record["interval_excursion"]
                else "LOCAL_ENDPOINT_PAIR_SUPPORT"
                if best["support_level"] == "bounded_interval"
                else "LOCAL_INSTANT_SUPPORT"
                if best["support_level"] == "instant"
                else "PARTIAL_INTERVAL_SUPPORT"
            )
    return result
