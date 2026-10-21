"""Independent human review notes, never merge assignments or training labels."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

VERDICTS = {"accepted", "problem", "uncertain", "unreviewed"}


class RevisionConflict(ValueError):
    """Another tab saved this review after the caller loaded it."""


class ReviewStore:
    def __init__(self, directory: Path, run_key: str):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "reviews.sqlite3"
        self.run_key = run_key
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS reviews (
                    run_key TEXT NOT NULL, method TEXT NOT NULL,
                    vehicle TEXT NOT NULL, case_id TEXT NOT NULL,
                    verdict TEXT NOT NULL, notes TEXT NOT NULL,
                    revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_key, method, vehicle, case_id)
                );
                CREATE TABLE IF NOT EXISTS review_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_key TEXT NOT NULL, payload TEXT NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def catalog(self, method: str, vehicle: str) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reviews WHERE run_key=? AND method=? AND vehicle=?",
                (self.run_key, method, vehicle),
            ).fetchall()
        return {r["case_id"]: dict(r) for r in rows}

    def get(self, method: str, vehicle: str, case_id: str):
        return self.catalog(method, vehicle).get(case_id)

    def save(self, method, vehicle, case_id, verdict, notes, revision):
        if verdict not in VERDICTS:
            raise ValueError("Unknown review status")
        if not isinstance(notes, str) or len(notes) > 10000:
            raise ValueError("Notes must be text of at most 10000 characters")
        if type(revision) is not int or revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        key = (self.run_key, method, vehicle, case_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT revision FROM reviews "
                "WHERE run_key=? AND method=? AND vehicle=? AND case_id=?",
                key,
            ).fetchone()
            current_revision = previous["revision"] if previous else 0
            if current_revision != revision:
                raise RevisionConflict(
                    "Another page has updated this review; reload before saving again"
                )
            saved = {
                "run_key": self.run_key,
                "method": method,
                "vehicle": vehicle,
                "case_id": case_id,
                "verdict": verdict,
                "notes": notes,
                "revision": revision + 1,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            conn.execute(
                "INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_key,method,vehicle,case_id) DO UPDATE SET "
                "verdict=excluded.verdict, notes=excluded.notes, "
                "revision=excluded.revision, updated_at=excluded.updated_at",
                tuple(saved.values()),
            )
            conn.execute(
                "INSERT INTO review_events(run_key,payload) VALUES (?,?)",
                (self.run_key, json.dumps(saved, ensure_ascii=False)),
            )
        return saved

    def export(self):
        with self._connect() as conn:
            reviews = conn.execute(
                "SELECT * FROM reviews WHERE run_key=? ORDER BY method,vehicle,case_id",
                (self.run_key,),
            ).fetchall()
            events = conn.execute(
                "SELECT payload FROM review_events WHERE run_key=? ORDER BY id",
                (self.run_key,),
            ).fetchall()
        return {
            "format": "merge-review-notes-v1",
            "run_key": self.run_key,
            "exported_at": datetime.now(UTC).isoformat(),
            "unblinded": True,
            "ground_truth": False,
            "warning": (
                "Human review after viewing model diagnostics; not independent "
                "ground truth, and the original association is unchanged"
            ),
            "reviews": [dict(r) for r in reviews],
            "events": [json.loads(r["payload"]) for r in events],
        }
