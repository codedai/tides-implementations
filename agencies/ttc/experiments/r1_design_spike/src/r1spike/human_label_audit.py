# ruff: noqa: E501
"""Non-destructive end-to-end audit of first-pass human labels."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from .human_label_artifacts import (
    AuditArtifactError,
    AuditArtifactWriter,
    git_revision,
    logical_sha256,
    source_tree_hash,
)
from .human_label_parser import (
    FirstPassLabel,
    HumanLabelParseError,
    PacketCase,
    ParsedSummary,
    load_first_pass_labels,
    load_summaries,
    neutral_primary_label,
    portable_path,
    read_packet,
    read_review_history,
    sha256_file,
)
from .label_insights import (
    build_audit_summary,
    build_readiness_report,
    proposed_label_schema_v2,
    render_findings,
    render_possible_next_steps,
)
from .label_readiness import (
    consistency_reason_codes,
    internal_consistency_status,
    priority_score,
    rationale_present,
    readiness,
    recommended_second_pass_questions,
    resolve_candidate_key,
    visible_evidence_metrics,
)
from .label_task_routing import explicit_reviewer_heuristics, suggest_routing
from .label_visualizations import generate_figures

AUDIT_CASE_COLUMNS = [
    "case_id",
    "original_label",
    "selected_card",
    "confidence",
    "notes_present",
    "rationale_present",
    "candidate_count",
    "displayed_evaluable_candidate_count",
    "candidate_table_key_count_total",
    "complete_candidate_count",
    "no_scheduled_alternative_count",
    "chunk_duration_s",
    "selected_overlap_s",
    "selected_coverage_pct",
    "selected_overlap_advantage_s",
    "selected_coverage_advantage_pct_points",
    "selected_start_residual_advantage_s",
    "selected_end_residual_advantage_s",
    "best_visible_overlap_advantage_s",
    "best_visible_coverage_advantage_pct_points",
    "selected_exact_tie_count",
    "selected_near_tie_count",
    "visible_near_tie_pair_count",
    "all_evaluable_candidates_tied",
    "case_grain_suggestion",
    "reviewability_suggestion",
    "task_routing_suggestion",
    "fragment_role_suggestion",
    "suggestion_strength",
    "suggestion_evidence_basis",
    "label_readiness_classes",
    "audit_reason_codes",
    "internal_consistency_status",
    "candidate_key_status",
    "candidate_card_key_mapping_status",
    "candidate_key_explicit_null_polarity",
    "adjacent_context_status",
    "boundary_stop",
    "boundary_time_relation",
    "boundary_offset_from_chunk_end_s",
    "note_time_references_rel_s",
    "review_priority_score",
    "recommended_second_pass_questions",
    "explicit_reviewer_heuristics",
    "diagnostics_revealed_before_save",
    "diagnostics_reveal_source",
    "diagnostics_reveal_consistency_status",
    "blind_second_pass_completed",
    "calibration_ready",
]


@dataclass(frozen=True)
class AuditConfig:
    cases_path: Path
    summaries_path: Path
    artifacts_path: Path
    output_path: Path
    current_labels_path: Path | None = None
    review_history_path: Path | None = None


@dataclass(frozen=True)
class AuditResult:
    output_path: Path
    case_count: int
    input_hashes: Mapping[str, str]
    audit_rows: tuple[Mapping[str, Any], ...]
    original_input_hashes_unchanged: bool


def run_audit(config: AuditConfig) -> AuditResult:
    """Run the audit once into a new directory and return its case rows."""

    cases_path = config.cases_path.resolve()
    summaries_path = config.summaries_path.resolve()
    artifacts_path = config.artifacts_path.resolve()
    ttc_root = _find_ttc_root(cases_path)
    experiment_root = ttc_root / "experiments" / "r1_design_spike"
    current_labels_path = (
        config.current_labels_path.resolve()
        if config.current_labels_path
        else ttc_root / "tools" / "labeling_ui" / "labels" / "scheduled_trip_labels.csv"
    )
    review_history_path = (
        config.review_history_path.resolve()
        if config.review_history_path
        else ttc_root
        / "tools"
        / "labeling_ui"
        / "labels"
        / "scheduled_trip_labels.jsonl"
    )

    packets = read_packet(cases_path)
    summaries = load_summaries(summaries_path)
    labels = load_first_pass_labels(current_labels_path, review_history_path)
    review_events = read_review_history(review_history_path)
    labels_by_case = _one_label_per_case(labels)
    _validate_case_union(packets, summaries, labels_by_case)

    evidence = _load_evidence(artifacts_path)
    source_files = _discover_source_files(
        ttc_root,
        cases_path,
        summaries,
        artifacts_path,
        current_labels_path,
        review_history_path,
    )
    input_hashes = {
        portable_path(path, [ttc_root]): sha256_file(path)
        for path in sorted(source_files)
    }
    original_hashes_by_path = {path: sha256_file(path) for path in source_files}
    tree_hash, tree_files = source_tree_hash(experiment_root)
    generated_at = _deterministic_generated_at(labels)
    revision = git_revision(ttc_root)

    first_pass_rows: list[dict[str, Any]] = []
    inventory_rows: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    label_export_hash = input_hashes.get(portable_path(current_labels_path, [ttc_root]))
    history_hash = input_hashes.get(portable_path(review_history_path, [ttc_root]))
    for packet in packets:
        label = labels_by_case[packet.case_id]
        summary = summaries[packet.case_id]
        case_evidence = evidence.case(packet.case_id)
        first_pass_rows.append(
            _first_pass_row(
                packet,
                label,
                summary,
                ttc_root,
                label_export_hash,
                history_hash,
            )
        )
        inventory = _inventory_row(packet, summary, case_evidence)
        inventory_rows.append(inventory)

        routing = suggest_routing(packet, label, summary)
        visible = visible_evidence_metrics(summary, label.raw_selected_card)
        key_resolution = resolve_candidate_key(
            label.raw_selected_card,
            label.raw_selected_candidate_id,
            case_evidence["candidate_keys"],
        )
        reasons = set(consistency_reason_codes(label, routing, visible, key_resolution))
        if not _summary_matches_label(summary, label):
            reasons.add("SUMMARY_FIRST_PASS_MISMATCH")
        if label.current_export_matches_first_pass is False:
            reasons.add("CURRENT_EXPORT_DIFFERS_FROM_FIRST_PASS")
        if label.diagnostics_reveal_consistency_status == "conflict":
            reasons.add("DIAGNOSTICS_REVEAL_METADATA_CONFLICT")
        if (
            inventory["displayed_window_naive_duration_status"]
            == "dst_elapsed_mismatch"
        ):
            reasons.add("DISPLAYED_WINDOW_DST_ELAPSED_TIME_MISMATCH")
        reasons_tuple = tuple(sorted(reasons))
        ready = readiness(
            label,
            routing,
            key_resolution,
            reasons_tuple,
            blind_second_pass_completed=False,
        )
        boundary_reference = next(
            (
                reference
                for reference in routing.note_time_references
                if reference.relation_to_window
                in {"before_current_window", "after_current_window"}
            ),
            None,
        )
        audit_rows.append(
            {
                "case_id": packet.case_id,
                "original_label": neutral_primary_label(label.raw_primary_label),
                "raw_primary_label": label.raw_primary_label,
                "original_stratum": packet.original_stratum,
                "selected_card": label.raw_selected_card,
                "confidence": label.raw_confidence,
                "notes_present": bool(label.raw_notes.strip()),
                "rationale_present": rationale_present(label),
                "candidate_count": visible.displayed_candidate_count,
                "displayed_evaluable_candidate_count": (
                    visible.displayed_evaluable_candidate_count
                ),
                "complete_candidate_count": case_evidence["complete_candidate_count"],
                "candidate_table_key_count_total": case_evidence[
                    "candidate_table_key_count_total"
                ],
                "no_scheduled_alternative_count": case_evidence[
                    "no_scheduled_alternative_count"
                ],
                "chunk_duration_s": inventory["chunk_duration_s"],
                "selected_overlap_s": visible.selected_overlap_s,
                "selected_coverage_pct": visible.selected_coverage_pct,
                "selected_overlap_advantage_s": (visible.selected_overlap_advantage_s),
                "selected_coverage_advantage_pct_points": (
                    visible.selected_coverage_advantage_pct_points
                ),
                "selected_start_residual_advantage_s": (
                    visible.selected_start_residual_advantage_s
                ),
                "selected_end_residual_advantage_s": (
                    visible.selected_end_residual_advantage_s
                ),
                "best_visible_overlap_advantage_s": (
                    visible.best_visible_overlap_advantage_s
                ),
                "best_visible_coverage_advantage_pct_points": (
                    visible.best_visible_coverage_advantage_pct_points
                ),
                "selected_exact_tie_count": visible.selected_exact_tie_count,
                "selected_near_tie_count": visible.selected_near_tie_count,
                "visible_near_tie_pair_count": visible.visible_near_tie_pair_count,
                "all_evaluable_candidates_tied": (
                    visible.all_evaluable_candidates_tied
                ),
                "case_grain_suggestion": routing.case_grain_suggestion,
                "reviewability_suggestion": routing.reviewability_suggestion,
                "task_routing_suggestion": routing.task_routing_suggestion,
                "fragment_role_suggestion": routing.fragment_role_suggestion,
                "suggestion_strength": routing.suggestion_strength,
                "suggestion_evidence_basis": routing.evidence_basis,
                "label_readiness_classes": ready.classes,
                "audit_reason_codes": reasons_tuple,
                "internal_consistency_status": internal_consistency_status(
                    reasons_tuple
                ),
                "candidate_key_status": key_resolution.status,
                "candidate_card_key_mapping_status": (
                    "stored_key_membership_verified_card_mapping_not_independently_reconstructed"
                    if label.raw_selected_card
                    else "not_applicable_no_selected_card"
                ),
                "candidate_key_explicit_null_polarity": (
                    key_resolution.explicit_null_polarity
                ),
                "adjacent_context_status": (
                    "referenced_outside_current_window"
                    if "ADJACENT_CONTEXT_USED" in reasons
                    else "not_referenced"
                ),
                "boundary_stop": routing.boundary_stop,
                "boundary_time_relation": (
                    boundary_reference.relation_to_window
                    if boundary_reference
                    else None
                ),
                "boundary_offset_from_chunk_end_s": (
                    boundary_reference.offset_from_window_s
                    if boundary_reference
                    and boundary_reference.relation_to_window == "after_current_window"
                    else None
                ),
                "note_time_references_rel_s": tuple(
                    _relative_time_reference(reference)
                    for reference in routing.note_time_references
                    if reference.offset_from_window_start_s is not None
                ),
                "referenced_cards": routing.referenced_cards,
                "review_priority_score": priority_score(reasons_tuple),
                "recommended_second_pass_questions": (
                    recommended_second_pass_questions(routing, reasons_tuple)
                ),
                "explicit_reviewer_heuristics": explicit_reviewer_heuristics(
                    label.raw_notes
                ),
                "diagnostics_revealed_before_save": (
                    label.diagnostics_revealed_before_save
                ),
                "diagnostics_reveal_source": label.diagnostics_reveal_source,
                "diagnostics_reveal_consistency_status": (
                    label.diagnostics_reveal_consistency_status
                ),
                "blind_second_pass_completed": False,
                "strict_gate_checks": dict(ready.strict_gate_checks),
                "calibration_ready": ready.calibration_ready,
            }
        )

    audit_summary = build_audit_summary(
        audit_rows,
        inventory_rows,
        review_history_summary=_review_history_summary(review_events, labels),
        design_contract_summary=_design_contract_summary(
            artifacts_path, {packet.case_id for packet in packets}, ttc_root
        ),
    )
    readiness_report = build_readiness_report(audit_rows)
    proposed_schema = proposed_label_schema_v2()
    findings = render_findings(
        audit_rows, audit_summary, readiness_report, input_hashes
    )
    next_steps = render_possible_next_steps(
        audit_rows, audit_summary["design_contract_audit"]
    )
    queue_rows = _adjudication_queue(audit_rows)
    child_rows = _child_case_requests(audit_rows)
    figures = generate_figures(audit_rows)
    input_inventory = _input_inventory(source_files, input_hashes, ttc_root)

    forbidden_paths = [
        *source_files,
        ttc_root / "database_offline",
        ttc_root / "src" / "ttc_tides",
        summaries_path,
        current_labels_path.parent,
        artifacts_path,
    ]
    writer = AuditArtifactWriter(
        config.output_path,
        input_hashes=input_hashes,
        source_tree_sha256=tree_hash,
        code_revision=revision,
        generated_at=generated_at,
        forbidden_paths=forbidden_paths,
    )
    try:
        _write_outputs(
            writer,
            first_pass_rows,
            inventory_rows,
            audit_rows,
            audit_summary,
            readiness_report,
            queue_rows,
            child_rows,
            proposed_schema,
            findings,
            next_steps,
            figures,
            input_inventory,
            tree_files,
        )
        original_unchanged = all(
            path.exists() and sha256_file(path) == digest
            for path, digest in original_hashes_by_path.items()
        )
        safety = _adversarial_review(
            writer.staging_dir,
            audit_rows,
            labels,
            summaries,
            original_unchanged,
        )
        logical_payload = {
            "scope": "primary domain artifacts written before checksum and manifest",
            "recursive_exclusions": [
                "logical_checksums.json",
                "run_manifest.json",
            ],
            "artifacts": {
                name: details["logical_sha256"]
                for name, details in sorted(writer.artifacts.items())
            },
        }
        writer.write_json(
            logical_payload,
            "logical_checksums.json",
            grain="one checksum per generated domain artifact",
            key=["artifact"],
            contains_human_labels=False,
            privacy="repository-safe-derived",
        )
        metadata_sidecars = {
            path.relative_to(writer.staging_dir).as_posix(): sha256_file(path)
            for path in sorted(writer.staging_dir.rglob("*.meta.json"))
        }
        manifest_payload = {
            "run_id": "human-label-audit-v1-"
            + logical_sha256(
                {"input_hashes": input_hashes, "source_tree_sha256": tree_hash}
            )[:12],
            "case_count": len(audit_rows),
            "input_file_count": len(input_hashes),
            "source_tree_file_count": len(tree_files),
            "artifacts": dict(sorted(writer.artifacts.items())),
            "artifact_registry_scope": (
                "all primary artifacts except this recursively self-describing run_manifest"
            ),
            "artifact_registry_recursive_exclusions": ["run_manifest.json"],
            "metadata_sidecars": metadata_sidecars,
            "adversarial_review": safety,
            "original_inputs_unchanged": original_unchanged,
            "architecture_verdict": "INCONCLUSIVE — NO REVIEWED HOLDOUT LABELS",
            "packet_is_independent_holdout": False,
            "phase_b_authorized": False,
            "factorial_authorized": False,
        }
        writer.write_json(
            manifest_payload,
            "run_manifest.json",
            grain="one audit run manifest",
            key=["run_id"],
            contains_human_labels=False,
            privacy="repository-safe-derived",
        )
        post_manifest_safety = _adversarial_review(
            writer.staging_dir,
            audit_rows,
            labels,
            summaries,
            original_unchanged,
        )
        if post_manifest_safety != safety:
            raise AuditArtifactError(
                "post-manifest adversarial review disagreed with the recorded review"
            )
        output_path = writer.finalize()
    except Exception:
        writer.abort()
        raise

    return AuditResult(
        output_path=output_path,
        case_count=len(audit_rows),
        input_hashes=input_hashes,
        audit_rows=tuple(audit_rows),
        original_input_hashes_unchanged=original_unchanged,
    )


class _EvidenceStore:
    def __init__(self, artifacts_path: Path):
        self.artifacts_path = artifacts_path
        self.restricted = _read_optional_parquet(
            artifacts_path / "labeling_packet_restricted.parquet"
        )
        self.chunks = _read_optional_parquet(
            artifacts_path / "x_refined_chunks.parquet"
        )
        self.candidates = _read_optional_parquet(
            artifacts_path / "x_complete_schedule_candidates.parquet"
        )
        self.edges = _read_optional_parquet(
            artifacts_path / "x_anchor_evidence_edges.parquet"
        )
        self.fragment_roles = _read_optional_parquet(
            artifacts_path / "x_fragment_roles.parquet"
        )
        self.anchor_diagnostics = _read_optional_parquet(
            artifacts_path / "x_anchor_candidates_v2.parquet"
        )
        self.case_to_chunk = self._case_to_chunk()

    def _case_to_chunk(self) -> dict[str, str]:
        if self.restricted is None:
            return {}
        return {
            str(row["case_id"]): str(row["real_id"])
            for row in self.restricted.iter_rows(named=True)
        }

    def case(self, case_id: str) -> dict[str, Any]:
        chunk_id = self.case_to_chunk.get(case_id)
        chunk_row: dict[str, Any] = {}
        if self.chunks is not None and chunk_id:
            selected = self.chunks.filter(pl.col("chunk_id") == chunk_id)
            if not selected.is_empty():
                chunk_row = selected.row(0, named=True)
        keys: list[str] = []
        complete_scheduled_keys: list[str] = []
        candidate_rows = None
        if self.candidates is not None and chunk_id:
            candidate_rows = self.candidates.filter(pl.col("chunk_id") == chunk_id)
            if not candidate_rows.is_empty():
                keys = sorted(candidate_rows["candidate_key"].unique().to_list())
                complete_scheduled_keys = [
                    key
                    for key in keys
                    if len(key.split("|")) == 5
                    and all(key.split("|")[index] for index in range(4))
                ]
        apc_count = None
        if self.edges is not None and chunk_id:
            selected_edges = self.edges.filter(
                (pl.col("derived_id") == chunk_id) & (pl.col("source_type") == "apc")
            )
            apc_count = selected_edges["source_record_id"].n_unique()
        before, after = self._context(chunk_row)
        role = None
        if self.fragment_roles is not None and chunk_id:
            selected_role = self.fragment_roles.filter(pl.col("chunk_id") == chunk_id)
            if not selected_role.is_empty():
                role = selected_role.row(0, named=True).get("role")
        return {
            "chunk_id": chunk_id,
            "duration_s": chunk_row.get("duration_s"),
            "avl_ping_count": chunk_row.get("n_pings"),
            "apc_record_count": apc_count,
            "context_before_s": before,
            "context_after_s": after,
            "candidate_keys": keys,
            "candidate_table_key_count_total": len(keys),
            "complete_candidate_count": len(complete_scheduled_keys),
            "no_scheduled_alternative_count": len(keys) - len(complete_scheduled_keys),
            "anchor_n_observed_max": (
                int(candidate_rows["n_observed"].max())
                if candidate_rows is not None and not candidate_rows.is_empty()
                else None
            ),
            "hidden_fragment_role_available": role is not None,
            "hidden_anchor_diagnostic_available": self.anchor_diagnostics is not None,
        }

    def _context(self, chunk_row: Mapping[str, Any]) -> tuple[int | None, int | None]:
        if not chunk_row or self.chunks is None:
            return None, None
        siblings = self.chunks.filter(
            (pl.col("run_id") == chunk_row["run_id"])
            & (pl.col("profile") == chunk_row["profile"])
        ).sort("start_instant_s")
        ids = siblings["chunk_id"].to_list()
        if chunk_row["chunk_id"] not in ids:
            return None, None
        index = ids.index(chunk_row["chunk_id"])
        before = None
        after = None
        if index > 0:
            previous = siblings.row(index - 1, named=True)
            before = int(chunk_row["start_instant_s"] - previous["start_instant_s"])
        if index + 1 < siblings.height:
            following = siblings.row(index + 1, named=True)
            after = int(following["end_instant_s"] - chunk_row["end_instant_s"])
        return before, after


def _load_evidence(path: Path) -> _EvidenceStore:
    return _EvidenceStore(path)


def _read_optional_parquet(path: Path) -> pl.DataFrame | None:
    return pl.read_parquet(path) if path.exists() else None


def _first_pass_row(
    packet: PacketCase,
    label: FirstPassLabel,
    summary: ParsedSummary,
    ttc_root: Path,
    current_export_sha256: str | None,
    history_sha256: str | None,
) -> dict[str, Any]:
    return {
        "case_id": packet.case_id,
        "original_stratum": packet.original_stratum,
        "original_kind": packet.original_kind,
        "original_n_events": packet.original_n_events,
        "original_span_rel_s": packet.original_span_rel_s,
        "original_summary": packet.original_summary,
        "raw_primary_label": label.raw_primary_label,
        "raw_selected_card": label.raw_selected_card,
        "raw_selected_candidate_id": label.raw_selected_candidate_id,
        "raw_confidence": label.raw_confidence,
        "raw_fragment_impression": label.raw_fragment_impression,
        "raw_rationale": json.dumps(
            list(label.raw_rationale), ensure_ascii=False, separators=(",", ":")
        ),
        "raw_notes": label.raw_notes,
        "summary_file": portable_path(summary.source_file, [ttc_root]),
        "summary_file_sha256": summary.source_sha256,
        "review_timestamp": label.review_timestamp,
        "reviewer_id": label.reviewer_id,
        "diagnostics_revealed_before_save": (label.diagnostics_revealed_before_save),
        "raw_diagnostics_revealed_before_save": (
            label.raw_diagnostics_revealed_before_save
        ),
        "history_diagnostics_revealed_before_save": (
            label.history_diagnostics_revealed_before_save
        ),
        "diagnostics_reveal_source": label.diagnostics_reveal_source,
        "diagnostics_reveal_consistency_status": (
            label.diagnostics_reveal_consistency_status
        ),
        "first_pass_source_kind": label.source_kind,
        "first_pass_source_line_number": label.source_line_number,
        "current_export_matches_first_pass": (label.current_export_matches_first_pass),
        "current_export_sha256": current_export_sha256,
        "review_history_sha256": history_sha256,
    }


def _inventory_row(
    packet: PacketCase,
    summary: ParsedSummary,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    duration = evidence.get("duration_s") or packet.chunk_duration_s
    naive_window_duration = (
        int((summary.window_end - summary.window_start).total_seconds())
        if summary.window_start is not None and summary.window_end is not None
        else None
    )
    window_status = "consistent"
    if naive_window_duration is not None and duration != naive_window_duration:
        window_status = "dst_elapsed_mismatch"
    evaluable = [
        candidate for candidate in summary.candidates if candidate.has_visible_metrics
    ]
    return {
        "case_id": packet.case_id,
        "original_stratum": packet.original_stratum,
        "original_kind": packet.original_kind,
        "original_n_events": packet.original_n_events,
        "original_span_rel_s": packet.original_span_rel_s,
        "n_events_meaning": "count of synthetic packet events",
        "span_rel_s_meaning": "span of synthetic packet-event offsets",
        "packet_metadata_status": "non_operational_and_non_informative",
        "stratum_status": "duration_proxy_with_hard_coded_route_fallback",
        "kind_status": "packet_task_not_verified_semantic_grain",
        "chunk_duration_s": duration,
        "displayed_window_naive_duration_s": naive_window_duration,
        "displayed_window_naive_duration_status": window_status,
        "avl_ping_count": evidence.get("avl_ping_count"),
        "apc_record_count": evidence.get("apc_record_count"),
        "apc_record_count_scope": "distinct anchor-evidence APC sources",
        "candidate_count": len(summary.candidates),
        "displayed_evaluable_candidate_count": len(evaluable),
        "complete_candidate_count": evidence.get("complete_candidate_count"),
        "candidate_table_key_count_total": evidence.get(
            "candidate_table_key_count_total"
        ),
        "no_scheduled_alternative_count": evidence.get(
            "no_scheduled_alternative_count"
        ),
        "observed_stop_count": None,
        "observed_stop_count_status": (
            "not_directly_available; candidate_n_observed_max_is_not_a_case-level_unique-stop_count"
        ),
        "candidate_n_observed_max": evidence.get("anchor_n_observed_max"),
        "candidate_stop_coverage_min_pct": min(
            (
                candidate.coverage_pct
                for candidate in evaluable
                if candidate.coverage_pct is not None
            ),
            default=None,
        ),
        "candidate_stop_coverage_max_pct": max(
            (
                candidate.coverage_pct
                for candidate in evaluable
                if candidate.coverage_pct is not None
            ),
            default=None,
        ),
        "context_before_s": evidence.get("context_before_s"),
        "context_after_s": evidence.get("context_after_s"),
        "context_before_s_definition": (
            "previous-chunk start to current-chunk start; available map context, not proof viewed"
        ),
        "context_after_s_definition": (
            "current-chunk end to next-chunk end; available map context, not proof viewed"
        ),
        "hidden_diagnostics_available": evidence.get(
            "hidden_anchor_diagnostic_available"
        ),
        "diagnostic_role_available_but_not_used_for_routing": evidence.get(
            "hidden_fragment_role_available"
        ),
    }


def _adjudication_queue(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        rows, key=lambda row: (-int(row["review_priority_score"]), row["case_id"])
    )
    return [
        {
            "queue_rank": index,
            "case_id": row["case_id"],
            "review_priority_score": row["review_priority_score"],
            "priority_is_probability": False,
            "preserved_original_label": row["original_label"],
            "case_grain_suggestion": row["case_grain_suggestion"],
            "task_routing_suggestion": row["task_routing_suggestion"],
            "audit_reason_codes": row["audit_reason_codes"],
            "recommended_second_pass_questions": row[
                "recommended_second_pass_questions"
            ],
            "hide_first_pass_during_review": True,
            "automatic_final_label": False,
        }
        for index, row in enumerate(ordered, start=1)
    ]


def _child_case_requests(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: item["case_id"]):
        grain = row["case_grain_suggestion"]
        if grain == "multi_trip_chunk":
            expected = max(2, len(row.get("referenced_cards", ())))
            requests.append(
                _child_request(
                    row,
                    "per_subtrip_cases",
                    f"request {expected} per-subtrip review objects",
                    expected,
                )
            )
            requests.append(
                _child_request(
                    row,
                    "boundary_cases",
                    f"request {expected - 1} boundary review objects",
                    expected - 1,
                )
            )
        elif grain == "boundary_spanning":
            requests.append(
                _child_request(
                    row,
                    "boundary_inbound_outbound_cases",
                    "request one boundary object plus separate inbound/outbound roles",
                    3,
                )
            )
        elif grain == "local_fragment":
            requests.append(
                _child_request(
                    row,
                    "fragment_role_case",
                    "request fragment role and previous/next stitch review",
                    1,
                )
            )
    for index, request in enumerate(requests, start=1):
        request["request_id"] = f"R-{index:03d}"
    return requests


def _child_request(
    row: Mapping[str, Any], request_type: str, detail: str, requested_count: int
) -> dict[str, Any]:
    return {
        "request_id": "",
        "parent_case_id": row["case_id"],
        "request_type": request_type,
        "request_detail": detail,
        "requested_child_count": requested_count,
        "parent_display_card_references": row.get("referenced_cards", ()),
        "boundary_stop_if_explicit": row.get("boundary_stop"),
        "boundary_time_relation": row.get("boundary_time_relation"),
        "boundary_offset_from_parent_end_s": row.get(
            "boundary_offset_from_chunk_end_s"
        ),
        "parent_note_time_references_rel_s": row.get("note_time_references_rel_s", ()),
        "inbound_candidate": None,
        "outbound_candidate": None,
        "request_status": "proposal_not_generated",
        "automatic_child_label_created": False,
    }


def _write_outputs(
    writer: AuditArtifactWriter,
    first_pass_rows: Sequence[Mapping[str, Any]],
    inventory_rows: Sequence[Mapping[str, Any]],
    audit_rows: Sequence[Mapping[str, Any]],
    audit_summary: Mapping[str, Any],
    readiness_report: Mapping[str, Any],
    queue_rows: Sequence[Mapping[str, Any]],
    child_rows: Sequence[Mapping[str, Any]],
    proposed_schema: Mapping[str, Any],
    findings: str,
    next_steps: str,
    figures: Mapping[str, str],
    input_inventory: Sequence[Mapping[str, Any]],
    tree_files: Mapping[str, str],
) -> None:
    writer.write_parquet(
        first_pass_rows,
        "first_pass_labels_v1.parquet",
        grain="one immutable first-pass human judgment per case",
        key=["case_id"],
        contains_human_labels=True,
        privacy="restricted-local",
    )
    writer.write_csv(
        inventory_rows,
        "label_case_inventory.csv",
        grain="one packet and replacement-metadata inventory row per case",
        key=["case_id"],
        contains_human_labels=False,
        privacy="repository-safe-derived",
    )
    safe_audit_rows = [
        {column: row.get(column) for column in AUDIT_CASE_COLUMNS} for row in audit_rows
    ]
    writer.write_csv(
        safe_audit_rows,
        "label_audit_cases.csv",
        grain="one nonbinding audit suggestion row per first-pass case",
        key=["case_id"],
        contains_human_labels=True,
        privacy="repository-safe-derived",
        columns=AUDIT_CASE_COLUMNS,
    )
    writer.write_json(
        audit_summary,
        "label_audit_summary.json",
        grain="one aggregate human-label audit summary",
        key=["schema_version"],
        contains_human_labels=True,
        privacy="repository-safe-derived",
    )
    writer.write_json(
        readiness_report,
        "label_readiness_report.json",
        grain="one aggregate task-specific readiness report",
        key=["schema_version"],
        contains_human_labels=True,
        privacy="repository-safe-derived",
    )
    writer.write_csv(
        queue_rows,
        "adjudication_queue_v2.csv",
        grain="one review-order entry per parent case",
        key=["queue_rank", "case_id"],
        contains_human_labels=True,
        privacy="repository-safe-derived",
    )
    writer.write_csv(
        child_rows,
        "child_case_requests.csv",
        grain="one proposed child-case request; no child label",
        key=["request_id"],
        contains_human_labels=True,
        privacy="repository-safe-derived",
    )
    writer.write_json(
        proposed_schema,
        "proposed_label_schema_v2.json",
        grain="one nonbinding label-schema proposal",
        key=["schema_version"],
        contains_human_labels=False,
        privacy="repository-safe-derived",
    )
    writer.write_markdown(
        findings,
        "human_label_audit_findings.md",
        grain="one repository-safe audit findings report",
        key=["report"],
        contains_human_labels=True,
        privacy="repository-safe-derived",
    )
    writer.write_markdown(
        next_steps,
        "possible_next_steps.md",
        grain="one ranked, non-authorizing next-step analysis",
        key=["report"],
        contains_human_labels=True,
        privacy="repository-safe-derived",
    )
    for name, svg in sorted(figures.items()):
        writer.write_svg(
            svg,
            name,
            grain="one repository-safe aggregate or pseudonymous audit figure",
            key=["figure"],
        )
    writer.write_json(
        {
            "files": list(input_inventory),
            "source_tree": {
                "combined_sha256": writer.source_tree_sha256,
                "file_count": len(tree_files),
            },
        },
        "input_hashes.json",
        grain="one row-equivalent per inspected source file",
        key=["path"],
        contains_human_labels=False,
        privacy="restricted-local",
    )


def _discover_source_files(
    ttc_root: Path,
    cases_path: Path,
    summaries: Mapping[str, ParsedSummary],
    artifacts_path: Path,
    current_labels_path: Path,
    review_history_path: Path,
) -> set[Path]:
    files = {cases_path, current_labels_path, review_history_path}
    files.update(summary.source_file for summary in summaries.values())
    optional = [
        ttc_root / "tools" / "labeling_ui" / "labels" / "drafts.json",
        ttc_root / "tools" / "labeling_ui" / "README.md",
        ttc_root / "docs" / "r1-spike02r-kernel-repair-findings.md",
        artifacts_path / "label_schema.json",
        artifacts_path / "labeling_packet_restricted.parquet",
        artifacts_path / "x_complete_schedule_candidates.parquet",
        artifacts_path / "x_refined_chunks.parquet",
        artifacts_path / "x_anchor_evidence_edges.parquet",
        artifacts_path / "x_anchor_candidates_v2.parquet",
        artifacts_path / "x_fragment_roles.parquet",
        artifacts_path / "experiment_preregistration.json",
        artifacts_path / "sampling_plan.json",
        artifacts_path / "run_manifest.json",
        artifacts_path / "r1-spike02r-findings-full.md",
        ttc_root
        / "experiments"
        / "r1_design_spike"
        / "src"
        / "r1spike"
        / "run_spike02r.py",
        ttc_root
        / "experiments"
        / "r1_design_spike"
        / "src"
        / "r1spike"
        / "label_factorial.py",
        ttc_root / "tools" / "labeling_ui" / "app.py",
        ttc_root / "tools" / "labeling_ui" / "labeling" / "resolver.py",
        ttc_root / "tools" / "labeling_ui" / "labeling" / "plots.py",
        ttc_root / "tools" / "labeling_ui" / "labeling" / "parsing.py",
        ttc_root / "tools" / "labeling_ui" / "labeling" / "store.py",
    ]
    files.update(path for path in optional if path.exists())
    missing = [
        path
        for path in (cases_path, current_labels_path, review_history_path)
        if not path.exists()
    ]
    if missing:
        raise FileNotFoundError(f"required label inputs are missing: {missing}")
    return files


def _input_inventory(
    paths: Sequence[Path] | set[Path],
    input_hashes: Mapping[str, str],
    ttc_root: Path,
) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(paths):
        portable = portable_path(path, [ttc_root])
        rows.append(
            {
                "path": portable,
                "sha256": input_hashes[portable],
                "role": _input_role(path.name),
                "privacy_classification": (
                    "restricted-local"
                    if path.suffix in {".parquet", ".jsonl"}
                    or "summary" in path.name
                    or "scheduled_trip_labels" in path.name
                    else "repository-safe-or-metadata"
                ),
                "used_for_human_label_source": path.name
                in {"scheduled_trip_labels.csv", "scheduled_trip_labels.jsonl"},
            }
        )
    return rows


def _input_role(name: str) -> str:
    if name == "labeling_packet_redacted.csv":
        return "original_packet"
    if "summary" in name:
        return "reviewed_summary"
    if name == "scheduled_trip_labels.csv":
        return "current_label_export"
    if name == "scheduled_trip_labels.jsonl":
        return "append_only_review_history"
    if name == "drafts.json":
        return "unsaved_draft_not_a_label"
    if name == "experiment_preregistration.json":
        return "frozen_preregistration"
    if name == "label_schema.json":
        return "existing_label_schema"
    if name.endswith(".parquet"):
        return "restricted_case_or_candidate_evidence"
    if "findings" in name:
        return "spike_findings"
    return "supporting_metadata"


def _adversarial_review(
    staging_dir: Path,
    audit_rows: Sequence[Mapping[str, Any]],
    labels: Sequence[FirstPassLabel],
    summaries: Mapping[str, ParsedSummary],
    original_unchanged: bool,
) -> dict[str, Any]:
    repository_safe = [
        path
        for path in staging_dir.rglob("*")
        if path.is_file()
        and path.name != "first_pass_labels_v1.parquet"
        and not path.name.startswith("first_pass_labels_v1.parquet.meta")
        and path.suffix in {".csv", ".json", ".md", ".svg"}
    ]
    combined = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in repository_safe
    )
    raw_vehicle_ids = set()
    for summary in summaries.values():
        text = summary.source_file.read_text(encoding="utf-8")
        match = re.search(r"^- vehicle:\s*(\S+)", text, flags=re.MULTILINE)
        if match:
            raw_vehicle_ids.add(match.group(1))
    candidate_keys = {
        label.raw_selected_candidate_id
        for label in labels
        if label.raw_selected_candidate_id
    }
    exact_review_timestamps = {
        label.review_timestamp for label in labels if label.review_timestamp
    }
    substantive_raw_notes = {
        label.raw_notes for label in labels if len(label.raw_notes.strip()) >= 12
    }
    checks = {
        "no_machine_final_or_replacement_label_field": all(
            "corrected_label" not in row and "final_label" not in row
            for row in audit_rows
        ),
        "suggestions_carry_evidence_strength": all(
            bool(row["suggestion_strength"]) for row in audit_rows
        ),
        "card_letters_not_exported_as_stable_identity": "candidate_card_identity"
        not in combined,
        "fragments_not_counted_as_single_trip": all(
            row["case_grain_suggestion"] != "single_trip_candidate"
            for row in audit_rows
            if "LOCAL_FRAGMENT_NOT_FULL_TRIP" in row["audit_reason_codes"]
        ),
        "multi_trip_cases_route_to_segmentation": all(
            row["task_routing_suggestion"] == "segmentation_multi_trip"
            for row in audit_rows
            if row["case_grain_suggestion"] == "multi_trip_chunk"
        ),
        "boundary_cases_not_forced_to_one_owner": all(
            row["task_routing_suggestion"] == "boundary_spanning_dual_role"
            for row in audit_rows
            if row["case_grain_suggestion"] == "boundary_spanning"
        ),
        "invalid_source_routes_to_quality": all(
            row["task_routing_suggestion"] == "source_time_quality"
            for row in audit_rows
            if row["case_grain_suggestion"] == "data_quality_invalid"
        ),
        "blank_notes_have_no_inferred_heuristics": all(
            not row["explicit_reviewer_heuristics"]
            for row in audit_rows
            if not row["notes_present"]
        ),
        "no_raw_vehicle_ids_in_repository_safe_outputs": not any(
            vehicle_id in combined for vehicle_id in raw_vehicle_ids
        ),
        "no_selected_complete_keys_in_repository_safe_outputs": not any(
            key in combined for key in candidate_keys
        ),
        "no_exact_review_event_timestamps_in_repository_safe_outputs": not any(
            timestamp in combined for timestamp in exact_review_timestamps
        ),
        "no_verbatim_substantive_notes_in_repository_safe_outputs": not any(
            note in combined for note in substantive_raw_notes
        ),
        "no_precise_case_timestamps_in_repository_safe_outputs": re.search(
            r"\b\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\b", combined
        )
        is None,
        "no_absolute_workspace_paths_in_repository_safe_outputs": "/home/"
        not in combined,
        "factorial_remains_blocked": "factorial remains blocked" in combined.lower(),
        "original_inputs_unchanged": original_unchanged,
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise AuditArtifactError(f"adversarial review failed: {failed}")
    return {"status": "passed", "lenses": checks}


def _find_ttc_root(path: Path) -> Path:
    for parent in (path, *path.parents):
        if (parent / "pyproject.toml").exists() and (parent / "experiments").exists():
            return parent
    raise HumanLabelParseError(f"cannot locate TTC project root from {path}")


def _one_label_per_case(labels: Sequence[FirstPassLabel]) -> dict[str, FirstPassLabel]:
    by_case: dict[str, list[FirstPassLabel]] = {}
    for label in labels:
        by_case.setdefault(label.case_id, []).append(label)
    duplicates = {
        case_id: values for case_id, values in by_case.items() if len(values) != 1
    }
    if duplicates:
        raise HumanLabelParseError(
            "audit expects one first-pass reviewer per case; found "
            + ", ".join(
                f"{case_id}={len(values)}" for case_id, values in duplicates.items()
            )
        )
    return {case_id: values[0] for case_id, values in by_case.items()}


def _validate_case_union(
    packets: Sequence[PacketCase],
    summaries: Mapping[str, ParsedSummary],
    labels: Mapping[str, FirstPassLabel],
) -> None:
    packet_ids = {packet.case_id for packet in packets}
    union = packet_ids | set(summaries) | set(labels)
    problems = {
        "missing_packet": sorted(union - packet_ids),
        "missing_summary": sorted(union - set(summaries)),
        "missing_label": sorted(union - set(labels)),
    }
    problems = {key: value for key, value in problems.items() if value}
    if problems:
        raise HumanLabelParseError(f"case inventory is incomplete: {problems}")


def _summary_matches_label(summary: ParsedSummary, label: FirstPassLabel) -> bool:
    return (
        summary.displayed_label == neutral_primary_label(label.raw_primary_label)
        and summary.displayed_selected_card == label.raw_selected_card
        and summary.displayed_confidence == label.raw_confidence
        and summary.displayed_notes == label.raw_notes
    )


def _deterministic_generated_at(labels: Sequence[FirstPassLabel]) -> str:
    timestamps = sorted(
        label.review_timestamp for label in labels if label.review_timestamp
    )
    if not timestamps:
        return "1970-01-01T00:00:00Z"
    # Reproducible artifacts cannot contain a changing wall clock.  Day-level
    # precision also avoids copying a review event's exact time into safe files.
    return timestamps[-1][:10] + "T00:00:00Z"


def _review_history_summary(
    events: Sequence[Mapping[str, Any]], labels: Sequence[FirstPassLabel]
) -> dict[str, Any]:
    event_counts: dict[str, int] = {}
    for event in events:
        event_name = str(event.get("event", "unknown"))
        event_counts[event_name] = event_counts.get(event_name, 0) + 1
    first_by_key = {(label.case_id, label.reviewer_id): label for label in labels}
    resaves = [event for event in events if event.get("event") == "resave"]
    identical_resaves = sum(
        _event_matches_first_pass(
            event,
            first_by_key.get(
                (str(event.get("case_id", "")), str(event.get("reviewer", "")))
            ),
        )
        for event in resaves
    )
    return {
        "event_count": len(events),
        "event_type_counts": dict(sorted(event_counts.items())),
        "first_pass_label_count": len(labels),
        "resave_count": len(resaves),
        "resaves_semantically_identical_to_first_pass": identical_resaves,
        "diagnostic_reveal_event_count": sum(
            "reveal" in str(event.get("event", "")).lower() for event in events
        ),
        "current_export_matches_first_pass_count": sum(
            label.current_export_matches_first_pass is True for label in labels
        ),
    }


def _design_contract_summary(
    artifacts_path: Path, current_case_ids: set[str], ttc_root: Path
) -> dict[str, Any]:
    prereg_path = artifacts_path / "experiment_preregistration.json"
    schema_path = artifacts_path / "label_schema.json"
    sampling_path = artifacts_path / "sampling_plan.json"
    ui_app_path = ttc_root / "tools" / "labeling_ui" / "app.py"
    ui_resolver_path = ttc_root / "tools" / "labeling_ui" / "labeling" / "resolver.py"
    prereg = (
        json.loads(prereg_path.read_text(encoding="utf-8"))
        if prereg_path.exists()
        else {}
    )
    schema = (
        json.loads(schema_path.read_text(encoding="utf-8"))
        if schema_path.exists()
        else {}
    )
    sampling = (
        json.loads(sampling_path.read_text(encoding="utf-8"))
        if sampling_path.exists()
        else {}
    )
    split = prereg.get("split", {})
    calibration = set(split.get("calibration_vehicle_days", []))
    holdout = set(split.get("holdout_vehicle_days", []))
    phase_b = set(sampling.get("selected_pseudonyms", []))
    ui_app = ui_app_path.read_text(encoding="utf-8") if ui_app_path.exists() else ""
    ui_resolver = (
        ui_resolver_path.read_text(encoding="utf-8")
        if ui_resolver_path.exists()
        else ""
    )
    return {
        "preregistration_present": prereg_path.exists(),
        "preregistration_version": prereg.get("preregistration_version"),
        "labels_reviewed": prereg.get("labels_reviewed"),
        "label_artifact_registered": bool(prereg.get("label_artifact_sha256")),
        "r1_closed": prereg.get("r1_closed"),
        "architecture_selected": prereg.get("architecture_selected"),
        "calibration_vehicle_day_count": len(calibration),
        "holdout_vehicle_day_count": len(holdout),
        "calibration_holdout_overlap_count": len(calibration & holdout),
        "development_day_excluded_from_holdout": split.get(
            "development_day_excluded_from_holdout"
        ),
        "acceptance_procedure": prereg.get("metrics", {}).get("acceptance_procedure"),
        "phase_b_selected_count": len(phase_b),
        "phase_b_processed": sampling.get("processed"),
        "phase_b_current_packet_case_id_overlap_count": len(phase_b & current_case_ids),
        "current_packet_holdout_status": "not_established",
        "existing_label_schema_present": schema_path.exists(),
        "existing_label_schema_version": schema.get("version"),
        "existing_label_schema_reviewed": schema.get("reviewed_by_human"),
        "existing_label_schema_task_sections": sorted(
            key for key in schema if key.endswith("_labels")
        ),
        "sampling_plan_present": sampling_path.exists(),
        "ui_evidence_audit": {
            "panels_available": [
                "map",
                "timeline",
                "time_distance",
                "stop_ladder",
                "comparison_table",
            ],
            "previous_chunk_map_default_visible": (
                'checkbox("prev chunk", value=True)' in ui_app
            ),
            "next_chunk_map_default_visible": (
                'checkbox("next chunk", value=True)' in ui_app
            ),
            "panels_viewed_recorded": False,
            "diagnostics_hidden_until_explicit_reveal": (
                "Hidden by default to limit anchoring" in ui_app
            ),
            "shortlist_uses_anchor_or_qscore": "highest-qscore keys" in ui_resolver,
            "card_order_is_neutral_after_shortlisting": (
                "neutral, deterministic letters" in ui_resolver
            ),
            "full_candidate_list_is_diagnostic_only": ("full candidate list" in ui_app),
            "markdown_summaries_include_adjacent_context": False,
        },
    }


def _event_matches_first_pass(
    event: Mapping[str, Any], label: FirstPassLabel | None
) -> bool:
    if label is None:
        return False
    rationale = event.get("rationale", [])
    if isinstance(rationale, str):
        rationale = [part for part in rationale.split("|") if part]
    return (
        str(event.get("label", "")) == label.raw_primary_label
        and (event.get("selected_candidate_letter") or None) == label.raw_selected_card
        and (event.get("selected_candidate_id") or None)
        == label.raw_selected_candidate_id
        and int(event["confidence"]) == label.raw_confidence
        and (event.get("fragment_impression") or None) == label.raw_fragment_impression
        and tuple(str(item) for item in rationale) == label.raw_rationale
        and str(event.get("notes", "")) == label.raw_notes
        and bool(event.get("diagnostics_revealed_before_save", False))
        is bool(label.raw_diagnostics_revealed_before_save)
    )


def _relative_time_reference(reference: Any) -> str:
    """Serialize a note clock as offsets only; never copy its exact timestamp."""

    start = int(reference.offset_from_window_start_s)
    end = int(reference.offset_from_window_end_s)
    return f"{reference.relation_to_window}:start{start:+d}:end{end:+d}"
