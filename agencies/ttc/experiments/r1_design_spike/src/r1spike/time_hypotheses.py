"""Experimental time-interpretation hypotheses for naive TTC wall times.

Contract: source timestamps are interpreted as naive America/Toronto wall
times. That interpretation is an EXPERIMENTAL run assumption of this
spike — it has NOT been confirmed against the source systems. Every
output is versioned under ``r1spike.ALGORITHM_VERSION`` semantics and
must never be treated as production truth.

Each naive wall time expands into its complete set of interpretations:

- unambiguous local time  -> exactly one valid candidate (fold 0);
- fall-back ambiguous time -> two valid candidates (fold 0 = earlier
  instant / larger UTC offset, fold 1 = later instant / smaller offset);
- spring-forward nonexistent time -> one structurally invalid candidate
  with no UTC instant.

Rule XT-FOLD-A (:func:`resolve_avl_folds`) is an EXPERIMENTAL,
deliberately conservative per-vehicle fold resolver and is not
production-ready. It evaluates exactly two uniform hypotheses (all
ambiguous pings at fold 0, or all at fold 1) against the vehicle's
unambiguous anchor pings, and it never invents a fold: any tie,
collision, or missing evidence leaves every ambiguous ping unresolved.
APC records are never independently fold-resolved here (design rule
D-05).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from itertools import pairwise
from zoneinfo import ZoneInfo

from r1spike.contracts import Reason, TimeCandidate

DEFAULT_TZ = "America/Toronto"


def _wall_text(wall: dt.datetime) -> str:
    timespec = "seconds" if wall.microsecond == 0 else "microseconds"
    return wall.isoformat(sep=" ", timespec=timespec)


def _valid_candidate(
    source_record_id: str,
    wall_text: str,
    tz: str,
    aware: dt.datetime,
    *,
    ambiguous: bool,
) -> TimeCandidate:
    offset = aware.utcoffset()
    if offset is None:  # pragma: no cover - ZoneInfo always has an offset
        raise ValueError(f"timezone {tz!r} produced no UTC offset")
    return TimeCandidate(
        source_record_id=source_record_id,
        wall_text=wall_text,
        timezone=tz,
        fold=aware.fold,
        utc_epoch_s=int(aware.timestamp()),
        utc_offset_s=int(offset.total_seconds()),
        ambiguous=ambiguous,
        valid=True,
        reason_codes=(Reason.TIME_AMBIGUOUS_FOLD,) if ambiguous else (),
    )


def wall_time_candidates(
    source_record_id: str,
    wall: dt.datetime,
    *,
    tz: str = DEFAULT_TZ,
) -> list[TimeCandidate]:
    """Expand one naive local wall time into its complete candidate set.

    Returns one valid candidate for an unambiguous time, two (fold 0
    then fold 1) for a fall-back ambiguous time, and one invalid
    candidate (no UTC instant) for a spring-forward nonexistent time.

    Raises ``ValueError`` on tz-aware input: the naive-wall-time
    interpretation is the experimental contract, and an aware input
    means the caller is holding a different contract.
    """
    if wall.tzinfo is not None:
        raise ValueError(
            "wall must be a naive local wall time; got tz-aware input for "
            f"source_record_id={source_record_id!r}"
        )
    zone = ZoneInfo(tz)
    text = _wall_text(wall)
    aware0 = wall.replace(fold=0, tzinfo=zone)
    aware1 = wall.replace(fold=1, tzinfo=zone)
    offset0 = aware0.utcoffset()
    offset1 = aware1.utcoffset()

    nonexistent = False
    if offset0 == offset1:
        # Normal time, unless the round trip through UTC fails to
        # reproduce the wall clock (defensive nonexistent detection).
        round_trip = aware0.astimezone(dt.UTC).astimezone(zone)
        nonexistent = round_trip.replace(tzinfo=None, fold=0) != wall
    elif offset1 is not None and offset0 is not None and offset1 > offset0:
        # Spring forward: fold 1 offset is LARGER (post-gap offset).
        nonexistent = True

    if nonexistent:
        return [
            TimeCandidate(
                source_record_id=source_record_id,
                wall_text=text,
                timezone=tz,
                fold=0,
                utc_epoch_s=None,
                utc_offset_s=None,
                ambiguous=False,
                valid=False,
                reason_codes=(Reason.TIME_NONEXISTENT_LOCAL,),
            )
        ]
    if offset0 == offset1:
        return [_valid_candidate(source_record_id, text, tz, aware0, ambiguous=False)]
    # Fall back: fold 1 offset is SMALLER; two real UTC instants exist.
    return [
        _valid_candidate(source_record_id, text, tz, aware0, ambiguous=True),
        _valid_candidate(source_record_id, text, tz, aware1, ambiguous=True),
    ]


@dataclass(frozen=True, slots=True)
class PingObservation:
    """One AVL ping of one vehicle: a record id and a naive wall time."""

    record_id: str
    wall: dt.datetime


@dataclass(frozen=True)
class FoldResolution:
    """XT-FOLD-A outcome for one ping. Status is never a guess."""

    record_id: str
    candidates: tuple[TimeCandidate, ...]
    status: str  # "resolved" | "unresolved" | "invalid"
    selected_fold: int | None
    selected_utc_epoch_s: int | None
    method: str
    margin_s: int | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _HypothesisGaps:
    """Adjacent-gap statistics for one uniform fold hypothesis.

    ``max_touch_gap`` is the maximum adjacent gap touching at least one
    ambiguous ping (anchor-anchor gaps are irrelevant); it decides
    consistency. ``max_mixed_gap`` restricts to anchor<->ambiguous gaps,
    the only gaps that differ between the two uniform hypotheses
    (ambiguous-ambiguous gaps shift rigidly with the fold); it feeds the
    margin.
    """

    max_touch_gap: int
    max_mixed_gap: int


def _hypothesis_gaps(
    anchor_epochs: list[int], ambiguous_epochs: list[int]
) -> _HypothesisGaps:
    sequence = sorted(
        [(epoch, False) for epoch in anchor_epochs]
        + [(epoch, True) for epoch in ambiguous_epochs]
    )
    max_touch = 0
    max_mixed = 0
    for (epoch_a, amb_a), (epoch_b, amb_b) in pairwise(sequence):
        gap = epoch_b - epoch_a
        if amb_a or amb_b:
            max_touch = max(max_touch, gap)
        if amb_a != amb_b:
            max_mixed = max(max_mixed, gap)
    return _HypothesisGaps(max_touch_gap=max_touch, max_mixed_gap=max_mixed)


def _ambiguous_group_decision(
    walls: list[dt.datetime],
    anchor_epochs: list[int],
    fold_epochs: dict[int, list[int]],
    *,
    max_gap_s: int,
) -> tuple[int | None, str, int | None]:
    """Decide (selected_fold, method, margin_s) for the ambiguous group."""
    if len(set(walls)) < len(walls):
        # Same-vehicle wall-time collision: the vehicle almost certainly
        # traversed the repeated hour twice, so BOTH uniform hypotheses
        # are structurally wrong.
        return None, "unresolved-collision", None
    if not anchor_epochs:
        return None, "unresolved-no-anchor", None
    gaps = {fold: _hypothesis_gaps(anchor_epochs, fold_epochs[fold]) for fold in (0, 1)}
    consistent = [fold for fold in (0, 1) if gaps[fold].max_touch_gap <= max_gap_s]
    if len(consistent) == 2:
        return None, "unresolved-both-consistent", None
    if not consistent:
        return None, "unresolved-none-consistent", None
    selected = consistent[0]
    rejected = 1 - selected
    margin = abs(gaps[rejected].max_mixed_gap - gaps[selected].max_mixed_gap)
    return selected, "xt-fold-a-uniform", margin


def resolve_avl_folds(
    pings: list[PingObservation],
    *,
    tz: str = DEFAULT_TZ,
    max_gap_s: int,
) -> list[FoldResolution]:
    """Apply experimental rule XT-FOLD-A to ONE vehicle's pings.

    The caller partitions pings by vehicle and passes them in a
    deterministic order of its choosing; this function never reorders
    the input and returns one resolution per ping in input order.

    Conservative by construction: ambiguous pings are resolved only when
    exactly one of the two uniform fold hypotheses is consistent with
    the vehicle's unambiguous anchor pings (maximum adjacent gap
    touching an ambiguous ping <= ``max_gap_s``). Record ids never break
    ties and no fold is ever defaulted.
    """
    expanded = [
        (ping, wall_time_candidates(ping.record_id, ping.wall, tz=tz)) for ping in pings
    ]

    anchor_epochs: list[int] = []
    ambiguous_walls: list[dt.datetime] = []
    fold_epochs: dict[int, list[int]] = {0: [], 1: []}
    for ping, candidates in expanded:
        first = candidates[0]
        if not first.valid:
            continue
        if first.ambiguous:
            ambiguous_walls.append(ping.wall)
            for candidate in candidates:
                if candidate.utc_epoch_s is not None:
                    fold_epochs[candidate.fold].append(candidate.utc_epoch_s)
        elif first.utc_epoch_s is not None:
            anchor_epochs.append(first.utc_epoch_s)

    selected_fold: int | None = None
    group_method = ""
    group_margin: int | None = None
    if ambiguous_walls:
        selected_fold, group_method, group_margin = _ambiguous_group_decision(
            ambiguous_walls, anchor_epochs, fold_epochs, max_gap_s=max_gap_s
        )

    resolutions: list[FoldResolution] = []
    for ping, candidates in expanded:
        first = candidates[0]
        if not first.valid:
            resolutions.append(
                FoldResolution(
                    record_id=ping.record_id,
                    candidates=tuple(candidates),
                    status="invalid",
                    selected_fold=None,
                    selected_utc_epoch_s=None,
                    method="invalid-nonexistent",
                    margin_s=None,
                    reason_codes=first.reason_codes,
                )
            )
        elif not first.ambiguous:
            resolutions.append(
                FoldResolution(
                    record_id=ping.record_id,
                    candidates=tuple(candidates),
                    status="resolved",
                    selected_fold=0,
                    selected_utc_epoch_s=first.utc_epoch_s,
                    method="unambiguous",
                    margin_s=None,
                    reason_codes=(),
                )
            )
        elif selected_fold is None:
            resolutions.append(
                FoldResolution(
                    record_id=ping.record_id,
                    candidates=tuple(candidates),
                    status="unresolved",
                    selected_fold=None,
                    selected_utc_epoch_s=None,
                    method=group_method,
                    margin_s=None,
                    reason_codes=(
                        Reason.TIME_AMBIGUOUS_FOLD,
                        Reason.TIME_UNRESOLVED_FOLD,
                    ),
                )
            )
        else:
            chosen = next(c for c in candidates if c.fold == selected_fold)
            resolutions.append(
                FoldResolution(
                    record_id=ping.record_id,
                    candidates=tuple(candidates),
                    status="resolved",
                    selected_fold=selected_fold,
                    selected_utc_epoch_s=chosen.utc_epoch_s,
                    method=group_method,
                    margin_s=group_margin,
                    reason_codes=(
                        Reason.TIME_AMBIGUOUS_FOLD,
                        Reason.TIME_FOLD_SEQUENCE_RESOLVED,
                    ),
                )
            )
    return resolutions


def apc_time_candidates(
    source_record_id: str,
    arrival_wall: dt.datetime | None,
    departure_wall: dt.datetime | None,
    *,
    tz: str = DEFAULT_TZ,
) -> dict[str, list[TimeCandidate]]:
    """Expand APC arrival/departure wall times into candidate sets.

    Keys are ``"arrival"`` and ``"departure"``; a missing input yields
    an empty list. APC records are NEVER independently fold-resolved
    here (design rule D-05): for an ambiguous time both fold candidates
    are returned and stay unresolved until (unless) an attachment
    decision selects one elsewhere.
    """
    return {
        "arrival": (
            []
            if arrival_wall is None
            else wall_time_candidates(source_record_id, arrival_wall, tz=tz)
        ),
        "departure": (
            []
            if departure_wall is None
            else wall_time_candidates(source_record_id, departure_wall, tz=tz)
        ),
    }
