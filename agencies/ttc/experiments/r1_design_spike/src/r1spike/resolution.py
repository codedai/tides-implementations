"""Experimental staged resolution (B2): Stage 1 -> barrier -> Stage 2 ->
barrier -> Stage 3, with caps failing closed and one disposition per APC
record.

This is NOT the production S7. It exists to measure: null-winner rates,
abstention rates, tie rates, cap pressure, Stage-1 policy sensitivity,
window-schedule invariance, and Stage-3 demotion/rollback frequency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .candidate_generation import ApcRecord
from .contracts import (
    NO_SCHEDULED_TRIP,
    UNRESOLVED_SERVICE_DATE,
    ApcCandidate,
    Decision,
    DispositionStatus,
    EvidenceRegime,
    HardValidity,
    Reason,
    ScoredCandidate,
    SegmentCandidate,
    SegmentDecisionStatus,
)
from .inputs import GtfsForDate
from .scoring import ScoreContract, decide
from .segmentation_baselines import Segment, SegPing
from .sequence_validation import (
    ObservedVisit,
    ScheduledOccurrence,
    SequenceValidationResult,
    validate_sequence,
)
from .stage1_policies import ApcHypothesisRef, compute_support
from .windows import Window, apc_owner, segment_owner

SEGMENT_STATUS_MAP = {
    "accepted": SegmentDecisionStatus.ACCEPTED.value,
    "null_winner": SegmentDecisionStatus.NULL_WINNER.value,
    "low_score": SegmentDecisionStatus.LOW_SCORE.value,
    "low_margin": SegmentDecisionStatus.LOW_MARGIN.value,
    "evidence_tie": SegmentDecisionStatus.EVIDENCE_TIE.value,
    "no_candidate": SegmentDecisionStatus.NO_CANDIDATE.value,
    "all_hard_invalid": SegmentDecisionStatus.NO_CANDIDATE.value,
}

APC_STATUS_MAP = {
    "accepted": DispositionStatus.ACCEPTED.value,
    "null_winner": DispositionStatus.NO_ATTACHMENT.value,
    "low_score": DispositionStatus.LOW_SCORE.value,
    "low_margin": DispositionStatus.LOW_MARGIN.value,
    "evidence_tie": DispositionStatus.EVIDENCE_TIE.value,
    "no_candidate": DispositionStatus.NO_CANDIDATE.value,
    "all_hard_invalid": DispositionStatus.NO_CANDIDATE.value,
    f"hard_invalid:{Reason.TEMPORAL_STALE}": DispositionStatus.STALE.value,
    f"hard_invalid:{Reason.GEOM_HARD_CONTRADICTION}": (
        DispositionStatus.GEOMETRY_CONTRADICTION.value
    ),
    f"hard_invalid:{Reason.SEG_TIME_UNCERTAIN}": (
        DispositionStatus.TIME_UNRESOLVED.value
    ),
    f"hard_invalid:{Reason.ATTACH_SEGMENT_UNRESOLVED}": (
        DispositionStatus.POLICY_BLOCKED.value
    ),
    f"hard_invalid:{Reason.TEMPORAL_INCOMPATIBLE_DATE}": (
        DispositionStatus.POLICY_BLOCKED.value
    ),
    f"hard_invalid:{Reason.STOP_UNMAPPED}": DispositionStatus.NO_CANDIDATE.value,
}


@dataclass
class ResolutionConfig:
    contract: ScoreContract
    stage1_policy: str
    policy_b_min_support_records: int
    segment_hypothesis_cap: int
    apc_candidate_cap: int
    component_record_cap: int


@dataclass
class ResolutionResult:
    segment_decisions: dict[str, Decision] = field(default_factory=dict)
    segment_regimes: dict[str, str] = field(default_factory=dict)
    apc_decisions: dict[str, Decision] = field(default_factory=dict)
    dispositions: dict[str, str] = field(default_factory=dict)
    disposition_reasons: dict[str, tuple[str, ...]] = field(default_factory=dict)
    sequence_results: dict[str, SequenceValidationResult] = field(default_factory=dict)
    fold_rollbacks: set[str] = field(default_factory=set)
    fold_selected_by_attachment: dict[str, int] = field(default_factory=dict)
    scored_segment_rows: list[dict] = field(default_factory=list)
    scored_apc_rows: list[dict] = field(default_factory=list)
    component_of: dict[str, int] = field(default_factory=dict)
    overflow_components: set[int] = field(default_factory=set)
    candidate_counts: dict[str, int] = field(default_factory=dict)

    def logical_fingerprint(self) -> dict:
        """Order-normalized logical output for schedule comparisons."""
        return {
            "segments": {
                sid: (
                    d.status,
                    d.selected_key,
                    d.best_q,
                    d.margin_q,
                )
                for sid, d in sorted(self.segment_decisions.items())
            },
            "apc": {
                rid: (
                    d.status,
                    d.selected_key,
                    d.best_q,
                    d.margin_q,
                )
                for rid, d in sorted(self.apc_decisions.items())
            },
            "dispositions": dict(sorted(self.dispositions.items())),
            "fold_rollbacks": sorted(self.fold_rollbacks),
        }


def _components(
    apc_candidates: dict[str, list[ApcCandidate]],
    segments: list[Segment],
) -> tuple[dict[str, int], dict[int, int]]:
    """Union-find over the record<->segment bipartite candidate graph.

    Returns (entity -> component id, component id -> record count).
    Component ids are renumbered deterministically by smallest member.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra > rb:
                ra, rb = rb, ra
            parent[rb] = ra

    for seg in segments:
        find(seg.segment_id)
    for rid, cands in sorted(apc_candidates.items()):
        find(f"rec:{rid}")
        for c in cands:
            if not c.is_null_alternative():
                union(f"rec:{rid}", c.candidate_segment_id)

    roots = sorted({find(x) for x in parent})
    root_index = {r: i for i, r in enumerate(roots)}
    entity_component = {x: root_index[find(x)] for x in parent}
    record_counts: dict[int, int] = {}
    for x, comp in entity_component.items():
        if x.startswith("rec:"):
            record_counts[comp] = record_counts.get(comp, 0) + 1
    return entity_component, record_counts


def _stage1_regime_and_extra(
    contract: ScoreContract,
    cfg: ResolutionConfig,
    segment_id: str,
    candidate: SegmentCandidate,
    refs: list[ApcHypothesisRef],
) -> tuple[EvidenceRegime, dict[str, int] | None, int | None]:
    support = compute_support(
        cfg.stage1_policy,
        segment_id,
        candidate.service_date_hypothesis,
        candidate.feed_id,
        refs,
        min_support_records=cfg.policy_b_min_support_records,
    )
    if support is None:
        return EvidenceRegime.AVL_ONLY, None, None
    spec = contract.features["seg_apc_date_support_millis"]
    q, reason, invalid = contract.feature_qscore(spec, support)
    if invalid:
        # A hard bound on the support feature is a contradiction and
        # must fail closed, never silently fall back to AVL-only. The
        # shipped contract declares no such bound, but a variant could.
        raise ValueError(
            "seg_apc_date_support_millis hard-invalidated "
            f"({reason}); a support hard bound must be handled as "
            "candidate hard-invalidity, which this spike does not "
            "implement — remove the bound or extend the resolver"
        )
    if q is None:
        return EvidenceRegime.AVL_ONLY, None, support
    return EvidenceRegime.APC_EVIDENCE, {"seg_apc_support": q}, support


def _score_segment_candidates(
    contract: ScoreContract,
    cfg: ResolutionConfig,
    segment: Segment,
    candidates: list[SegmentCandidate],
    refs: list[ApcHypothesisRef],
) -> tuple[list[ScoredCandidate], dict[tuple, dict]]:
    scored: list[ScoredCandidate] = []
    detail: dict[tuple, dict] = {}
    for cand in candidates:
        key = cand.candidate_key()
        if cand.service_date_hypothesis == UNRESOLVED_SERVICE_DATE:
            q = contract.null_scores["segment_selection"][UNRESOLVED_SERVICE_DATE]
            sc = ScoredCandidate(
                key=key,
                qscore=q,
                is_null=True,
                hard_validity=HardValidity.VALID,
                regime=EvidenceRegime.AVL_ONLY,
                family_qscores={},
                reason_codes=cand.reason_codes,
            )
            scored.append(sc)
            detail[key] = {"qscore": q, "regime": "null", "support": None}
            continue
        if cand.hard_validity == HardValidity.HARD_INVALID:
            scored.append(
                ScoredCandidate(
                    key=key,
                    qscore=0,
                    is_null=False,
                    hard_validity=HardValidity.HARD_INVALID,
                    regime=EvidenceRegime.AVL_ONLY,
                    family_qscores={},
                    reason_codes=cand.reason_codes,
                )
            )
            detail[key] = {"qscore": None, "regime": "hard_invalid"}
            continue
        regime, extra, support = _stage1_regime_and_extra(
            contract, cfg, segment.segment_id, cand, refs
        )
        q, families, reasons, hard_invalid = contract.score_candidate(
            "segment_selection",
            cand.feature_vector,
            regime,
            extra_family_q=extra,
        )
        scored.append(
            ScoredCandidate(
                key=key,
                qscore=q,
                is_null=False,
                hard_validity=(
                    HardValidity.HARD_INVALID if hard_invalid else HardValidity.VALID
                ),
                regime=regime,
                family_qscores=families,
                reason_codes=tuple(sorted({*cand.reason_codes, *reasons})),
            )
        )
        detail[key] = {
            "qscore": q,
            "regime": regime.value,
            "support": support,
            "families": families,
        }
    return scored, detail


def resolve(
    *,
    segments: list[Segment],
    pings_by_id: dict[str, SegPing],
    apc_records: list[ApcRecord],
    segment_candidates: dict[str, list[SegmentCandidate]],
    apc_candidates: dict[str, list[ApcCandidate]],
    gtfs_by_date: dict[str, GtfsForDate],
    cfg: ResolutionConfig,
    windows: list[Window],
    schedule: list[int],
) -> ResolutionResult:
    """Run the staged resolution under one window execution schedule.

    ``schedule`` is a permutation of window indices; identical logical
    outputs across schedules are asserted by the caller.
    """
    result = ResolutionResult()
    contract = cfg.contract
    segments_by_id = {s.segment_id: s for s in segments}
    records_by_id = {r.record_id: r for r in apc_records}

    entity_component, record_counts = _components(apc_candidates, segments)
    result.component_of = entity_component
    result.overflow_components = {
        comp for comp, n in record_counts.items() if n > cfg.component_record_cap
    }

    # Deduplicated: fold/route/stop candidate multiplicity must not
    # weight a record's Stage-1 mass toward feeds with more stop or
    # route hypotheses — one distinct (record, segment, date, feed)
    # hypothesis counts once.
    refs = sorted(
        {
            ApcHypothesisRef(
                apc_source_record_id=c.apc_source_record_id,
                candidate_segment_id=c.candidate_segment_id,
                service_date_hypothesis=c.service_date_hypothesis,
                feed_id=c.feed_id,
            )
            for rid, cands in sorted(apc_candidates.items())
            for c in cands
            if not c.is_null_alternative()
        },
        key=lambda r: (
            r.apc_source_record_id,
            r.candidate_segment_id,
            r.service_date_hypothesis,
            r.feed_id or "",
        ),
    )

    # --- Stage 1: segment decisions, per owner window in schedule order.
    seg_owner = {s.segment_id: segment_owner(s, windows) for s in segments}
    for widx in schedule:
        for seg in sorted(
            (s for s in segments if seg_owner[s.segment_id] == widx),
            key=lambda s: s.segment_id,
        ):
            sid = seg.segment_id
            cands = segment_candidates.get(sid, [])
            scored, detail = _score_segment_candidates(contract, cfg, seg, cands, refs)
            for cand in cands:
                key = cand.candidate_key()
                row = {
                    "segment_id": sid,
                    "service_date_hypothesis": cand.service_date_hypothesis,
                    "feed_id": cand.feed_id,
                    "route_mapping_hypothesis": cand.route_mapping_hypothesis,
                    "scheduled_trip_hypothesis": cand.scheduled_trip_hypothesis,
                    "hard_validity": cand.hard_validity.value,
                    "reason_codes": "|".join(cand.reason_codes),
                    "qscore": detail.get(key, {}).get("qscore"),
                    "regime": detail.get(key, {}).get("regime"),
                    "stage1_support_millis": detail.get(key, {}).get("support"),
                }
                for feat, value in sorted(cand.feature_vector.items()):
                    row[f"f_{feat}"] = value
                result.scored_segment_rows.append(row)

            comp = entity_component.get(sid)
            if comp in result.overflow_components:
                result.segment_decisions[sid] = Decision(
                    subject_id=sid,
                    status=SegmentDecisionStatus.CAP_OVERFLOW.value,
                    selected_key=None,
                    best_q=None,
                    runner_up_q=None,
                    null_q=None,
                    margin_q=None,
                    n_candidates=len(cands),
                    n_hard_valid=0,
                    reason_codes=(Reason.CAP_COMPONENT_OVERFLOW,),
                )
                continue
            n_hard_valid = sum(
                1
                for c in scored
                if not c.is_null and c.hard_validity == HardValidity.VALID
            )
            if n_hard_valid > cfg.segment_hypothesis_cap:
                result.segment_decisions[sid] = Decision(
                    subject_id=sid,
                    status=SegmentDecisionStatus.CAP_OVERFLOW.value,
                    selected_key=None,
                    best_q=None,
                    runner_up_q=None,
                    null_q=None,
                    margin_q=None,
                    n_candidates=len(cands),
                    n_hard_valid=n_hard_valid,
                    reason_codes=(Reason.CAP_SEGMENT_OVERFLOW,),
                )
                continue
            if seg.time_uncertain:
                result.segment_decisions[sid] = Decision(
                    subject_id=sid,
                    status=SegmentDecisionStatus.TIME_UNRESOLVED.value,
                    selected_key=None,
                    best_q=None,
                    runner_up_q=None,
                    null_q=None,
                    margin_q=None,
                    n_candidates=len(cands),
                    n_hard_valid=n_hard_valid,
                    reason_codes=(Reason.SEG_TIME_UNCERTAIN,),
                )
                continue
            decision = decide(
                sid,
                scored,
                contract.thresholds["segment_selection"],
                status_map=SEGMENT_STATUS_MAP,
            )
            result.segment_decisions[sid] = decision
            if decision.regime is not None:
                result.segment_regimes[sid] = decision.regime.value

    # --- barrier --- Stage 2: APC decisions with immutable Stage 1 context.
    rec_owner = {
        r.record_id: apc_owner(
            r, apc_candidates.get(r.record_id, []), segments_by_id, windows
        )
        for r in apc_records
    }
    for widx in schedule:
        for record in sorted(
            (r for r in apc_records if rec_owner[r.record_id] == widx),
            key=lambda r: r.record_id,
        ):
            rid = record.record_id
            decision = _stage2_decide(
                result, record, apc_candidates.get(rid, []), cfg, contract
            )
            result.apc_decisions[rid] = decision
            result.dispositions[rid] = decision.status
            result.disposition_reasons[rid] = decision.reason_codes
            if (
                decision.status == DispositionStatus.ACCEPTED.value
                and len(record.arrival_candidates) > 1
                and decision.selected_key is not None
            ):
                fold = decision.selected_key[1]
                result.fold_selected_by_attachment[rid] = fold

    # --- barrier --- Stage 3: global sequence validation per segment.
    _stage3(result, segments_by_id, records_by_id, gtfs_by_date, cfg)

    result.candidate_counts = {
        "segment_candidates": sum(len(v) for v in segment_candidates.values()),
        "apc_candidates": sum(len(v) for v in apc_candidates.values()),
        "components": len(record_counts),
        "overflow_components": len(result.overflow_components),
    }
    return result


def _stage2_decide(
    result: ResolutionResult,
    record: ApcRecord,
    cands: list[ApcCandidate],
    cfg: ResolutionConfig,
    contract: ScoreContract,
) -> Decision:
    rid = record.record_id
    comp = result.component_of.get(f"rec:{rid}")
    if comp in result.overflow_components:
        return Decision(
            subject_id=rid,
            status=DispositionStatus.CAP_OVERFLOW.value,
            selected_key=None,
            best_q=None,
            runner_up_q=None,
            null_q=None,
            margin_q=None,
            n_candidates=len(cands),
            n_hard_valid=0,
            reason_codes=(Reason.CAP_COMPONENT_OVERFLOW,),
        )
    scored: list[ScoredCandidate] = []
    for cand in cands:
        key = cand.candidate_key()
        if cand.is_null_alternative():
            q = contract.null_scores["apc_attachment"]["XNULL:NO_ATTACHMENT"]
            scored.append(
                ScoredCandidate(
                    key=key,
                    qscore=q,
                    is_null=True,
                    hard_validity=HardValidity.VALID,
                    regime=EvidenceRegime.APC_EVIDENCE,
                    family_qscores={},
                    reason_codes=cand.reason_codes,
                )
            )
            _apc_row(result, cand, q, "null", None)
            continue
        seg_decision = result.segment_decisions.get(cand.candidate_segment_id)
        extra_reasons: list[str] = list(cand.reason_codes)
        hard = cand.hard_validity == HardValidity.HARD_INVALID
        if seg_decision is None:
            hard = True
            extra_reasons.append(Reason.NO_SEGMENT_CANDIDATE)
        elif seg_decision.status == SegmentDecisionStatus.TIME_UNRESOLVED.value:
            hard = True
            extra_reasons.append(Reason.SEG_TIME_UNCERTAIN)
        elif seg_decision.status != SegmentDecisionStatus.ACCEPTED.value:
            hard = True
            extra_reasons.append(Reason.ATTACH_SEGMENT_UNRESOLVED)
        else:
            _sid, sel_date, sel_feed, _sel_route, _sel_trip = seg_decision.selected_key
            if cand.service_date_hypothesis != sel_date or cand.feed_id != sel_feed:
                hard = True
                extra_reasons.append(Reason.TEMPORAL_INCOMPATIBLE_DATE)
        if hard:
            scored.append(
                ScoredCandidate(
                    key=key,
                    qscore=0,
                    is_null=False,
                    hard_validity=HardValidity.HARD_INVALID,
                    regime=EvidenceRegime.APC_EVIDENCE,
                    family_qscores={},
                    reason_codes=tuple(sorted(set(extra_reasons))),
                )
            )
            _apc_row(result, cand, None, "hard_invalid", extra_reasons)
            continue
        q, families, reasons, hard_invalid = contract.score_candidate(
            "apc_attachment",
            cand.feature_vector,
            EvidenceRegime.APC_EVIDENCE,
        )
        all_reasons = tuple(sorted({*extra_reasons, *reasons}))
        scored.append(
            ScoredCandidate(
                key=key,
                qscore=q,
                is_null=False,
                hard_validity=(
                    HardValidity.HARD_INVALID if hard_invalid else HardValidity.VALID
                ),
                regime=EvidenceRegime.APC_EVIDENCE,
                family_qscores=families,
                reason_codes=all_reasons,
            )
        )
        _apc_row(
            result,
            cand,
            None if hard_invalid else q,
            "hard_invalid" if hard_invalid else "scored",
            all_reasons,
        )
    n_hard_valid = sum(
        1 for c in scored if not c.is_null and c.hard_validity == HardValidity.VALID
    )
    if n_hard_valid > cfg.apc_candidate_cap:
        return Decision(
            subject_id=rid,
            status=DispositionStatus.CAP_OVERFLOW.value,
            selected_key=None,
            best_q=None,
            runner_up_q=None,
            null_q=None,
            margin_q=None,
            n_candidates=len(cands),
            n_hard_valid=n_hard_valid,
            reason_codes=(Reason.CAP_APC_OVERFLOW,),
        )
    return decide(
        rid,
        scored,
        contract.thresholds["apc_attachment"],
        status_map=APC_STATUS_MAP,
    )


def _apc_row(
    result: ResolutionResult,
    cand: ApcCandidate,
    qscore: int | None,
    kind: str,
    reasons,
) -> None:
    row = {
        "apc_source_record_id": cand.apc_source_record_id,
        "time_fold_hypothesis": cand.time_fold_hypothesis,
        "service_date_hypothesis": cand.service_date_hypothesis,
        "feed_id": cand.feed_id,
        "vehicle_mapping_hypothesis": cand.vehicle_mapping_hypothesis,
        "route_mapping_hypothesis": cand.route_mapping_hypothesis,
        "stop_mapping_hypothesis": cand.stop_mapping_hypothesis,
        "candidate_segment_id": cand.candidate_segment_id,
        "hard_validity": cand.hard_validity.value,
        "qscore": qscore,
        "score_kind": kind,
        "reason_codes": "|".join(sorted(set(reasons))) if reasons else "",
    }
    for feat, value in sorted(cand.feature_vector.items()):
        row[f"f_{feat}"] = value
    result.scored_apc_rows.append(row)


def _stage3(
    result: ResolutionResult,
    segments_by_id: dict[str, Segment],
    records_by_id: dict[str, ApcRecord],
    gtfs_by_date: dict[str, GtfsForDate],
    cfg: ResolutionConfig,
) -> None:
    attached: dict[str, list[tuple[str, Decision]]] = {}
    for rid, decision in sorted(result.apc_decisions.items()):
        if decision.status != DispositionStatus.ACCEPTED.value:
            continue
        seg_id = decision.selected_key[7 - 1]  # candidate_segment_id slot
        attached.setdefault(seg_id, []).append((rid, decision))

    for seg_id in sorted(attached):
        seg_decision = result.segment_decisions.get(seg_id)
        if seg_decision is None or seg_decision.selected_key is None:
            continue
        _sid, sel_date, _feed, _route, sel_trip = seg_decision.selected_key
        if sel_trip == NO_SCHEDULED_TRIP:
            continue  # nothing to validate against (documented scope)
        gtfs = gtfs_by_date.get(sel_date)
        if gtfs is None:
            continue
        events = gtfs.scheduled_stop_events.filter(
            gtfs.scheduled_stop_events["trip_id"] == sel_trip
        ).sort("stop_sequence")
        scheduled = [
            ScheduledOccurrence(
                occ_index=i,
                stop_id=row["stop_id"],
                scheduled_time_s=row["arrival_epoch_s"],
            )
            for i, row in enumerate(events.iter_rows(named=True))
        ]
        observed = []
        for rid, decision in sorted(
            attached[seg_id],
            key=lambda item: (item[1].selected_key, item[0]),
        ):
            key = decision.selected_key
            stop_id = key[5]
            fold_by_attach = rid in result.fold_selected_by_attachment
            record = records_by_id[rid]
            fold = key[1]
            arr = [e for f, e in record.arrival_candidates if f == fold]
            if not arr:
                continue
            observed.append(
                ObservedVisit(
                    visit_id=rid,
                    stop_id=stop_id,
                    observed_time_s=arr[0],
                    along_m=None,
                    fold_selected_by_attachment=fold_by_attach,
                )
            )
        observed.sort(key=lambda v: (v.observed_time_s, v.visit_id))
        if not observed:
            continue
        seq = validate_sequence(scheduled, observed)
        result.sequence_results[seg_id] = seq
        for rid in sorted(seq.demoted):
            reasons = set(result.disposition_reasons.get(rid, ()))
            reasons.add(Reason.SEQ_INCONSISTENT)
            if rid in seq.ambiguous_demotions:
                reasons.add(Reason.SEQ_AMBIGUOUS_DEMOTION)
            result.dispositions[rid] = DispositionStatus.SEQUENCE_DEMOTED.value
            if rid in seq.fold_rollback_visit_ids:
                result.fold_rollbacks.add(rid)
                result.fold_selected_by_attachment.pop(rid, None)
                reasons.add(Reason.TIME_FOLD_ROLLED_BACK)
            result.disposition_reasons[rid] = tuple(sorted(reasons))
        if seq.demoted:
            # One-way segment weakening: recompute nothing upstream;
            # only annotate the segment decision when demotions removed
            # every attachment (a conservative, visible signal).
            kept = [rid for rid, _ in attached[seg_id] if rid not in seq.demoted]
            if not kept:
                old = result.segment_decisions[seg_id]
                result.segment_decisions[seg_id] = Decision(
                    subject_id=old.subject_id,
                    status=SegmentDecisionStatus.SEQUENCE_DEMOTED.value,
                    selected_key=old.selected_key,
                    best_q=old.best_q,
                    runner_up_q=old.runner_up_q,
                    null_q=old.null_q,
                    margin_q=old.margin_q,
                    n_candidates=old.n_candidates,
                    n_hard_valid=old.n_hard_valid,
                    reason_codes=tuple(
                        sorted({*old.reason_codes, Reason.SEQ_INCONSISTENT})
                    ),
                    regime=old.regime,
                )
