"""Stage 6: reversible APC membership decisions and observed stop intervals."""

from __future__ import annotations

import collections

from tools.merge_review.gtfs_matching.projection import project_apc_intervals

from .io import stable_id
from .projection import matching_input


def attach_apc(segments, observations, canonical, config):
    cfg = config["apc"]
    grouped = collections.defaultdict(list)
    for segment in segments:
        grouped[segment["vehicle_id"]].append(segment)
    avl_by_trip = collections.defaultdict(list)
    for row in observations:
        if (
            row["source"] == "avl"
            and row.get("trajectory_included")
            and row.get("trip_id_performed")
        ):
            avl_by_trip[row["trip_id_performed"]].append(row)
    raw_apc = [
        row for row in observations if row["source"] == "apc" and row["route_eligible"]
    ]
    projected = project_apc_intervals(
        [matching_input(row) for row in raw_apc], canonical, config["matcher"]
    )
    updates = {row["rid"]: dict(row) for row in observations}
    audit, visit_evidence = [], collections.defaultdict(list)
    for evidence in projected:
        row = updates[evidence["rid"]]
        row.update(evidence)
        row["trajectory_included"] = False  # APC is never an invented AVL point.
        a, b = row.get("arrival"), row.get("departure")
        if a is None or b is None or b < a or b - a > cfg["maximum_service_interval_s"]:
            row["apc_attachment_reason"] = "INVALID_SERVICE_INTERVAL"
            audit.append(
                {
                    "rid": row["rid"],
                    "trip_id_performed": None,
                    "reason": row["apc_attachment_reason"],
                }
            )
            continue
        choices = []
        for segment in grouped[row["vehicle_id"]]:
            start, end = (
                segment["effective_segment_start"],
                segment["effective_segment_end"],
            )
            # A confirmed censored tail must not be reintroduced by APC attachment.
            if segment.get("censor_reason") and a > end:
                continue
            gap = max(start - b, a - end, 0.0)
            if gap > cfg["attach_time_buffer_s"]:
                continue
            points = avl_by_trip[segment["trip_id_performed"]]
            references = {point.get("reference_id") for point in points}
            stops = [
                item
                for item in row["canonical_stop_intervals"]
                if item["reference_id"] in references and not item["ambiguous"]
            ]
            alternatives = {
                item["reference_id"]: item for item in row["projection_alternatives"]
            }
            stops = [
                item
                for item in stops
                if alternatives[item["reference_id"]]["projection_status"]
                in {"ON_REFERENCE", "STOP_INTERVAL_ONLY"}
            ]
            if not stops:
                continue
            overlap = max(0.0, min(b, end) - max(a, start))
            conflict = bool(
                row.get("direction_label")
                and points
                and row["direction_label"]
                not in {p.get("projected_direction_label") for p in points}
            )
            nearby = [
                point
                for point in points
                if a - cfg["attach_time_buffer_s"]
                <= point["t"]
                <= b + cfg["attach_time_buffer_s"]
            ]
            errors = [
                abs(point["s_m"] - stop["s_min_m"])
                for point in nearby
                for stop in stops
                if point.get("reference_id") == stop["reference_id"]
            ]
            progress_error = min(errors) if errors else None
            if (
                progress_error is not None
                and progress_error > cfg["attach_progress_tolerance_m"]
            ):
                continue
            choices.append(
                {
                    "segment": segment,
                    "stops": stops,
                    "quality": (
                        int(conflict),
                        gap,
                        -overlap,
                        progress_error
                        if progress_error is not None
                        else cfg["attach_progress_tolerance_m"],
                    ),
                    "progress_error_m": progress_error,
                }
            )
        choices.sort(
            key=lambda item: (item["quality"], item["segment"]["trip_id_performed"])
        )
        selected = (
            choices[0]
            if choices
            and (len(choices) == 1 or choices[0]["quality"] != choices[1]["quality"])
            else None
        )
        reason = (
            "ATTACHED"
            if selected
            else "AMBIGUOUS_TRIP_MEMBERSHIP"
            if choices
            else "NO_COMPATIBLE_PERFORMED_TRIP"
        )
        row["apc_attachment_reason"] = reason
        row["apc_trip_candidates"] = [
            item["segment"]["trip_id_performed"] for item in choices
        ]
        if selected:
            trip_id = selected["segment"]["trip_id_performed"]
            row["trip_id_performed"] = trip_id
            row["apc_evidence_included"] = True
            segment = selected["segment"]
            if segment.get("censor_reason"):
                row["interval_start"] = max(a, segment["effective_segment_start"])
                row["interval_end"] = min(b, segment["effective_segment_end"])
                row["decision_interval_clip"] = {
                    "original_start": a,
                    "original_end": b,
                    "clipped": row["interval_start"] != a or row["interval_end"] != b,
                    "reason": "EFFECTIVE_TRIP_EXTENT",
                    "passenger_counts_prorated": False,
                }
            stop_ids = {item["stop_id"] for item in selected["stops"]}
            row["mapped_stop_id"] = next(iter(stop_ids)) if len(stop_ids) == 1 else None
            if row["mapped_stop_id"]:
                visit_evidence[trip_id].append(row)
        else:
            row["apc_evidence_included"] = False
        audit.append(
            {
                "rid": row["rid"],
                "trip_id_performed": row.get("trip_id_performed"),
                "reason": reason,
                "candidates": row["apc_trip_candidates"],
                "progress_error_m": selected["progress_error_m"] if selected else None,
            }
        )
    visits = []
    segment_by_id = {row["trip_id_performed"]: row for row in segments}
    for trip_id, evidence in sorted(visit_evidence.items()):
        # One source APC interval is one observed visit hypothesis. Nearby rows
        # are not merged into fabricated passenger events or summed twice.
        evidence.sort(key=lambda row: (row["arrival"], row["departure"], row["rid"]))
        for sequence, row in enumerate(evidence, 1):
            segment = segment_by_id[trip_id]
            visit_id = stable_id("visit", trip_id, row["rid"])
            row["stop_visit_id"] = visit_id
            row["trip_stop_sequence"] = sequence
            visits.append(
                {
                    "stop_visit_id": visit_id,
                    "trip_id_performed": trip_id,
                    "service_date": segment["service_date"],
                    "vehicle_id": segment["vehicle_id"],
                    "stop_id": row["mapped_stop_id"],
                    "trip_stop_sequence": sequence,
                    "arrival_t": row["arrival"],
                    "departure_t": row["departure"],
                    "boarding": row.get("boarding"),
                    "alighting": row.get("alighting"),
                    "grain_uncertain": row.get("grain_uncertain", True),
                    "source_rids": [row["rid"]],
                    "passenger_event_semantics_valid": False,
                    "visit_basis": (
                        "source APC stop-time interval; "
                        "not an instantaneous passenger event"
                    ),
                }
            )
    return list(updates.values()), visits, audit
