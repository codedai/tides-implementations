"""Spike-02 anchor-and-align, regression, and safety-guard tests
(required tests 30-34, 36-38, 40, plus publication guards)."""

from __future__ import annotations

import polars as pl
import pytest
from r1spike.anchor_alignment import AlignmentInput, align
from r1spike.gtfs_anchors import topology_fallback
from r1spike.provisional_alignment import align_apc_records
from r1spike.provisional_runs import build_activity_runs
from r1spike.spike02_artifacts import (
    PublicationGuardError02,
    Spike02Run,
    check_run_dir02,
)
from r1spike.spike02_contracts import Reason02
from r1spike.spike02_diagnostics import refinement_fingerprint
from r1spike.spike02_pipeline import Spike02Prepared  # noqa: F401  (import check)

from .spike02_helpers import apc_record, trip_pings
from .test_spike02_signals_clusters import _synthetic_gtfs


def _problem(**overrides):
    kwargs = dict(
        segment_ids=("A1", "A2", "A3"),
        trip_ids=("G1", "G2", "G3", "G4"),
        candidate_costs={
            "A1": {"G1": 100, "G2": 400},
            "A2": {"G2": 100, "G3": 400},
            "A3": {"G4": 100},
        },
        unmatched_cost=1_000,
        anchors={},
        allow_many_to_one=False,
    )
    kwargs.update(overrides)
    return AlignmentInput(**kwargs)


class TestAnchorAndAlign:
    def test_30_skipped_scheduled_trip_is_free(self):
        result = align(_problem())
        assert result.optimal_cost == 300
        matched = {s.segment_id: s.trip_id for s in result.segments}
        assert matched == {"A1": "G1", "A2": "G2", "A3": "G4"}
        assert "G3" in result.skipped_trip_ids

    def test_31_unmatched_performed_segment(self):
        result = align(_problem(candidate_costs={"A1": {"G1": 100}, "A3": {"G4": 100}}))
        a2 = next(s for s in result.segments if s.segment_id == "A2")
        assert a2.status == "unmatched"
        assert Reason02.ALIGNMENT_UNMATCHED_SEGMENT in a2.reason_codes

    def test_32_tied_paths_abstain(self):
        result = align(
            _problem(
                candidate_costs={
                    "A1": {"G1": 100, "G2": 100},
                    "A2": {"G2": 100, "G3": 100},
                    "A3": {"G4": 100},
                }
            )
        )
        # A1->G1,A2->G2 / A1->G1,A2->G3 / A1->G2,A2->G3 all cost 300
        a1 = next(s for s in result.segments if s.segment_id == "A1")
        a2 = next(s for s in result.segments if s.segment_id == "A2")
        a3 = next(s for s in result.segments if s.segment_id == "A3")
        assert a1.status == "tied" and a2.status == "tied"
        assert a3.status == "matched"
        assert result.path_margin == 0 or result.n_optimal > 1

    def test_33_many_to_one_only_when_enabled(self):
        costs = {"A1": {"G1": 100}, "A2": {"G1": 150}, "A3": {"G4": 100}}
        strict = align(_problem(candidate_costs=costs))
        a2 = next(s for s in strict.segments if s.segment_id == "A2")
        assert a2.status == "unmatched"  # G1 already consumed
        loose = align(_problem(candidate_costs=costs, allow_many_to_one=True))
        a2 = next(s for s in loose.segments if s.segment_id == "A2")
        assert a2.status == "matched" and a2.trip_id == "G1"

    def test_anchor_pins_and_bounds_the_interval(self):
        result = align(
            _problem(
                candidate_costs={
                    "A1": {"G1": 500, "G3": 100},
                    "A2": {"G2": 100},
                    "A3": {"G4": 100},
                },
                anchors={"A1": "G1"},
            )
        )
        a1 = next(s for s in result.segments if s.segment_id == "A1")
        assert a1.status == "anchor" and a1.trip_id == "G1"
        # even though G3 was cheaper for A1, the anchor pins G1 and A2
        # still matches monotonically after it
        a2 = next(s for s in result.segments if s.segment_id == "A2")
        assert a2.trip_id == "G2"

    def test_no_greedy_propagation_of_an_early_weak_match(self):
        # A weak early candidate (G3) would block A2/A3 monotonically;
        # the global optimum leaves A1 on G1 despite a locally lower
        # cost on G3.
        result = align(
            _problem(
                candidate_costs={
                    "A1": {"G1": 200, "G3": 100},
                    "A2": {"G2": 100, "G3": 300},
                    "A3": {"G3": 100},
                }
            )
        )
        matched = {s.segment_id: s.trip_id for s in result.segments}
        assert matched == {"A1": "G1", "A2": "G2", "A3": "G3"}

    def test_path_margin_is_exact_second_best(self):
        result = align(
            _problem(
                candidate_costs={
                    "A1": {"G1": 100, "G2": 250},
                    "A2": {"G3": 100},
                    "A3": {"G4": 100},
                }
            )
        )
        assert result.optimal_cost == 300
        assert result.second_cost == 450
        assert result.path_margin == 150


class TestTopologyFallback:
    def test_34_topology_fallback_keeps_trip_id_null(self):
        out = topology_fallback("segX", "2023-11-26", _synthetic_gtfs(), "r1")
        assert out["scheduled_trip_id"] is None
        assert out["status"] == "topology_only"
        assert out["confidence"] == "reduced"
        assert out["topology_source_date"] == "2023-11-28"
        assert out["reason_codes"] == Reason02.TOPOLOGY_ONLY


class TestCensoringAndCoverage:
    def test_36_right_censored_run_is_flagged(self):
        pings = trip_pings("t", 1000, northbound=True)
        runs = build_activity_runs(
            {"v1": pings},
            max_continuity_gap_s=1800,
            slice_wall_start_epoch_s=0,
            slice_wall_end_epoch_s=pings[-1].instant_s + 100,
            censor_margin_s=900,
        )
        assert runs[0].boundary_censored_end
        assert not runs[0].boundary_censored_start

    def test_37_38_apc_only_vehicle_has_no_edges(self):
        pings = trip_pings("t", 1000, northbound=True, vehicle="v1")
        runs = build_activity_runs({"v1": pings}, max_continuity_gap_s=1800)
        ghost = apc_record("ghost", 1500, 1550, vehicle="v99")
        edges = align_apc_records(
            [ghost],
            runs,
            {p.record_id: p for p in pings},
            {},
            [],
            max_time_gap_s=900,
            interpolation_max_gap_s=300,
        )
        assert edges == []


class TestOrderDeterminism:
    def test_40_refinement_processing_order_is_irrelevant(self):
        from r1spike.boundary_refinement import RefinementConfig, refine_run
        from r1spike.terminal_episodes import EpisodeConfig, detect_episodes

        from .spike02_helpers import CLUSTERS
        from .test_spike02_episodes import standard_scenario

        run, pings, edges, signals = standard_scenario()
        episodes = detect_episodes(
            run, pings, CLUSTERS, edges, signals, cfg=EpisodeConfig()
        )
        results = [
            refine_run(
                run,
                pings,
                episodes,
                variant="v2_conditional",
                cfg=RefinementConfig(),
            )
            for _ in range(3)
        ]
        fps = {refinement_fingerprint([r]) for r in results}
        assert len(fps) == 1


class TestPublicationGuards:
    def test_run_dir_prefix_enforced(self, tmp_path):
        with pytest.raises(PublicationGuardError02):
            check_run_dir02(tmp_path / "artifacts" / "experimental-r1-spike-x")
        ok = tmp_path / "artifacts" / "experimental-r1-spike02-x"
        assert check_run_dir02(ok) == ok.resolve()

    def test_database_offline_refused(self, tmp_path):
        with pytest.raises(PublicationGuardError02):
            check_run_dir02(
                tmp_path
                / "database_offline"
                / "artifacts"
                / "experimental-r1-spike02-x"
            )

    def _run(self, tmp_path) -> Spike02Run:
        run_dir = tmp_path / "artifacts" / "experimental-r1-spike02-guard"
        return Spike02Run(
            run_dir,
            config_hash="c",
            contract_hash="s",
            input_hashes={},
            assumptions=[],
            unresolved_design_contracts=[],
        )

    def test_tides_shaped_names_refused(self, tmp_path):
        run = self._run(tmp_path)
        df = pl.DataFrame({"a": [1]})
        for bad in (
            "x_trips_performed.parquet",
            "x_stop_visits.parquet",
            "x_passenger_events.parquet",
            "datapackage.json",
            "x_tides_output.parquet",
            "x_trip_id_performed.parquet",
        ):
            with pytest.raises(PublicationGuardError02):
                if bad.endswith(".parquet"):
                    run.write_parquet(df, bad, grain="g", key=["a"], producer="test")
                else:
                    run.write_json({}, bad)

    def test_non_x_parquet_and_unlisted_json_refused(self, tmp_path):
        run = self._run(tmp_path)
        df = pl.DataFrame({"a": [1]})
        with pytest.raises(PublicationGuardError02):
            run.write_parquet(
                df, "segments.parquet", grain="g", key=["a"], producer="t"
            )
        with pytest.raises(PublicationGuardError02):
            run.write_json({}, "surprise_report.json")

    def test_markers_present_on_every_artifact(self, tmp_path):
        import json

        run = self._run(tmp_path)
        run.write_parquet(
            pl.DataFrame({"a": [1]}),
            "x_probe.parquet",
            grain="g",
            key=["a"],
            producer="test",
        )
        meta = json.loads((run.run_dir / "x_probe.parquet.meta.json").read_text())
        assert meta["profile"] == "diagnostic-experimental-r1-spike-02"
        assert meta["publishable"] is False
        assert meta["tides_conformance_claimed"] is False
        assert meta["normative_contract_claimed"] is False
        assert meta["r1_closed"] is False
        assert meta["production_modules_modified"] is False
        assert meta["producer"] == "test"
