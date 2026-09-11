"""Deterministic child-case generation from the audit's child-case requests.

Children are review *tasks*, never labels: no parent judgment, note, or
audit suggestion is copied into a child label. Seeds extracted from
first-pass notes are carried only as ``human_note_seeded_unverified``
context, and the UI keeps them hidden until the parent's own blind
second-pass judgment is saved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .labeling_v2_contracts import CHILD_CASE_GENERATION_VERSION, CHILD_TASK_TYPES

SEED_STATUS = "human_note_seeded_unverified"
_REQUEST_SOURCE = "artifacts/human-label-audit-v1/child_case_requests.csv"


class ChildCaseGenerationError(ValueError):
    """The request table violates the frozen generation contract."""


def _child_row(
    *,
    child_case_id: str,
    parent_case_id: str,
    child_task_type: str,
    request_id: str,
    generation_reason: str,
    seeded_from_human_note: bool,
    seed_note_time_references_rel_s: str | None,
    seed_boundary_stop: str | None,
    seed_boundary_offset_from_parent_end_s: int | None,
    seed_display_card_references: str | None,
    parent_universe_hash: str | None,
) -> dict[str, Any]:
    if child_task_type not in CHILD_TASK_TYPES:
        raise ChildCaseGenerationError(f"unknown child task type: {child_task_type}")
    return {
        "child_case_id": child_case_id,
        "parent_case_id": parent_case_id,
        "child_task_type": child_task_type,
        "context_interval_rel_s": None,
        "context_interval_source": "parent_chunk_full_window",
        "source_request_id": request_id,
        "source_artifact": _REQUEST_SOURCE,
        "generation_version": CHILD_CASE_GENERATION_VERSION,
        "generation_reason": generation_reason,
        "seeded_from_human_note": seeded_from_human_note,
        "seed_status": SEED_STATUS if seeded_from_human_note else None,
        "seed_note_time_references_rel_s": seed_note_time_references_rel_s,
        "seed_boundary_stop": seed_boundary_stop,
        "seed_boundary_offset_from_parent_end_s": (
            seed_boundary_offset_from_parent_end_s
        ),
        "seed_display_card_references": seed_display_card_references,
        "seed_hidden_until_parent_blind_save": seeded_from_human_note,
        "candidate_universe_hash": parent_universe_hash,
        "automatic_child_label_created": False,
        "review_status": "SECOND_PASS_NOT_STARTED",
    }


def _int_or_none(value: Any) -> int | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def generate_child_cases(
    requests: Sequence[Mapping[str, Any]],
    universe_hashes: Mapping[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Children plus a generation report explaining any count difference.

    ``requests`` are the rows of the audit's ``child_case_requests.csv`` in
    file order; ``universe_hashes`` maps parent case_id to its frozen
    candidate-universe hash. Output order is deterministic: request order,
    then within-request slug order, then protocol-added sequence children.
    """

    children: list[dict[str, Any]] = []
    multi_trip_parents: list[str] = []
    for request in requests:
        request_id = str(request["request_id"])
        parent = str(request["parent_case_id"])
        request_type = str(request["request_type"])
        count = _int_or_none(request.get("requested_child_count")) or 0
        seed_refs = str(request.get("parent_note_time_references_rel_s") or "") or None
        seed_cards = str(request.get("parent_display_card_references") or "") or None
        seed_stop = str(request.get("boundary_stop_if_explicit") or "") or None
        seed_offset = _int_or_none(request.get("boundary_offset_from_parent_end_s"))
        shared = {
            "request_id": request_id,
            "generation_reason": f"audit_child_request:{request_type}",
            "seeded_from_human_note": bool(seed_refs or seed_stop or seed_cards),
            "seed_note_time_references_rel_s": seed_refs,
            "seed_boundary_stop": seed_stop,
            "seed_boundary_offset_from_parent_end_s": seed_offset,
            "seed_display_card_references": seed_cards,
            "parent_universe_hash": universe_hashes.get(parent),
        }
        if request_type == "fragment_role_case":
            children.append(
                _child_row(
                    child_case_id=f"{parent}::fragment-role-1",
                    parent_case_id=parent,
                    child_task_type="fragment_role",
                    **shared,
                )
            )
        elif request_type == "per_subtrip_cases":
            if count < 1:
                raise ChildCaseGenerationError(
                    f"{request_id}: per_subtrip_cases needs a positive count"
                )
            multi_trip_parents.append(parent)
            for index in range(1, count + 1):
                children.append(
                    _child_row(
                        child_case_id=f"{parent}::subtrip-{index}",
                        parent_case_id=parent,
                        child_task_type="subtrip_scheduled_match",
                        **shared,
                    )
                )
        elif request_type == "boundary_cases":
            if count < 1:
                raise ChildCaseGenerationError(
                    f"{request_id}: boundary_cases needs a positive count"
                )
            for index in range(1, count + 1):
                children.append(
                    _child_row(
                        child_case_id=f"{parent}::boundary-{index}",
                        parent_case_id=parent,
                        child_task_type="boundary_interval",
                        **shared,
                    )
                )
        elif request_type == "boundary_inbound_outbound_cases":
            for slug, task in (
                ("boundary-1", "boundary_interval"),
                ("inbound-context-1", "inbound_context"),
                ("outbound-context-1", "outbound_context"),
            ):
                children.append(
                    _child_row(
                        child_case_id=f"{parent}::{slug}",
                        parent_case_id=parent,
                        child_task_type=task,
                        **shared,
                    )
                )
        else:
            raise ChildCaseGenerationError(
                f"{request_id}: unknown request type {request_type!r}"
            )
    for parent in sorted(set(multi_trip_parents)):
        children.append(
            _child_row(
                child_case_id=f"{parent}::sequence-1",
                parent_case_id=parent,
                child_task_type="sequence_review",
                request_id="protocol-v2.0",
                generation_reason="protocol_mandated_sequence_review_for_multi_trip",
                seeded_from_human_note=False,
                seed_note_time_references_rel_s=None,
                seed_boundary_stop=None,
                seed_boundary_offset_from_parent_end_s=None,
                seed_display_card_references=None,
                parent_universe_hash=universe_hashes.get(parent),
            )
        )
    seen: set[str] = set()
    for child in children:
        if child["child_case_id"] in seen:
            raise ChildCaseGenerationError(
                f"duplicate child case id: {child['child_case_id']}"
            )
        seen.add(child["child_case_id"])
    report = {
        "request_count": len(requests),
        "audit_expected_request_count": 11,
        "parent_count": len({child["parent_case_id"] for child in children}),
        "generated_child_count": len(children),
        "protocol_added_sequence_children": len(set(multi_trip_parents)),
        "count_difference_explanation": (
            "the audit issued request rows, not child rows; each request "
            "expands to its requested child count, and the v2 protocol adds "
            "one sequence_review child per multi-trip parent — the child "
            "count therefore exceeds the request count by design and no "
            "count was forced"
        ),
        "no_child_label_was_created": True,
    }
    return children, report
