"""Experimental AVL segmentation baselines X4-A and X4-B.

Formal S4 is frozen; these are deliberately named X4-* so they cannot be
mistaken for it. Both variants:

- partition by the normalized AVL vehicle namespace (``vehicle_id_raw``);
- order by time hypotheses (resolved instant, else earliest candidate
  instant), never by physical CSV order;
- never use ``tatripId`` as a partition key;
- keep cross-midnight continuity (no split at a date boundary);
- never split on an *uncertain* gap: two pings split only when the
  minimal possible gap across their retained time candidates exceeds
  the threshold;
- mark segments containing unresolved-fold pings ``time_uncertain``;
- mint only an experimental segment identifier (``xs1-<32 hex>``),
  never ``trip_id_performed``.

X4-B adds soft, debounced context evidence (route/destination/run/
``tatripId`` sustained changes) with a vote rule. Every threshold is
experimental configuration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from . import ALGORITHM_VERSION
from .contracts import Reason

SEGMENT_ID_VERSION = "xs1"

CONTEXT_FIELDS = ("route_label", "destination_label", "run_label", "tatrip_id")


@dataclass(frozen=True, slots=True)
class SegPing:
    """One AVL ping with its time-hypothesis summary."""

    record_id: str
    vehicle: str
    instant_s: int | None  # resolved UTC epoch seconds, or None
    candidate_instants: tuple[int, ...]  # all retained candidates (>=1)
    fold_status: str  # "resolved" | "unresolved" | "invalid"
    local_date: str  # ISO date of the wall time (evidence)
    route_label: str | None
    destination_label: str | None
    run_label: str | None
    tatrip_id: str | None
    latitude: float | None
    longitude: float | None

    @property
    def ordering_instant(self) -> int:
        if self.instant_s is not None:
            return self.instant_s
        return min(self.candidate_instants)


@dataclass(frozen=True)
class Segment:
    segment_id: str
    variant: str
    vehicle: str
    record_ids: tuple[str, ...]  # ordered source lineage
    start_instant_s: int
    end_instant_s: int
    time_uncertain: bool
    n_unresolved: int
    local_dates: tuple[str, ...]  # distinct, sorted
    route_label_counts: dict[str, int] = field(hash=False)
    boundary_reasons: tuple[str, ...] = ()

    @property
    def duration_s(self) -> int:
        return self.end_instant_s - self.start_instant_s

    @property
    def crosses_midnight(self) -> bool:
        return len(self.local_dates) > 1

    @property
    def route_purity_millis(self) -> int | None:
        total = sum(self.route_label_counts.values())
        if total == 0:
            return None
        return max(self.route_label_counts.values()) * 1000 // total


def _segment_id(variant: str, vehicle: str, record_ids: list[str]) -> str:
    key = json.dumps(
        [SEGMENT_ID_VERSION, ALGORITHM_VERSION, variant, vehicle, record_ids],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"{SEGMENT_ID_VERSION}-{digest}"


def _min_possible_gap(a: SegPing, b: SegPing) -> int:
    """Smallest |gap| over the retained candidate instants of both pings.

    Conservative: an uncertain gap (any candidate pair within the
    threshold) must not split a segment.
    """
    a_c = a.candidate_instants if a.instant_s is None else (a.instant_s,)
    b_c = b.candidate_instants if b.instant_s is None else (b.instant_s,)
    return min(abs(y - x) for x in a_c for y in b_c)


def order_pings(pings: list[SegPing]) -> list[SegPing]:
    """Canonical ordering: ordering instant, then record id.

    The record-id tiebreak is serialization determinism only — two pings
    with equal instants produce the same segmentation either way because
    their mutual gap is zero.
    """
    return sorted(pings, key=lambda p: (p.ordering_instant, p.record_id))


def _sustained(
    pings: list[SegPing],
    idx: int,
    fld: str,
    *,
    debounce_n: int,
    debounce_span_s: int,
) -> bool:
    """Is the new value of ``fld`` at index ``idx`` sustained?"""
    new_value = getattr(pings[idx], fld)
    run = [p for p in pings[idx:] if getattr(p, fld) == new_value]
    sustained_run: list[SegPing] = []
    for p in pings[idx:]:
        if getattr(p, fld) != new_value:
            break
        sustained_run.append(p)
    if len(sustained_run) < debounce_n:
        return False
    span = sustained_run[-1].ordering_instant - sustained_run[0].ordering_instant
    _ = run
    return span >= debounce_span_s


def segment_vehicle(
    pings: list[SegPing],
    *,
    variant: str,
    max_continuity_gap_s: int,
    x4b_debounce_pings_n: int = 5,
    x4b_debounce_span_s: int = 120,
    x4b_min_votes: int = 2,
) -> list[Segment]:
    """Segment one vehicle's pings under variant ``x4a`` or ``x4b``."""
    if variant not in {"x4a", "x4b"}:
        raise ValueError(f"unknown segmentation variant {variant!r}")
    usable = [p for p in pings if p.fold_status != "invalid"]
    if not usable:
        return []
    vehicles = {p.vehicle for p in usable}
    if len(vehicles) != 1:
        raise ValueError("segment_vehicle expects exactly one vehicle namespace")
    ordered = order_pings(usable)

    boundaries: list[tuple[int, str]] = []  # (index of first ping of new seg)
    for i in range(1, len(ordered)):
        gap = _min_possible_gap(ordered[i - 1], ordered[i])
        if gap > max_continuity_gap_s:
            boundaries.append((i, Reason.SEG_GAP_SPLIT))
            continue
        if variant == "x4b":
            votes = 0
            for fld in CONTEXT_FIELDS:
                prev_v = getattr(ordered[i - 1], fld)
                new_v = getattr(ordered[i], fld)
                if prev_v == new_v or new_v is None:
                    continue
                if _sustained(
                    ordered,
                    i,
                    fld,
                    debounce_n=x4b_debounce_pings_n,
                    debounce_span_s=x4b_debounce_span_s,
                ):
                    votes += 1
            if votes >= x4b_min_votes:
                boundaries.append((i, Reason.SEG_CONTEXT_SPLIT))

    segments: list[Segment] = []
    cut_indices = [0] + [b[0] for b in boundaries] + [len(ordered)]
    reason_by_cut = {b[0]: b[1] for b in boundaries}
    for start, end in zip(cut_indices, cut_indices[1:], strict=False):
        members = ordered[start:end]
        if not members:
            continue
        record_ids = [p.record_id for p in members]
        n_unresolved = sum(1 for p in members if p.fold_status == "unresolved")
        route_counts: dict[str, int] = {}
        for p in members:
            if p.route_label is not None:
                route_counts[p.route_label] = route_counts.get(p.route_label, 0) + 1
        reasons: list[str] = []
        if start in reason_by_cut:
            reasons.append(reason_by_cut[start])
        if n_unresolved:
            reasons.append(Reason.SEG_TIME_UNCERTAIN)
        segments.append(
            Segment(
                segment_id=_segment_id(variant, members[0].vehicle, record_ids),
                variant=variant,
                vehicle=members[0].vehicle,
                record_ids=tuple(record_ids),
                start_instant_s=members[0].ordering_instant,
                end_instant_s=members[-1].ordering_instant,
                time_uncertain=n_unresolved > 0,
                n_unresolved=n_unresolved,
                local_dates=tuple(sorted({p.local_date for p in members})),
                route_label_counts=route_counts,
                boundary_reasons=tuple(reasons),
            )
        )
    return segments


def variant_comparison(
    a_segments: list[Segment], b_segments: list[Segment]
) -> dict[str, object]:
    """Report-level disagreement between two segmentation variants."""

    def boundary_set(segments: list[Segment]) -> set[tuple[str, ...]]:
        return {s.record_ids for s in segments}

    a_sets = boundary_set(a_segments)
    b_sets = boundary_set(b_segments)
    return {
        "a_segments": len(a_segments),
        "b_segments": len(b_segments),
        "identical_segments": len(a_sets & b_sets),
        "a_only_segments": len(a_sets - b_sets),
        "b_only_segments": len(b_sets - a_sets),
    }
