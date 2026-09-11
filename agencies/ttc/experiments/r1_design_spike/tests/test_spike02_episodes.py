"""Spike-02 terminal/short-turn episode tests (required tests 1-4,
9-15, 25, 26, and the DST-preservation half of 35)."""

from __future__ import annotations

from r1spike.provisional_alignment import align_apc_records
from r1spike.spike02_contracts import (
    EpisodeStatus,
    EvidenceFamily,
    Reason02,
)
from r1spike.terminal_episodes import EpisodeConfig, detect_episodes

from .spike02_helpers import (
    CLUSTERS,
    LAT_PER_M,
    NORTH_LAT,
    SOUTH_LAT,
    apc_record,
    dwell_pings,
    make_cluster,
    make_run,
    trip_pings,
)

CFG = EpisodeConfig()


def standard_scenario(
    *,
    alighting_arrival: int = 2760,
    boarding_departure: int = 3140,
    with_apc: bool = True,
    with_direction: bool = True,
    apc_avg_sample: int = 1,
):
    """Northbound trip -> dwell at the north terminal -> southbound trip."""
    pings = (
        trip_pings("in", 1000, northbound=True, destination="WILSON")
        + dwell_pings("dw", 2800, NORTH_LAT, n=6, destination="WILSON")
        + trip_pings("out", 3200, northbound=False, destination="EXHIBITION")
    )
    run = make_run(pings)
    records = []
    signals = {}
    if with_apc:
        records = [
            apc_record(
                "apc-al",
                alighting_arrival,
                alighting_arrival + 30,
                direction="NORTH" if with_direction else None,
            ),
            apc_record(
                "apc-bd",
                boarding_departure - 30,
                boarding_departure,
                direction="SOUTH" if with_direction else None,
            ),
        ]
        signals = {
            "apc-al": _sig("apc-al", alighting=4, avg_sample=apc_avg_sample),
            "apc-bd": _sig("apc-bd", boarding=6, avg_sample=apc_avg_sample),
        }
        if with_direction:
            # mid-route context records so a direction transition exists
            records.append(
                apc_record(
                    "apc-pre",
                    2400,
                    2410,
                    lat=NORTH_LAT - 2000 * LAT_PER_M,
                    stop_code="200",
                    direction="NORTH",
                )
            )
            records.append(
                apc_record(
                    "apc-post",
                    3400,
                    3410,
                    lat=NORTH_LAT - 2000 * LAT_PER_M,
                    stop_code="200",
                    direction="SOUTH",
                )
            )
            signals["apc-pre"] = _sig("apc-pre", boarding=1)
            signals["apc-post"] = _sig("apc-post", boarding=1)
    pings_by_id = {p.record_id: p for p in pings}
    edges = align_apc_records(
        records,
        [run],
        pings_by_id,
        signals,
        CLUSTERS,
        max_time_gap_s=900,
        interpolation_max_gap_s=300,
    )
    return run, pings, edges, signals


def _sig(rid, *, boarding=0, alighting=0, avg_sample=1):
    from r1spike.boundary_signals import derive_signal

    return derive_signal(
        rid,
        boarding=boarding,
        alighting=alighting,
        avg_sample=avg_sample,
        interval_inverted=False,
        visit_key_collision=False,
        negative_count=False,
        vehicle_present=True,
        arrival_time_present=True,
        fold_resolved=True,
    )


def detect(run, pings, edges, signals, **kwargs):
    return detect_episodes(run, pings, CLUSTERS, edges, signals, cfg=CFG, **kwargs)


class TestFourTimestamps:
    def test_1_all_four_distinct(self):
        run, pings, edges, signals = standard_scenario()
        eps = [e for e in detect(run, pings, edges, signals) if e.kind == "terminal"]
        ep = next(e for e in eps if e.cluster_id == "cl-north")
        assert ep.physical_arrival_s is not None
        assert ep.service_end_s == 2760
        assert ep.service_start_s == 3140
        assert ep.physical_departure_s is not None
        times = [
            ep.physical_arrival_s,
            ep.service_end_s,
            ep.service_start_s,
            ep.physical_departure_s,
        ]
        assert len(set(times)) == 4
        assert ep.ordering_valid
        assert ep.status == EpisodeStatus.SELECTED

    def test_2_equal_service_times_are_not_a_violation(self):
        run, pings, edges, signals = standard_scenario(
            alighting_arrival=2900, boarding_departure=2900
        )
        ep = next(
            e for e in detect(run, pings, edges, signals) if e.cluster_id == "cl-north"
        )
        assert ep.service_end_s == ep.service_start_s == 2900
        assert ep.ordering_valid

    def test_3_missing_terminal_times_marked_partial(self):
        run, pings, edges, signals = standard_scenario(with_apc=False)
        ep = next(
            e for e in detect(run, pings, edges, signals) if e.cluster_id == "cl-north"
        )
        assert ep.service_end_s is None and ep.service_start_s is None
        assert Reason02.EPISODE_PARTIAL_TIMES in ep.reason_codes
        assert ep.status == EpisodeStatus.SELECTED  # physical evidence remains

    def test_4_invalid_ordering_stays_ambiguous_never_reordered(self):
        # boarding evidence BEFORE alighting evidence: service_start <
        # service_end.
        run, pings, edges, signals = standard_scenario(
            alighting_arrival=3100, boarding_departure=2820
        )
        ep = next(
            e for e in detect(run, pings, edges, signals) if e.cluster_id == "cl-north"
        )
        assert not ep.ordering_valid
        assert ep.status == EpisodeStatus.AMBIGUOUS
        assert Reason02.EPISODE_ORDER_VIOLATION in ep.reason_codes
        # values retained exactly as evidence produced them
        assert ep.service_end_s == 3100
        assert ep.service_start_s == 2820
        assert ("service_end_s", 3100) in ep.alternative_timestamps


class TestDirectionAlone:
    def test_9_direction_change_without_motion_evidence_creates_nothing(self):
        # Vehicle drives straight through; APC direction flips mid-route.
        pings = trip_pings("thru", 1000, northbound=True, n=60)
        run = make_run(pings)
        records = [
            apc_record(
                "d1",
                1300,
                1310,
                lat=SOUTH_LAT + 900 * LAT_PER_M,
                stop_code="200",
                direction="NORTH",
            ),
            apc_record(
                "d2",
                1500,
                1510,
                lat=SOUTH_LAT + 1500 * LAT_PER_M,
                stop_code="200",
                direction="SOUTH",
            ),
        ]
        signals = {r.record_id: _sig(r.record_id, boarding=1) for r in records}
        pings_by_id = {p.record_id: p for p in pings}
        edges = align_apc_records(
            records,
            [run],
            pings_by_id,
            signals,
            CLUSTERS,
            max_time_gap_s=900,
            interpolation_max_gap_s=300,
        )
        eps = detect(run, pings, edges, signals)
        assert all(e.kind != "short_turn_candidate" for e in eps)

    def test_10_11_12_early_late_chatter_destination_updates_do_not_split(self):
        for name, dests in (
            ("early", ["B"] * 5 + ["A"] * 55),
            ("late", ["A"] * 55 + ["B"] * 5),
            ("chatter", (["A", "B"] * 30)),
        ):
            pings = [
                p for i, p in enumerate(trip_pings("t", 1000, northbound=True, n=60))
            ]
            pings = [
                type(p)(
                    **{
                        **{
                            f: getattr(p, f)
                            for f in (
                                "record_id",
                                "vehicle",
                                "instant_s",
                                "candidate_instants",
                                "fold_status",
                                "local_date",
                                "route_label",
                                "run_label",
                                "tatrip_id",
                                "latitude",
                                "longitude",
                            )
                        },
                        "destination_label": dests[i],
                    }
                )
                for i, p in enumerate(pings)
            ]
            run = make_run(pings)
            eps = detect(run, pings, [], {})
            terminal_boundaries = [e for e in eps if e.status == EpisodeStatus.SELECTED]
            # a passing visit at the north terminal may appear at the end
            # of the run, but no mid-route episode may exist
            assert all(e.cluster_id is not None for e in terminal_boundaries), name


class TestGeofenceAndDwell:
    def test_13_geofence_transit_without_boundary_evidence_is_insufficient(self):
        # A mid-route cluster the vehicle drives straight through.
        mid_cluster = make_cluster("cl-mid", (NORTH_LAT + SOUTH_LAT) / 2)
        pings = trip_pings("t", 1000, northbound=True, n=60)
        run = make_run(pings)
        eps = detect_episodes(run, pings, [mid_cluster], [], {}, cfg=CFG)
        mid = [e for e in eps if e.cluster_id == "cl-mid"]
        assert mid, "transit visit should still be recorded as a candidate"
        assert all(e.status == EpisodeStatus.INSUFFICIENT for e in mid)
        assert all(
            Reason02.EPISODE_INSUFFICIENT_EVIDENCE in e.reason_codes for e in mid
        )

    def test_14_long_traffic_dwell_outside_terminal_creates_no_episode(self):
        mid_lat = SOUTH_LAT + 1200 * LAT_PER_M
        approach = [
            p for p in trip_pings("a", 1000, northbound=True) if p.latitude <= mid_lat
        ]
        onward = [
            p for p in trip_pings("b", 2600, northbound=True) if p.latitude >= mid_lat
        ]
        pings = approach + dwell_pings("jam", 2000, mid_lat, n=8) + onward
        pings.sort(key=lambda p: p.ordering_instant)
        # continue north after the jam: no reversal, no cluster
        run = make_run(pings)
        along = {p.record_id: (p.latitude - SOUTH_LAT) / LAT_PER_M for p in pings}
        eps = detect(run, pings, [], {}, along_by_id=along)
        assert all(e.kind != "short_turn_candidate" for e in eps)

    def test_15_terminal_arrival_with_long_turnaround_selected(self):
        run, pings, edges, signals = standard_scenario(with_apc=False)
        ep = next(
            e for e in detect(run, pings, edges, signals) if e.cluster_id == "cl-north"
        )
        assert ep.status == EpisodeStatus.SELECTED
        assert EvidenceFamily.DWELL.value in ep.evidence_families
        assert EvidenceFamily.GEOFENCE.value in ep.evidence_families


class TestShortTurn:
    def _short_turn_pings(self):
        mid_lat = SOUTH_LAT + 2500 * LAT_PER_M
        up = trip_pings("up", 1000, northbound=True, n=15)
        up = [p for p in up if p.latitude <= mid_lat]
        dwell = dwell_pings("sd", up[-1].instant_s + 60, mid_lat, n=6)
        down = [
            p
            for p in trip_pings(
                "down", dwell[-1].instant_s + 60, northbound=False, n=15
            )
            if p.latitude <= mid_lat
        ]
        return up + dwell + down

    def test_25_short_turn_with_progress_dwell_and_direction(self):
        pings = self._short_turn_pings()
        run = make_run(pings)
        along = {p.record_id: (p.latitude - SOUTH_LAT) / LAT_PER_M for p in pings}
        records = [
            apc_record(
                "pre",
                pings[3].instant_s,
                pings[3].instant_s + 10,
                lat=pings[3].latitude,
                stop_code="200",
                direction="NORTH",
            ),
            apc_record(
                "post",
                pings[-3].instant_s,
                pings[-3].instant_s + 10,
                lat=pings[-3].latitude,
                stop_code="200",
                direction="SOUTH",
            ),
        ]
        signals = {r.record_id: _sig(r.record_id, boarding=1) for r in records}
        pings_by_id = {p.record_id: p for p in pings}
        edges = align_apc_records(
            records,
            [run],
            pings_by_id,
            signals,
            CLUSTERS,
            max_time_gap_s=900,
            interpolation_max_gap_s=300,
        )
        eps = detect(run, pings, edges, signals, along_by_id=along)
        turns = [e for e in eps if e.kind == "short_turn_candidate"]
        assert turns
        assert turns[0].status == EpisodeStatus.SELECTED
        fams = set(turns[0].evidence_families)
        assert EvidenceFamily.PROGRESS_REVERSAL.value in fams
        assert len(fams) >= 2

    def test_26_direction_only_false_short_turn_rejected(self):
        # No dwell, no reversal in geometry: direction flip alone.
        pings = trip_pings("t", 1000, northbound=True, n=40)
        run = make_run(pings)
        along = {p.record_id: (p.latitude - SOUTH_LAT) / LAT_PER_M for p in pings}
        records = [
            apc_record(
                "pre",
                1200,
                1210,
                lat=pings[5].latitude,
                stop_code="200",
                direction="NORTH",
            ),
            apc_record(
                "post",
                1600,
                1610,
                lat=pings[12].latitude,
                stop_code="200",
                direction="SOUTH",
            ),
        ]
        signals = {r.record_id: _sig(r.record_id, boarding=1) for r in records}
        pings_by_id = {p.record_id: p for p in pings}
        edges = align_apc_records(
            records,
            [run],
            pings_by_id,
            signals,
            CLUSTERS,
            max_time_gap_s=900,
            interpolation_max_gap_s=300,
        )
        eps = detect(run, pings, edges, signals, along_by_id=along)
        assert all(e.kind != "short_turn_candidate" for e in eps)


class TestDstPreservation:
    def test_35_fold_unresolved_visit_yields_no_physical_timestamps(self):
        pings = (
            trip_pings("in", 1000, northbound=True)
            + dwell_pings("dw", 2800, NORTH_LAT, n=6, fold_status="unresolved")
            + trip_pings("out", 3400, northbound=False)
        )
        run = make_run(pings)
        ep = next(e for e in detect(run, pings, [], {}) if e.cluster_id == "cl-north")
        assert ep.physical_arrival_s is None
        assert ep.physical_departure_s is None
        assert Reason02.BOUNDARY_FOLD_UNRESOLVED in ep.reason_codes
        assert ep.status == EpisodeStatus.AMBIGUOUS
