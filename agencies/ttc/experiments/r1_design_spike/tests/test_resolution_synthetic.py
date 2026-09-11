"""Synthetic end-to-end tests of the staged resolution engine.

Covers required core tests 10 (caps fail closed), 13 (exactly one
disposition per record), 18/19 (window schedule invariance), and 20
(Stage-3 demotion returns an attachment-selected fold to unresolved).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from r1spike.candidate_generation import ApcRecord
from r1spike.contracts import (
    NO_ATTACHMENT,
    NO_ROUTE_MAPPING,
    NO_SCHEDULED_TRIP,
    UNRESOLVED_SERVICE_DATE,
    ApcCandidate,
    DispositionStatus,
    EvidenceRegime,
    SegmentCandidate,
)
from r1spike.inputs import GtfsForDate
from r1spike.resolution import ResolutionConfig, resolve
from r1spike.scoring import load_contract
from r1spike.segmentation_baselines import Segment
from r1spike.windows import build_windows, window_order

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "experimental_score_contract.yaml"
)

BASE = 1_700_000_000
DATE = "2023-11-28"
FEED = "feedX"


@pytest.fixture(scope="module")
def contract():
    return load_contract(CONTRACT_PATH, "conservative")


def make_config(contract, **overrides) -> ResolutionConfig:
    defaults = dict(
        contract=contract,
        stage1_policy="policy_a",
        policy_b_min_support_records=2,
        segment_hypothesis_cap=24,
        apc_candidate_cap=16,
        component_record_cap=400,
    )
    defaults.update(overrides)
    return ResolutionConfig(**defaults)


def make_segment(seg_id="xs1-" + "a" * 32, vehicle="1000", start=BASE):
    return Segment(
        segment_id=seg_id,
        variant="x4a",
        vehicle=vehicle,
        record_ids=("a1", "a2"),
        start_instant_s=start,
        end_instant_s=start + 3600,
        time_uncertain=False,
        n_unresolved=0,
        local_dates=(DATE,),
        route_label_counts={"29": 2},
    )


def seg_candidates(seg_id, *, trip="trip1"):
    good = SegmentCandidate(
        segment_id=seg_id,
        service_date_hypothesis=DATE,
        feed_id=FEED,
        route_mapping_hypothesis="r29",
        scheduled_trip_hypothesis=trip,
        evidence_regime=EvidenceRegime.AVL_ONLY,
        feature_vector={
            "seg_sched_start_residual_s": 100,
            "seg_sched_end_residual_s": 100,
            "seg_interval_overlap_millis": 950,
            "seg_duration_ratio_millis": 950,
            "seg_route_mapping_confidence_millis": 1000,
            "seg_date_key_agreement_millis": 1000,
        },
    )
    no_trip = SegmentCandidate(
        segment_id=seg_id,
        service_date_hypothesis=DATE,
        feed_id=FEED,
        route_mapping_hypothesis="r29",
        scheduled_trip_hypothesis=NO_SCHEDULED_TRIP,
        evidence_regime=EvidenceRegime.AVL_ONLY,
        feature_vector={
            "seg_route_mapping_confidence_millis": 1000,
            "seg_date_key_agreement_millis": 1000,
        },
    )
    null = SegmentCandidate(
        segment_id=seg_id,
        service_date_hypothesis=UNRESOLVED_SERVICE_DATE,
        feed_id=None,
        route_mapping_hypothesis=NO_ROUTE_MAPPING,
        scheduled_trip_hypothesis=NO_SCHEDULED_TRIP,
        evidence_regime=EvidenceRegime.AVL_ONLY,
        feature_vector={},
    )
    return [good, no_trip, null]


def apc_record(rid, *, vehicle="1000", ambiguous=False, arrival=BASE + 1000):
    if ambiguous:
        arrival_cands = ((0, arrival), (1, arrival + 3600))
    else:
        arrival_cands = ((0, arrival),)
    return ApcRecord(
        record_id=rid,
        vehicle=vehicle,
        arrival_wall="2023-11-28 10:00:00",
        departure_wall=None,
        local_date=DATE,
        route_label="29",
        branch_label=None,
        direction_label="NORTH",
        stop_code="123",
        latitude=43.65,
        longitude=-79.4,
        arrival_candidates=arrival_cands,
        departure_candidates=arrival_cands,
    )


def att_features(*, gap=0, overlap=1000, nearest=30):
    return {
        "att_interval_gap_s": gap,
        "att_interval_overlap_millis": overlap,
        "att_nearest_ping_gap_s": nearest,
        "att_route_agreement_millis": 1000,
        "att_stop_mapping_confidence_millis": 1000,
        "att_date_key_agreement_millis": 1000,
    }


def apc_candidate(rid, seg_id, *, fold=0, stop="st1", features=None):
    return ApcCandidate(
        apc_source_record_id=rid,
        time_fold_hypothesis=fold,
        service_date_hypothesis=DATE,
        feed_id=FEED,
        vehicle_mapping_hypothesis="1000",
        route_mapping_hypothesis="r29",
        stop_mapping_hypothesis=stop,
        candidate_segment_id=seg_id,
        feature_vector=features or att_features(),
    )


def null_candidate(rid):
    return ApcCandidate(
        apc_source_record_id=rid,
        time_fold_hypothesis=None,
        service_date_hypothesis=UNRESOLVED_SERVICE_DATE,
        feed_id=None,
        vehicle_mapping_hypothesis=None,
        route_mapping_hypothesis=NO_ROUTE_MAPPING,
        stop_mapping_hypothesis=NO_ATTACHMENT,
        candidate_segment_id=NO_ATTACHMENT,
        feature_vector={},
    )


def make_gtfs(stop_ids_times: list[tuple[str, int]], trip="trip1"):
    events = pl.DataFrame(
        {
            "trip_id": [trip] * len(stop_ids_times),
            "stop_sequence": list(range(1, len(stop_ids_times) + 1)),
            "stop_id": [s for s, _ in stop_ids_times],
            "arrival_epoch_s": [t for _, t in stop_ids_times],
            "departure_epoch_s": [t for _, t in stop_ids_times],
        }
    )
    empty = pl.DataFrame()
    return GtfsForDate(
        service_date=DATE,
        feed_id=FEED,
        artifact_dir=Path("."),
        routes=empty,
        trip_intervals=empty,
        scheduled_stop_events=events,
        stops=empty,
        shapes=empty,
        dominant_shape_by_route={},
    )


def run_resolve(
    contract,
    *,
    segments,
    apc_records,
    segment_candidates,
    apc_candidates,
    gtfs_by_date=None,
    cfg=None,
    order="forward",
    hours=24,
):
    windows = build_windows(BASE - 86400, BASE + 86400, hours=hours)
    schedule = window_order(len(windows), order, seed=7)
    return resolve(
        segments=segments,
        pings_by_id={},
        apc_records=apc_records,
        segment_candidates=segment_candidates,
        apc_candidates=apc_candidates,
        gtfs_by_date=gtfs_by_date or {},
        cfg=cfg or make_config(contract),
        windows=windows,
        schedule=schedule,
    )


class TestBasicFlow:
    def test_segment_accepts_and_record_attaches(self, contract):
        seg = make_segment()
        rec = apc_record("c1-r1")
        result = run_resolve(
            contract,
            segments=[seg],
            apc_records=[rec],
            segment_candidates={seg.segment_id: seg_candidates(seg.segment_id)},
            apc_candidates={
                "c1-r1": [
                    apc_candidate("c1-r1", seg.segment_id),
                    null_candidate("c1-r1"),
                ]
            },
        )
        assert result.segment_decisions[seg.segment_id].status == "accepted"
        assert result.dispositions["c1-r1"] == "accepted"

    def test_every_record_gets_exactly_one_disposition(self, contract):
        """Required test 13, including a no-candidate record."""
        seg = make_segment()
        records = [
            apc_record("c1-r1"),
            apc_record("c1-r2", vehicle="7027"),  # APC-only vehicle
            apc_record("c1-r3"),
        ]
        result = run_resolve(
            contract,
            segments=[seg],
            apc_records=records,
            segment_candidates={seg.segment_id: seg_candidates(seg.segment_id)},
            apc_candidates={
                "c1-r1": [
                    apc_candidate("c1-r1", seg.segment_id),
                    null_candidate("c1-r1"),
                ],
                "c1-r2": [null_candidate("c1-r2")],
                "c1-r3": [
                    apc_candidate(
                        "c1-r3",
                        seg.segment_id,
                        features=att_features(gap=2600, overlap=0, nearest=2600),
                    ),
                    null_candidate("c1-r3"),
                ],
            },
        )
        assert sorted(result.dispositions) == ["c1-r1", "c1-r2", "c1-r3"]
        valid = {s.value for s in DispositionStatus}
        assert set(result.dispositions.values()) <= valid
        # the APC-only vehicle has no non-null candidate at all
        assert result.dispositions["c1-r2"] == "no_candidate"

    def test_attachment_blocked_when_segment_unresolved(self, contract):
        """A record whose only candidate segment abstained upstream is
        policy-blocked, not silently attached."""
        seg = make_segment()
        weak = [
            c
            for c in seg_candidates(seg.segment_id)
            if c.scheduled_trip_hypothesis == NO_SCHEDULED_TRIP
        ]
        # only the null and a weak no-trip tuple with poor mapping: below
        # the absolute bar -> segment abstains
        weak[0].feature_vector["seg_route_mapping_confidence_millis"] = 0
        weak[0].feature_vector["seg_date_key_agreement_millis"] = 0
        result = run_resolve(
            contract,
            segments=[seg],
            apc_records=[apc_record("c1-r1")],
            segment_candidates={seg.segment_id: weak},
            apc_candidates={
                "c1-r1": [
                    apc_candidate("c1-r1", seg.segment_id),
                    null_candidate("c1-r1"),
                ]
            },
        )
        assert result.segment_decisions[seg.segment_id].status in {
            "low_score",
            "null_winner",
        }
        assert result.dispositions["c1-r1"] == "policy_blocked"


class TestCapsFailClosed:
    def test_apc_candidate_cap_overflow(self, contract):
        """Required test 10 (APC half): cap overflow -> unresolved."""
        seg_a = make_segment()
        seg_b = make_segment(seg_id="xs1-" + "b" * 32, start=BASE + 100)
        seg_c = make_segment(seg_id="xs1-" + "c" * 32, start=BASE + 200)
        cfg = make_config(contract, apc_candidate_cap=2)
        result = run_resolve(
            contract,
            segments=[seg_a, seg_b, seg_c],
            apc_records=[apc_record("c1-r1")],
            segment_candidates={
                s.segment_id: seg_candidates(s.segment_id)
                for s in (seg_a, seg_b, seg_c)
            },
            apc_candidates={
                "c1-r1": [
                    apc_candidate("c1-r1", seg_a.segment_id),
                    apc_candidate("c1-r1", seg_b.segment_id),
                    apc_candidate("c1-r1", seg_c.segment_id),
                    null_candidate("c1-r1"),
                ]
            },
            cfg=cfg,
        )
        assert result.dispositions["c1-r1"] == "cap_overflow"
        assert "XR1.CAP.APC_CANDIDATE_OVERFLOW" in (result.disposition_reasons["c1-r1"])

    def test_segment_hypothesis_cap_overflow(self, contract):
        cands = seg_candidates(make_segment().segment_id)
        # inflate distinct trip hypotheses beyond the cap
        seg = make_segment()
        many = [
            SegmentCandidate(
                segment_id=seg.segment_id,
                service_date_hypothesis=DATE,
                feed_id=FEED,
                route_mapping_hypothesis="r29",
                scheduled_trip_hypothesis=f"trip{i}",
                evidence_regime=EvidenceRegime.AVL_ONLY,
                feature_vector=dict(cands[0].feature_vector),
            )
            for i in range(4)
        ] + cands
        cfg = make_config(contract, segment_hypothesis_cap=3)
        result = run_resolve(
            contract,
            segments=[seg],
            apc_records=[],
            segment_candidates={seg.segment_id: many},
            apc_candidates={},
            cfg=cfg,
        )
        assert result.segment_decisions[seg.segment_id].status == "cap_overflow"

    def test_component_cap_makes_component_unresolved(self, contract):
        seg = make_segment()
        cfg = make_config(contract, component_record_cap=1)
        result = run_resolve(
            contract,
            segments=[seg],
            apc_records=[apc_record("c1-r1"), apc_record("c1-r2")],
            segment_candidates={seg.segment_id: seg_candidates(seg.segment_id)},
            apc_candidates={
                "c1-r1": [
                    apc_candidate("c1-r1", seg.segment_id),
                    null_candidate("c1-r1"),
                ],
                "c1-r2": [
                    apc_candidate("c1-r2", seg.segment_id),
                    null_candidate("c1-r2"),
                ],
            },
            cfg=cfg,
        )
        assert result.segment_decisions[seg.segment_id].status == "cap_overflow"
        assert result.dispositions["c1-r1"] == "cap_overflow"
        assert result.dispositions["c1-r2"] == "cap_overflow"


class TestWindowScheduleInvariance:
    def test_orders_and_window_sizes_agree(self, contract):
        """Required tests 18/19: execution layout never changes logic."""
        seg_a = make_segment()
        seg_b = make_segment(seg_id="xs1-" + "b" * 32, start=BASE + 40 * 3600)
        records = [
            apc_record("c1-r1"),
            apc_record("c1-r2", arrival=BASE + 40 * 3600 + 500),
        ]
        kwargs = dict(
            segments=[seg_a, seg_b],
            apc_records=records,
            segment_candidates={
                s.segment_id: seg_candidates(s.segment_id) for s in (seg_a, seg_b)
            },
            apc_candidates={
                "c1-r1": [
                    apc_candidate("c1-r1", seg_a.segment_id),
                    null_candidate("c1-r1"),
                ],
                "c1-r2": [
                    apc_candidate("c1-r2", seg_b.segment_id),
                    null_candidate("c1-r2"),
                ],
            },
        )
        baseline = run_resolve(contract, order="forward", hours=24, **kwargs)
        for order, hours in [
            ("reverse", 24),
            ("shuffled", 24),
            ("forward", 12),
            ("reverse", 12),
        ]:
            other = run_resolve(contract, order=order, hours=hours, **kwargs)
            assert other.logical_fingerprint() == baseline.logical_fingerprint(), (
                f"layout {order}/{hours}h changed logical output"
            )


class TestStage3FoldRollback:
    def test_demotion_returns_attachment_selected_fold_to_unresolved(self, contract):
        """Required test 20: the fold selected solely by an attachment
        is rolled back when Stage 3 demotes that attachment."""
        seg = make_segment()
        rec_bad = apc_record("c1-amb", ambiguous=True)
        rec_ok = apc_record("c1-ok", arrival=BASE + 400)
        gtfs = make_gtfs([("st1", BASE + 500), ("st2", BASE + 2000)])
        result = run_resolve(
            contract,
            segments=[seg],
            apc_records=[rec_bad, rec_ok],
            segment_candidates={seg.segment_id: seg_candidates(seg.segment_id)},
            apc_candidates={
                "c1-amb": [
                    apc_candidate("c1-amb", seg.segment_id, fold=0, stop="st9"),
                    apc_candidate(
                        "c1-amb",
                        seg.segment_id,
                        fold=1,
                        stop="st9",
                        features=att_features(gap=1200, overlap=0, nearest=1200),
                    ),
                    null_candidate("c1-amb"),
                ],
                "c1-ok": [
                    apc_candidate("c1-ok", seg.segment_id, stop="st1"),
                    null_candidate("c1-ok"),
                ],
            },
            gtfs_by_date={DATE: gtfs},
        )
        # fold 0 won the attachment before Stage 3
        assert result.apc_decisions["c1-amb"].status == "accepted"
        # st9 is not in the scheduled sequence -> demoted -> rollback
        assert result.dispositions["c1-amb"] == "sequence_demoted"
        assert "c1-amb" in result.fold_rollbacks
        assert "c1-amb" not in result.fold_selected_by_attachment
        assert "XR1.TIME.FOLD_ROLLED_BACK" in (result.disposition_reasons["c1-amb"])
        # the consistent record survives Stage 3
        assert result.dispositions["c1-ok"] == "accepted"

    def test_time_uncertain_segment_blocks_timestamped_attachment(self, contract):
        seg = Segment(
            segment_id="xs1-" + "d" * 32,
            variant="x4a",
            vehicle="1000",
            record_ids=("a1", "a2"),
            start_instant_s=BASE,
            end_instant_s=BASE + 3600,
            time_uncertain=True,
            n_unresolved=2,
            local_dates=(DATE,),
            route_label_counts={"29": 2},
        )
        result = run_resolve(
            contract,
            segments=[seg],
            apc_records=[apc_record("c1-r1")],
            segment_candidates={seg.segment_id: seg_candidates(seg.segment_id)},
            apc_candidates={
                "c1-r1": [
                    apc_candidate("c1-r1", seg.segment_id),
                    null_candidate("c1-r1"),
                ]
            },
        )
        assert result.segment_decisions[seg.segment_id].status == "time_unresolved"
        assert result.dispositions["c1-r1"] == "time_unresolved"
