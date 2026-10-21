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
    def __init__(self, trips, config, *, references=None, service_date=None):
        # None means the caller's already active feed pool, including previous
        # service-day tails on the same absolute clock. Explicit scope is strict.
        self.service_date_scope = (
            service_date if config.get("continuous_occurrence_model_enabled") else None
        )
        if self.service_date_scope is not None:
            trips = [t for t in trips if t.get("service_date") == service_date]
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
        self.references = references
        self._occurrences = None

    def occurrence_index(self):
        if self._occurrences is None:
            from .occurrence import OccurrenceIndex

            self._occurrences = OccurrenceIndex(self, self.references)
        return self._occurrences

    def confusability(self, trip, s_range, anchor_s, observed_t, observed_stop_ids=()):
        return self.occurrence_index().context(
            trip, s_range, anchor_s, observed_t, observed_stop_ids
        )

    def occurrence_evidence(self, trip, s_m, observed_t, *, context):
        if context is None:
            return {
                "status": "NO_OCCURRENCE_EVIDENCE",
                "crossed_occurrences": None,
                "outside_cell_s": None,
                "displacement_cost": None,
                "reasons": ["MISSING_COMMON_OBSERVATION_CONTEXT"],
            }
        return self.occurrence_index().evidence(trip, s_m, observed_t, context)

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

    def headway(self, trip, progress, *, context=None):
        if self.config.get("continuous_occurrence_model_enabled"):
            # Never infer the observed operating context from a candidate's own
            # timestamp. New scoring passes the shared context explicitly.
            return (
                context["local_headway_s"]
                if context is not None
                else self.occurrence_index().config[
                    "continuous_occurrence_default_headway_s"
                ]
            )
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


def schedule_pool(trips, config, *, references=None, service_date=None):
    # Keeps original trips alive; immutable canonical bundle is shared per worker.
    signature = (
        id(trips),
        id(references),
        service_date,
        tuple(
            sorted(
                (k, repr(v)) for k, v in config.items() if k.startswith("continuous_")
            )
        ),
    )
    if signature not in _POOLS:
        if len(_POOLS) >= 4:
            _POOLS.clear()
        _POOLS[signature] = (
            trips,
            SchedulePool(
                trips, config, references=references, service_date=service_date
            ),
        )
    return _POOLS[signature][1]


def retrieve(
    pool,
    points,
    windows,
    config,
    *,
    service_date=None,
    route_id=None,
    apc=None,
    rescue_context=None,
):
    if config.get("continuous_occurrence_model_enabled"):
        return _retrieve_occurrences(
            pool,
            points,
            windows,
            config,
            service_date=service_date,
            route_id=route_id,
            apc=apc,
            rescue_context=rescue_context,
        )
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


def _retrieve_occurrences(
    pool, points, windows, config, *, service_date, route_id, apc, rescue_context
):
    from .apc import normalize_apc

    if service_date is not None and pool.service_date_scope != service_date:
        pool = SchedulePool(
            list(pool.trips.values()),
            config,
            references=pool.references,
            service_date=service_date,
        )
    index = pool.occurrence_index()
    context = rescue_context or {}
    allowed = {
        key: t
        for key, t in pool.trips.items()
        if (route_id is None or str(t.get("route_id")) == str(route_id))
        and (service_date is None or t.get("service_date") == service_date)
    }
    refs = sorted({t["reference_id"] for t in allowed.values()})
    traces = {
        ref: {
            observation_key(row): row
            for row in for_reference(points, ref, include_unprojectable=True)
        }
        for ref in refs
    }
    stop_evidence = {ref: normalize_apc(apc or [], ref) for ref in refs}
    candidates, records = set(), []
    for window in windows["windows"]:
        anchors = {}
        for ref, trace in traces.items():
            raw = [trace[key] for key in window["observation_ids"] if key in trace]
            valid = sorted(
                (row for row in raw if valid_projection(row, config)),
                key=lambda row: (row["t"], observation_key(row)),
            )
            if (
                len(valid) < config["continuous_min_points"]
                or coverage_metrics(raw, set(), config)["projection"]["duration_s"]
                < config["continuous_min_duration_s"]
            ):
                continue
            bounds = min(p["s_m"] for p in valid), max(p["s_m"] for p in valid)
            if bounds[1] - bounds[0] < config["continuous_min_progress_m"]:
                continue
            ordered = sorted(
                (
                    p
                    for p in stop_evidence[ref]
                    if p["status"] == "EVIDENCE"
                    and p.get("stop_id") is not None
                    and p["confidence"] > 0
                    and p["confidence"]
                    >= index.config["continuous_occurrence_apc_min_confidence"]
                    and bounds[0] <= p["canonical_stop_s"] <= bounds[1]
                    and p["interval_start"] <= valid[-1]["t"]
                    and p["interval_end"] >= valid[0]["t"]
                ),
                key=lambda p: (p["interval_start"], p["canonical_stop_s"]),
            )
            stops = []
            for p in ordered:
                if not stops or stops[-1] != str(p["stop_id"]):
                    stops.append(str(p["stop_id"]))
            anchors[ref] = valid[len(valid) // 2], bounds, tuple(stops)
        ranked, protected = [], collections.defaultdict(set)
        neighbor_cache = {}
        for key, trip in allowed.items():
            if trip["reference_id"] not in anchors:
                continue
            anchor, bounds, stops = anchors[trip["reference_id"]]
            local = pool.confusability(trip, bounds, anchor["s_m"], anchor["t"], stops)
            cache_key = local["class_id"], anchor["s_m"], anchor["t"]
            if cache_key not in neighbor_cache:
                neighbor_cache[cache_key] = index.protected_neighbors(
                    local, anchor["s_m"], anchor["t"]
                )
            neighbors, table = neighbor_cache[cache_key]
            for identity, reasons in neighbors.items():
                if identity in allowed:
                    protected[identity].update(reasons)
            location = table["by_id"].get(key)
            if location is None:
                # Pattern-incompatible hypotheses may remain diagnostics, but
                # must never acquire a private singleton identity prior.
                passage = passage_at(pool.schedules[key], anchor["s_m"], config)
                if passage["status"] != "SUPPORTED" or len(passage["intervals"]) != 1:
                    continue
                lo, hi = passage["intervals"][0]
            else:
                lo, hi = location[1]["interval"]
            distance = max(lo - anchor["t"], anchor["t"] - hi, 0.0)
            headway = local["local_headway_s"]
            core = headway * config["continuous_core_headways"]
            extra = (
                config["continuous_clock_uncertainty_s"]
                + context.get("clock_uncertainty_s", 0.0)
                + context.get("boundary_uncertainty_s", 0.0)
                + (config["continuous_rescue_gap_s"] if window.get("has_gap") else 0.0)
            )
            rescue = (
                max(
                    core,
                    headway * config["continuous_rescue_headways"],
                    config.get("continuous_rescue_delay_s", 3600.0),
                )
                + extra
            )
            empirical = (
                context.get("calibrated") is True
                and isinstance(context.get("lower_s"), (int, float))
                and isinstance(context.get("upper_s"), (int, float))
                and anchor["t"] - hi <= context["upper_s"] + extra
                and anchor["t"] - lo >= context["lower_s"] - extra
            )
            sequence = key in context.get("sequence_candidate_ids", [])
            reasons = []
            if distance <= core:
                reasons.append("CORE_TIME_BUFFER")
            elif distance <= rescue:
                reasons.append("RESCUE_CONFIGURED_DELAY_OR_GAP")
            if empirical:
                reasons.append("RESCUE_VALIDATED_EMPIRICAL_DELAY_TAIL")
            if sequence:
                reasons.append("RESCUE_VEHICLE_SEQUENCE_EVIDENCE")
            if empirical or sequence:
                protected[key].update(reasons)
            ranked.append(
                dict(
                    candidate_id=key,
                    coarse_score=distance / headway,
                    reference_id=trip["reference_id"],
                    pattern_id=trip.get("pattern_id"),
                    confusability_class_id=local["class_id"],
                    local_headway_s=headway,
                    candidate_pattern_compatible=local["candidate_compatible"],
                    headway_source=local["headway_source"],
                    headway_confidence=local["headway_confidence"],
                    local_passage_intervals=[[lo, hi]],
                    core_buffer_s=core,
                    rescue_buffer_s=rescue,
                    rescue_used=distance > core,
                    observed_s=anchor["s_m"],
                    observed_t=anchor["t"],
                    observed_s_range=list(bounds),
                    observed_stop_ids=list(stops),
                    retrieval_reasons=reasons,
                    empirical_tail_provenance={
                        k: context.get(k)
                        for k in (
                            "source",
                            "validation_artifact",
                            "artifact_sha256",
                            "lower_s",
                            "upper_s",
                        )
                    }
                    if empirical
                    else None,
                )
            )
        ranked = [
            row
            for row in ranked
            if row["retrieval_reasons"] or row["candidate_id"] in protected
        ]
        ranked.sort(key=lambda row: (row["coarse_score"], row["candidate_id"]))
        grouped = collections.defaultdict(list)
        for row in ranked:
            grouped[row["confusability_class_id"]].append(row)
        kept = []
        for group in grouped.values():
            best = group[0]["coarse_score"]
            for rank, row in enumerate(group):
                mandatory = protected.get(row["candidate_id"], set())
                if (
                    mandatory
                    or rank < config["continuous_retrieval_top_k"]
                    or row["coarse_score"]
                    <= best + config["continuous_retrieval_delta"]
                ):
                    row["retrieval_reasons"] = sorted(
                        set(row["retrieval_reasons"]) | mandatory
                    )
                    row["protected_from_top_k"] = bool(mandatory)
                    kept.append(row)
        kept.sort(key=lambda row: (row["coarse_score"], row["candidate_id"]))
        candidates.update(row["candidate_id"] for row in kept)
        records.append(
            dict(
                window_id=window["id"],
                status="NO_EVIDENCE"
                if not anchors
                else "EVIDENCE"
                if kept
                else "NO_CANDIDATE",
                candidates=kept,
                compatible_reference_count=len(anchors),
                retrieval_basis=(
                    "local geometry and observed stop order; "
                    "shared occurrence neighbors plus explicit rescue"
                ),
            )
        )
    return dict(
        candidate_ids=sorted(candidates),
        trips=[pool.trips[key] for key in sorted(candidates)],
        windows=records,
        union_truncated=False,
        pattern_count=len(pool.patterns),
        confusability_class_count=len(index.classes),
        schedule_resampling_used=False,
        occurrence_model_enabled=True,
        exact_pattern_filter_used=False,
    )
