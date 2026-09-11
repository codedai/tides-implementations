"""Regression and safety tests for the non-destructive human-label audit."""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq
import pytest
from r1spike.human_label_artifacts import AuditArtifactError
from r1spike.human_label_audit import AuditConfig, run_audit
from r1spike.human_label_parser import (
    FirstPassLabel,
    PacketCase,
    ParsedSummary,
    SummaryCandidate,
    load_first_pass_labels,
    parse_summary,
    sha256_file,
)
from r1spike.label_readiness import (
    consistency_reason_codes,
    priority_score,
    readiness,
    resolve_candidate_key,
    visible_evidence_metrics,
)
from r1spike.label_task_routing import (
    extract_note_time_references,
    has_boundary_spanning_language,
    has_multi_trip_language,
    has_quality_failure_language,
    suggest_routing,
)

TTC_ROOT = Path(__file__).resolve().parents[3]
SPIKE_ROOT = TTC_ROOT / "experiments" / "r1_design_spike"
PACKET = (
    TTC_ROOT
    / "artifacts"
    / "experimental-r1-spike02r-p4"
    / "labeling_packet_redacted.csv"
)
ARTIFACTS = PACKET.parent
SUMMARIES = TTC_ROOT / "tools" / "labeling_results"
LABELS = TTC_ROOT / "tools" / "labeling_ui" / "labels" / "scheduled_trip_labels.csv"
HISTORY = LABELS.with_suffix(".jsonl")
AUDIT_SOURCE_FILES = sorted(
    path
    for path in (SPIKE_ROOT / "src" / "r1spike").glob("*.py")
    if path.name
    in {
        "human_label_artifacts.py",
        "human_label_audit.py",
        "human_label_parser.py",
        "label_insights.py",
        "label_readiness.py",
        "label_task_routing.py",
        "label_visualizations.py",
        "run_human_label_audit.py",
    }
)


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _json_payload(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("_artifact_metadata", None)
    return payload


@pytest.fixture(scope="module")
def audit_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    source_hashes = {path: sha256_file(path) for path in [PACKET, LABELS, HISTORY]}
    source_hashes.update(
        {path: sha256_file(path) for path in sorted(SUMMARIES.glob("C-*_summary*.md"))}
    )
    production_hash = _tree_hash(TTC_ROOT / "src" / "ttc_tides")
    output = tmp_path_factory.mktemp("human-label-audit") / "audit"
    result = run_audit(
        AuditConfig(
            cases_path=PACKET,
            summaries_path=SUMMARIES,
            artifacts_path=ARTIFACTS,
            output_path=output,
        )
    )
    return {
        "output": output,
        "result": result,
        "source_hashes": source_hashes,
        "production_hash": production_hash,
    }


def _packet(*, stratum: str = "route_929") -> PacketCase:
    return PacketCase("C-test", stratum, "scheduled_trip", 1, 0, "case", 2)


def _label(
    notes: str = "",
    *,
    primary: str = "MATCH_TOP",
    confidence: int = 5,
    card: str | None = "A",
    candidate_id: str | None = "2023-11-30|feed|route|trip|",
    rationale: tuple[str, ...] = (),
) -> FirstPassLabel:
    return FirstPassLabel(
        case_id="C-test",
        reviewer_id="reviewer",
        raw_primary_label=primary,
        raw_selected_card=card,
        raw_selected_candidate_id=candidate_id,
        raw_confidence=confidence,
        raw_fragment_impression=None,
        raw_rationale=rationale,
        raw_notes=notes,
        diagnostics_revealed_before_save=False,
        review_timestamp="2023-12-01T00:00:00Z",
        created_at="2023-12-01T00:00:00Z",
        source_kind="test",
        source_line_number=1,
    )


def _summary(
    candidates: tuple[SummaryCandidate, ...] | None = None,
) -> ParsedSummary:
    if candidates is None:
        candidates = (
            SummaryCandidate("A", "29", None, None, 100, 50.0),
            SummaryCandidate("B", "29", None, None, 90, 45.0),
        )
    from datetime import datetime

    return ParsedSummary(
        case_id="C-test",
        stratum="route_929",
        kind="scheduled_trip",
        displayed_summary="test chunk of 600s",
        window_start=datetime(2023, 11, 30, 6, 0),
        window_end=datetime(2023, 11, 30, 7, 0),
        candidates=candidates,
        displayed_label="matched",
        displayed_selected_card="A",
        displayed_confidence=5,
        displayed_notes="",
        source_file=Path("C-test_summary.md"),
        source_sha256="0" * 64,
    )


@pytest.mark.parametrize(
    ("label_line", "expected_label", "expected_card"),
    [
        ("- matched card C (confidence 4)", "matched", "C"),
        ("- ambiguous (confidence 3)", "ambiguous", None),
        ("- insufficient evidence (confidence 2)", "insufficient evidence", None),
        ("- no scheduled match (confidence 5)", "no scheduled match", None),
    ],
)
def test_parse_each_observed_markdown_label_format(
    tmp_path: Path,
    label_line: str,
    expected_label: str,
    expected_card: str | None,
) -> None:
    path = tmp_path / "C-test_summary.md"
    path.write_text(
        "\n".join(
            [
                "# case C-test",
                "",
                "- stratum: route_929 · kind: scheduled_trip",
                "- summary: test chunk of 600s",
                "- window: 2023-11-30 06:00:00 → 2023-11-30 07:00:00",
                "",
                "## candidates",
                "- A: route 29 2023-11-30 06:00:00→"
                "2023-11-30 07:00:00 overlap 10m covered 50% tags=['x']",
                "",
                "## label",
                label_line,
                "- notes:  preserve  both spaces  ",
                "",
            ]
        ),
        encoding="utf-8",
    )
    parsed = parse_summary(path)
    assert parsed.displayed_label == expected_label
    assert parsed.displayed_selected_card == expected_card
    assert parsed.displayed_notes == " preserve  both spaces  "


def test_parse_all_discovered_real_summaries() -> None:
    paths = sorted(SUMMARIES.glob("C-*_summary*.md"))
    parsed = [parse_summary(path) for path in paths]
    assert len(parsed) == 18
    assert len({summary.case_id for summary in parsed}) == len(parsed)
    assert {summary.displayed_label for summary in parsed} == {
        "matched",
        "ambiguous",
        "insufficient evidence",
        "no scheduled match",
    }


def test_earliest_save_and_raw_notes_are_preserved(tmp_path: Path) -> None:
    current = tmp_path / "labels.csv"
    current.write_text(
        "case_id,reviewer,label,selected_candidate_letter,selected_candidate_id,confidence,fragment_impression,rationale,notes\n"
        "C-x,r,MATCH_TOP,A,2023-11-30|f|r|t|,5,,same,current text\n",
        encoding="utf-8",
    )
    history = tmp_path / "labels.jsonl"
    events = [
        {
            "event": "save",
            "case_id": "C-x",
            "reviewer": "r",
            "label": "MATCH_TOP",
            "selected_candidate_letter": "A",
            "selected_candidate_id": "2023-11-30|f|r|t|",
            "confidence": 5,
            "rationale": ["same"],
            "notes": "first  note\nverbatim",
        },
        {
            "event": "save",
            "case_id": "C-x",
            "reviewer": "r",
            "label": "MATCH_OTHER",
            "selected_candidate_letter": "B",
            "selected_candidate_id": "2023-11-30|f|r|u|",
            "confidence": 1,
            "rationale": [],
            "notes": "later overwrite attempt",
        },
    ]
    history.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    parsed = load_first_pass_labels(current, history)
    assert len(parsed) == 1
    assert parsed[0].raw_primary_label == "MATCH_TOP"
    assert parsed[0].raw_notes == "first  note\nverbatim"
    assert parsed[0].source_kind == "append_only_first_save"
    assert parsed[0].current_export_matches_first_pass is False


@pytest.mark.parametrize(
    "notes",
    [
        "This is two trips and belongs to candidates A and E.",
        "Three trips; B, C, and E; should be split into three.",
        "This belongs to A and E.",
        "Use B, C, and E.",
        "Turns at 10:00 and then again at 11:00.",
    ],
)
def test_detect_multi_trip_language(notes: str) -> None:
    assert has_multi_trip_language(notes)


@pytest.mark.parametrize(
    "notes",
    [
        "One APC event belongs to two trips; split at stop 1652 "
        "direction change 07:22 EDT.",
        "One APC row spans two adjacent trips.",
        "Boundary occurs at the terminal stop.",
        "Split occurs at a direction transition.",
    ],
)
def test_detect_boundary_spanning_language_and_route(notes: str) -> None:
    assert has_boundary_spanning_language(notes)
    routed = suggest_routing(_packet(stratum="fragment"), _label(notes), _summary())
    assert routed.case_grain_suggestion == "boundary_spanning"
    assert routed.task_routing_suggestion == "boundary_spanning_dual_role"
    assert routed.fragment_role_suggestion == "ambiguous_fragment"
    assert "BOUNDARY_SPANNING_DUAL_ROLE" in routed.audit_reason_codes
    assert "NOTES_PRIMARY_LABEL_CONFLICT" in routed.audit_reason_codes


def test_notes_primary_conflict_variants_are_explicit_and_nonbinding() -> None:
    matched_multi = suggest_routing(
        _packet(), _label("This contains two trips."), _summary()
    )
    assert "NOTES_PRIMARY_LABEL_CONFLICT" in matched_multi.audit_reason_codes
    ambiguous_quality = suggest_routing(
        _packet(),
        _label(
            "Malformed trajectory with a spatial-temporal conflict.",
            primary="AMBIGUOUS",
            card=None,
            candidate_id=None,
        ),
        _summary(),
    )
    assert "NOTES_PRIMARY_LABEL_CONFLICT" in ambiguous_quality.audit_reason_codes
    explicit_fragment = suggest_routing(
        _packet(stratum="fragment"),
        _label("This is only a local fragment."),
        _summary(),
    )
    assert "NOTES_PRIMARY_LABEL_CONFLICT" in explicit_fragment.audit_reason_codes


@pytest.mark.parametrize(
    "notes",
    [
        "Vehicle appears at two spots simultaneously: spatial temporal conflict.",
        "Unresolved DST fold leaves impossible movement in a malformed trajectory.",
    ],
)
def test_detect_spatial_temporal_failure_language(notes: str) -> None:
    assert has_quality_failure_language(notes)


def test_detect_low_confidence_match() -> None:
    label = _label(confidence=1)
    routing = suggest_routing(_packet(), label, _summary())
    key = resolve_candidate_key(
        "A", label.raw_selected_candidate_id, [label.raw_selected_candidate_id]
    )
    reasons = consistency_reason_codes(
        label, routing, visible_evidence_metrics(_summary(), "A"), key
    )
    assert "LOW_CONFIDENCE_MATCH" in reasons
    assert label.raw_confidence == 1


def test_detect_high_confidence_blank_rationale_match() -> None:
    label = _label(notes="", rationale=())
    routing = suggest_routing(_packet(), label, _summary())
    key = resolve_candidate_key(
        "A", label.raw_selected_candidate_id, [label.raw_selected_candidate_id]
    )
    reasons = consistency_reason_codes(
        label, routing, visible_evidence_metrics(_summary(), "A"), key
    )
    assert "HIGH_CONFIDENCE_NO_RATIONALE" in reasons


def test_detect_visible_evidence_ties() -> None:
    candidates = tuple(
        SummaryCandidate(card, "29", None, None, 335, 27.0) for card in "ABCDE"
    )
    metrics = visible_evidence_metrics(_summary(candidates), "C")
    assert metrics.selected_exact_tie_count == 4
    assert metrics.all_evaluable_candidates_tied
    assert metrics.selected_overlap_advantage_s == 0
    assert metrics.selected_coverage_advantage_pct_points == 0


def test_nearly_identical_visible_evidence_without_rationale_is_flagged() -> None:
    candidates = (
        SummaryCandidate("A", "29", None, None, 335, 27.0),
        SummaryCandidate("B", "29", None, None, 320, 27.5),
    )
    summary = _summary(candidates)
    label = _label()
    routing = suggest_routing(_packet(), label, summary)
    key = resolve_candidate_key(
        "A", label.raw_selected_candidate_id, [label.raw_selected_candidate_id]
    )
    reasons = consistency_reason_codes(
        label, routing, visible_evidence_metrics(summary, "A"), key
    )
    assert "VISIBLE_EVIDENCE_NON_DISCRIMINATIVE" in reasons


def test_resolve_card_to_full_key_with_explicit_null_polarity() -> None:
    key = "2023-11-30|feed|mapping|trip|"
    resolution = resolve_candidate_key("C", None, [key], card_key_map={"C": key})
    assert resolution.status == "resolved_complete_key_explicit_null_polarity"
    assert resolution.explicit_null_polarity
    assert resolution.polarity_hypothesis_id is None
    assert resolution.serialized_key == key


@pytest.mark.parametrize(
    ("card", "candidate_id", "available", "status"),
    [
        ("C", None, [], "card_only_key_missing"),
        ("C", "2023-11-30|feed|mapping|trip", [], "invalid_key_arity"),
        (
            "C",
            "2023-11-30|feed|mapping|trip|pol_a",
            [],
            "key_not_in_case_candidate_table",
        ),
    ],
)
def test_full_key_resolution_fails_cleanly(
    card: str,
    candidate_id: str | None,
    available: list[str],
    status: str,
) -> None:
    assert resolve_candidate_key(card, candidate_id, available).status == status


def test_missing_match_key_blocks_readiness_without_relabeling() -> None:
    label = _label(candidate_id=None)
    routing = suggest_routing(_packet(), label, _summary())
    key = resolve_candidate_key("A", None, [])
    visible = visible_evidence_metrics(_summary(), "A")
    reasons = consistency_reason_codes(label, routing, visible, key)
    result = readiness(label, routing, key, reasons)
    assert "FULL_CANDIDATE_KEY_MISSING" in reasons
    assert "candidate_key_resolution_required" in result.classes
    assert result.calibration_ready is False
    assert label.raw_primary_label == "MATCH_TOP"


def test_plain_no_match_routes_to_schedule_abstention() -> None:
    label = _label(
        "No scheduled service fits the activity.",
        primary="NO_SCHEDULED_MATCH",
        card=None,
        candidate_id=None,
    )
    routing = suggest_routing(_packet(), label, _summary())
    assert routing.task_routing_suggestion == "scheduled_trip_abstention"


def test_detect_adjacent_context_and_timezone_inconsistency() -> None:
    summary = _summary()
    refs = extract_note_time_references(
        "split at 07:22 EDT in the next trip",
        summary.window_start,
        summary.window_end,
    )
    assert refs[0].relation_to_window == "after_current_window"
    assert refs[0].offset_from_window_s == 1320
    assert refs[0].expected_timezone_text == "EST"
    assert refs[0].timezone_consistent is False
    routed = suggest_routing(
        _packet(), _label("Review the next chunk before deciding."), summary
    )
    assert "ADJACENT_CONTEXT_USED" in routed.audit_reason_codes


def test_timezone_text_can_be_consistent_on_dst_calendar_date() -> None:
    from datetime import datetime

    refs = extract_note_time_references(
        "split at 00:43 EDT",
        datetime(2023, 11, 5, 0, 0),
        datetime(2023, 11, 5, 0, 30),
    )
    assert refs[0].expected_timezone_text == "EDT"
    assert refs[0].timezone_consistent is True


def test_ambiguous_fall_fold_accepts_either_valid_abbreviation() -> None:
    from datetime import datetime

    for abbreviation in ("EDT", "EST"):
        refs = extract_note_time_references(
            f"split at 01:30 {abbreviation}",
            datetime(2023, 11, 5, 0, 30),
            datetime(2023, 11, 5, 2, 0),
        )
        assert refs[0].timezone_consistent is True
        assert refs[0].expected_timezone_text == "EDT/EST"


def test_unknown_timezone_text_is_explicitly_unverifiable() -> None:
    routed = suggest_routing(
        _packet(), _label("Check the next trip at 07:22 AST."), _summary()
    )
    assert "REVIEWER_TIMEZONE_TEXT_UNVERIFIABLE" in routed.audit_reason_codes


def test_audit_suggestions_are_separate_from_final_labels(audit_run: dict) -> None:
    output = audit_run["output"]
    assert isinstance(output, Path)
    rows = list(csv.DictReader((output / "label_audit_cases.csv").open()))
    assert rows
    assert all(
        "corrected_label" not in row and "final_label" not in row for row in rows
    )
    schema = _json_payload(output / "proposed_label_schema_v2.json")
    assert schema["status"] == "proposal_only_not_populated"
    assert schema["audit"]["suggestions_are_nonbinding"] is True
    assert "activity_state" in schema
    assert "non_revenue" in schema["activity_state"]["enum"]


def test_priority_score_is_deterministic_and_additive() -> None:
    reasons = [
        "NOTES_PRIMARY_LABEL_CONFLICT",
        "BOUNDARY_SPANNING_DUAL_ROLE",
        "DATA_QUALITY_OR_TIME_RESOLUTION_FAILURE",
        "LOW_CONFIDENCE_MATCH",
        "HIGH_CONFIDENCE_NO_RATIONALE",
        "FULL_CANDIDATE_KEY_MISSING",
        "FRAGMENT_MATCH_AT_FULL_TRIP_GRAIN",
        "VISIBLE_EVIDENCE_NON_DISCRIMINATIVE",
        "ADJACENT_CONTEXT_USED",
        "REVIEWER_TIMEZONE_TEXT_INCONSISTENT",
        "NOTES_PRIMARY_LABEL_CONFLICT",
    ]
    assert priority_score(reasons) == 33
    assert priority_score(reversed(reasons)) == 33


def test_real_first_pass_counts_and_mandatory_case_routing(audit_run: dict) -> None:
    output = audit_run["output"]
    assert isinstance(output, Path)
    preserved = pl.read_parquet(output / "first_pass_labels_v1.parquet")
    assert preserved.height == 18
    assert Counter(preserved["raw_primary_label"].to_list()) == {
        "MATCH_TOP_CANDIDATE": 11,
        "MATCH_OTHER_CANDIDATE": 2,
        "AMBIGUOUS": 2,
        "INSUFFICIENT_EVIDENCE": 2,
        "NO_SCHEDULED_MATCH": 1,
    }
    audit = {
        row["case_id"]: row
        for row in csv.DictReader((output / "label_audit_cases.csv").open())
    }
    assert audit["C-2630793f"]["task_routing_suggestion"] == "segmentation_multi_trip"
    assert audit["C-ba6b0e92"]["task_routing_suggestion"] == "segmentation_multi_trip"
    assert audit["C-913f7d4c"]["boundary_stop"] == "1652"
    assert audit["C-913f7d4c"]["boundary_offset_from_chunk_end_s"] == "382"
    assert audit["C-c953dac7"]["boundary_stop"] == "2107"
    assert audit["C-c953dac7"]["boundary_offset_from_chunk_end_s"] == "418"
    assert audit["C-7d5fcf98"]["task_routing_suggestion"] == "source_time_quality"
    assert "LOW_CONFIDENCE_MATCH" in audit["C-1b38b137"]["audit_reason_codes"]
    assert audit["C-7d4b42b8"]["all_evaluable_candidates_tied"] == "true"
    assert audit["C-ba617064"]["reviewability_suggestion"] == "insufficient_context"
    assert audit["C-88e48154"]["task_routing_suggestion"] == "no_match_activity"
    summary = _json_payload(output / "label_audit_summary.json")
    assert summary["grain_routing"]["case_grain"] == {
        "boundary_spanning": 2,
        "data_quality_invalid": 1,
        "local_fragment": 5,
        "multi_trip_chunk": 2,
        "single_trip_candidate": 7,
        "turnaround_or_idle": 1,
    }
    assert summary["grain_routing"]["task_routing"] == {
        "boundary_spanning_dual_role": 2,
        "fragment_role_and_stitching": 4,
        "insufficient_context": 1,
        "no_match_activity": 1,
        "scheduled_trip_second_pass": 7,
        "segmentation_multi_trip": 2,
        "source_time_quality": 1,
    }
    readiness_report = _json_payload(output / "label_readiness_report.json")
    assert (
        readiness_report["strict_scheduled_calibration_gate"]["calibration_ready_count"]
        == 0
    )
    assert readiness_report["strict_scheduled_calibration_gate"][
        "selected_candidate_key_resolution"
    ] == {"applicable_count": 13, "resolved_count": 13, "unresolved_count": 0}
    assert summary["review_history"] == {
        "current_export_matches_first_pass_count": 18,
        "diagnostic_reveal_event_count": 0,
        "event_count": 21,
        "event_type_counts": {"resave": 3, "save": 18},
        "first_pass_label_count": 18,
        "resave_count": 3,
        "resaves_semantically_identical_to_first_pass": 3,
    }
    assert summary["candidate_separability"]["candidate_table_key_count_total"] == {
        "min": 26,
        "median": 34.5,
        "max": 57,
    }
    assert summary["candidate_separability"]["complete_candidate_count"] == {
        "min": 23,
        "median": 31.5,
        "max": 54,
    }
    design = summary["design_contract_audit"]
    assert design["labels_reviewed"] is False
    assert design["label_artifact_registered"] is False
    assert design["architecture_selected"] is False
    assert design["r1_closed"] is False
    assert design["phase_b_processed"] is False
    assert design["current_packet_holdout_status"] == "not_established"
    assert design["ui_evidence_audit"]["shortlist_uses_anchor_or_qscore"] is True
    assert design["ui_evidence_audit"]["panels_viewed_recorded"] is False


def test_packet_metadata_is_not_silently_reinterpreted(audit_run: dict) -> None:
    output = audit_run["output"]
    assert isinstance(output, Path)
    rows = list(csv.DictReader((output / "label_case_inventory.csv").open()))
    assert {row["original_n_events"] for row in rows} == {"1"}
    assert {row["original_span_rel_s"] for row in rows} == {"0"}
    assert {row["n_events_meaning"] for row in rows} == {
        "count of synthetic packet events"
    }
    assert len({row["chunk_duration_s"] for row in rows}) > 1
    assert all(row["avl_ping_count"] for row in rows)
    assert all(row["complete_candidate_count"] for row in rows)


def test_repository_safe_figures_and_reports_are_pseudonym_safe(
    audit_run: dict,
) -> None:
    output = audit_run["output"]
    assert isinstance(output, Path)
    safe_paths = [
        path
        for path in output.rglob("*")
        if path.is_file()
        and path.name != "first_pass_labels_v1.parquet"
        and not path.name.startswith("first_pass_labels_v1.parquet.meta")
        and path.suffix in {".csv", ".json", ".md", ".svg"}
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in safe_paths)
    vehicle_ids = set()
    for path in SUMMARIES.glob("C-*_summary*.md"):
        match = re.search(r"^- vehicle:\s*(\S+)", path.read_text(), re.MULTILINE)
        if match:
            vehicle_ids.add(match.group(1))
    assert not any(vehicle_id in text for vehicle_id in vehicle_ids)
    assert re.search(r"\b\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\b", text) is None
    assert "/home/" not in text
    figure_names = sorted(path.name for path in (output / "figures").glob("*.svg"))
    assert len(figure_names) == 9
    for path in (output / "figures").glob("*.svg"):
        assert ET.parse(path).getroot().tag.endswith("svg")


def test_every_required_artifact_has_metadata(audit_run: dict) -> None:
    output = audit_run["output"]
    assert isinstance(output, Path)
    manifest = _json_payload(output / "run_manifest.json")
    required = {
        "first_pass_labels_v1.parquet",
        "label_case_inventory.csv",
        "label_audit_cases.csv",
        "label_audit_summary.json",
        "label_readiness_report.json",
        "adjudication_queue_v2.csv",
        "child_case_requests.csv",
        "proposed_label_schema_v2.json",
        "human_label_audit_findings.md",
        "possible_next_steps.md",
        "run_manifest.json",
        "input_hashes.json",
        "logical_checksums.json",
    }
    assert required.issubset(path.name for path in output.iterdir())
    metadata_fields = (
        "row_grain",
        "logical_key",
        "producer",
        "input_hashes",
        "source_tree_sha256",
        "schema_version",
        "generated_time",
        "contains_human_labels",
        "privacy_classification",
    )
    parquet_metadata = pq.read_schema(output / "first_pass_labels_v1.parquet").metadata
    assert parquet_metadata is not None
    embedded = json.loads(parquet_metadata[b"r1spike.audit.metadata"])
    assert all(field in embedded for field in metadata_fields)

    for path in sorted(item for item in output.rglob("*") if item.is_file()):
        if path.name.endswith(".meta.json"):
            metadata = json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix == ".csv":
            metadata = json.loads(
                path.with_name(path.name + ".meta.json").read_text(encoding="utf-8")
            )
        elif path.suffix == ".svg":
            root = ET.parse(path).getroot()
            metadata_node = next(
                child for child in root if child.tag.endswith("metadata")
            )
            metadata = json.loads(metadata_node.text or "{}")
        elif path.suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            metadata = payload.get("_artifact_metadata", payload)
        elif path.suffix == ".md":
            body = path.read_text(encoding="utf-8")
            match = re.match(r"<!-- artifact-metadata\n(.*?)\n-->\n", body, re.DOTALL)
            assert match is not None
            metadata = json.loads(match.group(1))
        elif path.suffix == ".parquet":
            schema_metadata = pq.read_schema(path).metadata
            assert schema_metadata is not None
            metadata = json.loads(schema_metadata[b"r1spike.audit.metadata"])
        else:  # pragma: no cover - fail closed if the writer gains a new format
            raise AssertionError(f"unrecognized artifact format: {path}")
        assert all(field in metadata for field in metadata_fields), path
    assert manifest["adversarial_review"]["status"] == "passed"
    assert manifest["factorial_authorized"] is False


def test_outputs_are_identical_across_repeated_runs(tmp_path: Path) -> None:
    outputs = [tmp_path / "first" / "audit-a", tmp_path / "second" / "audit-b"]
    for output in outputs:
        run_audit(AuditConfig(PACKET, SUMMARIES, ARTIFACTS, output))
    first_files = {
        path.relative_to(outputs[0]).as_posix(): sha256_file(path)
        for path in outputs[0].rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(outputs[1]).as_posix(): sha256_file(path)
        for path in outputs[1].rglob("*")
        if path.is_file()
    }
    assert first_files == second_files


def test_existing_output_is_never_overwritten(audit_run: dict) -> None:
    output = audit_run["output"]
    assert isinstance(output, Path)
    with pytest.raises(AuditArtifactError, match="refusing to overwrite"):
        run_audit(AuditConfig(PACKET, SUMMARIES, ARTIFACTS, output))


def test_no_calibration_threshold_or_factorial_module_is_invoked(
    audit_run: dict,
) -> None:
    trees = [ast.parse(path.read_text(encoding="utf-8")) for path in AUDIT_SOURCE_FILES]
    imported_modules = {
        node.module
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("factorial" in module for module in imported_modules)
    assert not any("calibrat" in module for module in imported_modules)
    assert not any("threshold" in module for module in imported_modules)
    output = audit_run["output"]
    assert isinstance(output, Path)
    manifest = _json_payload(output / "run_manifest.json")
    assert manifest["factorial_authorized"] is False
    assert manifest["adversarial_review"]["lenses"]["factorial_remains_blocked"]


def test_reveal_after_save_does_not_retroactively_change_first_pass(
    tmp_path: Path,
) -> None:
    history = tmp_path / "labels.jsonl"
    events = [
        {
            "event": "save",
            "case_id": "C-x",
            "reviewer": "r",
            "label": "AMBIGUOUS",
            "confidence": 3,
            "notes": "",
        },
        {"event": "reveal_diagnostics", "case_id": "C-x", "reviewer": "r"},
    ]
    history.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    labels = load_first_pass_labels(None, history)
    assert len(labels) == 1
    assert labels[0].diagnostics_revealed_before_save is False
    assert labels[0].diagnostics_reveal_source == "append_only_event_chronology"
    assert labels[0].raw_primary_label == "AMBIGUOUS"


def test_reveal_before_save_is_recovered_from_history_chronology(
    tmp_path: Path,
) -> None:
    history = tmp_path / "labels.jsonl"
    events = [
        {"event": "reveal", "case_id": "C-x", "reviewer": "r"},
        {
            "event": "save",
            "case_id": "C-x",
            "reviewer": "r",
            "label": "AMBIGUOUS",
            "confidence": 3,
            "notes": "",
        },
    ]
    history.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    labels = load_first_pass_labels(None, history)
    assert labels[0].diagnostics_revealed_before_save is True
    assert labels[0].diagnostics_reveal_source == "append_only_event_chronology"


def test_reveal_chronology_conflict_is_preserved_and_conservatively_exposed(
    tmp_path: Path,
) -> None:
    history = tmp_path / "labels.jsonl"
    events = [
        {"event": "reveal", "case_id": "C-x", "reviewer": "r"},
        {
            "event": "save",
            "case_id": "C-x",
            "reviewer": "r",
            "label": "AMBIGUOUS",
            "confidence": 3,
            "notes": "",
            "diagnostics_revealed_before_save": False,
        },
    ]
    history.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    label = load_first_pass_labels(None, history)[0]
    assert label.raw_diagnostics_revealed_before_save is False
    assert label.history_diagnostics_revealed_before_save is True
    assert label.diagnostics_revealed_before_save is True
    assert label.diagnostics_reveal_source == "saved_field_event_chronology_conflict"
    assert label.diagnostics_reveal_consistency_status == "conflict"


def test_standalone_should_be_split_is_multi_trip_language() -> None:
    assert has_multi_trip_language("This should be split.")


def test_no_production_file_or_original_label_is_modified(audit_run: dict) -> None:
    source_hashes = audit_run["source_hashes"]
    assert isinstance(source_hashes, dict)
    assert all(
        path.exists() and sha256_file(path) == digest
        for path, digest in source_hashes.items()
    )
    assert _tree_hash(TTC_ROOT / "src" / "ttc_tides") == audit_run["production_hash"]
    result = audit_run["result"]
    assert result.original_input_hashes_unchanged is True


def test_first_pass_parquet_notes_exactly_match_earliest_history_save(
    audit_run: dict,
) -> None:
    earliest: dict[tuple[str, str], dict] = {}
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") == "save":
            earliest.setdefault((event["case_id"], event["reviewer"]), event)
    output = audit_run["output"]
    assert isinstance(output, Path)
    rows = pl.read_parquet(output / "first_pass_labels_v1.parquet").to_dicts()
    for row in rows:
        event = earliest[(row["case_id"], row["reviewer_id"])]
        assert row["raw_notes"] == event.get("notes", "")
        assert row["raw_primary_label"] == event["label"]
        assert row["raw_confidence"] == event["confidence"]
