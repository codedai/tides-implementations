"""Spike-02: source-signal derivation, quality tiers, terminal clusters."""

from __future__ import annotations

import polars as pl
import pytest
from r1spike.boundary_signals import derate, derive_signal, upgrade_to_q3
from r1spike.inputs import GtfsForDate
from r1spike.spike02_contracts import (
    CountSignalState,
    Reason02,
    SignalQuality,
)
from r1spike.terminal_clusters import build_terminal_clusters, point_cluster

from .spike02_helpers import CLUSTERS, LON, NORTH_LAT, SOUTH_LAT, make_cluster


def _signal(**overrides):
    kwargs = dict(
        boarding=2,
        alighting=3,
        avg_sample=1,
        interval_inverted=False,
        visit_key_collision=False,
        negative_count=False,
        vehicle_present=True,
        arrival_time_present=True,
        fold_resolved=True,
    )
    kwargs.update(overrides)
    return derive_signal("rec1", **kwargs)


class TestSignalStates:
    def test_states_from_positivity_only(self):
        assert _signal(boarding=2, alighting=3).state == (
            CountSignalState.MIXED_COUNT_SIGNAL
        )
        assert _signal(boarding=2, alighting=0).state == (
            CountSignalState.BOARDING_SIGNAL_ONLY
        )
        assert _signal(boarding=0, alighting=3).state == (
            CountSignalState.ALIGHTING_SIGNAL_ONLY
        )
        assert _signal(boarding=0, alighting=0).state == (
            CountSignalState.NO_COUNT_SIGNAL
        )

    def test_semantics_unconfirmed_reason_always_present(self):
        for s in (_signal(), _signal(interval_inverted=True)):
            assert Reason02.SIGNAL_SEMANTICS_UNCONFIRMED in s.reason_codes


class TestQualityTiers:
    def test_q0_on_structural_problems(self):
        assert _signal(interval_inverted=True).quality == SignalQuality.Q0_UNUSABLE
        assert _signal(negative_count=True).quality == SignalQuality.Q0_UNUSABLE
        assert _signal(vehicle_present=False).quality == SignalQuality.Q0_UNUSABLE
        assert _signal(arrival_time_present=False).quality == SignalQuality.Q0_UNUSABLE

    def test_q1_on_weak_evidence(self):
        assert _signal(avg_sample=2).quality == SignalQuality.Q1_WEAK
        assert _signal(visit_key_collision=True).quality == SignalQuality.Q1_WEAK
        assert _signal(fold_resolved=False).quality == SignalQuality.Q1_WEAK

    def test_q2_clean(self):
        assert _signal().quality == SignalQuality.Q2_USABLE

    def test_q3_requires_every_context_check(self):
        base = _signal()
        promoted = upgrade_to_q3(
            base,
            cluster_compatible=True,
            direction_progress_compatible=True,
            stop_context_compatible=True,
            state_consistent_with_episode=True,
        )
        assert promoted.quality == SignalQuality.Q3_STRONG
        for missing in range(4):
            flags = [True] * 4
            flags[missing] = False
            not_promoted = upgrade_to_q3(
                base,
                cluster_compatible=flags[0],
                direction_progress_compatible=flags[1],
                stop_context_compatible=flags[2],
                state_consistent_with_episode=flags[3],
            )
            assert not_promoted.quality == SignalQuality.Q2_USABLE

    def test_q1_never_promoted_to_q3(self):
        weak = _signal(avg_sample=2)
        promoted = upgrade_to_q3(
            weak,
            cluster_compatible=True,
            direction_progress_compatible=True,
            stop_context_compatible=True,
            state_consistent_with_episode=True,
        )
        assert promoted.quality == SignalQuality.Q1_WEAK

    def test_derate_never_raises_quality(self):
        weak = _signal(avg_sample=2)
        with pytest.raises(ValueError):
            derate(weak, to_quality=SignalQuality.Q2_USABLE, reasons=())


def _synthetic_gtfs() -> GtfsForDate:
    events = pl.DataFrame(
        {
            "trip_id": ["t1", "t1", "t1", "t2", "t2", "t2"],
            "stop_id": ["sA", "sM", "sB", "sB", "sM", "sA"],
            "stop_sequence": [1, 2, 3, 1, 2, 3],
            "arrival_epoch_s": [0, 600, 1200, 1500, 2100, 2700],
            "departure_epoch_s": [0, 600, 1200, 1500, 2100, 2700],
            "route_short_name": ["929"] * 6,
        }
    )
    trips = pl.DataFrame(
        {
            "trip_id": ["t1", "t2"],
            "route_id": ["r1", "r1"],
            "direction_id": [0, 1],
            "shape_id": ["sh1", "sh1"],
            "start_epoch_s": [0, 1500],
            "end_epoch_s": [1200, 2700],
        }
    )
    stops = pl.DataFrame(
        {
            "stop_id": ["sA", "sM", "sB"],
            "stop_code": ["100", "200", "300"],
            "stop_lat": [SOUTH_LAT, (SOUTH_LAT + NORTH_LAT) / 2, NORTH_LAT],
            "stop_lon": [LON, LON, LON],
        }
    )
    return GtfsForDate(
        service_date="2023-11-28",
        feed_id="feedX",
        artifact_dir=None,
        routes=pl.DataFrame({"route_id": ["r1"], "route_short_name": ["929"]}),
        trip_intervals=trips,
        scheduled_stop_events=events,
        stops=stops,
        shapes=pl.DataFrame(
            {
                "shape_id": ["sh1", "sh1"],
                "shape_pt_lat": [SOUTH_LAT, NORTH_LAT],
                "shape_pt_lon": [LON, LON],
                "shape_pt_sequence": [1, 2],
            }
        ),
        dominant_shape_by_route={"r1": "sh1"},
    )


class TestTerminalClusters:
    def test_pattern_endpoints_become_clusters(self):
        clusters = build_terminal_clusters(
            {"2023-11-28": _synthetic_gtfs()},
            geofence_radius_m=150.0,
            merge_radius_m=250.0,
            config_version="test",
        )
        # endpoints are sA and sB only; the mid stop never clusters
        codes = {code for c in clusters for code in c.member_stop_codes}
        assert codes == {"100", "300"}
        assert len(clusters) == 2

    def test_1652_hypothesis_attaches_only_inside_geofence(self):
        clusters = build_terminal_clusters(
            {"2023-11-28": _synthetic_gtfs()},
            geofence_radius_m=150.0,
            merge_radius_m=250.0,
            config_version="test",
            extra_stop_code_hypotheses={
                "1652": {"lat": NORTH_LAT, "lon": LON, "name": "WILSON HYP"},
            },
        )
        north = next(c for c in clusters if "300" in c.member_stop_codes)
        south = next(c for c in clusters if "100" in c.member_stop_codes)
        assert "1652" in north.member_stop_codes
        assert Reason02.CLUSTER_EXPERIMENTAL_1652 in north.reason_codes
        assert "1652" not in south.member_stop_codes

    def test_no_anonymous_nearest_terminal_fallback(self):
        # A point outside every geofence has NO terminal hypothesis.
        outside = point_cluster(CLUSTERS, (NORTH_LAT + SOUTH_LAT) / 2, LON)
        assert outside is None

    def test_exact_tie_between_clusters_returns_none(self):
        # Two distinct cluster hypotheses sharing a centroid: any point
        # inside is at exactly equal distance — ambiguity is surfaced.
        a = make_cluster("cl-a", 43.72, radius_m=500.0)
        b = make_cluster("cl-b", 43.72, radius_m=500.0)
        assert point_cluster([a, b], 43.72, LON) is None
