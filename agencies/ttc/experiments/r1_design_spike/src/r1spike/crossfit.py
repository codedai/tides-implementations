"""Deterministic APC evidence decoupling (Spike 02R section 9).

Cross-fitting: within each vehicle, APC source records are assigned to
fold A or fold B by a stable hash of their COLLISION GROUP — records
that plausibly describe the same physical visit (same vehicle, same
stop code, arrival within one aggregation window) always land in the
same fold, so a suspected duplicate cannot confirm itself across folds.

The fold hash is independent of every value being evaluated: it uses
only (vehicle, stop_code, coarse arrival bucket), never scores,
boundaries, or outcomes.

Cross-fitted evidence removes direct same-record reuse ONLY. It is not
independent ground truth and is never described as such.
"""

from __future__ import annotations

import hashlib

from .candidate_generation import ApcRecord

FOLD_BUCKET_S = 600  # aggregation window for suspected same-visit groups


def _arrival(record: ApcRecord) -> int:
    return record.arrival_candidates[0][1] if record.arrival_candidates else 0


def collision_group(record: ApcRecord) -> str:
    """Coarse bucket label (diagnostic); fold assignment itself uses
    connected components so near-boundary same-visit suspects can never
    span folds (adversarial-review repair)."""
    return f"{record.vehicle}|{record.stop_code}|{_arrival(record) // FOLD_BUCKET_S}"


def _component_groups(records: list[ApcRecord]) -> dict[str, str]:
    """record_id -> component group id: records of the same (vehicle,
    stop_code) chain into one group whenever consecutive arrivals are
    within FOLD_BUCKET_S. Value-independent and deterministic."""
    by_key: dict[tuple, list[ApcRecord]] = {}
    for r in records:
        by_key.setdefault((r.vehicle, r.stop_code), []).append(r)
    out: dict[str, str] = {}
    for key in sorted(by_key, key=str):
        chain = sorted(by_key[key], key=lambda r: (_arrival(r), r.record_id))
        group_anchor = None
        prev_arrival = None
        for r in chain:
            a = _arrival(r)
            if group_anchor is None or (
                prev_arrival is not None and a - prev_arrival > FOLD_BUCKET_S
            ):
                group_anchor = f"{key[0]}|{key[1]}|{a}"
            out[r.record_id] = group_anchor
            prev_arrival = a
    return out


def fold_of(record: ApcRecord) -> str:
    """Fold of a SINGLE record in isolation (its own group)."""
    anchor = f"{record.vehicle}|{record.stop_code}|{_arrival(record)}"
    digest = hashlib.sha256(anchor.encode()).hexdigest()
    return "A" if int(digest[:8], 16) % 2 == 0 else "B"


def assign_folds(records: list[ApcRecord]) -> dict[str, str]:
    """record_id -> fold. Deterministic, value-independent; suspected
    same-visit chains (union of within-window arrivals per vehicle+stop)
    always share one fold."""
    groups = _component_groups(records)
    out: dict[str, str] = {}
    for r in records:
        digest = hashlib.sha256(groups[r.record_id].encode()).hexdigest()
        out[r.record_id] = "A" if int(digest[:8], 16) % 2 == 0 else "B"
    return out


def split_records(
    records: list[ApcRecord], folds: dict[str, str]
) -> tuple[list[ApcRecord], list[ApcRecord]]:
    a = [r for r in records if folds[r.record_id] == "A"]
    b = [r for r in records if folds[r.record_id] == "B"]
    return a, b


def fold_rows(records: list[ApcRecord], folds: dict[str, str]) -> list[dict]:
    return [
        {
            "apc_source_record_id": r.record_id,
            "collision_group": collision_group(r),
            "fold": folds[r.record_id],
        }
        for r in sorted(records, key=lambda r: r.record_id)
    ]


def decision_change_matrix(in_sample: dict[str, str], crossfit: dict[str, str]) -> dict:
    """status-by-status transition counts between two decision maps."""
    matrix: dict[str, int] = {}
    for key in sorted(set(in_sample) | set(crossfit)):
        a = in_sample.get(key, "absent")
        b = crossfit.get(key, "absent")
        matrix[f"{a} -> {b}"] = matrix.get(f"{a} -> {b}", 0) + 1
    changed = sum(
        n for k, n in matrix.items() if k.split(" -> ")[0] != k.split(" -> ")[1]
    )
    return {
        "transitions": matrix,
        "changed": changed,
        "total": len(set(in_sample) | set(crossfit)),
    }
