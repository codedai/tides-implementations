"""Synthetic fixtures for Spike-02 tests.

A straight north-south corridor with a terminal at each end. All times
are integer epochs; distances are metre-accurate enough for the
equirectangular helpers (the corridor sits at Toronto latitudes).
"""

from __future__ import annotations

from r1spike.candidate_generation import ApcRecord
from r1spike.segmentation_baselines import SegPing
from r1spike.spike02_contracts import (
    ClusterStatus,
    SignalQuality,
    TerminalCluster,
    VehicleActivityRun,
)

LON = -79.45
SOUTH_LAT = 43.70
NORTH_LAT = 43.75
# 0.001 degrees latitude ~= 111.2 m
LAT_PER_M = 1.0 / 111_195.0


def make_cluster(
    cluster_id: str,
    lat: float,
    lon: float = LON,
    *,
    radius_m: float = 150.0,
    stop_codes: tuple[str, ...] = (),
) -> TerminalCluster:
    return TerminalCluster(
        cluster_id=cluster_id,
        name_hypothesis=cluster_id,
        feed_id="feedX",
        service_dates=("2023-11-28",),
        centroid_lat=lat,
        centroid_lon=lon,
        geofence_radius_m=radius_m,
        member_stop_ids=("s-" + cluster_id,),
        member_stop_codes=stop_codes,
        pattern_endpoint_route_ids=("r1",),
        evidence_sources=("gtfs_pattern_endpoint",),
        status=ClusterStatus.CONFIRMED_EXPERIMENTAL,
        config_version="test-1",
    )


NORTH_CLUSTER = make_cluster("cl-north", NORTH_LAT, stop_codes=("1652",))
SOUTH_CLUSTER = make_cluster("cl-south", SOUTH_LAT)
CLUSTERS = [NORTH_CLUSTER, SOUTH_CLUSTER]


def ping(
    rid: str,
    t: int,
    lat: float,
    *,
    vehicle: str = "v1",
    lon: float = LON,
    route: str | None = "929",
    destination: str | None = None,
    fold_status: str = "resolved",
) -> SegPing:
    return SegPing(
        record_id=rid,
        vehicle=vehicle,
        instant_s=t if fold_status == "resolved" else None,
        candidate_instants=(t,) if fold_status == "resolved" else (t, t + 3600),
        fold_status=fold_status,
        local_date="2023-11-28",
        route_label=route,
        destination_label=destination,
        run_label=None,
        tatrip_id=None,
        latitude=lat,
        longitude=lon,
    )


def trip_pings(
    prefix: str,
    t0: int,
    *,
    northbound: bool,
    n: int = 30,
    step_s: int = 60,
    vehicle: str = "v1",
    destination: str | None = None,
) -> list[SegPing]:
    """One one-way traversal of the corridor in ``n`` pings."""
    lats = []
    for i in range(n):
        frac = i / (n - 1)
        lat = SOUTH_LAT + (NORTH_LAT - SOUTH_LAT) * (frac if northbound else 1 - frac)
        lats.append(lat)
    return [
        ping(
            f"{prefix}-{i:03d}",
            t0 + i * step_s,
            lats[i],
            vehicle=vehicle,
            destination=destination,
        )
        for i in range(n)
    ]


def dwell_pings(
    prefix: str,
    t0: int,
    lat: float,
    *,
    n: int = 6,
    step_s: int = 60,
    vehicle: str = "v1",
    jitter_m: float = 5.0,
    destination: str | None = None,
    fold_status: str = "resolved",
) -> list[SegPing]:
    """Stationary pings (sub-dwell-threshold movement) at one location."""
    out = []
    for i in range(n):
        offset = (jitter_m if i % 2 else -jitter_m) * LAT_PER_M
        out.append(
            ping(
                f"{prefix}-{i:03d}",
                t0 + i * step_s,
                lat + offset,
                vehicle=vehicle,
                destination=destination,
                fold_status=fold_status,
            )
        )
    return out


def make_run(pings: list[SegPing], run_id: str = "xar2-test") -> VehicleActivityRun:
    route_counts: dict[str, int] = {}
    for p in pings:
        if p.route_label is not None:
            route_counts[p.route_label] = route_counts.get(p.route_label, 0) + 1
    n_unres = sum(1 for p in pings if p.fold_status == "unresolved")
    return VehicleActivityRun(
        run_id=run_id,
        vehicle=pings[0].vehicle,
        record_ids=tuple(p.record_id for p in pings),
        start_instant_s=pings[0].ordering_instant,
        end_instant_s=pings[-1].ordering_instant,
        time_uncertain=n_unres > 0,
        n_unresolved=n_unres,
        local_dates=tuple(sorted({p.local_date for p in pings})),
        route_label_counts=route_counts,
    )


def apc_record(
    rid: str,
    arrival_s: int,
    departure_s: int | None = None,
    *,
    vehicle: str = "v1",
    lat: float = NORTH_LAT,
    lon: float = LON,
    stop_code: str | None = "1652",
    route: str | None = "929",
    direction: str | None = None,
    folds: tuple[int, ...] = (0,),
) -> ApcRecord:
    dep = departure_s if departure_s is not None else arrival_s
    return ApcRecord(
        record_id=rid,
        vehicle=vehicle,
        arrival_wall=f"epoch:{arrival_s}",
        departure_wall=f"epoch:{dep}",
        local_date="2023-11-28",
        route_label=route,
        branch_label=None,
        direction_label=direction,
        stop_code=stop_code,
        latitude=lat,
        longitude=lon,
        arrival_candidates=tuple((f, arrival_s + f * 3600) for f in folds),
        departure_candidates=tuple((f, dep + f * 3600) for f in folds),
    )


def q2_signal(rid: str, *, boarding: int = 0, alighting: int = 0):
    from r1spike.boundary_signals import derive_signal

    return derive_signal(
        rid,
        boarding=boarding,
        alighting=alighting,
        avg_sample=1,
        interval_inverted=False,
        visit_key_collision=False,
        negative_count=False,
        vehicle_present=True,
        arrival_time_present=True,
        fold_resolved=True,
    )


def assert_quality(signal, quality: SignalQuality) -> None:
    assert signal.quality == quality, (signal.quality, signal.reason_codes)
