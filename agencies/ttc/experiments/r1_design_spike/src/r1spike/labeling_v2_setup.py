"""Deterministic builder for the human-labeling v2 run01 directory.

Reads only frozen inputs (the v1 audit run, the spike02r p4 run, the v1
label files and summaries), freezes the v2 protocol artifacts, materializes
the neutral candidate universe, the routing baseline, the child cases, and
the blind review order, and initializes an honest, empty live label zone.
Repeated runs over the same inputs produce identical artifacts: no wall
clock, no unseeded randomness.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from .human_label_artifacts import (
    git_revision,
    logical_sha256,
    source_tree_hash,
)
from .human_label_parser import portable_path, sha256_file
from .labeling_v2_artifacts import V2RunWriter
from .labeling_v2_child_cases import generate_child_cases
from .labeling_v2_contracts import (
    ACTIVITY_CASES,
    ADJUDICATION_COLUMNS,
    ALL_PILOT_CASES,
    ARCHITECTURE_VERDICT,
    AUDIT_CHILD_REQUEST_COUNT,
    BOUNDARY_SPANNING_CASES,
    CHILD_CASE_GENERATION_VERSION,
    FRAGMENT_CASES,
    INSUFFICIENT_CONTEXT_CASES,
    MULTI_TRIP_CASES,
    PRESENTATION_VERSION,
    PROMOTION_CONTRACT_VERSION,
    PROTOCOL_DOCUMENTS,
    PROTOCOL_VERSION,
    QUALITY_CASES,
    SCHEDULED_SECOND_PASS_CASES,
    SCHEMA_VERSION,
    SECOND_PASS_COLUMNS,
    UI_VERSION,
    blind_review_protocol_body,
    candidate_presentation_payload,
    child_case_generation_payload,
    label_promotion_payload,
    label_schema_v2_payload,
    label_ui_contract_body,
)
from .labeling_v2_readiness import empty_live_report_payloads
from .labeling_v2_store import LOCAL_TZ, protocol_sha256_from_hashes
from .labeling_v2_universe import (
    KIND_COMPLETE,
    KIND_NULL_OPTION,
    alias_map_rows,
    build_universe_manifest,
)

EXPECTED_BASELINE = {
    **{case: "scheduled_trip_second_pass" for case in SCHEDULED_SECOND_PASS_CASES},
    **{case: "segmentation_multi_trip" for case in MULTI_TRIP_CASES},
    **{case: "boundary_spanning_dual_role" for case in BOUNDARY_SPANNING_CASES},
    **{case: "fragment_role_and_stitching" for case in FRAGMENT_CASES},
    **{case: "insufficient_context" for case in INSUFFICIENT_CONTEXT_CASES},
    **{case: "source_time_quality" for case in QUALITY_CASES},
    **{case: "no_match_activity" for case in ACTIVITY_CASES},
}

ROUTING_COLUMNS = (
    "case_id",
    "chunk_id",
    "slice",
    "stratum",
    "stratum_caveat",
    "queue_rank",
    "review_priority_score",
    "initial_task_route",
    "case_grain_suggestion",
    "audit_reason_codes",
    "recommended_second_pass_questions",
    "hide_first_pass_during_review",
    "routing_is_baseline_not_final_label",
    "case_epoch_s",
    "chunk_start_epoch_s",
    "chunk_end_epoch_s",
    "chunk_duration_s",
    "chunk_n_pings",
    "avl_ping_count",
    "apc_record_count",
    "apc_record_count_scope",
    "context_before_s",
    "context_after_s",
    "candidate_count",
    "complete_candidate_count",
    "no_scheduled_alternative_count",
    "dst_crossing",
    "blind_review_order_position",
)

STRATUM_CAVEAT = "Original packet sampling stratum — not a human truth label."


class LabelingV2SetupError(RuntimeError):
    """A frozen input is missing, inconsistent, or unsafe to build from."""


@dataclass(frozen=True)
class SetupConfig:
    audit_dir: Path
    spike02r_dir: Path
    output_dir: Path
    artifacts_root: Path | None = None
    ttc_root: Path | None = None


@dataclass(frozen=True)
class SetupResult:
    output_path: Path
    case_count: int
    child_case_count: int
    universe_row_count: int
    protocol_sha256: str
    missing_optional_inputs: tuple[str, ...]
    inputs_unchanged: bool


def _find_ttc_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "pyproject.toml").exists() and (
            candidate / "experiments"
        ).is_dir():
            return candidate
    raise LabelingV2SetupError(f"could not locate the ttc root above {start}")


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _int_or_none(value: Any) -> int | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _dst_crossing(start_epoch: int | None, end_epoch: int | None) -> bool | None:
    if start_epoch is None or end_epoch is None:
        return None
    start = datetime.fromtimestamp(start_epoch, tz=UTC).astimezone(LOCAL_TZ)
    end = datetime.fromtimestamp(end_epoch, tz=UTC).astimezone(LOCAL_TZ)
    return start.utcoffset() != end.utcoffset()


def _collect_inputs(
    config: SetupConfig, ttc_root: Path
) -> tuple[dict[str, str], list[dict[str, Any]], list[str]]:
    """Hash every source input; return (hash map, file rows, missing)."""

    required = {
        "audit_run_manifest": config.audit_dir / "run_manifest.json",
        "audit_input_hashes": config.audit_dir / "input_hashes.json",
        "first_pass_labels": config.audit_dir / "first_pass_labels_v1.parquet",
        "label_audit_cases": config.audit_dir / "label_audit_cases.csv",
        "label_case_inventory": config.audit_dir / "label_case_inventory.csv",
        "label_readiness_report": config.audit_dir / "label_readiness_report.json",
        "adjudication_queue_v2": config.audit_dir / "adjudication_queue_v2.csv",
        "child_case_requests": config.audit_dir / "child_case_requests.csv",
        "proposed_label_schema_v2": config.audit_dir / "proposed_label_schema_v2.json",
        "restricted_packet": config.spike02r_dir / "labeling_packet_restricted.parquet",
        "redacted_packet": config.spike02r_dir / "labeling_packet_redacted.csv",
        "complete_schedule_candidates": config.spike02r_dir
        / "x_complete_schedule_candidates.parquet",
        "refined_chunks": config.spike02r_dir / "x_refined_chunks.parquet",
        "preregistration": config.spike02r_dir / "experiment_preregistration.json",
    }
    optional = {
        "audit_findings": config.audit_dir / "human_label_audit_findings.md",
        "possible_next_steps": config.audit_dir / "possible_next_steps.md",
        "anchor_evidence_edges": config.spike02r_dir
        / "x_anchor_evidence_edges.parquet",
        "fragment_roles": config.spike02r_dir / "x_fragment_roles.parquet",
        "anchor_candidates_v2": config.spike02r_dir / "x_anchor_candidates_v2.parquet",
        "xlabel1_schema": config.spike02r_dir / "label_schema.json",
        "sampling_plan": config.spike02r_dir / "sampling_plan.json",
        "spike02r_run_manifest": config.spike02r_dir / "run_manifest.json",
        "v1_labels_csv": ttc_root
        / "tools/labeling_ui/labels/scheduled_trip_labels.csv",
        "v1_labels_history": ttc_root
        / "tools/labeling_ui/labels/scheduled_trip_labels.jsonl",
    }
    summaries = sorted((ttc_root / "tools/labeling_results").glob("C-*_summary*.md"))
    hashes: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for role, path in sorted(required.items()):
        if not path.exists():
            raise LabelingV2SetupError(f"required input missing ({role}): {path}")
        digest = sha256_file(path)
        rel = portable_path(path, [ttc_root])
        hashes[rel] = digest
        rows.append(
            {
                "path": rel,
                "sha256": digest,
                "role": role,
                "required": True,
                "privacy_classification": "restricted-local",
            }
        )
    for role, path in sorted(optional.items()):
        if not path.exists():
            missing.append(f"{role}: {portable_path(path, [ttc_root])}")
            continue
        digest = sha256_file(path)
        rel = portable_path(path, [ttc_root])
        hashes[rel] = digest
        rows.append(
            {
                "path": rel,
                "sha256": digest,
                "role": role,
                "required": False,
                "privacy_classification": "restricted-local",
            }
        )
    for path in summaries:
        digest = sha256_file(path)
        rel = portable_path(path, [ttc_root])
        hashes[rel] = digest
        rows.append(
            {
                "path": rel,
                "sha256": digest,
                "role": "first_pass_summary",
                "required": False,
                "privacy_classification": "restricted-local",
            }
        )
    return hashes, rows, missing


def _deterministic_generated_at(audit_dir: Path) -> str:
    manifest = json.loads((audit_dir / "run_manifest.json").read_text(encoding="utf-8"))
    generated = manifest.get("_artifact_metadata", {}).get("generated_time")
    if not generated:
        raise LabelingV2SetupError(
            "audit run manifest lacks a generated_time to inherit"
        )
    return str(generated)


def _routing_rows(
    config: SetupConfig,
    restricted: pl.DataFrame,
    refined_chunks: pl.DataFrame,
    universe_rows: list[dict[str, Any]],
    order_positions: dict[str, int],
) -> list[dict[str, Any]]:
    queue = _read_csv_rows(config.audit_dir / "adjudication_queue_v2.csv")
    inventory = {
        row["case_id"]: row
        for row in _read_csv_rows(config.audit_dir / "label_case_inventory.csv")
    }
    universe_by_case: dict[str, list[dict[str, Any]]] = {}
    for row in universe_rows:
        universe_by_case.setdefault(row["case_id"], []).append(row)
    restricted_by_case = {
        row["case_id"]: row for row in restricted.iter_rows(named=True)
    }
    rows: list[dict[str, Any]] = []
    for queue_row in queue:
        case_id = queue_row["case_id"]
        route = queue_row["task_routing_suggestion"]
        expected = EXPECTED_BASELINE.get(case_id)
        if expected != route:
            raise LabelingV2SetupError(
                f"audit routing for {case_id} ({route}) does not match the "
                f"frozen protocol baseline ({expected})"
            )
        packet_row = restricted_by_case.get(case_id)
        if packet_row is None:
            raise LabelingV2SetupError(
                f"case missing from restricted packet: {case_id}"
            )
        chunk_id = packet_row["real_id"]
        chunk_rows = refined_chunks.filter(pl.col("chunk_id") == chunk_id).sort(
            "profile"
        )
        chunk = chunk_rows.row(0, named=True) if not chunk_rows.is_empty() else {}
        inv = inventory.get(case_id, {})
        universe = universe_by_case.get(case_id, [])
        real_candidates = [
            row for row in universe if row["candidate_kind"] != KIND_NULL_OPTION
        ]
        rows.append(
            {
                "case_id": case_id,
                "chunk_id": chunk_id,
                "slice": chunk.get("slice"),
                "stratum": packet_row.get("stratum"),
                "stratum_caveat": STRATUM_CAVEAT,
                "queue_rank": _int_or_none(queue_row.get("queue_rank")),
                "review_priority_score": _int_or_none(
                    queue_row.get("review_priority_score")
                ),
                "initial_task_route": route,
                "case_grain_suggestion": queue_row.get("case_grain_suggestion"),
                "audit_reason_codes": queue_row.get("audit_reason_codes"),
                "recommended_second_pass_questions": queue_row.get(
                    "recommended_second_pass_questions"
                ),
                "hide_first_pass_during_review": True,
                "routing_is_baseline_not_final_label": True,
                "case_epoch_s": packet_row.get("case_epoch_s"),
                "chunk_start_epoch_s": chunk.get("start_instant_s"),
                "chunk_end_epoch_s": chunk.get("end_instant_s"),
                "chunk_duration_s": chunk.get("duration_s"),
                "chunk_n_pings": chunk.get("n_pings"),
                "avl_ping_count": _int_or_none(inv.get("avl_ping_count")),
                "apc_record_count": _int_or_none(inv.get("apc_record_count")),
                "apc_record_count_scope": inv.get("apc_record_count_scope"),
                "context_before_s": _int_or_none(inv.get("context_before_s")),
                "context_after_s": _int_or_none(inv.get("context_after_s")),
                "candidate_count": len(real_candidates),
                "complete_candidate_count": sum(
                    1
                    for row in real_candidates
                    if row["candidate_kind"] == KIND_COMPLETE
                ),
                "no_scheduled_alternative_count": sum(
                    1
                    for row in real_candidates
                    if row["candidate_kind"] != KIND_COMPLETE
                ),
                "dst_crossing": _dst_crossing(
                    chunk.get("start_instant_s"), chunk.get("end_instant_s")
                ),
                "blind_review_order_position": order_positions[case_id],
            }
        )
    if {row["case_id"] for row in rows} != set(ALL_PILOT_CASES):
        raise LabelingV2SetupError(
            "adjudication queue does not cover exactly the 18 pilot cases"
        )
    return rows


def _blind_order(universe_rows: list[dict[str, Any]]) -> dict[str, Any]:
    universe_logical = logical_sha256(
        [(row["case_id"], row["candidate_key"]) for row in universe_rows]
    )
    seed_material = f"{PROTOCOL_VERSION}|{universe_logical}|blind-review-order"
    seed = int(logical_sha256(seed_material)[:16], 16)
    ordered = sorted(ALL_PILOT_CASES)
    random.Random(seed).shuffle(ordered)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "seed_generation_method": (
            "sha256 of 'PROTOCOL_VERSION|logical_sha256(universe case/key "
            "pairs)|blind-review-order', first 16 hex digits as an integer, "
            "driving random.Random().shuffle over the sorted case ids"
        ),
        "seed_material": seed_material,
        "seed": seed,
        "case_order": ordered,
        "not_grouped_by_first_pass_label": True,
        "fragments_not_forced_consecutive": True,
    }


def _universe_degradation(
    universe_rows: list[dict[str, Any]], artifacts_root: Path
) -> dict[str, Any]:
    """Explicit record of schedule-window gaps — never a silent fallback.

    When windows are missing the neutral ordering degrades to lexical trip
    order for the affected candidates; that fact must be visible in the run
    manifest, not discovered later as unexplained alias drift.
    """

    complete = [
        row
        for row in universe_rows
        if row["candidate_kind"] == "complete_scheduled_trip"
    ]
    unresolved = [row for row in complete if row["sched_start_epoch_s"] is None]
    missing_dirs = sorted(
        {
            f"gtfs-{row['service_date']}-{row['feed_id']}"
            for row in unresolved
            if row["service_date"]
            and row["feed_id"]
            and not (
                artifacts_root / f"gtfs-{row['service_date']}-{row['feed_id']}"
            ).exists()
        }
    )
    return {
        "complete_candidates": len(complete),
        "schedule_windows_unresolved": len(unresolved),
        "gtfs_dirs_missing": missing_dirs,
        "ordering_degraded_to_lexical_for_unresolved": bool(unresolved),
    }


def run_setup(config: SetupConfig) -> SetupResult:
    audit_dir = config.audit_dir.resolve()
    spike02r_dir = config.spike02r_dir.resolve()
    ttc_root = (config.ttc_root or _find_ttc_root(audit_dir)).resolve()
    artifacts_root = (config.artifacts_root or spike02r_dir.parent).resolve()
    input_hashes, input_rows, missing = _collect_inputs(config, ttc_root)
    generated_at = _deterministic_generated_at(audit_dir)
    tree_sha256, tree_files = source_tree_hash(ttc_root / "experiments/r1_design_spike")
    code_rev = git_revision(ttc_root)

    restricted = pl.read_parquet(spike02r_dir / "labeling_packet_restricted.parquet")
    candidates = pl.read_parquet(
        spike02r_dir / "x_complete_schedule_candidates.parquet"
    )
    refined_chunks = pl.read_parquet(spike02r_dir / "x_refined_chunks.parquet")

    universe_rows = build_universe_manifest(restricted, candidates, artifacts_root)
    real_by_case: dict[str, int] = {}
    for row in universe_rows:
        if row["candidate_kind"] != KIND_NULL_OPTION:
            real_by_case[row["case_id"]] = real_by_case.get(row["case_id"], 0) + 1
    empty_cases = [
        row["case_id"]
        for row in restricted.iter_rows(named=True)
        if real_by_case.get(row["case_id"], 0) == 0
    ]
    if empty_cases:
        raise LabelingV2SetupError(
            "cases with an empty candidate universe contradict the frozen "
            f"inputs: {sorted(empty_cases)}"
        )
    degradation = _universe_degradation(universe_rows, artifacts_root)
    aliases = alias_map_rows(universe_rows)
    universe_hashes = {
        row["case_id"]: row["candidate_universe_hash"] for row in universe_rows
    }
    order = _blind_order(universe_rows)
    order_positions = {case: idx for idx, case in enumerate(order["case_order"])}
    routing_rows = _routing_rows(
        config, restricted, refined_chunks, universe_rows, order_positions
    )
    chunkless = [
        row["case_id"] for row in routing_rows if row["chunk_start_epoch_s"] is None
    ]
    if chunkless:
        degradation["cases_missing_chunk_window"] = sorted(chunkless)
    requests = _read_csv_rows(audit_dir / "child_case_requests.csv")
    if len(requests) != AUDIT_CHILD_REQUEST_COUNT:
        raise LabelingV2SetupError(
            f"child_case_requests.csv has {len(requests)} rows; the frozen "
            f"audit issued exactly {AUDIT_CHILD_REQUEST_COUNT} — refusing to "
            "silently drop mandated review tasks"
        )
    children, child_report = generate_child_cases(requests, universe_hashes)

    writer = V2RunWriter(
        config.output_dir,
        input_hashes=input_hashes,
        source_tree_sha256=tree_sha256,
        code_revision=code_rev,
        generated_at=generated_at,
        forbidden_paths=[audit_dir, spike02r_dir, ttc_root / "tools"],
    )
    try:
        _write_all(
            writer,
            config,
            ttc_root=ttc_root,
            input_rows=input_rows,
            missing=missing,
            tree_sha256=tree_sha256,
            tree_files=tree_files,
            universe_rows=universe_rows,
            aliases=aliases,
            routing_rows=routing_rows,
            children=children,
            child_report=child_report,
            order=order,
            degradation=degradation,
        )
        output = writer.finalize()
    except BaseException:
        writer.abort()
        raise

    unchanged = all(
        sha256_file(ttc_root / rel) == digest
        for rel, digest in input_hashes.items()
        if (ttc_root / rel).exists()
    )
    manifest = json.loads(
        (output / "protocol_freeze_manifest.json").read_text(encoding="utf-8")
    )
    return SetupResult(
        output_path=output,
        case_count=len(routing_rows),
        child_case_count=len(children),
        universe_row_count=len(universe_rows),
        protocol_sha256=manifest["protocol_sha256"],
        missing_optional_inputs=tuple(missing),
        inputs_unchanged=unchanged,
    )


def _write_all(
    writer: V2RunWriter,
    config: SetupConfig,
    *,
    ttc_root: Path,
    input_rows,
    missing,
    tree_sha256,
    tree_files,
    universe_rows,
    aliases,
    routing_rows,
    children,
    child_report,
    order,
    degradation,
) -> None:
    writer.write_json(
        label_schema_v2_payload(),
        "label_schema_v2.0.json",
        grain="one frozen label ontology",
        key=["schema_version"],
        contains_human_labels=False,
        privacy="restricted-local",
    )
    writer.write_markdown(
        label_ui_contract_body(),
        "label_ui_contract_v2.0.md",
        grain="one frozen UI contract",
        key=["document"],
        contains_human_labels=False,
        privacy="restricted-local",
    )
    writer.write_json(
        candidate_presentation_payload(),
        "candidate_presentation_contract_v2.0.json",
        grain="one frozen candidate-presentation contract",
        key=["presentation_version"],
        contains_human_labels=False,
        privacy="restricted-local",
    )
    writer.write_json(
        child_case_generation_payload(),
        "child_case_generation_contract_v2.0.json",
        grain="one frozen child-case generation contract",
        key=["generation_version"],
        contains_human_labels=False,
        privacy="restricted-local",
    )
    writer.write_json(
        label_promotion_payload(),
        "label_promotion_contract_v2.0.json",
        grain="one frozen label-promotion contract",
        key=["promotion_version"],
        contains_human_labels=False,
        privacy="restricted-local",
    )
    writer.write_markdown(
        blind_review_protocol_body(),
        "blind_review_protocol_v2.0.md",
        grain="one frozen blind-review protocol",
        key=["document"],
        contains_human_labels=False,
        privacy="restricted-local",
    )
    universe_schema = {
        "case_id": pl.String,
        "chunk_id": pl.String,
        "slice": pl.String,
        "display_order": pl.Int64,
        "candidate_alias": pl.String,
        "candidate_key": pl.String,
        "service_date": pl.String,
        "feed_id": pl.String,
        "route_mapping_id": pl.String,
        "scheduled_trip_id": pl.String,
        "polarity_hypothesis_id": pl.String,
        "explicit_null_polarity": pl.Boolean,
        "candidate_kind": pl.String,
        "unresolved_warning": pl.String,
        "sched_start_epoch_s": pl.Int64,
        "sched_end_epoch_s": pl.Int64,
        "sched_n_stops": pl.Int64,
        "presentation_version": pl.String,
        "candidate_universe_hash": pl.String,
    }
    writer.write_parquet(
        universe_rows,
        "candidate_universe_manifest.parquet",
        grain="one neutrally ordered candidate per case (complete universe)",
        key=["case_id", "candidate_key"],
        contains_human_labels=False,
        privacy="restricted-local",
        schema=universe_schema,
    )
    alias_schema = {
        key: value
        for key, value in universe_schema.items()
        if key
        not in (
            "chunk_id",
            "slice",
            "unresolved_warning",
            "sched_start_epoch_s",
            "sched_end_epoch_s",
            "sched_n_stops",
        )
    }
    writer.write_parquet(
        aliases,
        "candidate_alias_map.parquet",
        grain="one immutable display alias per case candidate",
        key=["case_id", "candidate_alias"],
        contains_human_labels=False,
        privacy="restricted-local",
        schema=alias_schema,
    )
    routing_schema: dict[str, pl.DataType] = {
        name: pl.String for name in ROUTING_COLUMNS
    }
    for name in (
        "queue_rank",
        "review_priority_score",
        "case_epoch_s",
        "chunk_start_epoch_s",
        "chunk_end_epoch_s",
        "chunk_duration_s",
        "chunk_n_pings",
        "avl_ping_count",
        "apc_record_count",
        "context_before_s",
        "context_after_s",
        "candidate_count",
        "complete_candidate_count",
        "no_scheduled_alternative_count",
        "blind_review_order_position",
    ):
        routing_schema[name] = pl.Int64
    for name in (
        "hide_first_pass_during_review",
        "routing_is_baseline_not_final_label",
        "dst_crossing",
    ):
        routing_schema[name] = pl.Boolean
    writer.write_parquet(
        routing_rows,
        "case_routing_manifest.parquet",
        grain="one editable routing-baseline row per pilot case (not a label)",
        key=["case_id"],
        contains_human_labels=False,
        privacy="restricted-local",
        schema=routing_schema,
    )
    child_schema: dict[str, pl.DataType] = {
        "child_case_id": pl.String,
        "parent_case_id": pl.String,
        "child_task_type": pl.String,
        "context_interval_rel_s": pl.String,
        "context_interval_source": pl.String,
        "source_request_id": pl.String,
        "source_artifact": pl.String,
        "generation_version": pl.String,
        "generation_reason": pl.String,
        "seeded_from_human_note": pl.Boolean,
        "seed_status": pl.String,
        "seed_note_time_references_rel_s": pl.String,
        "seed_boundary_stop": pl.String,
        "seed_boundary_offset_from_parent_end_s": pl.Int64,
        "seed_display_card_references": pl.String,
        "seed_hidden_until_parent_blind_save": pl.Boolean,
        "candidate_universe_hash": pl.String,
        "automatic_child_label_created": pl.Boolean,
        "review_status": pl.String,
    }
    writer.write_parquet(
        children,
        "child_case_manifest.parquet",
        grain="one task-specific child review case; never a label",
        key=["child_case_id"],
        contains_human_labels=False,
        privacy="restricted-local",
        schema=child_schema,
    )
    writer.write_json(
        order,
        "blind_review_order.json",
        grain="one deterministic blind case ordering",
        key=["seed"],
        contains_human_labels=False,
        privacy="restricted-local",
    )
    writer.write_json(
        {"files": input_rows, "missing_optional_inputs": missing},
        "input_hashes.json",
        grain="one row-equivalent per inspected source file",
        key=["path"],
        contains_human_labels=False,
        privacy="restricted-local",
    )
    writer.write_json(
        {
            "combined_sha256": tree_sha256,
            "file_count": len(tree_files),
            "files": tree_files,
        },
        "source_tree_manifest.json",
        grain="one hash per experiment source file",
        key=["path"],
        contains_human_labels=False,
        privacy="restricted-local",
    )

    # -- live zone, honest and empty ----------------------------------------
    second_schema: dict[str, pl.DataType] = {
        name: pl.String for name in SECOND_PASS_COLUMNS
    }
    for name in (
        "is_child_case",
        "selected_explicit_null_polarity",
        "adjacent_context_used",
        "diagnostics_revealed_before_save",
        "diagnostics_revealed_after_save",
        "first_pass_hidden",
        "dual_role",
        "scheduled_match_applicable",
    ):
        second_schema[name] = pl.Boolean
    for name in (
        "review_round",
        "grain_confidence",
        "match_confidence",
        "evidence_quality",
        "split_confidence",
        "fragment_confidence",
        "subtrip_count",
        "unresolved_subsegments",
        "blind_review_order_position",
        "candidates_hidden_by_filter_at_save",
    ):
        second_schema[name] = pl.Int64
    writer.write_parquet(
        [],
        "second_pass_labels_v2.parquet",
        grain="one blind second-pass human judgment per case",
        key=["case_id", "reviewer_id", "review_round"],
        contains_human_labels=True,
        privacy="restricted-local",
        schema=second_schema,
    )
    writer.write_parquet(
        [],
        "adjudicated_labels_v2.parquet",
        grain="one explicit adjudication record; overwrites neither pass",
        key=["case_id", "record_id"],
        contains_human_labels=True,
        privacy="restricted-local",
        schema={name: pl.String for name in ADJUDICATION_COLUMNS},
    )
    writer.write_jsonl("review_history.jsonl", [])
    queue_columns = [
        "case_id",
        "parent_case_id",
        "queue_rank",
        "initial_task_route",
        "review_states",
        "second_pass_saved",
        "comparison_outcome",
        "requires_adjudication",
        "adjudicated",
        "audit_reason_codes",
        "automatic_final_label",
    ]
    # The initial live queue is sorted by case id and masks the audit's
    # priority rank, route, and reason codes — they derive from first-pass
    # findings and become visible per case only after its blind save.
    initial_queue = [
        {
            "case_id": row["case_id"],
            "parent_case_id": None,
            "queue_rank": None,
            "initial_task_route": None,
            "review_states": ["FIRST_PASS_PRESERVED", "SECOND_PASS_NOT_STARTED"],
            "second_pass_saved": False,
            "comparison_outcome": None,
            "requires_adjudication": False,
            "adjudicated": False,
            "audit_reason_codes": None,
            "automatic_final_label": False,
        }
        for row in sorted(routing_rows, key=lambda r: r["case_id"])
    ] + [
        {
            "case_id": child["child_case_id"],
            "parent_case_id": child["parent_case_id"],
            "queue_rank": None,
            "initial_task_route": child["child_task_type"],
            "review_states": ["SECOND_PASS_NOT_STARTED"],
            "second_pass_saved": False,
            "comparison_outcome": None,
            "requires_adjudication": False,
            "adjudicated": False,
            "audit_reason_codes": None,
            "automatic_final_label": False,
        }
        for child in children
    ]
    writer.write_csv(
        initial_queue,
        "adjudication_queue_v3.csv",
        grain="one queue entry per parent or child case; no winner chosen",
        key=["case_id"],
        contains_human_labels=False,
        privacy="restricted-local",
        columns=queue_columns,
    )
    for name, payload in empty_live_report_payloads().items():
        writer.write_json(
            payload,
            name,
            grain="one aggregate live report (empty until human review)",
            key=["report"],
            contains_human_labels=False,
            privacy="restricted-local",
        )

    # -- run manifest, then the freeze manifest (which hashes everything) ----
    writer.write_json(
        {
            "run_id": "human-labeling-v2-run01",
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "ui_version": UI_VERSION,
            "presentation_version": PRESENTATION_VERSION,
            "child_case_generation_version": CHILD_CASE_GENERATION_VERSION,
            "promotion_version": PROMOTION_CONTRACT_VERSION,
            "case_count": len(routing_rows),
            "child_case_generation_report": child_report,
            "input_degradation": degradation,
            "blind_review_required_cases": list(SCHEDULED_SECOND_PASS_CASES),
            "architecture_verdict": ARCHITECTURE_VERDICT,
            "human_review_completed": False,
            "second_pass_labels_present": False,
            "factorial_authorized": False,
            "phase_b_authorized": False,
            "packet_is_independent_holdout": False,
            "artifacts": dict(sorted(writer.artifacts.items())),
            "missing_optional_inputs": missing,
            "launch_command": (
                "cd experiments/r1_design_spike/labeling_ui_v2 && uv sync && "
                "uv run streamlit run app.py"
            ),
        },
        "ui_run_manifest.json",
        grain="one run manifest",
        key=["run_id"],
        contains_human_labels=False,
        privacy="restricted-local",
    )
    document_hashes = {
        name: writer.artifacts[name]["sha256"] for name in PROTOCOL_DOCUMENTS
    }
    frozen_hashes = {
        name: writer.artifacts[name]["sha256"]
        for name in writer.artifacts
        if name != "protocol_freeze_manifest.json"
    }
    logical_map = {
        name: details["logical_sha256"] for name, details in writer.artifacts.items()
    }
    writer.write_json(
        {
            "artifacts": logical_map,
            "scope": "logical checksums of run01 artifacts at freeze time",
            "recursive_exclusions": [
                "logical_checksums.json",
                "protocol_freeze_manifest.json",
            ],
        },
        "logical_checksums.json",
        grain="one logical checksum per artifact",
        key=["artifact"],
        contains_human_labels=False,
        privacy="restricted-local",
    )
    frozen_hashes["logical_checksums.json"] = writer.artifacts[
        "logical_checksums.json"
    ]["sha256"]
    writer.write_json(
        {
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "ui_version": UI_VERSION,
            "presentation_version": PRESENTATION_VERSION,
            "child_case_generation_version": CHILD_CASE_GENERATION_VERSION,
            "promotion_version": PROMOTION_CONTRACT_VERSION,
            "protocol_sha256": protocol_sha256_from_hashes(document_hashes),
            "protocol_document_hashes": document_hashes,
            "frozen_artifact_hashes": {
                name: frozen_hashes[name]
                for name in sorted(frozen_hashes)
                if name in FROZEN_OR_INITIAL
            },
            "source_tree_sha256": tree_sha256,
            "code_revision": writer.code_revision,
            "code_dirty_note": (
                "the surrounding tree is untracked experimental work; the "
                "source_tree_sha256 above is the binding revision record"
            ),
            "dependency_lock_sha256": _lock_hash(ttc_root),
            "reviewer_identity_policy": (
                "pseudonymous reviewer ids only; one blind judgment per case "
                "per reviewer per round"
            ),
            "frozen_case_set": list(ALL_PILOT_CASES),
            "randomization": {
                "seed": order["seed"],
                "seed_generation_method": order["seed_generation_method"],
            },
            "pilot_cases_excluded_from_independent_holdout": True,
            "factorial_remains_blocked": True,
            "labels_must_reference_protocol_sha256": True,
            "version_bump_required_on_any_protocol_change": True,
        },
        "protocol_freeze_manifest.json",
        grain="one immutable protocol freeze record",
        key=["protocol_version"],
        contains_human_labels=False,
        privacy="restricted-local",
    )


FROZEN_OR_INITIAL = set(
    name
    for name in (
        "label_schema_v2.0.json",
        "label_ui_contract_v2.0.md",
        "candidate_presentation_contract_v2.0.json",
        "child_case_generation_contract_v2.0.json",
        "label_promotion_contract_v2.0.json",
        "blind_review_protocol_v2.0.md",
        "candidate_universe_manifest.parquet",
        "candidate_alias_map.parquet",
        "case_routing_manifest.parquet",
        "child_case_manifest.parquet",
        "blind_review_order.json",
        "ui_run_manifest.json",
        "input_hashes.json",
        "source_tree_manifest.json",
        "logical_checksums.json",
    )
)


def _lock_hash(ttc_root: Path) -> str | None:
    lock = ttc_root / "uv.lock"
    return sha256_file(lock) if lock.exists() else None
