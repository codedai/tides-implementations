"""Read-only visualization adapters for immutable Experiment 05 stage outputs.

No scoring, assignment, projection or source joins occur here. All distances are
persisted canonical metres, times are UTC epoch seconds, and every reference has
its own progress axis. Display thinning never changes a matching metric.
"""

from __future__ import annotations

import bisect
import collections
import json
import math
import re
import threading
import tomllib
from pathlib import Path

import polars as pl

from .io import reject_symlinks, sha256

SCHEMA = "experiment05-visualization-v1"
MODES = ("global", "early", "middle", "late")
# Presentation limits, not algorithm thresholds. Detail retains every source row.
OVERVIEW_FRAGMENT_POINTS = 160
MAX_CACHED_RUNS = 2
OBS_COLUMNS = (
    "rid",
    "source",
    "source_row",
    "trip_id_performed",
    "vehicle_id",
    "t",
    "lat",
    "lon",
    "s_m",
    "reference_id",
    "fragment_id",
    "trajectory_included",
    "projection_status",
    "projection_distance_m",
    "duplicate_of",
    "censored_tail",
    "exclude_from_matching",
    "heading",
    "speed_kph",
    "direction_label",
    "projected_direction_label",
    "platform_state",
    "rejection_reason",
    "scope_separator_before",
    "sequence_restart",
    "arrival",
    "departure",
    "boarding",
    "alighting",
    "mapped_stop_id",
    "stop_visit_id",
    "trip_stop_sequence",
    "apc_attachment_reason",
    "apc_evidence_included",
    "grain_uncertain",
    "canonical_stop_intervals",
    "route_eligible",
)
CANDIDATE_COLUMNS = (
    "trip_id_performed",
    "match_mode",
    "candidate_id",
    "candidate_rank",
    "trip_id_scheduled",
    "trip_id",
    "reference_id",
    "family_id",
    "direction_id",
    "direction_label",
    "score",
    "estimated_delay_s",
    "delay_mad_s",
    "delay_uncertainty_s",
    "probability",
    "calibrated",
    "coverage",
    "hard_constraints_valid",
    "quality_eligible",
    "shape_id",
    "trip_headsign",
    "score_direction",
    "valid_projection_point_ratio",
    "valid_projection_duration_ratio",
    "schedule_aligned_point_ratio",
    "schedule_aligned_duration_ratio",
    "off_reference_ratio",
    "continuous_support_windows",
)
CANDIDATE_DIAGNOSTICS = (
    "score_components",
    "score_components_unweighted",
    "score_component_weights",
    "hard_constraint_reasons",
    "quality_gates",
    "reasons",
    "occurrence_decision_guard",
)


class VisualizationError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _signature(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _decode(rows, keys):
    for row in rows:
        for key in keys:
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    return rows


def _bounds(values):
    finite = [value for value in values if _finite(value)]
    return {
        "start_t": min(finite) if finite else None,
        "end_t": max(finite) if finite else None,
    }


def fragments(rows, gap_s, limit=None):
    """Never bridge rejected evidence, reference changes, restarts or GPS gaps."""
    output, points, previous = [], [], None

    def finish():
        nonlocal points
        if not points:
            return
        n = len(points)
        if limit is not None and n > limit:
            indices = sorted({round(i * (n - 1) / (limit - 1)) for i in range(limit)})
            shown = [points[i] for i in indices]
        else:
            shown = points
        output.append(
            {
                "fragment_id": (
                    f"{previous.get('fragment_id') or 'valid'}:display:{len(output)}"
                ),
                "reference_id": previous["reference_id"],
                "points": shown,
                "source_point_count": n,
                "display_point_count": len(shown),
            }
        )
        points = []

    for row in sorted(
        rows, key=lambda r: (r.get("t") is None, r.get("t") or 0, r["rid"])
    ):
        # Exact copies do not become false separators or extra service evidence.
        if row.get("duplicate_of"):
            continue
        valid = (
            row.get("trajectory_included")
            and not row.get("exclude_from_matching")
            and not row.get("censored_tail")
            and row.get("reference_id")
            and _finite(row.get("s_m"))
            and _finite(row.get("t"))
        )
        if not valid:
            finish()
            previous = None
            continue
        if previous is not None and (
            row.get("reference_id") != previous.get("reference_id")
            or row.get("fragment_id") != previous.get("fragment_id")
            or row.get("scope_separator_before")
            or row.get("sequence_restart")
            or row["t"] - previous["t"] > gap_s
        ):
            finish()
        points.append({"t": row["t"], "s_m": row["s_m"], "rid": row["rid"]})
        previous = row
    finish()
    return output


def scheduled_vertices(schedule):
    """Unshifted, unsampled piecewise-linear schedule; preserve nonzero dwell."""
    result = []
    for stop in schedule:
        if not _finite(stop.get("s_m")):
            continue
        for key in ("arrival_t", "departure_t"):
            if _finite(stop.get(key)):
                point = {"t": stop[key], "s_m": stop["s_m"]}
                if not result or result[-1] != point:
                    result.append(point)
    return result


def _check_assigned_candidate(assignment, candidates):
    if assignment.get("trip_id_scheduled") is None:
        return
    matched = [
        row
        for row in candidates
        if row.get("candidate_id") == assignment.get("candidate_id")
        and row.get("trip_id_scheduled") == assignment["trip_id_scheduled"]
    ]
    if len(matched) != 1:
        raise VisualizationError(
            409,
            "ASSIGNMENT_CANDIDATE_MISMATCH",
            "Published assignment does not reference exactly one "
            "saved top-three candidate",
        )


def _reference_geometry(reference, schedule):
    points = reference.get("points", [])
    ss = [row["s_m"] for row in points]
    scheduled_s = [row["s_m"] for row in schedule if _finite(row.get("s_m"))]
    if not points or not scheduled_s:
        return []

    def at(value):
        index = max(0, min(len(points) - 2, bisect.bisect_right(ss, value) - 1))
        a, b = points[index], points[min(index + 1, len(points) - 1)]
        fraction = (
            max(0, min(1, (value - a["s_m"]) / (b["s_m"] - a["s_m"])))
            if b["s_m"] != a["s_m"]
            else 0
        )
        return {
            "s_m": value,
            **{key: a[key] + fraction * (b[key] - a[key]) for key in ("lat", "lon")},
        }

    lo, hi = max(ss[0], min(scheduled_s)), min(ss[-1], max(scheduled_s))
    if hi < lo:
        return []
    return [
        at(lo),
        *[
            {k: p[k] for k in ("lat", "lon", "s_m")}
            for p in points
            if lo < p["s_m"] < hi
        ],
        at(hi),
    ]


class PinnedView:
    def __init__(self, folder, manifest):
        self.folder, self.manifest = reject_symlinks(folder), manifest
        self.pins = manifest["output_sha256"]
        self.signatures, self.cache = {}, {}

    def check_path(self, relative):
        # Filenames are internal constants, never user-supplied file paths.
        if relative not in self.pins or not re.fullmatch(
            r"[a-f0-9]{64}", self.pins[relative]
        ):
            raise VisualizationError(
                404,
                "VISUALIZATION_INPUT_UNAVAILABLE",
                f"Missing published input: {relative}",
            )
        try:
            path = reject_symlinks(self.folder / relative)
            before = _signature(path)
            if self.signatures.get(relative) != before:
                if sha256(path) != self.pins[relative] or _signature(path) != before:
                    raise VisualizationError(
                        409,
                        "OUTPUT_HASH_CHANGED",
                        f"Published input changed: {relative}",
                    )
                self.signatures[relative] = before
            return path
        except (OSError, ValueError) as exc:
            if isinstance(exc, VisualizationError):
                raise
            raise VisualizationError(
                409, "OUTPUT_HASH_CHANGED", f"Unreadable published input: {relative}"
            ) from exc

    def check_dependencies(self):
        for name in list(self.signatures):
            self.check_path(name)

    def metadata(self, relative):
        path = self.check_path(relative)
        if relative not in self.cache:
            self.cache[relative] = json.loads(path.read_text())
            self.check_path(relative)
        return self.cache[relative]

    def table(self, name, columns=None, predicate=None, decode=True):
        relative = name + ".parquet"
        path = self.check_path(relative)
        meta = self.metadata(name + ".schema.json")
        if meta.get("sha256") != self.pins[relative]:
            raise VisualizationError(409, "TABLE_PIN_MISMATCH", name)
        scan = pl.scan_parquet(path)
        if predicate is not None:
            scan = scan.filter(predicate)
        if columns is not None:
            scan = scan.select(
                [
                    pl.col(k) if k in meta["columns"] else pl.lit(None).alias(k)
                    for k in columns
                ]
            )
        frame = scan.collect()
        self.check_path(relative)
        if not decode:
            return frame
        return _decode(frame.to_dicts(), meta.get("json_columns", []))

    @property
    def canonical(self):
        return self.metadata("gtfs/canonical.json")

    @property
    def config(self):
        path = self.check_path("config.toml")
        if "config" not in self.cache:
            self.cache["config"] = tomllib.loads(path.read_text())
            self.check_path("config.toml")
        return self.cache["config"]

    def common(self, mode):
        return {
            "schema": SCHEMA,
            "run_id": self.manifest["run_id"],
            "service_date": self.manifest["service_date"],
            "route_id": self.manifest["route_id"],
            "timezone": self.config["normalization"]["timezone"],
            "match_mode": mode,
            "match_modes": self.manifest["match_modes"],
            "published_pipeline_version": self.manifest.get("pipeline_version"),
            "read_only": True,
            "scoring_recomputed": False,
        }

    def observations(self):
        if "observations" not in self.cache:
            self.cache["observations"] = self.table(
                "fused_projected_observations",
                OBS_COLUMNS,
                pl.col("route_eligible").fill_null(False),
                decode=False,
            )
        return self.cache["observations"]

    def assignments(self, mode):
        key = ("assignments", mode)
        if key not in self.cache:
            self.cache[key] = {
                r["trip_id_performed"]: r
                for r in self.table(
                    "assignments", predicate=pl.col("match_mode") == mode
                )
            }
        return self.cache[key]

    def ranks(self, mode):
        key = ("ranks", mode)
        if key not in self.cache:
            self.cache[key] = self.table(
                "candidate_rankings",
                CANDIDATE_COLUMNS,
                pl.col("match_mode") == mode,
                decode=False,
            )
        return self.cache[key]

    def catalog(self, mode):
        key = ("catalog", mode)
        if key in self.cache:
            return self.cache[key]
        segments = self.table("performed_trip_segments")
        assignment = self.assignments(mode)
        ranks = self.ranks(mode)
        nc = {
            r["trip_id_performed"]: r["len"]
            for r in ranks.group_by("trip_id_performed").len().to_dicts()
        }
        states = {
            r["trip_id_performed"]: r
            for r in self.table(
                "mode_results",
                ("trip_id_performed", "matcher_status"),
                pl.col("match_mode") == mode,
            )
        }
        observations = self.observations()
        stats = {}
        for group in observations.partition_by(
            "trip_id_performed", as_dict=True
        ).items():
            (trip_id,), frame = group
            if trip_id is None:
                continue
            avl = frame.filter(pl.col("source") == "avl")
            apc = frame.filter(pl.col("source") == "apc")
            stats[trip_id] = {
                "avl_count": avl.height,
                "apc_count": apc.height,
                "valid_avl_count": avl.filter(
                    pl.col("trajectory_included")
                    & ~pl.col("duplicate_of").is_not_null()
                    & ~pl.col("censored_tail").fill_null(False)
                    & ~pl.col("exclude_from_matching").fill_null(False)
                ).height,
                "off_reference_count": avl.filter(
                    pl.col("projection_status") == "OFF_REFERENCE"
                ).height,
                "censored_count": avl.filter(
                    pl.col("censored_tail").fill_null(False)
                ).height,
                "reference_ids": sorted(
                    avl["reference_id"].drop_nulls().unique().to_list()
                ),
            }
        direction_labels = {
            r["direction_id"]: r.get("direction_label") or str(r["direction_id"])
            for r in self.canonical["references"]
        }
        result = []
        for segment in segments:
            trip_id = segment["trip_id_performed"]
            assigned = assignment.get(trip_id, {})
            result.append(
                {
                    **segment,
                    **stats.get(trip_id, {}),
                    "direction_label": direction_labels.get(
                        segment["direction_id"], str(segment["direction_id"])
                    ),
                    "raw_start_t": segment["raw_segment_start"],
                    "raw_end_t": segment["raw_segment_end"],
                    "effective_start_t": segment["effective_segment_start"],
                    "effective_end_t": segment["effective_segment_end"],
                    "assignment_status": assigned.get("assignment_status", "UNMATCHED"),
                    "trip_id_scheduled": assigned.get("trip_id_scheduled"),
                    "candidate_count": nc.get(trip_id, 0),
                    "matcher_status": states.get(trip_id, {}).get("matcher_status"),
                    "unmatched_reason": assigned.get("unmatched_reason"),
                    "score": assigned.get("score"),
                    "candidate_rank": assigned.get("candidate_rank"),
                }
            )
        result.sort(key=lambda r: (r["raw_start_t"], r["trip_id_performed"]))
        self.cache[key] = {
            **self.common(mode),
            "trips": result,
            "directions": [
                {"direction_id": k, "direction_label": v}
                for k, v in sorted(direction_labels.items())
            ],
            "counts": {
                "performed_trips": len(result),
                "route_observations": observations.height,
                "unattached_apc": observations.filter(
                    (pl.col("source") == "apc")
                    & ~pl.col("apc_evidence_included").fill_null(False)
                ).height,
            },
        }
        return self.cache[key]

    def candidate(self, row, assignment, detailed=False):
        canonical_trip = next(
            (
                t
                for t in self.canonical["trips"]
                if t["trip_id"] == row.get("trip_id_scheduled", row.get("trip_id"))
                and t.get("reference_id") == row.get("reference_id")
            ),
            None,
        )
        schedule = (
            []
            if canonical_trip is None
            else [
                {
                    k: stop.get(k)
                    for k in (
                        "stop_id",
                        "stop_sequence",
                        "s_m",
                        "arrival_t",
                        "departure_t",
                    )
                }
                for stop in canonical_trip.get("scheduled", [])
            ]
        )
        ref = next(
            (
                r
                for r in self.canonical["references"]
                if r["reference_id"] == row.get("reference_id")
            ),
            {},
        )
        result = {
            **row,
            "assigned": assignment.get("candidate_id") is not None
            and row.get("candidate_id") == assignment["candidate_id"],
            "schedule": schedule,
            "trajectory": scheduled_vertices(schedule),
            "schedule_available": bool(schedule),
            "schedule_delay_shifted": False,
        }
        if detailed:
            result.update(
                shape_geo=_reference_geometry(ref, schedule),
                geometry_basis="CANONICAL_REFERENCE_WITHIN_SCHEDULED_PROGRESS",
            )
        return result

    def trip(self, mode, trip_id):
        segment = next(
            (
                r
                for r in self.catalog(mode)["trips"]
                if r["trip_id_performed"] == trip_id
            ),
            None,
        )
        if segment is None:
            raise VisualizationError(
                404, "TRIP_NOT_FOUND", "Performed trip is not present in this run"
            )
        raw = _decode(
            self.observations()
            .filter(pl.col("trip_id_performed") == trip_id)
            .to_dicts(),
            ("canonical_stop_intervals",),
        )
        avl = sorted(
            [r for r in raw if r["source"] == "avl"],
            key=lambda r: (r["t"] is None, r["t"] or 0, r["rid"]),
        )
        stops = {r["stop_id"]: r for r in self.metadata("gtfs/bundle.json")["stops"]}
        apc = []
        for row in raw:
            if row["source"] != "apc":
                continue
            stop = stops.get(row.get("mapped_stop_id"), {})
            apc.append(
                {
                    **row,
                    "stop_id": row.get("mapped_stop_id"),
                    "stop_name": stop.get("stop_name"),
                    "lat": stop.get("lat"),
                    "lon": stop.get("lon"),
                    "coordinate_role": "STOP_LOCATION_NOT_VEHICLE_PING",
                }
            )
        apc.sort(
            key=lambda r: (
                r.get("arrival") is None,
                r.get("arrival") or r.get("t") or 0,
                r["rid"],
            )
        )
        assignment = self.assignments(mode).get(trip_id, {})
        chosen = self.table(
            "candidate_rankings",
            (*CANDIDATE_COLUMNS, *CANDIDATE_DIAGNOSTICS),
            (pl.col("match_mode") == mode)
            & (pl.col("trip_id_performed") == trip_id)
            & (pl.col("candidate_rank") <= 3),
        )
        candidates = [
            self.candidate(r, assignment, True)
            for r in sorted(chosen, key=lambda r: r["candidate_rank"])
        ]
        _check_assigned_candidate(assignment, candidates)
        decisions = self.table(
            "mode_results",
            ("selection", "matcher_reasons", "trip_confidence", "pattern_confidence"),
            (pl.col("match_mode") == mode) & (pl.col("trip_id_performed") == trip_id),
        )
        decision = decisions[0] if decisions else {}
        selection = decision.get("selection") or {}
        selected_ids = {
            rid.split("|", 1)[-1] for rid in selection.get("selected_avl_ids", [])
        }
        for row in avl:
            row["selected_for_mode"] = row["rid"] in selected_ids
        refs = set(segment.get("reference_ids", [])) | {
            r.get("reference_id") for r in candidates
        }
        references = [
            {
                **{
                    k: r.get(k)
                    for k in (
                        "reference_id",
                        "family_id",
                        "direction_id",
                        "direction_label",
                        "length_m",
                    )
                },
                "points": [
                    {k: p[k] for k in ("lat", "lon", "s_m")} for p in r["points"]
                ],
            }
            for r in self.canonical["references"]
            if r["reference_id"] in refs
        ]
        gap = self.config["matcher"]["continuous_coverage_max_gap_s"]
        bounds = _bounds(
            [
                segment["raw_start_t"],
                segment["raw_end_t"],
                *[p["t"] for p in avl],
                *[p.get(k) for p in apc for k in ("arrival", "departure")],
                *[p["t"] for c in candidates for p in c["trajectory"]],
            ]
        )
        return {
            **self.common(mode),
            "trip": segment,
            "assignment": assignment,
            **decision,
            "selection": selection,
            "avl": avl,
            "apc": apc,
            "references": references,
            "fragments": fragments(avl, gap),
            "candidates": candidates,
            "bounds": bounds,
            "stop_visits": self.table(
                "stop_visits_evidence", predicate=pl.col("trip_id_performed") == trip_id
            ),
            "display_rules": {
                "detail_downsampled": False,
                "gps_gap_separator_s": gap,
                "schedule_time_shifted": False,
                "reference_axes_shared": False,
            },
        }

    def overview(self, mode):
        key = ("overview", mode)
        if key in self.cache:
            return self.cache[key]
        gap = self.config["matcher"]["continuous_coverage_max_gap_s"]
        avl = (
            self.observations()
            .filter(pl.col("source") == "avl")
            .select(
                "trip_id_performed",
                "rid",
                "t",
                "s_m",
                "reference_id",
                "fragment_id",
                "trajectory_included",
                "duplicate_of",
                "censored_tail",
                "exclude_from_matching",
                "scope_separator_before",
                "sequence_restart",
            )
        )
        by_trip = {
            key[0]: frame.to_dicts()
            for key, frame in avl.partition_by(
                "trip_id_performed", as_dict=True
            ).items()
            if key[0] is not None
        }
        ranks = collections.defaultdict(list)
        for row in self.ranks(mode).filter(pl.col("candidate_rank") <= 3).to_dicts():
            ranks[row["trip_id_performed"]].append(row)
        facets = {
            r["reference_id"]: {
                "reference_id": r["reference_id"],
                "direction_id": r["direction_id"],
                "direction_label": r["direction_label"],
                "length_m": r["length_m"],
                "segments": [],
            }
            for r in self.canonical["references"]
        }
        times = []
        for trip in self.catalog(mode)["trips"]:
            tid = trip["trip_id_performed"]
            assignment = self.assignments(mode).get(tid, {})
            candidates = sorted(ranks[tid], key=lambda r: r["candidate_rank"])
            _check_assigned_candidate(assignment, candidates)
            candidate = next(
                (
                    r
                    for r in candidates
                    if r["candidate_id"] == assignment.get("candidate_id")
                ),
                None,
            )
            role = (
                "ASSIGNED"
                if candidate is not None
                else "DIAGNOSTIC_FALLBACK"
                if candidates
                else "NO_CANDIDATE"
            )
            candidate = (
                self.candidate(candidate or candidates[0], assignment)
                if candidates
                else None
            )
            actual = fragments(by_trip.get(tid, []), gap, OVERVIEW_FRAGMENT_POINTS)
            refs = {f["reference_id"] for f in actual} | (
                {candidate["reference_id"]} if candidate else set()
            )
            refs.discard(None)
            for ref in sorted(refs):
                if ref not in facets:
                    continue
                facets[ref]["segments"].append(
                    {
                        "trip_id_performed": tid,
                        "vehicle_id": trip["vehicle_id"],
                        "assignment_status": trip["assignment_status"],
                        "display_role": role,
                        "fragments": [f for f in actual if f["reference_id"] == ref],
                        "candidate": candidate
                        if candidate and candidate["reference_id"] == ref
                        else None,
                    }
                )
            times.extend([trip["raw_start_t"], trip["raw_end_t"]])
            if candidate:
                times.extend(p["t"] for p in candidate["trajectory"])
        self.cache[key] = {
            **self.common(mode),
            "facets": list(facets.values()),
            "bounds": _bounds(times),
            "display_rules": {
                "overview_fragment_point_limit": OVERVIEW_FRAGMENT_POINTS,
                "detail_retains_all_source_rows": True,
                "reference_axes_shared": False,
                "fallback_is_assignment": False,
                "schedule_time_shifted": False,
            },
        }
        return self.cache[key]


class VisualizationStore:
    """Bounded run cache; validate all touched files again before cached responses."""

    def __init__(self):
        self.lock, self.runs = threading.RLock(), collections.OrderedDict()

    def get(self, folder, manifest, kind, mode, trip_id=None):
        if (
            manifest.get("status") != "complete"
            or manifest.get("validation_ok") is not True
        ):
            raise VisualizationError(
                409,
                "RUN_NOT_PUBLISHED",
                "Visualization requires a completed, validated run",
            )
        if mode not in MODES:
            raise VisualizationError(400, "INVALID_MODE", "Unknown match mode")
        if mode not in manifest.get("match_modes", []):
            raise VisualizationError(
                404, "MODE_NOT_PUBLISHED", "Selected mode was not run in this result"
            )
        if kind not in ("catalog", "trip", "overview"):
            raise VisualizationError(404, "NOT_FOUND", "Unknown visualization")
        if kind == "trip" and (
            not isinstance(trip_id, str) or not trip_id or len(trip_id) > 256
        ):
            raise VisualizationError(
                400, "INVALID_TRIP_ID", "A performed trip ID is required"
            )
        with self.lock:
            key = str(Path(folder).absolute())
            view = self.runs.get(key)
            if view is None or view.pins != manifest.get("output_sha256"):
                view = PinnedView(folder, manifest)
                self.runs[key] = view
            self.runs.move_to_end(key)
            while len(self.runs) > MAX_CACHED_RUNS:
                self.runs.popitem(last=False)
            view.check_dependencies()
            summary = view.metadata("summary.json")
            if summary.get("validation_ok") is not True:
                raise VisualizationError(
                    409, "RUN_NOT_PUBLISHED", "Summary has not passed validation"
                )
            result = (
                view.trip(mode, trip_id)
                if kind == "trip"
                else getattr(view, kind)(mode)
            )
            view.check_dependencies()
            return result
