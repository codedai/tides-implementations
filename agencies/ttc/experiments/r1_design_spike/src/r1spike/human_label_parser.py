# ruff: noqa: E501
"""Lossless parsers for the R1 Spike 02R human-label audit.

This module deliberately stops at parsing.  It does not adjudicate a label,
score a model, or translate an audit suggestion into a replacement judgment.
The earliest append-only ``save`` event is the first-pass source of truth;
the current CSV is used as a cross-check and a fallback only.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_CASE_RE = re.compile(r"^#\s+case\s+(?P<case_id>C-[A-Za-z0-9_-]+)\s*$")
_META_RE = re.compile(
    r"^-\s+stratum:\s*(?P<stratum>.*?)\s+·\s+kind:\s*(?P<kind>.*?)\s*$"
)
_SUMMARY_RE = re.compile(r"^-\s+summary:\s*(?P<summary>.*)$")
_WINDOW_RE = re.compile(
    r"^-\s+window:\s*(?P<start>n/a|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"\s*→\s*(?P<end>n/a|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$"
)
_CANDIDATE_RE = re.compile(
    r"^-\s+(?P<card>[A-Z]):\s+route\s+(?P<route>\S+)\s+"
    r"(?P<start>n/a|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"→(?P<end>n/a|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"overlap\s+(?P<overlap>n/a|[0-9hms]+)\s+"
    r"covered\s+(?P<coverage>n/a|[0-9]+(?:\.[0-9]+)?%)\s+"
    r"tags=(?P<tags>.*)$"
)
_MATCHED_RE = re.compile(
    r"^-\s+matched\s+card\s+(?P<card>[A-Z])\s+"
    r"\(confidence\s+(?P<confidence>[1-5])\)\s*$",
    re.IGNORECASE,
)
_NONMATCH_RE = re.compile(
    r"^-\s+(?P<label>ambiguous|insufficient evidence|no scheduled match)\s+"
    r"\(confidence\s+(?P<confidence>[1-5])\)\s*$",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(r"\bchunk\s+of\s+(?P<seconds>\d+)s\b", re.IGNORECASE)


class HumanLabelParseError(ValueError):
    """An input cannot be parsed without guessing at a human field."""


def sha256_file(path: Path) -> str:
    """Return a real content SHA-256 without changing the file."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_duration_s(value: str | None) -> int | None:
    """Parse the compact duration format emitted by the labeling UI."""

    if not value or value == "n/a":
        return None
    match = re.fullmatch(r"(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?", value)
    if match is None or not any(match.groupdict().values()):
        raise HumanLabelParseError(f"unrecognized duration {value!r}")
    return (
        int(match.group("h") or 0) * 3600
        + int(match.group("m") or 0) * 60
        + int(match.group("s") or 0)
    )


def parse_datetime(value: str) -> datetime | None:
    """Parse a displayed naive wall time, preserving its lack of timezone."""

    if value == "n/a":
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class PacketCase:
    case_id: str
    original_stratum: str
    original_kind: str
    original_n_events: int | None
    original_span_rel_s: int | None
    original_summary: str
    source_row_number: int

    @property
    def chunk_duration_s(self) -> int | None:
        match = _DURATION_RE.search(self.original_summary or "")
        return int(match.group("seconds")) if match else None


@dataclass(frozen=True)
class FirstPassLabel:
    case_id: str
    reviewer_id: str
    raw_primary_label: str
    raw_selected_card: str | None
    raw_selected_candidate_id: str | None
    raw_confidence: int | None
    raw_fragment_impression: str | None
    raw_rationale: tuple[str, ...]
    raw_notes: str
    diagnostics_revealed_before_save: bool | None
    review_timestamp: str | None
    created_at: str | None
    source_kind: str
    source_line_number: int | None
    current_export_matches_first_pass: bool | None = None
    diagnostics_reveal_source: str = "unavailable"
    raw_diagnostics_revealed_before_save: bool | None = None
    history_diagnostics_revealed_before_save: bool | None = None
    diagnostics_reveal_consistency_status: str = "unavailable"


@dataclass(frozen=True)
class SummaryCandidate:
    card: str
    route: str
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    overlap_s: int | None
    coverage_pct: float | None
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_visible_metrics(self) -> bool:
        return self.overlap_s is not None or self.coverage_pct is not None


@dataclass(frozen=True)
class ParsedSummary:
    case_id: str
    stratum: str | None
    kind: str | None
    displayed_summary: str | None
    window_start: datetime | None
    window_end: datetime | None
    candidates: tuple[SummaryCandidate, ...]
    displayed_label: str | None
    displayed_selected_card: str | None
    displayed_confidence: int | None
    displayed_notes: str
    source_file: Path
    source_sha256: str

    @property
    def chunk_duration_s(self) -> int | None:
        if self.displayed_summary:
            match = _DURATION_RE.search(self.displayed_summary)
            if match:
                return int(match.group("seconds"))
        # The displayed window is naive local wall text.  It is only a fallback:
        # subtracting it across a DST fold silently loses an elapsed hour.
        if self.window_start is not None and self.window_end is not None:
            return int((self.window_end - self.window_start).total_seconds())
        return None


def read_packet(path: Path) -> list[PacketCase]:
    """Read the original redacted packet in its original row order."""

    rows: list[PacketCase] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"case_id", "stratum", "kind", "n_events", "span_rel_s", "summary"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise HumanLabelParseError(
                f"packet {path} is missing columns: {sorted(missing)}"
            )
        for row_number, row in enumerate(reader, start=2):
            rows.append(
                PacketCase(
                    case_id=row["case_id"],
                    original_stratum=row["stratum"],
                    original_kind=row["kind"],
                    original_n_events=_int_or_none(row["n_events"]),
                    original_span_rel_s=_int_or_none(row["span_rel_s"]),
                    original_summary=row["summary"],
                    source_row_number=row_number,
                )
            )
    ids = [row.case_id for row in rows]
    if len(ids) != len(set(ids)):
        raise HumanLabelParseError("packet contains duplicate case_id values")
    return rows


def discover_summary_files(path: Path) -> list[Path]:
    """Discover every summary deterministically, excluding environment caches."""

    candidates = [path] if path.is_file() else list(path.rglob("C-*_summary*.md"))
    return sorted(
        p.resolve()
        for p in candidates
        if p.is_file() and not {".venv", ".git", "__pycache__"}.intersection(p.parts)
    )


def parse_summary(path: Path) -> ParsedSummary:
    """Parse every currently observed Markdown summary label format."""

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines:
        raise HumanLabelParseError(f"empty summary: {path}")
    case_match = _CASE_RE.match(lines[0])
    if case_match is None:
        raise HumanLabelParseError(f"summary has no case heading: {path}")

    case_id = case_match.group("case_id")
    stratum = kind = displayed_summary = None
    window_start = window_end = None
    candidates: list[SummaryCandidate] = []
    displayed_label = displayed_card = None
    displayed_confidence = None
    displayed_notes = ""

    for line in lines[1:]:
        if match := _META_RE.match(line):
            stratum = match.group("stratum")
            kind = match.group("kind")
        elif match := _SUMMARY_RE.match(line):
            displayed_summary = match.group("summary")
        elif match := _WINDOW_RE.match(line):
            window_start = parse_datetime(match.group("start"))
            window_end = parse_datetime(match.group("end"))
        elif match := _CANDIDATE_RE.match(line):
            tags_raw = match.group("tags")
            try:
                parsed_tags = ast.literal_eval(tags_raw)
            except (SyntaxError, ValueError) as exc:
                raise HumanLabelParseError(
                    f"invalid candidate tags in {path}: {tags_raw!r}"
                ) from exc
            if not isinstance(parsed_tags, list) or not all(
                isinstance(tag, str) for tag in parsed_tags
            ):
                raise HumanLabelParseError(
                    f"candidate tags must be a list of strings in {path}"
                )
            coverage = match.group("coverage")
            candidates.append(
                SummaryCandidate(
                    card=match.group("card"),
                    route=match.group("route"),
                    scheduled_start=parse_datetime(match.group("start")),
                    scheduled_end=parse_datetime(match.group("end")),
                    overlap_s=parse_duration_s(match.group("overlap")),
                    coverage_pct=(
                        float(coverage.removesuffix("%")) if coverage != "n/a" else None
                    ),
                    tags=tuple(parsed_tags),
                )
            )
        elif match := _MATCHED_RE.match(line):
            displayed_label = "matched"
            displayed_card = match.group("card")
            displayed_confidence = int(match.group("confidence"))
        elif match := _NONMATCH_RE.match(line):
            displayed_label = match.group("label").lower()
            displayed_confidence = int(match.group("confidence"))
        elif line.startswith("- notes:"):
            # Preserve every character following the single UI separator space.
            displayed_notes = line[len("- notes:") :]
            if displayed_notes.startswith(" "):
                displayed_notes = displayed_notes[1:]

    if not candidates:
        raise HumanLabelParseError(f"summary has no candidate rows: {path}")
    cards = [candidate.card for candidate in candidates]
    if cards != sorted(cards) or len(cards) != len(set(cards)):
        raise HumanLabelParseError(
            f"candidate cards are duplicated or unordered: {path}"
        )
    return ParsedSummary(
        case_id=case_id,
        stratum=stratum,
        kind=kind,
        displayed_summary=displayed_summary,
        window_start=window_start,
        window_end=window_end,
        candidates=tuple(candidates),
        displayed_label=displayed_label,
        displayed_selected_card=displayed_card,
        displayed_confidence=displayed_confidence,
        displayed_notes=displayed_notes,
        source_file=path.resolve(),
        source_sha256=sha256_file(path),
    )


def load_summaries(path: Path) -> dict[str, ParsedSummary]:
    """Load all discovered summaries and fail on duplicate case summaries."""

    parsed: dict[str, ParsedSummary] = {}
    for summary_path in discover_summary_files(path):
        summary = parse_summary(summary_path)
        if summary.case_id in parsed:
            raise HumanLabelParseError(
                f"multiple summaries discovered for {summary.case_id}: "
                f"{parsed[summary.case_id].source_file}, {summary_path}"
            )
        parsed[summary.case_id] = summary
    return parsed


def read_current_export(path: Path | None) -> dict[tuple[str, str], dict[str, Any]]:
    """Read the current upsert CSV without normalizing human text."""

    if path is None or not path.exists():
        return {}
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for line_number, row in enumerate(csv.DictReader(handle), start=2):
            case_id = row.get("case_id", "")
            reviewer = row.get("reviewer", "")
            if not case_id:
                continue
            row["_line_number"] = line_number
            rows[(case_id, reviewer)] = row
    return rows


def read_review_history(path: Path | None) -> list[dict[str, Any]]:
    """Read the append-only history in physical line order."""

    if path is None or not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HumanLabelParseError(
                    f"invalid JSONL event at {path}:{line_number}"
                ) from exc
            if not isinstance(event, dict):
                raise HumanLabelParseError(
                    f"review event at {path}:{line_number} is not an object"
                )
            event["_line_number"] = line_number
            events.append(event)
    return events


def load_first_pass_labels(
    current_export_path: Path | None,
    review_history_path: Path | None,
) -> list[FirstPassLabel]:
    """Recover the earliest saved human judgment per case/reviewer.

    A resave is not a second pass.  Reveal events are not labels.  When an
    append-only history is absent, the current CSV is used and the source is
    explicitly marked as a fallback.
    """

    current = read_current_export(current_export_path)
    history = read_review_history(review_history_path)
    first_events: dict[tuple[str, str], dict[str, Any]] = {}
    revealed: set[tuple[str, str]] = set()
    for event in history:
        key = (str(event.get("case_id", "")), str(event.get("reviewer", "")))
        if "reveal" in str(event.get("event", "")).lower():
            if key[0]:
                revealed.add(key)
            continue
        if event.get("event") != "save":
            continue
        if not key[0]:
            continue
        if key not in first_events:
            event = dict(event)
            event["_history_revealed_before_save"] = key in revealed
            first_events[key] = event

    keys = sorted(set(current) | set(first_events))
    out: list[FirstPassLabel] = []
    for key in keys:
        source = first_events.get(key)
        source_kind = "append_only_first_save"
        if source is None:
            source = current[key]
            source_kind = "current_export_fallback"
        current_row = current.get(key)
        matches = None
        if current_row is not None:
            matches = _same_human_payload(source, current_row)
        rationale = source.get("rationale", [])
        if isinstance(rationale, str):
            rationale_values = tuple(item for item in rationale.split("|") if item)
        elif isinstance(rationale, list):
            rationale_values = tuple(str(item) for item in rationale)
        else:
            rationale_values = ()
        raw_diagnostics = _bool_or_none(source.get("diagnostics_revealed_before_save"))
        history_diagnostics = (
            bool(source.get("_history_revealed_before_save", False))
            if source_kind == "append_only_first_save"
            else None
        )
        if raw_diagnostics is None and history_diagnostics is None:
            diagnostics = None
            diagnostics_source = "unavailable"
            diagnostics_consistency = "unavailable"
        elif raw_diagnostics is None:
            diagnostics = history_diagnostics
            diagnostics_source = "append_only_event_chronology"
            diagnostics_consistency = "saved_field_unavailable"
        elif history_diagnostics is None:
            diagnostics = raw_diagnostics
            diagnostics_source = "saved_field"
            diagnostics_consistency = "chronology_unavailable"
        elif raw_diagnostics == history_diagnostics:
            diagnostics = raw_diagnostics
            diagnostics_source = "saved_field_and_event_chronology"
            diagnostics_consistency = "agree"
        else:
            # Preserve both observations and conservatively record exposure if
            # either source says it occurred.  The conflict is surfaced to the
            # audit instead of silently preferring one source.
            diagnostics = raw_diagnostics or history_diagnostics
            diagnostics_source = "saved_field_event_chronology_conflict"
            diagnostics_consistency = "conflict"
        out.append(
            FirstPassLabel(
                case_id=key[0],
                reviewer_id=key[1],
                raw_primary_label=str(source.get("label", "")),
                raw_selected_card=_str_or_none(source.get("selected_candidate_letter")),
                raw_selected_candidate_id=_str_or_none(
                    source.get("selected_candidate_id")
                ),
                raw_confidence=_int_or_none(source.get("confidence")),
                raw_fragment_impression=_str_or_none(source.get("fragment_impression")),
                raw_rationale=rationale_values,
                raw_notes=str(source.get("notes", "")),
                diagnostics_revealed_before_save=diagnostics,
                review_timestamp=_str_or_none(
                    source.get("event_at") or source.get("modified_at")
                ),
                created_at=_str_or_none(source.get("created_at")),
                source_kind=source_kind,
                source_line_number=_int_or_none(source.get("_line_number")),
                current_export_matches_first_pass=matches,
                diagnostics_reveal_source=diagnostics_source,
                raw_diagnostics_revealed_before_save=raw_diagnostics,
                history_diagnostics_revealed_before_save=history_diagnostics,
                diagnostics_reveal_consistency_status=diagnostics_consistency,
            )
        )
    return out


def neutral_primary_label(raw_label: str) -> str:
    """Expose the human-facing judgment without erasing the raw UI value."""

    mapping = {
        "MATCH_TOP_CANDIDATE": "matched",
        "MATCH_OTHER_CANDIDATE": "matched",
        "NO_SCHEDULED_MATCH": "no scheduled match",
        "AMBIGUOUS": "ambiguous",
        "INSUFFICIENT_EVIDENCE": "insufficient evidence",
    }
    return mapping.get(raw_label, raw_label.strip().lower().replace("_", " "))


def portable_path(path: Path, roots: Iterable[Path]) -> str:
    """Represent a source path without baking a workstation prefix into output."""

    resolved = path.resolve()
    for root in roots:
        try:
            return resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
    return resolved.name


def _same_human_payload(event: dict[str, Any], current: dict[str, Any]) -> bool:
    fields = (
        "label",
        "selected_candidate_letter",
        "selected_candidate_id",
        "confidence",
        "fragment_impression",
        "notes",
        "diagnostics_revealed_before_save",
    )
    for field_name in fields:
        left = event.get(field_name)
        right = current.get(field_name)
        if field_name == "confidence":
            if _int_or_none(left) != _int_or_none(right):
                return False
        elif field_name == "diagnostics_revealed_before_save":
            if _bool_or_none(left) != _bool_or_none(right):
                return False
        elif (left or "") != (right or ""):
            return False
    left_rationale = event.get("rationale") or []
    if isinstance(left_rationale, str):
        left_rationale = [v for v in left_rationale.split("|") if v]
    right_rationale = [v for v in (current.get("rationale") or "").split("|") if v]
    return list(left_rationale) == right_rationale


def _str_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return None
