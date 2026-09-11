"""Pattern-first, local-passage occurrence retrieval without schedule sampling."""

import collections
import statistics

from ..retrieval import for_reference, trip_key
from ..windows import observation_key
from .evidence import coverage_metrics, valid_projection
from .schedule import build_schedule, passage_at


def pattern_key(trip):
    return tuple(
        str(trip.get(k, ""))
        for k in (
            "feed_version",
            "route_id",
            "direction_id",
            "family_id",
            "pattern_id",
            "reference_id",
        )
    )


class SchedulePool:
    def __init__(self, trips, config):
        self.trips = {trip_key(t): t for t in trips}
        if len(self.trips) != len(trips):
            raise ValueError("Duplicate scheduled occurrence identity")
        self.config = config
        self.schedules = {
            key: build_schedule(t, config) for key, t in self.trips.items()
        }
        self.patterns = collections.defaultdict(list)
        for key, trip in self.trips.items():
            self.patterns[pattern_key(trip)].append(key)
        self._passage_cache = collections.OrderedDict()

    def passages(self, trip, progress):
        key = pattern_key(trip), float(progress)
        if key not in self._passage_cache:
            values = []
            for identity in self.patterns[key[0]]:
                p = passage_at(self.schedules[identity], progress, self.config)
                intervals = p.get("intervals", [])
                if p["status"] == "SUPPORTED" and intervals:
                    values.append((sum(intervals[0]) / 2, identity))
            self._passage_cache[key] = sorted(values)
            if len(self._passage_cache) > 4096:
                self._passage_cache.popitem(last=False)
        return self._passage_cache[key]

    def headway(self, trip, progress):
        values = self.passages(trip, progress)
        identity = trip_key(trip)
        i = next((i for i, (_, key) in enumerate(values) if key == identity), None)
        gaps = []
        if i is not None:
            if i > 0:
                gaps.append(values[i][0] - values[i - 1][0])
            if i + 1 < len(values):
                gaps.append(values[i + 1][0] - values[i][0])
        gaps = [g for g in gaps if g > 0]
        value = (
            statistics.median(gaps)
            if gaps
            else self.config["continuous_headway_default_s"]
        )
        return max(
            self.config["continuous_headway_min_s"],
            min(self.config["continuous_headway_max_s"], value),
        )


_POOLS = {}


def schedule_pool(trips, config):
    # Keeps original trips alive; immutable canonical bundle is shared per worker.
    signature = (
        id(trips),
        tuple(
            sorted(
                (k, repr(v)) for k, v in config.items() if k.startswith("continuous_")
            )
        ),
    )
    if signature not in _POOLS:
        if len(_POOLS) >= 4:
            _POOLS.clear()
        _POOLS[signature] = (trips, SchedulePool(trips, config))
    return _POOLS[signature][1]


def retrieve(pool, points, windows, config, *, service_date=None, route_id=None):
    ids = set()
    records = []
    refs = {
        t.get("reference_id")
        for t in pool.trips.values()
        if (route_id is None or str(t.get("route_id")) == str(route_id))
        and (service_date is None or t.get("service_date") == service_date)
    }
    traces = {
        ref: {
            observation_key(r): r
            for r in for_reference(points, ref, include_unprojectable=True)
        }
        for ref in refs
    }
    for w in windows["windows"]:
        anchors = {}
        for ref, trace in traces.items():
            valid = [
                trace[k]
                for k in w["observation_ids"]
                if k in trace and valid_projection(trace[k], config)
            ]
            raw = [trace[k] for k in w["observation_ids"] if k in trace]
            if (
                len(valid) >= config["continuous_min_points"]
                and coverage_metrics(raw, set(), config)["projection"]["duration_s"]
                >= config["continuous_min_duration_s"]
                and max(p["s_m"] for p in valid) - min(p["s_m"] for p in valid)
                >= config["continuous_min_progress_m"]
            ):
                anchor = sorted(valid, key=lambda p: p["t"])[len(valid) // 2]
                anchors[ref] = anchor
        ranked = []
        for key, trip in pool.trips.items():
            anchor = anchors.get(trip.get("reference_id"))
            if (
                anchor is None
                or (route_id is not None and str(trip.get("route_id")) != str(route_id))
                or (
                    service_date is not None
                    and trip.get("service_date") != service_date
                )
            ):
                continue
            passage = passage_at(pool.schedules[key], anchor["s_m"], config)
            if not passage.get("intervals"):
                continue
            distance = min(
                max(lo - anchor["t"], anchor["t"] - hi, 0.0)
                for lo, hi in passage["intervals"]
            )
            headway = pool.headway(trip, anchor["s_m"])
            core = headway * config["continuous_core_headways"]
            rescue = (
                max(
                    core,
                    headway * config["continuous_rescue_headways"],
                    config.get("continuous_rescue_delay_s", 3600.0),
                )
                + config["continuous_clock_uncertainty_s"]
                + (config["continuous_rescue_gap_s"] if w.get("has_gap") else 0)
            )
            if distance <= rescue:
                ranked.append(
                    {
                        "candidate_id": key,
                        "coarse_score": distance / headway,
                        "reference_id": trip["reference_id"],
                        "pattern_id": trip.get("pattern_id"),
                        "local_headway_s": headway,
                        "local_passage_intervals": passage["intervals"],
                        "core_buffer_s": core,
                        "rescue_buffer_s": rescue,
                        "rescue_used": distance > core,
                        "observed_s": anchor["s_m"],
                        "observed_t": anchor["t"],
                    }
                )
        ranked.sort(key=lambda r: (r["coarse_score"], r["candidate_id"]))
        # Preserve top-K for EACH compatible pattern, not only the busy trunk.
        groups = collections.defaultdict(list)
        for row in ranked:
            groups[row["reference_id"], row["pattern_id"]].append(row)
        kept = []
        for group in groups.values():
            best = group[0]["coarse_score"]
            kept.extend(
                row
                for i, row in enumerate(group)
                if i < config["continuous_retrieval_top_k"]
                or row["coarse_score"] <= best + config["continuous_retrieval_delta"]
            )
        ids.update(row["candidate_id"] for row in kept)
        records.append(
            {
                "window_id": w["id"],
                "status": "NO_EVIDENCE"
                if not anchors
                else "EVIDENCE"
                if kept
                else "NO_CANDIDATE",
                "candidates": kept,
                "compatible_reference_count": len(anchors),
                "retrieval_basis": (
                    "compatible pattern then continuous passage at observed progress"
                ),
            }
        )
    return {
        "candidate_ids": sorted(ids),
        "trips": [pool.trips[k] for k in sorted(ids)],
        "windows": records,
        "union_truncated": False,
        "pattern_count": len(pool.patterns),
        "schedule_resampling_used": False,
    }
