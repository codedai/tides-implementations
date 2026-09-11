"""Label schema, deterministic review packet, and factorial
pre-registration (Spike 02R sections 20-21).

No human ground truth exists and NONE is invented here: the packet and
schema prepare labeling; correctness metrics remain BLOCKED until a
reviewed label artifact exists. Repository-safe rows use pseudonymous
case ids and relative times only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

LABEL_SCHEMA_VERSION = "xlabel-1"

LABEL_SCHEMA = {
    "version": LABEL_SCHEMA_VERSION,
    "reviewed_by_human": False,
    "boundary_labels": {
        "fields": {
            "case_id": "pseudonymous id",
            "decision": ["split", "no_split", "ambiguous"],
            "boundary_time_range_rel_s": "[lo, hi] relative to case epoch",
            "confidence": ["high", "medium", "low"],
            "evidence_shown": "list of evidence families shown to reviewer",
            "censoring": ["none", "left", "right"],
        }
    },
    "apc_attachment_labels": {
        "fields": {
            "case_id": "pseudonymous id",
            "decision": [
                "owner_chunk",
                "no_valid_owner",
                "ambiguous",
                "coverage_missing",
            ],
            "owner_case_ref": "pseudonymous chunk ref or null",
        }
    },
    "scheduled_trip_labels": {
        "fields": {
            "case_id": "pseudonymous id",
            "candidate_key": "date|feed|route|trip|polarity or null",
            "decision": ["match", "no_scheduled_match", "ambiguous"],
            "confidence": ["high", "medium", "low"],
        }
    },
    "fragment_labels": {
        "fields": {
            "case_id": "pseudonymous id",
            "role": [
                "trip_core",
                "turnaround",
                "arrival_fragment",
                "departure_fragment",
                "independent_trip",
                "unresolved",
            ],
            "stitch_with_previous": [True, False, "ambiguous"],
            "stitch_with_next": [True, False, "ambiguous"],
        }
    },
}

STRATA = [
    "route_29",
    "route_929",
    "peak",
    "off_peak",
    "terminal",
    "mid_route",
    "close_headway",
    "apc_rich",
    "apc_sparse",
    "avl_only",
    "short_turn",
    "cross_midnight",
    "dst",
    "month_boundary",
    "fragment",
    "no_candidate",
    "unique_survivor",
    "stale_or_high_residual",
    "v1r_v2r_disagreement",
]


def case_id(real_id: str, salt: str) -> str:
    return "C-" + hashlib.sha256(f"{salt}|{real_id}".encode()).hexdigest()[:8]


def redact_time(epoch_s: int, case_epoch_s: int) -> int:
    """Relative seconds from the case epoch (repository-safe)."""
    return epoch_s - case_epoch_s


def build_packet_rows(cases: list[dict], *, salt: str) -> tuple[list[dict], list[dict]]:
    """(restricted_rows, redacted_rows).

    Each input case dict needs: real_id, stratum, kind, epoch_s,
    events = [(real_ref, epoch_s, description)], plus free-form
    restricted detail fields.
    """
    restricted: list[dict] = []
    redacted: list[dict] = []
    for case in sorted(cases, key=lambda c: (c["stratum"], c["real_id"])):
        cid = case_id(case["real_id"], salt)
        restricted.append(
            {
                "case_id": cid,
                "stratum": case["stratum"],
                "kind": case["kind"],
                "real_id": case["real_id"],
                "case_epoch_s": case["epoch_s"],
                "detail": json.dumps(case.get("detail", {}), default=str),
            }
        )
        redacted.append(
            {
                "case_id": cid,
                "stratum": case["stratum"],
                "kind": case["kind"],
                "n_events": len(case.get("events", [])),
                "span_rel_s": (
                    max((e[1] for e in case.get("events", [])), default=0)
                    - min((e[1] for e in case.get("events", [])), default=0)
                ),
                "summary": case.get("public_summary", ""),
            }
        )
    return restricted, redacted


def preregistration(
    *,
    label_artifact_hash: str | None,
    feature_registry_hash: str,
    config_hash: str,
    calibration_vehicle_days: list[str],
    holdout_vehicle_days: list[str],
) -> dict:
    """Frozen machine-readable factorial pre-registration. The split is
    by vehicle-day so correlated records cannot leak; the original
    three-vehicle development day is EXCLUDED from holdout by rule."""
    return {
        "preregistration_version": 1,
        "frozen_utc": dt.datetime.now(dt.UTC).isoformat(),
        "factors": {
            "segmentation": {
                "S0": "V0 coarse AVL activity runs",
                "S1": "V1R true AVL-only refinement",
                "S2": "V2R-STRUCTURAL APC-assisted refinement",
                "S3": "V2R-ACTIVITY APC-assisted refinement",
            },
            "evidence": {
                "E0": "interval/start/end only",
                "E1": "per-stop occurrence only",
                "E2": "terminal residual only",
                "E3": "per-stop + terminal",
            },
            "t_mid_s": [1200, 1800, 2700, 3600],
            "direction_weight": 0,
            "direction_note": "diagnostic ablation only; weight 0 in primary",
            "evidence_independence": ["in_sample", "cross_fitted"],
            "alignment": ["local_resolution", "revocable_anchor_align"],
            "confidence": {
                "diagnostic_unsafe": "survival margin only",
                "required": "evidence + survival margin",
            },
        },
        "pruning_rule": (
            "cells pruned only when logically redundant (declared before "
            "execution); never on observed success"
        ),
        "split": {
            "rule": "group split by vehicle-day; no vehicle-day in both sets",
            "calibration_vehicle_days": sorted(calibration_vehicle_days),
            "holdout_vehicle_days": sorted(holdout_vehicle_days),
            "development_day_excluded_from_holdout": True,
        },
        "metrics": {
            "primary_endpoint": (
                "held-out accepted-match precision at the pre-registered "
                "abstention profile (evidence+survival margins)"
            ),
            "secondary_endpoints": [
                "boundary split/merge precision and recall",
                "boundary-time error",
                "attachment precision/coverage/abstention",
                "unique-survivor rate",
                "fragment stitch precision/recall",
                "in-sample vs cross-fitted decision-change rate",
            ],
            "acceptance_procedure": (
                "thresholds frozen on calibration; holdout opened once; "
                "no threshold change after opening"
            ),
            "fail_closed_rule": (
                "if labels are absent or the kernel fails adversarial "
                "review, the result is NO RESULT — architecture verdict "
                "INCONCLUSIVE"
            ),
        },
        "label_artifact_sha256": label_artifact_hash,
        "labels_reviewed": label_artifact_hash is not None,
        "feature_registry_sha256": feature_registry_hash,
        "config_sha256": config_hash,
    }
