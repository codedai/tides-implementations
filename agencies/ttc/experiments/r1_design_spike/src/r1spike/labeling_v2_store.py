"""Blind second-pass review store for Human Labeling Protocol v2.

Persistence model: an append-only ``review_history.jsonl`` inside the run
directory's live zone is the source of truth; every derived live artifact
(second-pass parquet, adjudication queue, agreement, readiness, go/no-go)
is regenerated from it after each mutation. First-pass labels are read only
through :class:`FirstPassGate`, which refuses access until the case's blind
judgment is saved and logs every reveal.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from .human_label_artifacts import logical_sha256
from .human_label_parser import sha256_file
from .labeling_v2_artifacts import LabelingV2ArtifactError, LiveArtifactUpdater
from .labeling_v2_contracts import (
    ACTIVITY_STATES,
    ADJUDICATION_COLUMNS,
    ADJUDICATION_RESOLUTIONS,
    CASE_GRAINS,
    CHILD_GRAIN_BY_TASK,
    EVIDENCE_FAMILIES,
    FRAGMENT_ROLES,
    FROZEN_ARTIFACTS,
    PROTOCOL_DOCUMENTS,
    QUALITY_FLAGS,
    REVIEWABILITY,
    SCHEDULED_JUDGMENTS,
    SECOND_PASS_COLUMNS,
    SINGLE_DECISIVE_ANCHOR,
    TRISTATE,
    UI_VERSION,
)
from .labeling_v2_universe import KIND_COMPLETE, KIND_NULL_OPTION

LOCAL_TZ = ZoneInfo("America/Toronto")

FIRST_PASS_LABEL_MAP = {
    "MATCH_TOP_CANDIDATE": "matched",
    "MATCH_OTHER_CANDIDATE": "matched",
    "NO_SCHEDULED_MATCH": "no_scheduled_match",
    "AMBIGUOUS": "ambiguous_candidates",
    "INSUFFICIENT_EVIDENCE": "insufficient_evidence",
}


class BlindProtocolViolation(RuntimeError):
    """An action would break the blind-review contract."""


class LabelValidationError(ValueError):
    """A submitted judgment violates the frozen v2 schema."""


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_timezone_label(epoch_s: int | float | None) -> str | None:
    """UTC-offset-derived tz label (EST/EDT) for an instant; never from text."""

    if epoch_s is None:
        return None
    return datetime.fromtimestamp(int(epoch_s), tz=UTC).astimezone(LOCAL_TZ).tzname()


def derive_timezone_label_for_naive_local(value: datetime) -> str:
    """Label for a naive Toronto wall time; explicit about the fall-back fold."""

    if value.tzinfo is not None:
        raise ValueError("expected a naive local wall time")
    earlier = value.replace(fold=0, tzinfo=LOCAL_TZ)
    later = value.replace(fold=1, tzinfo=LOCAL_TZ)
    if earlier.utcoffset() == later.utcoffset():
        return str(earlier.tzname())
    return f"ambiguous ({earlier.tzname()}/{later.tzname()} fall-back fold)"


def verify_frozen_zone(run_dir: Path) -> dict[str, Any]:
    """Recompute frozen-artifact hashes against the freeze manifest.

    The protocol hash is re-derived from the on-disk protocol documents —
    the manifest's own claim is cross-checked, never trusted, so a
    document edit with a patched per-file hash still fails verification.
    """

    run_dir = Path(run_dir)
    manifest_path = run_dir / "protocol_freeze_manifest.json"
    if not manifest_path.exists():
        return {"ok": False, "error": "protocol_freeze_manifest.json missing"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recorded = manifest.get("frozen_artifact_hashes", {})
    mismatches: list[str] = []
    actual_hashes: dict[str, str] = {}
    for name in FROZEN_ARTIFACTS:
        if name == "protocol_freeze_manifest.json":
            continue
        path = run_dir / name
        if not path.exists():
            mismatches.append(f"{name}: missing")
            continue
        actual = sha256_file(path)
        actual_hashes[name] = actual
        if recorded.get(name) != actual:
            mismatches.append(f"{name}: hash mismatch")
    recorded_documents = manifest.get("protocol_document_hashes", {})
    for name in PROTOCOL_DOCUMENTS:
        if (
            name in actual_hashes
            and recorded_documents.get(name) != (actual_hashes[name])
        ):
            mismatches.append(f"{name}: protocol document hash mismatch")
    if all(name in actual_hashes for name in PROTOCOL_DOCUMENTS):
        derived = protocol_sha256_from_hashes(
            {name: actual_hashes[name] for name in PROTOCOL_DOCUMENTS}
        )
        if derived != manifest.get("protocol_sha256"):
            mismatches.append(
                "protocol_sha256: manifest value does not match the "
                "on-disk protocol documents"
            )
    return {
        "ok": not mismatches,
        "mismatches": mismatches,
        "protocol_version": manifest.get("protocol_version"),
        "protocol_sha256": manifest.get("protocol_sha256"),
        "manifest": manifest,
    }


def protocol_sha256_from_hashes(document_hashes: dict[str, str]) -> str:
    ordered = {name: document_hashes[name] for name in PROTOCOL_DOCUMENTS}
    return logical_sha256(ordered)


@dataclass
class CaseState:
    case_id: str
    is_child: bool
    states: set[str] = field(default_factory=set)
    blind_save: dict[str, Any] | None = None
    post_reveal_records: list[dict[str, Any]] = field(default_factory=list)
    adjudications: list[dict[str, Any]] = field(default_factory=list)
    reveals: list[dict[str, Any]] = field(default_factory=list)
    routing_changes: list[dict[str, Any]] = field(default_factory=list)
    comparison: dict[str, Any] | None = None

    @property
    def second_pass_saved(self) -> bool:
        return self.blind_save is not None


class FirstPassGate:
    """Read access to first-pass labels, closed until the blind save exists."""

    def __init__(self, first_pass_path: Path, store: ReviewStore):
        self._path = Path(first_pass_path)
        self._store = store
        self._frame: pl.DataFrame | None = None

    def sha256(self) -> str | None:
        return sha256_file(self._path) if self._path.exists() else None

    def _load(self) -> pl.DataFrame:
        if self._frame is None:
            self._frame = pl.read_parquet(self._path)
        return self._frame

    def for_case(self, case_id: str, *, reviewer_id: str, log: bool = True) -> dict:
        state = self._store.case_state(case_id)
        if not state.second_pass_saved:
            raise BlindProtocolViolation(
                f"first-pass label for {case_id} stays hidden until the blind "
                "second-pass judgment is saved"
            )
        rows = self._load().filter(pl.col("case_id") == case_id)
        if rows.is_empty():
            raise KeyError(f"no first-pass label for case {case_id}")
        if log:
            self._store.log_event(
                {
                    "event": "first_pass_reveal",
                    "case_id": case_id,
                    "reviewer_id": reviewer_id,
                }
            )
        return rows.row(0, named=True)


class ReviewStore:
    """Append-only event store plus derived live artifacts for one run."""

    def __init__(
        self,
        run_dir: Path,
        *,
        first_pass_path: Path,
        reviewer_id: str,
        session_id: str | None = None,
        readiness_builder=None,
    ):
        self.run_dir = Path(run_dir).resolve()
        verification = verify_frozen_zone(self.run_dir)
        if not verification["ok"]:
            raise BlindProtocolViolation(
                "frozen protocol zone failed verification — bump the protocol "
                f"version instead of editing in place: {verification}"
            )
        self.protocol_sha256: str = verification["protocol_sha256"]
        self.protocol_version: str = verification["protocol_version"]
        self.reviewer_id = reviewer_id
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.updater = LiveArtifactUpdater(self.run_dir)
        self.history_path = self.run_dir / "review_history.jsonl"
        self.alias_map = pl.read_parquet(self.run_dir / "candidate_alias_map.parquet")
        self.routing = pl.read_parquet(self.run_dir / "case_routing_manifest.parquet")
        self.children = pl.read_parquet(self.run_dir / "child_case_manifest.parquet")
        self.blind_order = json.loads(
            (self.run_dir / "blind_review_order.json").read_text(encoding="utf-8")
        )
        self.first_pass = FirstPassGate(first_pass_path, self)
        self._verify_first_pass_input(Path(first_pass_path))
        self._readiness_builder = readiness_builder

    def _verify_first_pass_input(self, first_pass_path: Path) -> None:
        """The first-pass file must exist and match its frozen hash.

        Without this, a missing or swapped first-pass parquet silently
        disables the comparison-and-adjudication mechanism.
        """

        if not first_pass_path.exists():
            raise BlindProtocolViolation(
                f"first-pass labels missing: {first_pass_path} — the blind "
                "comparison cannot run without the preserved first pass"
            )
        recorded = None
        try:
            frozen = json.loads(
                (self.run_dir / "input_hashes.json").read_text(encoding="utf-8")
            )
            for entry in frozen.get("files", []):
                if entry["path"].endswith("first_pass_labels_v1.parquet"):
                    recorded = entry["sha256"]
                    break
        except (OSError, json.JSONDecodeError):
            recorded = None
        if recorded is not None and sha256_file(first_pass_path) != recorded:
            raise BlindProtocolViolation(
                "first-pass labels changed since the protocol freeze — "
                "refusing to compare against a mutated first pass"
            )

    # ------------------------------------------------------------------ events

    def _lock(self):
        """Advisory exclusive lock spanning state-read plus append (v1 parity)."""

        import contextlib

        @contextlib.contextmanager
        def _noop():
            yield

        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX fallback
            return _noop()

        @contextlib.contextmanager
        def _locked():
            lock_path = self.run_dir / ".lock"
            with lock_path.open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

        return _locked()

    def events(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if self.history_path.exists():
            with self.history_path.open(encoding="utf-8") as handle:
                for index, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise BlindProtocolViolation(
                            "review history is corrupt at "
                            f"{self.history_path}:{index} — repair the line "
                            "before continuing; the append-only log is the "
                            f"source of truth ({error})"
                        ) from error
                    if not isinstance(event, dict):
                        raise BlindProtocolViolation(
                            "review history is corrupt at "
                            f"{self.history_path}:{index} — expected a JSON "
                            "object"
                        )
                    event["_line_number"] = index
                    records.append(event)
        return records

    def log_event(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = {
            **event,
            "event_at": utc_now_iso(),
            "session_id": self.session_id,
            "protocol_version": self.protocol_version,
            "protocol_sha256": self.protocol_sha256,
            "ui_version": UI_VERSION,
        }
        with self._lock():
            self.updater.append_jsonl("review_history.jsonl", payload)
        return payload

    # ------------------------------------------------------------------ state

    def known_case_ids(self) -> list[str]:
        parents = self.routing["case_id"].to_list()
        children = self.children["child_case_id"].to_list()
        return list(parents) + list(children)

    def is_child(self, case_id: str) -> bool:
        return "::" in case_id

    def case_state(self, case_id: str) -> CaseState:
        return self.all_states().get(
            case_id, CaseState(case_id=case_id, is_child=self.is_child(case_id))
        )

    def all_states(self) -> dict[str, CaseState]:
        states: dict[str, CaseState] = {}
        for case_id in self.known_case_ids():
            state = CaseState(case_id=case_id, is_child=self.is_child(case_id))
            if not state.is_child:
                state.states.add("FIRST_PASS_PRESERVED")
            state.states.add("SECOND_PASS_NOT_STARTED")
            states[case_id] = state
        for event in self.events():
            case_id = event.get("case_id")
            if case_id is None:
                continue
            state = states.setdefault(
                case_id, CaseState(case_id=case_id, is_child=self.is_child(case_id))
            )
            kind = event.get("event")
            if kind == "second_pass_save":
                if event.get("comparison_only"):
                    state.comparison = event.get("comparison")
                    outcome = (state.comparison or {}).get("outcome")
                    if outcome:
                        state.states.add(outcome)
                    if (
                        outcome
                        in (
                            "FIRST_SECOND_DISAGREE",
                            "NOT_EVALUABLE_AT_CURRENT_GRAIN",
                            "COMPARISON_UNAVAILABLE",
                        )
                        and "ADJUDICATED" not in state.states
                    ):
                        state.states.add("REQUIRES_ADJUDICATION")
                elif state.blind_save is None:
                    state.blind_save = event
                    state.states.discard("SECOND_PASS_NOT_STARTED")
                    state.states.add("SECOND_PASS_SAVED")
                else:
                    state.post_reveal_records.append(event)
            elif kind in ("post_reveal_revision", "post_reveal_confirm"):
                state.post_reveal_records.append(event)
            elif kind == "adjudication_save":
                state.adjudications.append(event)
                if event.get("resolution") == "deferred":
                    # a deferral records the decision but keeps the case in
                    # the adjudication queue
                    state.states.add("REQUIRES_ADJUDICATION")
                else:
                    state.states.add("ADJUDICATED")
                    state.states.discard("REQUIRES_ADJUDICATION")
            elif kind in ("diagnostics_reveal", "first_pass_reveal"):
                state.reveals.append(event)
            elif kind == "routing_change":
                state.routing_changes.append(event)
        return states

    def diagnostics_revealed_before_save(self, case_id: str) -> bool:
        state = self.case_state(case_id)
        if state.second_pass_saved:
            return bool(state.blind_save.get("diagnostics_revealed_before_save"))
        return any(
            reveal.get("event") == "diagnostics_reveal" for reveal in state.reveals
        )

    # ------------------------------------------------------------ validation

    def _alias_rows(self, case_id: str) -> pl.DataFrame:
        root = case_id.split("::")[0]
        return self.alias_map.filter(pl.col("case_id") == root)

    def resolve_alias(self, case_id: str, alias: str) -> dict[str, Any]:
        rows = self._alias_rows(case_id).filter(pl.col("candidate_alias") == alias)
        if rows.is_empty():
            raise LabelValidationError(
                f"alias {alias!r} is not in the frozen universe for {case_id}"
            )
        return rows.row(0, named=True)

    def _validate_key_membership(
        self,
        case_id: str,
        key: str,
        *,
        allowed_kinds: tuple[str, ...] = (KIND_COMPLETE,),
        slot: str = "candidate key",
    ) -> dict[str, Any]:
        rows = self._alias_rows(case_id).filter(pl.col("candidate_key") == key)
        if rows.is_empty():
            raise LabelValidationError(
                f"{slot} is not in the frozen universe for {case_id}"
            )
        row = rows.row(0, named=True)
        if row["candidate_kind"] not in allowed_kinds:
            raise LabelValidationError(
                f"{slot} must be a complete scheduled-trip key, not "
                f"{row['candidate_kind']} (use an explicit null instead)"
            )
        return row

    def _routing_row(self, case_id: str) -> dict[str, Any] | None:
        rows = self.routing.filter(pl.col("case_id") == case_id.split("::")[0])
        return None if rows.is_empty() else rows.row(0, named=True)

    CONFIDENCE_FIELDS = (
        "grain_confidence",
        "match_confidence",
        "evidence_quality",
        "split_confidence",
        "fragment_confidence",
    )

    def _validate_confidence(self, name: str, value: Any, *, nullable: bool) -> None:
        if value is None:
            if nullable:
                return
            raise LabelValidationError(f"{name} is required")
        if isinstance(value, bool) or not isinstance(value, int):
            raise LabelValidationError(
                f"{name} must be a plain integer 1..5, got {value!r}"
            )
        if not 1 <= value <= 5:
            raise LabelValidationError(f"{name} must be an integer 1..5")

    def _validate_rationale_gate(self, record: dict[str, Any]) -> None:
        # Every confidence dimension participates in the gate — including
        # evidence_quality; a non-integer confidence never sneaks past it
        # because _validate_confidence has already rejected it.
        confidences = [record.get(name) for name in self.CONFIDENCE_FIELDS]
        high = any(isinstance(c, int) and c >= 4 for c in confidences)
        flags = record.get("evidence_flags") or []
        text = (record.get("rationale_text") or "").strip()
        if high:
            if not flags:
                raise LabelValidationError(
                    "confidence 4-5 requires at least one structured rationale"
                )
            if len(text) < 8:
                raise LabelValidationError(
                    "confidence 4-5 requires a short free-text rationale"
                )
        for flag in flags:
            if flag not in EVIDENCE_FAMILIES and flag != SINGLE_DECISIVE_ANCHOR:
                raise LabelValidationError(f"unknown evidence flag: {flag}")
        if record.get("scheduled_judgment") == "matched" and high:
            families = {f for f in flags if f in EVIDENCE_FAMILIES}
            if len(families) < 2:
                if SINGLE_DECISIVE_ANCHOR not in flags:
                    raise LabelValidationError(
                        "a high-confidence scheduled match requires two "
                        "distinct evidence families or an explicit "
                        f"{SINGLE_DECISIVE_ANCHOR} declaration"
                    )
                if record.get("single_decisive_anchor_family") not in EVIDENCE_FAMILIES:
                    raise LabelValidationError(
                        "single_decisive_anchor requires the decisive evidence "
                        "family to be identified"
                    )

    def _validate_scheduled(self, case_id: str, record: dict[str, Any]) -> None:
        judgment = record.get("scheduled_judgment")
        if judgment not in SCHEDULED_JUDGMENTS:
            raise LabelValidationError(f"invalid scheduled_judgment: {judgment}")
        alias = record.get("selected_candidate_alias")
        if judgment == "matched":
            if not alias:
                raise LabelValidationError("a scheduled match requires a candidate")
            resolved = self.resolve_alias(case_id, alias)
            if resolved["candidate_kind"] == KIND_NULL_OPTION:
                raise LabelValidationError(
                    "the null option cannot be recorded as matched; use "
                    "no_scheduled_match"
                )
            if resolved["candidate_kind"] != KIND_COMPLETE:
                raise LabelValidationError(
                    "a scheduled match requires a complete five-position candidate key"
                )
            record["selected_candidate_key"] = resolved["candidate_key"]
            record["selected_service_date"] = resolved["service_date"]
            record["selected_feed_id"] = resolved["feed_id"]
            record["selected_route_mapping_id"] = resolved["route_mapping_id"]
            record["selected_scheduled_trip_id"] = resolved["scheduled_trip_id"]
            record["selected_polarity_hypothesis_id"] = resolved[
                "polarity_hypothesis_id"
            ]
            record["selected_explicit_null_polarity"] = resolved[
                "explicit_null_polarity"
            ]
            self._validate_confidence(
                "match_confidence", record.get("match_confidence"), nullable=False
            )
        else:
            if alias and judgment == "no_scheduled_match":
                raise LabelValidationError(
                    "no_scheduled_match cannot carry a selected candidate"
                )

    def _validate_multi_trip(self, case_id: str, record: dict[str, Any]) -> None:
        count = record.get("subtrip_count")
        if not isinstance(count, int) or count < 1:
            raise LabelValidationError("multi-trip requires subtrip_count >= 1")
        sequence = record.get("ordered_candidate_sequence")
        if not isinstance(sequence, list) or len(sequence) != count:
            raise LabelValidationError(
                "ordered_candidate_sequence must list one candidate key or "
                "null per subtrip"
            )
        for key in sequence:
            if key is not None:
                self._validate_key_membership(case_id, key)
        intervals = record.get("split_intervals_rel_s") or []
        routing = self._routing_row(case_id)
        duration = routing.get("chunk_duration_s") if routing else None
        warnings = []
        for interval in intervals:
            if (
                not isinstance(interval, (list, tuple))
                or len(interval) != 2
                or interval[0] > interval[1]
            ):
                raise LabelValidationError(
                    "split_intervals_rel_s entries must be [lo, hi] with lo <= hi"
                )
            if duration is not None and (interval[0] < 0 or interval[1] > duration):
                warnings.append(
                    f"interval {list(interval)} lies outside the displayed chunk"
                )
        if warnings:
            record["outside_displayed_chunk_warning"] = "; ".join(warnings)
        if intervals and duration is None:
            record["bounds_check_note"] = "bounds not checkable: chunk window unknown"

    def _validate_boundary(self, case_id: str, record: dict[str, Any]) -> None:
        for side in ("inbound_candidate_key", "outbound_candidate_key"):
            key = record.get(side)
            if key is not None:
                self._validate_key_membership(case_id, key)
        interval = record.get("boundary_time_interval_rel_s")
        routing = self._routing_row(case_id)
        if interval is not None:
            if (
                not isinstance(interval, (list, tuple))
                or len(interval) != 2
                or interval[0] > interval[1]
            ):
                raise LabelValidationError(
                    "boundary_time_interval_rel_s must be [lo, hi] with lo <= hi"
                )
            if routing and routing.get("chunk_duration_s") is not None:
                duration = routing["chunk_duration_s"]
                if interval[0] < 0 or interval[1] > duration:
                    record["outside_displayed_chunk_warning"] = (
                        f"boundary interval {list(interval)} lies outside the "
                        "displayed chunk"
                    )
            if routing and routing.get("chunk_start_epoch_s") is not None:
                midpoint = (
                    routing["chunk_start_epoch_s"] + (interval[0] + interval[1]) // 2
                )
                record["boundary_timezone_label_derived"] = derive_timezone_label(
                    midpoint
                )
        if record.get("boundary_timezone_label_derived") is None and routing:
            record["boundary_timezone_label_derived"] = derive_timezone_label(
                routing.get("chunk_end_epoch_s")
            )
        if routing is None or routing.get("chunk_duration_s") is None:
            record["bounds_check_note"] = "bounds not checkable: chunk window unknown"

    def _validate_fragment(self, case_id: str, record: dict[str, Any]) -> None:
        role = record.get("fragment_role")
        if role not in FRAGMENT_ROLES:
            raise LabelValidationError(f"invalid fragment_role: {role}")
        self._validate_confidence(
            "fragment_confidence", record.get("fragment_confidence"), nullable=True
        )
        for side in ("stitch_previous", "stitch_next"):
            if record.get(side) not in TRISTATE:
                raise LabelValidationError(f"{side} must be one of {TRISTATE}")
        local_key = record.get("local_scheduled_context_candidate_key")
        if local_key is not None:
            self._validate_key_membership(
                case_id, local_key, slot="local scheduled context"
            )
        judgment = record.get("complete_trip_judgment")
        if judgment is not None and judgment not in SCHEDULED_JUDGMENTS:
            raise LabelValidationError(f"invalid complete_trip_judgment: {judgment}")
        if judgment == "matched":
            raise LabelValidationError(
                "a fragment can never carry a complete performed-trip match; "
                "if this really is one whole trip, change the case grain "
                "instead"
            )

    def _validate_quality(self, record: dict[str, Any]) -> None:
        flags = record.get("quality_flags") or []
        if not flags:
            raise LabelValidationError("quality review requires quality_flags")
        for flag in flags:
            if flag not in QUALITY_FLAGS:
                raise LabelValidationError(f"unknown quality flag: {flag}")
        if record.get("scheduled_match_applicable") is False:
            if record.get("scheduled_judgment") not in (None, "not_applicable"):
                raise LabelValidationError(
                    "an invalid trajectory cannot carry a scheduled judgment"
                )
            record["scheduled_judgment"] = "not_applicable"

    def _validate_activity(self, record: dict[str, Any]) -> None:
        if record.get("activity_state") not in ACTIVITY_STATES:
            raise LabelValidationError(
                f"invalid activity_state: {record.get('activity_state')}"
            )

    # Fields that only alias resolution may set; caller-supplied values are
    # discarded so a fabricated key can never ride into the record.
    DERIVED_SELECTION_FIELDS = (
        "selected_candidate_key",
        "selected_service_date",
        "selected_feed_id",
        "selected_route_mapping_id",
        "selected_scheduled_trip_id",
        "selected_polarity_hypothesis_id",
        "selected_explicit_null_polarity",
    )

    def _child_task_type(self, case_id: str) -> str | None:
        rows = self.children.filter(pl.col("child_case_id") == case_id)
        return None if rows.is_empty() else rows.row(0, named=True)["child_task_type"]

    def validate_record(self, record: dict[str, Any]) -> dict[str, Any]:
        record = dict(record)
        for field_name in self.DERIVED_SELECTION_FIELDS:
            record.pop(field_name, None)
        case_id = record.get("case_id")
        if case_id not in set(self.known_case_ids()):
            raise LabelValidationError(f"unknown case: {case_id}")
        grain = record.get("case_grain")
        if grain not in CASE_GRAINS:
            raise LabelValidationError(f"invalid case_grain: {grain}")
        if self.is_child(case_id):
            task = self._child_task_type(case_id)
            pinned = CHILD_GRAIN_BY_TASK.get(task or "")
            if pinned is not None and grain != pinned:
                raise LabelValidationError(
                    f"child case {case_id} is pinned to grain {pinned} by its "
                    f"manifest task {task}; it cannot be saved as {grain}"
                )
        reviewability = record.get("reviewability")
        if reviewability is not None and reviewability not in REVIEWABILITY:
            raise LabelValidationError(f"invalid reviewability: {reviewability}")
        self._validate_confidence(
            "grain_confidence", record.get("grain_confidence"), nullable=False
        )
        self._validate_confidence(
            "evidence_quality", record.get("evidence_quality"), nullable=False
        )
        self._validate_confidence(
            "match_confidence", record.get("match_confidence"), nullable=True
        )
        self._validate_confidence(
            "split_confidence", record.get("split_confidence"), nullable=True
        )
        self._validate_confidence(
            "fragment_confidence", record.get("fragment_confidence"), nullable=True
        )
        if grain == "single_trip_candidate" and record.get("scheduled_judgment"):
            self._validate_scheduled(case_id, record)
        elif record.get("scheduled_judgment") not in (None, "not_applicable"):
            raise LabelValidationError(
                "a scheduled judgment other than not_applicable is only "
                f"valid at the single_trip_candidate grain, not {grain}; "
                "use the task-specific form for this grain"
            )
        if grain == "multi_trip_chunk":
            self._validate_multi_trip(case_id, record)
        if grain == "boundary_spanning":
            self._validate_boundary(case_id, record)
        if grain == "local_fragment":
            self._validate_fragment(case_id, record)
        if grain == "data_quality_invalid":
            self._validate_quality(record)
        if grain == "turnaround_or_idle":
            self._validate_activity(record)
        self._validate_rationale_gate(record)
        return record

    # ------------------------------------------------------------------ save

    def _first_pass_comparison(self, record: dict[str, Any]) -> dict[str, Any] | None:
        case_id = record["case_id"]
        if self.is_child(case_id):
            return None
        try:
            first = self.first_pass.for_case(
                case_id, reviewer_id=record.get("reviewer_id", ""), log=False
            )
        except BlindProtocolViolation:
            return None
        except (KeyError, OSError) as error:
            # A broken first-pass input is NOT "no comparison needed": the
            # case must not resolve silently. It lands in the adjudication
            # queue with an explicit unavailable outcome.
            return {
                "outcome": "COMPARISON_UNAVAILABLE",
                "basis": f"first-pass record unavailable: {error}",
                "first_pass_judgment_class": None,
                "first_pass_key_matches": None,
            }
        mapped = FIRST_PASS_LABEL_MAP.get(first["raw_primary_label"])
        first_key = first.get("raw_selected_candidate_id")
        outcome: str
        basis: str
        if record["case_grain"] != "single_trip_candidate":
            outcome = "NOT_EVALUABLE_AT_CURRENT_GRAIN"
            basis = (
                "second pass places the case at a different grain than the "
                "first-pass scheduled-trip framing"
            )
        else:
            second = record.get("scheduled_judgment")
            if mapped == second == "matched":
                same = first_key == record.get("selected_candidate_key")
                outcome = "FIRST_SECOND_AGREE" if same else "FIRST_SECOND_DISAGREE"
                basis = "matched judgments compared on the complete candidate key"
            elif mapped == second:
                outcome = "FIRST_SECOND_AGREE"
                basis = "non-match judgments compared on the judgment class"
            else:
                outcome = "FIRST_SECOND_DISAGREE"
                basis = "judgment classes differ"
        return {
            "outcome": outcome,
            "basis": basis,
            "first_pass_judgment_class": mapped,
            "first_pass_key_matches": (
                first_key == record.get("selected_candidate_key")
                if mapped == "matched" and record.get("selected_candidate_key")
                else None
            ),
        }

    def _reverify_before_save(self, case_id: str) -> None:
        """Save-time integrity: the frozen zone and alias map must still be
        the ones this store opened — a swapped or regenerated run directory
        must never silently absorb a judgment made against different data."""

        verification = verify_frozen_zone(self.run_dir)
        if not verification["ok"]:
            raise BlindProtocolViolation(
                "run directory changed since this session opened it; refusing "
                f"to save: {verification['mismatches']}"
            )
        if verification["protocol_sha256"] != self.protocol_sha256:
            raise BlindProtocolViolation(
                "protocol hash changed mid-session; restart against the "
                "current run directory"
            )
        on_disk = pl.read_parquet(self.run_dir / "candidate_alias_map.parquet")
        root = case_id.split("::")[0]
        mine = self.alias_map.filter(pl.col("case_id") == root).sort("display_order")
        theirs = on_disk.filter(pl.col("case_id") == root).sort("display_order")
        if not mine.equals(theirs):
            raise BlindProtocolViolation(
                "alias map on disk differs from this session's frozen copy; "
                "refusing to save an alias against a different universe"
            )

    def save_second_pass(self, record: dict[str, Any]) -> dict[str, Any]:
        record = self.validate_record(record)
        case_id = record["case_id"]
        self._reverify_before_save(case_id)
        state = self.case_state(case_id)
        if state.second_pass_saved:
            raise BlindProtocolViolation(
                f"a blind second-pass judgment for {case_id} already exists; "
                "post-reveal changes must be post_reveal_revision events"
            )
        if self.is_child(case_id):
            parent_state = self.case_state(case_id.split("::")[0])
            if not parent_state.second_pass_saved:
                raise BlindProtocolViolation(
                    f"child {case_id} cannot be saved before its parent's "
                    "blind judgment — note-derived child seeds presume the "
                    "parent pass is complete"
                )
        root_alias_rows = self._alias_rows(case_id)
        universe_hash = (
            root_alias_rows["candidate_universe_hash"][0]
            if not root_alias_rows.is_empty()
            else None
        )
        supplied_hash = record.get("candidate_universe_hash")
        if supplied_hash is not None and supplied_hash != universe_hash:
            raise BlindProtocolViolation(
                "candidate_universe_hash does not match the frozen universe "
                "for this case; the session is stale"
            )
        record_id = uuid.uuid4().hex[:16]
        payload = {
            **record,
            "event": "second_pass_save",
            "record_id": record_id,
            "review_round": record.get("review_round", 2),
            "reviewer_id": record.get("reviewer_id") or self.reviewer_id,
            "is_child_case": self.is_child(case_id),
            "parent_case_id": case_id.split("::")[0]
            if self.is_child(case_id)
            else None,
            "first_pass_hidden": not any(
                reveal.get("event") == "first_pass_reveal" for reveal in state.reveals
            ),
            "diagnostics_revealed_before_save": self.diagnostics_revealed_before_save(
                case_id
            ),
            "diagnostics_revealed_after_save": False,
            "candidate_universe_hash": record.get("candidate_universe_hash")
            or universe_hash,
            "review_saved_at": utc_now_iso(),
        }
        event = self.log_event(payload)
        comparison = self._first_pass_comparison(payload)
        if comparison is not None:
            # The comparison is computed only after the blind event is
            # durably appended; it is stored as its own event so the blind
            # save line itself stays first-pass-free.
            self.log_event(
                {
                    "event": "second_pass_save",
                    "amends_record_id": record_id,
                    "case_id": case_id,
                    "comparison": comparison,
                    "comparison_only": True,
                }
            )
        self.regenerate_live_artifacts()
        return {**event, "comparison": comparison}

    def record_post_reveal(
        self, case_id: str, record: dict[str, Any], *, revises_record_id: str
    ) -> dict[str, Any]:
        state = self.case_state(case_id)
        if not state.second_pass_saved:
            raise BlindProtocolViolation(
                "post-reveal records require a saved blind judgment"
            )
        record = self.validate_record({**record, "case_id": case_id})
        payload = {
            **record,
            "event": "post_reveal_revision",
            "record_id": uuid.uuid4().hex[:16],
            "post_reveal_revision_of": revises_record_id,
            "reviewer_id": record.get("reviewer_id") or self.reviewer_id,
            "review_saved_at": utc_now_iso(),
        }
        event = self.log_event(payload)
        self.regenerate_live_artifacts()
        return event

    def record_diagnostics_reveal(self, case_id: str) -> dict[str, Any]:
        event = self.log_event(
            {
                "event": "diagnostics_reveal",
                "case_id": case_id,
                "reviewer_id": self.reviewer_id,
                "after_save": self.case_state(case_id).second_pass_saved,
            }
        )
        self.regenerate_live_artifacts()
        return event

    def record_routing_change(
        self, case_id: str, *, from_route: str, to_grain: str, reason: str
    ) -> dict[str, Any]:
        return self.log_event(
            {
                "event": "routing_change",
                "case_id": case_id,
                "reviewer_id": self.reviewer_id,
                "from_route": from_route,
                "to_grain": to_grain,
                "reason": reason,
                "stored_as_new_human_judgment": True,
            }
        )

    def record_child_case_request(
        self, parent_case_id: str, *, child_task_type: str, detail: str
    ) -> dict[str, Any]:
        return self.log_event(
            {
                "event": "child_case_request",
                "case_id": parent_case_id,
                "reviewer_id": self.reviewer_id,
                "child_task_type": child_task_type,
                "detail": detail,
                "generation_reason": "second_pass_reviewer_reclassification",
            }
        )

    def record_interaction(
        self, case_id: str, *, kind: str, detail: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = ("panel_view", "candidate_filter_applied", "adjacent_context_view")
        if kind not in allowed:
            raise ValueError(f"unknown interaction kind: {kind}")
        return self.log_event(
            {
                "event": kind,
                "case_id": case_id,
                "reviewer_id": self.reviewer_id,
                **detail,
            }
        )

    def save_adjudication(self, record: dict[str, Any]) -> dict[str, Any]:
        record = dict(record)
        case_id = record.get("case_id")
        state = self.case_state(case_id)
        if not state.second_pass_saved:
            raise BlindProtocolViolation(
                "adjudication requires a saved blind second pass"
            )
        resolution = record.get("resolution")
        if resolution not in ADJUDICATION_RESOLUTIONS:
            raise LabelValidationError(f"invalid adjudication resolution: {resolution}")
        if len((record.get("resolution_rationale") or "").strip()) < 8:
            raise LabelValidationError(
                "adjudication requires a substantive resolution rationale"
            )
        blind = state.blind_save or {}
        # resolved_* content is derived per resolution — an adjudication
        # never claims the rejected judgment as its own resolution.
        if resolution == "second_pass_upheld":
            record["resolved_case_grain"] = blind.get("case_grain")
            record["resolved_scheduled_judgment"] = blind.get("scheduled_judgment")
            record["resolved_candidate_key"] = blind.get("selected_candidate_key")
        elif resolution == "first_pass_upheld":
            first = self.first_pass.for_case(
                case_id, reviewer_id=self.reviewer_id, log=False
            )
            record["resolved_case_grain"] = "single_trip_candidate"
            record["resolved_scheduled_judgment"] = FIRST_PASS_LABEL_MAP.get(
                first["raw_primary_label"]
            )
            record["resolved_candidate_key"] = first.get("raw_selected_candidate_id")
        elif resolution == "new_judgment":
            key = record.get("resolved_candidate_key")
            if key is not None:
                self._validate_key_membership(
                    case_id, key, slot="adjudicated candidate key"
                )
            if record.get("resolved_case_grain") not in CASE_GRAINS:
                raise LabelValidationError(
                    "new_judgment adjudication requires a resolved case grain"
                )
        else:  # deferred / not_evaluable_at_current_grain
            record["resolved_case_grain"] = None
            record["resolved_scheduled_judgment"] = None
            record["resolved_candidate_key"] = None
        payload = {
            **record,
            "event": "adjudication_save",
            "record_id": uuid.uuid4().hex[:16],
            "adjudicated_by": record.get("adjudicated_by") or self.reviewer_id,
            "adjudicated_at": utc_now_iso(),
            "first_pass_record_ref": "first_pass_labels_v1.parquet",
            "second_pass_record_ref": blind.get("record_id"),
        }
        event = self.log_event(payload)
        self.regenerate_live_artifacts()
        return event

    # ------------------------------------------------------- derived artifacts

    def _live_meta(self, artifact: str, *, grain: str, key, rows_payload) -> dict:
        manifest = json.loads(
            (self.run_dir / "protocol_freeze_manifest.json").read_text(encoding="utf-8")
        )
        from .labeling_v2_artifacts import base_metadata

        return base_metadata(
            artifact,
            grain=grain,
            key=key,
            contains_human_labels=True,
            privacy="restricted-local",
            logical_checksum=logical_sha256(rows_payload),
            input_hashes={},
            source_tree_sha256=manifest.get("source_tree_sha256", ""),
            code_revision=manifest.get("code_revision"),
            generated_at=utc_now_iso(),
            generated_time_is_wall_clock=True,
            generation_time_basis="live_label_zone_update_wall_clock",
        )

    def second_pass_rows(self) -> list[dict[str, Any]]:
        rows = []
        for state in sorted(self.all_states().values(), key=lambda s: s.case_id):
            if state.blind_save is None:
                continue
            event = state.blind_save
            row = {column: event.get(column) for column in SECOND_PASS_COLUMNS}
            for column in (
                "evidence_flags",
                "panels_viewed",
                "candidate_filters_applied",
                "ordered_candidate_sequence",
                "split_intervals_rel_s",
                "boundary_time_interval_rel_s",
                "quality_flags",
            ):
                if row.get(column) is not None and not isinstance(row[column], str):
                    row[column] = json.dumps(
                        row[column], ensure_ascii=False, sort_keys=True
                    )
            for column in (
                "is_child_case",
                "selected_explicit_null_polarity",
                "adjacent_context_used",
                "diagnostics_revealed_before_save",
                "diagnostics_revealed_after_save",
                "first_pass_hidden",
                "dual_role",
                "scheduled_match_applicable",
            ):
                if row.get(column) is not None:
                    row[column] = bool(row[column])
            rows.append(row)
        return rows

    def adjudication_rows(self) -> list[dict[str, Any]]:
        rows = []
        for state in sorted(self.all_states().values(), key=lambda s: s.case_id):
            for event in state.adjudications:
                rows.append(
                    {column: event.get(column) for column in ADJUDICATION_COLUMNS}
                )
        return rows

    def queue_rows(self) -> list[dict[str, Any]]:
        # Sorted by case id, never by audit priority: the audit's ranking
        # and reason codes derive from first-pass findings and stay masked
        # until the case's own blind save exists.
        states = self.all_states()
        rows: list[dict[str, Any]] = []
        for routing_row in self.routing.sort("case_id").iter_rows(named=True):
            case_id = routing_row["case_id"]
            state = states[case_id]
            saved = state.second_pass_saved
            rows.append(
                {
                    "case_id": case_id,
                    "parent_case_id": None,
                    "queue_rank": routing_row["queue_rank"] if saved else None,
                    "initial_task_route": (
                        routing_row["initial_task_route"] if saved else None
                    ),
                    "review_states": sorted(state.states),
                    "second_pass_saved": saved,
                    "comparison_outcome": (state.comparison or {}).get("outcome"),
                    "requires_adjudication": "REQUIRES_ADJUDICATION" in state.states,
                    "adjudicated": "ADJUDICATED" in state.states,
                    "audit_reason_codes": (
                        routing_row["audit_reason_codes"] if saved else None
                    ),
                    "automatic_final_label": False,
                }
            )
        for child_row in self.children.sort("child_case_id").iter_rows(named=True):
            child_id = child_row["child_case_id"]
            state = states[child_id]
            rows.append(
                {
                    "case_id": child_id,
                    "parent_case_id": child_row["parent_case_id"],
                    "queue_rank": None,
                    "initial_task_route": child_row["child_task_type"],
                    "review_states": sorted(state.states),
                    "second_pass_saved": state.second_pass_saved,
                    "comparison_outcome": None,
                    "requires_adjudication": "REQUIRES_ADJUDICATION" in state.states,
                    "adjudicated": "ADJUDICATED" in state.states,
                    "audit_reason_codes": None,
                    "automatic_final_label": False,
                }
            )
        return rows

    def regenerate_live_artifacts(self) -> None:
        from .labeling_v2_readiness import build_live_reports

        second_rows = self.second_pass_rows()
        schema = {name: pl.String for name in SECOND_PASS_COLUMNS}
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
            schema[name] = pl.Boolean
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
            schema[name] = pl.Int64
        self.updater.rewrite_parquet(
            "second_pass_labels_v2.parquet",
            second_rows,
            self._live_meta(
                "second_pass_labels_v2.parquet",
                grain="one blind second-pass human judgment per case",
                key=["case_id", "reviewer_id", "review_round"],
                rows_payload=second_rows,
            ),
            schema=schema,
        )
        adjudication_rows = self.adjudication_rows()
        adj_schema = {name: pl.String for name in ADJUDICATION_COLUMNS}
        self.updater.rewrite_parquet(
            "adjudicated_labels_v2.parquet",
            adjudication_rows,
            self._live_meta(
                "adjudicated_labels_v2.parquet",
                grain="one explicit adjudication record; overwrites neither pass",
                key=["case_id", "record_id"],
                rows_payload=adjudication_rows,
            ),
            schema=adj_schema,
        )
        queue_rows = self.queue_rows()
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
        self.updater.rewrite_csv(
            "adjudication_queue_v3.csv",
            queue_rows,
            queue_columns,
            self._live_meta(
                "adjudication_queue_v3.csv",
                grain="one queue entry per parent or child case; no winner chosen",
                key=["case_id"],
                rows_payload=queue_rows,
            ),
        )
        reports = build_live_reports(self)
        for name, payload in reports.items():
            self.updater.rewrite_json(
                name,
                payload,
                self._live_meta(
                    name,
                    grain="one aggregate live report",
                    key=["report"],
                    rows_payload=payload,
                ),
            )

    # ----------------------------------------------------------------- guards

    def assert_first_pass_untouched(self, expected_hashes: dict[str, str]) -> bool:
        for path_text, expected in expected_hashes.items():
            path = Path(path_text)
            if not path.exists() or sha256_file(path) != expected:
                raise LabelingV2ArtifactError(
                    f"first-pass input changed or missing: {path}"
                )
        return True


def _mode_is_owner_only(path: Path) -> bool:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    return mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
