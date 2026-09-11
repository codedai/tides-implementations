# ruff: noqa: E501
"""Deterministic audit flags and task-routing suggestions.

The functions in this module produce *suggestions*.  They never write a final
label and never claim that a keyword proves the underlying operational state.
Every result carries a visible evidence basis and a conservative strength.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .human_label_parser import FirstPassLabel, PacketCase, ParsedSummary

LOCAL_TZ = ZoneInfo("America/Toronto")

CASE_GRAINS = {
    "single_trip_candidate",
    "multi_trip_chunk",
    "local_fragment",
    "boundary_spanning",
    "turnaround_or_idle",
    "data_quality_invalid",
    "coverage_censored",
    "unknown",
}
REVIEWABILITY = {
    "reviewable_for_schedule_match",
    "not_reviewable_at_current_grain",
    "insufficient_context",
    "invalid_source_state",
    "requires_boundary_adjudication",
    "requires_segmentation_adjudication",
}
TASK_ROUTES = {
    "scheduled_trip_second_pass",
    "scheduled_trip_abstention",
    "segmentation_multi_trip",
    "boundary_spanning_dual_role",
    "fragment_role_and_stitching",
    "source_time_quality",
    "no_match_activity",
    "insufficient_context",
    "manual_schema_review",
}


_MULTI_RE = re.compile(
    r"\b(?:two|three|multiple)\s+(?:avl\s+)?trips?\b|"
    r"\bshould\s+be\s+split(?:\s+into\s+(?:two|three))?\b|"
    r"\bbelongs?\s+to\s+(?:two|three)\s+cand|"
    r"\bbelongs?\s+to\s+(?:candidates?\s*)?[A-E]\s*"
    r"(?:,|and)\s*(?:[A-E]\s*(?:,|and)\s*)?[A-E]\b|"
    r"\b[A-E]\s*,\s*[A-E]\s*,?\s+and\s+[A-E]\b",
    re.IGNORECASE,
)
_BOUNDARY_RE = re.compile(
    r"\b(?:apc\s+(?:event|interval|record|row)|one\s+apc)\b.*"
    r"\b(?:two\s+(?:adjacent\s+)?trips|split|splition|boundary)\b|"
    r"\b(?:apc\s+(?:event|interval|record|row)|one\s+apc)\b.*"
    r"\b(?:spans?|crosses?)\b.*\btrips?\b|"
    r"\b(?:inbound\s+and\s+outbound|inbound/outbound|dual[- ]role)\b|"
    r"\b(?:split|splition|boundary)\b.*"
    r"\b(?:terminal\s+stop|stop\s+\d+|direction\s+transition)\b",
    re.IGNORECASE,
)
_QUALITY_RE = re.compile(
    r"\b(?:two\s+(?:spots|locations)|simultaneous\s+(?:spots|locations)|"
    r"spatial[ -]?temporal\s+conflict|spatial\s+temporal\s+confliction|"
    r"impossible\s+movement|unresolved\s+(?:dst|fold)|malformed\s+trajectory|"
    r"vehicle\s+appears\s+at\s+two\s+spots|messy.*not\s+being\s+split)\b",
    re.IGNORECASE,
)
_IDLE_RE = re.compile(
    r"\b(?:idling|idle|layover|without\s+moving|non[- ]revenue)\b",
    re.IGNORECASE,
)
_EXPLICIT_FRAGMENT_RE = re.compile(
    r"\b(?:local\s+)?fragment(?:-like)?\b", re.IGNORECASE
)
_PREV_NEXT_RE = re.compile(
    r"\b(?:previous|prior|preceding|next|following|adjacent)\s+"
    r"(?:chunk|trip|segment|context)\b",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    r"(?<!\d)(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)"
    r"(?::(?P<second>[0-5]\d))?(?:\s*(?P<zone>EDT|EST|UTC|[A-Z]{3,4}))?\b"
)
_STOP_RE = re.compile(r"\bstop\s+(?P<stop>\d+)\b", re.IGNORECASE)
_CARD_RE = re.compile(r"\bcandi(?:date|s)?\s*([A-E])\b", re.IGNORECASE)


@dataclass(frozen=True)
class NoteTimeReference:
    text: str
    resolved_local: datetime | None
    timezone_text: str | None
    expected_timezone_text: str | None
    timezone_consistent: bool | None
    relation_to_window: str
    offset_from_window_s: int | None
    offset_from_window_start_s: int | None
    offset_from_window_end_s: int | None


@dataclass(frozen=True)
class RoutingSuggestion:
    case_grain_suggestion: str
    reviewability_suggestion: str
    task_routing_suggestion: str
    fragment_role_suggestion: str | None
    audit_reason_codes: tuple[str, ...]
    evidence_basis: tuple[str, ...]
    suggestion_strength: str
    note_time_references: tuple[NoteTimeReference, ...]
    boundary_stop: str | None
    referenced_cards: tuple[str, ...]


def has_multi_trip_language(notes: str) -> bool:
    value = notes or ""
    if _MULTI_RE.search(value):
        return True
    return (
        len(tuple(_TIME_RE.finditer(value))) >= 2
        and re.search(
            r"\b(?:splits?|turns?|transitions?|boundaries|boundary)\b",
            value,
            re.IGNORECASE,
        )
        is not None
    )


def has_boundary_spanning_language(notes: str) -> bool:
    return bool(_BOUNDARY_RE.search(notes or ""))


def has_quality_failure_language(notes: str) -> bool:
    return bool(_QUALITY_RE.search(notes or ""))


def has_idle_language(notes: str) -> bool:
    return bool(_IDLE_RE.search(notes or ""))


def is_fragment_like(packet: PacketCase, summary: ParsedSummary) -> bool:
    """Use an explicit packet stratum or an explicit displayed tag.

    This is presentation evidence, not proof that the object is an independent
    performed trip or that any selected scheduled candidate is incorrect.  Low
    stop coverage alone is intentionally insufficient: a long idle/no-revenue
    object can have equally low coverage without being a local fragment.
    """

    if packet.original_stratum == "fragment":
        return True
    visible = [
        candidate for candidate in summary.candidates if candidate.has_visible_metrics
    ]
    return any(
        "fragment-like" in tag.lower()
        for candidate in visible
        for tag in candidate.tags
    )


def extract_note_time_references(
    notes: str,
    window_start: datetime | None,
    window_end: datetime | None,
) -> tuple[NoteTimeReference, ...]:
    """Resolve note clock text to the nearest local date around the window."""

    references: list[NoteTimeReference] = []
    for match in _TIME_RE.finditer(notes or ""):
        zone_text = match.group("zone")
        resolved = None
        expected = None
        consistent = None
        relation = "window_unavailable"
        offset = None
        offset_from_start = None
        offset_from_end = None
        if window_start is not None and window_end is not None:
            midpoint = window_start + (window_end - window_start) / 2
            clock = {
                "hour": int(match.group("hour")),
                "minute": int(match.group("minute")),
                "second": int(match.group("second") or 0),
            }
            dates = {
                (window_start + timedelta(days=delta)).date() for delta in (-1, 0, 1)
            } | {(window_end + timedelta(days=delta)).date() for delta in (-1, 0, 1)}
            candidates = [
                datetime.combine(date_value, datetime.min.time()).replace(**clock)
                for date_value in sorted(dates)
            ]
            resolved = min(candidates, key=lambda value: abs(value - midpoint))
            offset_from_start = int((resolved - window_start).total_seconds())
            offset_from_end = int((resolved - window_end).total_seconds())
            if resolved < window_start:
                relation = "before_current_window"
                offset = -int((window_start - resolved).total_seconds())
            elif resolved > window_end:
                relation = "after_current_window"
                offset = int((resolved - window_end).total_seconds())
            else:
                relation = "inside_current_window"
                offset = 0
            if zone_text in {"EDT", "EST"}:
                valid_names = set()
                for fold in (0, 1):
                    aware = resolved.replace(tzinfo=LOCAL_TZ, fold=fold)
                    roundtrip = aware.astimezone(UTC).astimezone(LOCAL_TZ)
                    if roundtrip.replace(tzinfo=None) == resolved:
                        valid_names.add(str(aware.tzname()))
                expected = "/".join(sorted(valid_names)) or None
                consistent = zone_text in valid_names
        references.append(
            NoteTimeReference(
                text=match.group(0),
                resolved_local=resolved,
                timezone_text=zone_text,
                expected_timezone_text=expected,
                timezone_consistent=consistent,
                relation_to_window=relation,
                offset_from_window_s=offset,
                offset_from_window_start_s=offset_from_start,
                offset_from_window_end_s=offset_from_end,
            )
        )
    return tuple(references)


def suggest_routing(
    packet: PacketCase,
    label: FirstPassLabel,
    summary: ParsedSummary,
) -> RoutingSuggestion:
    """Apply conservative, documented routing precedence to one case."""

    notes = label.raw_notes
    reasons: set[str] = set()
    basis: list[str] = []
    boundary = has_boundary_spanning_language(notes)
    multi = has_multi_trip_language(notes)
    quality = has_quality_failure_language(notes)
    idle = has_idle_language(notes)
    fragment = is_fragment_like(packet, summary)

    if quality:
        grain = "data_quality_invalid"
        reviewability = "invalid_source_state"
        route = "source_time_quality"
        reasons.add("DATA_QUALITY_OR_TIME_RESOLUTION_FAILURE")
        basis.append("explicit first-pass note describes a source/time conflict")
        strength = "explicit_note"
    elif boundary:
        grain = "boundary_spanning"
        reviewability = "requires_boundary_adjudication"
        route = "boundary_spanning_dual_role"
        reasons.add("BOUNDARY_SPANNING_DUAL_ROLE")
        basis.append(
            "explicit first-pass note describes one record with two trip roles"
        )
        strength = "explicit_note"
    elif multi:
        grain = "multi_trip_chunk"
        reviewability = "requires_segmentation_adjudication"
        route = "segmentation_multi_trip"
        reasons.add("MULTI_TRIP_OR_SEQUENCE_CASE")
        basis.append("explicit first-pass note describes multiple trips or a split")
        strength = "explicit_note"
    elif idle:
        grain = "turnaround_or_idle"
        reviewability = "not_reviewable_at_current_grain"
        route = "no_match_activity"
        reasons.add("TURNAROUND_IDLE_OR_NON_REVENUE_ACTIVITY")
        basis.append("explicit first-pass note describes idling or no movement")
        strength = "explicit_note"
    elif fragment:
        grain = "local_fragment"
        reviewability = (
            "insufficient_context"
            if "insufficient_context" in label.raw_rationale
            else "not_reviewable_at_current_grain"
        )
        route = (
            "insufficient_context"
            if reviewability == "insufficient_context"
            else "fragment_role_and_stitching"
        )
        reasons.add("LOCAL_FRAGMENT_NOT_FULL_TRIP")
        basis.append(
            "packet stratum or explicit displayed tag describes a local fragment"
        )
        strength = "presentation_metadata"
    else:
        grain = "single_trip_candidate"
        reviewability = "reviewable_for_schedule_match"
        route = (
            "scheduled_trip_abstention"
            if label.raw_primary_label == "NO_SCHEDULED_MATCH"
            else "scheduled_trip_second_pass"
        )
        basis.append("no explicit grain exception found in first-pass fields")
        strength = "absence_of_exception"

    if fragment:
        reasons.add("LOCAL_FRAGMENT_NOT_FULL_TRIP")

    references = extract_note_time_references(
        notes, summary.window_start, summary.window_end
    )
    if _PREV_NEXT_RE.search(notes) or any(
        reference.relation_to_window
        in {"before_current_window", "after_current_window"}
        for reference in references
    ):
        reasons.add("ADJACENT_CONTEXT_USED")
    if any(reference.timezone_consistent is False for reference in references):
        reasons.add("REVIEWER_TIMEZONE_TEXT_INCONSISTENT")
    if any(
        reference.timezone_text is not None and reference.timezone_consistent is None
        for reference in references
    ):
        reasons.add("REVIEWER_TIMEZONE_TEXT_UNVERIFIABLE")

    matched = label.raw_primary_label.startswith("MATCH_")
    if fragment and matched:
        reasons.add("FRAGMENT_MATCH_AT_FULL_TRIP_GRAIN")

    if grain in {"boundary_spanning", "multi_trip_chunk"} and matched:
        reasons.add("NOTES_PRIMARY_LABEL_CONFLICT")
    elif grain == "data_quality_invalid" and label.raw_primary_label == "AMBIGUOUS":
        reasons.add("NOTES_PRIMARY_LABEL_CONFLICT")
    elif fragment and matched and _EXPLICIT_FRAGMENT_RE.search(notes):
        reasons.add("NOTES_PRIMARY_LABEL_CONFLICT")
    if grain == "boundary_spanning" and not matched:
        reasons.add("PRIMARY_LABEL_TASK_GRAIN_MISMATCH")
    elif grain == "multi_trip_chunk":
        reasons.add("PRIMARY_LABEL_TASK_GRAIN_MISMATCH")
    elif grain in {"data_quality_invalid", "turnaround_or_idle"}:
        reasons.add("PRIMARY_LABEL_TASK_GRAIN_MISMATCH")

    stop_match = _STOP_RE.search(notes)
    card_values = {match.upper() for match in _CARD_RE.findall(notes)}
    if multi or boundary:
        # Uppercase singletons are display cards in the observed notes.  Lowercase
        # b-e are also accepted after multi-trip language; lowercase ``a`` is
        # deliberately excluded because it is usually an article.
        card_values.update(re.findall(r"\b[A-E]\b", notes))
        card_values.update(match.upper() for match in re.findall(r"\b[b-e]\b", notes))
    cards = tuple(sorted(card_values))
    fragment_role = _fragment_role(notes, label.raw_fragment_impression, grain)

    assert grain in CASE_GRAINS
    assert reviewability in REVIEWABILITY
    assert route in TASK_ROUTES
    return RoutingSuggestion(
        case_grain_suggestion=grain,
        reviewability_suggestion=reviewability,
        task_routing_suggestion=route,
        fragment_role_suggestion=fragment_role,
        audit_reason_codes=tuple(sorted(reasons)),
        evidence_basis=tuple(basis),
        suggestion_strength=strength,
        note_time_references=references,
        boundary_stop=stop_match.group("stop") if stop_match else None,
        referenced_cards=cards,
    )


def explicit_reviewer_heuristics(notes: str) -> tuple[str, ...]:
    """Extract only reasoning the reviewer actually wrote down."""

    lowered = (notes or "").lower()
    heuristics: set[str] = set()
    if "on time departure" in lowered or "closest starting time" in lowered:
        heuristics.add("departure_time_priority")
    if re.search(r"\b(?:terminal|arrival|departure|station)\b", lowered):
        heuristics.add("terminal_arrival_departure")
    if "direction" in lowered:
        heuristics.add("direction_transition")
    if re.search(r"\b(?:traj|trajectory|avl trip)\b", lowered):
        heuristics.add("trajectory_continuity")
    if _STOP_RE.search(notes) or "station" in lowered:
        heuristics.add("stop_location")
    if _IDLE_RE.search(notes):
        heuristics.add("idling")
    if re.search(
        r"\b(?:split|splition|shortturn|short turn|two trips|three trips)\b", lowered
    ):
        heuristics.add("split_short_turn_recognition")
    return tuple(sorted(heuristics))


def _fragment_role(notes: str, raw_impression: str | None, grain: str) -> str | None:
    if raw_impression == "turnaround" or has_idle_language(notes):
        return "turnaround"
    if grain not in {"local_fragment", "boundary_spanning"}:
        return None
    if grain == "boundary_spanning":
        # A dual-role parent cannot be reduced to one arrival/departure owner.
        return "ambiguous_fragment"
    lowered = notes.lower()
    if "arrival" in lowered or "inbound" in lowered:
        return "arrival_fragment"
    if "departure" in lowered or "outbound" in lowered:
        return "departure_fragment"
    if "previous" in lowered:
        return "stitch_previous"
    if "next" in lowered:
        return "stitch_next"
    return "ambiguous_fragment"
