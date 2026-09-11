"""Read-only, lazy adapters for inspecting persisted APC–AVL evidence.

Review cases are frozen experimental chunks or explicit APC review windows, not
verified performed trips. This module neither runs the matcher nor changes its
outputs. All numeric times use the original naive wall-clock coordinate axis.
"""

from __future__ import annotations

import collections
import copy
import hashlib
import json
import math
import re
import threading
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

METHODS = ("gap_only", "avl_context", "apc_assisted")
STREAM_METHODS = ("vehicle_only", "vehicle_only_dedup")
DIRECTION_METHOD = "direction_destination"
ALL_METHODS = (*METHODS, *STREAM_METHODS, DIRECTION_METHOD)
RAW_AVL_EXACT_FIELDS = (
    "Date_Key",
    "Message_DateTime",
    "Vehicle",
    "Latitude",
    "Longitude",
    "Heading",
    "Route",
    "Run",
    "Destination",
    "OffRoute",
    "Delayed",
    "KPH",
    "tatripId",
)
EPOCH = datetime(1970, 1, 1)
TIME_BASIS = (
    "Original Toronto naive wall-clock observations, encoded as seconds since "
    "naive 1970-01-01. These are NOT UTC instants. Display UTC clock fields only "
    "to preserve the source clock; no timezone conversion or DST correction."
)
JSON_INTEGER_ENCODING = (
    "Integers outside JavaScript's exact range [-9007199254740991, "
    "9007199254740991] are displayed as decimal strings without rounding. "
    "Original Parquet values and types are unchanged."
)
WARNING = (
    "Experimental chunks are candidate trips, not verified trip performed IDs. "
    "Local evidence support does not prove trip identity. APC grain and clock "
    "synchronization remain unverified; counts must not be summed as trip totals."
)
PROGRESS_DEFINITION = (
    "Cumulative observed GPS-to-GPS distance within each contiguous valid run, "
    "reset to zero at a break. Breaks occur at invalid/ambiguous/conflicting "
    "instants, gaps > 90 s, speed > 130 km/h, or omitted displayed neighbours. "
    "Unknown positions have null progress. Exact duplicates add no distance. "
    "This is not scheduled route progress; gaps are never interpolated."
)
DAY_BOUNDARY_WARNING = (
    "Single-day debug data are truncated at 00:00 and the following 00:00. "
    "These are data-scope boundaries, not verified trip starts or ends. "
    "Source timestamps and witness IDs are retained without clipping or fabrication; "
    "only the displayed timeline window is clipped to the debug date."
)
STREAM_WARNING = (
    "VehicleID-only source stream: AVL and APC rows are combined with UNION ALL "
    "and displayed in stable timestamp order. No trip segmentation, observation "
    "matching, nearest-GPS association, passenger aggregation, or GTFS is used. "
    "Same-time display order is not causal evidence. In-focus means vehicle-day "
    "membership only, never an accepted APC–AVL association."
)
DEDUP_STREAM_WARNING = (
    "VehicleID-only source stream with exact AVL duplicate removal: only one "
    "representative (minimum original source_row) of each identical set of all "
    "13 raw CSV field strings is displayed. No trimming, numeric normalization "
    "or hash-only equality is used. Every APC row remains unchanged. Removed AVL "
    "source identities remain in the complete deduplication mapping. This is not "
    "a display of every original AVL row, and no trip or observation matching is used."
)
DIRECTION_WARNING = (
    "Experimental direction-based candidate segments, not verified performed trips. "
    "Exact duplicate AVL copies and audited PI/PO AVL representatives are omitted; "
    "all APC rows remain. Candidate membership is not an APC–AVL match. Direction "
    "conflicts and neighbouring observations remain visible as distinct context. "
    "No observation matching, APC progress projection, GTFS or clock correction."
)


def wall_seconds(value) -> float | None:
    """Never call datetime.timestamp(): it depends on the host timezone."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise ValueError("Expected naive source wall-clock datetime")
        return (value - EPOCH).total_seconds()
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def json_safe(value):
    if isinstance(value, datetime):
        return wall_seconds(value)
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def _read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def _coordinates_valid(row: dict) -> bool:
    lat, lon = row.get("lat"), row.get("lon")
    return bool(
        isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
        and (lat != 0 or lon != 0)
    )


def _valid_ping(row: dict) -> bool:
    return bool(
        row.get("t") is not None
        and _coordinates_valid(row)
        and not any(
            row.get(field)
            for field in ("invalid", "time_ambiguous", "time_nonexistent")
        )
    )


def _distance(a: dict, b: dict) -> float:
    rad = math.pi / 180
    dy = (b["lat"] - a["lat"]) * rad
    dx = (b["lon"] - a["lon"]) * rad * math.cos((a["lat"] + b["lat"]) * rad / 2)
    return 6371008.8 * math.hypot(dx, dy)


def _bounds(row: dict) -> tuple[float | None, float | None]:
    times = [row.get(k) for k in ("arrival", "departure") if row.get(k) is not None]
    return (min(times), max(times)) if times else (None, None)


def _overlaps(start, end, lower, upper) -> bool:
    return start is not None and lower is not None and start <= upper and end >= lower


def _routes(rows) -> list[str]:
    return sorted({str(row["route"]) for row in rows if row.get("route")})


def _all_ping_ids(value) -> set[str]:
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "ping_ids" and isinstance(item, list):
                result.update(item)
            else:
                result.update(_all_ping_ids(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_all_ping_ids(item))
    return result


def _owners(decision: dict, candidate: bool = False) -> set[str]:
    owners = set(decision.get("candidate_owners" if candidate else "segment_ids") or [])
    if candidate:
        for item in decision.get("candidates") or []:
            owners.update(item.get("segment_ids") or [])
            if item.get("segment_id"):
                owners.add(item["segment_id"])
    elif decision.get("segment_id"):
        owners.add(decision["segment_id"])
    return owners


def _warning_flags(row: dict, decision: dict) -> list[str]:
    flags = []
    for field in (
        "invalid",
        "grain_uncertain",
        "time_ambiguous",
        "time_nonexistent",
    ):
        if row.get(field) or decision.get(field):
            flags.append(field)
    for field in (
        "route_conflict",
        "interval_excursion",
        "coverage_start_missing",
        "coverage_end_missing",
    ):
        if decision.get(field):
            flags.append(field)
    flags.extend(decision.get("overlap_flags") or [])
    if decision.get("context_status") in {"multiple_segments", "unmapped"}:
        flags.append(decision["context_status"])
    if decision.get("status") not in {"direct", "partial"}:
        flags.append(decision.get("status") or "missing_decision")
    if decision.get("counter_witnesses"):
        flags.append("counter_evidence")
    return sorted(set(flags))


def _decorate_progress(rows: list[dict]) -> None:
    """Annotate raw rows without deleting duplicates or manufacturing positions."""
    groups = collections.defaultdict(list)
    for row in rows:
        if row["t"] is not None:
            groups[row["t"]].append(row)
    previous = None
    distance = 0.0
    run = 0
    for instant in sorted(groups):
        group = groups[instant]
        conflict = len({(r.get("lat"), r.get("lon")) for r in group}) > 1
        valid = not conflict and all(_valid_ping(r) for r in group)
        representative = group[0]
        speed = None
        reason = None
        if not valid:
            reason = "duplicate_coordinate_conflict" if conflict else "invalid_instant"
        elif previous is None:
            reason = "start_or_after_invalid"
        else:
            gap = instant - previous["t"]
            step = _distance(previous, representative)
            speed = step / gap * 3.6 if gap > 0 else None
            if gap > 90:
                reason = "gap_over_90s"
            elif speed is not None and speed > 130:
                reason = "speed_over_130kph"
            else:
                distance += step
        if reason is not None:
            distance = 0.0
            run += 1
        for position, row in enumerate(group):
            row.update(
                valid=valid,
                break_before=reason is not None and position == 0,
                break_reason=reason,
                duplicate_count=len(group),
                duplicate_conflict=conflict,
                progress_m=distance if valid else None,
                progress_run=run if valid else None,
                speed_kph=speed if valid else None,
            )
        previous = representative if valid else None
    for row in rows:
        if row["t"] is None:
            row.update(
                valid=False,
                break_before=True,
                break_reason="missing_time",
                duplicate_count=1,
                duplicate_conflict=False,
                progress_m=None,
                progress_run=None,
                speed_kph=None,
            )


def _direction_json_times(value, *, source_record=False):
    """Decode persisted direction evidence clocks, without interpreting source text."""
    if isinstance(value, list):
        return [
            _direction_json_times(item, source_record=source_record) for item in value
        ]
    if not isinstance(value, dict):
        return value
    result = {}
    clock_fields = {
        "start",
        "end",
        "at",
        "arrival",
        "departure",
        "event_time",
        "time",
        "window_start",
        "window_end",
        "start_time",
        "end_time",
        "first_definite_at",
        "previous_definite_at",
        "source_start",
        "source_end",
        "direction_evidence_start",
        "direction_evidence_end",
    }
    for key, item in value.items():
        if source_record:
            result[key] = (
                wall_seconds(datetime.fromisoformat(item))
                if key in {"time", "arrival", "departure"} and isinstance(item, str)
                else copy.deepcopy(item)
            )
            continue
        if key in clock_fields and isinstance(item, str):
            item = wall_seconds(datetime.fromisoformat(item))
        result[key] = _direction_json_times(item, source_record=key == "filtered_avl")
    return result


def _decorate_selected_progress(rows, positions):
    """Never draw through a filtered or omitted original representative."""
    blocks = []
    previous = None
    for row in rows:
        index = positions[row["rid"]]
        if previous is None or index != previous + 1:
            blocks.append([])
        blocks[-1].append(row)
        previous = index
    run_offset = 0
    for block in blocks:
        _decorate_progress(block)
        if block[0].get("t") is not None:
            block[0]["break_before"] = True
            block[0]["break_reason"] = "display_or_filter_boundary"
        maximum = 0
        for row in block:
            if row.get("progress_run") is not None:
                maximum = max(maximum, row["progress_run"])
                row["progress_run"] += run_offset
        run_offset += maximum + 1


class ReviewData:
    """Read at most two vehicle payloads into memory; load methods on demand."""

    def __init__(self, run: Path, *, verify_dependencies=None):
        self.run = Path(run).resolve()
        self._verify_dependencies = verify_dependencies
        self.manifest = _read_json(self.run / "manifest.json")
        selection = _read_json(self.run / "selection.json")
        if not isinstance(self.manifest, dict) or not isinstance(selection, list):
            raise ValueError("Run requires manifest.json and selection.json")
        methods = self.manifest.get("methods", METHODS)
        if (
            not isinstance(methods, (list, tuple))
            or not methods
            or len(set(methods)) != len(methods)
            or any(method not in ALL_METHODS for method in methods)
        ):
            raise ValueError("Manifest methods must be a nonempty known method list")
        self.methods = tuple(methods)
        if (
            any(
                method in (*STREAM_METHODS, DIRECTION_METHOD) for method in self.methods
            )
            and len(self.methods) != 1
        ):
            raise ValueError(
                "Vehicle-only representation cannot mix segmentation methods"
            )
        self.vehicle_stream = self.methods[0] in STREAM_METHODS
        self.direction_candidates = self.methods == (DIRECTION_METHOD,)
        self.avl_deduplication = self.methods in (
            ("vehicle_only_dedup",),
            (DIRECTION_METHOD,),
        )
        self._verified_raw_fields = None
        self.debug_date = self.manifest.get("debug_date")
        scope = self.manifest.get("scope") or {}
        self.scope = {"start": None, "end": None}
        if self.debug_date:
            beginning = datetime.strptime(self.debug_date, "%Y-%m-%d")
            self.scope = {
                "start": wall_seconds(beginning),
                "end": wall_seconds(beginning + timedelta(days=1)),
            }
        if isinstance(scope, dict) and scope.get("start") is not None:
            for key in ("start", "end"):
                value = scope.get(key)
                if isinstance(value, str):
                    value = datetime.fromisoformat(value)
                self.scope[key] = wall_seconds(value)
        if self.scope["start"] is not None and (
            self.scope["end"] is None or self.scope["end"] <= self.scope["start"]
        ):
            raise ValueError("Invalid debug time scope")
        if (self.vehicle_stream or self.direction_candidates) and self.scope[
            "start"
        ] is None:
            raise ValueError("Vehicle-only stream requires an explicit day scope")
        self.audit = self._manifest_path("audit")
        self.baseline = self._manifest_path("baseline")
        self.vehicles = {}
        for item in selection:
            key, label = item["vehicle_case"], item["raw_vehicle"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key) or key in self.vehicles:
                raise ValueError("Invalid or duplicate vehicle case in selection")
            self.vehicles[key] = label
        self._cache = collections.OrderedDict()
        self._lock = threading.RLock()
        self._summary = _read_json(self.run / "summary.json", {})
        self._config = _read_json(self.run / "config.json", {})

    def _manifest_path(self, name: str) -> Path:
        path = Path(self.manifest[name])
        return path.resolve() if path.is_absolute() else (self.run / path).resolve()

    def metadata(self) -> dict:
        if self._verify_dependencies is not None:
            self._verify_dependencies()
        return copy.deepcopy(
            {
                "run_id": self.run.name,
                "run_path": str(self.run),
                "methods": list(self.methods),
                "default_method": (
                    "avl_context" if "avl_context" in self.methods else self.methods[0]
                ),
                "debug_date": self.debug_date,
                "scope": self.scope,
                "day_boundary_warning": DAY_BOUNDARY_WARNING
                if self.scope["start"] is not None
                else None,
                "default_vehicle": next(
                    (key for key, value in self.vehicles.items() if value == "9027"),
                    next(iter(self.vehicles), None),
                ),
                "vehicles": [
                    {"id": key, "label": value} for key, value in self.vehicles.items()
                ],
                "summary": self._summary,
                "representation": "direction_candidate_segments"
                if self.direction_candidates
                else "vehicle_observation_stream"
                if self.vehicle_stream
                else "apc_avl_direct_evidence",
                "trip_segmentation_used": not self.vehicle_stream,
                "observation_matching_used": not (
                    self.vehicle_stream or self.direction_candidates
                ),
                "segmentation_used": not self.vehicle_stream,
                "matching_used": not (self.vehicle_stream or self.direction_candidates),
                "avl_deduplication": self.avl_deduplication,
                "deduplication_performed": self.avl_deduplication,
                "filtering": copy.deepcopy(self.manifest.get("filtering")),
                "direction_segmentation": copy.deepcopy(
                    self.manifest.get("direction_segmentation")
                ),
                "time_basis": TIME_BASIS,
                "json_integer_encoding": JSON_INTEGER_ENCODING,
                "warning": DIRECTION_WARNING
                if self.direction_candidates
                else DEDUP_STREAM_WARNING
                if self.avl_deduplication
                else (STREAM_WARNING if self.vehicle_stream else WARNING),
                "progress_definition": PROGRESS_DEFINITION,
                "config": self._config,
                "merge_config": self._config.get("merge", self._config),
                "catalog_definition": DIRECTION_WARNING
                if self.direction_candidates
                else (
                    DEDUP_STREAM_WARNING if self.avl_deduplication else STREAM_WARNING
                )
                if self.vehicle_stream
                else (
                    "Frozen chunks plus 15-minute APC review buckets for rows "
                    "without one accepted owner. These are review windows, not "
                    "additional inferred trips. Context rows are not counted as "
                    "accepted associations. Multi-owner rows can appear in more "
                    "than one chunk and also an unresolved review window."
                ),
            }
        )

    def _validate(self, method: str, vehicle: str) -> None:
        if method not in self.methods:
            raise ValueError("Unknown segmentation method")
        if vehicle not in self.vehicles:
            raise ValueError("Unknown vehicle case")

    def _vehicle(self, vehicle: str) -> dict:
        # A cached payload is not proof that its raw inputs still exist unchanged.
        if self._verify_dependencies is not None:
            self._verify_dependencies()
        if vehicle in self._cache:
            self._cache.move_to_end(vehicle)
            return self._cache[vehicle]
        label = self.vehicles[vehicle]
        payload = {}
        for source in ("avl", "apc"):
            frame = (
                pl.scan_parquet(self.audit / f"{source}.parquet")
                .filter(pl.col("vehicle") == label)
                .collect()
            )
            rows = json_safe(frame.to_dicts())
            if len({r["rid"] for r in rows}) != len(rows):
                raise ValueError(f"Duplicate raw row ID in {source}")
            if source == "avl":
                for row in rows:
                    row["t"] = row.get("time")
                rows.sort(key=lambda r: (r["t"] is None, r["t"] or 0, r["rid"]))
                _decorate_progress(rows)
            else:
                rows.sort(
                    key=lambda r: (
                        _bounds(r)[0] is None,
                        _bounds(r)[0] or 0,
                        r["rid"],
                    )
                )
            payload[source] = rows
            payload[source + "_by_id"] = {r["rid"]: r for r in rows}
        payload["methods"] = {}
        payload["avl_times"] = [
            row["t"] for row in payload["avl"] if row["t"] is not None
        ]
        self._cache[vehicle] = payload
        while len(self._cache) > 2:
            self._cache.popitem(last=False)
        return payload

    def _method(self, method: str, vehicle: str) -> tuple[dict, dict]:
        self._validate(method, vehicle)
        payload = self._vehicle(vehicle)
        if method in payload["methods"]:
            return payload, payload["methods"][method]
        if method == DIRECTION_METHOD:
            state = self._build_direction(vehicle, payload)
            payload["methods"][method] = state
            return payload, state
        if method in STREAM_METHODS:
            state = self._build_stream(vehicle, payload)
            payload["methods"][method] = state
            return payload, state
        decision_path = self.run / "assignments" / method / f"{vehicle}.parquet"
        # The frozen evaluator deliberately writes no parquet for empty APC sets.
        decisions = (
            json_safe(pl.read_parquet(decision_path).to_dicts())
            if decision_path.exists()
            else []
        )
        by_id = {r["apc_rid"]: r for r in decisions}
        if len(by_id) != len(decisions) or set(by_id) != set(payload["apc_by_id"]):
            raise ValueError(
                "Persisted decisions must cover every APC row exactly once"
            )
        chunk_path = self.baseline / "chunks" / method / f"{vehicle}.parquet"
        chunks = (
            json_safe(pl.read_parquet(chunk_path).to_dicts())
            if chunk_path.exists()
            else []
        )
        chunk_map, membership = {}, {}
        for chunk in chunks:
            key = chunk["segment_id"]
            if key in chunk_map:
                raise ValueError("Duplicate frozen segment ID")
            chunk_map[key] = chunk
            for rid in chunk.get("ping_ids") or []:
                if rid in membership or rid not in payload["avl_by_id"]:
                    raise ValueError("Invalid frozen AVL membership")
                membership[rid] = key
        related = collections.defaultdict(set)
        review_groups = collections.defaultdict(list)
        for row in payload["apc"]:
            decision = by_id[row["rid"]]
            accepted = (
                _owners(decision)
                if decision.get("status") in {"direct", "partial"}
                else set()
            )
            candidates = _owners(decision, candidate=True)
            for owner in accepted | candidates:
                if owner in chunk_map:
                    related[owner].add(row["rid"])
            if len(accepted) != 1 or not accepted.issubset(chunk_map):
                start, _ = _bounds(row)
                kind = (
                    "apc_only"
                    if decision.get("status") == "unmatched" and not candidates
                    else "unresolved"
                )
                bucket = math.floor(start / 900) if start is not None else "untimed"
                review_groups[kind, bucket].append(row["rid"])
        method_data = {
            "decisions": by_id,
            "chunks": chunk_map,
            "membership": membership,
            "focus": {},
            "summaries": {},
        }
        for key, chunk in chunk_map.items():
            ping_rows = [
                payload["avl_by_id"][rid] for rid in chunk.get("ping_ids") or []
            ]
            focus = sorted(related[key])
            summary = self._summary_row(
                vehicle,
                key,
                "segment",
                chunk.get("start"),
                chunk.get("end"),
                ping_rows,
                focus,
                payload,
                by_id,
            )
            summary["boundary_before"] = chunk.get("boundary_before") or []
            summary["segment_flags"] = [
                field
                for field in (
                    "possible_terminal",
                    "time_ambiguous",
                    "time_nonexistent",
                    "position_uncertain",
                    "chronology_unresolved",
                )
                if chunk.get(field)
            ]
            summary["warning_count"] += len(summary["segment_flags"])
            if (
                summary["start"] is not None
                and summary["end"] is not None
                and summary["end"] - summary["start"] > 7200
            ):
                summary["segment_flags"].append("long_chunk_over_2h_not_verified_trip")
                summary["warning_count"] += 1
            summary["source_trip_labels"] = sorted(
                {str(row["source_trip"]) for row in ping_rows if row.get("source_trip")}
            )
            method_data["summaries"][key] = summary
            method_data["focus"][key] = set(focus)
        for (kind, bucket), focus in review_groups.items():
            digest = hashlib.sha256(
                (method + "|" + vehicle + "|" + kind + "|" + str(bucket)).encode()
            ).hexdigest()[:20]
            key = f"{kind}-{digest}"
            times = [
                value
                for rid in focus
                for value in _bounds(payload["apc_by_id"][rid])
                if value is not None
            ]
            summary = self._summary_row(
                vehicle,
                key,
                kind,
                min(times) if times else None,
                max(times) if times else None,
                [],
                focus,
                payload,
                by_id,
            )
            method_data["summaries"][key] = summary
            method_data["focus"][key] = set(focus)
        method_data["catalog"] = sorted(
            method_data["summaries"].values(),
            key=lambda r: (r["start"] is None, r["start"] or 0, r["kind"], r["id"]),
        )
        payload["methods"][method] = method_data
        return payload, method_data

    def _verified_dedup_fields_path(self) -> Path:
        spec = self.manifest.get("deduplication") or {}
        if (
            tuple(spec.get("fields") or []) != RAW_AVL_EXACT_FIELDS
            or spec.get("rule") != "raw_avl_all_13_fields_exact_min_source_row"
            or spec.get("keep") != "minimum_source_row"
            or spec.get("source_row_origin") != 1
            or spec.get("raw_fields_path") != "deduplication/raw_avl_fields.parquet"
        ):
            raise ValueError("Unsupported exact AVL deduplication provenance")
        fixture = _read_json(self.audit / "manifest.json", {})
        source_audit = fixture.get("source_audit")
        expected_inventory = (fixture.get("input_sha256") or {}).get("inventory.json")
        if not source_audit or not expected_inventory:
            raise ValueError(
                "Raw AVL field provenance requires original audit identity"
            )
        source_audit = Path(source_audit)
        if not source_audit.is_absolute():
            source_audit = self.audit / source_audit
        inventory_path = source_audit / "inventory.json"
        with inventory_path.open("rb") as stream:
            inventory_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        inventory = _read_json(inventory_path, {})
        if inventory_hash != expected_inventory or (inventory.get("sources") or {}).get(
            "avl", {}
        ).get("sha256") != spec.get("raw_csv_sha256"):
            raise ValueError("Raw AVL CSV identity disagrees with verified audit")
        path = self.run / "deduplication" / "raw_avl_fields.parquet"
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns, spec.get("raw_fields_sha256"))
        if signature != self._verified_raw_fields:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != spec.get("raw_fields_sha256"):
                raise ValueError("Raw AVL exact-field evidence checksum mismatch")
            self._verified_raw_fields = signature
        return path

    def _deduplicated_avl(self, vehicle: str, payload: dict):
        """Independently rebuild exact raw-string groups, not hash-only groups."""
        path = self._verified_dedup_fields_path()
        fields = (
            pl.scan_parquet(path)
            .filter(pl.col("vehicle") == self.vehicles[vehicle])
            .collect()
            .to_dicts()
        )
        original = payload["avl_by_id"]
        keys = {row["source_rid"]: row for row in fields}
        if len(keys) != len(fields) or set(keys) != set(original):
            raise ValueError(
                "Raw exact-field evidence must cover every original AVL row"
            )
        mapping_path = self.run / "deduplication" / f"{vehicle}.parquet"
        mapping = (
            pl.read_parquet(mapping_path).to_dicts() if mapping_path.exists() else []
        )
        mappings = {row["source_rid"]: row for row in mapping}
        if len(mappings) != len(mapping) or set(mappings) != set(original):
            raise ValueError("Deduplication mapping must cover every original AVL row")
        groups = collections.defaultdict(list)
        seen_rows = set()
        for rid, key in keys.items():
            number = original[rid].get("source_row")
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or number < 1
                or key.get("source_row") != number
                or number in seen_rows
            ):
                raise ValueError(
                    "Raw exact fields changed original source row identity"
                )
            seen_rows.add(number)
            if any(
                field not in key or not isinstance(key[field], (str, type(None)))
                for field in RAW_AVL_EXACT_FIELDS
            ):
                raise ValueError(
                    "Exact deduplication requires raw CSV strings, "
                    "not normalized values"
                )
            groups[tuple(key[field] for field in RAW_AVL_EXACT_FIELDS)].append(rid)
        representatives, lineage, group_ids = set(), {}, set()
        for members in groups.values():
            members.sort(key=lambda rid: (original[rid]["source_row"], rid))
            representative = members[0]
            group_id = mappings[representative].get("group_id")
            if not isinstance(group_id, str) or not group_id or group_id in group_ids:
                raise ValueError(
                    "Duplicate-group identity disagrees with raw field groups"
                )
            group_ids.add(group_id)
            for rid in members:
                row = mappings[rid]
                if (
                    row.get("source") != "avl"
                    or row.get("representative_rid") != representative
                    or row.get("source_row") != original[rid]["source_row"]
                    or row.get("representative_source_row")
                    != original[representative]["source_row"]
                    or row.get("is_representative") is not (rid == representative)
                    or row.get("duplicate_group_size") != len(members)
                    or row.get("group_id") != group_id
                ):
                    raise ValueError(
                        "AVL deduplication mapping differs from exact raw-field groups"
                    )
            representatives.add(representative)
            lineage[representative] = {
                "representative_rid": representative,
                "duplicate_group_size": len(members),
                "source_rids": members,
                "source_rows": [original[rid]["source_row"] for rid in members],
                "group_id": group_id,
                "raw_fields": {
                    field: keys[representative][field] for field in RAW_AVL_EXACT_FIELDS
                },
            }
        retained = [
            copy.deepcopy(row)
            for row in payload["avl"]
            if row["rid"] in representatives
        ]
        # The visual series is recomputed from displayed representatives only.
        # Canonical source rows remain untouched in the two-vehicle cache.
        _decorate_progress(retained)
        return retained, json_safe(mapping), lineage

    def _read_observations(
        self, vehicle: str, payload: dict, display_avl: list
    ) -> list:
        """Verify persisted source identity, payload and order independently."""
        path = self.run / "observations" / f"{vehicle}.parquet"
        observations = json_safe(pl.read_parquet(path).to_dicts())
        expected = {
            (source, row["rid"]): row
            for source, rows in (("avl", display_avl), ("apc", payload["apc"]))
            for row in rows
        }
        observed = [(row.get("source"), row.get("rid")) for row in observations]
        if len(observed) != len(set(observed)) or set(observed) != set(expected):
            raise ValueError(
                "Vehicle stream must conserve every source row exactly once"
            )
        previous_key = None
        for index, row in enumerate(observations):
            source = row["source"]
            original = expected[source, row["rid"]]
            if source == "avl":
                instant, basis = original.get("time"), "avl.time"
            elif original.get("arrival") is not None:
                instant, basis = original["arrival"], "apc.arrival"
            else:
                instant, basis = original.get("departure"), "apc.departure_fallback"
            if instant is None:
                basis = "missing"
            if (
                row.get("event_time") != instant
                or row.get("event_time_basis") != basis
                or row.get("display_order") != index
                or row.get("source_rid", row["rid"]) != row["rid"]
            ):
                raise ValueError(
                    "Vehicle stream order/time lineage disagrees with source"
                )
            order_key = (
                instant is None,
                instant or 0,
                source != "avl",
                original.get("source_row") is None,
                original.get("source_row") or 0,
                row["rid"],
            )
            if previous_key is not None and order_key < previous_key:
                raise ValueError(
                    "Vehicle stream is not in stable source timestamp order"
                )
            previous_key = order_key
            raw = (
                {
                    key: value
                    for key, value in original.items()
                    if key
                    not in {
                        "t",
                        "valid",
                        "break_before",
                        "break_reason",
                        "duplicate_count",
                        "duplicate_conflict",
                        "progress_m",
                        "progress_run",
                        "speed_kph",
                    }
                }
                if source == "avl"
                else original
            )
            if any(key not in row or row[key] != value for key, value in raw.items()):
                raise ValueError("Vehicle stream changed an original source field")
            row["time_basis"] = basis
            row["source_rid"] = row["rid"]
            row["source_fields"] = copy.deepcopy(raw)
        return observations

    def _build_stream(self, vehicle: str, payload: dict) -> dict:
        """Read an already persisted UNION ALL stream, never join its observations."""
        display_avl, mapping, lineage = (
            self._deduplicated_avl(vehicle, payload)
            if self.avl_deduplication
            else (payload["avl"], [], {})
        )
        observations = self._read_observations(vehicle, payload, display_avl)
        start, end = self.scope["start"], self.scope["end"]
        case_id = (
            "vehicle-day-"
            + hashlib.sha256(f"{vehicle}|{start}|{end}".encode()).hexdigest()[:20]
        )
        avl_count, apc_count = len(payload["avl"]), len(payload["apc"])
        retained_avl_count = len(display_avl)
        removed_avl = avl_count - retained_avl_count
        for observation in observations:
            if observation["source"] == "avl" and self.avl_deduplication:
                observation["deduplication"] = copy.deepcopy(
                    lineage[observation["rid"]]
                )
        summary = {
            "id": case_id,
            "kind": "vehicle_stream",
            "vehicle": vehicle,
            "vehicle_label": self.vehicles[vehicle],
            "date": self.debug_date
            or (EPOCH + timedelta(seconds=start)).date().isoformat(),
            "start": start,
            "end": end,
            "source_start": start,
            "source_end": end,
            "routes": _routes(payload["avl"] + payload["apc"]),
            "avl_count": retained_avl_count,
            "avl_member_count": 0,
            "window_avl_count": self._window_count(
                {
                    "avl_times": [
                        row["t"] for row in display_avl if row["t"] is not None
                    ]
                },
                start,
                end,
            ),
            "context_avl_count": retained_avl_count,
            "apc_count": apc_count,
            "apc_rids": sorted(payload["apc_by_id"]),
            "observation_count": len(observations),
            "source_counts": {"avl": avl_count, "apc": apc_count},
            "retained_counts": {"avl": retained_avl_count, "apc": apc_count},
            "removed_avl": removed_avl,
            "avl_deduplication": self.avl_deduplication,
            "status_counts": {"not_evaluated": apc_count},
            "accepted_count": 0,
            "candidate_count": 0,
            "warning_count": sum(
                bool(
                    row.get("invalid")
                    or row.get("time_ambiguous")
                    or row.get("time_nonexistent")
                )
                for row in display_avl + payload["apc"]
            ),
            "title": "车辆日源观测流 · AVL 完全重复副本已合并"
            if self.avl_deduplication
            else "车辆日源观测流 · 仅 VehicleID 合并",
            "date_clipped_start": False,
            "date_clipped_end": False,
            "scope_limited": True,
            "trip_segmentation_used": False,
            "observation_matching_used": False,
        }
        return {
            "catalog": [summary],
            "summaries": {case_id: summary},
            "focus": {case_id: set(payload["apc_by_id"])},
            "observations": observations,
            "display_avl": display_avl,
            "deduplication": mapping,
            "lineage": lineage,
            "observation_by_id": {
                (row["source"], row["rid"]): row for row in observations
            },
        }

    def _build_direction(self, vehicle: str, payload: dict) -> dict:
        """Read candidate boundaries; independently verify source/filter lineage."""
        representatives, mapping, lineage = self._deduplicated_avl(vehicle, payload)
        document = _read_json(self.run / "direction" / f"{vehicle}.json")
        if not isinstance(document, dict) or any(
            not isinstance(document.get(key), list)
            for key in ("segments", "conflicts", "filtered_avl")
        ):
            raise ValueError(
                "Direction metadata requires segments, conflicts and filter audit"
            )
        document = json_safe(_direction_json_times(document))
        expected_filtered = {
            row["rid"]: row
            for row in representatives
            if str(row.get("route") or "").strip().upper() in {"PI", "PO"}
        }
        filtered = {row["rid"]: row for row in document["filtered_avl"]}
        if len(filtered) != len(document["filtered_avl"]) or set(filtered) != set(
            expected_filtered
        ):
            raise ValueError(
                "PI/PO filter audit must identify exactly the excluded "
                "AVL representatives"
            )
        decorated_fields = {
            "t",
            "valid",
            "break_before",
            "break_reason",
            "duplicate_count",
            "duplicate_conflict",
            "progress_m",
            "progress_run",
            "speed_kph",
        }
        for rid, row in filtered.items():
            original = expected_filtered[rid]
            if (
                row.get("filter_reason") != "avl_route_pi_po"
                or row.get("filter_normalized_route")
                != str(original.get("route") or "").strip().upper()
                or any(
                    key not in row or row[key] != value
                    for key, value in original.items()
                    if key not in decorated_fields
                )
            ):
                raise ValueError(
                    "PI/PO filter audit changed original fields or exclusion reason"
                )
            row["deduplication"] = copy.deepcopy(lineage[rid])
        display_avl = [row for row in representatives if row["rid"] not in filtered]
        observations = self._read_observations(vehicle, payload, display_avl)
        by_id = {(row["source"], row["rid"]): row for row in observations}
        segments = {row["id"]: row for row in document["segments"]}
        conflicts = {row["id"]: row for row in document["conflicts"]}
        if len(segments) != len(document["segments"]) or len(conflicts) != len(
            document["conflicts"]
        ):
            raise ValueError("Duplicate direction segment or conflict identity")

        def identities(items):
            if not isinstance(items, list):
                raise ValueError(
                    "Direction evidence requires explicit source identities"
                )
            keys = [(item.get("source"), item.get("rid")) for item in items]
            if len(set(keys)) != len(keys) or any(key not in by_id for key in keys):
                raise ValueError(
                    "Direction evidence has duplicate or absent source identities"
                )
            return set(keys)

        members = {}
        for key, segment in segments.items():
            if (
                not isinstance(key, str)
                or not key
                or segment.get("direction") not in {None, "NORTH", "SOUTH"}
                or segment.get("candidate_only") is not True
                or segment.get("complete_trip") is not False
                or segment.get("start") is None
                or segment.get("end") is None
                or segment["end"] < segment["start"]
                or segment["start"] < self.scope["start"]
                or segment["end"] > self.scope["end"]
            ):
                raise ValueError("Invalid candidate direction segment or day boundary")
            members[key] = identities(segment.get("source_rids"))
            identities(segment.get("boundary_evidence", []))
            identities(segment.get("previous_direction_evidence", []))
            if not set(segment.get("conflict_ids", [])).issubset(conflicts):
                raise ValueError("Candidate references missing direction conflict")
        conflict_members = {}
        for key, conflict in conflicts.items():
            if (
                not isinstance(key, str)
                or not key
                or conflict.get("start") is None
                or conflict.get("end") is None
                or conflict["end"] < conflict["start"]
                or conflict["start"] < self.scope["start"]
                or conflict["end"] > self.scope["end"]
            ):
                raise ValueError("Invalid direction conflict identity or time bounds")
            conflict_members[key] = identities(conflict.get("source_rids"))
            north = identities(conflict.get("north_source_rids", []))
            south = identities(conflict.get("south_source_rids", []))
            if (
                north & south
                or north | south != conflict_members[key]
                or not north
                or not south
            ):
                raise ValueError(
                    "Direction conflict must preserve both opposite evidence sides"
                )
            for direction, keys in (("NORTH", north), ("SOUTH", south)):
                if any(
                    by_id[item].get("direction_label") != direction for item in keys
                ):
                    raise ValueError(
                        "Direction conflict sides disagree with source direction"
                    )
            conflict["directions"] = ["NORTH", "SOUTH"]
            conflict["severity"] = "review"
            conflict["description"] = (
                "Opposite source direction evidence overlaps here; this is a "
                "review window, not a verified mismatch or an independent trip event."
            )
        expected_members = collections.defaultdict(set)
        expected_conflicts = collections.defaultdict(set)
        for key, identities_set in members.items():
            for identity in identities_set:
                expected_members[identity].add(key)
        for key, identities_set in conflict_members.items():
            for identity in identities_set:
                expected_conflicts[identity].add(key)
        for identity, row in by_id.items():
            source = row["source"]
            text = row.get("destination") if source == "avl" else row.get("direction")
            if source == "avl":
                first_word = (
                    re.match(r"^\s*([A-Za-z]+)\b", text)
                    if isinstance(text, str)
                    else None
                )
                label = first_word.group(1).upper() if first_word else None
            else:
                label = text.strip().upper() if isinstance(text, str) else None
            label = label if label in {"NORTH", "SOUTH"} else None
            own_members = row.get("candidate_segment_ids")
            own_conflicts = row.get("direction_conflict_ids")
            if (
                row.get("direction_label") != label
                or row.get("direction_basis")
                != (
                    "avl.destination_first_word" if source == "avl" else "apc.direction"
                )
                or not isinstance(own_members, list)
                or len(own_members) != len(set(own_members))
                or set(own_members) != expected_members[identity]
                or not isinstance(own_conflicts, list)
                or len(own_conflicts) != len(set(own_conflicts))
                or set(own_conflicts) != expected_conflicts[identity]
            ):
                raise ValueError(
                    "Direction labels, candidate membership or conflicts "
                    "disagree with source evidence"
                )
            if any(
                field in row and field not in row["source_fields"]
                for field in ("status", "score", "witnesses", "ping_ids", "segment_id")
            ):
                raise ValueError(
                    "Direction candidates must not contain fabricated "
                    "observation matches"
                )
            row["candidate_ids"] = list(own_members)
            if source == "avl":
                row["deduplication"] = copy.deepcopy(lineage[row["rid"]])
        unresolved = {identity for identity in by_id if not expected_members[identity]}
        focus = dict(members)
        if unresolved or not focus:
            key = (
                "direction-unresolved-"
                + hashlib.sha256(vehicle.encode()).hexdigest()[:20]
            )
            if key in segments:
                raise ValueError(
                    "Reserved unresolved review identity used by a candidate"
                )
            focus[key] = unresolved
        source_counts = {"avl": len(payload["avl"]), "apc": len(payload["apc"])}
        retained_counts = {"avl": len(display_avl), "apc": len(payload["apc"])}
        summaries = {}
        avl_times = [row["t"] for row in display_avl if row["t"] is not None]
        for key, identities_set in focus.items():
            segment = segments.get(key)
            rows = [by_id[item] for item in identities_set]
            if segment:
                start, end = segment["start"], segment["end"]
            else:
                start, end = self.scope["start"], self.scope["end"]
            lower, upper, clipped_start, clipped_end = self._clip_window(start, end)
            source_instants = [
                value
                for row in rows
                for value in (
                    (row.get("event_time"),) if row["source"] == "avl" else _bounds(row)
                )
                if value is not None
            ]
            conflict_ids = sorted(
                set(segment.get("conflict_ids", [])) if segment else set()
            )
            conflict_ids = sorted(
                set(conflict_ids)
                | {
                    cid
                    for identity in identities_set
                    for cid in expected_conflicts[identity]
                }
            )
            avl_count = sum(r["source"] == "avl" for r in rows)
            apc_ids = sorted(r["rid"] for r in rows if r["source"] == "apc")
            direction = segment.get("direction") if segment else None
            summaries[key] = {
                "id": key,
                "kind": "direction_segment" if segment else "direction_unresolved",
                "vehicle": vehicle,
                "vehicle_label": self.vehicles[vehicle],
                "date": self.debug_date,
                "start": lower,
                "end": upper,
                "source_start": min(source_instants) if source_instants else None,
                "source_end": max(source_instants) if source_instants else None,
                "direction": direction,
                "direction_label": direction or "UNKNOWN",
                "routes": _routes(rows),
                "avl_count": avl_count,
                "avl_member_count": avl_count if segment else 0,
                "apc_member_count": len(apc_ids) if segment else 0,
                "apc_count": len(apc_ids),
                "apc_rids": apc_ids,
                "observation_count": len(rows),
                "window_avl_count": self._window_count(
                    {"avl_times": avl_times}, lower, upper
                ),
                "status_counts": {"not_evaluated": len(apc_ids)},
                "accepted_count": 0,
                "candidate_count": len(rows) if segment else 0,
                "warning_count": len(conflict_ids) + (direction is None),
                "direction_conflict_count": len(conflict_ids),
                "conflict_ids": conflict_ids,
                "source_counts": source_counts,
                "retained_counts": retained_counts,
                "deduplicated_counts": {
                    "avl": len(representatives),
                    "apc": len(payload["apc"]),
                },
                "removed_avl": len(payload["avl"]) - len(representatives),
                "filtered_avl_pi_po": len(filtered),
                "avl_deduplication": True,
                "title": f"方向候选片段 · {direction or '未知方向'}"
                if segment
                else "未归入方向候选片段的源记录"
                if rows
                else "车辆日排除记录审计 · 无候选片段",
                "candidate_only": bool(segment),
                "complete_trip": False,
                "date_clipped_start": clipped_start,
                "date_clipped_end": clipped_end,
                "scope_limited": True,
                "scope_boundary_start": start == self.scope["start"],
                "scope_boundary_end": end == self.scope["end"],
                "scope_censored_start": start == self.scope["start"],
                "scope_censored_end": end == self.scope["end"],
                "first_definite_at": segment.get("first_definite_at")
                if segment
                else None,
                "trip_segmentation_used": True,
                "observation_matching_used": False,
                "boundary_reason": segment.get("boundary_reason") if segment else None,
                "boundary_phase": segment.get("boundary_phase") if segment else None,
                "unknown_duration_s": segment.get("unknown_duration_s")
                if segment
                else None,
                "conflict_duration_s": segment.get("conflict_duration_s")
                if segment
                else None,
            }
        return {
            "catalog": sorted(
                summaries.values(),
                key=lambda r: (r["start"] is None, r["start"] or 0, r["id"]),
            ),
            "summaries": summaries,
            "focus": focus,
            "segments": segments,
            "conflicts": conflicts,
            "conflict_members": conflict_members,
            "observations": observations,
            "observation_by_id": by_id,
            "display_avl": display_avl,
            "deduplication": mapping,
            "lineage": lineage,
            "filtered_avl": list(filtered.values()),
            "representative_positions": {
                row["rid"]: i for i, row in enumerate(representatives)
            },
            "deduplicated_avl_total": len(representatives),
            "document": document,
        }

    def _direction_case(self, vehicle, payload, state, case_id, context_s):
        summary = state["summaries"][case_id]
        focus = state["focus"][case_id]
        lower, upper = summary["start"], summary["end"]
        if lower is not None:
            lower -= context_s
            upper += context_s
        conflicts = [
            copy.deepcopy(state["conflicts"][key]) for key in summary["conflict_ids"]
        ]
        forced = set(focus)
        conflict_forced = set()
        boundary_forced = set()
        selected_segment = state["segments"].get(case_id, {})
        for field in ("boundary_evidence", "previous_direction_evidence"):
            boundary_forced.update(
                (r["source"], r["rid"]) for r in selected_segment.get(field, [])
            )
        forced.update(boundary_forced)
        bounds = [value for value in (lower, upper) if value is not None]
        for conflict in conflicts:
            forced.update(state["conflict_members"][conflict["id"]])
            conflict_forced.update(state["conflict_members"][conflict["id"]])
            bounds.extend([conflict["start"], conflict["end"]])
        lower, upper, clipped_start, clipped_end = self._clip_window(
            min(bounds) if bounds else None, max(bounds) if bounds else None
        )
        observations = []
        for source in state["observations"]:
            identity = source["source"], source["rid"]
            in_window = self._in_window(source.get("event_time"), lower, upper)
            if identity not in forced and not in_window:
                continue
            row = copy.deepcopy(source)
            member = identity in focus
            candidate_member = member and case_id in state["segments"]
            row.update(
                in_focus=member,
                is_candidate_member=candidate_member,
                is_segment_member=candidate_member,
                in_window=in_window,
                membership_relation="member"
                if candidate_member
                else "unresolved"
                if member
                else "context",
                relation="candidate_member"
                if candidate_member
                else "unassigned"
                if member
                else "context",
                is_conflict_context=identity in conflict_forced and not member,
                is_direction_boundary_context=identity in boundary_forced
                and not member,
            )
            observations.append(row)
        by_id = {(row["source"], row["rid"]): row for row in observations}
        avl = []
        for original in state["display_avl"]:
            obs = by_id.get(("avl", original["rid"]))
            if obs is None:
                continue
            row = copy.deepcopy(original)
            row.update(
                {
                    key: copy.deepcopy(value)
                    for key, value in obs.items()
                    if key not in original
                }
            )
            row.update(
                segment_id=case_id if obs["is_candidate_member"] else None,
                is_witness=False,
                in_core_window=self._in_window(
                    row["t"], summary["start"], summary["end"]
                ),
            )
            avl.append(row)
        _decorate_selected_progress(avl, state["representative_positions"])
        apc = []
        for obs in observations:
            if obs["source"] != "apc":
                continue
            row = copy.deepcopy(obs)
            row.update(
                status="not_evaluated",
                support_level=None,
                reason="DIRECTION_CANDIDATE_NO_OBSERVATION_MATCHING",
                progress_m=None,
                decision={
                    "mode": "direction_candidate_only",
                    "status": "not_evaluated",
                    "reason": "DIRECTION_CANDIDATE_NO_OBSERVATION_MATCHING",
                    "observation_matching_used": False,
                    "trip_segmentation_used": True,
                    "trip_identity_verified": False,
                },
                warning_flags=[
                    field
                    for field in (
                        "invalid",
                        "grain_uncertain",
                        "time_ambiguous",
                        "time_nonexistent",
                    )
                    if row.get(field)
                ],
            )
            apc.append(row)
        included_segments = {
            sid for row in observations for sid in row["candidate_segment_ids"]
        }
        segments = [
            {
                **copy.deepcopy(segment),
                "in_focus": key == case_id,
                "routes": state["summaries"][key]["routes"],
                "event_source_start": segment.get("source_start"),
                "event_source_end": segment.get("source_end"),
                "source_start": state["summaries"][key]["source_start"],
                "source_end": state["summaries"][key]["source_end"],
            }
            for key, segment in state["segments"].items()
            if key == case_id or key in included_segments
        ]
        original_count = len(payload["avl"])
        deduplicated_count = state["deduplicated_avl_total"]
        retained_count = len(state["display_avl"])
        counts = {
            "observations": len(observations),
            "observation_members": len(focus),
            "observation_context": len(observations) - len(focus),
            "vehicle_observations": len(state["observations"]),
            "avl_returned": len(avl),
            "apc_returned": len(apc),
            "avl_focus": sum(r["in_focus"] for r in avl),
            "apc_focus": sum(r["in_focus"] for r in apc),
            "avl_member_count": sum(r["is_candidate_member"] for r in avl),
            "apc_member_count": sum(r["is_candidate_member"] for r in apc),
            "context_avl_count": sum(not r["in_focus"] for r in avl),
            "apc_context": sum(not r["in_focus"] for r in apc),
            "window_avl_count": summary["window_avl_count"],
            "source_avl_total": original_count,
            "source_apc_total": len(payload["apc"]),
            "deduplicated_avl_total": deduplicated_count,
            "retained_avl_total": retained_count,
            "retained_apc_total": len(payload["apc"]),
            "removed_avl_duplicates": original_count - deduplicated_count,
            "filtered_avl_pi_po": len(state["filtered_avl"]),
            "source_rows_preserved": False,
            "source_lineage_preserved": True,
            "all_original_rows_displayed": False,
            "duplicate_source_identities": 0,
            "avl_sampled": 0,
            "missing_witness_ping_ids": 0,
            "avl_sampled_or_omitted": original_count - len(avl),
            "direction_conflicts": len(conflicts),
            "avl_unplottable": sum(
                r["t"] is None or not _coordinates_valid(r) for r in avl
            ),
            "apc_unplottable": sum(
                _bounds(r)[0] is None or not _coordinates_valid(r) for r in apc
            ),
        }
        return {
            "case": copy.deepcopy(summary),
            "window": {"start": lower, "end": upper},
            "observations": observations,
            "avl": avl,
            "apc": apc,
            "segments": segments,
            "direction_conflicts": conflicts,
            "conflicts": copy.deepcopy(conflicts),
            "filtered_avl": copy.deepcopy(state["filtered_avl"]),
            "deduplication": copy.deepcopy(state["deduplication"]),
            "avl_deduplication": True,
            "counts": counts,
            "date_clipping": {"start": clipped_start, "end": clipped_end},
            "representation": "direction_candidate_segments",
            "trip_segmentation_used": True,
            "observation_matching_used": False,
            "warnings": [DIRECTION_WARNING, DAY_BOUNDARY_WARNING, PROGRESS_DEFINITION]
            + (
                [
                    "Candidate bounds include unknown direction time or a day-scope "
                    "edge; use source_start/source_end for actual member observation "
                    "coverage, not a claimed complete trip."
                ]
                if summary.get("unknown_duration_s")
                or summary["scope_boundary_start"]
                or summary["scope_boundary_end"]
                else []
            ),
            "provenance": {
                "run_id": self.run.name,
                "run_path": str(self.run),
                "method": DIRECTION_METHOD,
                "vehicle_case": vehicle,
                "raw_vehicle": self.vehicles[vehicle],
                "time_basis": TIME_BASIS,
                "json_integer_encoding": JSON_INTEGER_ENCODING,
                "manifest": copy.deepcopy(self.manifest),
                "config": copy.deepcopy(self._config),
                "debug_date": self.debug_date,
                "scope": copy.deepcopy(self.scope),
                "representation": "direction_candidate_segments",
                "trip_segmentation_used": True,
                "observation_matching_used": False,
                "segmentation_recomputed": False,
                "matcher_recomputed": False,
                "trip_identity_verified": False,
                "gtfs_used": False,
                "counts_aggregated": False,
                "avl_deduplication": True,
                "deduplication_performed": True,
                "deduplication": copy.deepcopy(self.manifest.get("deduplication")),
                "filtering": copy.deepcopy(self.manifest.get("filtering")),
                "direction_policy": copy.deepcopy(state["document"].get("policy")),
                "progress_definition": PROGRESS_DEFINITION,
                "apc_progress_projection": "disabled; direction membership "
                "is not a GPS association",
                "sampling": "No sampling. Only this candidate and explicit context "
                "are returned; exact duplicate AVL copies and PI/PO representatives "
                "are audited exclusions.",
                "source_identity_definition": "Every original AVL is conserved "
                "through deduplication mapping and PI/PO filter audit; every APC "
                "remains in the vehicle observations and at least one review case. "
                "Candidate memberships may overlap and must not be summed.",
                "source_validation": "Raw13 exact groups, minimum source-row "
                "representatives, PI/PO-only original-field exclusions, retained "
                "payload/order, all APC identities and candidate/conflict "
                "references independently checked.",
                "source_coverage_definition": "source_start/source_end span actual "
                "member AVL times and both original APC endpoints, including "
                "cross-boundary intervals. They are not exclusive trip bounds; "
                "event_source_start/end retain the persisted event-time-only extent.",
            },
        }

    def _stream_case(
        self, vehicle: str, payload: dict, state: dict, case_id: str
    ) -> dict:
        summary = state["summaries"][case_id]
        start, end = summary["start"], summary["end"]
        avl, apc = [], []
        for row in state["display_avl"]:
            item = copy.deepcopy(row)
            item.update(
                segment_id=None,
                in_focus=True,
                is_segment_member=False,
                relation="context",
                membership_relation="context",
                in_core_window=self._in_window(row["t"], start, end),
                in_window=self._in_window(row["t"], start, end),
                is_witness=False,
            )
            item["source_fields"] = copy.deepcopy(
                state["observation_by_id"]["avl", row["rid"]]["source_fields"]
            )
            if self.avl_deduplication:
                item["deduplication"] = copy.deepcopy(state["lineage"][row["rid"]])
            avl.append(item)
        for row in payload["apc"]:
            item = copy.deepcopy(row)
            item.update(
                route_name=row.get("route_name", row.get("RouteName")),
                branch=row.get("branch", row.get("Branch")),
                status="not_evaluated",
                support_level=None,
                reason="VEHICLE_ID_ONLY_NO_OBSERVATION_MATCHING",
                in_focus=True,
                is_segment_member=False,
                relation="context",
                membership_relation="context",
                progress_m=None,
                source_fields=copy.deepcopy(row),
                decision={
                    "mode": "vehicle_group_only",
                    "status": "not_evaluated",
                    "reason": "VEHICLE_ID_ONLY_NO_OBSERVATION_MATCHING",
                    "observation_matching_used": False,
                    "trip_segmentation_used": False,
                    "trip_identity_verified": False,
                },
                warning_flags=[
                    field
                    for field in (
                        "invalid",
                        "grain_uncertain",
                        "time_ambiguous",
                        "time_nonexistent",
                    )
                    if row.get(field)
                ],
            )
            apc.append(item)
        return {
            "case": copy.deepcopy(summary),
            "window": {"start": start, "end": end},
            "observations": copy.deepcopy(state["observations"]),
            "avl": avl,
            "apc": apc,
            "segments": [],
            "deduplication": copy.deepcopy(state["deduplication"]),
            "avl_deduplication": self.avl_deduplication,
            "warnings": [
                DEDUP_STREAM_WARNING if self.avl_deduplication else STREAM_WARNING,
                DAY_BOUNDARY_WARNING,
                PROGRESS_DEFINITION,
            ],
            "counts": {
                "avl_returned": len(avl),
                "apc_returned": len(apc),
                "avl_focus": len(avl),
                "apc_focus": len(apc),
                "apc_context": 0,
                "avl_member_count": 0,
                "window_avl_count": summary["window_avl_count"],
                "context_avl_count": len(avl),
                "observations": len(state["observations"]),
                "source_avl_total": len(payload["avl"]),
                "source_apc_total": len(apc),
                "retained_avl_total": len(avl),
                "retained_apc_total": len(apc),
                "removed_avl_duplicates": len(payload["avl"]) - len(avl),
                "source_rows_preserved": not self.avl_deduplication,
                "source_lineage_preserved": True,
                "all_original_rows_displayed": len(payload["avl"]) == len(avl),
                "duplicate_source_identities": 0,
                "avl_unplottable": sum(
                    r["t"] is None or not _coordinates_valid(r) for r in avl
                ),
                "apc_unplottable": sum(
                    _bounds(r)[0] is None or not _coordinates_valid(r) for r in apc
                ),
                "avl_sampled_or_omitted": len(payload["avl"]) - len(avl),
                "avl_sampled": 0,
                "missing_witness_ping_ids": 0,
            },
            "date_clipping": {"start": False, "end": False},
            "representation": "vehicle_observation_stream",
            "trip_segmentation_used": False,
            "observation_matching_used": False,
            "provenance": {
                "run_id": self.run.name,
                "run_path": str(self.run),
                "method": self.methods[0],
                "vehicle_case": vehicle,
                "raw_vehicle": self.vehicles[vehicle],
                "time_basis": TIME_BASIS,
                "progress_definition": PROGRESS_DEFINITION,
                "manifest": copy.deepcopy(self.manifest),
                "config": copy.deepcopy(self._config),
                "debug_date": self.debug_date,
                "scope": copy.deepcopy(self.scope),
                "representation": "vehicle_observation_stream",
                "trip_segmentation_used": False,
                "observation_matching_used": False,
                "segmentation_recomputed": False,
                "matcher_recomputed": False,
                "gtfs_used": False,
                "counts_aggregated": False,
                "trip_identity_verified": False,
                "sampling": "no sampling; exact duplicate AVL copies omitted "
                "from display with complete source identity mapping"
                if self.avl_deduplication
                else "none; all vehicle-day source rows retained",
                "apc_progress_projection": "disabled; no nearest-GPS association",
                "json_integer_encoding": JSON_INTEGER_ENCODING,
                "deduplication_performed": self.avl_deduplication,
                "avl_deduplication": self.avl_deduplication,
                "deduplication": copy.deepcopy(self.manifest.get("deduplication")),
                "deduplication_validation": (
                    "Raw-field sidecar checksum and original audit CSV identity "
                    "verified; "
                    "13-field tuples regrouped independently; minimum source_row, "
                    "complete mapping, representative observations and every "
                    "APC row checked."
                    if self.avl_deduplication
                    else None
                ),
                "source_identity_definition": (
                    "Original AVL source plus rid is preserved in the deduplication "
                    "mapping; only representatives are plotted. APC identities and "
                    "rows are all displayed without removal."
                )
                if self.avl_deduplication
                else (
                    "Source plus original rid identifies each raw row. Identical "
                    "payloads with distinct source row IDs are retained unchanged."
                ),
            },
        }

    def _summary_row(
        self, vehicle, key, kind, start, end, pings, focus, payload, decisions
    ):
        rows = [payload["apc_by_id"][rid] for rid in focus]
        statuses = collections.Counter(
            decisions[rid].get("status", "missing") for rid in focus
        )
        accepted_count = (
            sum(
                decisions[rid].get("status") in {"direct", "partial"}
                and key in _owners(decisions[rid])
                for rid in focus
            )
            if kind == "segment"
            else 0
        )
        source_start, source_end = start, end
        start, end, clipped_start, clipped_end = self._clip_window(start, end)
        window_count = self._window_count(payload, start, end)
        members_in_window = sum(self._in_window(row["t"], start, end) for row in pings)
        return {
            "id": key,
            "kind": kind,
            "vehicle": vehicle,
            "vehicle_label": self.vehicles[vehicle],
            "date": (EPOCH + timedelta(seconds=start)).date().isoformat()
            if start is not None
            else None,
            "start": start,
            "end": end,
            "source_start": source_start,
            "source_end": source_end,
            "date_clipped_start": clipped_start,
            "date_clipped_end": clipped_end,
            "scope_limited": self.scope["start"] is not None,
            "outside_scope": source_start is not None and start is None,
            "routes": _routes(pings + rows),
            "avl_count": len(pings),
            "avl_member_count": len(pings),
            "window_avl_count": window_count,
            "context_avl_count": window_count - members_in_window,
            "apc_count": len(focus),
            "apc_rids": sorted(focus),
            "accepted_count": accepted_count,
            "candidate_count": len(focus) - accepted_count if kind == "segment" else 0,
            "status_counts": dict(statuses),
            "warning_count": sum(
                bool(_warning_flags(row, decisions[row["rid"]])) for row in rows
            ),
            "title": {
                "segment": "候选 trip · 冻结 AVL 片段",
                "apc_only": "未关联 APC · 审查窗口（不表示没有 GPS）",
                "unresolved": "待判定 · 非唯一片段归属 / 未接受关联",
            }[kind],
        }

    def _clip_window(self, start, end):
        if start is None or end is None or self.scope["start"] is None:
            return start, end, False, False
        lower, upper = self.scope["start"], self.scope["end"]
        clipped_start, clipped_end = start < lower, end > upper
        start, end = max(lower, start), min(upper, end)
        if start > end or start >= upper or end < lower:
            return None, None, clipped_start, clipped_end
        return start, end, clipped_start, clipped_end

    def _in_window(self, instant, start, end):
        return bool(
            _overlaps(instant, instant, start, end)
            and (self.scope["end"] is None or instant < self.scope["end"])
        )

    def _window_count(self, payload, start, end):
        if start is None or end is None:
            return 0
        times = payload["avl_times"]
        upper = (
            bisect_left(times, end)
            if end == self.scope["end"]
            else bisect_right(times, end)
        )
        return max(0, upper - bisect_left(times, start))

    def catalog(self, method: str, vehicle: str) -> list[dict]:
        with self._lock:
            _, state = self._method(method, vehicle)
            return copy.deepcopy(state["catalog"])

    def has_case(self, method: str, vehicle: str, case_id: str) -> bool:
        with self._lock:
            try:
                _, state = self._method(method, vehicle)
            except ValueError:
                return False
            return case_id in state["summaries"]

    def case(self, method: str, vehicle: str, case_id: str, context_s=180) -> dict:
        with self._lock:
            if (
                not isinstance(context_s, (int, float))
                or not math.isfinite(context_s)
                or not 0 <= context_s <= 3600
            ):
                raise ValueError("Context must be between 0 and 3600 seconds")
            payload, state = self._method(method, vehicle)
            if case_id not in state["summaries"]:
                raise ValueError("Unknown review case")
            if method in STREAM_METHODS:
                return self._stream_case(vehicle, payload, state, case_id)
            if method == DIRECTION_METHOD:
                return self._direction_case(vehicle, payload, state, case_id, context_s)
            summary = state["summaries"][case_id]
            focus = state["focus"][case_id]
            bounds = [summary["start"], summary["end"]]
            for rid in focus:
                bounds.extend(_bounds(payload["apc_by_id"][rid]))
            bounds = [value for value in bounds if value is not None]
            lower, upper = (
                (min(bounds) - context_s, max(bounds) + context_s)
                if bounds
                else (None, None)
            )
            lower, upper, clipped_start, clipped_end = self._clip_window(lower, upper)
            selected_apc = []
            witness_ids = set()
            for row in payload["apc"]:
                in_focus = row["rid"] in focus
                if not in_focus and not _overlaps(*_bounds(row), lower, upper):
                    continue
                decision = state["decisions"][row["rid"]]
                if summary["kind"] != "segment":
                    relation = "unassigned" if in_focus else "context"
                elif decision.get("status") in {
                    "direct",
                    "partial",
                } and case_id in _owners(decision):
                    relation = "accepted"
                elif case_id in _owners(decision, candidate=True):
                    relation = "candidate"
                else:
                    relation = "context"
                witness_ids.update(_all_ping_ids(decision))
                selected_apc.append(
                    {
                        **{
                            field: row.get(field)
                            for field in (
                                "rid",
                                "arrival",
                                "departure",
                                "lat",
                                "lon",
                                "route",
                                "route_name",
                                "RouteName",
                                "branch",
                                "Branch",
                                "direction",
                                "stop",
                                "boarding",
                                "alighting",
                                "arrival_load",
                                "departure_load",
                                "avg_sample",
                                "no_trips",
                                "invalid",
                                "grain_uncertain",
                                "time_ambiguous",
                                "time_nonexistent",
                            )
                        },
                        "status": decision.get("status"),
                        "route_name": row.get("route_name", row.get("RouteName")),
                        "branch": row.get("branch", row.get("Branch")),
                        "support_level": decision.get("support_level"),
                        "reason": decision.get("reason"),
                        "in_focus": in_focus,
                        "relation": relation,
                        "warning_flags": _warning_flags(row, decision),
                        "decision": copy.deepcopy(decision),
                        "source": copy.deepcopy(row),
                    }
                )
            selected_avl = []
            previous_index = None
            displayed_run = 0
            displayed_origin = 0.0
            for index, row in enumerate(payload["avl"]):
                owner = state["membership"].get(row["rid"])
                is_member = summary["kind"] == "segment" and owner == case_id
                in_core = self._in_window(row["t"], summary["start"], summary["end"])
                in_focus = is_member or (summary["kind"] != "segment" and in_core)
                in_window = self._in_window(row["t"], lower, upper)
                if not in_focus and not in_window and row["rid"] not in witness_ids:
                    continue
                item = {
                    field: row.get(field)
                    for field in (
                        "rid",
                        "t",
                        "lat",
                        "lon",
                        "route",
                        "valid",
                        "break_before",
                        "break_reason",
                        "progress_m",
                        "progress_run",
                        "speed_kph",
                        "duplicate_count",
                        "duplicate_conflict",
                        "invalid",
                        "time_ambiguous",
                        "time_nonexistent",
                        "source_trip",
                        "run",
                        "destination",
                        "speed",
                        "heading",
                    )
                }
                if previous_index is None or index != previous_index + 1:
                    item["break_before"] = True
                    if not item["break_reason"]:
                        item["break_reason"] = "display_window_boundary"
                if item["break_before"]:
                    displayed_run += 1
                    displayed_origin = row.get("progress_m") or 0.0
                item["source_progress_run"] = row.get("progress_run")
                item["progress_run"] = displayed_run if row["valid"] else None
                item["progress_m"] = (
                    max(0.0, row["progress_m"] - displayed_origin)
                    if row["progress_m"] is not None
                    else None
                )
                item.update(
                    segment_id=owner,
                    in_focus=in_focus,
                    is_segment_member=is_member,
                    relation="member" if is_member else "context",
                    membership_relation="member" if is_member else "context",
                    in_core_window=in_core,
                    in_window=in_window,
                    is_witness=row["rid"] in witness_ids,
                )
                selected_avl.append(item)
                previous_index = index
            included_owners = {row["segment_id"] for row in selected_avl}
            segments = []
            for key, chunk in state["chunks"].items():
                if (
                    key == case_id
                    or key in included_owners
                    or _overlaps(chunk.get("start"), chunk.get("end"), lower, upper)
                ):
                    segments.append(
                        {
                            "id": key,
                            "start": chunk.get("start"),
                            "end": chunk.get("end"),
                            "in_focus": key == case_id,
                            "routes": state["summaries"][key]["routes"],
                        }
                    )
            counts = {
                "avl_returned": len(selected_avl),
                "avl_focus": sum(row["in_focus"] for row in selected_avl),
                "avl_member_count": sum(
                    row["is_segment_member"] for row in selected_avl
                ),
                "window_avl_count": summary["window_avl_count"],
                "context_avl_count": sum(
                    not row["is_segment_member"] for row in selected_avl
                ),
                "avl_padding_context": sum(not row["in_focus"] for row in selected_avl),
                "apc_returned": len(selected_apc),
                "apc_focus": sum(row["in_focus"] for row in selected_apc),
                "apc_context": sum(not row["in_focus"] for row in selected_apc),
                "avl_witnesses_outside_window": sum(
                    row["is_witness"] and not row["in_window"] for row in selected_avl
                ),
                "missing_witness_ping_ids": len(
                    witness_ids - set(payload["avl_by_id"])
                ),
                "avl_unplottable": sum(
                    row["t"] is None or not _coordinates_valid(row)
                    for row in selected_avl
                ),
                "apc_unplottable": sum(
                    _bounds(row)[0] is None or not _coordinates_valid(row)
                    for row in selected_apc
                ),
                "avl_sampled_or_omitted": 0,
            }
            warnings = [WARNING, PROGRESS_DEFINITION]
            if self.scope["start"] is not None:
                warnings.append(DAY_BOUNDARY_WARNING)
            if "long_chunk_over_2h_not_verified_trip" in summary.get(
                "segment_flags", []
            ):
                warnings.append(
                    "This frozen chunk exceeds two hours and may contain multiple "
                    "performed trips. Its bounds are not verified trip boundaries; "
                    "the two-hour display warning is not a segmentation rule."
                )
            if summary["kind"] != "segment":
                warnings.append(
                    "This case is an APC review bucket, not an inferred trip "
                    "or accepted segment assignment. GPS in its core time window "
                    "is highlighted for inspection but remains unassociated context; "
                    "in_focus does not mean accepted membership."
                )
            if counts["avl_witnesses_outside_window"]:
                warnings.append(
                    "Actual witness pings outside the timeline window are included "
                    "and marked in_window=false."
                )
            if counts["missing_witness_ping_ids"]:
                warnings.append(
                    "Some persisted witness IDs are absent from this vehicle's "
                    "canonical AVL; evidence is incomplete."
                )
            if counts["avl_unplottable"] or counts["apc_unplottable"]:
                warnings.append(
                    "Rows with missing time or coordinates are retained in the "
                    "payload but cannot all be plotted."
                )
            return {
                "case": copy.deepcopy(summary),
                "window": {"start": lower, "end": upper},
                "date_clipping": {"start": clipped_start, "end": clipped_end},
                "avl": selected_avl,
                "apc": selected_apc,
                "segments": segments,
                "warnings": warnings,
                "counts": counts,
                "provenance": {
                    "run_id": self.run.name,
                    "run_path": str(self.run),
                    "method": method,
                    "vehicle_case": vehicle,
                    "raw_vehicle": self.vehicles[vehicle],
                    "time_basis": TIME_BASIS,
                    "progress_definition": PROGRESS_DEFINITION,
                    "manifest": copy.deepcopy(self.manifest),
                    "config": copy.deepcopy(self._config),
                    "debug_date": self.debug_date,
                    "scope": copy.deepcopy(self.scope),
                    "segmentation_recomputed": False,
                    "matcher_recomputed": False,
                    "gtfs_used": False,
                    "counts_aggregated": False,
                    "trip_identity_verified": False,
                    "sampling": "none; all selected raw rows retained",
                },
            }
