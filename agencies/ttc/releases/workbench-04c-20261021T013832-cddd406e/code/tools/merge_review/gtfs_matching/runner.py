"""Append-only route-29 matching after auditable joint-source gap refinement."""

from __future__ import annotations

import collections
import gzip
import hashlib
import json
import math
import multiprocessing
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from tools.merge_review import direction_trips
from tools.merge_review.data import json_safe

from . import METHOD, REPRESENTATION
from .case_storage import decode_case_artifact, encode_case_artifact

_WORKER_CONTEXT = None


def initialize_worker(canonical, config):
    global _WORKER_CONTEXT
    _WORKER_CONTEXT = canonical, config


def evaluate_segment(observations, canonical=None, config=None):
    """Independent pure-data worker; no output or original input writes."""
    from .decision import apply_decision_guard
    from .integration import match_candidate_segment
    from .projection import project_sequence

    if canonical is None:
        canonical, config = _WORKER_CONTEXT
    avl = [
        dict(r, t=clock(r.get("time"))) for r in observations if r["source"] == "avl"
    ]
    apc = [
        dict(
            r,
            t=clock(r.get("event_time")),
            arrival=clock(r.get("arrival")),
            departure=clock(r.get("departure")),
        )
        for r in observations
        if r["source"] == "apc"
    ]
    projection = project_sequence(avl, canonical["references"], config, apc=apc)
    result = match_candidate_segment(
        projection,
        projection.get("apc_evidence", []),
        canonical["references"],
        canonical["trips"],
        config,
        service_date=None,
        route_id=canonical["route_id"],
    )
    return projection, apply_decision_guard(result, config)


def evaluate_segment_modes(observations, canonical=None, config=None):
    """Project once; retain the original whole-segment result as the global mode."""
    if canonical is None:
        canonical, config = _WORKER_CONTEXT
    projection, result = evaluate_segment(observations, canonical, config)
    modes = {}
    if (config or {}).get("matching_partial_modes_enabled", False):
        from .partial import evaluate_matching_modes

        modes = evaluate_matching_modes(projection, canonical, config, result)
    return projection, result, modes


def artifact_key(case_id):
    return hashlib.sha256(case_id.encode()).hexdigest()[:24]


def delay_clock_violations(result, points):
    """Reject impossible corrected clocks before publishing a completed run."""
    from .windows import observation_key

    sources = {observation_key(row): row for row in points}
    violations = 0
    for candidate in result.get("all_candidates", []):
        previous = {}
        for row in candidate.get("whole_alignment", {}).get("delay_process", []):
            corrected = row.get("corrected_t")
            if corrected is None:
                continue
            t, delay, fragment = (
                row.get("t"),
                row.get("delay_s"),
                row.get("fragment_id"),
            )
            original = sources.get(row.get("source_id"))
            if (
                original is None
                or t != original.get("t")
                or fragment is None
                or not all(
                    isinstance(v, (int, float)) and math.isfinite(v)
                    for v in (t, delay, corrected)
                )
            ):
                violations += 1
                continue
            violations += abs((t - delay) - corrected) > 1e-6
            if fragment in previous:
                last_t, last_corrected = previous[fragment]
                violations += t < last_t or corrected + 1e-6 < last_corrected
            previous[fragment] = (t, corrected)
    return violations


def write_case(path, artifact):
    """Append-only gzip; store the global result once, retain all evidence."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(
        encode_case_artifact(artifact), separators=(",", ":"), allow_nan=False
    ).encode()
    with path.open("xb") as stream:
        path.chmod(0o600)
        with gzip.GzipFile(
            fileobj=stream, mode="wb", compresslevel=1, mtime=0
        ) as archive:
            archive.write(payload)


def read_case(path):
    with gzip.open(path, "rt") as stream:
        return decode_case_artifact(json.load(stream))


def write_fresh_continuous_shadow(folder, rows_by_mode, config, output_hashes):
    """Persist standalone diagnostics without inventing a historical comparator.

    Fresh raw-to-current runs have no old scorer evaluation. The same saved
    episode format remains available for a later explicit shadow comparison;
    vehicle-day decoding is annotation-only and cannot promote a trip decision.
    """
    from tools.merge_review.debug import _write

    from .continuous.evaluation import evaluate_shadow
    from .continuous.sequence import decode_vehicle_day
    from .feed import file_sha256

    provenance = {
        "mode": "fresh_raw_to_current",
        "comparison_status": "NO_COMPARATOR",
        "old_scorer_evaluated": False,
        "independent_labels_available": False,
        "sequence_annotation_only": True,
        "final_decisions_changed_by_sequence": False,
    }
    for mode, records in rows_by_mode.items():
        rows = sorted(records, key=lambda row: row["case_id"])
        report = evaluate_shadow([], rows, config=config)
        report["provenance"] = dict(provenance)
        report["descriptive"].update(
            status_changes=None,
            top_rank_changes=None,
            comparison_status="NO_COMPARATOR",
        )
        sequence = decode_vehicle_day(rows, dict(config, sequence_enabled=True))
        sequence["provenance"] = dict(provenance)
        for relative, value in (
            (
                f"shadow/{mode}-episodes.json",
                {"old": [], "new": rows, "provenance": dict(provenance)},
            ),
            (f"shadow/{mode}-report.json", report),
            (f"shadow/{mode}-vehicle-sequence.json", sequence),
        ):
            _write(folder / relative, value)
            output_hashes[relative] = file_sha256(folder / relative)


def route_29(row):
    return str(row.get("route", "")).strip() == "29"


def clock(value):
    if isinstance(value, datetime):
        return (value - datetime(1970, 1, 1)).total_seconds()
    if isinstance(value, str):
        return clock(datetime.fromisoformat(value))
    return value


def direction_parent(workspace, parent_run_id):
    if parent_run_id is None:
        raise ValueError("Experiment 03 requires an experiment 02 parent run")
    parent = workspace.get_run(parent_run_id)
    from tools.merge_review.debug import _json

    manifest = _json(parent / "manifest.json")
    if manifest.get("representation") == REPRESENTATION:
        direction_run_id = manifest["direction_parent_run_id"]
        try:
            direction = workspace.get_run(direction_run_id)
        except ValueError:
            # Retention removes only old run locations, not their pinned evidence.
            # Existing malformed/incomplete originals must still fail closed.
            if not isinstance(direction_run_id, str):
                raise
            original = workspace.root / "runs" / direction_run_id
            if original.exists() or original.is_symlink():
                raise
            from tools.merge_review.retention import resolve_direction_parent

            direction = resolve_direction_parent(parent, manifest)
        parent = direction
        manifest = _json(parent / "manifest.json")
    if manifest.get("representation") != direction_trips.REPRESENTATION:
        raise ValueError("Select experiment 02 (or its GTFS descendant) as the parent")
    if manifest.get("debug_date") != workspace.date:
        raise ValueError("Parent must use the same debug day")
    return parent, manifest


def scope_vehicle(rows, metadata, vehicle, vehicle_case):
    """Filter route only; never re-infer or silently repair a parent boundary."""
    selected, excluded = [], []
    skipped_avl = False
    for original in rows:
        if not route_29(original):
            excluded.append(original)
            skipped_avl |= original["source"] == "avl"
            continue
        row = dict(original)
        if row["source"] == "avl":
            row["scope_separator_before"] = skipped_avl
            skipped_avl = False
        selected.append(row)
    members = collections.defaultdict(list)
    for row in selected:
        ids = row.get("candidate_segment_ids") or [f"direction:{vehicle}:unresolved"]
        for case_id in ids:
            members[case_id].append(row)
    metadata = direction_trips.restore_metadata(metadata)
    segments = {s["id"]: s for s in metadata.get("segments", [])}
    catalog = []
    for case_id, observations in members.items():
        segment = segments.get(case_id, {})
        times = [
            clock(r["event_time"])
            for r in observations
            if r.get("event_time") is not None
        ]
        apc = [r for r in observations if r["source"] == "apc"]
        conflicts = sorted(
            {c for r in observations for c in (r.get("direction_conflict_ids") or [])}
        )
        start = clock(segment.get("start"))
        end = clock(segment.get("end"))
        catalog.append(
            {
                **json_safe(segment),
                "id": case_id,
                "kind": "direction_segment" if segment else "direction_unresolved",
                "vehicle": vehicle_case,
                "vehicle_label": vehicle,
                "date": "2023-11-06",
                "start": start if start is not None else min(times, default=None),
                "end": end if end is not None else max(times, default=None),
                "source_start": min(times, default=None),
                "source_end": max(times, default=None),
                "routes": ["29"],
                "avl_count": len(observations) - len(apc),
                "avl_member_count": len(observations) - len(apc),
                "apc_count": len(apc),
                "apc_rids": [r["rid"] for r in apc],
                "conflict_ids": conflicts,
                "direction_conflict_count": len(conflicts),
                "conflict_count": len(conflicts),
                "candidate_only": True,
                "complete_trip": False,
                "scope_boundary_start": segment.get("scope_censored_start", False),
                "scope_boundary_end": segment.get("scope_censored_end", False),
                "parent_boundary_preserved": True,
                "parent_source_rids": json_safe(segment.get("source_rids", [])),
                "source_rids": [
                    dict(source=r["source"], rid=r["rid"]) for r in observations
                ],
                "parent_route_929_influence_possible": any(
                    case_id in (r.get("candidate_segment_ids") or []) for r in excluded
                ),
            }
        )
    catalog.sort(key=lambda r: (r["start"] or 0, r["id"]))
    return selected, excluded, members, catalog


def create_run(workspace, config, label, parent_run_id, progress=None):
    from tools.merge_review.debug import (
        VERSION,
        _directory,
        _json,
        _parquet,
        _rows,
        _write,
    )

    from .canonical import build_references
    from .feed import file_sha256, load_feed
    from .gaps import policy, split_direction_gaps, validate_gap_split

    started = time.perf_counter()

    def emit(stage, done=0, total=0, **extra):
        if progress:
            progress(
                dict(
                    stage=stage,
                    message=stage,
                    completed=done,
                    total=total,
                    elapsed_s=round(time.perf_counter() - started, 3),
                    **extra,
                )
            )

    emit("validating_experiment_02_parent")
    parent, parent_manifest = direction_parent(workspace, parent_run_id)
    fixture = Path(parent_manifest["audit"])
    fixture_manifest = _json(fixture / "manifest.json")
    workspace._fixture_valid(fixture, fixture_manifest)
    if workspace._inputs()[0] != fixture_manifest["input_sha256"]:
        raise ValueError("Original source identity changed since experiment 02")
    code = workspace._code_hashes()
    parent_hashes = {
        name: file_sha256(parent / name)
        for name in (
            "manifest.json",
            "config.json",
            "summary.json",
            "completion.json",
            "selection.json",
        )
    }
    emit("loading_pinned_route_29_gtfs")
    cfg = config["gtfs"]
    continuous_enabled = cfg.get("continuous_matching_enabled", False)
    bundle = load_feed(workspace.date, "29", cfg)
    canonical = build_references(bundle, cfg)
    run_id = (
        "run-20231106-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        + "-"
        + uuid.uuid4().hex[:12]
    )
    folder = workspace.root / "runs" / run_id
    _directory(folder.parent)
    folder.mkdir(mode=0o700)
    created = datetime.now(UTC).isoformat()
    selection, inputs = [], {}
    for item in _json(parent / "selection.json"):
        path = parent / "observations" / f"{item['vehicle_case']}.parquet"
        frame = pl.read_parquet(path)
        if (
            "route" not in frame.columns
            or not frame.filter(
                pl.col("route").cast(pl.String).str.strip_chars() == "29"
            ).height
        ):
            continue
        selection.append(item)
        inputs[item["vehicle_case"]] = file_sha256(path)
        for relative in (
            f"direction/{item['vehicle_case']}.json",
            f"deduplication/{item['vehicle_case']}.parquet",
        ):
            if (parent / relative).is_file():
                parent_hashes[relative] = file_sha256(parent / relative)
    sidecar = "deduplication/raw_avl_fields.parquet"
    if (parent / sidecar).is_file():
        parent_hashes[sidecar] = file_sha256(parent / sidecar)
    _write(folder / "selection.json", selection)
    _write(folder / "config.json", config)
    manifest = {
        "version": VERSION,
        "run_id": run_id,
        "debug_date": workspace.date,
        "scope": parent_manifest["scope"],
        "created_at": created,
        "audit": str(fixture),
        "baseline": str(folder),
        "methods": [METHOD],
        "segmentation_methods": [METHOD],
        "representation": REPRESENTATION,
        "case_artifact_suffix": ".json.gz",
        "parent_run_id": parent_run_id,
        "direction_parent_run_id": parent.name,
        "direction_parent_path": str(parent),
        "route_scope": ["29"],
        "excluded_routes": ["929"],
        "code_sha256": code,
        "canonical_input_sha256": parent_manifest["canonical_input_sha256"],
        "fixture_identity": parent_manifest["fixture_identity"],
        "parent_sha256": parent_hashes,
        "parent_observations_sha256": inputs,
        "source_counts": parent_manifest["source_counts"],
        "gtfs_used": True,
        "gtfs_feed_version": bundle["feed_version"],
        "gtfs_archive_sha256": bundle["archive_sha256"],
        "gtfs_selection": bundle["selection"],
        "route_id": bundle["route_id"],
        "trip_segmentation_used": True,
        "parent_boundaries_preserved": True,
        "parent_boundaries_refined": True,
        "joint_gap_segmentation": policy(cfg),
        "observation_matching_used": False,
        "schedule_matching_used": True,
        "available_matching_modes": (
            ["global", "early", "middle", "late"]
            if cfg.get("matching_partial_modes_enabled", False)
            else ["global"]
        ),
        "avl_deduplication": True,
        "full_debug_day": True,
        "full_fixture": False,
        "service_date_inferred": False,
        "accuracy": None,
        "publishable": False,
        "probability_calibrated": False,
        "boundary_censoring": parent_manifest.get("boundary_censoring"),
    }
    if continuous_enabled:
        manifest.update(
            scorer="continuous_passage_v1",
            fresh_continuous_matching=True,
            shadow_comparison_status="NO_COMPARATOR",
            sequence_annotation_only=True,
        )
    _write(folder / "manifest.json", manifest)
    _write(
        folder / "gtfs" / "feed.json",
        {
            k: v
            for k, v in bundle.items()
            if k not in {"trips", "reference_trips", "stops", "shapes"}
        },
    )
    _write(folder / "gtfs" / "canonical.json", canonical)
    output_hashes = {}
    totals, statuses, violations = (
        collections.Counter(),
        collections.Counter(),
        collections.Counter(),
    )
    mode_statuses = collections.defaultdict(collections.Counter)
    overview_cases = collections.defaultdict(list)
    shadow_rows = {mode: [] for mode in manifest["available_matching_modes"]}
    executor = (
        ProcessPoolExecutor(
            max_workers=cfg["execution_workers"],
            mp_context=multiprocessing.get_context("spawn"),
            initializer=initialize_worker,
            initargs=(canonical, cfg),
        )
        if cfg["execution_workers"] > 1
        else None
    )
    try:
        for vehicle_index, item in enumerate(selection, 1):
            key, vehicle = item["vehicle_case"], item["raw_vehicle"]
            rows = _rows(parent / "observations" / f"{key}.parquet")
            metadata = _json(parent / "direction" / f"{key}.json")
            selected, excluded, members, catalog = scope_vehicle(
                rows, metadata, vehicle, key
            )
            parent_members, parent_catalog = members, catalog
            members, catalog, gap_audit = split_direction_gaps(members, catalog, cfg)
            gap_check = validate_gap_split(
                parent_members, parent_catalog, members, catalog, cfg
            )
            violations.update(
                {"gap_" + k: v for k, v in gap_check["violations"].items()}
            )
            if any(violations.values()):
                raise ValueError(
                    f"Joint-source gap invariant failure: {dict(violations)}"
                )
            totals["parent_cases"] += len(parent_catalog)
            totals["gap_split_parent_cases"] += gap_audit["split_parent_cases"]
            totals["joint_gap_count"] += gap_audit["gap_count"]
            totals["gap_unlocated_observations"] += gap_audit["unlocated_count"]
            original_frame = pl.read_parquet(parent / "observations" / f"{key}.parquet")
            _parquet(
                folder / "observations" / f"{key}.parquet",
                original_frame.filter(
                    pl.col("route").cast(pl.String).str.strip_chars() == "29"
                ),
            )
            _write(
                folder / "scope" / f"{key}.json",
                dict(
                    selected_count=len(selected),
                    excluded_count=len(excluded),
                    excluded_by_route=dict(
                        collections.Counter(str(r.get("route")) for r in excluded)
                    ),
                    excluded_source_ids=[
                        dict(
                            source=r["source"],
                            rid=r["rid"],
                            route=r.get("route"),
                            event_time=clock(r.get("event_time")),
                        )
                        for r in excluded
                    ],
                    unresolved_count=sum(
                        not r.get("candidate_segment_ids") for r in selected
                    ),
                    parent_boundaries_preserved=True,
                    parent_boundaries_refined=True,
                    joint_gap_segmentation=gap_audit,
                ),
            )
            violations["persisted_observation_fields"] += not pl.read_parquet(
                folder / "observations" / f"{key}.parquet"
            ).equals(
                original_frame.filter(
                    pl.col("route").cast(pl.String).str.strip_chars() == "29"
                )
            )
            totals.update(r["source"] for r in selected)
            totals["excluded_observations"] += len(excluded)
            futures = (
                {
                    s["id"]: executor.submit(evaluate_segment_modes, members[s["id"]])
                    for s in catalog
                }
                if executor
                else {}
            )
            for case_index, summary in enumerate(catalog, 1):
                observations = members[summary["id"]]
                avl = [
                    dict(r, t=clock(r.get("time")))
                    for r in observations
                    if r["source"] == "avl"
                ]
                apc = [
                    dict(
                        r,
                        t=clock(r.get("event_time")),
                        arrival=clock(r.get("arrival")),
                        departure=clock(r.get("departure")),
                    )
                    for r in observations
                    if r["source"] == "apc"
                ]
                projection, matched, matching_modes = (
                    futures[summary["id"]].result()
                    if executor
                    else evaluate_segment_modes(observations, canonical, cfg)
                )
                evidence = projection.get("apc_evidence", [])
                points = projection.get("points", [])
                evidence_by_id = {str(r["rid"]): r for r in evidence}
                violations["apc_source_conservation"] += set(evidence_by_id) != {
                    str(r["rid"]) for r in apc
                }
                violations["apc_duplicate_identity"] += len(evidence) != len(
                    evidence_by_id
                )
                for original in apc:
                    projected_apc = evidence_by_id.get(str(original["rid"]), {})
                    for field in (
                        "rid",
                        "source_row",
                        "vehicle",
                        "arrival",
                        "departure",
                        "stop",
                        "route",
                        "direction",
                        "boarding",
                        "alighting",
                        "lat",
                        "lon",
                    ):
                        violations["apc_original_fields"] += json_safe(
                            projected_apc.get(field)
                        ) != json_safe(original.get(field))
                    violations["apc_interval_time"] += projected_apc.get(
                        "interval_start"
                    ) != original.get("arrival") or projected_apc.get(
                        "interval_end"
                    ) != original.get("departure")
                violations["projection_source_conservation"] += set(
                    str(r["rid"]) for r in avl
                ) != set(str(r["rid"]) for r in points)
                violations["projection_duplicate_identity"] += len(points) != len(
                    {str(r["rid"]) for r in points}
                )
                violations["distance_gate"] += sum(
                    r.get("projection_status") == "ON_REFERENCE"
                    and float(
                        r.get("projection_distance_m", r.get("lateral_m", 0)) or 0
                    )
                    > cfg.get("max_projection_distance_m", 100) + 1e-6
                    for r in points
                )
                violations["too_many_final_candidates"] += (
                    len(matched.get("candidates", [])) > 3
                )
                violations["alternate_distance_gate"] += sum(
                    a.get("projection_status") == "ON_REFERENCE"
                    and (
                        a.get("lateral_m") is None
                        or a["lateral_m"] > cfg["max_projection_distance_m"] + 1e-6
                    )
                    for r in points
                    for a in r.get("projection_alternatives", [])
                )
                violations["shared_delay_clock"] += delay_clock_violations(
                    matched, points
                )
                if any(violations.values()):
                    raise ValueError(
                        f"GTFS pipeline invariant failure: {dict(violations)}"
                    )
                artifact = dict(
                    case_id=summary["id"],
                    projection=projection,
                    apc_evidence=evidence,
                    result=matched,
                    parent_boundary=summary,
                    config=cfg,
                )
                if matching_modes:
                    from .multimode import validate_modes

                    validate_modes(projection, matched, matching_modes, cfg)
                    artifact["matching_modes"] = matching_modes
                    summary["matching_modes"] = mode_summaries(matching_modes)
                    for mode, entry in matching_modes.items():
                        mode_statuses[mode][entry["result"]["status"]] += 1
                from .overview import overview_case

                for mode in manifest["available_matching_modes"]:
                    overview_cases[mode].append(
                        overview_case(artifact, summary, key, vehicle, canonical, mode)
                    )
                    if continuous_enabled:
                        from .continuous.serialization import shadow_row

                        mode_result = matching_modes.get(mode, {}).get(
                            "result", matched
                        )
                        shadow_rows[mode].append(
                            shadow_row(mode_result, summary, canonical, mode)
                        )
                case_path = folder / "cases" / f"{artifact_key(summary['id'])}.json.gz"
                write_case(case_path, json_safe(artifact))
                violations["persisted_case_content"] += read_case(
                    case_path
                ) != json_safe(artifact)
                output_hashes[str(case_path.relative_to(folder))] = file_sha256(
                    case_path
                )
                summary.update(
                    gtfs_status=matched.get("status", "UNMATCHED"),
                    gtfs_candidate_count=len(matched.get("candidates", [])),
                    pattern_confidence=matched.get("pattern_confidence"),
                    trip_confidence=matched.get("trip_confidence"),
                )
                statuses[summary["gtfs_status"]] += 1
                totals["cases"] += 1
                totals["projection_points"] += len(points)
                totals["off_reference_points"] += sum(
                    r.get("projection_status") == "OFF_REFERENCE" for r in points
                )
                emit(
                    "matching_route_29_direction_candidates",
                    vehicle_index - 1,
                    len(selection),
                    vehicle=vehicle,
                    case_completed=case_index,
                    case_total=len(catalog),
                    matched_cases=totals["cases"],
                    case_id=summary["id"],
                )
            _write(folder / "catalog" / f"{key}.json", json_safe(catalog))
            violations["persisted_catalog_content"] += _json(
                folder / "catalog" / f"{key}.json"
            ) != json_safe(catalog)
            persisted_gap_check = validate_gap_split(
                parent_members,
                parent_catalog,
                members,
                _json(folder / "catalog" / f"{key}.json"),
                cfg,
            )
            violations.update(
                {
                    "persisted_gap_" + k: v
                    for k, v in persisted_gap_check["violations"].items()
                }
            )
            for relative in (
                f"observations/{key}.parquet",
                f"scope/{key}.json",
                f"catalog/{key}.json",
            ):
                output_hashes[relative] = file_sha256(folder / relative)
        from .overview import build_day_overview

        for mode, records in overview_cases.items():
            relative = f"overview/{mode}.json"
            _write(
                folder / relative,
                build_day_overview(
                    records,
                    canonical,
                    run_id=run_id,
                    day=workspace.date,
                    matching_mode=mode,
                    available_matching_modes=manifest["available_matching_modes"],
                ),
            )
            output_hashes[relative] = file_sha256(folder / relative)
        if continuous_enabled:
            write_fresh_continuous_shadow(folder, shadow_rows, cfg, output_hashes)
        violations["parent_changed"] = sum(
            file_sha256(parent / name) != digest
            for name, digest in parent_hashes.items()
        )
        violations["parent_observations_changed"] = sum(
            file_sha256(parent / "observations" / f"{key}.parquet") != digest
            for key, digest in inputs.items()
        )
        violations["gtfs_archive_changed"] = (
            file_sha256(bundle["feed_path"]) != bundle["archive_sha256"]
        )
        violations["gtfs_manifest_changed"] = (
            file_sha256(bundle["manifest_path"]) != bundle["manifest_sha256"]
        )
        violations["code_changed"] = workspace._code_hashes() != code
        violations["original_inputs_changed"] = (
            workspace._inputs()[0] != fixture_manifest["input_sha256"]
        )
        workspace._fixture_valid(fixture, fixture_manifest)
        if any(violations.values()):
            raise ValueError("Source or code changed during the experiment")
        summary = dict(
            totals,
            observations=totals["avl"] + totals["apc"],
            vehicles=len(selection),
            retained_counts={s: totals[s] for s in ("avl", "apc")},
            status_counts=dict(statuses),
            matching_mode_status_counts={
                mode: dict(counts) for mode, counts in mode_statuses.items()
            },
            representation=REPRESENTATION,
            gtfs_feed=bundle["selection"],
            gtfs_diagnostics=bundle["diagnostics"],
            canonical_diagnostics=canonical["diagnostics"],
            validation_ok=True,
            violations=dict(violations),
            accuracy=None,
            publishable=False,
            probability_calibrated=False,
            joint_gap_segmentation=policy(cfg),
            elapsed_s=round(time.perf_counter() - started, 3),
            validation_scope=(
                "Source conservation, scope, projection gate and persistence "
                "invariants; not independent matching accuracy."
            ),
        )
        if continuous_enabled:
            summary.update(
                scorer="continuous_passage_v1",
                fresh_continuous_matching=True,
                available_matching_modes=manifest["available_matching_modes"],
                shadow_comparison_status="NO_COMPARATOR",
                shadow_evaluation_reports=[
                    f"shadow/{mode}-report.json" for mode in shadow_rows
                ],
                sequence_annotation_only=True,
            )
        _write(folder / "summary.json", summary)
        for relative in (
            "manifest.json",
            "config.json",
            "selection.json",
            "gtfs/feed.json",
            "gtfs/canonical.json",
            "summary.json",
        ):
            output_hashes[relative] = file_sha256(folder / relative)
        _write(folder / "output_sha256.json", output_hashes)
        record = dict(
            run_id=run_id,
            label=label,
            parent_run_id=parent_run_id,
            date=workspace.date,
            method=METHOD,
            representation=REPRESENTATION,
            created_at=created,
            config=config,
            summary=summary,
            path=str(folder),
            status="complete",
        )
        _write(folder / "run.json", record)
        _write(
            folder / "completion.json",
            dict(
                debug_date=workspace.date,
                validation_ok=True,
                full_debug_day=True,
                route_scope=["29"],
                publishable=False,
            ),
        )
        emit("complete", len(selection), len(selection))
        return record
    except BaseException as exc:
        _write(
            folder / "failure.json",
            dict(error=str(exc), violations=dict(violations), publishable=False),
        )
        raise
    finally:
        if executor:
            executor.shutdown(wait=True, cancel_futures=True)


def mode_summaries(modes):
    return {
        mode: {
            "status": entry["result"].get("status", "UNMATCHED"),
            "candidate_count": len(entry["result"].get("candidates", [])),
            "gtfs_status": entry["result"].get("status", "UNMATCHED"),
            "gtfs_candidate_count": len(entry["result"].get("candidates", [])),
            "pattern_confidence": entry["result"].get("pattern_confidence"),
            "trip_confidence": entry["result"].get("trip_confidence"),
            "selection_status": entry["selection"].get("status"),
            "effective_progress_m": entry["selection"].get("effective_progress_m"),
        }
        for mode, entry in modes.items()
    }


def compare_runs(base, current, labels, vehicle, bm, cm):
    """Compare scope conservation and candidate statuses, not matching accuracy."""
    from tools.merge_review.debug import _json, _rows

    counts = {"base": collections.Counter(), "current": collections.Counter()}
    changes, violations = [], collections.Counter()
    for key, label in labels.items():
        sides = []
        for name, folder in (("base", base), ("current", current)):
            rows = [
                r
                for r in _rows(folder / "observations" / f"{key}.parquet")
                if route_29(r)
            ]
            sides.append({(r["source"], str(r["rid"])): r for r in rows})
            counts[name].update(r["source"] for r in rows)
        left, right = sides
        violations["route_29_identity_changed"] += len(left.keys() ^ right.keys())
        for name, folder in (("base", base), ("current", current)):
            path = folder / "catalog" / f"{key}.json"
            if path.is_file():
                for case in _json(path):
                    changes.append(
                        dict(
                            vehicle=key,
                            vehicle_label=label,
                            case_id=case["id"],
                            side=name,
                            change="GTFS_EVALUATION",
                            status=case.get("gtfs_status"),
                        )
                    )
    return dict(
        base_run_id=base.name,
        run_id=current.name,
        vehicle=vehicle,
        comparable=True,
        not_comparable=False,
        comparison_status="scope_and_diagnostics_only",
        reason=(
            "Compare route-29 source conservation and GTFS diagnostics only; "
            "experiment 02 has no GTFS decisions. No accuracy gain is implied."
        ),
        base_representation=bm.get("representation"),
        representation=cm.get("representation"),
        route_scope=["29"],
        source_compatible=bm.get("canonical_input_sha256")
        == cm.get("canonical_input_sha256"),
        source_counts={k: dict(v) for k, v in counts.items()},
        summary={"violations": dict(violations)},
        changes=changes,
        boundaries={"added": [], "removed": [], "changed": []},
        accuracy=None,
        publishable=False,
    )
