"""Slice diagnostics: rates, sensitivity deltas, edge-case inventory.

Inventory rows carry subject IDs and non-sensitive summaries only — no
raw passenger details, no precise traces.
"""

from __future__ import annotations

import hashlib
import json

from .contracts import DispositionStatus, Reason
from .resolution import ResolutionResult
from .spike_pipeline import PreparedSlice


def fingerprint_checksum(result: ResolutionResult) -> str:
    payload = json.dumps(result.logical_fingerprint(), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def disposition_rates(result: ResolutionResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status in result.dispositions.values():
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def segment_status_rates(result: ResolutionResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in result.segment_decisions.values():
        counts[d.status] = counts.get(d.status, 0) + 1
    return dict(sorted(counts.items()))


def reason_rates(result: ResolutionResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reasons in result.disposition_reasons.values():
        for r in reasons:
            counts[r] = counts.get(r, 0) + 1
    return dict(sorted(counts.items()))


def decision_changes(a: ResolutionResult, b: ResolutionResult) -> dict[str, list[str]]:
    """Subjects whose logical decision differs between two runs."""
    seg_changed = [
        sid
        for sid in sorted(set(a.segment_decisions) | set(b.segment_decisions))
        if (
            a.segment_decisions.get(sid)
            and b.segment_decisions.get(sid)
            and (
                a.segment_decisions[sid].status != b.segment_decisions[sid].status
                or a.segment_decisions[sid].selected_key
                != b.segment_decisions[sid].selected_key
            )
        )
        or (sid in a.segment_decisions) != (sid in b.segment_decisions)
    ]
    apc_changed = [
        rid
        for rid in sorted(set(a.dispositions) | set(b.dispositions))
        if a.dispositions.get(rid) != b.dispositions.get(rid)
        or (
            a.apc_decisions.get(rid)
            and b.apc_decisions.get(rid)
            and a.apc_decisions[rid].selected_key != b.apc_decisions[rid].selected_key
        )
    ]
    return {"segments": seg_changed, "apc_records": apc_changed}


def edge_case_inventory(
    prepared: PreparedSlice, result: ResolutionResult, *, slice_name: str
) -> list[dict]:
    rows: list[dict] = []

    def add(category: str, subject_id: str, summary: str) -> None:
        rows.append(
            {
                "slice": slice_name,
                "category": category,
                "subject_id": subject_id,
                "summary": summary,
            }
        )

    for row in result.scored_apc_rows:
        residual = row.get("f_att_stop_to_path_residual_dm")
        if residual is not None and residual > 250.0:
            add(
                "high_spatial_residual",
                row["apc_source_record_id"],
                f"stop-to-path residual {residual:.0f} m vs candidate "
                f"{row['candidate_segment_id'][:16]}",
            )
        gap = row.get("f_att_interval_gap_s")
        if gap is not None and gap > 3600:
            add(
                "stale_time",
                row["apc_source_record_id"],
                f"interval gap {gap} s beyond hard bound",
            )

    for rid, decision in sorted(result.apc_decisions.items()):
        if decision.status == DispositionStatus.NO_CANDIDATE.value:
            add("no_segment_candidate", rid, "no non-null candidate")
        if decision.status == DispositionStatus.EVIDENCE_TIE.value:
            add("fold_or_evidence_tie", rid, "exact qscore tie")
        if decision.status == DispositionStatus.CAP_OVERFLOW.value:
            add("candidate_explosion", rid, "cap overflow fail-closed")
        if decision.status == DispositionStatus.SEQUENCE_DEMOTED.value:
            add("sequence_inconsistency", rid, "Stage 3 demotion")
        if Reason.STOP_AMBIGUOUS in decision.reason_codes:
            add("stop_ambiguity", rid, "multiple stop_code mappings")
        if Reason.ROUTE_AMBIGUOUS in decision.reason_codes:
            add("route_ambiguity", rid, "multiple route mappings")

    for seg in prepared.segments_x4a:
        if seg.crosses_midnight:
            add(
                "cross_midnight",
                seg.segment_id,
                f"local dates {'/'.join(seg.local_dates)}",
            )
        if seg.time_uncertain:
            add(
                "unresolved_time",
                seg.segment_id,
                f"{seg.n_unresolved} unresolved-fold pings",
            )
        purity = seg.route_purity_millis
        if purity is not None and purity < 900:
            add(
                "route_label_mixture",
                seg.segment_id,
                f"route purity {purity}/1000",
            )

    for sid, decision in sorted(result.segment_decisions.items()):
        if decision.n_hard_valid > 8:
            add(
                "multiple_segment_candidates",
                sid,
                f"{decision.n_hard_valid} hard-valid tuples",
            )
    return rows


def metrics_payload(
    prepared: PreparedSlice,
    primary: ResolutionResult,
    *,
    slice_name: str,
    variant_deltas: dict[str, dict],
    policy_deltas: dict[str, dict],
) -> dict:
    apc_time_unresolved = sum(
        1 for r in prepared.apc_records if len(r.arrival_candidates) > 1
    )
    avl_unresolved = sum(
        1 for p in prepared.pings_by_id.values() if p.fold_status == "unresolved"
    )
    return {
        "slice": slice_name,
        "inputs": {
            "avl_rows": prepared.avl_rows,
            "apc_rows": len(prepared.apc_records),
            "vehicles": prepared.vehicles,
            "vehicle_query": prepared.vehicle_query,
        },
        "time": {
            "avl_unresolved_fold_pings": avl_unresolved,
            "apc_ambiguous_arrival_records": apc_time_unresolved,
            "fold_rollbacks": sorted(primary.fold_rollbacks),
            "fold_selected_by_attachment": len(primary.fold_selected_by_attachment),
        },
        "candidates": primary.candidate_counts,
        "segment_status_rates": segment_status_rates(primary),
        "disposition_rates": disposition_rates(primary),
        "reason_rates": reason_rates(primary),
        "sensitivity": {
            "score_variant_deltas": variant_deltas,
            "stage1_policy_deltas": policy_deltas,
        },
        "stage_timings_s": {
            k: round(v, 3) for k, v in prepared.stage_timings_s.items()
        },
    }
