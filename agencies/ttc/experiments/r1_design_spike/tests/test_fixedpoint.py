"""Tests for integer fixed-point arithmetic (``r1spike.fixedpoint``).

Every decision-bearing comparison in the spike happens on integers, so
these tests pin down the exact rounding scheme: half-even everywhere,
exact binary rationals for float inputs, and order-free aggregation.
"""

from __future__ import annotations

import random
from fractions import Fraction

import pytest
from r1spike.fixedpoint import (
    interpolate_piecewise,
    quantize,
    round_half_even_div,
    round_half_even_fraction,
    weighted_mean_q,
)


class TestRoundHalfEvenDiv:
    def test_tie_with_odd_quotient_rounds_down_to_even(self) -> None:
        assert round_half_even_div(5, 2) == 2  # 2.5 -> 2 (even)

    def test_tie_with_even_target_above_rounds_up_to_even(self) -> None:
        assert round_half_even_div(7, 2) == 4  # 3.5 -> 4 (even)

    def test_negative_ties_round_to_even_in_both_parities(self) -> None:
        assert round_half_even_div(-5, 2) == -2  # -2.5 -> -2 (even)
        assert round_half_even_div(-7, 2) == -4  # -3.5 -> -4 (even)

    def test_negative_non_tie_rounds_to_nearest(self) -> None:
        assert round_half_even_div(-7, 3) == -2  # -2.33.. -> -2
        assert round_half_even_div(-8, 3) == -3  # -2.66.. -> -3

    def test_positive_non_tie_rounds_to_nearest(self) -> None:
        assert round_half_even_div(7, 3) == 2
        assert round_half_even_div(8, 3) == 3

    def test_zero_denominator_raises(self) -> None:
        with pytest.raises(ValueError, match="denominator must be positive"):
            round_half_even_div(1, 0)

    def test_negative_denominator_raises(self) -> None:
        with pytest.raises(ValueError, match="denominator must be positive"):
            round_half_even_div(1, -2)


class TestRoundHalfEvenFraction:
    def test_ties_go_to_even_for_positive_and_negative_fractions(self) -> None:
        assert round_half_even_fraction(Fraction(5, 2)) == 2
        assert round_half_even_fraction(Fraction(7, 2)) == 4
        assert round_half_even_fraction(Fraction(-5, 2)) == -2
        assert round_half_even_fraction(Fraction(-7, 2)) == -4

    def test_non_tie_fractions_round_to_nearest_integer(self) -> None:
        assert round_half_even_fraction(Fraction(1, 3)) == 0
        assert round_half_even_fraction(Fraction(2, 3)) == 1
        assert round_half_even_fraction(Fraction(41, 10)) == 4


class TestQuantize:
    def test_float_sum_quantizes_via_its_exact_binary_rational(self) -> None:
        # 0.1 + 0.2 is the double 1351079888211149 / 2**52, whose exact
        # value in tenths is 3.000000000000000444... -> 3 exactly, and
        # identically for every evaluation of the same float bytes.
        assert quantize(0.1 + 0.2, Fraction(1, 10)) == 3

    def test_metres_to_decimetre_example_uses_the_exact_float(self) -> None:
        # The double nearest to 30.05 is 8458323050155213 / 2**48, which
        # is strictly above 300.5 decimetres (by 2**-48 dm). Half-even
        # therefore never sees a tie here: the answer is 301, not 300.
        assert quantize(30.05, Fraction("0.1")) == 301

    def test_integer_and_fraction_inputs_quantize_exactly(self) -> None:
        assert quantize(3, Fraction(1)) == 3
        assert quantize(Fraction(61, 2), Fraction("0.5")) == 61
        assert quantize(Fraction(1, 2), Fraction(1)) == 0  # tie -> even

    def test_zero_quantum_raises(self) -> None:
        with pytest.raises(ValueError, match="quantum must be positive"):
            quantize(1.0, Fraction(0))

    def test_negative_quantum_raises(self) -> None:
        with pytest.raises(ValueError, match="quantum must be positive"):
            quantize(1.0, Fraction(-1, 10))


class TestWeightedMeanQ:
    def test_shuffled_pair_list_gives_identical_result(self) -> None:
        pairs = [(900_000, 3), (250_000, 5), (700_000, 2), (100_000, 7)]
        expected = weighted_mean_q(pairs)
        shuffled = list(pairs)
        random.Random(1234).shuffle(shuffled)
        assert shuffled != pairs  # the shuffle actually changed the order
        assert weighted_mean_q(shuffled) == expected
        assert weighted_mean_q(list(reversed(pairs))) == expected

    def test_tie_between_adjacent_integers_resolves_half_even(self) -> None:
        assert weighted_mean_q([(1, 1), (2, 1)]) == 2  # 1.5 -> 2 (even)
        assert weighted_mean_q([(0, 1), (1, 1)]) == 0  # 0.5 -> 0 (even)

    def test_single_pair_returns_its_qscore(self) -> None:
        assert weighted_mean_q([(123_456, 7)]) == 123_456

    def test_empty_input_raises(self) -> None:
        with pytest.raises(ValueError, match="positive total weight"):
            weighted_mean_q([])

    def test_zero_total_weight_raises(self) -> None:
        with pytest.raises(ValueError, match="positive total weight"):
            weighted_mean_q([(500_000, 0), (250_000, 0)])


class TestInterpolatePiecewise:
    POINTS = ((0, 1_000_000), (60, 900_000), (300, 600_000))

    def test_clamps_below_the_first_breakpoint(self) -> None:
        assert interpolate_piecewise(list(self.POINTS), -50) == 1_000_000

    def test_clamps_above_the_last_breakpoint(self) -> None:
        assert interpolate_piecewise(list(self.POINTS), 10_000) == 600_000

    def test_exact_breakpoint_hits_return_declared_qscores(self) -> None:
        assert interpolate_piecewise(list(self.POINTS), 0) == 1_000_000
        assert interpolate_piecewise(list(self.POINTS), 60) == 900_000
        assert interpolate_piecewise(list(self.POINTS), 300) == 600_000

    def test_interpolates_exactly_between_breakpoints(self) -> None:
        assert interpolate_piecewise(list(self.POINTS), 30) == 950_000
        assert interpolate_piecewise(list(self.POINTS), 180) == 750_000

    def test_interpolation_ties_round_half_even(self) -> None:
        # Between (0, 0) and (4, 2) the exact values at x=1 and x=3 are
        # 0.5 and 1.5; both are ties and both go to the even integer.
        points = [(0, 0), (4, 2)]
        assert interpolate_piecewise(points, 1) == 0
        assert interpolate_piecewise(points, 3) == 2

    def test_single_point_transform_is_constant_everywhere(self) -> None:
        points = [(10, 500_000)]
        assert interpolate_piecewise(points, -1_000) == 500_000
        assert interpolate_piecewise(points, 10) == 500_000
        assert interpolate_piecewise(points, 99_999) == 500_000

    def test_empty_transform_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one point"):
            interpolate_piecewise([], 5)
