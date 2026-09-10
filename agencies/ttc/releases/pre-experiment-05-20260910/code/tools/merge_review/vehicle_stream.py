"""Lossless vehicle-grouped UNION ALL observations, with no trip or matching.

Display order is event time, AVL before APC, source row, then source rid. Ties
have no causal meaning. APC departure is only a fallback for missing arrival;
the original arrival, departure, and AVL time fields remain untouched.
"""

from __future__ import annotations

import collections
import copy
import json
from datetime import datetime

REPRESENTATION = "vehicle_observation_stream"
ORDERING = (
    "event_time ascending, missing last; at equal event_time AVL before APC, "
    "source_row ascending (missing last), then source rid lexicographically. "
    "This is deterministic display order, not inferred causal order."
)
DERIVED_FIELDS = {
    "source",
    "source_rid",
    "event_time",
    "event_time_basis",
    "display_order",
}
COMMON_FIELDS = {
    "source_row",
    "route",
    "route_name",
    "branch",
    "arrival",
    "departure",
    "lat",
    "lon",
    "time",
}


def _event(row, source):
    if source == "avl":
        value, basis = row.get("time"), "avl.time"
    elif row.get("arrival") is not None:
        value, basis = row["arrival"], "apc.arrival"
    else:
        value, basis = row.get("departure"), "apc.departure_fallback"
    if value is None:
        return None, "missing"
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("Stream times must be naive source datetimes or null")
    return value, basis


def _order(row):
    instant = row["event_time"]
    number = row.get("source_row")
    return (
        instant is None,
        instant or datetime.max,
        0 if row["source"] == "avl" else 1,
        number is None,
        number or 0,
        row["source_rid"],
    )


def build_vehicle_stream(avl: list[dict], apc: list[dict]) -> list[dict]:
    """Retain each input record once, including duplicate payloads/invalid rows."""
    vehicles = {r.get("vehicle") for r in avl + apc}
    if len(vehicles) > 1 or None in vehicles or "" in vehicles:
        raise ValueError("A stream requires one nonempty canonical VehicleID")
    result = []
    for source, rows in (("avl", avl), ("apc", apc)):
        seen = set()
        for original in rows:
            rid = original.get("rid")
            if not isinstance(rid, str) or not rid or rid in seen:
                raise ValueError("Source rid must be a unique nonempty string")
            if DERIVED_FIELDS & original.keys():
                raise ValueError("Stream aliases must not overwrite source fields")
            number = original.get("source_row")
            if number is not None and (
                isinstance(number, bool) or not isinstance(number, int) or number < 0
            ):
                raise ValueError("source_row must be an integer or null")
            seen.add(rid)
            row = copy.deepcopy(original)
            instant, basis = _event(row, source)
            for field in COMMON_FIELDS:
                row.setdefault(field, None)
            row.update(
                source=source,
                source_rid=rid,
                event_time=instant,
                event_time_basis=basis,
            )
            result.append(row)
    result.sort(key=_order)
    for ordinal, row in enumerate(result):
        row["display_order"] = ordinal
    return result


def _same(first, second):
    # Stable for canonical NaN/null/datetimes as well as nested future fields.
    return json.dumps(first, sort_keys=True, default=str) == json.dumps(
        second, sort_keys=True, default=str
    )


def validate_vehicle_stream(avl, apc, stream, start=None, end=None) -> dict:
    """Independently replay membership, original values, date, and display order.

    This intentionally does not call the builder, its event selector or sort-key
    function. A passing result says nothing about trip identity or APC–AVL matches.
    """
    counts = collections.Counter(
        {
            "observation_count_difference": abs(len(stream) - len(avl) - len(apc)),
            "duplicate_input_rid": 0,
            "duplicate_output_rid": 0,
            "missing_source_records": 0,
            "extra_source_records": 0,
            "original_field_changed": 0,
            "source_rid_alias_changed": 0,
            "event_time_changed": 0,
            "event_basis_changed": 0,
            "day_membership_violation": 0,
            "display_order_violation": 0,
            "ordinal_violation": 0,
            "multiple_vehicles": 0,
            "fabricated_matching_fields": 0,
        }
    )
    expected = {}
    for source, rows in (("avl", avl), ("apc", apc)):
        for row in rows:
            key = (source, row["rid"])
            counts["duplicate_input_rid"] += key in expected
            expected[key] = row
    observed = collections.Counter(
        (r.get("source"), r.get("source_rid")) for r in stream
    )
    counts["duplicate_output_rid"] = sum(
        max(0, value - 1) for value in observed.values()
    )
    counts["missing_source_records"] = len(expected.keys() - observed.keys())
    counts["extra_source_records"] = len(observed.keys() - expected.keys())
    counts["multiple_vehicles"] = len({r.get("vehicle") for r in stream}) > 1
    previous = None
    for ordinal, row in enumerate(stream):
        source = row.get("source")
        original = expected.get((source, row.get("source_rid")))
        if original is None:
            continue
        for key, value in original.items():
            counts["original_field_changed"] += not _same(value, row.get(key))
        counts["source_rid_alias_changed"] += row.get("rid") != row.get("source_rid")
        if source == "avl":
            value, basis = original.get("time"), "avl.time"
        elif original.get("arrival") is not None:
            value, basis = original["arrival"], "apc.arrival"
        else:
            value, basis = original.get("departure"), "apc.departure_fallback"
        if value is None:
            basis = "missing"
        counts["event_time_changed"] += row.get("event_time") != value
        counts["event_basis_changed"] += row.get("event_time_basis") != basis
        if start is not None and end is not None:
            counts["day_membership_violation"] += (
                value is None or not start <= value < end
            )
        number = original.get("source_row")
        key = (
            value is None,
            value if value is not None else datetime.max,
            {"avl": 0, "apc": 1}[source],
            number is None,
            0 if number is None else number,
            original["rid"],
        )
        counts["display_order_violation"] += previous is not None and key < previous
        counts["ordinal_violation"] += row.get("display_order") != ordinal
        previous = key
        counts["fabricated_matching_fields"] += any(
            field in row and field not in original
            for field in ("status", "segment_id", "segment_ids", "witnesses", "score")
        )
    return {
        "violations": dict(counts),
        "examined": {"observations": len(stream), "avl": len(avl), "apc": len(apc)},
    }
