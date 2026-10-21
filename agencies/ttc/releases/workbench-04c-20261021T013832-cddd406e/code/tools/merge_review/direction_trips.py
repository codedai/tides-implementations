"""Conservative, two-source direction *candidate* segments, not matched trips.

All equal-time evidence is considered together. APC intervals are closed;
AVL holds are left-closed/right-open and stop at the next distinct AVL time
(including filtered/unknown rows), at 30 seconds, or at the day boundary.
An isolated final AVL observation supplies only point evidence. Conflicts and
unknown gaps never themselves cut a segment. The first definite direction
labels the initial candidate, without claiming it was known before that time.
"""

from __future__ import annotations

import collections
import copy
import heapq
import json
import re
from datetime import datetime, timedelta

from tools.merge_review.vehicle_stream import build_vehicle_stream, validate_vehicle_stream

METHOD = "direction_destination"
REPRESENTATION = "direction_candidate_segments"
DAY_START = datetime(2023, 11, 6)
DAY_END = datetime(2023, 11, 7)
AVL_HOLD_SECONDS = 30
DERIVED_FIELDS = {
    "direction_label", "direction_basis", "direction_interval_kind",
    "direction_evidence_start", "direction_evidence_end", "direction_end_inclusive",
    "direction_evidence_eligible", "direction_exclusion_reasons",
    "direction_day_censored", "candidate_segment_ids", "candidate_boundary_overlap",
    "direction_conflict_ids",
}
DEFINITION = {
    "version": "direction-evidence-v1",
    "avl_route_filter": "canonical route stripped and uppercased, exact PI or PO only",
    "avl_direction": "case-insensitive first word NORTH or SOUTH in Destination",
    "apc_direction": "stripped uppercase direction must equal NORTH or SOUTH",
    "avl_hold_seconds": AVL_HOLD_SECONDS,
    "avl_hold": "[time,next distinct AVL time), capped at 30 seconds; next includes PI/PO and unknown; final AVL is a point",
    "apc_interval": "[arrival,departure] if ordered; zero/reversed/missing endpoint uses an explicitly marked point, never an invented interval",
    "invalid_time_policy": "invalid, time_ambiguous or time_nonexistent rows retained but provide no direction evidence",
    "boundary": "only definite nonconflicting NORTH/SOUTH reversal; conflict and unknown preserve last candidate",
    "equal_time": "all simultaneous evidence is batched; closed point then right-open span, never source display order",
    "scope": "day-bounded evidence; original source times untouched; no neighbor-day AVL",
    "membership": "AVL event point; APC observed valid interval or explicit fallback point; candidate IDs may be multiple or empty",
    "conflict_windows": "maximal connected atoms with identical active evidence; windows are not counts of independent incidents",
    "trip_identity_verified": False,
    "heading_used": False,
    "route_changes_segment": False,
    "gtfs_used": False,
    "observation_matching_used": False,
}


def _scope(start, end):
    if any(not isinstance(t, datetime) or t.tzinfo is not None for t in (start, end)) or start >= end:
        raise ValueError("Scope must be increasing naive wall-clock datetimes")


def _label(row, source):
    value = row.get("destination" if source == "avl" else "direction")
    if not isinstance(value, str):
        return None
    if source == "avl":
        match = re.match(r"^\s*(NORTH|SOUTH)\b", value, re.IGNORECASE)
        return match.group(1).upper() if match else None
    value = value.strip().upper()
    return value if value in {"NORTH", "SOUTH"} else None


def _filtered(row):
    value = row.get("route")
    return isinstance(value, str) and value.strip().upper() in {"PI", "PO"}


def _refs(evidence):
    return [{"source": s, "rid": r} for s, r in sorted({(e["source"], e["rid"]) for e in evidence})]


def _describe(row, next_times, start, end):
    source, instant = row["source"], row["event_time"]
    label = _label(row, source)
    left = right = instant
    inclusive = True
    if instant is None:
        kind = "missing_time"
    elif source == "avl":
        nxt = next_times.get(instant)
        if nxt is None:
            kind = "avl_last_point"
        else:
            kind = "avl_hold"
            right = min(nxt, instant + timedelta(seconds=AVL_HOLD_SECONDS))
            inclusive = False
    else:
        arrival, departure = row.get("arrival"), row.get("departure")
        if departure is not None and (not isinstance(departure, datetime) or departure.tzinfo is not None):
            raise ValueError("APC departure must be a naive datetime or null")
        if arrival is None:
            kind = "apc_departure_fallback_point"
        elif departure is None:
            kind = "apc_arrival_only_point"
        elif departure < arrival:
            kind = "apc_reversed_arrival_point"
        elif departure == arrival:
            kind = "apc_zero_duration_point"
        else:
            kind, right = "apc_interval", departure
    reasons = []
    if label is None:
        reasons.append("unknown_direction_label")
    for flag in ("invalid", "time_ambiguous", "time_nonexistent"):
        if row.get(flag):
            reasons.append(flag)
    censored = left is not None and (left < start or right >= end)
    if left is None:
        reasons.append("missing_event_time")
    elif left >= end or right < start or (right == start and not inclusive):
        reasons.append("outside_scope")
        left = right = None
    else:
        left = max(left, start)
        if right >= end:
            right, inclusive = end, False
    row.update(
        direction_label=label,
        direction_basis="avl.destination_first_word" if source == "avl" else "apc.direction",
        direction_interval_kind=kind,
        direction_evidence_start=left,
        direction_evidence_end=right,
        direction_end_inclusive=inclusive,
        direction_evidence_eligible=not reasons,
        direction_exclusion_reasons=reasons,
        direction_day_censored=bool(censored),
    )
    if reasons:
        return None
    return dict(id=f"evidence:{source}:{row['source_rid']}", source=source,
                rid=row["source_rid"], direction=label, start=left, end=right,
                start_inclusive=True, end_inclusive=inclusive, interval_kind=kind)


def _atoms(evidence, start, end):
    """Event sweep, O(events log events); no second-by-second expansion."""
    starts, stops = collections.defaultdict(list), collections.defaultdict(list)
    by_id = {e["id"]: e for e in evidence}
    for item in evidence:
        starts[item["start"]].append(item["id"])
        stops[item["end"]].append(item["id"])
    times = sorted(set(starts) | set(stops) | {start, end})
    active = set()
    for index, instant in enumerate(times[:-1]):
        # Half-open AVL endings do not participate in this instant. Closed APC
        # endings still do, even when another opposite APC begins here.
        active.difference_update(i for i in stops[instant] if not by_id[i]["end_inclusive"])
        active.update(starts[instant])
        yield dict(start=instant, end=instant, start_inclusive=True,
                   end_inclusive=True, kind="point", evidence=[by_id[i] for i in sorted(active)])
        active.difference_update(stops[instant])
        yield dict(start=instant, end=times[index + 1], start_inclusive=False,
                   end_inclusive=False, kind="interval", evidence=[by_id[i] for i in sorted(active)])


def _state(items):
    labels = {e["direction"] for e in items}
    return "conflict" if len(labels) > 1 else next(iter(labels), "unknown")


def _intersects(left, right):
    start, end = max(left["start"], right["start"]), min(left["end"], right["end"])
    if start < end:
        return True
    if start > end:
        return False
    return all(
        (start != item["start"] or item.get("start_inclusive", True))
        and (start != item["end"] or item.get("end_inclusive", False))
        for item in (left, right)
    )


def _conflict_types(items, arrival_times):
    north, south = ([e for e in items if e["direction"] == d] for d in ("NORTH", "SOUTH"))
    types = set()
    for n in north:
        for s in south:
            if n["source"] != s["source"]:
                types.add("cross_source_direction")
            elif n["source"] == "avl":
                types.add("avl_same_time_opposite")
            else:
                types.add("apc_internal_direction")
                arrival = arrival_times.get(n["rid"])
                if arrival is not None and arrival == arrival_times.get(s["rid"]):
                    types.add("apc_same_arrival_opposite")
    return sorted(types)


def build_direction_trips(retained_avl, apc, *, start=DAY_START, end=DAY_END):
    """Build one vehicle's lossless direction candidates after exact AVL dedup.

    Inputs are canonical original rows, not existing observation-stream aliases.
    Empty inputs/all-filtered vehicles are valid. Missing-time observations are
    preserved with empty candidate IDs; they must not disappear from a viewer.
    """
    _scope(start, end)
    all_rows = build_vehicle_stream(retained_avl, apc)  # validates source IDs/times
    if any((DERIVED_FIELDS | {"filter_reason", "filter_normalized_route"}) & row.keys() for row in retained_avl + apc):
        raise ValueError("Direction annotations must not overwrite original fields")
    filtered = [dict(copy.deepcopy(r), filter_reason="avl_route_pi_po",
                     filter_normalized_route=r["route"].strip().upper())
                for r in retained_avl if _filtered(r)]
    filtered.sort(key=lambda r: (r.get("source_row") is None, r.get("source_row") or 0, r["rid"]))
    observations = [r for r in all_rows if r["source"] != "avl" or not _filtered(r)]
    for index, row in enumerate(observations):
        row["display_order"] = index
    times = sorted({r.get("time") for r in retained_avl if r.get("time") is not None})
    next_times = dict(zip(times, times[1:]))
    evidence = []
    for row in observations:
        item = _describe(row, next_times, start, end)
        if item is not None:
            evidence.append(item)
    evidence.sort(key=lambda e: (e["start"], e["end"], e["source"], e["rid"]))
    vehicle = next((r["vehicle"] for r in all_rows), None)
    segments, conflicts = [], []
    last_direction, last_at, last_evidence = None, None, []
    event_times = {(r["source"], r["rid"]): r["event_time"] for r in observations}
    arrival_times = {r["rid"]: r.get("arrival") for r in observations if r["source"] == "apc"}

    def new_segment(at, included, previous=None, phase="point"):
        segment = dict(id=f"direction:{vehicle}:{len(segments):04d}", direction=None,
                       start=at, end=end, start_inclusive=included, end_inclusive=False,
                       first_definite_at=None, boundary_reason="scope_start" if previous is None else "definite_direction_reversal",
                       boundary_from_direction=previous, boundary_phase=phase,
                       boundary_evidence=[], previous_definite_at=last_at,
                       previous_direction_evidence=copy.deepcopy(last_evidence),
                       source_rids=[], conflict_ids=[], unknown_duration_s=0.0,
                       conflict_duration_s=0.0, candidate_only=True, complete_trip=False)
        segments.append(segment)
        return segment

    current = new_segment(start, True) if observations else None
    for atom in _atoms(evidence, start, end):
        items, state = atom["evidence"], _state(atom["evidence"])
        duration = (atom["end"] - atom["start"]).total_seconds()
        if state in {"NORTH", "SOUTH"} and current is not None:
            if last_direction is not None and state != last_direction:
                current["end"] = atom["start"]
                current["end_inclusive"] = atom["kind"] == "interval"
                current = new_segment(atom["start"], atom["kind"] == "point",
                                      last_direction, "point" if atom["kind"] == "point" else "right_limit")
            if current["direction"] is None:
                current["direction"] = state
                current["first_definite_at"] = atom["start"]
                current["boundary_evidence"] = _refs(items)
            last_direction, last_at, last_evidence = state, atom["start"], _refs(items)
        elif state == "unknown" and current is not None:
            current["unknown_duration_s"] += duration
        elif state == "conflict":
            if current is not None:
                current["conflict_duration_s"] += duration
            refs = _refs(items)
            can_extend = (conflicts and conflicts[-1]["source_rids"] == refs
                          and conflicts[-1]["end"] == atom["start"]
                          and (conflicts[-1]["end_inclusive"] or atom["start_inclusive"]))
            if can_extend:
                conflicts[-1].update(end=atom["end"], end_inclusive=atom["end_inclusive"],
                                     kind="point" if conflicts[-1]["start"] == atom["end"] else "interval")
            else:
                conflicts.append(dict(id=f"conflict:{vehicle}:{len(conflicts):04d}",
                                      start=atom["start"], end=atom["end"],
                                      start_inclusive=atom["start_inclusive"], end_inclusive=atom["end_inclusive"],
                                      kind=atom["kind"], types=_conflict_types(items, arrival_times),
                                      source_rids=refs,
                                      north_source_rids=_refs(e for e in items if e["direction"] == "NORTH"),
                                      south_source_rids=_refs(e for e in items if e["direction"] == "SOUTH")))
    conflict_by_source = collections.defaultdict(list)
    for conflict in conflicts:
        for ref in conflict["source_rids"]:
            conflict_by_source[(ref["source"], ref["rid"])].append(conflict["id"])
        for segment in segments:
            if _intersects(conflict, segment):
                segment["conflict_ids"].append(conflict["id"])
    for row in observations:
        left = row["direction_evidence_start"]
        right = row["direction_evidence_end"]
        inclusive = row["direction_end_inclusive"]
        if row["source"] == "avl":
            left = right = row["event_time"]
            inclusive = True
        window = dict(start=left, end=right, start_inclusive=True, end_inclusive=inclusive)
        members = [s for s in segments if left is not None and _intersects(window, s)]
        row["candidate_segment_ids"] = [s["id"] for s in members]
        row["candidate_boundary_overlap"] = len(members) > 1
        row["direction_conflict_ids"] = conflict_by_source[(row["source"], row["rid"])]
        for segment in members:
            segment["source_rids"].append(dict(source=row["source"], rid=row["rid"]))
    for segment in segments:
        segment["source_rids"].sort(key=lambda r: (r["source"], r["rid"]))
        located = [event_times[(r["source"], r["rid"])] for r in segment["source_rids"]
                   if event_times[(r["source"], r["rid"])] is not None]
        segment["source_start"] = min(located, default=None)
        segment["source_end"] = max(located, default=None)
        segment["scope_censored_start"] = segment is segments[0]
        segment["scope_censored_end"] = segment is segments[-1]
    summary = dict(source_counts={"avl": len(retained_avl), "apc": len(apc)},
                   retained_counts={"avl": len(retained_avl) - len(filtered), "apc": len(apc)},
                   observations=len(observations), filtered_avl_count=len(filtered),
                   segments=len(segments), conflict_windows=len(conflicts),
                   conflict_point_windows=sum(c["kind"] == "point" for c in conflicts),
                   conflict_duration_s=sum((s["conflict_duration_s"] for s in segments), 0.0),
                   unknown_duration_s=sum((s["unknown_duration_s"] for s in segments), 0.0),
                   direction_counts=dict(collections.Counter(r["direction_label"] or "unknown" for r in observations)),
                   unresolved_observations=sum(not r["candidate_segment_ids"] for r in observations),
                   candidate_boundary_overlap_count=sum(r["candidate_boundary_overlap"] for r in observations),
                   direction_conflict_observations=sum(bool(r["direction_conflict_ids"]) for r in observations),
                   accuracy=None, publishable=False, candidate_only=True,
                   trip_identity_verified=False, observation_matching_used=False)
    return dict(observations=observations, filtered_avl=filtered, segments=segments,
                conflicts=conflicts, evidence=evidence, summary=summary,
                scope=dict(start=start, end=end), policy=copy.deepcopy(DEFINITION))


_TIMESTAMP_FIELDS = {
    "time", "arrival", "departure", "event_time", "start", "end", "first_definite_at",
    "previous_definite_at", "direction_evidence_start", "direction_evidence_end",
    "source_start", "source_end",
}


def restore_metadata(value):
    """Restore known flat schema timestamps without parsing opaque source data.

    In particular, a future source payload may contain a nested key ``start``
    holding an ISO-looking *string*. That must remain a string, not a datetime.
    """
    output = copy.deepcopy(value)
    if not isinstance(output, dict):
        return output
    for collection in ("observations", "filtered_avl", "segments", "conflicts", "evidence", "scope"):
        items = output.get(collection, [])
        items = [items] if isinstance(items, dict) else items
        for item in items:
            if collection == "filtered_avl":
                fields = {"time", "arrival", "departure"}
            elif collection == "observations":
                fields = {"time", "arrival", "departure", "event_time", "direction_evidence_start", "direction_evidence_end"}
            else:
                fields = _TIMESTAMP_FIELDS
            for key in fields & item.keys():
                if isinstance(item[key], str):
                    try:
                        item[key] = datetime.fromisoformat(item[key])
                    except ValueError:
                        pass  # validator reports malformed timestamps; never guess
    return output


def _same(left, right):
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(right, sort_keys=True, default=str)


def validate_direction_trips(retained_avl, apc, result, start=DAY_START, end=DAY_END):
    """Independent conservation + interval replay; does not call the builder.

    The replay derives evidence again directly from original source fields and
    uses a heap of expiry times (not the builder's start/end set sweep). It tests
    points and intervening open spans, so endpoint-only conflicts are checked.
    Passing is a structural feasibility check, never trip/matching accuracy.
    """
    _scope(start, end)
    counts = collections.Counter({name: 0 for name in (
        "filtered_membership", "filtered_original_fields", "filtered_reason",
        "direction_original_fields", "evidence_definition", "evidence_inventory",
        "scope_changed", "segment_structure", "segment_boundary_evidence",
        "segment_membership", "observation_membership", "observation_conflicts",
        "conflict_structure", "conflict_evidence", "conflict_coverage",
        "summary_changed", "policy_changed", "fabricated_trip_certainty",
    )})
    keep, exclude = [], {}
    for row in retained_avl:
        value = row.get("route")
        if isinstance(value, str) and value.strip().upper() in ("PI", "PO"):
            exclude[row["rid"]] = row
        else:
            keep.append(row)
    rows = result.get("observations", [])
    rows_by_key = collections.defaultdict(list)
    for row in rows:
        rows_by_key[(row.get("source"), row.get("source_rid"))].append(row)
    stream_check = validate_vehicle_stream(keep, apc, rows)
    counts.update({"stream_" + k: v for k, v in stream_check["violations"].items()})
    filtered = result.get("filtered_avl", [])
    counts["filtered_membership"] += collections.Counter(r.get("rid") for r in filtered) != collections.Counter(exclude.keys())
    for row in filtered:
        original = exclude.get(row.get("rid"))
        if original is None:
            continue
        counts["filtered_original_fields"] += any(not _same(v, row.get(k)) for k, v in original.items())
        counts["filtered_reason"] += row.get("filter_reason") != "avl_route_pi_po" or row.get("filter_normalized_route") != original["route"].strip().upper()
    counts["scope_changed"] += result.get("scope") != dict(start=start, end=end)
    counts["policy_changed"] += result.get("policy") != DEFINITION
    originals = {(source, r["rid"]): r for source, source_rows in (("avl", keep), ("apc", apc)) for r in source_rows}
    avl_times = sorted({r["time"] for r in retained_avl if r.get("time") is not None})
    successors = {time: avl_times[i + 1] for i, time in enumerate(avl_times[:-1])}
    expected_evidence, expected_windows, labels = {}, {}, {}
    # Deliberately independent source-to-evidence implementation.
    for key, original in originals.items():
        source, rid = key
        raw = original.get("destination" if source == "avl" else "direction")
        if source == "avl":
            token = re.findall(r"^\s*(NORTH|SOUTH)\b", raw, re.I) if isinstance(raw, str) else []
            direction = token[0].upper() if token else None
            left = original.get("time")
            nxt = successors.get(left)
            right = min(nxt, left + timedelta(seconds=30)) if nxt is not None else left
            kind = "missing_time" if left is None else "avl_last_point" if nxt is None else "avl_hold"
            inclusive = nxt is None
        else:
            direction = raw.strip().upper() if isinstance(raw, str) else None
            direction = direction if direction in ("NORTH", "SOUTH") else None
            a, d = original.get("arrival"), original.get("departure")
            left, inclusive = a if a is not None else d, True
            right = left
            if left is None:
                kind = "missing_time"
            elif a is None:
                kind = "apc_departure_fallback_point"
            elif d is None:
                kind = "apc_arrival_only_point"
            elif d < a:
                kind = "apc_reversed_arrival_point"
            elif d == a:
                kind = "apc_zero_duration_point"
            else:
                kind, right = "apc_interval", d
        original_event = original.get("time") if source == "avl" else original.get("arrival") if original.get("arrival") is not None else original.get("departure")
        censored = left is not None and (left < start or right >= end)
        reasons = (["unknown_direction_label"] if direction is None else []) + [f for f in ("invalid", "time_ambiguous", "time_nonexistent") if original.get(f)]
        if left is None:
            reasons.append("missing_event_time")
        elif left >= end or right < start or right == start and not inclusive:
            reasons.append("outside_scope")
            left = right = None
        else:
            left = max(start, left)
            if right >= end:
                right, inclusive = end, False
        labels[key] = direction
        expected_windows[key] = (original_event, original_event, True) if source == "avl" else (left, right, inclusive)
        annotation = dict(direction_label=direction,
                          direction_basis="avl.destination_first_word" if source == "avl" else "apc.direction",
                          direction_interval_kind=kind, direction_evidence_start=left,
                          direction_evidence_end=right, direction_end_inclusive=inclusive,
                          direction_evidence_eligible=not reasons, direction_exclusion_reasons=reasons,
                          direction_day_censored=bool(censored))
        for row in rows_by_key[key]:
            counts["evidence_definition"] += any(not _same(v, row.get(k)) for k, v in annotation.items())
        if not reasons:
            item = dict(id=f"evidence:{source}:{rid}", source=source, rid=rid, direction=direction,
                        start=left, end=right, start_inclusive=True, end_inclusive=inclusive,
                        interval_kind=kind)
            expected_evidence[item["id"]] = item
    actual_evidence = result.get("evidence", [])
    counts["evidence_inventory"] += len(actual_evidence) != len(expected_evidence)
    counts["evidence_inventory"] += len({e.get("id") for e in actual_evidence}) != len(actual_evidence)
    counts["evidence_inventory"] += sum(not _same(expected_evidence.get(e.get("id")), e) for e in actual_evidence)
    segments, conflicts = result.get("segments", []), result.get("conflicts", [])
    counts["segment_structure"] += bool(rows) != bool(segments)
    counts["segment_structure"] += len({s.get("id") for s in segments}) != len(segments)
    counts["conflict_structure"] += len({c.get("id") for c in conflicts}) != len(conflicts)

    def active_at(item, moment, right_limit=False):
        if right_limit:
            return item["start"] <= moment < item["end"]
        return (item["start"] < moment or item["start"] == moment and item.get("start_inclusive", True)) and (item["end"] > moment or item["end"] == moment and item.get("end_inclusive", False))

    malformed = False
    for collection, category in ((segments, "segment_structure"), (conflicts, "conflict_structure")):
        for item in collection:
            valid = all(isinstance(item.get(k), datetime) and item[k].tzinfo is None for k in ("start", "end"))
            valid = valid and start <= item["start"] <= item["end"] <= end
            if not valid:
                counts[category] += 1
                malformed = True
            if collection is conflicts and valid:
                counts[category] += item.get("kind") != ("point" if item["start"] == item["end"] else "interval")
                counts[category] += item["start"] == item["end"] and not (item.get("start_inclusive") and item.get("end_inclusive"))
    if malformed:
        return dict(violations=dict(counts), examined=dict(observations=len(rows), segments=len(segments), conflicts=len(conflicts)))
    # Add submitted endpoints, so fabricated sub-windows cannot hide between
    # legitimate evidence endpoints. Sweep uses a separate expiry heap.
    check_times = {start, end}
    for item in list(expected_evidence.values()) + segments + conflicts:
        check_times.update((item["start"], item["end"]))
    ordered = sorted(check_times)
    pending = sorted(expected_evidence.values(), key=lambda e: (e["start"], e["id"]))
    cursor, active, expirations = 0, {}, []
    previous, expected_boundaries, unknown_s, conflict_s = None, [], 0.0, 0.0
    actual_members = collections.defaultdict(list)
    contributor_conflicts = collections.defaultdict(list)
    for conflict in conflicts:
        for ref in conflict.get("source_rids", []):
            contributor_conflicts[(ref.get("source"), ref.get("rid"))].append(conflict["id"])
    segment_unknown, segment_conflict = collections.Counter(), collections.Counter()
    for ti, instant in enumerate(ordered[:-1]):
        for phase, moment, duration in (
            ("point", instant, 0.0),
            ("right_limit", instant, (ordered[ti + 1] - instant).total_seconds()),
        ):
            while cursor < len(pending) and pending[cursor]["start"] <= moment:
                item = pending[cursor]
                active[item["id"]] = item
                heapq.heappush(expirations, (item["end"], item["end_inclusive"], item["id"]))
                cursor += 1
            while expirations and (expirations[0][0] < moment or expirations[0][0] == moment and (not expirations[0][1] or phase == "right_limit")):
                active.pop(heapq.heappop(expirations)[2], None)
            directions = {e["direction"] for e in active.values()}
            state = "conflict" if len(directions) == 2 else next(iter(directions), "unknown")
            if state in ("NORTH", "SOUTH"):
                if state != previous:
                    expected_boundaries.append((instant, phase, state, previous, _refs(active.values())))
                previous = state
            containing = [s for s in segments if active_at(s, moment, phase == "right_limit")]
            counts["segment_structure"] += bool(rows) and len(containing) != 1
            if containing and state == "unknown":
                segment_unknown[containing[0]["id"]] += duration
            if containing and state == "conflict":
                segment_conflict[containing[0]["id"]] += duration
            unknown_s += duration if state == "unknown" and rows else 0
            conflict_s += duration if state == "conflict" else 0
            windows = [c for c in conflicts if active_at(c, moment, phase == "right_limit")]
            counts["conflict_coverage"] += len(windows) != (1 if state == "conflict" else 0)
            for window in windows:
                expected_refs = _refs(active.values())
                counts["conflict_evidence"] += window.get("source_rids") != expected_refs
                for label, field in (("NORTH", "north_source_rids"), ("SOUTH", "south_source_rids")):
                    counts["conflict_evidence"] += window.get(field) != _refs(e for e in active.values() if e["direction"] == label)
                # Independent pair classification from original labels/events.
                types = set()
                north = [(e["source"], e["rid"]) for e in active.values() if e["direction"] == "NORTH"]
                south = [(e["source"], e["rid"]) for e in active.values() if e["direction"] == "SOUTH"]
                for n in north:
                    for s in south:
                        if n[0] != s[0]:
                            types.add("cross_source_direction")
                        elif n[0] == "avl":
                            types.add("avl_same_time_opposite")
                        else:
                            types.add("apc_internal_direction")
                            a, b = originals[n], originals[s]
                            if a.get("arrival") is not None and a.get("arrival") == b.get("arrival"):
                                types.add("apc_same_arrival_opposite")
                counts["conflict_evidence"] += window.get("types") != sorted(types)
    reversals = expected_boundaries[1:]
    counts["segment_structure"] += len(segments) != (len(reversals) + 1 if rows else 0)
    for index, segment in enumerate(segments):
        first = expected_boundaries[0] if expected_boundaries else None
        boundary = first if index == 0 else reversals[index - 1] if index - 1 < len(reversals) else None
        counts["fabricated_trip_certainty"] += segment.get("candidate_only") is not True or segment.get("complete_trip") is not False
        expected_start = start if index == 0 else boundary[0] if boundary else None
        following = reversals[index] if index < len(reversals) else None
        counts["segment_structure"] += segment.get("end") != (following[0] if following else end)
        counts["segment_structure"] += segment.get("start_inclusive") != (index == 0 or boundary is not None and boundary[1] == "point")
        counts["segment_structure"] += segment.get("end_inclusive") != (following is not None and following[1] == "right_limit")
        counts["segment_structure"] += segment.get("scope_censored_start") != (index == 0)
        counts["segment_structure"] += segment.get("scope_censored_end") != (index == len(segments) - 1)
        counts["segment_boundary_evidence"] += segment.get("start") != expected_start
        counts["segment_boundary_evidence"] += segment.get("direction") != (boundary[2] if boundary else None)
        counts["segment_boundary_evidence"] += segment.get("first_definite_at") != (boundary[0] if boundary else None)
        counts["segment_boundary_evidence"] += segment.get("boundary_evidence") != (boundary[4] if boundary else [])
        counts["segment_boundary_evidence"] += segment.get("boundary_from_direction") != (None if index == 0 else boundary[3] if boundary else None)
        counts["segment_boundary_evidence"] += segment.get("boundary_reason") != ("scope_start" if index == 0 else "definite_direction_reversal")
        if index > 0:
            counts["segment_boundary_evidence"] += boundary is None or segment.get("boundary_phase") != boundary[1]
        counts["segment_structure"] += segment.get("unknown_duration_s") != segment_unknown[segment["id"]]
        counts["segment_structure"] += segment.get("conflict_duration_s") != segment_conflict[segment["id"]]
        true_conflicts = [c["id"] for c in conflicts if _intersects(c, segment)]
        counts["segment_membership"] += segment.get("conflict_ids") != true_conflicts
    # Membership is derived independently from source intervals, not from
    # submitted direction annotations or segment source lists.
    for row in rows:
        key = row.get("source"), row.get("source_rid")
        if key not in expected_windows:
            continue
        left, right, inclusive = expected_windows[key]
        ids = []
        if left is not None:
            for segment in segments:
                lo, hi = max(left, segment["start"]), min(right, segment["end"])
                point_ok = (lo != right or inclusive) and (lo != segment["start"] or segment["start_inclusive"]) and (lo != segment["end"] or segment["end_inclusive"])
                if lo < hi or lo == hi and point_ok:
                    ids.append(segment["id"])
                    actual_members[segment["id"]].append(dict(source=key[0], rid=key[1]))
        counts["observation_membership"] += row.get("candidate_segment_ids") != ids
        counts["observation_membership"] += row.get("candidate_boundary_overlap") != (len(ids) > 1)
        counts["observation_conflicts"] += row.get("direction_conflict_ids") != contributor_conflicts[key]
    for segment in segments:
        expected_refs = sorted(actual_members[segment["id"]], key=lambda r: (r["source"], r["rid"]))
        counts["segment_membership"] += segment.get("source_rids") != expected_refs
        located = []
        for ref in expected_refs:
            original = originals[(ref["source"], ref["rid"])]
            instant = original.get("time") if ref["source"] == "avl" else original.get("arrival") if original.get("arrival") is not None else original.get("departure")
            if instant is not None:
                located.append(instant)
        counts["segment_membership"] += segment.get("source_start") != min(located, default=None)
        counts["segment_membership"] += segment.get("source_end") != max(located, default=None)
    expected_summary = dict(source_counts={"avl": len(retained_avl), "apc": len(apc)},
                            retained_counts={"avl": len(keep), "apc": len(apc)},
                            observations=len(keep) + len(apc), filtered_avl_count=len(exclude),
                            segments=len(segments), conflict_windows=len(conflicts),
                            conflict_point_windows=sum(c["start"] == c["end"] for c in conflicts),
                            conflict_duration_s=conflict_s, unknown_duration_s=unknown_s,
                            direction_counts=dict(collections.Counter(d or "unknown" for d in labels.values())),
                            unresolved_observations=sum(not r.get("candidate_segment_ids") for r in rows),
                            candidate_boundary_overlap_count=sum(bool(r.get("candidate_boundary_overlap")) for r in rows),
                            direction_conflict_observations=sum(bool(r.get("direction_conflict_ids")) for r in rows),
                            accuracy=None, publishable=False, candidate_only=True,
                            trip_identity_verified=False, observation_matching_used=False)
    counts["summary_changed"] += not _same(result.get("summary"), expected_summary)
    return dict(violations=dict(counts), examined=dict(observations=len(rows), avl=len(retained_avl),
                                                     apc=len(apc), segments=len(segments), conflicts=len(conflicts)))
