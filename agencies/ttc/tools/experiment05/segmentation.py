"""Post-projection direction/progress boundaries, then auditable trip extents."""

from __future__ import annotations

import collections

from .io import stable_id


def _presence_gap(left, right, apc):
    """Largest actual gap in AVL instants + closed APC service intervals."""
    end, largest = left, 0.0
    intervals = []
    for row in apc:
        a, b = row.get("arrival"), row.get("departure")
        if a is None:
            a = b
        if a is None:
            continue
        if b is None or b < a:
            b = a
        if a <= right and b >= left:
            intervals.append((max(a, left), min(b, right)))
    for a, b in sorted(intervals):
        largest = max(largest, a - end)
        end = max(end, b)
    return max(largest, right - end)


def _boundaries(rows, apc, config):
    cfg = config["segmentation"]
    cuts = {0: "SOURCE_EXTENT_START"}
    direction, tentative = None, None
    for index, row in enumerate(rows):
        label = row.get("direction_id")
        if (
            index
            and row["t"] - rows[index - 1]["t"] > cfg["joint_gap_s"]
            and _presence_gap(rows[index - 1]["t"], row["t"], apc) > cfg["joint_gap_s"]
        ):
            cuts[index] = "JOINT_AVL_APC_GAP"
            direction, tentative = label, None
        if label is None:
            continue
        if direction is None:
            direction = label
        if label == direction:
            tentative = None
        else:
            if tentative is None or tentative["direction"] != label:
                tentative = {
                    "direction": label,
                    "index": index,
                    "count": 0,
                    "start": row["t"],
                }
            tentative["count"] += 1
            if (
                tentative["count"] >= cfg["direction_confirm_points"]
                and row["t"] - tentative["start"] >= cfg["direction_confirm_duration_s"]
            ):
                cuts[tentative["index"]] = "CONFIRMED_PROJECTED_DIRECTION_CHANGE"
                direction, tentative = label, None
        if index:
            previous = rows[index - 1]
            if (
                row.get("reference_id")
                and row.get("reference_id") == previous.get("reference_id")
                and row.get("s_m") is not None
                and previous.get("s_m") is not None
                and previous["s_m"] - row["s_m"] > cfg["progress_reset_m"]
                and row.get("sequence_restart")
            ):
                cuts[index] = "SEQUENCE_RESTART_PROGRESS_RESET"
    return cuts


def segment_trips(observations, canonical, service_date, config):
    """Platform module may trim an extent, never pick the winning GTFS trip."""
    from .plateau import assess_trip_extent

    groups, apc_groups = collections.defaultdict(list), collections.defaultdict(list)
    updated = {row["rid"]: dict(row) for row in observations}
    for row in observations:
        if not row.get("route_eligible") or not row.get("vehicle_id"):
            continue
        if row["source"] == "apc":
            apc_groups[row["vehicle_id"]].append(row)
        elif row.get("t") is not None and not row.get("duplicate_of"):
            groups[row["vehicle_id"]].append(row)
    segments, diagnostics = [], []
    for vehicle, rows in sorted(groups.items()):
        rows.sort(key=lambda row: (row["t"], row["rid"]))
        cuts = _boundaries(rows, apc_groups[vehicle], config)
        edges = sorted(cuts) + [len(rows)]
        for left, right in zip(edges, edges[1:], strict=False):
            points = rows[left:right]
            counts = collections.Counter(
                row["direction_id"]
                for row in points
                if row.get("direction_id") is not None
            )
            direction = (
                min(counts, key=lambda key: (-counts[key], key))
                if counts
                else "UNKNOWN"
            )
            identity = stable_id(
                "performed",
                service_date,
                canonical["feed_version"],
                canonical["route_id"],
                vehicle,
                points[0]["rid"],
                points[-1]["rid"],
            )
            relevant_apc = [
                row
                for row in apc_groups[vehicle]
                if row.get("t") is not None
                and points[0]["t"] <= row["t"] <= points[-1]["t"]
            ]
            extent = assess_trip_extent(points, relevant_apc, canonical, config)
            summary = dict(
                extent["extent"],
                trip_id_performed=identity,
                service_date=service_date,
                route_id=canonical["route_id"],
                direction_id=direction,
                gtfs_feed_version=canonical["feed_version"],
                vehicle_id=vehicle,
                actual_start_t=points[0]["t"],
                boundary_reason=cuts[left],
                avl_count=len(points),
                inferred_segment=True,
                direction_conflict_count=sum(
                    bool(
                        row.get("direction_label")
                        and row.get("projected_direction_label")
                        and row["direction_label"] != row["projected_direction_label"]
                    )
                    for row in points
                ),
            )
            segments.append(summary)
            for point in extent["points"]:
                updated[point["rid"]].update(point, trip_id_performed=identity)
            for item in extent["diagnostics"]:
                diagnostics.append(dict(item, trip_id_performed=identity))
    # Duplicate observations retain their exact source identity and inherit only
    # their representative's membership, never independent scoring weight.
    for row in updated.values():
        if row.get("duplicate_of") in updated:
            row["trip_id_performed"] = updated[row["duplicate_of"]].get(
                "trip_id_performed"
            )
            row["trajectory_included"] = False
    return segments, list(updated.values()), diagnostics
