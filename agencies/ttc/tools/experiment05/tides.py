"""Pinned TIDES v1.0 research export; no matching, hidden thresholds or repairs.

Inputs are Experiment 05 evidence/assignments, with UTC epoch-second timestamps.
The published package is structurally validated, not certified operational truth.
"""

from __future__ import annotations

import collections
import csv
import hashlib
import json
import math
import shutil
import tempfile
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path

import frictionless
from jsonschema import Draft7Validator

SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas/experiment05/tides-v1"
SPEC_COMMIT = "b2cb23178e00cb58dbdb1b9a5a8626818278ecbb"
MODES = {"global", "early", "middle", "late"}
CONSERVATIVE_EVENT_POLICY = "omit_without_event_semantics"
APC_DEPARTURE_PROXY_EVENT_POLICY = "apc_stop_interval_departure_proxy_v1"
PASSENGER_EVENT_POLICIES = {
    CONSERVATIVE_EVENT_POLICY,
    APC_DEPARTURE_PROXY_EVENT_POLICY,
}
TABLES = (
    "vehicles",
    "devices",
    "trips_performed",
    "vehicle_locations",
    "stop_visits",
    "passenger_events",
)


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_schemas():
    """Reject changes to any locally vendored schema or attribution file."""
    manifest = json.loads((SCHEMA_ROOT / "vendor.json").read_text())
    if manifest.get("commit") != SPEC_COMMIT or manifest.get("tag") != "v1.0":
        raise ValueError("Unexpected TIDES specification revision")
    expected = {f"{name}.schema.json" for name in TABLES} | {
        "table-schema.json",
        "tides-datapackage-profile.json",
        "LICENSE-CC-BY-4.0",
    }
    if set(manifest.get("sha256", {})) != expected:
        raise ValueError("Incomplete TIDES vendor manifest")
    for name, digest in manifest["sha256"].items():
        path = SCHEMA_ROOT / name
        if path.is_symlink() or _digest(path) != digest:
            raise ValueError(f"Pinned TIDES file changed: {name}")
    return manifest


@lru_cache(maxsize=len(TABLES))
def _schema(name):
    if name not in TABLES:
        raise ValueError(f"Unknown TIDES resource: {name}")
    return json.loads((SCHEMA_ROOT / f"{name}.schema.json").read_text())


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _stamp(value):
    if value is None:
        return None
    if not _finite(value):
        raise ValueError("TIDES timestamp must be finite UTC epoch seconds")
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def _identifier(value, label, *, nullable=False):
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or value in {"NA", "NaN", "null", "None"}
    ):
        raise ValueError(f"Invalid {label}; identifiers must be nonempty strings")
    return value


def _service_date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("service_date must be an explicit YYYY-MM-DD service date")
    return value


def _integer(value, label, *, minimum=0, nullable=False):
    if value is None and nullable:
        return None
    if not _finite(value) or value < minimum or int(value) != value:
        raise ValueError(f"{label} must be an integer >= {minimum}; no rounding")
    return int(value)


def _key(row):
    return (
        _service_date(row.get("service_date")),
        _identifier(row.get("trip_id_performed"), "trip_id_performed"),
    )


def _row(name, values):
    columns = [field["name"] for field in _schema(name)["fields"]]
    unknown = set(values) - set(columns)
    if unknown:
        raise ValueError(f"Unknown TIDES core columns in {name}: {sorted(unknown)}")
    return {column: values.get(column) for column in columns}


def _schedule_index(bundle):
    indexed = {}
    for row in bundle.get("trips", []):
        feed = row.get("feed_version", bundle.get("feed_version"))
        key = (feed, _service_date(row.get("service_date")), str(row["trip_id"]))
        if key in indexed:
            raise ValueError("Duplicate scheduled occurrence in active GTFS bundle")
        indexed[key] = row
    return indexed


def _assignment_rows(assignments, mode):
    rows = assignments.get(mode, []) if isinstance(assignments, dict) else assignments
    return [row for row in rows if row.get("match_mode", mode) == mode]


def _resolve_segment(row, segments_by_key, by_id):
    identity = row.get("trip_id_performed")
    if identity is None:
        return None
    _identifier(identity, "trip_id_performed")
    if row.get("service_date") is not None:
        key = _key(row)
    else:
        keys = by_id.get(identity, [])
        if len(keys) != 1:
            raise ValueError("Absent or ambiguous performed-trip service date")
        key = keys[0]
    if key not in segments_by_key:
        raise ValueError("Performed-trip foreign key does not resolve")
    return key


def build_tides_rows(
    mode, segments, observations, stop_visits, assignments, gtfs_bundle, config=None
):
    """Pure core-table mapping, returning rows plus explicit inference/omission audit.

    No confidence threshold is applied: the supplied one-to-one 05 assignment is
    the scheduled association. Raw/effective segment envelopes stay in sidecars.
    """
    verify_schemas()
    if mode not in MODES:
        raise ValueError("Unknown Experiment 05 matching mode")
    config = config or {}
    include_devices = config.get("export", {}).get("include_devices", True)
    if not isinstance(include_devices, bool):
        raise ValueError("export.include_devices must be boolean")
    event_policy = config.get("export", {}).get(
        "passenger_event_policy", CONSERVATIVE_EVENT_POLICY
    )
    if (
        not isinstance(event_policy, str)
        or event_policy not in PASSENGER_EVENT_POLICIES
    ):
        raise ValueError("Unknown export.passenger_event_policy")
    use_departure_proxy = event_policy == APC_DEPARTURE_PROXY_EVENT_POLICY
    tables = {name: [] for name in TABLES}
    diagnostics = {
        "research_only": True,
        "inferred_assignments": True,
        "calibrated_certification": False,
        "match_mode": mode,
        "assignment_policy": "experiment05_one_to_one_without_score_threshold",
        "schema_version": "TIDES v1.0",
        "schema_commit": SPEC_COMMIT,
        "timezone": "UTC",
        "source_timezone": config.get("normalization", {}).get(
            "timezone", config.get("timezone", "America/Toronto")
        ),
        "export_policy": {
            "include_devices": include_devices,
            "passenger_event_policy": event_policy,
        },
        "omissions": [],
        "lineage": [],
        "passenger_event_lineage": [],
        "segment_envelopes": [],
    }
    segment_index, by_id = {}, collections.defaultdict(list)
    for segment in segments:
        key = _key(segment)
        if key in segment_index:
            raise ValueError("Duplicate performed-trip composite key")
        _identifier(segment.get("vehicle_id"), "vehicle_id")
        segment_index[key] = segment
        by_id[key[1]].append(key)
        diagnostics["segment_envelopes"].append(
            {
                "service_date": key[0],
                "trip_id_performed": key[1],
                "raw_direction_id": segment.get("direction_id"),
                **{
                    field: segment.get(field)
                    for field in (
                        "raw_segment_start",
                        "raw_segment_end",
                        "effective_segment_start",
                        "effective_segment_end",
                    )
                },
                "actual_trip_endpoints_not_imputed_from_envelope": True,
            }
        )
    schedule = _schedule_index(gtfs_bundle)
    assigned, used = {}, set()
    for assignment in _assignment_rows(assignments, mode):
        key = _resolve_segment(assignment, segment_index, by_id)
        if key is None or key in assigned:
            raise ValueError("Missing or duplicate assignment performed-trip key")
        trip_id = assignment.get("trip_id_scheduled")
        if trip_id is None:
            assigned[key] = None
            continue
        _identifier(trip_id, "trip_id_scheduled")
        segment = segment_index[key]
        feed = assignment.get(
            "gtfs_feed_version",
            segment.get("gtfs_feed_version", gtfs_bundle.get("feed_version")),
        )
        scheduled_key = (feed, key[0], trip_id)
        candidate = schedule.get(scheduled_key)
        if candidate is None:
            raise ValueError(
                "Assignment does not resolve to an active feed/service-date trip"
            )
        if scheduled_key in used:
            raise ValueError(
                "Experiment 05 scheduled occurrence assigned more than once"
            )
        if segment.get("route_id") is not None and str(candidate["route_id"]) != str(
            segment["route_id"]
        ):
            raise ValueError("Assignment route differs from performed trip")
        if segment.get("direction_id") not in {None, "UNKNOWN"} and int(
            candidate["direction_id"]
        ) != int(segment["direction_id"]):
            raise ValueError("Assignment direction differs from performed trip")
        used.add(scheduled_key)
        assigned[key] = candidate
    for key, segment in sorted(segment_index.items()):
        candidate = assigned.get(key)
        values = {
            "service_date": key[0],
            "trip_id_performed": key[1],
            "vehicle_id": segment["vehicle_id"],
            "route_id": segment.get("route_id"),
            "direction_id": None
            if segment.get("direction_id") == "UNKNOWN"
            else segment.get("direction_id"),
            "actual_trip_start": _stamp(segment.get("actual_trip_start")),
            "actual_trip_end": _stamp(segment.get("actual_trip_end")),
        }
        if candidate:
            stops = candidate.get("stop_times", [])
            values.update(
                trip_id_scheduled=candidate["trip_id"],
                route_id=candidate["route_id"],
                direction_id=_integer(int(candidate["direction_id"]), "direction_id"),
                shape_id=candidate.get("shape_id"),
                block_id=candidate.get("block_id"),
                schedule_relationship="Scheduled",
                schedule_trip_start=_stamp(stops[0].get("departure_t"))
                if stops
                else None,
                schedule_trip_end=_stamp(stops[-1].get("arrival_t")) if stops else None,
            )
        tables["trips_performed"].append(_row("trips_performed", values))
    seen_rids, devices, vehicle_ids = set(), {}, {s["vehicle_id"] for s in segments}
    source_index = {}
    for observation in observations:
        rid = _identifier(observation.get("rid"), "source rid")
        if rid in seen_rids:
            raise ValueError("Duplicate observation source identity")
        seen_rids.add(rid)
        source_index[rid] = observation
        source = observation.get("source")
        lineage = {
            field: observation.get(field)
            for field in (
                "rid",
                "source",
                "source_row",
                "source_file_sha256",
                "projection_status",
                "trajectory_included",
                "device_id",
            )
        }
        lineage.update(location_ping_id=None, disposition="not_location_evidence")
        if (
            source != "avl"
            and observation.get("genuine_vehicle_coordinates") is not True
        ):
            diagnostics["lineage"].append(lineage)
            continue
        if observation.get("t") is None:
            lineage["disposition"] = "TIME_UNRESOLVED"
            diagnostics["lineage"].append(lineage)
            diagnostics["omissions"].append(
                {
                    "rid": rid,
                    "resource": "vehicle_locations",
                    "reason": "TIME_UNRESOLVED",
                }
            )
            continue
        vehicle = _identifier(observation.get("vehicle_id"), "vehicle_id")
        vehicle_ids.add(vehicle)
        key = _resolve_segment(observation, segment_index, by_id)
        if key and segment_index[key]["vehicle_id"] != vehicle:
            raise ValueError("Observation vehicle differs from performed trip")
        device = _identifier(observation.get("device_id"), "device_id", nullable=True)
        if device and not include_devices:
            diagnostics["omissions"].append(
                {
                    "rid": rid,
                    "resource": "vehicle_locations",
                    "field": "device_id",
                    "reason": "DEVICES_EXPORT_DISABLED_SOURCE_ID_RETAINED_IN_LINEAGE",
                }
            )
            device = None
        if device:
            devices.setdefault(device, set()).add(vehicle)
        candidate = assigned.get(key)
        speed = observation.get("speed_kph")
        if speed is not None and (not _finite(speed) or speed < 0):
            raise ValueError("Invalid source speed; no silent repair")
        values = {
            "location_ping_id": rid,
            "event_timestamp": _stamp(observation["t"]),
            "vehicle_id": vehicle,
            "device_id": device,
            "latitude": observation.get("lat"),
            "longitude": observation.get("lon"),
            "heading": observation.get("heading"),
            "speed": None if speed is None else speed / 3.6,
            "service_date": key[0] if key else None,
            "trip_id_performed": key[1] if key else None,
            "trip_id_scheduled": candidate["trip_id"] if candidate else None,
        }
        tables["vehicle_locations"].append(_row("vehicle_locations", values))
        lineage.update(location_ping_id=rid, disposition="vehicle_location_exported")
        diagnostics["lineage"].append(lineage)
    attributed_sources, visit_ids = set(), set()
    for visit in stop_visits:
        visit_id = _identifier(visit.get("stop_visit_id"), "stop_visit_id")
        if visit_id in visit_ids:
            raise ValueError("Duplicate stop-visit identity")
        visit_ids.add(visit_id)
        key = _resolve_segment(visit, segment_index, by_id)
        if key is None:
            raise ValueError("A stop visit requires a performed trip")
        segment = segment_index[key]
        vehicle = visit.get("vehicle_id", segment["vehicle_id"])
        if vehicle != segment["vehicle_id"]:
            raise ValueError("Visit vehicle differs from performed trip")
        for rid in visit.get("source_rids", []):
            if rid not in source_index:
                raise ValueError("Stop visit source identity does not resolve")
            if rid in attributed_sources:
                raise ValueError(
                    "A source observation was attributed to multiple visits"
                )
            source = source_index[rid]
            if source.get("vehicle_id") != vehicle:
                raise ValueError("Stop visit source vehicle is inconsistent")
            source_key = _resolve_segment(source, segment_index, by_id)
            if source_key is not None and source_key != key:
                raise ValueError(
                    "Stop visit source performed-trip link is inconsistent"
                )
            attributed_sources.add(rid)
        seq = _integer(visit.get("trip_stop_sequence"), "trip_stop_sequence", minimum=1)
        arrival, departure = visit.get("arrival_t"), visit.get("departure_t")
        if arrival is not None and departure is not None and departure < arrival:
            raise ValueError("Inverted stop-visit interval; no repair")
        dwell = (
            departure - arrival
            if arrival is not None and departure is not None
            else None
        )
        if dwell is not None and int(dwell) != dwell:
            diagnostics["omissions"].append(
                {
                    "stop_visit_id": visit.get("stop_visit_id"),
                    "field": "dwell",
                    "reason": "NONINTEGER_SECONDS_NO_ROUNDING",
                }
            )
            dwell = None
        candidate = assigned.get(key)
        scheduled = None
        scheduled_seq = visit.get("scheduled_stop_sequence")
        if scheduled_seq is not None:
            if not candidate:
                raise ValueError(
                    "Scheduled stop sequence requires assigned scheduled occurrence"
                )
            scheduled_seq = _integer(scheduled_seq, "scheduled_stop_sequence")
            matches = [
                s
                for s in candidate.get("stop_times", [])
                if int(s["stop_sequence"]) == scheduled_seq
                and s["stop_id"] == visit.get("stop_id")
            ]
            if len(matches) != 1:
                raise ValueError("Scheduled visit foreign key is absent or ambiguous")
            scheduled = matches[0]
        values = {
            "service_date": key[0],
            "trip_id_performed": key[1],
            "trip_stop_sequence": seq,
            "vehicle_id": vehicle,
            "stop_id": visit.get("stop_id"),
            "actual_arrival_time": _stamp(arrival),
            "actual_departure_time": _stamp(departure),
            "dwell": None if dwell is None else int(dwell),
            "scheduled_stop_sequence": scheduled_seq,
            "schedule_arrival_time": _stamp(scheduled.get("arrival_t"))
            if scheduled
            else None,
            "schedule_departure_time": _stamp(scheduled.get("departure_t"))
            if scheduled
            else None,
        }
        tables["stop_visits"].append(_row("stop_visits", values))
        diagnostics["lineage"].append(
            {
                "stop_visit_id": visit.get("stop_visit_id"),
                "service_date": key[0],
                "trip_id_performed": key[1],
                "trip_stop_sequence": seq,
                "source_rids": visit.get("source_rids", []),
                "disposition": "inferred_stop_visit_exported",
            }
        )
        source_rids = list(visit.get("source_rids", []))
        apc_source_rids = [
            rid for rid in source_rids if source_index[rid].get("source") == "apc"
        ]
        events = [
            ("Passenger boarded", "boarding", visit.get("boarding")),
            ("Passenger alighted", "alighting", visit.get("alighting")),
        ]
        if use_departure_proxy:
            events = [
                ("Vehicle arrived at stop", "arrival", 1),
                ("Vehicle departed stop", "departure", 1),
                *events,
            ]
        for kind, field, count in events:
            is_count_event = field in {"boarding", "alighting"}
            event_t = (
                (arrival if field == "arrival" else departure)
                if use_departure_proxy
                else visit.get(f"{field}_event_t")
            )
            reason = None
            if count is None:
                reason = "COUNT_UNAVAILABLE"
            elif not _finite(count) or count < 0 or int(count) != count:
                reason = "COUNT_SEMANTICS_INVALID_NO_ROUNDING"
            elif count == 0:
                continue
            elif use_departure_proxy and not apc_source_rids:
                reason = "APC_SOURCE_LINEAGE_REQUIRED_FOR_PROXY_POLICY"
            elif use_departure_proxy and not (
                _finite(arrival) and _finite(departure) and departure >= arrival
            ):
                reason = "COMPLETE_APC_STOP_INTERVAL_REQUIRED_FOR_PROXY_POLICY"
            elif not use_departure_proxy and (
                visit.get("passenger_event_semantics_valid") is not True
                or event_t is None
            ):
                reason = "PASSENGER_EVENT_SEMANTICS_OR_TIMESTAMP_UNCONFIRMED"
            if reason:
                diagnostics["omissions"].append(
                    {
                        "resource": "passenger_events",
                        "stop_visit_id": visit.get("stop_visit_id"),
                        "event_type": kind,
                        "reason": reason,
                    }
                )
                continue
            if (
                not _finite(event_t)
                or (arrival is not None and event_t < arrival)
                or (departure is not None and event_t > departure)
            ):
                raise ValueError(
                    "Passenger event time is outside its observed stop interval"
                )
            identity = json.dumps([key, seq, kind], separators=(",", ":"))
            event_id = "e05-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
            tables["passenger_events"].append(
                _row(
                    "passenger_events",
                    {
                        "passenger_event_id": event_id,
                        "service_date": key[0],
                        "trip_id_performed": key[1],
                        "trip_stop_sequence": seq,
                        "event_type": kind,
                        "event_count": int(count),
                        "event_timestamp": _stamp(event_t),
                        "vehicle_id": vehicle,
                        "stop_id": visit.get("stop_id"),
                        "scheduled_stop_sequence": scheduled_seq,
                        "trip_id_scheduled": candidate["trip_id"]
                        if candidate
                        else None,
                    },
                )
            )
            diagnostics["passenger_event_lineage"].append(
                {
                    "passenger_event_id": event_id,
                    "stop_visit_id": visit_id,
                    "service_date": key[0],
                    "trip_id_performed": key[1],
                    "trip_stop_sequence": seq,
                    "event_type": kind,
                    "event_count": int(count),
                    "event_timestamp": _stamp(event_t),
                    "source_rids": source_rids,
                    "apc_source_rids": apc_source_rids,
                    "original_interval": {
                        "arrival_t": arrival,
                        "departure_t": departure,
                        "arrival_time": _stamp(arrival),
                        "departure_time": _stamp(departure),
                    },
                    "timestamp_basis": (
                        "APC_DEPARTURE_PROXY_FOR_VISIT_TOTAL"
                        if is_count_event
                        else "APC_RECORDED_ARRIVAL"
                        if field == "arrival"
                        else "APC_RECORDED_DEPARTURE"
                    )
                    if use_departure_proxy
                    else "EXPLICIT_CONFIRMED_EVENT_TIMESTAMP",
                    "timestamp_is_proxy": use_departure_proxy and is_count_event,
                    "semantics_assumed": use_departure_proxy,
                    "measured_individual_passenger_time": False
                    if use_departure_proxy
                    else None,
                    "assumption_policy": event_policy if use_departure_proxy else None,
                    "source_grain_uncertain": visit.get("grain_uncertain"),
                    "count_basis": "APC_VISIT_TOTAL"
                    if use_departure_proxy and is_count_event
                    else "ONE_SOURCE_RECORDED_VEHICLE_EVENT"
                    if use_departure_proxy
                    else "EXPLICIT_CONFIRMED_EVENT_COUNT",
                }
            )
    tables["vehicles"] = [
        _row("vehicles", {"vehicle_id": value}) for value in sorted(vehicle_ids)
    ]
    tables["devices"] = [
        _row(
            "devices",
            {
                "device_id": device,
                "vehicle_id": next(iter(vehicles)) if len(vehicles) == 1 else None,
            },
        )
        for device, vehicles in sorted(devices.items())
    ]
    for name, rows in tables.items():
        pk = _schema(name)["primaryKey"]
        fields = [pk] if isinstance(pk, str) else pk
        rows.sort(key=lambda row: tuple(row[field] for field in fields))
    diagnostics["omission_counts"] = dict(
        collections.Counter(item["reason"] for item in diagnostics["omissions"])
    )
    diagnostics["input_counts"] = {
        "segments": len(segments),
        "observations": len(observations),
        "stop_visits": len(stop_visits),
    }
    return tables, diagnostics


def validate_tides_rows(tables, gtfs_bundle=None):
    """Check exact core fields, schema constraints and composite business FKs."""
    verify_schemas()
    if set(tables) - set(TABLES):
        raise ValueError("Unknown TIDES resource")
    errors = []
    for name, rows in tables.items():
        schema = _schema(name)
        columns = [field["name"] for field in schema["fields"]]
        for index, row in enumerate(rows):
            if set(row) != set(columns):
                errors.append(
                    {
                        "resource": name,
                        "row": index,
                        "reason": "UNKNOWN_OR_MISSING_CORE_COLUMNS",
                    }
                )
        if not rows or errors:
            continue
        report = frictionless.Resource(
            data=[columns, *[[r[column] for column in columns] for r in rows]],
            schema=frictionless.Schema.from_descriptor(schema),
        ).validate()
        if not report.valid:
            errors.append(
                {
                    "resource": name,
                    "reason": "SCHEMA_INVALID",
                    "details": report.flatten(
                        ["rowNumber", "fieldName", "type", "note"]
                    ),
                }
            )
    if errors:
        return {
            "valid": False,
            "errors": errors,
            "schema_version": "TIDES v1.0",
            "frictionless_version": frictionless.__version__,
            "rows": {name: len(rows) for name, rows in tables.items()},
            "semantic_certification": False,
        }
    vehicles = {r["vehicle_id"] for r in tables.get("vehicles", [])}
    devices = {r["device_id"] for r in tables.get("devices", [])}
    trips = {
        (r["service_date"], r["trip_id_performed"]): r
        for r in tables.get("trips_performed", [])
    }
    visits = {
        (r["service_date"], r["trip_id_performed"], r["trip_stop_sequence"]): r
        for r in tables.get("stop_visits", [])
    }
    locations = {r["location_ping_id"]: r for r in tables.get("vehicle_locations", [])}
    gtfs_stops = {str(r["stop_id"]) for r in (gtfs_bundle or {}).get("stops", [])}
    gtfs_trips = collections.defaultdict(list)
    for trip in (gtfs_bundle or {}).get("trips", []):
        gtfs_trips[(trip.get("service_date"), str(trip["trip_id"]))].append(trip)
    sequences = collections.defaultdict(list)
    for name, rows in tables.items():
        for index, row in enumerate(rows):

            def fail(reason, name=name, index=index):
                errors.append({"resource": name, "row": index, "reason": reason})

            if row.get("vehicle_id") is not None and row["vehicle_id"] not in vehicles:
                fail("VEHICLE_FOREIGN_KEY")
            if (
                row.get("device_id") is not None
                and name != "devices"
                and row["device_id"] not in devices
            ):
                fail("DEVICE_FOREIGN_KEY")
            key = (row.get("service_date"), row.get("trip_id_performed"))
            if key[1] is not None:
                if key not in trips:
                    fail("TRIP_COMPOSITE_FOREIGN_KEY")
                elif (
                    row.get("vehicle_id") is not None
                    and row["vehicle_id"] != trips[key]["vehicle_id"]
                ):
                    fail("TRIP_VEHICLE_INCONSISTENT")
                elif (
                    name != "trips_performed"
                    and row.get("trip_id_scheduled") is not None
                    and row["trip_id_scheduled"] != trips[key]["trip_id_scheduled"]
                ):
                    fail("SCHEDULE_ASSIGNMENT_INCONSISTENT")
            if (
                name in {"passenger_events", "vehicle_locations"}
                and row.get("trip_stop_sequence") is not None
            ):
                if (*key, row["trip_stop_sequence"]) not in visits:
                    fail("STOP_VISIT_COMPOSITE_FOREIGN_KEY")
            if row.get("location_ping_id") is not None and name != "vehicle_locations":
                location = locations.get(row["location_ping_id"])
                if location is None or location["vehicle_id"] != row["vehicle_id"]:
                    fail("LOCATION_FOREIGN_KEY")
            if (
                gtfs_bundle is not None
                and row.get("stop_id") is not None
                and row["stop_id"] not in gtfs_stops
            ):
                fail("GTFS_STOP_FOREIGN_KEY")
            if (
                gtfs_bundle is not None
                and name == "trips_performed"
                and row.get("trip_id_scheduled") is not None
            ):
                candidates = gtfs_trips.get(
                    (row["service_date"], row["trip_id_scheduled"]), []
                )
                if len(candidates) != 1:
                    fail("GTFS_SCHEDULED_OCCURRENCE_FOREIGN_KEY")
                else:
                    for field in ("route_id", "direction_id", "shape_id", "block_id"):
                        if row.get(field) is not None and str(row[field]) != str(
                            candidates[0].get(field)
                        ):
                            fail(f"GTFS_{field.upper()}_INCONSISTENT")
            if name == "stop_visits":
                sequences[key].append(row["trip_stop_sequence"])
                arrival, departure = (
                    row["actual_arrival_time"],
                    row["actual_departure_time"],
                )
                if arrival is not None and departure is not None:
                    seconds = (
                        datetime.fromisoformat(departure)
                        - datetime.fromisoformat(arrival)
                    ).total_seconds()
                    if seconds < 0 or (
                        row["dwell"] is not None and row["dwell"] != seconds
                    ):
                        fail("STOP_VISIT_INTERVAL_OR_DWELL_INCONSISTENT")
    for key, values in sequences.items():
        if sorted(values) != list(range(1, len(values) + 1)):
            errors.append(
                {
                    "resource": "stop_visits",
                    "trip_key": key,
                    "reason": "ACTUAL_VISIT_SEQUENCE_NOT_CONSECUTIVE",
                }
            )
    return {
        "valid": not errors,
        "errors": errors,
        "schema_version": "TIDES v1.0",
        "frictionless_version": frictionless.__version__,
        "rows": {name: len(rows) for name, rows in tables.items()},
        "semantic_certification": False,
    }


def _write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _validate_run_binding(value):
    if value is not None and (
        not isinstance(value, str)
        or len(value) != 64
        or not set(value).issubset("0123456789abcdef")
    ):
        raise ValueError("TIDES run binding must be a lowercase SHA-256 digest or None")


def publish_tides(
    output_dir,
    mode,
    segments,
    observations,
    stop_visits,
    assignments,
    gtfs_bundle,
    config=None,
    *,
    run_binding=None,
):
    """Write a new research package only after exact-byte schema and FK validation.

    Existing destinations are never overwritten. A failed private staging folder
    is retained for diagnosis; its existence does not indicate publication.
    Production callers bind the package to their run and upstream artifact pins.
    """
    _validate_run_binding(run_binding)
    target = Path(output_dir)
    if target.exists() or target.is_symlink():
        raise ValueError("TIDES output must be a new directory")
    for ancestor in target.parents:
        if ancestor.is_symlink():
            raise ValueError("TIDES output cannot traverse a symlink")
    tables, diagnostics = build_tides_rows(
        mode, segments, observations, stop_visits, assignments, gtfs_bundle, config
    )
    validation = validate_tides_rows(tables, gtfs_bundle)
    if not validation["valid"]:
        raise ValueError(
            "TIDES validation failed: " + json.dumps(validation["errors"][:5])
        )
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent)
    )
    resources, hashes = [], {}
    local_schemas = staging / "schemas"
    local_schemas.mkdir(mode=0o700)
    for name in [*verify_schemas()["sha256"], "vendor.json"]:
        destination = local_schemas / name
        shutil.copyfile(SCHEMA_ROOT / name, destination)
        destination.chmod(0o600)
        hashes[f"schemas/{name}"] = _digest(destination)
    for name, rows in tables.items():
        if name in {"devices", "passenger_events"} and not rows:
            continue
        path = staging / f"{name}.csv"
        columns = [field["name"] for field in _schema(name)["fields"]]
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=columns, lineterminator="\n", extrasaction="raise"
            )
            writer.writeheader()
            writer.writerows(rows)
        path.chmod(0o600)
        hashes[path.name] = _digest(path)
        resources.append(
            {
                "name": name,
                "tides-table": name,
                "profile": "tabular-data-resource",
                "path": path.name,
                "schema": f"schemas/{name}.schema.json",
                "format": "csv",
                "encoding": "utf-8",
                "hash": f"sha256:{hashes[path.name]}",
            }
        )
    package = {
        "title": f"Experiment 05 {mode}: inferred research TIDES package",
        "profile": f"https://raw.githubusercontent.com/TIDES-transit/TIDES/{SPEC_COMMIT}/spec/tides-datapackage-profile.json",
        "description": (
            "Experimental one-to-one schedule assignments; no score threshold. "
            "Inferred trip/visit records, not calibrated operational certification."
        ),
        "resources": resources,
    }
    profile = json.loads((SCHEMA_ROOT / "tides-datapackage-profile.json").read_text())
    profile_errors = [
        error.message for error in Draft7Validator(profile).iter_errors(package)
    ]
    if profile_errors:
        raise ValueError("TIDES package profile invalid: " + repr(profile_errors))
    _write_json(staging / "datapackage.json", package)
    disk_errors = []
    for resource in resources:
        report = frictionless.Resource(
            resource["path"],
            basepath=str(staging),
            schema=frictionless.Schema.from_descriptor(_schema(resource["name"])),
        ).validate()
        if not report.valid:
            disk_errors.append(
                {
                    "resource": resource["name"],
                    "errors": report.flatten(
                        ["rowNumber", "fieldName", "type", "note"]
                    ),
                }
            )
    if disk_errors:
        raise ValueError("Serialized TIDES CSV validation failed: " + repr(disk_errors))
    validation.update(
        profile_valid=True,
        serialized_resources_valid=True,
        vendor_sha256=verify_schemas()["sha256"],
        output_sha256=hashes,
    )
    summary = {
        "match_mode": mode,
        "research_only": True,
        "rows": validation["rows"],
        "omission_counts": diagnostics["omission_counts"],
        "assigned_scheduled_trips": sum(
            r["trip_id_scheduled"] is not None for r in tables["trips_performed"]
        ),
    }
    _write_json(staging / "diagnostics.json", diagnostics)
    _write_json(staging / "validation_report.json", validation)
    _write_json(staging / "summary.json", summary)
    _write_json(
        staging / "completion.json",
        {
            "complete": True,
            "research_only": True,
            "run_binding": run_binding,
            "output_sha256": {
                str(path.relative_to(staging)): _digest(path)
                for path in sorted(staging.rglob("*"))
                if path.is_file()
            },
        },
    )
    if target.exists() or target.is_symlink():
        raise ValueError("TIDES output appeared during validation; refusing overwrite")
    staging.rename(target)
    return {"path": str(target), "validation": validation, "summary": summary}


def read_completed_tides(output_dir, mode, *, expected_binding=None):
    """Read a successful, byte-pinned mode for same-run stage resumption.

    The pipeline must first verify its input/code/config/prerequisite pins. This
    helper never rewrites, deletes, or promotes a partial/incomplete package.
    An expected binding rejects otherwise valid packages from a different run,
    including unbound standalone packages. None is for standalone use only.
    """
    _validate_run_binding(expected_binding)
    target = Path(output_dir)
    if mode not in MODES or target.is_symlink() or not target.is_dir():
        raise ValueError("Invalid completed TIDES directory or mode")
    completion_path = target / "completion.json"
    if completion_path.is_symlink() or not completion_path.is_file():
        raise ValueError("TIDES package has no regular completion marker")
    complete = json.loads(completion_path.read_text())
    stored_binding = complete.get("run_binding")
    _validate_run_binding(stored_binding)
    if expected_binding is not None and stored_binding != expected_binding:
        raise ValueError("Completed TIDES run binding differs from current run")
    pins = complete.get("output_sha256")
    required = {
        "datapackage.json",
        "validation_report.json",
        "summary.json",
        "diagnostics.json",
        "vehicles.csv",
        "trips_performed.csv",
        "stop_visits.csv",
        "vehicle_locations.csv",
        "schemas/vendor.json",
    }
    if (
        complete.get("complete") is not True
        or complete.get("research_only") is not True
        or not isinstance(pins, dict)
        or not required.issubset(pins)
    ):
        raise ValueError("Incomplete TIDES completion contract")
    actual_files = set()
    for path in target.rglob("*"):
        if path.is_symlink():
            raise ValueError("Completed TIDES package contains a symlink")
        if path.is_file():
            actual_files.add(str(path.relative_to(target)))
    if actual_files != set(pins) | {"completion.json"}:
        raise ValueError("Completed TIDES package file inventory differs")
    for name, digest in pins.items():
        path = target / name
        if (
            not isinstance(name, str)
            or Path(name).is_absolute()
            or not path.resolve().is_relative_to(target.resolve())
            or _digest(path) != digest
        ):
            raise ValueError(f"Completed TIDES output missing or changed: {name}")
    validation = json.loads((target / "validation_report.json").read_text())
    summary = json.loads((target / "summary.json").read_text())
    if (
        not validation.get("valid")
        or not validation.get("profile_valid")
        or not validation.get("serialized_resources_valid")
        or summary.get("match_mode") != mode
        or summary.get("research_only") is not True
        or validation.get("vendor_sha256") != verify_schemas()["sha256"]
    ):
        raise ValueError("Completed TIDES validation/mode/schema contract differs")
    return {"path": str(target), "validation": validation, "summary": summary}
