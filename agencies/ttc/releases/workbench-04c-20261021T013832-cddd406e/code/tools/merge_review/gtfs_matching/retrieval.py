"""Service-aware, local-headway retrieval; union is never capped to three trips."""

from __future__ import annotations

import math
import statistics

from .windows import observation_key, usable

DEFAULTS = {
    "retrieval_top_k": 8,
    "retrieval_best_delta": 0.25,
    "retrieval_headway_multiplier": 2.0,
    "retrieval_default_headway_s": 600,
    "retrieval_min_buffer_s": 60,
    "retrieval_history_delay_s": 0,
    "retrieval_gap_rescue_s": 600,
    "retrieval_progress_padding_m": 100,
}


def get_config(config=None):
    return DEFAULTS | (config or {})


def trip_key(trip):
    return "|".join(
        str(trip.get(k, "")) for k in ("feed_version", "service_date", "trip_id")
    )


def for_reference(rows, reference_id, *, include_unprojectable=False):
    output = []
    for original in rows:
        if original.get("reference_id") == reference_id:
            output.append(dict(original, s_m=original.get("s_m", original.get("s"))))
            continue
        alternative = next(
            (
                a
                for a in original.get("projection_alternatives", [])
                if a.get("reference_id") == reference_id
            ),
            None,
        )
        if alternative is not None:
            output.append(dict(original, **alternative))
        elif include_unprojectable:
            output.append(
                dict(
                    original,
                    reference_id=reference_id,
                    s_m=None,
                    projection_status="UNPROJECTABLE",
                    quality=0,
                    reference_exclusion_reason="NO_PROJECTION_FOR_REFERENCE",
                )
            )
    return output


def passage_times(trip, progress):
    """Scheduled stop intervals/interstop passage only; never extrapolate."""
    points = [
        p for p in trip.get("scheduled", []) if p.get("s_m", p.get("s")) is not None
    ]
    result = []
    for point in points:
        if abs(point.get("s_m", point.get("s")) - progress) < 1e-6:
            result.extend(
                [
                    point.get("arrival_t", point["t"]),
                    point.get("departure_t", point["t"]),
                ]
            )
    for first, second in zip(points, points[1:], strict=False):
        a, b = first.get("s_m", first.get("s")), second.get("s_m", second.get("s"))
        if a < progress < b:
            fraction = (progress - a) / (b - a)
            result.append(
                first.get("departure_t", first["t"])
                + fraction
                * (
                    second.get("arrival_t", second["t"])
                    - first.get("departure_t", first["t"])
                )
            )
    return sorted(
        set(t for t in result if isinstance(t, (int, float)) and math.isfinite(t))
    )


def retrieve_candidates(
    window_bundle, observations, trips, config=None, *, service_date=None, route_id=None
):
    c = get_config(config)
    allowed = [
        t
        for t in trips
        if (service_date is None or str(t.get("service_date")) == str(service_date))
        and (route_id is None or str(t.get("route_id")) == str(route_id))
    ]
    by_id = {observation_key(row): row for row in observations}
    selected, windows = {}, []
    for window in window_bundle["windows"]:
        rows = [by_id[key] for key in window["observation_ids"]]
        anchors = {}
        passage_cache = {}
        scored = []
        for trip in allowed:
            ref = trip.get("reference_id")
            if ref not in anchors:
                good = [r for r in for_reference(rows, ref) if usable(r, c)]
                anchors[ref] = (
                    (
                        statistics.median(r["t"] for r in good),
                        statistics.median(r["s_m"] for r in good),
                        min(r["s_m"] for r in good),
                        max(r["s_m"] for r in good),
                    )
                    if good
                    else None
                )
            anchor = anchors[ref]
            if anchor is None:
                continue
            at, progress, lo, hi = anchor
            if ref not in passage_cache:
                passage_cache[ref] = {
                    trip_key(other): passage_times(other, progress)
                    for other in allowed
                    if other.get("reference_id") == ref
                }
            scheduled = trip.get("scheduled", [])
            ss = [
                p.get("s_m", p.get("s"))
                for p in scheduled
                if p.get("s_m", p.get("s")) is not None
            ]
            if (
                not ss
                or hi < min(ss) - c["retrieval_progress_padding_m"]
                or lo > max(ss) + c["retrieval_progress_padding_m"]
            ):
                continue
            passages = passage_cache[ref][trip_key(trip)]
            if not passages:
                continue
            local = []
            for values in passage_cache[ref].values():
                local.extend(values)
            nearest = min(passages, key=lambda t: abs(t - at))
            neighbours = sorted(
                {abs(t - nearest) for t in local if abs(t - nearest) > 1}
            )
            headway = neighbours[0] if neighbours else c["retrieval_default_headway_s"]
            buffer = max(
                c["retrieval_min_buffer_s"], headway * c["retrieval_headway_multiplier"]
            )
            buffer += abs(c["retrieval_history_delay_s"])
            if window["has_gap"]:
                buffer += c["retrieval_gap_rescue_s"]
            residual = at - nearest
            if abs(residual) <= buffer:
                scored.append(
                    {
                        "candidate_id": trip_key(trip),
                        "trip_id": trip["trip_id"],
                        "service_date": trip.get("service_date"),
                        "feed_version": trip.get("feed_version"),
                        "reference_id": ref,
                        "score": abs(residual) / max(buffer, 1),
                        "estimated_delay_s": residual,
                        "scheduled_passage_t": nearest,
                        "local_headway_s": headway,
                        "buffer_s": buffer,
                        "progress_m": progress,
                    }
                )
        scored.sort(key=lambda row: (row["score"], row["candidate_id"]))
        kept = [
            row
            for index, row in enumerate(scored)
            if index < c["retrieval_top_k"]
            or row["score"] <= scored[0]["score"] + c["retrieval_best_delta"]
        ]
        selected.update({row["candidate_id"]: row for row in kept})
        windows.append(
            {"window_id": window["id"], "retrieved": scored, "selected": kept}
        )
    lookup = {trip_key(trip): trip for trip in allowed}
    return {
        "candidate_ids": sorted(selected),
        "trips": [lookup[key] for key in sorted(selected)],
        "windows": windows,
        "union_truncated": False,
        "rule": "Per-window top K OR best+delta, union across all scales; "
        "no global top-three retrieval cap.",
    }
