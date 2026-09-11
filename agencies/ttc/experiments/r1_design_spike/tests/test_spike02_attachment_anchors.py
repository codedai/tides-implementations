"""Spike-02 adaptive attachment and GTFS anchor tests (required tests
23, 24, 27, 28, 29, plus cluster rescue and polarity abstention)."""

from __future__ import annotations

from pathlib import Path

from r1spike.adaptive_attachment import (
    AttachmentConfig,
    TerminalContext,
    apply_t_mid,
    generate_candidates02,
)
from r1spike.contracts import (
    ApcCandidate,
    EvidenceRegime,
    HardValidity,
    SegmentCandidate,
)
from r1spike.gtfs_anchors import (
    AnchorCandidate,
    AnchorConfig,
    anchor_candidates_for_segment,
    decide_anchor,
)
from r1spike.segmentation_baselines import Segment
from r1spike.spike02_contracts import Reason02
from r1spike.spike02_scoring import load_spike02_contract

from .spike02_helpers import (
    CLUSTERS,
    LON,
    NORTH_LAT,
    SOUTH_LAT,
    apc_record,
    trip_pings,
)
from .test_spike02_signals_clusters import _synthetic_gtfs

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _cand(rid, seg, *, gap, overlap, reasons=()):
    return ApcCandidate(
        apc_source_record_id=rid,
        time_fold_hypothesis=0,
        service_date_hypothesis="2023-11-28",
        feed_id="feedX",
        vehicle_mapping_hypothesis="v1",
        route_mapping_hypothesis="r1",
        stop_mapping_hypothesis="s1",
        candidate_segment_id=seg,
        feature_vector={
            "att_interval_gap_s": gap,
            "att_interval_overlap_millis": overlap,
        },
        reason_codes=tuple(reasons),
    )


def _null(rid):
    return ApcCandidate(
        apc_source_record_id=rid,
        time_fold_hypothesis=None,
        service_date_hypothesis="XNULL:UNRESOLVED_SERVICE_DATE",
        feed_id=None,
        vehicle_mapping_hypothesis=None,
        route_mapping_hypothesis="XNULL:NO_ROUTE_MAPPING",
        stop_mapping_hypothesis="XNULL:NO_ATTACHMENT",
        candidate_segment_id="XNULL:NO_ATTACHMENT",
        feature_vector={},
    )


class TestTMid:
    def test_23_mid_route_t_mid_sensitivity(self):
        cands = {
            "rec": [
                _cand("rec", "segA", gap=0, overlap=800),
                _cand("rec", "segB", gap=1500, overlap=0),
                _null("rec"),
            ]
        }
        tight, pruned_tight = apply_t_mid(cands, {}, t_mid_s=1200)
        loose, pruned_loose = apply_t_mid(cands, {}, t_mid_s=1800)
        tight_segs = {
            c.candidate_segment_id for c in tight["rec"] if not c.is_null_alternative()
        }
        loose_segs = {
            c.candidate_segment_id for c in loose["rec"] if not c.is_null_alternative()
        }
        assert tight_segs == {"segA"}
        assert loose_segs == {"segA", "segB"}
        assert len(pruned_tight) == 1
        assert pruned_tight[0]["reason"] == Reason02.ATTACH_TMID_PRUNED
        assert not pruned_loose

    def test_24_terminal_record_bypasses_t_mid(self):
        ctx = {
            "rec": TerminalContext(
                episode_id="ep1",
                cluster_id="cl-north",
                arrival_segment_id="segB",
                departure_segment_id="segC",
                spanning=True,
            )
        }
        cands = {
            "rec": [
                _cand("rec", "segB", gap=1500, overlap=0),
                _cand("rec", "segC", gap=1600, overlap=0),
                _cand("rec", "segD", gap=1700, overlap=0),
                _null("rec"),
            ]
        }
        filtered, pruned = apply_t_mid(cands, ctx, t_mid_s=1200)
        kept = {
            c.candidate_segment_id
            for c in filtered["rec"]
            if not c.is_null_alternative()
        }
        assert kept == {"segB", "segC"}  # episode-adjacent kept, segD pruned
        assert {row["candidate_segment_id"] for row in pruned} == {"segD"}
        terminal_kept = [
            c for c in filtered["rec"] if c.candidate_segment_id in {"segB", "segC"}
        ]
        assert all(
            Reason02.ATTACH_TERMINAL_PATH in c.reason_codes for c in terminal_kept
        )


class TestClusterRescue:
    def test_unmapped_stop_in_cluster_gains_experimental_candidates(self):
        pings = trip_pings("t", 1000, northbound=True)
        pings_by_id = {p.record_id: p for p in pings}
        seg = Segment(
            segment_id="seg1",
            variant="xrf2-test",
            vehicle="v1",
            record_ids=tuple(p.record_id for p in pings),
            start_instant_s=pings[0].instant_s,
            end_instant_s=pings[-1].instant_s,
            time_uncertain=False,
            n_unresolved=0,
            local_dates=("2023-11-28",),
            route_label_counts={"929": len(pings)},
        )
        # stop code 1652 is NOT in the synthetic GTFS stops
        record = apc_record(
            "wilson",
            pings[-1].instant_s - 60,
            pings[-1].instant_s,
            lat=NORTH_LAT,
            stop_code="1652",
        )
        gtfs = _synthetic_gtfs()
        cands = generate_candidates02(
            [record],
            [seg],
            pings_by_id,
            {"2023-11-28": gtfs},
            lambda *a, **k: None,
            CLUSTERS,
            service_date_candidates=["2023-11-28"],
            candidate_prefilter_gap_s=7200,
            interpolation_max_gap_s=300,
            cfg=AttachmentConfig(),
        )["wilson"]
        rescued = [
            c for c in cands if c.stop_mapping_hypothesis.startswith("XCLUSTER:")
        ]
        assert rescued
        assert all(c.hard_validity == HardValidity.VALID for c in rescued)
        assert all(
            Reason02.CLUSTER_EXPERIMENTAL_1652 in c.reason_codes for c in rescued
        )
        # the original hard-invalid unmapped tuples are retained too
        assert any(
            c.stop_mapping_hypothesis == "XR1:STOP_UNMAPPED"
            and c.hard_validity == HardValidity.HARD_INVALID
            for c in cands
        )

    def test_unmapped_stop_outside_cluster_gets_no_rescue(self):
        pings = trip_pings("t", 1000, northbound=True)
        pings_by_id = {p.record_id: p for p in pings}
        seg = Segment(
            segment_id="seg1",
            variant="xrf2-test",
            vehicle="v1",
            record_ids=tuple(p.record_id for p in pings),
            start_instant_s=pings[0].instant_s,
            end_instant_s=pings[-1].instant_s,
            time_uncertain=False,
            n_unresolved=0,
            local_dates=("2023-11-28",),
            route_label_counts={"929": len(pings)},
        )
        mid_lat = (NORTH_LAT + SOUTH_LAT) / 2
        record = apc_record(
            "midstop",
            1500,
            1560,
            lat=mid_lat,
            stop_code="9999",
        )
        cands = generate_candidates02(
            [record],
            [seg],
            pings_by_id,
            {"2023-11-28": _synthetic_gtfs()},
            lambda *a, **k: None,
            CLUSTERS,
            service_date_candidates=["2023-11-28"],
            candidate_prefilter_gap_s=7200,
            interpolation_max_gap_s=300,
            cfg=AttachmentConfig(),
        )["midstop"]
        assert not any(c.stop_mapping_hypothesis.startswith("XCLUSTER:") for c in cands)


def _corridor_segment(pings):
    return Segment(
        segment_id="segT",
        variant="xrf2-test",
        vehicle="v1",
        record_ids=tuple(p.record_id for p in pings),
        start_instant_s=pings[0].instant_s,
        end_instant_s=pings[-1].instant_s,
        time_uncertain=False,
        n_unresolved=0,
        local_dates=("2023-11-28",),
        route_label_counts={"929": len(pings)},
    )


def _spike02_contract():
    return load_spike02_contract(
        CONFIG_DIR / "experimental_score_contract.yaml",
        CONFIG_DIR / "spike02_score_contract_ext.yaml",
        "conservative",
    )


class TestAnchorStopFeatures:
    def _run(self, records, *, gtfs=None):

        gtfs = gtfs or _synthetic_gtfs()
        # scheduled trip t1: sA(0) sM(600) sB(1200); segment covering it
        pings = trip_pings("t", 0, northbound=True, n=21, step_s=60)
        seg = _corridor_segment(pings)
        pings_by_id = {p.record_id: p for p in pings}
        contract = _spike02_contract()
        cands = anchor_candidates_for_segment(
            seg,
            records,
            pings_by_id,
            {"2023-11-28": gtfs},
            lambda *a, **k: None,
            (None, None),
            contract,
            service_date_candidates=["2023-11-28"],
            schedule_window_expansion_s=1800,
            soft_bound_m=50.0,
            cfg=AnchorConfig(shortlist_overlap_required=False),
            clusters=CLUSTERS,
        )
        return [
            c
            for c in cands
            if c.base.scheduled_trip_hypothesis == "t1" and c.polarity is None
        ]

    def test_27_loop_repeated_stop_uses_occurrences(self):
        import polars as pl

        gtfs = _synthetic_gtfs()
        # make t1 a loop: sA, sM, sA
        events = pl.DataFrame(
            {
                "trip_id": ["t1", "t1", "t1"],
                "stop_id": ["sA", "sM", "sA"],
                "stop_sequence": [1, 2, 3],
                "arrival_epoch_s": [0, 600, 1200],
                "departure_epoch_s": [0, 600, 1200],
                "route_short_name": ["929"] * 3,
            }
        )
        gtfs = type(gtfs)(
            **{
                **{f: getattr(gtfs, f) for f in gtfs.__dataclass_fields__},
                "scheduled_stop_events": events,
            }
        )
        records = [
            apc_record("v1a", 5, 10, lat=SOUTH_LAT, stop_code="100"),
            apc_record("v1m", 590, 600, lat=SOUTH_LAT + 0.025, stop_code="200"),
            apc_record("v1a2", 1190, 1200, lat=SOUTH_LAT, stop_code="100"),
        ]
        cands = self._run(records, gtfs=gtfs)
        assert cands
        c = cands[0]
        # both visits to the repeated stop matched to DISTINCT occurrences
        assert c.n_aligned == 3

    def test_28_skipped_scheduled_stop_counted(self):
        records = [
            apc_record("va", 5, 10, lat=SOUTH_LAT, stop_code="100"),
            apc_record("vb", 1195, 1200, lat=NORTH_LAT, stop_code="300"),
        ]
        cands = self._run(records)
        c = cands[0]
        assert c.n_aligned == 2
        skipped = c.base.feature_vector.get("anchor_skipped_sched_frac_millis")
        assert skipped is not None and skipped > 0  # sM skipped in span

    def test_29_extra_observed_stop_counted(self):
        records = [
            apc_record("va", 5, 10, lat=SOUTH_LAT, stop_code="100"),
            apc_record("vx", 400, 410, lat=SOUTH_LAT + 0.01, stop_code="9999"),
            apc_record("vb", 1195, 1200, lat=NORTH_LAT, stop_code="300"),
        ]
        cands = self._run(records)
        c = cands[0]
        # the unmapped extra stop is excluded from visits (counted as
        # excluded), so aligned stays 2 and extra frac stays 0 — the
        # EXTRA case needs a mapped stop that is not on the trip:
        assert c.n_aligned == 2

    def test_29b_mapped_stop_not_on_trip_is_extra(self):
        import polars as pl

        gtfs = _synthetic_gtfs()
        stops = pl.DataFrame(
            {
                "stop_id": ["sA", "sM", "sB", "sZ"],
                "stop_code": ["100", "200", "300", "400"],
                "stop_lat": [SOUTH_LAT, 43.725, NORTH_LAT, 43.73],
                "stop_lon": [LON] * 4,
            }
        )
        gtfs = type(gtfs)(
            **{
                **{f: getattr(gtfs, f) for f in gtfs.__dataclass_fields__},
                "stops": stops,
            }
        )
        records = [
            apc_record("va", 5, 10, lat=SOUTH_LAT, stop_code="100"),
            apc_record("vz", 700, 710, lat=43.73, stop_code="400"),
            apc_record("vb", 1195, 1200, lat=NORTH_LAT, stop_code="300"),
        ]
        cands = self._run(records, gtfs=gtfs)
        c = cands[0]
        extra = c.base.feature_vector.get("anchor_extra_stop_frac_millis")
        assert c.n_aligned == 2
        assert extra is not None and extra > 0


class TestPolarityAbstention:
    def test_equal_evidence_between_polarities_ties(self):
        contract = _spike02_contract()

        def cand(trip, polarity, q):
            return AnchorCandidate(
                base=SegmentCandidate(
                    segment_id="segT",
                    service_date_hypothesis="2023-11-28",
                    feed_id="feedX",
                    route_mapping_hypothesis="r1",
                    scheduled_trip_hypothesis=trip,
                    evidence_regime=EvidenceRegime.AVL_ONLY,
                    feature_vector={},
                ),
                polarity=polarity,
                qscore=q,
                hard_invalid=False,
                n_aligned=6,
                n_observed=6,
                n_alignment_optima=1,
                reason_codes=(),
            )

        null_cand = AnchorCandidate(
            base=SegmentCandidate(
                segment_id="segT",
                service_date_hypothesis="XNULL:UNRESOLVED_SERVICE_DATE",
                feed_id=None,
                route_mapping_hypothesis="XNULL:NO_ROUTE_MAPPING",
                scheduled_trip_hypothesis="XNULL:NO_SCHEDULED_TRIP",
                evidence_regime=EvidenceRegime.AVL_ONLY,
                feature_vector={},
            ),
            polarity=None,
            qscore=None,
            hard_invalid=False,
            n_aligned=0,
            n_observed=0,
            n_alignment_optima=0,
            reason_codes=(),
        )
        result = decide_anchor(
            "segT",
            [
                cand("t1", "pol_a", 800_000),
                cand("t1", "pol_b", 650_000),
                cand("t2", "pol_a", 650_000),
                cand("t2", "pol_b", 800_000),
                null_cand,
            ],
            contract,
            cfg=AnchorConfig(),
            null_score_q=400_000,
        )
        assert result.status == "evidence_tie"
        assert not result.is_anchor
