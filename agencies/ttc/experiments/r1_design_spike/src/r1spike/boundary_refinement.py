"""One-pass boundary refinement of provisional activity runs (Spike 02).

Refinement happens EXACTLY ONCE per run: candidates are gathered from
terminal/short-turn episodes, decided deterministically (absolute bar +
margin on integer rule scores; ties and ambiguity are retained, never
broken by IDs), and applied in a single pass, after which membership is
frozen. There is deliberately no API that accepts refined segments back
into refinement — the segment→APC→segment feedback cycle is the failure
mode this restriction exists to prevent. A post-refinement diagnostic
may REPORT residual boundary evidence but cannot apply it.

Variants (experimental):

- ``v1_avl_only``  — terminal geofence + motion/dwell + direction or
  progress reversal; APC count-signal evidence ignored; the cut is the
  coarse physical buffer [physical_arrival, physical_departure).
- ``v2_conditional`` — V1 plus conditional APC precedence: only a
  Q3-quality episode with BOTH service_end and service_start and valid
  ordering may narrow the cut to [service_end, service_start); weak or
  conflicting APC falls back to the V1 buffer; all-weak evidence
  abstains (boundary retained as ambiguous, run not split there).
- ``vx_unconditional`` — INTENTIONALLY UNSAFE diagnostic: any usable
  APC signal (>= Q1) at a terminal visit forces a boundary, bypassing
  the evidence gate. Exists only to measure the damage; never a
  recommended design.

Every AVL ping of the run is conserved into exactly one refined
segment, turnaround interval, or unresolved region; refined segments
never overlap and no ping appears twice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .segmentation_baselines import SegPing
from .spike02_contracts import (
    ALGORITHM_VERSION02,
    BOUNDARY_ID_VERSION,
    REFINED_ID_VERSION,
    BoundaryCandidate,
    BoundaryDecision,
    BoundaryStatus,
    EpisodeStatus,
    EvidenceFamily,
    PingState,
    Reason02,
    RefinedSegment,
    RefinementResult,
    SegmentTypeHypothesis,
    SignalQuality,
    SpanningApcRole,
    TerminalEpisode,
    TurnaroundInterval,
    VehicleActivityRun,
)

REFINEMENT_VARIANTS = ("v1_avl_only", "v2_conditional", "vx_unconditional")

_APC_FAMILIES = {
    EvidenceFamily.APC_ALIGHTING_BEFORE.value,
    EvidenceFamily.APC_BOARDING_AFTER.value,
}

_NONREVENUE_LABELS = {"PO", "PI"}

_QUALITY_ORDER = {
    SignalQuality.Q0_UNUSABLE: 0,
    SignalQuality.Q1_WEAK: 1,
    SignalQuality.Q2_USABLE: 2,
    SignalQuality.Q3_STRONG: 3,
}


@dataclass(frozen=True)
class RefinementConfig:
    """Experimental, unapproved boundary-decision parameters."""

    score_bar_q: int = 400_000
    margin_q: int = 50_000
    family_weight_q: int = 150_000
    physical_weight_q: int = 100_000
    q3_bonus_q: int = 200_000
    q2_bonus_q: int = 50_000


def _boundary_id(run_id: str, anchor: str) -> str:
    key = json.dumps(
        [BOUNDARY_ID_VERSION, ALGORITHM_VERSION02, run_id, anchor],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{BOUNDARY_ID_VERSION}-{hashlib.sha256(key.encode()).hexdigest()[:32]}"


def _refined_id(variant: str, run_id: str, record_ids: tuple[str, ...]) -> str:
    key = json.dumps(
        [REFINED_ID_VERSION, ALGORITHM_VERSION02, variant, run_id, list(record_ids)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{REFINED_ID_VERSION}-{hashlib.sha256(key.encode()).hexdigest()[:32]}"


def _first_index_at_or_after(pings: list[SegPing], instant_s: int) -> int:
    for i, p in enumerate(pings):
        if p.ordering_instant >= instant_s:
            return i
    return len(pings)


def propose_boundaries(
    run: VehicleActivityRun,
    pings: list[SegPing],
    episodes: list[TerminalEpisode],
    *,
    variant: str,
    cfg: RefinementConfig,
) -> list[BoundaryCandidate]:
    """Deterministic boundary candidates for one run under one variant."""
    if variant not in REFINEMENT_VARIANTS:
        raise ValueError(f"unknown refinement variant {variant!r}")
    candidates: list[BoundaryCandidate] = []
    for ep in sorted(episodes, key=lambda e: (e.ping_index_first, e.episode_id)):
        if ep.run_id != run.run_id:
            continue
        families = set(ep.evidence_families)
        if variant == "v1_avl_only":
            families -= _APC_FAMILIES
        physical = bool(
            families
            & {
                EvidenceFamily.GEOFENCE.value,
                EvidenceFamily.DWELL.value,
                EvidenceFamily.PROGRESS_REVERSAL.value,
                EvidenceFamily.OUTBOUND_MOTION.value,
            }
        )
        reasons: list[str] = list(ep.reason_codes)

        apc_quality = ep.apc_quality if variant != "v1_avl_only" else None
        apc_precedence = False
        cut_start: int | None = None
        cut_end: int | None = None

        if (
            variant == "v2_conditional"
            and apc_quality == SignalQuality.Q3_STRONG
            and ep.service_end_s is not None
            and ep.service_start_s is not None
            and ep.ordering_valid
        ):
            apc_precedence = True
            cut_start, cut_end = ep.service_end_s, ep.service_start_s
            reasons.append(Reason02.BOUNDARY_APPLIED_APC)
        elif variant == "vx_unconditional" and ep.apc_record_ids:
            # ANY aligned APC record forces a boundary — including weak
            # Q1 evidence and insufficient-evidence visits. This is the
            # intentionally unsafe behaviour under test.
            apc_precedence = True
            cut_start = (
                ep.service_end_s
                if ep.service_end_s is not None
                else ep.physical_arrival_s
            )
            cut_end = (
                ep.service_start_s
                if ep.service_start_s is not None
                else ep.physical_departure_s
            )
            reasons.append(Reason02.BOUNDARY_APPLIED_APC)
        if cut_start is None or cut_end is None:
            if ep.physical_arrival_s is None or ep.physical_departure_s is None:
                continue  # fold-unresolved or timeless: no candidate at all
            cut_start, cut_end = ep.physical_arrival_s, ep.physical_departure_s
            apc_precedence = False
            reasons.append(Reason02.BOUNDARY_APPLIED_AVL)
        if cut_end < cut_start:
            # APC-derived interval inverted relative to expectations —
            # never reorder; keep the physical buffer instead.
            if ep.physical_arrival_s is None or ep.physical_departure_s is None:
                continue
            cut_start, cut_end = ep.physical_arrival_s, ep.physical_departure_s
            apc_precedence = False
            reasons.append(Reason02.EPISODE_APC_CONFLICT)

        score = cfg.family_weight_q * len(families)
        if physical:
            score += cfg.physical_weight_q
        if apc_quality == SignalQuality.Q3_STRONG:
            score += cfg.q3_bonus_q
        elif apc_quality == SignalQuality.Q2_USABLE:
            score += cfg.q2_bonus_q

        first_inside = _first_index_at_or_after(pings, cut_start)
        first_after = _first_index_at_or_after(pings, cut_end)
        candidates.append(
            BoundaryCandidate(
                boundary_id=_boundary_id(run.run_id, ep.episode_id + ":" + variant),
                run_id=run.run_id,
                episode_id=ep.episode_id,
                source=(
                    "terminal_episode"
                    if ep.kind == "terminal"
                    else "short_turn_episode"
                ),
                cut_start_s=cut_start,
                cut_end_s=cut_end,
                cut_index_first_inside=first_inside,
                cut_index_first_after=first_after,
                evidence_families=tuple(sorted(families)),
                apc_precedence_used=apc_precedence,
                quality=apc_quality,
                score_q=score,
                reason_codes=tuple(dict.fromkeys(reasons)),
            )
        )
    return candidates


def _decide_boundaries(
    candidates: list[BoundaryCandidate],
    episodes_by_id: dict[str, TerminalEpisode],
    *,
    variant: str,
    cfg: RefinementConfig,
) -> list[BoundaryDecision]:
    """Bar + conflict/margin decisions. Ambiguity is retained: an
    episode that is not SELECTED never applies its boundary. One
    variant-scoped exception: the AVL-only variant is not blocked by
    ambiguity that is PURELY APC-derived (an APC/AVL conflict or an
    ordering violation among APC service times) — its physical buffer
    is unaffected evidence. Fold-unresolved ambiguity blocks every
    variant."""
    _APC_ONLY_AMBIGUITY = {
        Reason02.EPISODE_APC_CONFLICT,
        Reason02.EPISODE_ORDER_VIOLATION,
    }
    decisions: dict[str, BoundaryDecision] = {}
    passing: list[BoundaryCandidate] = []
    for cand in candidates:
        ep = episodes_by_id.get(cand.episode_id or "")
        gate_ok = ep is not None and ep.status == EpisodeStatus.SELECTED
        if (
            not gate_ok
            and variant == "v1_avl_only"
            and ep is not None
            and ep.status == EpisodeStatus.AMBIGUOUS
        ):
            blocking = set(ep.reason_codes) & (
                _APC_ONLY_AMBIGUITY
                | {
                    Reason02.BOUNDARY_FOLD_UNRESOLVED,
                    Reason02.EPISODE_INSUFFICIENT_EVIDENCE,
                }
            )
            gate_ok = bool(blocking) and blocking <= _APC_ONLY_AMBIGUITY
        if variant == "vx_unconditional" and cand.apc_precedence_used:
            gate_ok = ep is not None
        bar_ok = cand.score_q >= cfg.score_bar_q or (
            variant == "vx_unconditional" and cand.apc_precedence_used
        )
        if not gate_ok:
            decisions[cand.boundary_id] = BoundaryDecision(
                boundary_id=cand.boundary_id,
                run_id=cand.run_id,
                status=BoundaryStatus.AMBIGUOUS_NOT_APPLIED,
                score_q=cand.score_q,
                margin_q=None,
                reason_codes=tuple(
                    dict.fromkeys((*cand.reason_codes, Reason02.BOUNDARY_AMBIGUOUS))
                ),
            )
            continue
        if not bar_ok:
            decisions[cand.boundary_id] = BoundaryDecision(
                boundary_id=cand.boundary_id,
                run_id=cand.run_id,
                status=BoundaryStatus.BELOW_BAR,
                score_q=cand.score_q,
                margin_q=None,
                reason_codes=tuple(
                    dict.fromkeys((*cand.reason_codes, Reason02.BOUNDARY_LOW_SCORE))
                ),
            )
            continue
        passing.append(cand)

    # Conflict resolution among overlapping cut intervals.
    passing.sort(key=lambda c: (c.cut_start_s, c.cut_end_s, c.boundary_id))
    suppressed: set[str] = set()
    for i, a in enumerate(passing):
        for b in passing[i + 1 :]:
            # Half-open cut intervals that merely touch do not overlap.
            if b.cut_start_s >= a.cut_end_s:
                break
            # overlap: compare complete hypotheses by score with margin
            if a.boundary_id in suppressed or b.boundary_id in suppressed:
                continue
            margin = abs(a.score_q - b.score_q)
            if margin < cfg.margin_q:
                for c in (a, b):
                    suppressed.add(c.boundary_id)
                    decisions[c.boundary_id] = BoundaryDecision(
                        boundary_id=c.boundary_id,
                        run_id=c.run_id,
                        status=BoundaryStatus.AMBIGUOUS_NOT_APPLIED,
                        score_q=c.score_q,
                        margin_q=margin,
                        reason_codes=tuple(
                            dict.fromkeys(
                                (
                                    *c.reason_codes,
                                    Reason02.BOUNDARY_CONFLICT,
                                    Reason02.BOUNDARY_TIE,
                                )
                            )
                        ),
                    )
            else:
                loser = a if a.score_q < b.score_q else b
                suppressed.add(loser.boundary_id)
                decisions[loser.boundary_id] = BoundaryDecision(
                    boundary_id=loser.boundary_id,
                    run_id=loser.run_id,
                    status=BoundaryStatus.CONFLICT_SUPPRESSED,
                    score_q=loser.score_q,
                    margin_q=margin,
                    reason_codes=tuple(
                        dict.fromkeys((*loser.reason_codes, Reason02.BOUNDARY_CONFLICT))
                    ),
                )

    for cand in passing:
        if cand.boundary_id in suppressed:
            continue
        decisions[cand.boundary_id] = BoundaryDecision(
            boundary_id=cand.boundary_id,
            run_id=cand.run_id,
            status=BoundaryStatus.APPLIED,
            score_q=cand.score_q,
            margin_q=None,
            reason_codes=cand.reason_codes,
        )
    return [decisions[c.boundary_id] for c in candidates]


def _segment_type(route_counts: dict[str, int]) -> SegmentTypeHypothesis:
    if not route_counts:
        return SegmentTypeHypothesis.UNRESOLVED
    total = sum(route_counts.values())
    nonrevenue = sum(route_counts.get(lbl, 0) for lbl in _NONREVENUE_LABELS)
    if nonrevenue * 2 > total:
        return SegmentTypeHypothesis.DEADHEAD_OR_NONREVENUE_UNCONFIRMED
    return SegmentTypeHypothesis.REVENUE_CANDIDATE


def refine_run(
    run: VehicleActivityRun,
    pings: list[SegPing],
    episodes: list[TerminalEpisode],
    *,
    variant: str,
    cfg: RefinementConfig,
) -> RefinementResult:
    """The single refinement pass for one provisional run.

    Deterministic and idempotent: the same inputs always produce the
    same frozen result; there is no iterative loop and no API to feed a
    result back in.
    """
    episodes_by_id = {e.episode_id: e for e in episodes}
    candidates = propose_boundaries(run, pings, episodes, variant=variant, cfg=cfg)
    decisions = _decide_boundaries(candidates, episodes_by_id, variant=variant, cfg=cfg)
    decisions_by_id = {d.boundary_id: d for d in decisions}
    applied = [
        c
        for c in candidates
        if decisions_by_id[c.boundary_id].status == BoundaryStatus.APPLIED
    ]
    applied.sort(key=lambda c: (c.cut_index_first_inside, c.boundary_id))

    ping_states: dict[str, str] = {}
    cut_owner: dict[int, BoundaryCandidate] = {}
    # Applied cuts that own NO ping index (instantaneous cuts, or cut
    # intervals falling entirely between two pings) still split the run:
    # they are flushed as pure boundaries with an empty turnaround.
    empty_cuts: dict[int, list[BoundaryCandidate]] = {}
    for cand in applied:
        if cand.cut_index_first_inside >= cand.cut_index_first_after:
            empty_cuts.setdefault(cand.cut_index_first_inside, []).append(cand)
            continue
        for idx in range(cand.cut_index_first_inside, cand.cut_index_first_after):
            if 0 <= idx < len(pings) and idx not in cut_owner:
                cut_owner[idx] = cand

    segments: list[RefinedSegment] = []
    turnarounds: list[TurnaroundInterval] = []
    unresolved_ids: list[str] = []

    current: list[tuple[int, SegPing]] = []
    boundary_before: str | None = None

    def flush_segment(boundary_after: str | None) -> None:
        nonlocal current, boundary_before
        if not current:
            boundary_before = boundary_after
            return
        members = [p for _, p in current]
        record_ids = tuple(p.record_id for p in members)
        route_counts: dict[str, int] = {}
        for p in members:
            if p.route_label is not None:
                route_counts[p.route_label] = route_counts.get(p.route_label, 0) + 1
        n_unres = sum(1 for p in members if p.fold_status == "unresolved")
        segments.append(
            RefinedSegment(
                refined_segment_id=_refined_id(variant, run.run_id, record_ids),
                run_id=run.run_id,
                variant=variant,
                record_ids=record_ids,
                start_instant_s=members[0].ordering_instant,
                end_instant_s=members[-1].ordering_instant,
                segment_type=_segment_type(route_counts),
                boundary_id_before=boundary_before,
                boundary_id_after=boundary_after,
                time_uncertain=n_unres > 0,
                n_unresolved=n_unres,
                local_dates=tuple(sorted({p.local_date for p in members})),
                route_label_counts=route_counts,
            )
        )
        for p in members:
            ping_states[p.record_id] = PingState.SEGMENT.value
        current = []
        boundary_before = boundary_after

    i = 0
    while i < len(pings):
        for empty in empty_cuts.get(i, ()):
            flush_segment(empty.boundary_id)
        cand = cut_owner.get(i)
        if cand is None:
            current.append((i, pings[i]))
            i += 1
            continue
        flush_segment(cand.boundary_id)
        members: list[SegPing] = []
        while i < len(pings) and cut_owner.get(i) is cand:
            members.append(pings[i])
            i += 1
        ep = episodes_by_id.get(cand.episode_id or "")
        well_formed = ep is not None and ep.status == EpisodeStatus.SELECTED
        state = (
            PingState.TURNAROUND.value
            if well_formed
            else PingState.BOUNDARY_UNRESOLVED.value
        )
        for p in members:
            ping_states[p.record_id] = state
            if state == PingState.BOUNDARY_UNRESOLVED.value:
                unresolved_ids.append(p.record_id)
        if members and state == PingState.TURNAROUND.value:
            turnarounds.append(
                TurnaroundInterval(
                    turnaround_id=_boundary_id(run.run_id, "ta:" + cand.boundary_id),
                    run_id=run.run_id,
                    episode_id=cand.episode_id,
                    cluster_id=(ep.cluster_id if ep else None),
                    start_instant_s=members[0].ordering_instant,
                    end_instant_s=members[-1].ordering_instant,
                    record_ids=tuple(p.record_id for p in members),
                )
            )
    for empty in empty_cuts.get(len(pings), ()):
        flush_segment(empty.boundary_id)
    flush_segment(None)

    if segments:
        first = segments[0]
        segments[0] = RefinedSegment(
            **{
                **_as_kwargs(first),
                "censored_start": run.boundary_censored_start,
            }
        )
        last = segments[-1]
        segments[-1] = RefinedSegment(
            **{
                **_as_kwargs(last),
                "censored_end": run.boundary_censored_end,
            }
        )

    # Conservation invariants (fail loudly, never silently).
    if len(ping_states) != len(pings):
        raise AssertionError(
            f"ping conservation violated in run {run.run_id}: "
            f"{len(ping_states)} states for {len(pings)} pings"
        )
    seen: set[str] = set()
    for seg in segments:
        for rid in seg.record_ids:
            if rid in seen:
                raise AssertionError(f"ping {rid} owned by two refined segments")
            seen.add(rid)

    return RefinementResult(
        run_id=run.run_id,
        variant=variant,
        refined_segments=tuple(segments),
        turnarounds=tuple(turnarounds),
        unresolved_record_ids=tuple(unresolved_ids),
        ping_states=ping_states,
        boundary_decisions=tuple(decisions),
        applied_boundary_ids=tuple(c.boundary_id for c in applied),
        ambiguous_boundary_ids=tuple(
            d.boundary_id
            for d in decisions
            if d.status == BoundaryStatus.AMBIGUOUS_NOT_APPLIED
        ),
        was_split=len(segments) > 1,
    )


def _as_kwargs(seg: RefinedSegment) -> dict:
    return {
        "refined_segment_id": seg.refined_segment_id,
        "run_id": seg.run_id,
        "variant": seg.variant,
        "record_ids": seg.record_ids,
        "start_instant_s": seg.start_instant_s,
        "end_instant_s": seg.end_instant_s,
        "segment_type": seg.segment_type,
        "boundary_id_before": seg.boundary_id_before,
        "boundary_id_after": seg.boundary_id_after,
        "time_uncertain": seg.time_uncertain,
        "n_unresolved": seg.n_unresolved,
        "local_dates": seg.local_dates,
        "route_label_counts": seg.route_label_counts,
        "censored_start": seg.censored_start,
        "censored_end": seg.censored_end,
        "reason_codes": seg.reason_codes,
    }


def spanning_roles(
    result: RefinementResult,
    episodes: list[TerminalEpisode],
) -> list[SpanningApcRole]:
    """Dual evidence roles for boundary-spanning APC records at APPLIED
    boundaries. One source record, two derived anchors; counts are
    never duplicated and no second passenger event is created."""
    seg_by_boundary_after: dict[str, str] = {}
    seg_by_boundary_before: dict[str, str] = {}
    for seg in result.refined_segments:
        if seg.boundary_id_after is not None:
            seg_by_boundary_after[seg.boundary_id_after] = seg.refined_segment_id
        if seg.boundary_id_before is not None:
            seg_by_boundary_before[seg.boundary_id_before] = seg.refined_segment_id

    episode_by_boundary = {
        _boundary_id(e.run_id, e.episode_id + ":" + result.variant): e
        for e in episodes
        if e.run_id == result.run_id
    }
    roles: list[SpanningApcRole] = []
    applied = set(result.applied_boundary_ids)
    for d in result.boundary_decisions:
        if d.boundary_id not in applied:
            continue
        inbound = seg_by_boundary_after.get(d.boundary_id)
        outbound = seg_by_boundary_before.get(d.boundary_id)
        episode = episode_by_boundary.get(d.boundary_id)
        if episode is None:
            continue
        for rid in episode.spanning_apc_record_ids:
            roles.append(
                SpanningApcRole(
                    apc_source_record_id=rid,
                    run_id=result.run_id,
                    episode_id=episode.episode_id,
                    boundary_role="spanning_terminal_visit",
                    arrival_role_hypothesis="inbound_service_end_evidence",
                    departure_role_hypothesis="outbound_service_start_evidence",
                    arrival_segment_candidate=inbound,
                    departure_segment_candidate=outbound,
                    dual_role=True,
                    reason_codes=(Reason02.SPAN_DUAL_ROLE,),
                )
            )
    return roles


def residual_boundary_diagnostic(
    result: RefinementResult,
    episodes: list[TerminalEpisode],
) -> dict[str, int]:
    """Report-only: evidence that a second pass WOULD act on. Never
    applies anything (single-pass restriction is load-bearing)."""
    applied = set(result.applied_boundary_ids)
    ambiguous = set(result.ambiguous_boundary_ids)
    n_selected_unapplied = 0
    for e in episodes:
        if e.run_id != result.run_id or e.status != EpisodeStatus.SELECTED:
            continue
        bid = _boundary_id(e.run_id, e.episode_id + ":" + result.variant)
        if bid not in applied and bid not in ambiguous:
            n_selected_unapplied += 1
    return {
        "applied_boundaries": len(applied),
        "ambiguous_boundaries": len(ambiguous),
        "selected_episodes_without_applied_boundary": n_selected_unapplied,
    }
