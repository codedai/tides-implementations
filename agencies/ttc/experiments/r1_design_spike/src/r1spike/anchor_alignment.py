"""Anchor-and-align: bounded order-preserving trip alignment (Spike 02).

Given one vehicle's ordered refined segments and the date-valid ordered
scheduled trips, high-confidence anchors pin their (segment, trip)
pairs and every interval between anchors is solved with an exact
integer dynamic program. NO repeated greedy matching exists here — an
early weak match cannot propagate, because the DP optimizes the whole
path and evidence-equal paths make the affected segments abstain.

Transitions:
- performed segment matched to a scheduled trip (cost = score deficit);
- unmatched/unscheduled performed segment (flat cost, explicit state);
- skipped scheduled trip (FREE — other vehicles run most of the day's
  trips; a skipped trip is the normal case, never a penalty);
- many-to-one (several performed segments referencing one scheduled
  trip, permitted by TIDES) only as a separately-reported variant.

All optimal paths are enumerated (bounded); only assignments present in
EVERY optimum are selected. Segments whose assignment varies across
optima are tied (``XR2.ALIGNPATH.TIED_OPTIMA``) and abstain. The
second-best distinct path cost is tracked exactly, so the path margin
is measured, not estimated.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice

from .spike02_contracts import Reason02

_INF = None  # second-best sentinel


@dataclass(frozen=True)
class AlignmentInput:
    """One vehicle-scope alignment problem."""

    segment_ids: tuple[str, ...]  # ordered by start time
    trip_ids: tuple[str, ...]  # ordered by scheduled start
    candidate_costs: dict[str, dict[str, int]]  # segment -> trip -> cost
    unmatched_cost: int
    anchors: dict[str, str]  # segment -> pinned trip
    allow_many_to_one: bool = False


@dataclass(frozen=True)
class SegmentAlignment:
    segment_id: str
    status: str  # matched | unmatched | tied | anchor
    trip_id: str | None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlignmentPathResult:
    optimal_cost: int
    second_cost: int | None
    path_margin: int | None
    n_optimal: int
    truncated: bool
    segments: tuple[SegmentAlignment, ...]
    skipped_trip_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


def _two_best_add(pair: tuple[int, int | None], c: int) -> tuple[int, int | None]:
    best, second = pair
    return (best + c, None if second is None else second + c)


def _two_best_merge(
    a: tuple[int, int | None], b: tuple[int, int | None]
) -> tuple[int, int | None]:
    values: list[int] = [a[0], b[0]]
    if a[1] is not None:
        values.append(a[1])
    if b[1] is not None:
        values.append(b[1])
    distinct = sorted(set(values))
    best = distinct[0]
    second = distinct[1] if len(distinct) > 1 else None
    return best, second


def align(problem: AlignmentInput, *, max_optima: int = 64) -> AlignmentPathResult:
    """Solve one alignment problem exactly. Deterministic."""
    n = len(problem.segment_ids)
    m = len(problem.trip_ids)
    trip_index = {t: k for k, t in enumerate(problem.trip_ids)}

    cand: list[list[tuple[int, int]]] = []  # per segment: (trip_idx, cost)
    for sid in problem.segment_ids:
        anchor_trip = problem.anchors.get(sid)
        costs = problem.candidate_costs.get(sid, {})
        if anchor_trip is not None:
            if anchor_trip not in trip_index:
                raise ValueError(
                    f"anchor trip {anchor_trip!r} not in the ordered trip list"
                )
            pairs = [(trip_index[anchor_trip], costs.get(anchor_trip, 0))]
        else:
            pairs = sorted(
                (trip_index[t], c) for t, c in costs.items() if t in trip_index
            )
        cand.append(pairs)

    # f[i][j] = (best, second) cost for segments[i:], next trip index >= j.
    # Anchored segments have exactly one candidate and CANNOT be left
    # unmatched (an anchor is a commitment; the interval DP works around
    # it, which is what bounds the subproblems).
    anchored = [problem.segment_ids[i] in problem.anchors for i in range(n)]
    f: list[list[tuple[int, int | None]]] = [
        [(0, None)] * (m + 1) for _ in range(n + 1)
    ]
    infeasible = (1 << 60, None)
    for i in range(n - 1, -1, -1):
        row = f[i]
        nxt = f[i + 1]
        for j in range(m + 1):
            acc: tuple[int, int | None] | None = None
            if not anchored[i]:
                acc = _two_best_add(nxt[j], problem.unmatched_cost)
            for k, c in cand[i]:
                if k < j:
                    continue
                nxt_j = k if problem.allow_many_to_one else k + 1
                option = _two_best_add(nxt[nxt_j], c)
                acc = option if acc is None else _two_best_merge(acc, option)
            row[j] = acc if acc is not None else infeasible
    optimal_cost, second_cost = f[0][0]
    if optimal_cost >= infeasible[0]:
        raise ValueError("infeasible alignment: an anchored segment has no path")
    if second_cost is not None and second_cost >= infeasible[0]:
        second_cost = None

    def paths(i: int, j: int) -> Iterator[tuple[tuple[str, str | None], ...]]:
        if i == n:
            yield ()
            return
        target = f[i][j][0]
        sid = problem.segment_ids[i]
        for k, c in cand[i]:
            if k < j:
                continue
            nxt_j = k if problem.allow_many_to_one else k + 1
            if c + f[i + 1][nxt_j][0] == target:
                for rest in paths(i + 1, nxt_j):
                    yield ((sid, problem.trip_ids[k]), *rest)
        if not anchored[i] and problem.unmatched_cost + f[i + 1][j][0] == target:
            for rest in paths(i + 1, j):
                yield ((sid, None), *rest)

    enumerated = list(islice(paths(0, 0), max_optima + 1))
    truncated = len(enumerated) > max_optima
    if truncated:
        enumerated = enumerated[:max_optima]
    n_optimal = len(enumerated)

    assignments: dict[str, set[str | None]] = {
        sid: set() for sid in problem.segment_ids
    }
    for path in enumerated:
        for sid, trip in path:
            assignments[sid].add(trip)

    segments: list[SegmentAlignment] = []
    matched_trips: set[str] = set()
    reason_codes: list[str] = []
    for sid in problem.segment_ids:
        options = assignments[sid]
        if len(options) > 1:
            segments.append(
                SegmentAlignment(
                    segment_id=sid,
                    status="tied",
                    trip_id=None,
                    reason_codes=(Reason02.ALIGNMENT_TIED,),
                )
            )
            continue
        only = next(iter(options))
        if only is None:
            segments.append(
                SegmentAlignment(
                    segment_id=sid,
                    status="unmatched",
                    trip_id=None,
                    reason_codes=(Reason02.ALIGNMENT_UNMATCHED_SEGMENT,),
                )
            )
            continue
        matched_trips.add(only)
        segments.append(
            SegmentAlignment(
                segment_id=sid,
                status="anchor" if sid in problem.anchors else "matched",
                trip_id=only,
            )
        )
    if any(s.status == "tied" for s in segments):
        reason_codes.append(Reason02.ALIGNMENT_TIED)
    if truncated:
        reason_codes.append("XR2.ALIGNPATH.OPTIMA_TRUNCATED")

    skipped = tuple(t for t in problem.trip_ids if t not in matched_trips)
    path_margin = None if second_cost is None else second_cost - optimal_cost
    return AlignmentPathResult(
        optimal_cost=optimal_cost,
        second_cost=second_cost,
        path_margin=path_margin,
        n_optimal=n_optimal,
        truncated=truncated,
        segments=tuple(segments),
        skipped_trip_ids=skipped,
        reason_codes=tuple(reason_codes),
    )
