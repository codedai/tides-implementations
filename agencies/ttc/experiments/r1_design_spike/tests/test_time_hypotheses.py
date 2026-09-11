"""Tests for experimental time hypotheses and rule XT-FOLD-A.

All epochs are pinned to the America/Toronto 2023-11-05 fall-back
transition (02:00 EDT -> 01:00 EST) and the 2024-03-10 spring-forward
gap. Hand-computed reference: 2023-11-28 12:00:00 America/Toronto (EST,
UTC-5) = 2023-11-28 17:00:00 UTC = epoch 1701190800.
"""

from __future__ import annotations

import datetime as dt

import pytest
from r1spike.contracts import Reason
from r1spike.time_hypotheses import (
    DEFAULT_TZ,
    PingObservation,
    apc_time_candidates,
    resolve_avl_folds,
    wall_time_candidates,
)

# 2023-11-28 12:00:00 EST = 17:00:00 UTC; 19688 days * 86400 + 17 * 3600.
EPOCH_2023_11_28_NOON_TORONTO = 1701190800
# 2023-11-05 01:30:00 fold 0 (EDT, UTC-4) = 05:30:00 UTC.
EPOCH_FALL_BACK_0130_FOLD0 = 1699162200

FALL_BACK_DAY = dt.date(2023, 11, 5)


def _wall(hour: int, minute: int, day: dt.date = FALL_BACK_DAY) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute))


class TestWallTimeCandidates:
    def test_unambiguous_time_yields_single_valid_candidate_with_exact_epoch(
        self,
    ) -> None:
        candidates = wall_time_candidates("rec-1", dt.datetime(2023, 11, 28, 12, 0))
        assert len(candidates) == 1
        (candidate,) = candidates
        assert candidate.valid is True
        assert candidate.ambiguous is False
        assert candidate.fold == 0
        assert candidate.utc_epoch_s == EPOCH_2023_11_28_NOON_TORONTO
        assert candidate.utc_offset_s == -5 * 3600
        assert candidate.timezone == DEFAULT_TZ
        assert candidate.wall_text == "2023-11-28 12:00:00"
        assert candidate.reason_codes == ()

    def test_fall_back_ambiguous_time_yields_two_folds_with_3600s_epoch_gap(
        self,
    ) -> None:
        candidates = wall_time_candidates("rec-2", _wall(1, 30))
        assert len(candidates) == 2
        fold0, fold1 = candidates
        assert (fold0.fold, fold1.fold) == (0, 1)
        assert fold0.utc_offset_s == -4 * 3600  # EDT, earlier instant
        assert fold1.utc_offset_s == -5 * 3600  # EST, later instant
        assert fold0.utc_epoch_s == EPOCH_FALL_BACK_0130_FOLD0
        assert fold1.utc_epoch_s - fold0.utc_epoch_s == 3600
        for candidate in candidates:
            assert candidate.valid is True
            assert candidate.ambiguous is True
            assert Reason.TIME_AMBIGUOUS_FOLD in candidate.reason_codes

    def test_spring_forward_nonexistent_time_is_structurally_invalid(self) -> None:
        candidates = wall_time_candidates("rec-3", dt.datetime(2024, 3, 10, 2, 30))
        assert len(candidates) == 1
        (candidate,) = candidates
        assert candidate.valid is False
        assert candidate.ambiguous is False
        assert candidate.utc_epoch_s is None
        assert candidate.utc_offset_s is None
        assert Reason.TIME_NONEXISTENT_LOCAL in candidate.reason_codes

    def test_tz_aware_input_raises_value_error(self) -> None:
        aware = dt.datetime(2023, 11, 28, 12, 0, tzinfo=dt.UTC)
        with pytest.raises(ValueError, match="naive local wall time"):
            wall_time_candidates("rec-4", aware)

    def test_wall_text_preserves_microseconds_only_when_present(self) -> None:
        (whole,) = wall_time_candidates("rec-5", dt.datetime(2023, 11, 28, 6, 5, 4))
        assert whole.wall_text == "2023-11-28 06:05:04"
        (fractional,) = wall_time_candidates(
            "rec-6", dt.datetime(2023, 11, 28, 6, 5, 4, 250)
        )
        assert fractional.wall_text == "2023-11-28 06:05:04.000250"


class TestResolveAvlFolds:
    def test_anchors_before_window_resolve_fold_zero_with_margin_3600(self) -> None:
        pings = [
            PingObservation("a1", _wall(0, 30)),
            PingObservation("a2", _wall(0, 50)),
            PingObservation("x1", _wall(1, 10)),
            PingObservation("x2", _wall(1, 40)),
        ]
        resolutions = resolve_avl_folds(pings, max_gap_s=1800)
        assert [r.record_id for r in resolutions] == ["a1", "a2", "x1", "x2"]
        anchor_a1, anchor_a2, amb_x1, amb_x2 = resolutions
        for anchor in (anchor_a1, anchor_a2):
            assert anchor.status == "resolved"
            assert anchor.method == "unambiguous"
            assert anchor.selected_fold == 0
            assert anchor.margin_s is None
        for ambiguous in (amb_x1, amb_x2):
            assert ambiguous.status == "resolved"
            assert ambiguous.method == "xt-fold-a-uniform"
            assert ambiguous.selected_fold == 0
            assert ambiguous.margin_s == 3600
            assert Reason.TIME_AMBIGUOUS_FOLD in ambiguous.reason_codes
            assert Reason.TIME_FOLD_SEQUENCE_RESOLVED in ambiguous.reason_codes
        # Fold-0 instants: 00:50 EDT -> 01:10 EDT is 1200 s.
        assert amb_x1.selected_utc_epoch_s - anchor_a2.selected_utc_epoch_s == 1200
        assert amb_x2.selected_utc_epoch_s - amb_x1.selected_utc_epoch_s == 1800

    def test_anchor_after_window_resolves_fold_one(self) -> None:
        pings = [
            PingObservation("x1", _wall(1, 50)),
            PingObservation("a1", _wall(2, 10)),
        ]
        resolutions = resolve_avl_folds(pings, max_gap_s=1800)
        ambiguous, anchor = resolutions
        assert anchor.status == "resolved"
        assert anchor.method == "unambiguous"
        assert ambiguous.status == "resolved"
        assert ambiguous.method == "xt-fold-a-uniform"
        assert ambiguous.selected_fold == 1
        assert ambiguous.margin_s == 3600
        # Fold-1 instant: 01:50 EST -> 02:10 EST is 1200 s.
        assert anchor.selected_utc_epoch_s - ambiguous.selected_utc_epoch_s == 1200

    def test_same_wall_time_collision_leaves_all_ambiguous_unresolved(self) -> None:
        pings = [
            PingObservation("a1", _wall(0, 50)),
            PingObservation("x1", _wall(1, 15)),
            PingObservation("x2", _wall(1, 15)),
        ]
        resolutions = resolve_avl_folds(pings, max_gap_s=100_000)
        anchor, first, second = resolutions
        assert anchor.status == "resolved"
        for ambiguous in (first, second):
            assert ambiguous.status == "unresolved"
            assert ambiguous.method == "unresolved-collision"
            assert ambiguous.selected_fold is None
            assert ambiguous.selected_utc_epoch_s is None
            assert Reason.TIME_UNRESOLVED_FOLD in ambiguous.reason_codes

    def test_both_folds_consistent_stays_unresolved(self) -> None:
        # Anchors 00:30 EDT (04:30 UTC) and 02:30 EST (07:30 UTC) leave a
        # symmetric slot: fold 0 (05:30 UTC) and fold 1 (06:30 UTC) both
        # fit gaps of at most 7200 s.
        pings = [
            PingObservation("a1", _wall(0, 30)),
            PingObservation("x1", _wall(1, 30)),
            PingObservation("a2", _wall(2, 30)),
        ]
        resolutions = resolve_avl_folds(pings, max_gap_s=7200)
        ambiguous = resolutions[1]
        assert ambiguous.status == "unresolved"
        assert ambiguous.method == "unresolved-both-consistent"
        assert ambiguous.selected_fold is None
        assert ambiguous.margin_s is None
        assert Reason.TIME_UNRESOLVED_FOLD in ambiguous.reason_codes

    def test_neither_fold_consistent_stays_unresolved(self) -> None:
        pings = [
            PingObservation("a1", _wall(0, 10)),
            PingObservation("x1", _wall(1, 30)),
        ]
        # Fold 0 gap is 4800 s, fold 1 gap is 8400 s; neither fits 600 s.
        resolutions = resolve_avl_folds(pings, max_gap_s=600)
        ambiguous = resolutions[1]
        assert ambiguous.status == "unresolved"
        assert ambiguous.method == "unresolved-none-consistent"
        assert ambiguous.selected_fold is None

    def test_no_anchor_at_all_uses_distinct_no_anchor_method(self) -> None:
        pings = [
            PingObservation("x1", _wall(1, 10)),
            PingObservation("x2", _wall(1, 40)),
        ]
        resolutions = resolve_avl_folds(pings, max_gap_s=100_000)
        for ambiguous in resolutions:
            assert ambiguous.status == "unresolved"
            assert ambiguous.method == "unresolved-no-anchor"
            assert ambiguous.selected_fold is None

    def test_input_without_ambiguity_resolves_everything_as_unambiguous(self) -> None:
        pings = [
            PingObservation("a1", dt.datetime(2023, 11, 28, 12, 0)),
            PingObservation("a2", dt.datetime(2023, 11, 28, 12, 30)),
        ]
        resolutions = resolve_avl_folds(pings, max_gap_s=1800)
        for resolution in resolutions:
            assert resolution.status == "resolved"
            assert resolution.method == "unambiguous"
            assert resolution.selected_fold == 0
            assert resolution.margin_s is None
            assert resolution.reason_codes == ()

    def test_nonexistent_ping_is_invalid_and_never_serves_as_an_anchor(self) -> None:
        pings = [
            PingObservation("bad", dt.datetime(2024, 3, 10, 2, 30)),
            PingObservation("x1", _wall(1, 30)),
        ]
        resolutions = resolve_avl_folds(pings, max_gap_s=100_000_000)
        invalid, ambiguous = resolutions
        assert invalid.status == "invalid"
        assert invalid.selected_fold is None
        assert invalid.selected_utc_epoch_s is None
        assert Reason.TIME_NONEXISTENT_LOCAL in invalid.reason_codes
        # If the invalid ping counted as an anchor, the huge max_gap_s
        # would have resolved x1; instead there is no anchor at all.
        assert ambiguous.status == "unresolved"
        assert ambiguous.method == "unresolved-no-anchor"


class TestApcTimeCandidates:
    def test_ambiguous_arrival_returns_both_candidates_and_selects_neither(
        self,
    ) -> None:
        result = apc_time_candidates("apc-1", _wall(1, 30), None)
        assert set(result) == {"arrival", "departure"}
        assert result["departure"] == []
        arrival = result["arrival"]
        assert len(arrival) == 2
        assert [candidate.fold for candidate in arrival] == [0, 1]
        for candidate in arrival:
            assert candidate.valid is True
            assert candidate.ambiguous is True
        # Design rule D-05: no independent fold resolution for APC —
        # TimeCandidate has no selection field and both folds survive.

    def test_missing_inputs_yield_empty_lists_and_present_ones_expand(self) -> None:
        empty = apc_time_candidates("apc-2", None, None)
        assert empty == {"arrival": [], "departure": []}
        both = apc_time_candidates(
            "apc-3",
            dt.datetime(2023, 11, 28, 12, 0),
            dt.datetime(2023, 11, 28, 12, 1),
        )
        assert len(both["arrival"]) == 1
        assert len(both["departure"]) == 1
        assert both["arrival"][0].utc_epoch_s == EPOCH_2023_11_28_NOON_TORONTO
        assert both["departure"][0].utc_epoch_s == EPOCH_2023_11_28_NOON_TORONTO + 60
