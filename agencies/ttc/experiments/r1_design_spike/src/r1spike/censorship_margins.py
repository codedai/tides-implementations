"""Candidate-censorship taxonomy and dual margins (Spike 02R section 12).

Every blocked alternative is classified, kept visible, and — when it is
merely censored rather than disproven — allowed back into the EVIDENCE
margin. Acceptance requires both margins; a decision whose survival
margin passes while its evidence margin fails is a
``unique_survivor_by_exclusion``, never an acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import HardValidity, Reason
from .fixedpoint import interpolate_piecewise, quantize, weighted_mean_q
from .scoring import ScoreContract
from .spike02_contracts import Reason02
from .spike02r_contracts import (
    CensorClass,
    DualMarginDecision,
    Reason02R,
)

# Reasons that constitute PROOF of invalidity (spec 12A): temporal or
# geometric impossibility under the declared contract.
PROOF_INVALID_REASONS = {
    Reason.TEMPORAL_STALE,
    Reason.GEOM_HARD_CONTRADICTION,
}
# Missing feed coverage is absence of evidence, not proof (spec 12D).
COVERAGE_REASONS = {Reason.DATE_NO_FEED}
POLICY_REASONS = {Reason.POLICY_BLOCKED}
# Blocks that are heuristic/experimental censorship (spec 12B).
CENSORED_REASONS = {
    Reason.SEG_TIME_UNCERTAIN,
    Reason.ATTACH_SEGMENT_UNRESOLVED,
    Reason02.ATTACH_TMID_PRUNED,
}
# Blocks that are unresolved contracts/mappings (spec 12C).
CONTRACT_REASONS = {
    Reason.STOP_UNMAPPED,
    Reason.STOP_AMBIGUOUS,
    Reason.ROUTE_UNMAPPED,
    Reason.ROUTE_AMBIGUOUS,
    Reason02.CLUSTER_EXPERIMENTAL_1652,
    Reason02.ATTACH_CLUSTER_RESCUE,
}


def classify_candidate(
    reason_codes: tuple[str, ...],
    hard_validity: str,
    *,
    tmid_pruned: bool = False,
) -> CensorClass:
    reasons = set(reason_codes)
    if tmid_pruned:
        return CensorClass.PLAUSIBLE_BUT_CENSORED
    if hard_validity == HardValidity.VALID.value and not (reasons & CONTRACT_REASONS):
        return CensorClass.HARD_VALID
    if reasons & PROOF_INVALID_REASONS:
        return CensorClass.PROOF_INVALID
    if reasons & POLICY_REASONS:
        return CensorClass.POLICY_BLOCKED
    if reasons & COVERAGE_REASONS:
        return CensorClass.COVERAGE_MISSING
    if reasons & CONTRACT_REASONS:
        return CensorClass.CONTRACT_UNRESOLVED
    if reasons & CENSORED_REASONS:
        return CensorClass.PLAUSIBLE_BUT_CENSORED
    if hard_validity == HardValidity.VALID.value:
        return CensorClass.HARD_VALID
    return CensorClass.PLAUSIBLE_BUT_CENSORED


def score_ignoring_hard_blocks(
    contract: ScoreContract,
    decision: str,
    feature_vector: dict,
    regime,
) -> int:
    """Score a candidate's features while CLAMPING hard-bound violations
    to the transform floor instead of hard-invalidating.

    Used ONLY to give censored alternatives a comparable evidence score
    for the evidence margin — never to accept them.
    """
    families: dict[str, list[tuple[int, int]]] = {}
    for name in sorted(feature_vector):
        spec = contract.features.get(name)
        if spec is None or decision not in spec.decisions:
            continue
        if regime.value not in spec.regimes:
            continue
        value = feature_vector[name]
        if value is None:
            if spec.missing_mode == "neutral":
                families.setdefault(spec.family, []).append(
                    (spec.missing_neutral_q, spec.weight)
                )
            continue
        raw_q = quantize(value, spec.quantum)
        if spec.hard_bound_min is not None and raw_q < spec.hard_bound_min:
            raw_q = spec.hard_bound_min
        if spec.hard_bound_max is not None and raw_q > spec.hard_bound_max:
            # clamp to the WORST endpoint for the feature's direction
            raw_q = (
                spec.transform_points[-1][0]
                if spec.direction == "lower_better"
                else spec.transform_points[0][0]
            )
        q = interpolate_piecewise(list(spec.transform_points), raw_q)
        families.setdefault(spec.family, []).append((q, spec.weight))
    family_q = {fam: weighted_mean_q(pairs) for fam, pairs in sorted(families.items())}
    fam_weights = contract.family_weights[decision][regime.value]
    null_family_q = contract.null_family_q.get(decision, {})
    pairs = []
    for fam, w in sorted(fam_weights.items()):
        if fam in family_q:
            pairs.append((family_q[fam], w))
        elif fam in null_family_q:
            pairs.append((null_family_q[fam], w))
        else:
            pairs.append((0, w))
    return weighted_mean_q(pairs)


@dataclass(frozen=True)
class MarginCandidate:
    key: str
    qscore: int
    censor_class: CensorClass
    is_null: bool = False


def dual_margin_decide(
    subject_id: str,
    candidates: list[MarginCandidate],
    *,
    absolute_q: int,
    survival_margin_bar_q: int,
    evidence_margin_bar_q: int,
) -> DualMarginDecision:
    """Threshold + DUAL margin decision (spec section 12).

    - survival margin: against surviving hard-valid competitors + null;
    - evidence margin: additionally against plausible_but_censored and
      contract_unresolved alternatives (proof_invalid, coverage_missing
      and policy_blocked never compete);
    - a contract_unresolved competitor within the evidence bar blocks
      acceptance outright.
    """
    hard = [c for c in candidates if c.censor_class == CensorClass.HARD_VALID]
    nulls = [c for c in candidates if c.is_null]
    null_q = max((c.qscore for c in nulls), default=None)
    censored = [
        c
        for c in candidates
        if c.censor_class
        in (CensorClass.PLAUSIBLE_BUT_CENSORED, CensorClass.CONTRACT_UNRESOLVED)
    ]
    real_hard = [c for c in hard if not c.is_null]
    if not real_hard:
        contract = [
            c for c in censored if c.censor_class == CensorClass.CONTRACT_UNRESOLVED
        ]
        if contract:
            reason = Reason02R.CENSOR_CONTRACT
        elif censored:
            reason = Reason02R.CENSOR_PLAUSIBLE
        else:
            reason = Reason02R.CENSOR_COVERAGE
        return DualMarginDecision(
            subject_id=subject_id,
            status="no_candidate",
            best_key=None,
            best_q=None,
            survival_margin_q=None,
            evidence_margin_q=None,
            censored_alternatives_considered=len(censored),
            reason_codes=(reason,),
        )
    best = max(real_hard, key=lambda c: c.qscore)
    top = [c for c in real_hard if c.qscore == best.qscore]
    if len(top) > 1:
        return DualMarginDecision(
            subject_id=subject_id,
            status="evidence_tie",
            best_key=None,
            best_q=best.qscore,
            survival_margin_q=0,
            evidence_margin_q=0,
            censored_alternatives_considered=len(censored),
            reason_codes=("XR1.SCORE.EVIDENCE_TIE",),
        )
    surv_rivals = [c.qscore for c in real_hard if c.key != best.key]
    if null_q is not None:
        surv_rivals.append(null_q)
    survival_margin = best.qscore - max(surv_rivals) if surv_rivals else best.qscore
    ev_rivals = surv_rivals + [c.qscore for c in censored]
    evidence_margin = best.qscore - max(ev_rivals) if ev_rivals else best.qscore
    blockers = tuple(
        c.key
        for c in censored
        if c.censor_class == CensorClass.CONTRACT_UNRESOLVED
        and c.qscore >= best.qscore - evidence_margin_bar_q
    )
    reasons: list[str] = []
    if null_q is not None and null_q >= best.qscore:
        status = "null_winner"
    elif best.qscore < absolute_q:
        status = "low_score"
    elif survival_margin < survival_margin_bar_q:
        status = "low_margin"
    elif blockers:
        status = "contract_blocked"
        reasons.append(Reason02R.CONTRACT_COMPETITOR)
    elif evidence_margin < evidence_margin_bar_q:
        status = "unique_survivor_by_exclusion"
        reasons.append(Reason02R.UNIQUE_SURVIVOR)
        reasons.append(Reason02R.EVIDENCE_MARGIN_FAIL)
    else:
        status = "accepted"
    return DualMarginDecision(
        subject_id=subject_id,
        status=status,
        best_key=best.key if status == "accepted" else best.key,
        best_q=best.qscore,
        survival_margin_q=survival_margin,
        evidence_margin_q=evidence_margin,
        blocking_contract_competitors=blockers,
        censored_alternatives_considered=len(censored),
        reason_codes=tuple(reasons),
    )
