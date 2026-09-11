"""Repaired boundary decision kernel (Spike 02R, spec section 11/16).

Replaces Spike-02's pairwise/procedural boundary selection with:

- COMPLETE boundary candidates carrying evidence regime, censorship
  class, and dependency records;
- an explicit ``NO_BOUNDARY`` alternative: a candidate is applied only
  when the global objective is strictly better WITH it than without it
  — never because it is the only surviving proposal;
- a bounded global objective (DP over non-overlapping cut intervals)
  with a fragmentation penalty for consecutive cuts that would create
  sub-minimum segments;
- EXACT per-candidate optimum analysis: a candidate is applied only if
  it is mandatory across ALL optimal selections (forbidding it strictly
  lowers the optimum); candidates that appear in some but not all
  optima are tied and abstain. No enumeration truncation exists.

Candidate IDs order serialization only; they never break a tie.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .spike02_contracts import EpisodeStatus, SignalQuality, TerminalEpisode
from .spike02r_contracts import (
    NO_BOUNDARY,
    BoundaryCandidateV2,
    BoundaryDecisionV2,
    CensorClass,
    Reason02R,
)

_APC_FAMILIES = {"apc_alighting_signal_before", "apc_boarding_signal_after"}
_PHYSICAL = {
    "terminal_geofence",
    "sustained_dwell",
    "geometry_progress_reversal",
    "sustained_outbound_motion",
}


@dataclass(frozen=True)
class BoundaryV2Config:
    """Experimental, unapproved kernel parameters."""

    family_weight_q: int = 150_000
    physical_weight_q: int = 100_000
    q3_bonus_q: int = 200_000
    q2_bonus_q: int = 50_000
    no_boundary_bar_q: int = 400_000  # reward = qscore - bar
    min_segment_s: int = 900
    fragmentation_penalty_q: int = 120_000
    tie_epsilon_q: int = 0  # exact integer ties only


def _cand_id(run_id: str, episode_id: str, profile: str) -> str:
    key = f"xbd2r|{run_id}|{episode_id}|{profile}"
    return "xbd2r-" + hashlib.sha256(key.encode()).hexdigest()[:24]


def candidates_from_episodes(
    run_id: str,
    episodes: list[TerminalEpisode],
    *,
    profile: str,  # "avl" | "apc-structural" | "apc-activity"
    cfg: BoundaryV2Config,
) -> list[BoundaryCandidateV2]:
    """Complete boundary candidates for one run under one profile.

    Censorship classes: fold-unresolved episodes are
    ``plausible_but_censored`` (blocked by the current fold heuristic,
    spec section 12B), APC/ordering conflicts are ``contract_unresolved``
    at APC profiles, insufficient-evidence visits stay in the candidate
    universe as weak (their reward is negative, so NO_BOUNDARY beats
    them on evidence, not by censorship).
    """
    out: list[BoundaryCandidateV2] = []
    avl_only = profile == "avl"
    for ep in sorted(episodes, key=lambda e: (e.ping_index_first, e.episode_id)):
        if ep.run_id != run_id:
            continue
        families = set(ep.evidence_families)
        if avl_only:
            families -= _APC_FAMILIES
        apc_quality = None if avl_only else ep.apc_quality
        use_service = (
            not avl_only
            and apc_quality == SignalQuality.Q3_STRONG
            and ep.service_end_s is not None
            and ep.service_start_s is not None
            and ep.ordering_valid
        )
        if use_service:
            start, end = ep.service_end_s, ep.service_start_s
        elif ep.physical_arrival_s is not None and (
            ep.physical_departure_s is not None
        ):
            start, end = ep.physical_arrival_s, ep.physical_departure_s
        else:
            start = end = None

        score = cfg.family_weight_q * len(families)
        if families & _PHYSICAL:
            score += cfg.physical_weight_q
        if apc_quality == SignalQuality.Q3_STRONG:
            score += cfg.q3_bonus_q
        elif apc_quality == SignalQuality.Q2_USABLE:
            score += cfg.q2_bonus_q

        if start is None:
            hard = CensorClass.PLAUSIBLE_BUT_CENSORED  # fold heuristic
            known = [
                t
                for t in (
                    ep.physical_arrival_s,
                    ep.service_end_s,
                    ep.service_start_s,
                    ep.physical_departure_s,
                )
                if t is not None
            ]
            start = end = min(known) if known else 0
        elif ep.status == EpisodeStatus.AMBIGUOUS:
            hard = (
                CensorClass.CONTRACT_UNRESOLVED
                if not avl_only
                else CensorClass.PLAUSIBLE_BUT_CENSORED
            )
        else:
            hard = CensorClass.HARD_VALID

        out.append(
            BoundaryCandidateV2(
                candidate_id=_cand_id(run_id, ep.episode_id, profile),
                activity_run_id=run_id,
                candidate_start_s=start,
                candidate_end_s=end,
                boundary_role=(
                    "short_turn" if ep.kind == "short_turn_candidate" else "terminal"
                ),
                evidence_families=tuple(sorted(families)),
                evidence_regime="avl_only" if avl_only else "apc_evidence",
                qscore=score,
                hard_status=hard.value,
                dependency_records=(() if avl_only else ep.apc_record_ids),
                service_derived=use_service,
                reason_codes=tuple(ep.reason_codes),
            )
        )
    return out


def _overlap(a: BoundaryCandidateV2, b: BoundaryCandidateV2) -> bool:
    return not (
        b.candidate_start_s >= a.candidate_end_s
        or a.candidate_start_s >= b.candidate_end_s
    )


def _best_selection_value(
    cands: list[BoundaryCandidateV2],
    rewards: list[int],
    cfg: BoundaryV2Config,
    *,
    forbidden: int | None = None,
    forced: int | None = None,
) -> int:
    """Exact DP over orderable candidates: max total reward of any
    non-overlapping selection, with a fragmentation penalty when two
    consecutive selected cuts are closer than ``min_segment_s``.
    ``forbidden``/``forced`` restrict candidate participation."""
    n = len(cands)
    NEG = -(1 << 60)
    # ordered by start; dp[i] = best value of a selection whose LAST
    # selected candidate is i.
    dp = [NEG] * n
    for i in range(n):
        if forbidden == i:
            continue
        base = rewards[i]
        best_prev = 0
        for j in range(i):
            if dp[j] == NEG:
                continue
            if _overlap(cands[j], cands[i]):
                continue
            if cands[j].candidate_end_s > cands[i].candidate_start_s:
                continue
            gap = cands[i].candidate_start_s - cands[j].candidate_end_s
            pen = cfg.fragmentation_penalty_q if gap < cfg.min_segment_s else 0
            best_prev = max(best_prev, dp[j] - pen)
        dp[i] = base + best_prev
    if forced is not None:
        # best selection that INCLUDES forced: dp[forced] + best suffix
        # after forced (computed by symmetric pass)
        return _suffix_value(cands, rewards, cfg, dp, forced)
    return max([0] + [v for v in dp if v != NEG])


def _suffix_value(cands, rewards, cfg, dp, k) -> int:
    NEG = -(1 << 60)
    if dp[k] == NEG:
        return NEG
    n = len(cands)
    suf = [NEG] * n
    best = 0
    for i in range(n - 1, -1, -1):
        base = rewards[i]
        best_next = 0
        for j in range(i + 1, n):
            if suf[j] == NEG:
                continue
            if _overlap(cands[i], cands[j]):
                continue
            if cands[i].candidate_end_s > cands[j].candidate_start_s:
                continue
            gap = cands[j].candidate_start_s - cands[i].candidate_end_s
            pen = cfg.fragmentation_penalty_q if gap < cfg.min_segment_s else 0
            best_next = max(best_next, suf[j] - pen)
        suf[i] = base + best_next
    _ = best
    # dp[k] already includes k's reward and its best prefix; suf[k]
    # includes k's reward and its best suffix — subtract one copy.
    return dp[k] + suf[k] - rewards[k]


def decide_boundaries_v2(
    run_id: str,
    candidates: list[BoundaryCandidateV2],
    *,
    cfg: BoundaryV2Config,
) -> list[BoundaryDecisionV2]:
    """Global decision with explicit NO_BOUNDARY and exact stability.

    A candidate is APPLIED only when it is mandatory in every optimal
    selection: forbidding it strictly reduces the optimum. Appearing in
    some optimum but not all -> tied abstention. Never applied because
    alternatives were censored: censored candidates keep zero reward
    but remain reported.
    """
    ordered = sorted(
        candidates,
        key=lambda c: (c.candidate_start_s, c.candidate_end_s, c.candidate_id),
    )
    competing = [c for c in ordered if c.hard_status == CensorClass.HARD_VALID.value]
    rewards = [c.qscore - cfg.no_boundary_bar_q for c in competing]
    best = _best_selection_value(competing, rewards, cfg)

    decisions: list[BoundaryDecisionV2] = []
    for idx, cand in enumerate(competing):
        with_forced = _best_selection_value(competing, rewards, cfg, forced=idx)
        without = _best_selection_value(competing, rewards, cfg, forbidden=idx)
        in_some = with_forced == best
        mandatory = in_some and without < best
        margin_vs_null = best - without if in_some else None
        if mandatory:
            status, reasons = "applied", ()
        elif in_some:
            status, reasons = "tied_abstain", (Reason02R.BOUNDARY_TIE,)
        elif rewards[idx] <= 0:
            status, reasons = "no_boundary", (Reason02R.NOB_WINS,)
        else:
            status, reasons = "not_in_optimum", (Reason02R.BOUNDARY_FRAG_PENALTY,)
        decisions.append(
            BoundaryDecisionV2(
                candidate_id=cand.candidate_id,
                activity_run_id=run_id,
                selected=mandatory,
                mandatory_in_all_optima=mandatory if in_some else False,
                survival_margin_q=margin_vs_null,
                evidence_margin_q=margin_vs_null,
                margin_vs_no_boundary_q=(
                    rewards[idx] if rewards[idx] > 0 else rewards[idx]
                ),
                status=status,
                reason_codes=tuple(dict.fromkeys((*cand.reason_codes, *reasons))),
            )
        )
    for cand in ordered:
        if cand.hard_status != CensorClass.HARD_VALID.value:
            decisions.append(
                BoundaryDecisionV2(
                    candidate_id=cand.candidate_id,
                    activity_run_id=run_id,
                    selected=False,
                    mandatory_in_all_optima=None,
                    survival_margin_q=None,
                    evidence_margin_q=None,
                    margin_vs_no_boundary_q=None,
                    status=f"censored:{cand.hard_status}",
                    reason_codes=cand.reason_codes,
                )
            )
    if not competing or best == 0:
        decisions.append(
            BoundaryDecisionV2(
                candidate_id=NO_BOUNDARY,
                activity_run_id=run_id,
                selected=best == 0,
                mandatory_in_all_optima=None,
                survival_margin_q=None,
                evidence_margin_q=None,
                margin_vs_no_boundary_q=None,
                status="no_boundary",
                reason_codes=(Reason02R.NOB_WINS,),
            )
        )
    return decisions


def applied_cuts(
    candidates: list[BoundaryCandidateV2],
    decisions: list[BoundaryDecisionV2],
) -> list[BoundaryCandidateV2]:
    sel = {d.candidate_id for d in decisions if d.selected}
    return sorted(
        (c for c in candidates if c.candidate_id in sel),
        key=lambda c: c.candidate_start_s,
    )
