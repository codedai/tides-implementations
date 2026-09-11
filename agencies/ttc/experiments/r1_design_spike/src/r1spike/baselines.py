"""B0 / B1 baselines (B2 is the resolution engine itself).

B0 — legacy artifact-conditioned forced baseline. Reproduces the legacy
project's join mechanics (backward as-of on a precomputed ``TripID``
with no tolerance) so its failure modes are measurable. It runs ONLY if
the legacy clean artifacts are locally present and hash-verified; they
are upstream inputs this repository does not contain. B0 agreement is
never treated as correctness.

B1 — bounded spatiotemporal baseline: explicit hard filters, no scores;
accepts only a unique hard-valid attachment, otherwise abstains.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from .candidate_generation import ApcRecord
from .segmentation_baselines import Segment
from .temporal_features import interval_gap_s

LEGACY_ARTIFACT_NAMES = (
    "ttc_avl_seg_clean_data.csv",
    "ttc_apc_clean_data.csv",
)


@dataclass(frozen=True)
class B0Availability:
    available: bool
    checked_paths: tuple[str, ...]
    sha256: dict[str, str]


def check_b0_availability(search_dirs: list[Path]) -> B0Availability:
    found: dict[str, Path] = {}
    checked: list[str] = []
    for base in search_dirs:
        for name in LEGACY_ARTIFACT_NAMES:
            p = base / name
            checked.append(str(p))
            if p.exists():
                found[name] = p
    if set(found) != set(LEGACY_ARTIFACT_NAMES):
        return B0Availability(False, tuple(checked), {})
    hashes = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in sorted(found.items())
    }
    return B0Availability(True, tuple(checked), hashes)


def b0_join(avl: pl.DataFrame, apc: pl.DataFrame) -> pl.DataFrame:
    """The legacy join, faithfully unsafe: backward as-of within TripID,
    no tolerance, missing load coerced to integer zero (``pc``).

    Columns: avl needs (TripID, Message_DateTime); apc needs
    (TripID, StopArrivalTime, Arrload). Used on synthetic fixtures in
    tests and on the real legacy artifacts only when available.
    """
    joined = avl.sort("Message_DateTime").join_asof(
        apc.sort("StopArrivalTime"),
        by="TripID",
        left_on="Message_DateTime",
        right_on="StopArrivalTime",
        strategy="backward",
    )
    return joined.with_columns(
        pl.col("Arrload").fill_null(0.0).cast(pl.Int64).alias("pc")
    )


@dataclass(frozen=True)
class B1Decision:
    apc_source_record_id: str
    status: str  # accepted_unique | abstain_none | abstain_multiple
    segment_id: str | None
    n_surviving: int


def b1_resolve(
    record: ApcRecord,
    segments: list[Segment],
    *,
    max_gap_s: int,
    stop_mapping_unique: bool,
    route_labels_by_segment: dict[str, str | None],
) -> B1Decision:
    """Bounded spatiotemporal baseline for one record.

    Hard filters only: same vehicle namespace; interval gap within the
    hard bound under at least one fold; matching route label; unique
    stop mapping required. Accepts only a unique survivor.
    """
    if not stop_mapping_unique or not record.arrival_candidates:
        return B1Decision(record.record_id, "abstain_none", None, 0)
    survivors: list[str] = []
    for seg in sorted(segments, key=lambda s: s.segment_id):
        if seg.vehicle != record.vehicle or seg.time_uncertain:
            continue
        if route_labels_by_segment.get(seg.segment_id) != record.route_label:
            continue
        gaps = []
        for fold, a0 in record.arrival_candidates:
            dep = [e for f, e in record.departure_candidates if f == fold]
            a1 = max(dep) if dep else a0
            gaps.append(interval_gap_s(a0, a1, seg.start_instant_s, seg.end_instant_s))
        if min(gaps) <= max_gap_s:
            survivors.append(seg.segment_id)
    if len(survivors) == 1:
        return B1Decision(record.record_id, "accepted_unique", survivors[0], 1)
    if not survivors:
        return B1Decision(record.record_id, "abstain_none", None, 0)
    return B1Decision(record.record_id, "abstain_multiple", None, len(survivors))


def b1_b2_agreement(
    b1: dict[str, B1Decision],
    b2_dispositions: dict[str, str],
    b2_selected_segment: dict[str, str | None],
) -> dict:
    """B1/B2 agreement report. Never a correctness claim."""
    both_accept_same = 0
    both_accept_differ = 0
    b1_only = 0
    b2_only = 0
    both_abstain = 0
    for rid in sorted(b1):
        d1 = b1[rid]
        b2_status = b2_dispositions.get(rid)
        b2_accepted = b2_status == "accepted"
        if d1.status == "accepted_unique" and b2_accepted:
            if b2_selected_segment.get(rid) == d1.segment_id:
                both_accept_same += 1
            else:
                both_accept_differ += 1
        elif d1.status == "accepted_unique":
            b1_only += 1
        elif b2_accepted:
            b2_only += 1
        else:
            both_abstain += 1
    return {
        "both_accept_same_segment": both_accept_same,
        "both_accept_different_segment": both_accept_differ,
        "b1_accept_b2_abstain": b1_only,
        "b2_accept_b1_abstain": b2_only,
        "both_abstain": both_abstain,
    }
