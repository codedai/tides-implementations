"""Label persistence.

- ``scheduled_trip_labels.csv``: one row per (case_id, reviewer) — the
  current judgment, rewritten atomically on every save.
- ``scheduled_trip_labels.jsonl``: append-only audit log — one object per
  save event (including re-saves), never rewritten.
- ``drafts.json``: unsaved form state per case, so navigation loses nothing.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX: degrade to no inter-process locking
    fcntl = None

log = logging.getLogger(__name__)

PRIMARY_LABELS = [
    "MATCH_TOP_CANDIDATE",
    "MATCH_OTHER_CANDIDATE",
    "NO_SCHEDULED_MATCH",
    "AMBIGUOUS",
    "INSUFFICIENT_EVIDENCE",
]

FRAGMENT_IMPRESSIONS = [
    "",
    "trip_core",
    "arrival_fragment",
    "departure_fragment",
    "turnaround",
    "stitch_prev",
    "stitch_next",
    "ambiguous",
]

RATIONALE_FLAGS = [
    "time_fit_good",
    "spatial_fit_good",
    "sequence_fit_good",
    "candidate_conflict",
    "fragment_like",
    "terminal_behavior",
    "insufficient_context",
    "data_quality_concern",
]

CSV_COLUMNS = [
    "case_id",
    "reviewer",
    "label",
    "selected_candidate_letter",
    "selected_candidate_id",
    "confidence",
    "fragment_impression",
    "rationale",
    "notes",
    "diagnostics_revealed_before_save",
    "created_at",
    "modified_at",
    "session_id",
    "app_version",
    "chunk_id",
    "cases_file",
    "flagged_for_revisit",
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int_or_none(v) -> int | None:
    """Tolerate '3', '3.0', '', None, junk — hand-edited CSVs happen."""
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


@dataclass
class LabelRecord:
    case_id: str
    reviewer: str
    label: str
    selected_candidate_letter: str | None = None
    selected_candidate_id: str | None = None
    confidence: int | None = None
    fragment_impression: str | None = None
    rationale: list[str] = field(default_factory=list)
    notes: str = ""
    diagnostics_revealed_before_save: bool = False
    created_at: str = ""
    modified_at: str = ""
    session_id: str = ""
    app_version: str = ""
    chunk_id: str | None = None
    cases_file: str | None = None
    flagged_for_revisit: bool = False

    def to_csv_row(self) -> dict:
        d = asdict(self)
        d["rationale"] = "|".join(self.rationale)
        return {k: d.get(k, "") for k in CSV_COLUMNS}

    @classmethod
    def from_csv_row(cls, row: dict) -> "LabelRecord":
        return cls(
            case_id=row.get("case_id", ""),
            reviewer=row.get("reviewer", ""),
            label=row.get("label", ""),
            selected_candidate_letter=row.get("selected_candidate_letter") or None,
            selected_candidate_id=row.get("selected_candidate_id") or None,
            confidence=_int_or_none(row.get("confidence")),
            fragment_impression=row.get("fragment_impression") or None,
            rationale=[r for r in (row.get("rationale") or "").split("|") if r],
            notes=row.get("notes", ""),
            diagnostics_revealed_before_save=(
                str(row.get("diagnostics_revealed_before_save", "")).lower() == "true"
            ),
            created_at=row.get("created_at", ""),
            modified_at=row.get("modified_at", ""),
            session_id=row.get("session_id", ""),
            app_version=row.get("app_version", ""),
            chunk_id=row.get("chunk_id") or None,
            cases_file=row.get("cases_file") or None,
            flagged_for_revisit=(
                str(row.get("flagged_for_revisit", "")).lower() == "true"
            ),
        )


class LabelStore:
    def __init__(self, labels_dir: str | Path):
        self.dir = Path(labels_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.dir / "scheduled_trip_labels.csv"
        self.jsonl_path = self.dir / "scheduled_trip_labels.jsonl"
        self.drafts_path = self.dir / "drafts.json"

    # ---- locking -----------------------------------------------------------

    @contextmanager
    def _locked(self):
        """Exclusive inter-process lock around read-modify-write cycles, so
        two tabs/reviewers on the same labels dir cannot lose saves."""
        if fcntl is None:
            yield
            return
        with (self.dir / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    # ---- current labels ----------------------------------------------------

    def load_all(self) -> dict[tuple[str, str], LabelRecord]:
        """Keyed by (case_id, reviewer). Malformed rows are skipped with a
        warning, never fatal."""
        out: dict[tuple[str, str], LabelRecord] = {}
        if not self.csv_path.exists():
            return out
        with self.csv_path.open(newline="") as f:
            for i, row in enumerate(csv.DictReader(f)):
                try:
                    rec = LabelRecord.from_csv_row(row)
                except Exception as exc:
                    log.warning("skipping malformed label row %d: %s", i, exc)
                    continue
                if rec.case_id:
                    out[(rec.case_id, rec.reviewer)] = rec
        return out

    def get(self, case_id: str, reviewer: str) -> LabelRecord | None:
        return self.load_all().get((case_id, reviewer))

    def save(self, rec: LabelRecord) -> LabelRecord:
        """Upsert into the CSV and append a save event to the JSONL log."""
        now = _utcnow()
        with self._locked():
            existing = self.load_all()
            prior = existing.get((rec.case_id, rec.reviewer))
            rec.created_at = prior.created_at if prior and prior.created_at else now
            rec.modified_at = now
            existing[(rec.case_id, rec.reviewer)] = rec
            self._write_csv(existing)

            event = asdict(rec)
            event["event"] = "resave" if prior else "save"
            event["event_at"] = now
            with self.jsonl_path.open("a") as f:
                f.write(json.dumps(event) + "\n")
            # the saved record supersedes any draft
            self._delete_draft(rec.case_id, rec.reviewer)
        return rec

    # ---- diagnostics-reveal audit ------------------------------------------

    def record_reveal(self, case_id: str, reviewer: str) -> None:
        """Reveals are audit events in their own right, persisted so blinding
        state survives page reloads and sessions."""
        with self._locked():
            with self.jsonl_path.open("a") as f:
                f.write(
                    json.dumps(
                        {
                            "event": "reveal",
                            "case_id": case_id,
                            "reviewer": reviewer,
                            "event_at": _utcnow(),
                        }
                    )
                    + "\n"
                )

    def reveals(self) -> set[tuple[str, str]]:
        """(case_id, reviewer) pairs with a persisted reveal event."""
        out: set[tuple[str, str]] = set()
        if not self.jsonl_path.exists():
            return out
        for line in self.jsonl_path.read_text().splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("event") == "reveal":
                out.add((str(obj.get("case_id")), str(obj.get("reviewer"))))
        return out

    def _write_csv(self, records: dict[tuple[str, str], LabelRecord]) -> None:
        # atomic rewrite so a crash mid-save never truncates the file
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".csv.tmp")
        try:
            with os.fdopen(fd, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                w.writeheader()
                for key in sorted(records):
                    w.writerow(records[key].to_csv_row())
            os.replace(tmp, self.csv_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ---- drafts ------------------------------------------------------------

    def load_drafts(self) -> dict:
        if not self.drafts_path.exists():
            return {}
        try:
            data = json.loads(self.drafts_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_draft(self, case_id: str, reviewer: str, form_state: dict) -> None:
        drafts = self.load_drafts()
        drafts[f"{case_id}::{reviewer}"] = {
            "form": form_state,
            "saved_at": _utcnow(),
        }
        self._write_drafts(drafts)

    def get_draft(self, case_id: str, reviewer: str) -> dict | None:
        entry = self.load_drafts().get(f"{case_id}::{reviewer}")
        if not isinstance(entry, dict):
            return None
        form = entry.get("form")
        return form if isinstance(form, dict) else None

    def _delete_draft(self, case_id: str, reviewer: str) -> None:
        drafts = self.load_drafts()
        if drafts.pop(f"{case_id}::{reviewer}", None) is not None:
            self._write_drafts(drafts)

    def _write_drafts(self, drafts: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(drafts, f, indent=1)
            os.replace(tmp, self.drafts_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
