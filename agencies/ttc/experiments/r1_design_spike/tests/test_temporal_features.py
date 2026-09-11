"""Tests for raw temporal evidence features (``r1spike.temporal_features``).

The module computes raw values only; hard bounds live in the score
contract. The legacy 9,083-second stale case therefore flows through
here as a plain large gap and is shown to die at the contract's hard
bound (``XR1.TEMPORAL.STALE``) in the conservative variant.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from r1spike.scoring import load_contract
from r1spike.temporal_features import (
    agreement_millis,
    interpolate_between_pings,
    interval_gap_s,
    interval_overlap_millis,
    interval_overlap_s,
    nearest_event_gap_s,
    scheduled_residual_s,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "experimental_score_contract.yaml"
)

# (instant_s, lat, lon), sorted; the 200 -> 1300 bracket is 1100 s wide.
PINGS = [
    (100, 10.0, 20.0),
    (200, 12.0, 24.0),
    (1300, 13.0, 25.0),
]


class TestIntervalGap:
    def test_overlapping_intervals_have_zero_gap(self) -> None:
        assert interval_gap_s(100, 200, 150, 250) == 0

    def test_apc_interval_after_segment_measures_forward_gap(self) -> None:
        assert interval_gap_s(300, 400, 100, 200) == 100

    def test_apc_interval_before_segment_measures_backward_gap(self) -> None:
        assert interval_gap_s(0, 50, 100, 200) == 50

    def test_touching_intervals_have_zero_gap(self) -> None:
        assert interval_gap_s(200, 300, 100, 200) == 0

    def test_legacy_9083_second_stale_case_flows_through_as_raw_gap(self) -> None:
        s0, s1 = 1_700_000_000, 1_700_003_600
        a0 = s1 + 9_083  # the legacy backward as-of match distance
        a1 = a0 + 60
        assert interval_gap_s(a0, a1, s0, s1) == 9_083

    def test_legacy_9083_second_gap_dies_at_contract_hard_bound(self) -> None:
        contract = load_contract(CONTRACT_PATH, "conservative")
        spec = contract.features["att_interval_gap_s"]
        qscore, reason, hard_invalid = contract.feature_qscore(spec, 9_083)
        assert hard_invalid is True
        assert reason == "XR1.TEMPORAL.STALE"
        assert qscore is None


class TestIntervalOverlap:
    def test_partial_overlap_length(self) -> None:
        assert interval_overlap_s(100, 200, 150, 250) == 50

    def test_disjoint_intervals_overlap_zero(self) -> None:
        assert interval_overlap_s(0, 50, 100, 200) == 0

    def test_contained_interval_overlaps_its_own_length(self) -> None:
        assert interval_overlap_s(120, 180, 100, 200) == 60


class TestIntervalOverlapMillis:
    def test_full_containment_scores_1000(self) -> None:
        assert interval_overlap_millis(120, 180, 100, 200) == 1000

    def test_zero_length_apc_dwell_stays_defined_via_floored_denominator(self) -> None:
        # A zero-length dwell inside the segment: the declared shorter-
        # interval denominator max(1, min(0, 100)) floors to 1 second, so
        # the result is min(1000, 0 * 1000 // 1) == 0, not a crash.
        assert interval_overlap_millis(150, 150, 100, 200) == 0

    def test_half_overlap_of_the_shorter_interval_scores_500(self) -> None:
        # APC [150, 250] vs segment [100, 200]: overlap 50 of shorter 100.
        assert interval_overlap_millis(150, 250, 100, 200) == 500

    def test_identical_intervals_hit_the_1000_cap_exactly(self) -> None:
        assert interval_overlap_millis(100, 200, 100, 200) == 1000


class TestNearestEventGap:
    def test_empty_instant_list_is_missing_not_zero(self) -> None:
        assert nearest_event_gap_s(100, []) is None

    def test_nearest_instant_wins_regardless_of_side(self) -> None:
        assert nearest_event_gap_s(100, [40, 130, 300]) == 30
        assert nearest_event_gap_s(100, [90, 115]) == 10

    def test_exact_hit_has_zero_gap(self) -> None:
        assert nearest_event_gap_s(100, [100, 500]) == 0


class TestInterpolateBetweenPings:
    def test_event_before_first_ping_is_never_extrapolated(self) -> None:
        assert interpolate_between_pings(50, PINGS, max_gap_s=600) is None

    def test_event_after_last_ping_is_never_extrapolated(self) -> None:
        assert interpolate_between_pings(2_000, PINGS, max_gap_s=600) is None

    def test_bracketing_gap_beyond_max_gap_yields_missing(self) -> None:
        # Event 700 is bracketed by pings 200 and 1300: an 1100 s bracket,
        # wider than max_gap_s, so the evidence stays missing.
        assert interpolate_between_pings(700, PINGS, max_gap_s=600) is None

    def test_exact_midpoint_interpolates_linearly(self) -> None:
        interp = interpolate_between_pings(150, PINGS, max_gap_s=600)
        assert interp is not None
        assert interp.lat == pytest.approx(11.0)
        assert interp.lon == pytest.approx(22.0)
        assert interp.prev_gap_s == 50
        assert interp.next_gap_s == 50

    def test_event_exactly_on_a_ping_degenerates_to_that_ping(self) -> None:
        # prev and next are the same ping, so t1 == t0: the ping's own
        # position comes back with zero gaps on both sides.
        interp = interpolate_between_pings(200, PINGS, max_gap_s=600)
        assert interp is not None
        assert (interp.lat, interp.lon) == (12.0, 24.0)
        assert interp.prev_gap_s == 0
        assert interp.next_gap_s == 0

    def test_empty_ping_list_yields_missing(self) -> None:
        assert interpolate_between_pings(100, [], max_gap_s=600) is None


class TestScheduledResidual:
    def test_none_observed_propagates_missing(self) -> None:
        assert scheduled_residual_s(None, 100) is None

    def test_none_scheduled_propagates_missing(self) -> None:
        assert scheduled_residual_s(100, None) is None

    def test_both_none_propagates_missing(self) -> None:
        assert scheduled_residual_s(None, None) is None

    def test_residual_is_absolute_in_both_directions(self) -> None:
        assert scheduled_residual_s(90, 150) == 60
        assert scheduled_residual_s(150, 90) == 60


class TestAgreementMillis:
    def test_zero_total_is_missing_not_zero(self) -> None:
        assert agreement_millis(0, 0) is None

    def test_negative_total_is_missing(self) -> None:
        assert agreement_millis(3, -1) is None

    def test_fraction_scales_to_millis(self) -> None:
        assert agreement_millis(3, 4) == 750
        assert agreement_millis(4, 4) == 1000
        assert agreement_millis(0, 4) == 0
