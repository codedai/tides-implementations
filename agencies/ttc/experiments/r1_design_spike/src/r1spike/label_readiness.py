# ruff: noqa: E501
"""Candidate-key, consistency, priority, and readiness helpers.

Readiness is task-specific and nonexclusive.  In particular, a case can be a
useful pilot case while still requiring a blind second pass; no helper here
turns a first-pass selection into a calibration label.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import median

from .human_label_parser import FirstPassLabel, ParsedSummary, SummaryCandidate
from .label_task_routing import RoutingSuggestion

NEAR_OVERLAP_TOLERANCE_S = 30
NEAR_COVERAGE_TOLERANCE_PCT_POINTS = 1.0


@dataclass(frozen=True)
class CandidateKeyResolution:
    status: str
    serialized_key: str | None
    service_date: str | None
    feed_id: str | None
    route_mapping_id: str | None
    scheduled_trip_id: str | None
    polarity_hypothesis_id: str | None
    explicit_null_polarity: bool
    resolved_from: str

    @property
    def resolved(self) -> bool:
        return self.status in {
            "resolved_complete_key",
            "resolved_complete_key_explicit_null_polarity",
        }


@dataclass(frozen=True)
class VisibleEvidenceMetrics:
    displayed_candidate_count: int
    displayed_evaluable_candidate_count: int
    selected_overlap_s: int | None
    selected_coverage_pct: float | None
    selected_overlap_advantage_s: int | None
    selected_coverage_advantage_pct_points: float | None
    selected_start_residual_advantage_s: int | None
    selected_end_residual_advantage_s: int | None
    best_visible_overlap_advantage_s: int | None
    best_visible_coverage_advantage_pct_points: float | None
    selected_exact_tie_count: int
    selected_near_tie_count: int
    visible_near_tie_pair_count: int
    all_evaluable_candidates_tied: bool


@dataclass(frozen=True)
class ReadinessResult:
    classes: tuple[str, ...]
    strict_gate_checks: Mapping[str, bool]
    calibration_ready: bool


def resolve_candidate_key(
    selected_card: str | None,
    selected_candidate_id: str | None,
    candidate_keys_for_case: Iterable[str],
    *,
    card_key_map: Mapping[str, str] | None = None,
) -> CandidateKeyResolution:
    """Resolve a display card to the local five-position candidate contract.

    The Spike 02R contract explicitly permits a null polarity hypothesis when
    no direction evidence exists.  A trailing delimiter therefore encodes a
    complete tuple with ``polarity_hypothesis_id=None``; it is not silently
    filled or treated as a missing column.
    """

    available = set(candidate_keys_for_case)
    raw = selected_candidate_id
    resolved_from = "stored_candidate_id"
    if not raw and selected_card and card_key_map:
        raw = card_key_map.get(selected_card)
        resolved_from = "display_card_mapping"
    if not raw:
        status = (
            "card_only_key_missing"
            if selected_card
            else "not_applicable_no_selected_candidate"
        )
        return CandidateKeyResolution(
            status=status,
            serialized_key=None,
            service_date=None,
            feed_id=None,
            route_mapping_id=None,
            scheduled_trip_id=None,
            polarity_hypothesis_id=None,
            explicit_null_polarity=False,
            resolved_from="none",
        )

    parts = raw.split("|")
    if len(parts) != 5:
        return _unresolved(raw, "invalid_key_arity", resolved_from)
    try:
        date.fromisoformat(parts[0])
    except ValueError:
        return _unresolved(raw, "invalid_service_date", resolved_from)
    if not all(parts[index] for index in (0, 1, 2, 3)):
        return _unresolved(raw, "required_key_component_missing", resolved_from)
    if raw not in available:
        return _unresolved(raw, "key_not_in_case_candidate_table", resolved_from)
    explicit_null = parts[4] == ""
    return CandidateKeyResolution(
        status=(
            "resolved_complete_key_explicit_null_polarity"
            if explicit_null
            else "resolved_complete_key"
        ),
        serialized_key=raw,
        service_date=parts[0],
        feed_id=parts[1],
        route_mapping_id=parts[2],
        scheduled_trip_id=parts[3],
        polarity_hypothesis_id=parts[4] or None,
        explicit_null_polarity=explicit_null,
        resolved_from=resolved_from,
    )


def rationale_present(label: FirstPassLabel) -> bool:
    return bool(label.raw_rationale) or substantive_notes(label.raw_notes)


def substantive_notes(notes: str) -> bool:
    value = notes.strip()
    return len(value) >= 8 and value.lower() not in {
        "no notes",
        "none",
        "unknown",
        "n/a",
        "not sure",
    }


def visible_evidence_metrics(
    summary: ParsedSummary,
    selected_card: str | None,
) -> VisibleEvidenceMetrics:
    """Compute signed advantages from the evidence exported to Markdown."""

    candidates = list(summary.candidates)
    evaluable = [candidate for candidate in candidates if candidate.has_visible_metrics]
    selected = next(
        (candidate for candidate in candidates if candidate.card == selected_card), None
    )
    others = [candidate for candidate in evaluable if candidate.card != selected_card]

    selected_overlap = selected.overlap_s if selected else None
    selected_coverage = selected.coverage_pct if selected else None
    overlap_advantage = _signed_advantage(
        selected_overlap, [candidate.overlap_s for candidate in others]
    )
    coverage_advantage = _signed_advantage(
        selected_coverage, [candidate.coverage_pct for candidate in others]
    )
    start_residual_advantage = _residual_advantage(
        selected,
        others,
        summary,
        boundary="start",
    )
    end_residual_advantage = _residual_advantage(
        selected,
        others,
        summary,
        boundary="end",
    )

    exact_ties = 0
    near_ties = 0
    if selected and selected.has_visible_metrics:
        for candidate in others:
            if _same_visible_pair(selected, candidate):
                exact_ties += 1
            if _near_visible_pair(selected, candidate):
                near_ties += 1

    pairs = {(candidate.overlap_s, candidate.coverage_pct) for candidate in evaluable}
    near_pair_count = sum(
        _near_visible_pair(left, right)
        for index, left in enumerate(evaluable)
        for right in evaluable[index + 1 :]
    )
    return VisibleEvidenceMetrics(
        displayed_candidate_count=len(candidates),
        displayed_evaluable_candidate_count=len(evaluable),
        selected_overlap_s=selected_overlap,
        selected_coverage_pct=selected_coverage,
        selected_overlap_advantage_s=_int_or_none(overlap_advantage),
        selected_coverage_advantage_pct_points=coverage_advantage,
        selected_start_residual_advantage_s=_int_or_none(start_residual_advantage),
        selected_end_residual_advantage_s=_int_or_none(end_residual_advantage),
        best_visible_overlap_advantage_s=_int_or_none(
            _best_advantage([candidate.overlap_s for candidate in evaluable])
        ),
        best_visible_coverage_advantage_pct_points=_best_advantage(
            [candidate.coverage_pct for candidate in evaluable]
        ),
        selected_exact_tie_count=exact_ties,
        selected_near_tie_count=near_ties,
        visible_near_tie_pair_count=near_pair_count,
        all_evaluable_candidates_tied=len(evaluable) >= 2 and len(pairs) == 1,
    )


def consistency_reason_codes(
    label: FirstPassLabel,
    routing: RoutingSuggestion,
    visible: VisibleEvidenceMetrics,
    key: CandidateKeyResolution,
) -> tuple[str, ...]:
    reasons = set(routing.audit_reason_codes)
    matched = label.raw_primary_label.startswith("MATCH_")
    if matched and label.raw_confidence is not None and label.raw_confidence <= 2:
        reasons.add("LOW_CONFIDENCE_MATCH")
    if (
        matched
        and label.raw_confidence is not None
        and label.raw_confidence >= 4
        and not rationale_present(label)
    ):
        reasons.add("HIGH_CONFIDENCE_NO_RATIONALE")
    if visible.visible_near_tie_pair_count > 0 and not rationale_present(label):
        reasons.add("VISIBLE_EVIDENCE_NON_DISCRIMINATIVE")
    if visible.all_evaluable_candidates_tied:
        reasons.add("ALL_VISIBLE_CANDIDATES_TIED")
    if matched and not key.resolved:
        reasons.add("FULL_CANDIDATE_KEY_MISSING")
    # The five-card shortlist is qscore-informed even when diagnostics stay hidden.
    reasons.add("MODEL_INFORMED_CANDIDATE_SHORTLIST")
    reasons.add("PACKET_METADATA_NON_OPERATIONAL")
    return tuple(sorted(reasons))


def internal_consistency_status(reason_codes: Sequence[str]) -> str:
    if "NOTES_PRIMARY_LABEL_CONFLICT" in reason_codes:
        return "explicit_conflict_flagged"
    if "PRIMARY_LABEL_TASK_GRAIN_MISMATCH" in reason_codes:
        return "task_grain_mismatch_flagged"
    anomalies = {
        "LOW_CONFIDENCE_MATCH",
        "HIGH_CONFIDENCE_NO_RATIONALE",
        "VISIBLE_EVIDENCE_NON_DISCRIMINATIVE",
        "FULL_CANDIDATE_KEY_MISSING",
        "REVIEWER_TIMEZONE_TEXT_INCONSISTENT",
        "SUMMARY_FIRST_PASS_MISMATCH",
        "CURRENT_EXPORT_DIFFERS_FROM_FIRST_PASS",
        "DIAGNOSTICS_REVEAL_METADATA_CONFLICT",
        "DISPLAYED_WINDOW_DST_ELAPSED_TIME_MISMATCH",
    }
    if anomalies.intersection(reason_codes):
        return "anomaly_flagged_no_explicit_conflict"
    return "no_internal_conflict_detected"


def priority_score(reason_codes: Sequence[str]) -> int:
    """Transparent review ordering; never an uncertainty probability."""

    reasons = set(reason_codes)
    score = 0
    if "NOTES_PRIMARY_LABEL_CONFLICT" in reasons:
        score += 5
    if reasons.intersection(
        {"MULTI_TRIP_OR_SEQUENCE_CASE", "BOUNDARY_SPANNING_DUAL_ROLE"}
    ):
        score += 5
    if "DATA_QUALITY_OR_TIME_RESOLUTION_FAILURE" in reasons:
        score += 5
    if "LOW_CONFIDENCE_MATCH" in reasons:
        score += 4
    if "HIGH_CONFIDENCE_NO_RATIONALE" in reasons:
        score += 3
    if "FULL_CANDIDATE_KEY_MISSING" in reasons:
        score += 3
    if "FRAGMENT_MATCH_AT_FULL_TRIP_GRAIN" in reasons:
        score += 3
    if "VISIBLE_EVIDENCE_NON_DISCRIMINATIVE" in reasons:
        score += 2
    if "ADJACENT_CONTEXT_USED" in reasons:
        score += 2
    if "REVIEWER_TIMEZONE_TEXT_INCONSISTENT" in reasons:
        score += 1
    return score


def readiness(
    label: FirstPassLabel,
    routing: RoutingSuggestion,
    key: CandidateKeyResolution,
    reason_codes: Sequence[str],
    *,
    blind_second_pass_completed: bool = False,
) -> ReadinessResult:
    """Return nonexclusive task readiness plus the strict calibration gate."""

    classes: set[str] = {"pending_adjudication"}
    grain = routing.case_grain_suggestion
    if grain == "single_trip_candidate":
        classes.add("blind_second_pass_required")
    elif grain == "multi_trip_chunk":
        classes.add("segmentation_label_needed")
    elif grain == "boundary_spanning":
        classes.add("boundary_label_needed")
    elif grain == "local_fragment":
        classes.add("fragment_label_needed")
        if routing.reviewability_suggestion == "insufficient_context":
            classes.add("not_evaluable_from_current_packet")
    elif grain == "data_quality_invalid":
        classes.update({"quality_label_needed", "not_evaluable_from_current_packet"})
    elif grain == "turnaround_or_idle":
        classes.add("no_match_activity_review_needed")

    matched = label.raw_primary_label.startswith("MATCH_")
    key_ready = key.resolved
    if matched and not key.resolved:
        classes.add("candidate_key_resolution_required")

    gate = {
        "single_trip_candidate": grain == "single_trip_candidate",
        "reviewable_for_schedule_match": (
            routing.reviewability_suggestion == "reviewable_for_schedule_match"
        ),
        "full_candidate_key_resolved": key_ready,
        "no_unresolved_notes_label_conflict": (
            "NOTES_PRIMARY_LABEL_CONFLICT" not in reason_codes
        ),
        "rationale_present": rationale_present(label),
        "blind_second_pass_completed": blind_second_pass_completed,
    }
    pilot_checks = {key_name: value for key_name, value in gate.items()}
    pilot_checks.pop("blind_second_pass_completed")
    if matched and all(pilot_checks.values()):
        classes.add("schedule_match_pilot_ready")
    abstention_checks = {
        "single_trip_candidate": grain == "single_trip_candidate",
        "reviewable_for_schedule_match": (
            routing.reviewability_suggestion == "reviewable_for_schedule_match"
        ),
        "no_unresolved_notes_label_conflict": (
            "NOTES_PRIMARY_LABEL_CONFLICT" not in reason_codes
        ),
        "rationale_present": rationale_present(label),
    }
    if label.raw_primary_label == "NO_SCHEDULED_MATCH" and all(
        abstention_checks.values()
    ):
        classes.add("schedule_abstention_pilot_ready")
    return ReadinessResult(
        classes=tuple(sorted(classes)),
        strict_gate_checks=gate,
        calibration_ready=all(gate.values()),
    )


def recommended_second_pass_questions(
    routing: RoutingSuggestion,
    reason_codes: Sequence[str],
) -> tuple[str, ...]:
    grain = routing.case_grain_suggestion
    questions: list[str]
    if grain == "multi_trip_chunk":
        questions = [
            "How many operational subtrips are present?",
            "Where are the boundary intervals relative to the parent chunk?",
            "What complete candidate key, if any, applies to each child segment?",
        ]
    elif grain == "boundary_spanning":
        questions = [
            "What are the inbound and outbound candidate roles?",
            "What boundary stop and relative-time interval are supported?",
            "Does the record require dual ownership rather than one natural owner?",
        ]
    elif grain == "local_fragment":
        questions = [
            "What is the fragment role?",
            "Should it stitch to the previous or next object?",
            "Is adjacent context sufficient before asking for a scheduled match?",
        ]
    elif grain == "data_quality_invalid":
        questions = [
            "Which source/time quality state applies?",
            "Can the simultaneous-position or fold conflict be resolved without guessing?",
        ]
    elif grain == "turnaround_or_idle":
        questions = [
            "Is this turnaround, layover, idling, or other non-revenue activity?",
            "Should scheduled abstention be retained as a separate dimension?",
        ]
    else:
        questions = [
            "Does the object represent exactly one operational trip?",
            "Which complete five-position scheduled candidate key is supported?",
            "Which visible evidence supports the decision?",
        ]
    if "VISIBLE_EVIDENCE_NON_DISCRIMINATIVE" in reason_codes:
        questions.append("Which non-tabular evidence separates the tied visible cards?")
    if "LOW_CONFIDENCE_MATCH" in reason_codes:
        questions.append(
            "Would abstention be more appropriate under the revised schema?"
        )
    return tuple(questions)


def duration_bucket(duration_s: int | None) -> str:
    if duration_s is None:
        return "unknown"
    if duration_s < 600:
        return "<10m"
    if duration_s < 1800:
        return "10-<30m"
    if duration_s < 3600:
        return "30-<60m"
    return ">=60m"


def candidate_count_summary(values: Sequence[int]) -> dict[str, float | int | None]:
    if not values:
        return {"min": None, "median": None, "max": None}
    return {"min": min(values), "median": median(values), "max": max(values)}


def _unresolved(raw: str, status: str, source: str) -> CandidateKeyResolution:
    parts = raw.split("|") + [""] * 5
    return CandidateKeyResolution(
        status=status,
        serialized_key=raw,
        service_date=parts[0] or None,
        feed_id=parts[1] or None,
        route_mapping_id=parts[2] or None,
        scheduled_trip_id=parts[3] or None,
        polarity_hypothesis_id=parts[4] or None,
        explicit_null_polarity=len(raw.split("|")) == 5 and parts[4] == "",
        resolved_from=source,
    )


def _signed_advantage(
    selected: float | int | None, others: Sequence[float | int | None]
) -> float | None:
    valid = [value for value in others if value is not None]
    if selected is None or not valid:
        return None
    return float(selected) - float(max(valid))


def _best_advantage(values: Sequence[float | int | None]) -> float | None:
    valid = sorted(
        (float(value) for value in values if value is not None), reverse=True
    )
    if len(valid) < 2:
        return None
    return valid[0] - valid[1]


def _same_visible_pair(left: SummaryCandidate, right: SummaryCandidate) -> bool:
    return (
        left.overlap_s is not None
        and left.coverage_pct is not None
        and left.overlap_s == right.overlap_s
        and left.coverage_pct == right.coverage_pct
    )


def _near_visible_pair(left: SummaryCandidate, right: SummaryCandidate) -> bool:
    if (
        left.overlap_s is None
        or left.coverage_pct is None
        or right.overlap_s is None
        or right.coverage_pct is None
    ):
        return False
    return (
        abs(left.overlap_s - right.overlap_s) <= NEAR_OVERLAP_TOLERANCE_S
        and abs(left.coverage_pct - right.coverage_pct)
        <= NEAR_COVERAGE_TOLERANCE_PCT_POINTS
    )


def _residual_advantage(
    selected: SummaryCandidate | None,
    others: Sequence[SummaryCandidate],
    summary: ParsedSummary,
    *,
    boundary: str,
) -> float | None:
    window_value = summary.window_start if boundary == "start" else summary.window_end
    if selected is None or window_value is None:
        return None
    selected_value = (
        selected.scheduled_start if boundary == "start" else selected.scheduled_end
    )
    if selected_value is None:
        return None
    other_values = [
        candidate.scheduled_start if boundary == "start" else candidate.scheduled_end
        for candidate in others
    ]
    residuals = [
        abs((value - window_value).total_seconds())
        for value in other_values
        if value is not None
    ]
    if not residuals:
        return None
    selected_residual = abs((selected_value - window_value).total_seconds())
    return min(residuals) - selected_residual


def _int_or_none(value: float | None) -> int | None:
    return int(value) if value is not None else None
