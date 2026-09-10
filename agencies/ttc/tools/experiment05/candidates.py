"""Stage 7: freeze 04c rankings before any one-to-one assignment starts."""

from __future__ import annotations

import copy
import math

from tools.merge_review.gtfs_matching.decision import apply_decision_guard
from tools.merge_review.gtfs_matching.integration import match_candidate_segment
from tools.merge_review.gtfs_matching.partial import MODES, partition_projection
from tools.merge_review.gtfs_matching.retrieval import trip_key

from .io import stable_id

HEAVY_CANDIDATE_FIELDS = {"scheduled_trajectory", "delay_process"}
AUDIT_ONLY_SOURCE_FIELDS = {
    "raw_payload",
    "audit_avg_otp",
    "avg_otp",
    "source_trip",
    "source_date",
    "off_route",
    "delayed",
    "run",
}


def scheduled_lookup(canonical):
    rows = []
    for trip in canonical["trips"]:
        instants = [
            row[field]
            for row in trip["scheduled"]
            for field in ("arrival_seconds", "departure_seconds")
            if row.get(field) is not None
        ]
        if not instants or min(instants) < 0:
            continue
        rows.append(
            {
                "trip_id_scheduled": trip["trip_id"],
                "candidate_id": trip_key(trip),
                "service_date": trip["service_date"],
                "route_id": trip["route_id"],
                "direction_id": trip["direction_id"],
                "gtfs_feed_version": trip["feed_version"],
                "scheduled_start_s": min(instants),
                "scheduled_end_s": max(instants),
                "reference_id": trip["reference_id"],
                "family_id": trip["family_id"],
                "canonical_valid": trip["canonical_valid"],
            }
        )
    return rows


def _projection(segment, observations):
    points, apc = [], []
    for row in observations:
        if row.get("trip_id_performed") != segment["trip_id_performed"]:
            continue
        if row["source"] == "avl":
            if (
                row.get("duplicate_of")
                or row.get("exclude_from_matching")
                or row.get("censored_tail")
            ):
                continue
            # OFF_REFERENCE evidence remains in the denominator; only confirmed
            # censored tails and exact duplicate pings are masked out here.
            point = {
                key: copy.deepcopy(value)
                for key, value in row.items()
                if key not in AUDIT_ONLY_SOURCE_FIELDS
            }
            points.append(point)
        elif row.get("apc_evidence_included"):
            evidence = {
                key: copy.deepcopy(value)
                for key, value in row.items()
                if key not in AUDIT_ONLY_SOURCE_FIELDS
            }
            if row.get("decision_interval_clip"):
                evidence.update(
                    arrival=row["interval_start"],
                    departure=row["interval_end"],
                    t=row["interval_start"],
                )
            apc.append(evidence)
    points.sort(key=lambda row: (row["t"] is None, row["t"] or 0, row["rid"]))
    return {
        "points": points,
        "apc_evidence": apc,
        "fragments": [],
        "off_reference_runs": [],
        "diagnostics": {"source_ids_preserved": True},
    }


def rank_segment(segment, observations, canonical, config, modes):
    projection = _projection(segment, observations)
    parts = partition_projection(projection, config["matcher"])
    schedules = {row["candidate_id"]: row for row in scheduled_lookup(canonical)}
    rankings, windows, mode_results = [], [], []
    for mode in MODES:
        if mode not in modes:
            continue
        part = parts[mode]
        selected = part["projection"]
        result = apply_decision_guard(
            match_candidate_segment(
                selected,
                selected.get("apc_evidence", []),
                canonical["references"],
                canonical["trips"],
                config["matcher"],
                service_date=segment["service_date"],
                route_id=canonical["route_id"],
            ),
            config["matcher"],
        )
        candidates = result.get("all_candidates", [])
        if candidates != sorted(
            candidates, key=lambda row: (row["score"], row["candidate_id"])
        ):
            raise ValueError("04c candidate ordering contract changed")
        for rank, candidate in enumerate(candidates, 1):
            reasons = []
            reference = schedules.get(candidate["candidate_id"])
            if reference is None:
                reasons.append("CANDIDATE_DOES_NOT_EXIST_OR_UNORDERABLE")
            else:
                for field in ("service_date", "route_id", "direction_id"):
                    if str(candidate.get(field)) != str(segment.get(field)) or str(
                        reference[field]
                    ) != str(segment.get(field)):
                        reasons.append(f"INCOMPATIBLE_{field.upper()}")
                if candidate.get("feed_version") != segment["gtfs_feed_version"]:
                    reasons.append("INCOMPATIBLE_GTFS_FEED")
                if not reference["canonical_valid"]:
                    reasons.append("INVALID_CANONICAL_STOP_ORDER")
            if not math.isfinite(candidate["score"]):
                raise ValueError("Nonfinite candidate score cannot receive a rank")
            row = {
                key: value
                for key, value in candidate.items()
                if key not in HEAVY_CANDIDATE_FIELDS
            }
            row.update(
                trip_id_performed=segment["trip_id_performed"],
                match_mode=mode,
                trip_id_scheduled=candidate.get("trip_id"),
                candidate_rank=rank,
                gtfs_feed_version=candidate.get("feed_version"),
                hard_constraints_valid=not reasons,
                hard_constraint_reasons=reasons,
                assignment_eligible_rank=rank <= 3,
                ordering_tuple=[candidate["score"], candidate["candidate_id"]],
                score_direction="lower_is_better",
                scheduled_trajectory_reference=candidate["candidate_id"],
                delay_process_point_count=len(candidate.get("delay_process", [])),
                score_threshold_applied_to_assignment=False,
            )
            if config["execution"].get("persist_alignment_traces", False):
                row["delay_process"] = candidate.get("delay_process", [])
            rankings.append(row)
        local = result.get("local_scores", {})
        if isinstance(local, dict):
            for identity, entries in local.items():
                for index, row in enumerate(entries):
                    windows.append(
                        dict(
                            row,
                            trip_id_performed=segment["trip_id_performed"],
                            match_mode=mode,
                            candidate_id=identity,
                            local_evidence_id=stable_id(
                                "local",
                                segment["trip_id_performed"],
                                mode,
                                identity,
                                index,
                            ),
                        )
                    )
        mode_results.append(
            {
                "trip_id_performed": segment["trip_id_performed"],
                "match_mode": mode,
                "candidate_count": len(candidates),
                "matcher_status": result.get("status"),
                "matcher_reasons": result.get("reasons", []),
                "selection": part["selection"],
                "pattern_confidence": result.get("pattern_confidence"),
                "trip_confidence": result.get("trip_confidence"),
                "calibration": result.get("calibration"),
                "candidate_ranking_fixed": True,
            }
        )
    return {"rankings": rankings, "windows": windows, "mode_results": mode_results}
