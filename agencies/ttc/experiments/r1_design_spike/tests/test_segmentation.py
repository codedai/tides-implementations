"""Tests for the experimental X4-A/X4-B segmentation baselines.

All epochs are plain integers around ``BASE``; segmentation depends only
on gaps and context fields, never on wall-clock semantics, so nothing
here needs a timezone library.
"""

from __future__ import annotations

import random
import re

import pytest
from r1spike.contracts import Reason
from r1spike.segmentation_baselines import (
    Segment,
    SegPing,
    order_pings,
    segment_vehicle,
)

BASE = 1_700_000_000
SEGMENT_ID_RE = re.compile(r"^xs1-[0-9a-f]{32}$")


def make_ping(
    record_id: str,
    instant_s: int | None,
    *,
    vehicle: str = "veh-1",
    candidate_instants: tuple[int, ...] | None = None,
    fold_status: str = "resolved",
    local_date: str = "2023-11-14",
    route_label: str | None = "39",
    destination_label: str | None = "FINCH STN",
    run_label: str | None = "39-01",
    tatrip_id: str | None = "T-100",
    latitude: float | None = 43.70,
    longitude: float | None = -79.40,
) -> SegPing:
    if candidate_instants is None:
        assert instant_s is not None
        candidate_instants = (instant_s,)
    return SegPing(
        record_id=record_id,
        vehicle=vehicle,
        instant_s=instant_s,
        candidate_instants=candidate_instants,
        fold_status=fold_status,
        local_date=local_date,
        route_label=route_label,
        destination_label=destination_label,
        run_label=run_label,
        tatrip_id=tatrip_id,
        latitude=latitude,
        longitude=longitude,
    )


def record_groupings(segments: list[Segment]) -> list[tuple[str, ...]]:
    return [s.record_ids for s in segments]


class TestX4aGapSplits:
    def test_gap_above_continuity_threshold_splits_with_gap_reason(self) -> None:
        pings = [
            make_ping("r1", BASE),
            make_ping("r2", BASE + 300),
            make_ping("r3", BASE + 600),
            make_ping("r4", BASE + 600 + 601),
        ]
        segments = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
        assert record_groupings(segments) == [("r1", "r2", "r3"), ("r4",)]
        assert Reason.SEG_GAP_SPLIT in segments[1].boundary_reasons

    def test_gap_at_or_below_threshold_does_not_split(self) -> None:
        pings = [
            make_ping("r1", BASE),
            make_ping("r2", BASE + 600),  # exactly the threshold: continuous
            make_ping("r3", BASE + 600 + 599),
        ]
        segments = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
        assert record_groupings(segments) == [("r1", "r2", "r3")]
        assert segments[0].boundary_reasons == ()


class TestCrossMidnightContinuity:
    def test_small_gaps_across_local_date_change_stay_one_segment(self) -> None:
        pings = [
            make_ping("r1", BASE, local_date="2023-11-14"),
            make_ping("r2", BASE + 60, local_date="2023-11-14"),
            make_ping("r3", BASE + 120, local_date="2023-11-15"),
            make_ping("r4", BASE + 180, local_date="2023-11-15"),
        ]
        segments = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
        assert len(segments) == 1
        (segment,) = segments
        assert segment.record_ids == ("r1", "r2", "r3", "r4")
        assert segment.crosses_midnight is True
        assert segment.local_dates == ("2023-11-14", "2023-11-15")


class TestConservativeUncertainGapRule:
    def test_one_candidate_pair_within_threshold_prevents_split(self) -> None:
        # The unresolved ping could be 300 s or 3,900 s away; the minimal
        # possible gap (300) is within the threshold, so no split.
        pings = [
            make_ping("r1", BASE),
            make_ping(
                "r2",
                None,
                candidate_instants=(BASE + 300, BASE + 3_900),
                fold_status="unresolved",
            ),
        ]
        segments = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
        assert record_groupings(segments) == [("r1", "r2")]

    def test_split_when_even_minimal_possible_gap_exceeds_threshold(self) -> None:
        pings = [
            make_ping("r1", BASE),
            make_ping(
                "r2",
                None,
                candidate_instants=(BASE + 700, BASE + 4_300),
                fold_status="unresolved",
            ),
        ]
        segments = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
        assert record_groupings(segments) == [("r1",), ("r2",)]
        assert Reason.SEG_GAP_SPLIT in segments[1].boundary_reasons

    def test_unresolved_pings_mark_segment_time_uncertain_with_reason(self) -> None:
        pings = [
            make_ping("r1", BASE),
            make_ping(
                "r2",
                None,
                candidate_instants=(BASE + 300, BASE + 3_900),
                fold_status="unresolved",
            ),
        ]
        (segment,) = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
        assert segment.time_uncertain is True
        assert segment.n_unresolved == 1
        assert Reason.SEG_TIME_UNCERTAIN in segment.boundary_reasons

    def test_fully_resolved_segment_is_not_time_uncertain(self) -> None:
        pings = [make_ping("r1", BASE), make_ping("r2", BASE + 60)]
        (segment,) = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
        assert segment.time_uncertain is False
        assert segment.n_unresolved == 0
        assert Reason.SEG_TIME_UNCERTAIN not in segment.boundary_reasons


class TestTatripIdNeverPartitions:
    def test_x4a_segmentation_identical_under_churning_tatrip_values(self) -> None:
        # Six pings, a real 900 s gap, then four more; tatripId churns on
        # every ping in one input and is constant in the other. Under
        # x4a the record_id groupings must be byte-identical.
        instants = [BASE + i * 120 for i in range(6)]
        instants += [BASE + 600 + 900 + i * 120 for i in range(4)]
        constant = [
            make_ping(f"r{i}", t, tatrip_id="T-100") for i, t in enumerate(instants)
        ]
        churning = [
            make_ping(f"r{i}", t, tatrip_id=f"T-{i}") for i, t in enumerate(instants)
        ]
        segments_constant = segment_vehicle(
            constant, variant="x4a", max_continuity_gap_s=600
        )
        segments_churning = segment_vehicle(
            churning, variant="x4a", max_continuity_gap_s=600
        )
        assert record_groupings(segments_churning) == record_groupings(
            segments_constant
        )
        assert len(segments_constant) == 2  # only the real gap splits


class TestX4bContextDebounce:
    def test_sustained_route_and_tatrip_change_splits_with_reason(self) -> None:
        # Five pings of (route 39, trip T-1), then five of (52, T-2) at
        # 30 s spacing: the new context is sustained for 5 pings over
        # 120 s, and route+tatrip changing together is 2 votes.
        pings = [
            make_ping(f"a{i}", BASE + i * 30, route_label="39", tatrip_id="T-1")
            for i in range(5)
        ]
        pings += [
            make_ping(f"b{i}", BASE + 150 + i * 30, route_label="52", tatrip_id="T-2")
            for i in range(5)
        ]
        segments = segment_vehicle(pings, variant="x4b", max_continuity_gap_s=600)
        assert record_groupings(segments) == [
            ("a0", "a1", "a2", "a3", "a4"),
            ("b0", "b1", "b2", "b3", "b4"),
        ]
        assert Reason.SEG_CONTEXT_SPLIT in segments[1].boundary_reasons

    def test_single_ping_route_chatter_does_not_split(self) -> None:
        pings = [
            make_ping(f"r{i}", BASE + i * 30, route_label="52" if i == 4 else "39")
            for i in range(10)
        ]
        segments = segment_vehicle(pings, variant="x4b", max_continuity_gap_s=600)
        assert record_groupings(segments) == [tuple(f"r{i}" for i in range(10))]

    def test_sustained_route_change_alone_is_one_vote_and_no_split(self) -> None:
        pings = [make_ping(f"a{i}", BASE + i * 30, route_label="39") for i in range(5)]
        pings += [
            make_ping(f"b{i}", BASE + 150 + i * 30, route_label="52") for i in range(5)
        ]
        segments = segment_vehicle(pings, variant="x4b", max_continuity_gap_s=600)
        assert len(segments) == 1
        assert segments[0].boundary_reasons == ()


class TestSegmentIds:
    def test_ids_carry_xs1_prefix_and_32_hex_digest(self) -> None:
        pings = [make_ping("r1", BASE), make_ping("r2", BASE + 60)]
        (segment,) = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
        assert SEGMENT_ID_RE.fullmatch(segment.segment_id)

    def test_identical_inputs_mint_identical_ids(self) -> None:
        def build() -> list[str]:
            pings = [
                make_ping("r1", BASE),
                make_ping("r2", BASE + 60),
                make_ping("r3", BASE + 2_000),
            ]
            segments = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
            return [s.segment_id for s in segments]

        assert build() == build()

    def test_different_variant_mints_a_different_id_for_same_pings(self) -> None:
        pings = [make_ping("r1", BASE), make_ping("r2", BASE + 60)]
        (seg_a,) = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
        (seg_b,) = segment_vehicle(pings, variant="x4b", max_continuity_gap_s=600)
        assert seg_a.record_ids == seg_b.record_ids
        assert seg_a.segment_id != seg_b.segment_id


class TestOrderPings:
    def test_equal_instants_break_ties_on_record_id(self) -> None:
        pings = [make_ping("r-b", BASE), make_ping("r-a", BASE)]
        ordered = order_pings(pings)
        assert [p.record_id for p in ordered] == ["r-a", "r-b"]

    def test_input_order_never_changes_the_segmentation(self) -> None:
        pings = [make_ping(f"r{i}", BASE + i * 300) for i in range(4)]
        pings += [make_ping(f"s{i}", BASE + 5_000 + i * 300) for i in range(4)]
        shuffled = list(pings)
        random.Random(7).shuffle(shuffled)
        assert shuffled != pings  # the shuffle actually changed the order
        expected = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
        observed = segment_vehicle(shuffled, variant="x4a", max_continuity_gap_s=600)
        assert len(expected) == 2  # the 4,100 s gap splits
        assert record_groupings(observed) == record_groupings(expected)
        assert [s.segment_id for s in observed] == [s.segment_id for s in expected]


class TestSegmentVehicleGuards:
    def test_mixed_vehicle_namespaces_raise(self) -> None:
        pings = [
            make_ping("r1", BASE, vehicle="veh-1"),
            make_ping("r2", BASE + 60, vehicle="veh-2"),
        ]
        with pytest.raises(ValueError, match="exactly one vehicle namespace"):
            segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)

    def test_unknown_variant_raises(self) -> None:
        pings = [make_ping("r1", BASE)]
        with pytest.raises(ValueError, match="unknown segmentation variant"):
            segment_vehicle(pings, variant="s4", max_continuity_gap_s=600)

    def test_invalid_pings_are_excluded_from_every_segment(self) -> None:
        pings = [
            make_ping("r1", BASE),
            make_ping("r2", BASE + 300, fold_status="invalid"),
            make_ping("r3", BASE + 500),
        ]
        (segment,) = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
        assert segment.record_ids == ("r1", "r3")

    def test_all_invalid_pings_yield_no_segments(self) -> None:
        pings = [make_ping("r1", BASE, fold_status="invalid")]
        assert segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600) == []
