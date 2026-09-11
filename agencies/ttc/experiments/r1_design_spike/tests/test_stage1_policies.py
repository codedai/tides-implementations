"""Tests for the Stage 1 APC date-consistency support policies.

All scenarios use hand-built ``ApcHypothesisRef`` lists (spike §12). The
invariants under test: one APC source record contributes total mass
<= 1; multiple fold/stop hypotheses divide a record's mass, never
multiply it; support is a per-record average, so APC density and segment
duration have no term; insufficient support yields ``None`` — a regime
switch, never a renormalization.
"""

from __future__ import annotations

import dataclasses
import inspect
import random

import pytest
from r1spike.stage1_policies import (
    ApcHypothesisRef,
    compute_support,
    policy_a_support,
    policy_b_support,
    policy_c_support,
)

SEG = "xs1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_SEG = "xs1-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
DATE = "2023-11-05"
PREV_DATE = "2023-11-04"
FEED = "feed-2023-44"


def _hyp(
    record_id: str,
    segment_id: str = SEG,
    date: str = DATE,
    feed: str | None = FEED,
) -> ApcHypothesisRef:
    return ApcHypothesisRef(
        apc_source_record_id=record_id,
        candidate_segment_id=segment_id,
        service_date_hypothesis=date,
        feed_id=feed,
    )


class TestPolicyA:
    def test_policy_a_always_returns_none_regardless_of_input(self) -> None:
        assert policy_a_support(SEG, DATE, FEED, []) is None
        fully_matching = [_hyp("r1"), _hyp("r2"), _hyp("r3")]
        assert policy_a_support(SEG, DATE, FEED, fully_matching) is None
        mixed = [
            _hyp("r1"),
            _hyp("r1", date=PREV_DATE),
            _hyp("r2", segment_id=OTHER_SEG),
            _hyp("r3", feed=None),
        ]
        assert policy_a_support(SEG, DATE, FEED, mixed) is None


class TestPolicyB:
    def test_four_hypotheses_two_matching_yield_500_mass_divided_not_multiplied(
        self,
    ) -> None:
        """THE KEY INVARIANT: a record contributes at most once.

        One record with four fold/stop hypotheses all on one segment
        (fold/stop distinctions collapse to identical Stage-1 rows), two
        of them matching (date, feed): the record's mass 1 is divided
        equally over its four hypotheses, so support is 2/4 = 500 millis
        — never 2 x 1000 from counting matches independently.
        """
        hypotheses = [
            _hyp("r1"),
            _hyp("r1"),
            _hyp("r1", date=PREV_DATE),
            _hyp("r1", date=PREV_DATE),
        ]
        support = policy_b_support(SEG, DATE, FEED, hypotheses, min_support_records=1)
        assert support == 500
        assert support <= 1000

    def test_record_shared_across_two_segments_contributes_nothing(self) -> None:
        """A record with candidates on two physical segments is shared:
        Policy B must not pre-decide its attachment, so it contributes
        neither mass nor a denominator slot. Support comes only from the
        unshared records and is identical with the shared record removed
        from the input entirely."""
        shared = [
            _hyp("r-shared"),  # matches (date, feed) on SEG — still excluded
            _hyp("r-shared", segment_id=OTHER_SEG),
        ]
        unshared = [
            _hyp("r-u1"),
            _hyp("r-u2"),
            _hyp("r-u2", date=PREV_DATE),
        ]
        with_shared = policy_b_support(
            SEG, DATE, FEED, shared + unshared, min_support_records=1
        )
        without_shared = policy_b_support(
            SEG, DATE, FEED, unshared, min_support_records=1
        )
        # r-u1 contributes 1, r-u2 contributes 1/2 -> (3/2) / 2 = 750.
        assert with_shared == 750
        assert with_shared == without_shared

    def test_duplicated_hypothesis_rows_never_multiply_mass_beyond_one(self) -> None:
        """Many hypothesis rows from one record on one segment divide
        the record's mass; six all-matching duplicates still sum to
        exactly mass 1, so support stays 1000, never 6000."""
        duplicated = [_hyp("r1") for _ in range(6)]
        assert (
            policy_b_support(SEG, DATE, FEED, duplicated, min_support_records=1) == 1000
        )
        with_second_record = [*duplicated, _hyp("r2")]
        assert (
            policy_b_support(SEG, DATE, FEED, with_second_record, min_support_records=1)
            == 1000
        )

    def test_doubling_apc_density_does_not_increase_normalized_support(self) -> None:
        """Support is a per-record average: two fully-matching records
        give 1000, four give 1000 too, and mixing in eligible
        non-matching records lowers it proportionally (never raises
        it)."""
        two = [_hyp(f"r{i}") for i in range(2)]
        four = [_hyp(f"r{i}") for i in range(4)]
        assert policy_b_support(SEG, DATE, FEED, two, min_support_records=1) == 1000
        assert policy_b_support(SEG, DATE, FEED, four, min_support_records=1) == 1000
        non_matching = [_hyp(f"n{i}", date=PREV_DATE) for i in range(2)]
        assert (
            policy_b_support(SEG, DATE, FEED, two + non_matching, min_support_records=1)
            == 500
        )
        more_non_matching = [_hyp(f"n{i}", date=PREV_DATE) for i in range(6)]
        assert (
            policy_b_support(
                SEG, DATE, FEED, two + more_non_matching, min_support_records=1
            )
            == 250
        )

    def test_support_has_no_duration_input_by_construction(self) -> None:
        """ "Doubling segment duration" has no term to test at the value
        level, and this is verifiable at the API surface: neither the
        policy functions nor ``ApcHypothesisRef`` accept any timestamp,
        duration, or interval input. Support is a per-record average
        over hypothesis-identity fractions, so stretching a segment in
        time cannot change it — there is nothing to perturb."""
        for policy_function in (policy_b_support, policy_c_support):
            parameter_names = set(inspect.signature(policy_function).parameters)
            assert parameter_names == {
                "segment_id",
                "date_hypothesis",
                "feed_id",
                "hypotheses",
                "min_support_records",
            }
            assert not any("duration" in name for name in parameter_names)
        hypothesis_fields = {f.name for f in dataclasses.fields(ApcHypothesisRef)}
        assert hypothesis_fields == {
            "apc_source_record_id",
            "candidate_segment_id",
            "service_date_hypothesis",
            "feed_id",
        }

    def test_below_min_support_records_returns_none_not_renormalized(self) -> None:
        """Insufficient support is a regime switch to AVL-only, never a
        renormalized score from partial evidence."""
        one_record = [_hyp("r1")]
        assert (
            policy_b_support(SEG, DATE, FEED, one_record, min_support_records=2) is None
        )
        # A shared record is not an eligible record: alone it cannot
        # meet even min_support_records=1.
        shared_only = [_hyp("r-s"), _hyp("r-s", segment_id=OTHER_SEG)]
        assert (
            policy_b_support(SEG, DATE, FEED, shared_only, min_support_records=1)
            is None
        )


class TestPolicyC:
    def test_shared_record_mass_marginalized_across_all_segments_never_exceeds_one(
        self,
    ) -> None:
        """A shared record participates in Policy C, but its mass 1 is
        divided over ALL of its hypotheses across every candidate
        segment: 2 of 4 hypotheses on (SEG, DATE) -> 1/2, and the
        supports over every (segment, date) combination it touches sum
        to exactly 1 record-mass — marginalization never exceeds 1 per
        record."""
        shared = [
            _hyp("r1"),
            _hyp("r1"),
            _hyp("r1", segment_id=OTHER_SEG),
            _hyp("r1", segment_id=OTHER_SEG, date=PREV_DATE),
        ]
        support_here = policy_c_support(SEG, DATE, FEED, shared, min_support_records=1)
        assert support_here == 500  # 2/4 of the record's mass
        per_combo = {
            (SEG, DATE): support_here,
            (OTHER_SEG, DATE): policy_c_support(
                OTHER_SEG, DATE, FEED, shared, min_support_records=1
            ),
            (OTHER_SEG, PREV_DATE): policy_c_support(
                OTHER_SEG, PREV_DATE, FEED, shared, min_support_records=1
            ),
        }
        assert per_combo[(OTHER_SEG, DATE)] == 250
        assert per_combo[(OTHER_SEG, PREV_DATE)] == 250
        assert all(value <= 1000 for value in per_combo.values())
        assert sum(per_combo.values()) == 1000  # total mass exactly 1

    def test_policy_c_and_policy_b_measurably_disagree_on_shared_records(
        self,
    ) -> None:
        """Same input, different support: Policy B excludes the shared
        record (1000), Policy C marginalizes it in ((1/2 + 1)/2 = 750).
        The disagreement is the measurement the spike reports."""
        hypotheses = [
            _hyp("r-shared"),
            _hyp("r-shared"),
            _hyp("r-shared", segment_id=OTHER_SEG),
            _hyp("r-shared", segment_id=OTHER_SEG),
            _hyp("r-unshared"),
        ]
        support_b = policy_b_support(SEG, DATE, FEED, hypotheses, min_support_records=1)
        support_c = policy_c_support(SEG, DATE, FEED, hypotheses, min_support_records=1)
        assert support_b == 1000
        assert support_c == 750
        assert support_b != support_c


class TestComputeSupportDispatch:
    def test_compute_support_dispatches_to_each_policy(self) -> None:
        hypotheses = [
            _hyp("r1"),
            _hyp("r1", segment_id=OTHER_SEG),
            _hyp("r2"),
            _hyp("r2", date=PREV_DATE),
        ]
        dispatched_a = compute_support(
            "policy_a", SEG, DATE, FEED, hypotheses, min_support_records=1
        )
        assert dispatched_a is None
        dispatched_b = compute_support(
            "policy_b", SEG, DATE, FEED, hypotheses, min_support_records=1
        )
        assert dispatched_b == policy_b_support(
            SEG, DATE, FEED, hypotheses, min_support_records=1
        )
        assert dispatched_b == 500  # r1 shared -> excluded; r2 -> 1/2
        dispatched_c = compute_support(
            "policy_c", SEG, DATE, FEED, hypotheses, min_support_records=1
        )
        assert dispatched_c == policy_c_support(
            SEG, DATE, FEED, hypotheses, min_support_records=1
        )
        assert dispatched_c == 500  # r1 -> 1/2 marginalized; r2 -> 1/2

    def test_compute_support_raises_value_error_for_unknown_policy(self) -> None:
        with pytest.raises(ValueError, match="unknown stage1 policy"):
            compute_support("policy_z", SEG, DATE, FEED, [], min_support_records=1)


class TestDeterminism:
    def test_shuffling_hypothesis_order_never_changes_any_policy_output(self) -> None:
        """The policies sort internally, so input order (including
        duplicate rows, shared records, and a None feed exercising the
        sort key) must never change any output."""
        canonical = [
            _hyp("r1"),
            _hyp("r1"),
            _hyp("r1", date=PREV_DATE),
            _hyp("r2", segment_id=OTHER_SEG),
            _hyp("r2"),
            _hyp("r3", feed=None),
            _hyp("r3"),
            _hyp("r4", date=PREV_DATE),
            _hyp("r4"),
            _hyp("r5"),
        ]
        expected = {
            policy: compute_support(
                policy, SEG, DATE, FEED, canonical, min_support_records=1
            )
            for policy in ("policy_a", "policy_b", "policy_c")
        }
        assert expected["policy_b"] is not None
        assert expected["policy_c"] is not None
        for seed in range(10):
            shuffled = list(canonical)
            random.Random(seed).shuffle(shuffled)
            for policy, expected_value in expected.items():
                assert (
                    compute_support(
                        policy, SEG, DATE, FEED, shuffled, min_support_records=1
                    )
                    == expected_value
                )
