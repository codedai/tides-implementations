"""Integer fixed-point arithmetic for experimental score decisions.

Every decision-bearing comparison in the spike happens on integers.
Floats may exist upstream (geometry residuals), but they are quantized
here, once, through an explicit quantum, and never compared directly.

The scheme:

- raw feature values are quantized to integer raw units via a declared
  quantum with half-even rounding (exact rational arithmetic; a float
  input is converted to its exact binary rational first);
- transforms map integer raw units to an integer qscore in
  [0, SCORE_SCALE] using exact rational interpolation, rounded half-even;
- aggregations are integer sums and integer weighted means, so they are
  independent of iteration order by construction;
- thresholds, null scores, and margins are integers.
"""

from __future__ import annotations

from fractions import Fraction

SCORE_SCALE = 1_000_000


def round_half_even_div(numerator: int, denominator: int) -> int:
    """Round numerator/denominator to the nearest integer, ties to even.

    ``denominator`` must be positive. Correct for negative numerators
    because Python's ``divmod`` yields a nonnegative remainder.
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    q, r = divmod(numerator, denominator)
    twice = 2 * r
    if twice > denominator or (twice == denominator and q % 2 != 0):
        return q + 1
    return q


def round_half_even_fraction(value: Fraction) -> int:
    return round_half_even_div(value.numerator, value.denominator)


def quantize(value: float | int | Fraction, quantum: Fraction) -> int:
    """Quantize a raw value to integer units of ``quantum``, half-even.

    A float input is converted to its exact binary rational, so identical
    input bytes always quantize identically.
    """
    if quantum <= 0:
        raise ValueError("quantum must be positive")
    exact = Fraction(value)
    return round_half_even_fraction(exact / quantum)


def weighted_mean_q(pairs: list[tuple[int, int]]) -> int:
    """Integer weighted mean of (qscore, weight) pairs, half-even.

    Order-free by construction (integer sums). Raises on empty input or
    zero total weight: callers must decide explicitly what an absent
    evidence family means (regime switch), never silently renormalize.
    """
    total_w = sum(w for _, w in pairs)
    if total_w <= 0:
        raise ValueError("weighted_mean_q requires positive total weight")
    total = sum(q * w for q, w in pairs)
    return round_half_even_div(total, total_w)


def interpolate_piecewise(points: list[tuple[int, int]], x: int) -> int:
    """Evaluate a piecewise-linear transform at integer raw value ``x``.

    ``points`` is a list of (raw_units, qscore) breakpoints with strictly
    increasing raw units. Outside the covered range the boundary qscore
    is held constant (clamped), which preserves monotonicity. Between
    breakpoints, exact rational interpolation rounded half-even.
    """
    if not points:
        raise ValueError("piecewise transform needs at least one point")
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x0 <= x <= x1:
            num = y0 * (x1 - x0) + (y1 - y0) * (x - x0)
            return round_half_even_div(num, x1 - x0)
    raise AssertionError("unreachable: breakpoints not increasing")
