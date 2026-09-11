"""Versioned experimental terminal/turnback cluster hypotheses (Spike 02).

Evidence sources: GTFS pattern endpoints (first/last scheduled stop per
trip) and stop coordinates, plus optional APC-observed stop-code
hypotheses supplied by configuration (e.g. the unmapped code 1652 whose
coordinates and street text suggest Wilson Station).

Nothing here is an approved crosswalk: D-10 (stop code 1652) is OPEN,
so any code association is an experimental hypothesis with explicit
provenance, valid for this run only. There is NO anonymous
nearest-terminal fallback: a point either falls inside a versioned
cluster's geofence or it has no terminal hypothesis.
"""

from __future__ import annotations

import hashlib
import json
import math

import polars as pl

from .inputs import GtfsForDate
from .spike02_contracts import (
    ALGORITHM_VERSION02,
    CLUSTER_ID_VERSION,
    ClusterStatus,
    Reason02,
    TerminalCluster,
)

EARTH_RADIUS_M = 6_371_008.8


def _dist_m(lat0: float, lon0: float, lat1: float, lon1: float) -> float:
    lat_ref = math.radians((lat0 + lat1) / 2.0)
    dx = EARTH_RADIUS_M * math.cos(lat_ref) * math.radians(lon1 - lon0)
    dy = EARTH_RADIUS_M * math.radians(lat1 - lat0)
    return math.hypot(dx, dy)


def _find(parent: dict[str, str], x: str) -> str:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _cluster_id(feed_id: str, member_stop_ids: list[str]) -> str:
    key = json.dumps(
        [CLUSTER_ID_VERSION, ALGORITHM_VERSION02, feed_id, sorted(member_stop_ids)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{CLUSTER_ID_VERSION}-{hashlib.sha256(key.encode()).hexdigest()[:32]}"


def _pattern_endpoints(gtfs: GtfsForDate) -> dict[str, set[str]]:
    """stop_id -> set of route_ids for which the stop is a trip endpoint."""
    events = gtfs.scheduled_stop_events
    if events.height == 0:
        return {}
    firsts = (
        events.sort("trip_id", "stop_sequence")
        .group_by("trip_id", maintain_order=True)
        .agg(pl.col("stop_id").first().alias("stop_id"))
    )
    lasts = (
        events.sort("trip_id", "stop_sequence")
        .group_by("trip_id", maintain_order=True)
        .agg(pl.col("stop_id").last().alias("stop_id"))
    )
    trip_routes = dict(gtfs.trip_intervals.select("trip_id", "route_id").iter_rows())
    endpoints: dict[str, set[str]] = {}
    for frame in (firsts, lasts):
        for trip_id, stop_id in frame.iter_rows():
            route_id = trip_routes.get(trip_id)
            if route_id is None:
                continue
            endpoints.setdefault(stop_id, set()).add(route_id)
    return endpoints


def build_terminal_clusters(
    gtfs_by_date: dict[str, GtfsForDate],
    *,
    geofence_radius_m: float,
    merge_radius_m: float,
    config_version: str,
    extra_stop_code_hypotheses: dict[str, dict] | None = None,
) -> list[TerminalCluster]:
    """Build per-feed terminal cluster hypotheses from pattern endpoints.

    ``extra_stop_code_hypotheses`` maps an (unmapped) source stop code
    to ``{"lat": .., "lon": .., "name": .., "provenance": ..}``; the
    code is associated with a cluster ONLY when its coordinates fall
    inside the cluster geofence, and the association is recorded as an
    experimental hypothesis, never as an approved mapping.
    """
    extra = extra_stop_code_hypotheses or {}
    by_feed: dict[str, list[GtfsForDate]] = {}
    for gtfs in gtfs_by_date.values():
        by_feed.setdefault(gtfs.feed_id, []).append(gtfs)

    clusters: list[TerminalCluster] = []
    for feed_id in sorted(by_feed):
        feeds = by_feed[feed_id]
        dates = tuple(sorted(g.service_date for g in feeds))
        # Endpoints are stable within one feed; merge across its dates.
        endpoint_routes: dict[str, set[str]] = {}
        stops_seen: dict[str, tuple[str | None, float | None, float | None]] = {}
        for gtfs in feeds:
            for stop_id, routes in _pattern_endpoints(gtfs).items():
                endpoint_routes.setdefault(stop_id, set()).update(routes)
            for row in gtfs.stops.iter_rows(named=True):
                stops_seen.setdefault(
                    row["stop_id"],
                    (row["stop_code"], row["stop_lat"], row["stop_lon"]),
                )
        usable = {
            sid: (code, lat, lon)
            for sid, (code, lat, lon) in stops_seen.items()
            if sid in endpoint_routes and lat is not None and lon is not None
        }
        # Union-find proximity merge of endpoint stops.
        ids = sorted(usable)
        parent = {sid: sid for sid in ids}
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                _, lat_a, lon_a = usable[a]
                _, lat_b, lon_b = usable[b]
                if _dist_m(lat_a, lon_a, lat_b, lon_b) <= merge_radius_m:
                    ra, rb = _find(parent, a), _find(parent, b)
                    if ra != rb:
                        if ra > rb:
                            ra, rb = rb, ra
                        parent[rb] = ra

        groups: dict[str, list[str]] = {}
        for sid in ids:
            groups.setdefault(_find(parent, sid), []).append(sid)

        for root in sorted(groups):
            members = sorted(groups[root])
            lats = [usable[m][1] for m in members]
            lons = [usable[m][2] for m in members]
            centroid_lat = sum(lats) / len(lats)
            centroid_lon = sum(lons) / len(lons)
            member_codes = sorted(
                {usable[m][0] for m in members if usable[m][0] is not None}
            )
            routes = sorted({r for m in members for r in endpoint_routes[m]})
            evidence = ["gtfs_pattern_endpoint", "gtfs_stop_coordinates"]
            reasons: list[str] = []
            # Experimental source-code association by coordinates only.
            for code in sorted(extra):
                hyp = extra[code]
                if (
                    _dist_m(hyp["lat"], hyp["lon"], centroid_lat, centroid_lon)
                    <= geofence_radius_m
                ):
                    member_codes.append(str(code))
                    evidence.append("apc_observed_stop_code_hypothesis")
                    if str(code) == "1652":
                        reasons.append(Reason02.CLUSTER_EXPERIMENTAL_1652)
            name_parts = [
                str(extra[c].get("name"))
                for c in sorted(extra)
                if str(c) in member_codes and extra[c].get("name")
            ]
            clusters.append(
                TerminalCluster(
                    cluster_id=_cluster_id(feed_id, members),
                    name_hypothesis=(
                        name_parts[0] if name_parts else f"endpoint:{members[0]}"
                    ),
                    feed_id=feed_id,
                    service_dates=dates,
                    centroid_lat=centroid_lat,
                    centroid_lon=centroid_lon,
                    geofence_radius_m=geofence_radius_m,
                    member_stop_ids=tuple(members),
                    member_stop_codes=tuple(sorted(set(member_codes))),
                    pattern_endpoint_route_ids=tuple(routes),
                    evidence_sources=tuple(dict.fromkeys(evidence)),
                    status=ClusterStatus.CONFIRMED_EXPERIMENTAL,
                    config_version=config_version,
                    reason_codes=tuple(reasons),
                )
            )
    return clusters


def point_cluster(
    clusters: list[TerminalCluster],
    lat: float | None,
    lon: float | None,
    *,
    service_date: str | None = None,
) -> TerminalCluster | None:
    """The cluster whose geofence contains the point, or None.

    If more than one cluster contains the point the SMALLEST distance
    wins only when unambiguous; an exact tie returns None (ambiguity is
    surfaced, never resolved by list order). No nearest-cluster
    fallback exists for points outside every geofence.
    """
    if lat is None or lon is None:
        return None
    hits: list[tuple[float, TerminalCluster]] = []
    for cluster in clusters:
        if service_date is not None and cluster.service_dates:
            if service_date not in cluster.service_dates:
                continue
        d = _dist_m(lat, lon, cluster.centroid_lat, cluster.centroid_lon)
        if d <= cluster.geofence_radius_m:
            hits.append((d, cluster))
    if not hits:
        return None
    hits.sort(key=lambda pair: (pair[0], pair[1].cluster_id))
    if len(hits) > 1 and hits[0][0] == hits[1][0]:
        return None
    return hits[0][1]
