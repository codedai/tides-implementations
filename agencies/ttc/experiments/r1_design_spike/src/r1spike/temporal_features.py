"""Temporal evidence features (experimental, spike §8).

Contract highlights:

- ``interval_gap_s = max(0, a0 - s1, s0 - a1)`` for APC interval
  ``[a0, a1]`` vs segment interval ``[s0, s1]`` (integer seconds).
- ``interval_overlap_s = max(0, min(a1, s1) - max(a0, s0))``.
- Overlap ratio denominator (declared here once):
  ``max(1, min(a1 - a0, s1 - s0))`` — the shorter interval, floored at
  one second so zero-length APC dwells stay defined.
- Missing evidence stays missing (``None``); it is never coerced to 0.
- Evidence never propagates across incompatible folds, service-date
  hypotheses, or segments: compatibility is checked by the caller
  passing only same-hypothesis inputs, and re-checked here where the
  inputs carry the hypothesis.
- Stale candidates beyond the hard bound are the score contract's job
  (``XR1.TEMPORAL.STALE``); this module only computes raw values, so the
  9,083-second legacy case flows through as a large gap and dies at the
  contract's hard bound (see tests).
"""

from __future__ import annotations

from dataclasses import dataclass


def interval_gap_s(a0: int, a1: int, s0: int, s1: int) -> int:
    return max(0, a0 - s1, s0 - a1)


def interval_overlap_s(a0: int, a1: int, s0: int, s1: int) -> int:
    return max(0, min(a1, s1) - max(a0, s0))


def interval_overlap_millis(a0: int, a1: int, s0: int, s1: int) -> int:
    """Overlap ratio scaled to 0..1000, shorter-interval denominator."""
    denominator = max(1, min(a1 - a0, s1 - s0))
    ratio = interval_overlap_s(a0, a1, s0, s1) * 1000 // denominator
    return min(1000, ratio)


def nearest_event_gap_s(event_s: int, instants: list[int]) -> int | None:
    """Distance from an event to the nearest instant; None when empty.

    Missing ping evidence stays ``None`` — never zero.
    """
    if not instants:
        return None
    return min(abs(event_s - t) for t in instants)


@dataclass(frozen=True, slots=True)
class BracketingInterpolation:
    """Linear interpolation between two bracketing pings."""

    lat: float
    lon: float
    prev_gap_s: int
    next_gap_s: int


def interpolate_between_pings(
    event_s: int,
    pings: list[tuple[int, float, float]],  # (instant_s, lat, lon) sorted
    *,
    max_gap_s: int,
) -> BracketingInterpolation | None:
    """Interpolate a position at ``event_s`` between bracketing pings.

    Interpolation only — an event outside the ping span, or whose
    bracketing pings are more than ``max_gap_s`` apart, yields ``None``
    (missing evidence). No extrapolation, ever (legacy lesson: the
    one-second dead-reckoning interpolator had no gap bound).
    """
    if not pings:
        return None
    prev = None
    nxt = None
    for t, lat, lon in pings:
        if t <= event_s:
            prev = (t, lat, lon)
        if t >= event_s and nxt is None:
            nxt = (t, lat, lon)
    if prev is None or nxt is None:
        return None
    t0, lat0, lon0 = prev
    t1, lat1, lon1 = nxt
    if t1 - t0 > max_gap_s:
        return None
    if t1 == t0:
        return BracketingInterpolation(lat0, lon0, event_s - t0, t1 - event_s)
    frac = (event_s - t0) / (t1 - t0)
    return BracketingInterpolation(
        lat=lat0 + (lat1 - lat0) * frac,
        lon=lon0 + (lon1 - lon0) * frac,
        prev_gap_s=event_s - t0,
        next_gap_s=t1 - event_s,
    )


def scheduled_residual_s(observed_s: int | None, scheduled_s: int | None) -> int | None:
    if observed_s is None or scheduled_s is None:
        return None
    return abs(observed_s - scheduled_s)


def agreement_millis(matching: int, total: int) -> int | None:
    """Fraction scaled to 0..1000; None when total is zero (missing)."""
    if total <= 0:
        return None
    return matching * 1000 // total
