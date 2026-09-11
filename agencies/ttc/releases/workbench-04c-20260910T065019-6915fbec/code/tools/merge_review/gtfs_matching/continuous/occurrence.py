"""Local competing occurrences conditioned on common observed evidence.

These are schedule-confusability diagnostics, never independent evidence of a
large real delay. Times are on the caller's existing common service clock.
Ordered *observed* stops constrain the class. Conflicting hypothetical schedule
orders, without discriminating observations, remain competing occurrences: a
candidate may not choose its own smaller pairwise-compatible neighborhood.
"""

from __future__ import annotations

import collections
import hashlib
import math
import statistics
from bisect import bisect_left, bisect_right

from ..canonical import make_index, project_candidates
from ..retrieval import trip_key
from .schedule import passage_at

DEFAULTS = {
    "continuous_occurrence_model_enabled": False,
    "continuous_occurrence_min_range_coverage": 0.8,
    "continuous_occurrence_apc_min_confidence": 0.5,
    "continuous_occurrence_stop_ambiguity_confidence_factor": 0.5,
    "continuous_occurrence_stop_padding_m": 5.0,
    "continuous_occurrence_geometry_gate_m": 30.0,
    "continuous_occurrence_geometry_sample_m": 100.0,
    "continuous_occurrence_mapping_ambiguity_m": 75.0,
    "continuous_occurrence_mapping_tie_m": 5.0,
    "continuous_occurrence_time_bin_s": 3600.0,
    "continuous_occurrence_min_gap_samples": 2,
    "continuous_occurrence_gap_outlier_factor": 3.0,
    "continuous_occurrence_default_headway_s": 600.0,
    "continuous_occurrence_min_headway_s": 30.0,
    "continuous_occurrence_tie_s": 1.0,
    "continuous_occurrence_rescue_neighbors": 3,
    "continuous_occurrence_crossing_weight": 1.0,
    "continuous_occurrence_cell_weight": 1.0,
    "continuous_occurrence_cache_size": 4096,
}


def namespace(trip):
    return tuple(
        str(trip.get(k, "")) for k in ("feed_version", "route_id", "direction_id")
    )


def _lerp(nodes, value):
    if not nodes or value < nodes[0][0] - 1e-6 or value > nodes[-1][0] + 1e-6:
        return None
    i = max(0, bisect_right([p[0] for p in nodes], value) - 1)
    if i + 1 == len(nodes):
        return nodes[i][1]
    a, b = nodes[i : i + 2]
    fraction = (value - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
    return a[1] + fraction * (b[1] - a[1])


def _coverage(schedule, bounds):
    lo, hi = bounds
    if schedule["status"] != "READY":
        return 0.0
    if hi == lo:
        return float(schedule["s_min"] <= lo <= schedule["s_max"])
    return max(0.0, min(hi, schedule["s_max"]) - max(lo, schedule["s_min"])) / (hi - lo)


def _stop_sequence(schedule, bounds, padding):
    return [
        str(p["stop_id"])
        for p in schedule["stops"]
        if p.get("stop_id") is not None
        and bounds[0] - padding <= p["s_m"] <= bounds[1] + padding
    ]


def _subsequence(needle, haystack):
    index = 0
    for token in haystack:
        if index < len(needle) and needle[index] == token:
            index += 1
    return index == len(needle)


def _compatible_stops(first, second, observed):
    common = set(first) & set(second)
    return (
        [p for p in first if p in common] == [p for p in second if p in common]
        and _subsequence(observed, first)
        and _subsequence(observed, second)
    )


class OccurrenceIndex:
    def __init__(self, pool, references=None):
        self.pool = pool
        self.config = DEFAULTS | pool.config
        for key, default in DEFAULTS.items():
            value = self.config[key]
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be boolean")
            elif (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or value < 0
                or (isinstance(default, int) and int(value) != value)
            ):
                raise ValueError(f"Invalid occurrence configuration: {key}")
        for key in (
            "geometry_sample_m",
            "time_bin_s",
            "min_gap_samples",
            "default_headway_s",
            "min_headway_s",
            "cache_size",
        ):
            if self.config["continuous_occurrence_" + key] <= 0:
                raise ValueError(f"Occurrence {key} must be positive")
        if not 0 <= self.config["continuous_occurrence_min_range_coverage"] <= 1:
            raise ValueError("Occurrence coverage must be a ratio")
        if (
            not 0
            <= self.config["continuous_occurrence_stop_ambiguity_confidence_factor"]
            <= 1
        ):
            raise ValueError("Occurrence ambiguity confidence factor must be a ratio")
        self.references = {r["reference_id"]: r for r in references or []}
        self.reference_indices = {
            key: make_index(ref) for key, ref in self.references.items()
        }
        self.structures, self.by_namespace = {}, collections.defaultdict(set)
        self.key_structure = {}
        for key, trip in pool.trips.items():
            schedule = pool.schedules[key]
            structural = (
                namespace(trip),
                trip.get("reference_id"),
                schedule["status"],
                tuple((p["stop_id"], p["s_m"]) for p in schedule["stops"]),
            )
            self.structures.setdefault(structural, []).append(key)
            self.by_namespace[namespace(trip)].add(structural)
            self.key_structure[key] = structural
        self.classes = collections.OrderedDict()
        self.class_queries = collections.OrderedDict()
        self.geometry = collections.OrderedDict()
        self.passage_cache = collections.OrderedDict()
        self.context_cache = collections.OrderedDict()
        self.fallback = self._fallback_events()

    def _remember(self, cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self.config["continuous_occurrence_cache_size"]:
            cache.popitem(last=False)
        return value

    def _mapping(self, first, second, bounds):
        key = first, second, bounds
        if key in self.geometry:
            return self.geometry[key]
        if first == second:
            return [(bounds[0], bounds[0]), (bounds[1], bounds[1])]
        if first not in self.references or second not in self.references:
            return None  # Different progress axes cannot be compared by numeric s.
        ref, other = self.references[first], self.references[second]
        points = ref["points"]
        positions = {bounds[0], bounds[1]}
        step = self.config["continuous_occurrence_geometry_sample_m"]
        positions.update(p["s_m"] for p in points if bounds[0] < p["s_m"] < bounds[1])
        positions.update(
            bounds[0] + i * step
            for i in range(1, int((bounds[1] - bounds[0]) / step) + 1)
        )
        latitude = [(p["s_m"], p["lat"]) for p in points]
        longitude = [(p["s_m"], p["lon"]) for p in points]
        nodes = []
        for s in sorted(positions):
            lat, lon = _lerp(latitude, s), _lerp(longitude, s)
            options = project_candidates(
                lat,
                lon,
                other,
                gate=self.config["continuous_occurrence_geometry_gate_m"],
                top_k=4,
                index=self.reference_indices[second],
            )
            if not options:
                return self._remember(self.geometry, key, None)
            best = options[0]
            if any(
                p["lateral_m"]
                <= best["lateral_m"]
                + self.config["continuous_occurrence_mapping_tie_m"]
                and abs(p["s_m"] - best["s_m"])
                > self.config["continuous_occurrence_mapping_ambiguity_m"]
                for p in options[1:]
            ):
                return self._remember(self.geometry, key, None)
            if nodes and best["s_m"] < nodes[-1][1] - 1e-6:
                return self._remember(self.geometry, key, None)
            nodes.append((s, best["s_m"]))
        return self._remember(self.geometry, key, nodes)

    def _class(self, trip, bounds, observed):
        query = namespace(trip), trip["reference_id"], bounds, observed
        cached = self.class_queries.get(query)
        if cached in self.classes:
            self.classes.move_to_end(cached)
            return self.classes[cached]
        c = self.config
        members, mappings, rejected = [], {}, collections.Counter()
        stop_sequences = set()
        for other_structure in sorted(self.by_namespace[namespace(trip)], key=repr):
            keys = self.structures[other_structure]
            other = self.pool.trips[keys[0]]
            mapping = self._mapping(trip["reference_id"], other["reference_id"], bounds)
            if mapping is None:
                rejected["NO_VALIDATED_LOCAL_GEOMETRY_MAPPING"] += len(keys)
                continue
            mapped_bounds = mapping[0][1], mapping[-1][1]
            schedule = self.pool.schedules[keys[0]]
            if (
                _coverage(schedule, mapped_bounds)
                < c["continuous_occurrence_min_range_coverage"]
            ):
                rejected["INSUFFICIENT_OBSERVED_RANGE_COVERAGE"] += len(keys)
                continue
            stops = _stop_sequence(
                schedule, mapped_bounds, c["continuous_occurrence_stop_padding_m"]
            )
            if not _subsequence(observed, stops):
                rejected["OBSERVED_ORDERED_STOP_CONTRADICTION"] += len(keys)
                continue
            members.extend(
                key for key in keys if self.pool.schedules[key]["status"] == "READY"
            )
            mappings[other["reference_id"]] = mapping
            stop_sequences.add(tuple(stops))
        members.sort()
        sequences = sorted(stop_sequences)
        conflicts = sum(
            not _compatible_stops(first, second, ())
            for i, first in enumerate(sequences)
            for second in sequences[i + 1 :]
        )
        identity = (
            "confusability:"
            + hashlib.sha256(
                repr(
                    (namespace(trip), trip["reference_id"], bounds, observed, members)
                ).encode()
            ).hexdigest()[:20]
        )
        result = dict(
            class_id=identity,
            member_ids=members,
            mappings=mappings,
            observed_s_range=list(bounds),
            observed_stop_ids=list(observed),
            reference_id=trip["reference_id"],
            namespace=namespace(trip),
            rejected=dict(rejected),
            pairwise_stop_order_conflicts=conflicts,
            stop_order_ambiguous=bool(conflicts),
        )
        self._remember(self.classes, identity, result)
        self._remember(self.class_queries, query, identity)
        return result

    def passages(self, local, progress):
        cache_key = local["class_id"], float(progress)
        if cache_key in self.passage_cache:
            self.passage_cache.move_to_end(cache_key)
            return self.passage_cache[cache_key]
        values = []
        mapped_by_reference = {
            ref: _lerp(nodes, progress) for ref, nodes in local["mappings"].items()
        }
        for key in local["member_ids"]:
            trip = self.pool.trips[key]
            mapped = mapped_by_reference[trip["reference_id"]]
            if mapped is None:
                continue
            passage = passage_at(self.pool.schedules[key], mapped, self.pool.config)
            if passage["status"] == "SUPPORTED" and len(passage["intervals"]) == 1:
                lo, hi = passage["intervals"][0]
                values.append(
                    dict(
                        candidate_id=key,
                        time=(lo + hi) / 2,
                        interval=[lo, hi],
                        mapped_s_m=mapped,
                    )
                )
        values.sort(key=lambda row: (row["time"], row["candidate_id"]))
        groups = []
        tie = self.config["continuous_occurrence_tie_s"]
        for row in values:
            if not groups or row["time"] - groups[-1]["time"] > tie:
                groups.append(
                    dict(
                        time=row["time"],
                        members=[],
                        lower_t=row["interval"][0],
                        upper_t=row["interval"][1],
                    )
                )
            group = groups[-1]
            group["members"].append(row)
            group["lower_t"] = min(group["lower_t"], row["interval"][0])
            group["upper_t"] = max(group["upper_t"], row["interval"][1])
        result = dict(
            groups=groups,
            times=[g["time"] for g in groups],
            by_id={
                row["candidate_id"]: (index, row)
                for index, group in enumerate(groups)
                for row in group["members"]
            },
        )
        return self._remember(self.passage_cache, cache_key, result)

    def _fallback_events(self):
        stops = collections.defaultdict(list)
        for key, trip in self.pool.trips.items():
            if self.pool.schedules[key]["status"] != "READY":
                continue
            counts = collections.Counter()
            for stop in self.pool.schedules[key]["stops"]:
                token = str(stop["stop_id"])
                counts[token] += 1
                stops[namespace(trip), token, counts[token]].append(
                    ((stop["arrival_t"] + stop["departure_t"]) / 2, key)
                )
        result = collections.defaultdict(list)
        for (ns, _, _), values in stops.items():
            unique = sorted(set(values))
            for first, second in zip(unique, unique[1:], strict=False):
                gap = second[0] - first[0]
                if gap > self.config["continuous_occurrence_tie_s"]:
                    result[ns].append(
                        ((first[0] + second[0]) / 2, gap, first[1], second[1])
                    )
        return result

    def _estimate(self, rows):
        pairs = collections.defaultdict(list)
        for _, gap, first, second in rows:
            pairs[first, second].append(gap)
        gaps = [statistics.median(values) for values in pairs.values()]
        minimum = self.config["continuous_occurrence_min_gap_samples"]
        if len(gaps) < minimum:
            return None
        # Separate service breaks from a genuine low-frequency regime. Two
        # vastly unequal gaps cannot justify an inflated midpoint "headway".
        clusters = []
        for gap in sorted(gaps):
            if (
                not clusters
                or gap
                > clusters[-1][-1]
                * self.config["continuous_occurrence_gap_outlier_factor"]
            ):
                clusters.append([])
            clusters[-1].append(gap)
        kept = max(clusters, key=lambda rows: (len(rows), -statistics.median(rows)))
        return (statistics.median(kept), len(kept)) if len(kept) >= minimum else None

    def context(self, trip, s_range, anchor_s, observed_t, observed_stop_ids=()):
        bounds = tuple(map(float, s_range))
        if (
            len(bounds) != 2
            or not all(math.isfinite(v) for v in (*bounds, anchor_s, observed_t))
            or bounds[1] < bounds[0]
        ):
            raise ValueError("Finite ordered observed range and anchor required")
        if not bounds[0] <= anchor_s <= bounds[1]:
            raise ValueError("Anchor must lie inside the common observed range")
        local = self._class(trip, bounds, tuple(map(str, observed_stop_ids)))
        table = self.passages(local, anchor_s)
        groups = table["groups"]
        cache_key = local["class_id"], float(anchor_s), float(observed_t)
        cached = self.context_cache.get(cache_key)
        if cached is not None:
            base = dict(cached)
            return self._candidate_context(base, trip, table)
        width = self.config["continuous_occurrence_time_bin_s"]
        lower = math.floor(observed_t / width) * width

        def within(t):
            return lower <= t < lower + width

        local_rows = [
            (
                (a["time"] + b["time"]) / 2,
                b["time"] - a["time"],
                a["members"][0]["candidate_id"],
                b["members"][0]["candidate_id"],
            )
            for a, b in zip(groups, groups[1:], strict=False)
        ]
        fallback = self.fallback[namespace(trip)]
        layers = [
            (
                "local_confusability_time_bin",
                [row for row in local_rows if within(row[0])],
                0.9,
            ),
            (
                "route_direction_time_bin",
                [row for row in fallback if within(row[0])],
                0.65,
            ),
            ("route_direction_schedule_pool", fallback, 0.35),
        ]
        headway, count, source, confidence = (
            self.config["continuous_occurrence_default_headway_s"],
            0,
            "configured_default",
            0.1,
        )
        for label, rows, certainty in layers:
            estimate = self._estimate(rows)
            if estimate:
                headway, count = estimate
                source, confidence = label, certainty
                break
        headway = max(self.config["continuous_occurrence_min_headway_s"], headway)
        if local["stop_order_ambiguous"]:
            confidence *= self.config[
                "continuous_occurrence_stop_ambiguity_confidence_factor"
            ]
        result = dict(
            class_id=local["class_id"],
            member_ids=local["member_ids"],
            observed_s_range=list(bounds),
            anchor_s_m=anchor_s,
            observed_t=observed_t,
            local_headway_s=headway,
            headway_source=source,
            headway_confidence=confidence,
            headway_sample_count=count,
            time_bin=[lower, lower + width],
            class_rejections=local["rejected"],
            pairwise_stop_order_conflicts=local["pairwise_stop_order_conflicts"],
            stop_order_ambiguous=local["stop_order_ambiguous"],
            class_definition="common_local_geometry_and_observed_ordered_stop_subsequence",
            active_service_dates=sorted(
                {
                    str(self.pool.trips[key].get("service_date", ""))
                    for key in local["member_ids"]
                }
            ),
            service_date_scope=self.pool.service_date_scope,
            exact_pattern_filter_used=False,
            geometry_basis="shared canonical axis or validated local monotone mapping",
            calibrated=False,
        )
        self._remember(self.context_cache, cache_key, dict(result))
        return self._candidate_context(result, trip, table)

    def _candidate_context(self, result, trip, table):
        location = table["by_id"].get(trip_key(trip))
        result["candidate_compatible"] = location is not None
        result["previous_headway_s"] = result["next_headway_s"] = None
        if location is not None:
            i, row = location
            groups = table["groups"]
            result["previous_headway_s"] = (
                row["time"] - groups[i - 1]["time"] if i else None
            )
            result["next_headway_s"] = (
                groups[i + 1]["time"] - row["time"] if i + 1 < len(groups) else None
            )
        return result

    def evidence(self, trip, progress, observed_t, context):
        local = self.classes.get(context["class_id"])
        if local is None or trip_key(trip) not in local["member_ids"]:
            return dict(
                status="NO_OCCURRENCE_EVIDENCE",
                crossed_occurrences=None,
                outside_cell_s=None,
                displacement_cost=None,
                reasons=["MISSING_LOCAL_CONFUSABILITY_CONTEXT"],
            )
        table = self.passages(local, progress)
        location = table["by_id"].get(trip_key(trip))
        if location is None:
            return dict(
                status="NO_OCCURRENCE_EVIDENCE",
                crossed_occurrences=None,
                outside_cell_s=None,
                displacement_cost=None,
                reasons=["NO_UNAMBIGUOUS_LOCAL_SCHEDULE_PASSAGE"],
            )
        index, row = location
        groups, times = table["groups"], table["times"]
        lo, hi = row["interval"]
        h = context["local_headway_s"]
        previous = groups[index - 1] if index else None
        following = groups[index + 1] if index + 1 < len(groups) else None
        lower = min(lo, (previous["upper_t"] + lo) / 2) if previous else lo - h / 2
        upper = max(hi, (following["lower_t"] + hi) / 2) if following else hi + h / 2
        if lo <= observed_t <= hi:
            crossed = 0
        elif observed_t > hi:
            crossed = max(
                0,
                bisect_right(times, observed_t)
                - max(index + 1, bisect_right(times, hi)),
            )
        else:
            crossed = max(
                0, min(index, bisect_left(times, lo)) - bisect_left(times, observed_t)
            )
        outside = max(lower - observed_t, observed_t - upper, 0.0)
        return dict(
            status="EVIDENCE",
            class_id=local["class_id"],
            crossed_occurrences=crossed,
            outside_cell_s=outside,
            displacement_cost=self.config["continuous_occurrence_crossing_weight"]
            * crossed
            + self.config["continuous_occurrence_cell_weight"] * outside / h,
            candidate_passage_interval=[lo, hi],
            cell_lower_t=lower,
            cell_upper_t=upper,
            previous_gap_s=row["time"] - previous["time"] if previous else None,
            next_gap_s=following["time"] - row["time"] if following else None,
            previous_headway_s=row["time"] - previous["time"] if previous else None,
            next_headway_s=following["time"] - row["time"] if following else None,
            previous_candidate_ids=[r["candidate_id"] for r in previous["members"]]
            if previous
            else [],
            next_candidate_ids=[r["candidate_id"] for r in following["members"]]
            if following
            else [],
            simultaneous_candidate_ids=[
                r["candidate_id"] for r in groups[index]["members"]
            ],
            local_headway_s=h,
            headway_source=context["headway_source"],
            headway_confidence=context["headway_confidence"],
            fallback_cell_sides=int(previous is None) + int(following is None),
            independent_delay_proof=False,
        )

    def protected_neighbors(self, context, anchor_s, observed_t):
        local = self.classes[context["class_id"]]
        table = self.passages(local, anchor_s)
        groups = table["groups"]
        if not groups:
            return {}, table
        distances = [
            max(g["lower_t"] - observed_t, observed_t - g["upper_t"], 0.0)
            for g in groups
        ]
        best = min(distances)
        closest = [
            i
            for i, distance in enumerate(distances)
            if abs(distance - best) <= self.config["continuous_occurrence_tie_s"]
        ]
        result = collections.defaultdict(set)
        rescue = max(1, int(self.config["continuous_occurrence_rescue_neighbors"]))
        for centre in closest:
            for i in range(
                max(0, centre - rescue), min(len(groups), centre + rescue + 1)
            ):
                label = (
                    "CORE_NEAREST_AND_ADJACENT"
                    if abs(i - centre) <= 1
                    else "RESCUE_EXTRA_NEIGHBOR"
                )
                for row in groups[i]["members"]:
                    result[row["candidate_id"]].add(label)
        return {key: sorted(values) for key, values in result.items()}, table
