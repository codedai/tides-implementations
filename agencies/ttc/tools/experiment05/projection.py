"""Stage 4: reuse 04c sequence projection before assigning any performed ID."""

from __future__ import annotations

import collections

from tools.merge_review.gtfs_matching.projection import project_sequence


def matching_input(row):
    # Explicit allowlist excludes raw schedule-derived values such as AvgOTP.
    keys = (
        "rid",
        "source_rid",
        "source_row",
        "source_file_sha256",
        "source",
        "vehicle",
        "vehicle_id",
        "device_id",
        "t",
        "time",
        "event_time",
        "lat",
        "lon",
        "heading",
        "speed",
        "speed_kph",
        "direction_label",
        "direction",
        "destination",
        "route",
        "stop",
        "arrival",
        "departure",
        "boarding",
        "alighting",
        "grain_uncertain",
        "avg_sample",
        "no_trips",
        "invalid",
        "time_ambiguous",
        "time_nonexistent",
        "scope_separator_before",
    )
    return {key: row[key] for key in keys if key in row}


def projection_jobs(timeline, config):
    groups = collections.defaultdict(list)
    for row in timeline:
        if row["source"] == "avl" and row.get("vehicle_id"):
            groups[row["vehicle_id"]].append(row)
    jobs = []
    for vehicle, rows in sorted(groups.items()):
        selected, previous, out_of_scope = [], None, False
        for row in rows:
            if not row["route_eligible"]:
                out_of_scope = True
                continue
            if row.get("duplicate_of"):
                continue
            point = matching_input(row)
            separation = out_of_scope
            if (
                previous
                and point.get("t") is not None
                and previous.get("t") is not None
            ):
                separation |= (
                    point["t"] - previous["t"]
                    > config["projection"]["numerical_chunk_gap_s"]
                )
                if config["projection"]["reset_at_raw_direction_change"]:
                    separation |= bool(
                        point.get("direction_label")
                        and previous.get("direction_label")
                        and point["direction_label"] != previous["direction_label"]
                    )
            point["scope_separator_before"] = separation
            selected.append(point)
            previous, out_of_scope = point, False
        if selected:
            jobs.append((vehicle, selected))
    return jobs


def project_vehicle(rows, canonical, config):
    result = project_sequence(rows, canonical, config["matcher"])
    references = {row["reference_id"]: row for row in canonical["references"]}
    for row in result["points"]:
        reference = references.get(row.get("reference_id"), {})
        row["direction_id"] = reference.get("direction_id")
        row["projected_direction_label"] = reference.get("direction_label")
        row["projection_distance_m"] = row.get("lateral_m")
        row["trajectory_included"] = row["projection_status"] == "ON_REFERENCE"
        row["platform_state"] = "NORMAL"
    result["diagnostics"]["coordinates"] = (
        "canonical reference metres; UTC epoch seconds"
    )
    return result


def combine_projection(timeline, projected):
    by_rid = {row["rid"]: row for result in projected for row in result["points"]}
    output = []
    for original in timeline:
        row = dict(original)
        projection = by_rid.get(row["rid"])
        if projection:
            row.update(projection)
        else:
            row.update(
                projection_status=(
                    "PENDING_STOP_INTERVAL"
                    if row["source"] == "apc"
                    else "NOT_PROJECTED_DIAGNOSTIC"
                ),
                trajectory_included=False,
                projection_distance_m=None,
                reference_id=None,
                s_m=None,
                fragment_id=None,
                platform_state="NORMAL",
            )
        output.append(row)
    if len(output) != len(timeline) or {row["rid"] for row in output} != {
        row["rid"] for row in timeline
    }:
        raise ValueError("Projection changed source identity inventory")
    return output
