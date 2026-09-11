"""Auditable, JSON-safe episode summaries and scoring-feature identity."""

import functools
import hashlib
import json
import math
from pathlib import Path

from . import SCORER


@functools.lru_cache(maxsize=1)
def _code_digest():
    folder = Path(__file__).parent
    paths = sorted(folder.glob("*.py")) + [
        folder.parent / (name + ".py")
        for name in (
            "canonical",
            "projection",
            "off_reference",
            "retrieval",
            "windows",
            "partial",
        )
    ]
    return {
        str(p.relative_to(folder.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
    }


def scorer_fingerprint(config):
    # Do not include obsolete temporary GTFS sampling knobs: they are not features.
    parameters = {
        k: v
        for k, v in config.items()
        if k.startswith(
            (
                "continuous_",
                "projection_",
                "canonical_",
                "off_reference_",
                "window_",
                "segmentation_",
            )
        )
        and k != "continuous_scorer_fingerprint"
    }
    parameters["max_projection_distance_m"] = config.get(
        "max_projection_distance_m", 100
    )
    return hashlib.sha256(
        json.dumps(
            {"scorer": SCORER, "code": _code_digest(), "features": parameters},
            sort_keys=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def validate_result(result):
    """Publication invariant: components, ownership and coverage denominators."""
    json.dumps(result, allow_nan=False)
    for candidate in result.get("all_candidates", []):
        if not math.isclose(
            candidate["score"],
            sum(candidate["score_components"].values()),
            abs_tol=1e-9,
        ):
            raise ValueError("Continuous score components do not sum to score")
        for key, value in candidate["score_components"].items():
            if not math.isclose(
                value,
                candidate["score_components_unweighted"][key]
                * candidate["score_component_weights"][key],
                abs_tol=1e-9,
            ):
                raise ValueError("Continuous raw score times weight is inconsistent")
        coverage = candidate["coverage_metrics"]
        for unit in ("point_ratio", "interval_ratio", "duration_ratio"):
            if not math.isclose(
                coverage["projection"][unit] * coverage["schedule"][unit],
                coverage["end_to_end"][unit],
                abs_tol=1e-9,
            ):
                raise ValueError("Three-level coverage identity failed")
    if len(result.get("candidates", [])) > 3:
        raise ValueError("Credible set exceeds reportable limit")
    return {
        "status": "PASS",
        "score_components_consistent": True,
        "coverage_identity_valid": True,
    }


def json_ready(result):
    """Normalize interval tuples to JSON arrays before immutable round-trip checks."""
    return json.loads(json.dumps(result, allow_nan=False, separators=(",", ":")))


def shadow_row(result, summary, canonical, mode):
    candidates = result.get("all_candidates", [])
    best = candidates[0] if candidates else {}
    boundary = result.get("whole_trip", {}).get("boundary_models", {})
    process = best.get("whole_alignment", {}).get("delay_process", [])
    schedules = {str(trip["trip_id"]): trip for trip in canonical["trips"]}
    serialized = []
    for candidate in candidates:
        native = schedules.get(str(candidate.get("trip_id")), {}).get("scheduled", [])
        row = {
            k: candidate.get(k)
            for k in (
                "candidate_id",
                "trip_id",
                "feed_version",
                "service_date",
                "pattern_id",
                "score",
                "probability",
                "calibrated",
                "estimated_delay_s",
                "reference_id",
                "block_id",
                "score_components",
                "coverage_metrics",
                "quality_eligible",
            )
        }
        row["scheduled_start_time_s"] = native[0].get("departure_t") if native else None
        row["scheduled_end_time_s"] = native[-1].get("arrival_t") if native else None
        serialized.append(row)
    return {
        "case_id": summary["id"],
        "feed_version": canonical.get("feed_version"),
        "route_id": str(canonical.get("route_id")),
        "service_date": summary["date"],
        "episode_id": summary["id"],
        "matching_mode": mode,
        "vehicle_id": summary.get("vehicle_label"),
        "status": result["status"],
        "scorer": result.get("scorer", "legacy_dtw"),
        "scorer_fingerprint": result.get("scorer_fingerprint"),
        "all_candidates": serialized,
        "candidates": [
            {
                k: c.get(k)
                for k in (
                    "trip_id",
                    "candidate_id",
                    "score",
                    "probability",
                    "calibrated",
                )
            }
            for c in result.get("candidates", [])
        ],
        "retrieved_trip_ids": [
            t.get("trip_id") for t in result.get("retrieval", {}).get("trips", [])
        ],
        "start_time_s": summary.get("start"),
        "end_time_s": summary.get("end"),
        "start_progress_m": process[0]["s_m"]
        if process and "s_m" in process[0]
        else None,
        "end_progress_m": process[-1]["s_m"]
        if process and "s_m" in process[-1]
        else None,
        "estimated_delay_s": best.get("estimated_delay_s"),
        "valid_projection_point_ratio": best.get("valid_projection_point_ratio"),
        "local_headway_s": sorted(
            p["headway_s"] for p in process if p.get("headway_s")
        )[len(process) // 2]
        if process and all(p.get("headway_s") for p in process)
        else None,
        "shape_structure": "single_reference"
        if len(canonical.get("references", [])) == 1
        else "multiple_references",
        "data_quality": "adequate"
        if best.get("quality_eligible")
        else "limited_or_contradictory",
        "runtime_s": result.get("diagnostics", {}).get("runtime_s"),
        "boundary_times_s": [boundary["change_time"]]
        if boundary.get("boundary_uncertain")
        and boundary.get("change_time") is not None
        else [],
        "reasons": result.get("reasons", []),
    }
