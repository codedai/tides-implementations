"""Isolated, immutable single-calendar-day APC–AVL debugging experiments.

This runner adds isolated experiments, never changing frozen results or source data. Its
checks establish source conservation and elementary witness consistency, never
accuracy, performed-trip identity, service-date truth, or publication readiness.
"""

from __future__ import annotations

import collections
import copy
import hashlib
import json
import math
import re
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from experiments.apc_avl_coverage_study import segmentation
from experiments.apc_avl_direct_evidence import evaluate, matcher
from tools.merge_review import (
    avl_dedup,
    direction_runner,
    direction_trips,
    vehicle_stream,
)
from tools.merge_review.gtfs_matching import METHOD as GTFS_METHOD
from tools.merge_review.gtfs_matching import REPRESENTATION as GTFS_REPRESENTATION
from tools.merge_review.gtfs_matching import config as gtfs_config
from tools.merge_review.paths import project_root

DATE = "2023-11-06"
VERSION = "merge-review-single-day-v4"
STREAM_METHODS = ("vehicle_only", "vehicle_only_dedup")
METHODS = (*STREAM_METHODS, direction_trips.METHOD, GTFS_METHOD, *segmentation.METHODS)
EPOCH = datetime(1970, 1, 1)
DEFAULTS = {
    "method": "avl_context",
    "segmentation": dict(segmentation.DEFAULT_CONFIG),
    "merge": dict(matcher.DEFAULT_CONFIG),
}
_SEGMENTATION_LIMITS = {
    "gap_seconds": (10, 7200, 10),
    "context_min_points": (2, 50, 1),
    "context_min_seconds": (0, 600, 5),
    "boundary_cooldown_seconds": (0, 3600, 30),
    "reversal_window_points": (2, 50, 1),
    "reversal_min_displacement_m": (0, 2000, 10),
    "reversal_angle_degrees": (0, 180, 5),
    "reversal_min_straightness": (0, 1, 0.05),
    "apc_min_direction_points": (2, 50, 1),
    "apc_anchor_max_gap_seconds": (0, 1800, 10),
    "same_time_conflict_distance_m": (0, 1000, 10),
}
_MERGE_LIMITS = {
    "time_tolerance_s": (1, 300, 5),
    "radius_m": (1, 500, 10),
    "interpolation_gap_s": (1, 300, 5),
    "episode_gap_s": (1, 600, 5),
    "contradiction_radius_m": (1, 2000, 10),
    "contemporaneous_s": (0, 60, 1),
    "max_speed_kph": (1, 150, 5),
    "max_apc_dwell_s": (1, 1800, 30),
    "accept_score": (0, 1000, 10),
    "null_score": (0, 1000, 10),
    "margin_score": (0, 1000, 10),
    "max_episodes": (1, 64, 1),
    "max_measurements_per_record": (2, 4096, 1),
}
_INTEGER_KEYS = {
    "context_min_points",
    "reversal_window_points",
    "apc_min_direction_points",
    "max_episodes",
    "max_measurements_per_record",
}


def _parameters():
    result = {}
    for section, limits in (
        ("segmentation", _SEGMENTATION_LIMITS),
        ("merge", _MERGE_LIMITS),
    ):
        result[section] = [
            {
                "key": key,
                "label": key.replace("_", " "),
                "type": "integer" if key in _INTEGER_KEYS else "number",
                "min": low,
                "max": high,
                "step": step,
                "default": DEFAULTS[section][key],
            }
            for key, (low, high, step) in limits.items()
        ]
    result["merge"].append(
        {
            "key": "route_policy",
            "label": "Route conflict policy",
            "type": "enum",
            "choices": ["flag", "abstain_conflict"],
            "default": "flag",
        }
    )
    return result


PARAMETERS = _parameters()
_CODE_FILES = {
    "segmentation": Path(segmentation.__file__),
    "matcher": Path(matcher.__file__),
    "validator": Path(evaluate.__file__),
    "debug_runner": Path(__file__),
    "vehicle_stream": Path(vehicle_stream.__file__),
    "avl_dedup": Path(avl_dedup.__file__),
    "direction_trips": Path(direction_trips.__file__),
    "direction_runner": Path(direction_runner.__file__),
    "project_paths": Path(__file__).with_name("paths.py"),
}
_CODE_FILES.update(
    {
        "gtfs_" + name: Path(__file__).parent / "gtfs_matching" / f"{name}.py"
        for name in (
            *gtfs_config.MODULES,
            "__init__",
            "config",
            "runner",
            "adapter",
            "decision",
            "partial",
            "multimode",
            "overview",
        )
    }
)
_CODE_FILES.update(
    {
        "gtfs_continuous_" + path.stem: path
        for path in (Path(__file__).parent / "gtfs_matching" / "continuous").glob(
            "*.py"
        )
    }
)
_LOADED_CODE_SHA256 = {
    name: evaluate.file_hash(path) for name, path in _CODE_FILES.items()
}


def validate_config(config: dict) -> dict:
    """Normalize a bounded whitelist; booleans are not numeric parameters."""
    if isinstance(config, dict) and config.get("method") == GTFS_METHOD:
        if set(config) - {"method", "segmentation", "merge", "gtfs"}:
            raise ValueError("Unknown GTFS experiment configuration section")
        if any(
            not isinstance(config.get(s, {}), dict) or config.get(s, {})
            for s in ("segmentation", "merge")
        ):
            raise ValueError(
                "GTFS matching refines experiment 02 via its GTFS parameters; "
                "legacy segmentation/merge parameters must be empty"
            )
        return {
            "method": GTFS_METHOD,
            "segmentation": {},
            "merge": {},
            "gtfs": gtfs_config.validate(config.get("gtfs", {})),
        }
    if not isinstance(config, dict) or set(config) - set(DEFAULTS):
        raise ValueError("Expected method, segmentation and merge configuration only")
    result = copy.deepcopy(DEFAULTS)
    method = config.get("method", result["method"])
    if not isinstance(method, str) or method not in METHODS:
        raise ValueError("Unknown segmentation method")
    if method in (*STREAM_METHODS, direction_trips.METHOD):
        for section in ("segmentation", "merge"):
            supplied = config.get(section, {})
            if not isinstance(supplied, dict) or supplied:
                raise ValueError(
                    f"{method} requires empty segmentation and merge parameters"
                )
        return {"method": method, "segmentation": {}, "merge": {}}
    result["method"] = method
    for section in ("segmentation", "merge"):
        supplied = config.get(section, {})
        if not isinstance(supplied, dict) or set(supplied) - set(result[section]):
            raise ValueError(f"Unknown {section} parameter")
        for spec in PARAMETERS[section]:
            key = spec["key"]
            value = supplied.get(key, result[section][key])
            if spec["type"] == "enum":
                if not isinstance(value, str) or value not in spec["choices"]:
                    raise ValueError(f"Invalid {key}")
            else:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (float, int))
                    or not math.isfinite(value)
                    or not spec["min"] <= value <= spec["max"]
                ):
                    raise ValueError(
                        f"{key} must be finite in [{spec['min']}, {spec['max']}]"
                    )
                if spec["type"] == "integer":
                    if int(value) != value:
                        raise ValueError(f"{key} must be an integer")
                    value = int(value)
            result[section][key] = value
    if result["merge"]["contradiction_radius_m"] < result["merge"]["radius_m"]:
        raise ValueError("contradiction_radius_m must be >= radius_m")
    return result


def _json(path: Path):
    return json.loads(path.read_text())


def _directory(path: Path):
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for folder in reversed(missing):
        folder.mkdir(mode=0o700, exist_ok=True)


def _write(path: Path, value):
    _directory(path.parent)
    with path.open("x") as stream:
        path.chmod(0o600)
        json.dump(value, stream, indent=2, default=str, allow_nan=False)
        stream.write("\n")


def _rows(path: Path):
    return pl.read_parquet(path).to_dicts() if path.is_file() else []


def _save(path: Path, rows: list[dict], schema_overrides=None):
    if rows:
        _parquet(
            path,
            pl.from_dicts(
                rows, infer_schema_length=None, schema_overrides=schema_overrides
            ),
        )


def _parquet(path: Path, frame: pl.DataFrame):
    _directory(path.parent)
    with path.open("xb") as stream:
        path.chmod(0o600)
        frame.write_parquet(stream)


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def _case(vehicle: str) -> str:
    return "vehicle-" + hashlib.sha256(vehicle.encode()).hexdigest()[:16]


def _clock(value):
    return (value - EPOCH).total_seconds() if isinstance(value, datetime) else value


class DebugWorkspace:
    """A separate day cache and append-only run history; never a production run."""

    validate_config = staticmethod(validate_config)

    def __init__(self, root: Path, audit: Path, date: str = DATE):
        if date != DATE:
            raise ValueError(f"Only the fixed debug calendar date {DATE} is allowed")
        self.root, self.audit, self.date = (
            Path(root).resolve(),
            Path(audit).resolve(),
            date,
        )
        if self.root.is_relative_to(self.audit) or self.audit.is_relative_to(self.root):
            raise ValueError("Debug outputs must be separate from source audit inputs")
        self.start = datetime.fromisoformat(date)
        self.end = self.start + timedelta(days=1)
        self._fixture = None
        self._fixture_lock = threading.Lock()

    def _code_hashes(self):
        actual = {name: evaluate.file_hash(path) for name, path in _CODE_FILES.items()}
        if actual != _LOADED_CODE_SHA256:
            raise ValueError(
                "Algorithm or runner source changed after import; "
                "restart the server before running"
            )
        return actual

    def _inputs(self):
        hashes = {
            name: evaluate.file_hash(self.audit / name)
            for name in ("avl.parquet", "apc.parquet")
        }
        inventory = self.audit / "inventory.json"
        raw = None
        note = (
            "Raw APC export unavailable: RouteName is null, never inferred from Route."
        )
        if inventory.is_file():
            hashes["inventory.json"] = evaluate.file_hash(inventory)
            source = _json(inventory).get("sources", {}).get("apc", {})
            given = source.get("path")
            if given:
                candidate = Path(given)
                if not candidate.is_absolute():
                    candidate = project_root() / candidate
                if candidate.is_file():
                    observed = evaluate.file_hash(candidate)
                    if not source.get("sha256") or observed != source["sha256"]:
                        raise ValueError(
                            "Raw APC RouteName export hash is not verified"
                        )
                    raw = candidate
                    hashes["raw_apc.csv"] = observed
                    note = (
                        "RouteName read verbatim from hash-verified raw APC, "
                        "joined on source_row."
                    )
        return hashes, raw, note

    def _fixture_valid(self, folder: Path, manifest: dict):
        expected = manifest.get("fixture_sha256", {})
        if set(expected) != {"avl.parquet", "apc.parquet"} or any(
            evaluate.file_hash(folder / name) != digest
            for name, digest in expected.items()
        ):
            raise ValueError("Cached day fixture content changed")

    def ensure_fixture(self) -> Path:
        """Preserve source IDs/timestamps, with a fixed calendar-day inclusion rule."""
        with self._fixture_lock:
            inputs, raw, note = self._inputs()
            code = self._code_hashes()
            identity = _digest(
                {"version": VERSION, "date": self.date, "inputs": inputs, "code": code}
            )
            fixtures = self.root / "fixtures"
            if fixtures.is_dir():
                for candidate in sorted(fixtures.glob(f"fixture-{identity[:20]}-*")):
                    if (
                        candidate.is_symlink()
                        or not (candidate / "manifest.json").is_file()
                    ):
                        continue
                    manifest = _json(candidate / "manifest.json")
                    if manifest.get("identity") == identity:
                        self._fixture_valid(candidate, manifest)
                        self._fixture = candidate
                        return candidate
            folder = fixtures / f"fixture-{identity[:20]}-{uuid.uuid4().hex[:8]}"
            _directory(folder.parent)
            folder.mkdir(mode=0o700)
            selected = {}
            for source, field in (("avl", "time"), ("apc", "arrival")):
                query = pl.scan_parquet(self.audit / f"{source}.parquet")
                membership = pl.col(field)
                if source == "apc":
                    membership = pl.coalesce("arrival", "departure")
                frame = query.filter(
                    membership.is_between(self.start, self.end, closed="left")
                ).collect()
                if frame["rid"].n_unique() != frame.height:
                    raise ValueError("Source rid must be unique within the day fixture")
                if (
                    frame["vehicle"].null_count()
                    or frame.filter(pl.col("vehicle") == "").height
                ):
                    raise ValueError(
                        "Missing vehicle identity cannot be assigned to a vehicle case"
                    )
                if source == "apc":
                    if raw is not None:
                        names = (
                            pl.scan_csv(
                                raw,
                                infer_schema=False,
                                row_index_name="source_row",
                                row_index_offset=1,
                            )
                            .select(
                                "source_row", pl.col("RouteName").alias("route_name")
                            )
                            .collect()
                        )
                        if names["source_row"].n_unique() != names.height:
                            raise ValueError("Raw APC source_row must be unique")
                        frame = frame.join(
                            names, on="source_row", how="left", validate="1:1"
                        )
                    else:
                        frame = frame.with_columns(
                            pl.lit(None, dtype=pl.String).alias("route_name")
                        )
                    frame = frame.with_columns(
                        (
                            pl.col("arrival").is_null()
                            | (pl.col("arrival") < self.start)
                        ).alias("debug_day_censored_start"),
                        (
                            pl.col("departure").is_null()
                            | (pl.col("departure") >= self.end)
                        ).alias("debug_day_censored_end"),
                        pl.col("arrival")
                        .is_null()
                        .alias("debug_membership_uses_departure"),
                    )
                _parquet(folder / f"{source}.parquet", frame)
                selected[source] = frame
            if self._inputs()[0] != inputs or self._code_hashes() != code:
                raise ValueError(
                    "Inputs or algorithm source changed during day-fixture creation"
                )
            manifest = {
                "version": VERSION,
                "identity": identity,
                "debug_date": self.date,
                "scope": {"start": self.start.isoformat(), "end": self.end.isoformat()},
                "source_audit": str(self.audit),
                "input_sha256": inputs,
                "code_sha256": code,
                "fixture_sha256": {
                    f"{s}.parquet": evaluate.file_hash(folder / f"{s}.parquet")
                    for s in selected
                },
                "source_counts": {s: f.height for s, f in selected.items()},
                "route_name_note": note,
                "route_name_field": "route_name",
                "membership": (
                    "AVL time and APC arrival in [00:00, next 00:00); "
                    "only if arrival is null use departure."
                ),
                "boundary_censoring": (
                    "Calendar-day context is censored at both edges; departures "
                    "are never clipped. No neighbouring-day GPS loaded."
                ),
                "cross_midnight_apc": selected["apc"]
                .filter(pl.col("departure") >= self.end)
                .height,
                "departure_membership_apc": selected["apc"]
                .filter(pl.col("arrival").is_null())
                .height,
                "neighbouring_day_gps": 0,
                "service_date_inferred": False,
                "accuracy": None,
                "publishable": False,
            }
            _write(folder / "manifest.json", manifest)
            self._fixture = folder
            return folder

    def get_run(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not re.fullmatch(
            r"run-20231106-[A-Za-z0-9_-]{1,80}", run_id
        ):
            raise ValueError("Invalid debug run ID")
        candidate = self.root / "runs" / run_id
        if (
            candidate.is_symlink()
            or not candidate.resolve().is_relative_to(self.root)
            or candidate.resolve().parent != (self.root / "runs").resolve()
        ):
            raise ValueError("Debug run path must remain inside this workspace")
        if not (candidate / "completion.json").is_file():
            raise ValueError("Debug run is absent or incomplete")
        completion = _json(candidate / "completion.json")
        if not completion.get("validation_ok") or completion.get("debug_date") != DATE:
            raise ValueError("Debug run did not complete validation")
        return candidate

    def metadata(self) -> dict:
        fixture = self._fixture or self.ensure_fixture()
        runs = []
        for folder in sorted((self.root / "runs").glob("run-20231106-*"), reverse=True):
            try:
                checked = self.get_run(folder.name)
            except ValueError:
                continue
            runs.append(_json(checked / "run.json"))
        return {
            "date": self.date,
            "fixture": _json(fixture / "manifest.json") | {"path": str(fixture)},
            "defaults": copy.deepcopy(DEFAULTS),
            "parameters": copy.deepcopy(PARAMETERS),
            "methods": list(METHODS),
            "gtfs_defaults": gtfs_config.defaults(),
            "gtfs_parameters": gtfs_config.parameters(),
            "method_descriptions": {
                "vehicle_only": (
                    "VehicleID UNION ALL source observations, time sorted; "
                    "no trip segmentation or observation matching."
                ),
                "vehicle_only_dedup": (
                    "Vehicle-only source stream after exact original thirteen-field "
                    "AVL deduplication; APC unchanged, no trip or matching."
                ),
                direction_trips.METHOD: (
                    "Experiment 02: exact AVL deduplication, PI/PO route filtering, "
                    "and direction/Destination temporal candidate segmentation; "
                    "overlapping conflicts retained, no observation matching."
                ),
                GTFS_METHOD: (
                    "Experiment 03: route 29 only, refine experiment 02 at joint "
                    "APC/AVL gaps over 30 minutes (configurable); sequence-constrained "
                    "canonical projection and hierarchical GTFS "
                    "matching. Draft feed pin and uncalibrated probabilities "
                    "are explicit."
                ),
            },
            "parameter_methods": list(segmentation.METHODS),
            "runs": runs,
        }

    def create_run(
        self, config: dict, label: str = "", parent_run_id=None, progress=None
    ) -> dict:
        config = validate_config(config)
        if not isinstance(label, str) or len(label) > 200:
            raise ValueError("Run label must be text of at most 200 characters")
        if parent_run_id is not None:
            self.get_run(parent_run_id)
        if config["method"] == GTFS_METHOD:
            from tools.merge_review.gtfs_matching.runner import create_run

            return create_run(self, config, label, parent_run_id, progress)
        if config["method"] == direction_trips.METHOD:
            return direction_runner.create_run(
                self, config, label, parent_run_id, progress
            )
        started = time.perf_counter()

        def emit(stage, completed=0, total=0, **extra):
            if progress:
                progress(
                    {
                        "stage": stage,
                        "message": stage,
                        "completed": completed,
                        "total": total,
                        "elapsed_s": round(time.perf_counter() - started, 3),
                        **extra,
                    }
                )

        emit("preparing_day_fixture")
        fixture = self.ensure_fixture()
        fixture_manifest = _json(fixture / "manifest.json")
        code = self._code_hashes()
        method = config["method"]
        stream_mode = method in STREAM_METHODS
        dedup_mode = method == "vehicle_only_dedup"
        frames = {s: pl.read_parquet(fixture / f"{s}.parquet") for s in ("avl", "apc")}
        raw_fields, dedup_definition = None, None
        retained_counts = dict(fixture_manifest["source_counts"])
        removed_avl = duplicate_groups = duplicate_rows = 0
        if dedup_mode:
            emit("loading_exact_original_avl_fields")
            raw_fields, dedup_definition = avl_dedup.load_raw_avl_fields(
                self.audit, frames["avl"]
            )
            sizes = raw_fields.group_by(list(avl_dedup.RAW_FIELDS)).len()
            retained_counts["avl"] = sizes.height
            removed_avl = frames["avl"].height - sizes.height
            repeated = sizes.filter(pl.col("len") > 1)
            duplicate_groups = repeated.height
            duplicate_rows = int(repeated["len"].sum() or 0)
        run_id = (
            "run-20231106-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid.uuid4().hex[:12]
        )
        folder = self.root / "runs" / run_id
        _directory(folder.parent)
        folder.mkdir(mode=0o700)
        created = datetime.now(UTC).isoformat()
        vehicles = sorted(set(frames["avl"]["vehicle"]) | set(frames["apc"]["vehicle"]))
        selection = [{"vehicle_case": _case(v), "raw_vehicle": v} for v in vehicles]
        _write(folder / "selection.json", selection)
        _write(folder / "config.json", config)
        if dedup_mode:
            raw_fields_path = folder / "deduplication" / "raw_avl_fields.parquet"
            _parquet(raw_fields_path, raw_fields)
            dedup_definition.update(
                raw_fields_path="deduplication/raw_avl_fields.parquet",
                raw_fields_sha256=evaluate.file_hash(raw_fields_path),
            )
        representation = (
            vehicle_stream.REPRESENTATION if stream_mode else "apc_avl_direct_evidence"
        )
        manifest = {
            "version": VERSION,
            "run_id": run_id,
            "debug_date": self.date,
            "scope": fixture_manifest["scope"],
            "created_at": created,
            "audit": str(fixture),
            "baseline": str(folder),
            "methods": [method],
            "segmentation_methods": [] if stream_mode else [method],
            "representation": representation,
            "trip_segmentation_used": not stream_mode,
            "observation_matching_used": not stream_mode,
            "observation_ordering": vehicle_stream.ORDERING if stream_mode else None,
            "code_sha256": code,
            "canonical_input_sha256": fixture_manifest["fixture_sha256"],
            "fixture_identity": fixture_manifest["identity"],
            "parent_run_id": parent_run_id,
            "source_counts": fixture_manifest["source_counts"],
            "retained_counts": retained_counts,
            "removed_avl": removed_avl,
            "avl_deduplication": dedup_mode,
            "deduplication": dedup_definition,
            "full_fixture": False,
            "full_debug_day": True,
            "gtfs_used": False,
            "clock_correction": False,
            "service_date_inferred": False,
            "accuracy": None,
            "publishable": False,
            "boundary_censoring": fixture_manifest["boundary_censoring"],
        }
        _write(folder / "manifest.json", manifest)
        statuses, violations, examined = (
            collections.Counter(),
            collections.Counter(),
            collections.Counter(),
        )
        segments = 0
        observations = 0
        try:
            for ordinal, item in enumerate(selection, 1):
                vehicle, case = item["raw_vehicle"], item["vehicle_case"]
                avl, apc = (
                    frames[s].filter(pl.col("vehicle") == vehicle).to_dicts()
                    for s in ("avl", "apc")
                )
                source_before = _digest({"avl": avl, "apc": apc})
                if stream_mode:
                    retained, mapping, raw_rows = avl, [], []
                    if dedup_mode:
                        raw_rows = raw_fields.filter(
                            pl.col("vehicle") == vehicle
                        ).to_dicts()
                        retained, mapping = avl_dedup.deduplicate_avl(avl, raw_rows)
                        dedup_check = avl_dedup.validate_avl_deduplication(
                            avl, raw_rows, retained, mapping
                        )
                        violations.update(
                            {
                                "dedup_" + key: value
                                for key, value in dedup_check["violations"].items()
                            }
                        )
                        examined.update(
                            {
                                "dedup_" + key: value
                                for key, value in dedup_check["examined"].items()
                            }
                        )
                    stream = vehicle_stream.build_vehicle_stream(retained, apc)
                    check = vehicle_stream.validate_vehicle_stream(
                        retained, apc, stream, self.start, self.end
                    )
                    violations.update(check["violations"])
                    examined.update(check["examined"])
                    violations["source_mutation"] += source_before != _digest(
                        {"avl": avl, "apc": apc}
                    )
                    if any(violations.values()):
                        raise ValueError(
                            "Independent vehicle-stream conservation validation failed"
                        )
                    output = folder / "observations" / f"{case}.parquet"
                    _save(
                        output,
                        stream,
                        dict(frames["avl"].schema) | dict(frames["apc"].schema),
                    )
                    persisted = _rows(output)
                    persisted_check = vehicle_stream.validate_vehicle_stream(
                        retained, apc, persisted, self.start, self.end
                    )
                    violations.update(
                        {
                            "persisted_" + key: value
                            for key, value in persisted_check["violations"].items()
                        }
                    )
                    if dedup_mode:
                        mapping_path = folder / "deduplication" / f"{case}.parquet"
                        if mapping:
                            _save(mapping_path, mapping, avl_dedup.MAPPING_SCHEMA)
                        else:
                            _parquet(
                                mapping_path,
                                pl.DataFrame(schema=avl_dedup.MAPPING_SCHEMA),
                            )
                        saved_dedup = avl_dedup.validate_avl_deduplication(
                            avl,
                            raw_rows,
                            [r for r in persisted if r["source"] == "avl"],
                            _rows(mapping_path),
                        )
                        violations.update(
                            {
                                "persisted_dedup_" + key: value
                                for key, value in saved_dedup["violations"].items()
                            }
                        )
                    if any(violations.values()):
                        raise ValueError(
                            "Persisted vehicle-stream conservation validation failed"
                        )
                    observations += len(stream)
                    emit(
                        "ordering_vehicle_observations",
                        ordinal,
                        len(selection),
                        vehicle=vehicle,
                    )
                    continue
                seg = segmentation.segment_vehicle(
                    avl, apc, method, config["segmentation"]
                )
                index = matcher.build_index(avl, config["merge"])
                assigned = matcher.match_direct(apc, index, seg, config["merge"])
                check = evaluate.validate_assignments(
                    avl, apc, seg, assigned, config["merge"]
                )
                violations.update(check["violations"])
                examined.update(check["examined"])
                members = [
                    rid for chunk in seg["segments"] for rid in chunk["ping_ids"]
                ]
                excluded = seg["excluded_ping_ids"]
                all_ids = {r["rid"] for r in avl}
                violations["duplicate_chunk_membership"] += len(members) - len(
                    set(members)
                )
                violations["avl_unaccounted"] += len(
                    all_ids - set(members) - set(excluded)
                )
                violations["avl_extra_membership"] += len(
                    (set(members) | set(excluded)) - all_ids
                )
                violations["avl_excluded_and_assigned"] += len(
                    set(members) & set(excluded)
                )
                violations["source_mutation"] += source_before != _digest(
                    {"avl": avl, "apc": apc}
                )
                if any(violations.values()):
                    raise ValueError(
                        "Independent source-conservation or witness validation failed"
                    )
                for chunk in seg["segments"]:
                    chunk["debug_day_context_censored"] = True
                _save(folder / "chunks" / method / f"{case}.parquet", seg["segments"])
                _save(
                    folder / "boundaries" / method / f"{case}.parquet",
                    seg["boundaries"],
                )
                _save(folder / "assignments" / method / f"{case}.parquet", assigned)
                _write(
                    folder / "segmentation" / method / f"{case}.json",
                    {
                        "excluded_ping_ids": excluded,
                        "unlocated_invalid_ping_ids": seg["unlocated_invalid_ping_ids"],
                        "chronology_unresolved": seg["chronology_unresolved"],
                    },
                )
                statuses.update(r["status"] for r in assigned)
                segments += len(seg["segments"])
                emit("processing_vehicles", ordinal, len(selection), vehicle=vehicle)
            self._fixture_valid(fixture, fixture_manifest)
            if dedup_mode:
                if (
                    avl_dedup.file_sha256(dedup_definition["raw_csv_path"])
                    != dedup_definition["raw_csv_sha256"]
                    or evaluate.file_hash(folder / dedup_definition["raw_fields_path"])
                    != dedup_definition["raw_fields_sha256"]
                ):
                    raise ValueError(
                        "Exact AVL original CSV or raw-field cache changed during run"
                    )
                violations["dedup_global_observation_count"] = abs(
                    observations - sum(retained_counts.values())
                )
                if any(violations.values()):
                    raise ValueError("Exact AVL global source conservation failed")
            if (
                self._code_hashes() != code
                or self._inputs()[0] != fixture_manifest["input_sha256"]
            ):
                raise ValueError(
                    "Algorithm source or raw inputs changed during the experiment"
                )
            summary = {
                "source_counts": fixture_manifest["source_counts"],
                "representation": representation,
                "vehicles": len(selection),
                "status_counts": dict(statuses),
                "segments": segments,
                "validation_ok": True,
                "violations": dict(violations),
                "examined": dict(examined),
                "accuracy": None,
                "publishable": False,
                "elapsed_s": round(time.perf_counter() - started, 3),
                "validation_scope": (
                    "Source conservation and elementary accepted-witness bounds; "
                    "not independent trip truth, counter-evidence completeness, "
                    "or accuracy."
                ),
            }
            if stream_mode:
                summary.update(
                    observations=observations,
                    vehicle_streams=len(selection),
                    trip_segmentation_used=False,
                    observation_matching_used=False,
                    matched_accuracy=None,
                    avl_deduplication=dedup_mode,
                    retained_counts=retained_counts,
                    removed_avl=removed_avl,
                    exact_duplicate_groups=duplicate_groups,
                    duplicate_involving_rows=duplicate_rows,
                    validation_scope=(
                        "Every source record retained once; original fields, "
                        "calendar-day membership and deterministic display order "
                        "checked independently. "
                        "No trip segmentation or APC–AVL matching was executed."
                    ),
                )
                if dedup_mode:
                    summary["validation_scope"] = (
                        "All thirteen original CSV field strings checked for exact AVL "
                        "equality; minimum source_row retained and every original AVL "
                        "mapped once. All APC and representative values preserved; "
                        "in-memory and persisted mapping/ordering independently "
                        "replayed. "
                        "No trip segmentation, matching or accuracy evaluation."
                    )
            _write(folder / "summary.json", summary)
            record = {
                "run_id": run_id,
                "label": label,
                "parent_run_id": parent_run_id,
                "date": self.date,
                "method": method,
                "representation": representation,
                "created_at": created,
                "config": config,
                "summary": summary,
                "path": str(folder),
                "status": "complete",
            }
            _write(folder / "run.json", record)
            _write(
                folder / "completion.json",
                {
                    "debug_date": self.date,
                    "validation_ok": True,
                    "full_debug_day": True,
                    "publishable": False,
                },
            )
            emit("complete", len(selection), len(selection))
            return record
        except Exception as exc:
            _write(
                folder / "failure.json",
                {
                    "error": str(exc),
                    "violations": dict(violations),
                    "publishable": False,
                },
            )
            raise

    def compare(self, base_id: str, run_id: str, vehicle: str | None = None) -> dict:
        base, current = self.get_run(base_id), self.get_run(run_id)
        bm, cm = _json(base / "manifest.json"), _json(current / "manifest.json")
        selections = [_json(p / "selection.json") for p in (base, current)]
        labels = {
            r["vehicle_case"]: r["raw_vehicle"] for rows in selections for r in rows
        }
        if vehicle is not None:
            matches = [key for key, value in labels.items() if vehicle in (key, value)]
            if len(matches) != 1:
                raise ValueError("Unknown or ambiguous comparison vehicle")
            labels = {matches[0]: labels[matches[0]]}
        base_representation = bm.get("representation", "apc_avl_direct_evidence")
        representation = cm.get("representation", "apc_avl_direct_evidence")
        if GTFS_REPRESENTATION in (base_representation, representation):
            from tools.merge_review.gtfs_matching.runner import compare_runs

            return compare_runs(base, current, labels, vehicle, bm, cm)
        if direction_trips.REPRESENTATION in (base_representation, representation):
            allowed = {vehicle_stream.REPRESENTATION, direction_trips.REPRESENTATION}
            if base_representation in allowed and representation in allowed:
                return direction_runner.compare_runs(
                    base, current, labels, vehicle, bm, cm
                )
        if base_representation != representation:
            return {
                "base_run_id": base_id,
                "run_id": run_id,
                "vehicle": vehicle,
                "comparable": False,
                "not_comparable": True,
                "comparison_status": "not_comparable",
                "reason": (
                    "Vehicle-only UNION ALL did not execute observation matching or "
                    "trip segmentation. Its untested observations cannot be compared "
                    "with matching dispositions as gains, losses, or unmatched rows."
                ),
                "base_representation": base_representation,
                "representation": representation,
                "scopes": {"base": bm.get("scope"), "current": cm.get("scope")},
                "source_counts": {
                    "base": bm.get("source_counts"),
                    "current": cm.get("source_counts"),
                },
                "source_compatible": bm.get("canonical_input_sha256")
                == cm.get("canonical_input_sha256"),
                "summary": None,
                "changes": [],
                "boundaries": {"added": [], "removed": [], "changed": []},
                "accuracy": None,
                "publishable": False,
            }
        if representation == vehicle_stream.REPRESENTATION:
            return self._compare_streams(base, current, labels, vehicle, bm, cm)
        sources = {}
        for manifest in (bm, cm):
            query = pl.scan_parquet(Path(manifest["audit"]) / "apc.parquet")
            if vehicle is not None:
                query = query.filter(pl.col("vehicle").is_in(list(labels.values())))
            sources.update({r["rid"]: r for r in query.collect().to_dicts()})
        changes, added_boundaries, removed_boundaries, changed_boundaries = (
            [],
            [],
            [],
            [],
        )
        totals = collections.Counter()
        transitions = collections.Counter()
        for case, label in sorted(labels.items()):
            sides = []
            for folder in (base, current):
                method = _json(folder / "config.json")["method"]
                chunks = _rows(folder / "chunks" / method / f"{case}.parquet")
                signatures = {
                    r["segment_id"]: _digest(sorted(r["ping_ids"])) for r in chunks
                }
                assignments = {
                    r["apc_rid"]: r
                    for r in _rows(folder / "assignments" / method / f"{case}.parquet")
                }
                boundaries = {
                    (r["before_rid"], r["after_rid"]): r
                    for r in _rows(folder / "boundaries" / method / f"{case}.parquet")
                }
                sides.append((signatures, assignments, boundaries))
            old, new = sides[0][1], sides[1][1]
            totals.update(
                apc_common=len(old.keys() & new.keys()),
                apc_added=len(new.keys() - old.keys()),
                apc_removed=len(old.keys() - new.keys()),
            )
            for rid in sorted(old.keys() | new.keys()):
                a, b = old.get(rid), new.get(rid)
                kinds = []
                if a is None or b is None:
                    kinds.append("added" if a is None else "removed")
                else:
                    transitions[f"{a.get('status')}→{b.get('status')}"] += 1
                    if a.get("status") != b.get("status"):
                        kinds.append("status_changed")
                    if (a.get("support_level"), a.get("support_kind")) != (
                        b.get("support_level"),
                        b.get("support_kind"),
                    ):
                        kinds.append("support_changed")

                    def owners(row, signatures):
                        ids = set(row.get("segment_ids") or [])
                        if row.get("segment_id"):
                            ids.add(row["segment_id"])
                        return sorted(signatures.get(key, "unmapped") for key in ids)

                    if owners(a, sides[0][0]) != owners(b, sides[1][0]):
                        kinds.append("owner_membership_changed")
                    evidence_fields = (
                        "ping_ids",
                        "witnesses",
                        "counter_witnesses",
                        "endpoint_conflicts",
                        "route_state",
                        "interval_excursion",
                    )
                    if _digest({k: a.get(k) for k in evidence_fields}) != _digest(
                        {k: b.get(k) for k in evidence_fields}
                    ):
                        kinds.append("evidence_changed")
                    if any(
                        a.get(key) != b.get(key)
                        for key in ("score", "margin", "reason", "candidate_count")
                    ):
                        kinds.append("decision_changed")
                if kinds:
                    totals.update(k for k in kinds if k not in ("added", "removed"))
                    source = sources.get(rid, {})

                    def compact(row):
                        return (
                            None
                            if row is None
                            else {
                                k: row.get(k)
                                for k in (
                                    "status",
                                    "support_level",
                                    "support_kind",
                                    "segment_id",
                                    "segment_ids",
                                    "context_status",
                                )
                            }
                        )

                    changes.append(
                        {
                            "apc_rid": rid,
                            "vehicle": label,
                            "vehicle_case": case,
                            "arrival": _clock(source.get("arrival")),
                            "departure": _clock(source.get("departure")),
                            "change_types": kinds,
                            "base": compact(a),
                            "current": compact(b),
                        }
                    )
            for index, target in ((0, removed_boundaries), (1, added_boundaries)):
                for key in sorted(sides[index][2].keys() - sides[1 - index][2].keys()):
                    boundary = sides[index][2][key]
                    target.append(
                        {
                            "vehicle": label,
                            "vehicle_case": case,
                            "before_rid": key[0],
                            "after_rid": key[1],
                            "at": _clock(boundary["at"]),
                            "reasons": boundary.get("reasons", []),
                        }
                    )
            for key in sorted(sides[0][2].keys() & sides[1][2].keys()):
                previous, present = sides[0][2][key], sides[1][2][key]
                if set(previous.get("reasons", [])) != set(present.get("reasons", [])):
                    changed_boundaries.append(
                        {
                            "vehicle": label,
                            "vehicle_case": case,
                            "before_rid": key[0],
                            "after_rid": key[1],
                            "at": _clock(present["at"]),
                            "base_reasons": previous.get("reasons", []),
                            "current_reasons": present.get("reasons", []),
                        }
                    )
        keys = (
            "apc_common",
            "apc_added",
            "apc_removed",
            "status_changed",
            "support_changed",
            "owner_membership_changed",
            "evidence_changed",
            "decision_changed",
        )
        summary = {key: totals[key] for key in keys} | {
            "changed_rows": len(changes),
            "boundaries_added": len(added_boundaries),
            "boundaries_removed": len(removed_boundaries),
            "boundary_reasons_changed": len(changed_boundaries),
            "status_transitions": dict(transitions),
        }
        return {
            "base_run_id": base_id,
            "run_id": run_id,
            "vehicle": vehicle,
            "comparable": True,
            "not_comparable": False,
            "comparison_status": "comparable",
            "source_compatible": bm.get("canonical_input_sha256")
            == cm.get("canonical_input_sha256"),
            "summary": summary,
            "changes": changes,
            "boundaries": {
                "added": added_boundaries,
                "removed": removed_boundaries,
                "changed": changed_boundaries,
            },
            "truncated": False,
            "accuracy": None,
            "publishable": False,
        }

    def _compare_streams(self, base, current, labels, vehicle, bm, cm):
        """Compare only source identity, retained values and display chronology."""
        totals = collections.Counter()
        scoped_retained_counts = [
            collections.Counter(avl=0, apc=0),
            collections.Counter(avl=0, apc=0),
        ]
        changes = []
        relative_order_preserved = True
        ignored = vehicle_stream.DERIVED_FIELDS
        dedup_comparison = bool(
            bm.get("avl_deduplication") or cm.get("avl_deduplication")
        )
        raw_caches = []
        for folder, manifest in ((base, bm), (current, cm)):
            if not manifest.get("avl_deduplication"):
                raw_caches.append(None)
                continue
            definition = manifest.get("deduplication") or {}
            if definition.get("rule") != avl_dedup.RULE or definition.get(
                "fields"
            ) != list(avl_dedup.RAW_FIELDS):
                raise ValueError("Unknown exact-AVL deduplication rule in comparison")
            raw_path = folder / "deduplication" / "raw_avl_fields.parquet"
            if evaluate.file_hash(raw_path) != definition.get("raw_fields_sha256"):
                raise ValueError("Raw exact-AVL comparison fields changed")
            raw_caches.append(pl.read_parquet(raw_path))
        apc_unchanged = True
        for case, label in sorted(labels.items()):
            rows = [
                _rows(folder / "observations" / f"{case}.parquet")
                for folder in (base, current)
            ]
            for counts, side in zip(scoped_retained_counts, rows, strict=True):
                counts.update(r["source"] for r in side)
            keys = [[(r["source"], r["source_rid"]) for r in side] for side in rows]
            indexes = [
                dict(zip(keylist, side, strict=True))
                for keylist, side in zip(keys, rows, strict=True)
            ]
            old, new = indexes
            lineage = []
            for side, (folder, raw_cache) in enumerate(
                zip((base, current), raw_caches, strict=True)
            ):
                if raw_cache is None:
                    lineage.append({})
                    continue
                maps = _rows(folder / "deduplication" / f"{case}.parquet")
                raw = raw_cache.filter(pl.col("vehicle") == label).to_dicts()
                original_keys = [
                    {"rid": r["source_rid"], "source_row": r["source_row"]} for r in raw
                ]
                retained_rows = [r for r in rows[side] if r["source"] == "avl"]
                check = avl_dedup.validate_avl_deduplication(
                    original_keys, raw, retained_rows, maps
                )
                if any(check["violations"].values()):
                    raise ValueError("Exact-AVL comparison lineage does not validate")
                lineage.append({r["source_rid"]: r for r in maps})
            common = old.keys() & new.keys()
            old_apc, new_apc = (
                {k for k in index if k[0] == "apc"} for index in indexes
            )
            apc_unchanged &= old_apc == new_apc
            totals.update(
                observations_common=len(common),
                observations_added=len(new.keys() - old.keys()),
                observations_removed=len(old.keys() - new.keys()),
                duplicate_source_rids=sum(
                    len(side) - len(index)
                    for side, index in zip(rows, indexes, strict=True)
                ),
            )
            relative_order_preserved &= [key for key in keys[0] if key in common] == [
                key for key in keys[1] if key in common
            ]
            for key in sorted(old.keys() | new.keys()):
                previous, present = old.get(key), new.get(key)
                kinds = []
                if previous is None or present is None:
                    added = previous is None
                    explanation = (
                        lineage[0 if added else 1].get(key[1])
                        if key[0] == "avl"
                        else None
                    )
                    if explanation and not explanation["is_representative"]:
                        kinds.append(
                            "exact_avl_duplicate_restored"
                            if added
                            else "exact_avl_duplicate_removed"
                        )
                        totals[
                            "dedup_explained_additions"
                            if added
                            else "dedup_explained_removals"
                        ] += 1
                    else:
                        kinds.append("added" if added else "removed")
                        totals[
                            "unexplained_additions" if added else "unexplained_removals"
                        ] += 1
                else:
                    if previous.get("event_time") != present.get("event_time"):
                        kinds.append("event_time_changed")
                    if previous.get("display_order") != present.get("display_order"):
                        if dedup_comparison:
                            totals["display_ordinals_renumbered"] += 1
                        else:
                            kinds.append("display_order_changed")
                    original_keys = (previous.keys() | present.keys()) - ignored
                    if _digest({k: previous.get(k) for k in original_keys}) != _digest(
                        {k: present.get(k) for k in original_keys}
                    ):
                        kinds.append("payload_changed")
                        if key[0] == "apc":
                            apc_unchanged = False
                if kinds:
                    totals.update(k for k in kinds if k not in {"added", "removed"})
                    row = present if present is not None else previous
                    changes.append(
                        {
                            "source": key[0],
                            "source_rid": key[1],
                            "rid": key[1],
                            "apc_rid": key[1] if key[0] == "apc" else None,
                            "vehicle": label,
                            "vehicle_case": case,
                            "event_time": _clock(row.get("event_time")),
                            "arrival": _clock(row.get("arrival")),
                            "departure": _clock(row.get("departure")),
                            "change_types": kinds,
                            "representative_rid": (
                                explanation["representative_rid"]
                                if (previous is None or present is None) and explanation
                                else None
                            ),
                        }
                    )
        fields = (
            "observations_common",
            "observations_added",
            "observations_removed",
            "duplicate_source_rids",
            "event_time_changed",
            "display_order_changed",
            "payload_changed",
            "dedup_explained_removals",
            "dedup_explained_additions",
            "unexplained_removals",
            "unexplained_additions",
            "display_ordinals_renumbered",
        )
        summary = {key: totals[key] for key in fields} | {
            "changed_rows": len(changes),
            "source_records_conserved": not any(
                totals[key]
                for key in (
                    "observations_added",
                    "observations_removed",
                    "duplicate_source_rids",
                )
            ),
            "relative_order_preserved": relative_order_preserved,
            "apc_unchanged": apc_unchanged,
            "source_lineage_conserved": apc_unchanged
            and not any(
                totals[key]
                for key in (
                    "unexplained_removals",
                    "unexplained_additions",
                    "duplicate_source_rids",
                    "event_time_changed",
                    "payload_changed",
                )
            ),
        }
        return {
            "base_run_id": base.name,
            "run_id": current.name,
            "vehicle": vehicle,
            "comparable": True,
            "not_comparable": False,
            "comparison_status": "source_stream_comparison",
            "representation": vehicle_stream.REPRESENTATION,
            "avl_deduplication_comparison": dedup_comparison,
            "deduplication_note": (
                "Exact raw-field AVL duplicate copies are represented by their "
                "minimum-source-row retained record, with complete original lineage. "
                "Explained removals are not missing observations or matching accuracy "
                "improvements; display ordinals are renumbered after filtering."
            )
            if dedup_comparison
            else None,
            "retained_counts": {
                "base": dict(scoped_retained_counts[0]),
                "current": dict(scoped_retained_counts[1]),
            },
            "global_retained_counts": {
                "base": bm.get("retained_counts", bm.get("source_counts")),
                "current": cm.get("retained_counts", cm.get("source_counts")),
            },
            "source_compatible": bm.get("canonical_input_sha256")
            == cm.get("canonical_input_sha256"),
            "summary": summary,
            "changes": changes,
            "boundaries": {"added": [], "removed": [], "changed": []},
            "scopes": {"base": bm.get("scope"), "current": cm.get("scope")},
            "source_counts": {
                "base": bm.get("source_counts"),
                "current": cm.get("source_counts"),
            },
            "truncated": False,
            "accuracy": None,
            "publishable": False,
        }
