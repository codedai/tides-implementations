"""The one shared, resumable stage implementation used by CLI and local web."""

from __future__ import annotations

import collections
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path

from tools.merge_review.gtfs_matching.canonical import build_references
from tools.merge_review.gtfs_matching.partial import MODES

from . import PIPELINE_VERSION, STAGES
from .config import DEFAULT_CONFIG, ROOT, load_config
from .io import (
    concatenate_rows,
    read_json,
    read_rows,
    reject_symlinks,
    sha256,
    write_json,
    write_rows,
)
from .sources import available_dates, load_active_feed, parse_date, source_paths

_WORKER = None
STAGE_OUTPUTS = {
    "load_raw": ("stages/raw_observations.parquet",),
    "normalize": ("stages/normalized_observations.parquet",),
    "event_timeline": ("stages/event_timeline.parquet",),
    "project_avl": (
        "stages/projected_observations.parquet",
        "gtfs",
        "projection_diagnostics.json",
    ),
    "segment_trips": (
        "performed_trip_segments.parquet",
        "platform_diagnostics.parquet",
        "stages/segmented_observations.parquet",
    ),
    "attach_apc": (
        "fused_projected_observations.parquet",
        "stop_visits_evidence.parquet",
        "apc_attachment_audit.parquet",
    ),
    "generate_candidates": (
        "stages/candidate_shards",
        "candidate_rankings.parquet",
        "local_window_evidence.parquet",
        "mode_results.parquet",
        "scheduled_lookup.parquet",
    ),
    "assign_scheduled": (
        "assignments.parquet",
        "assignment_audit.parquet",
        "assignment_summary.json",
    ),
    "map_tides": ("tides", "tides_reports.json"),
    "validate_publish": ("validation_report.json",),
}


def _stage_output_pins(folder, stage):
    paths = []
    for relative in STAGE_OUTPUTS[stage]:
        path = folder / relative
        if path.is_dir():
            paths.extend(child for child in path.rglob("*") if child.is_file())
        else:
            paths.append(path)
            if path.suffix == ".parquet":
                paths.append(path.with_suffix(".schema.json"))
    return {str(path.relative_to(folder)): sha256(path) for path in sorted(paths)}


def _worker_init(canonical, config, modes):
    global _WORKER
    _WORKER = canonical, config, modes


def _project_job(job):
    from .projection import project_vehicle

    return job[0], project_vehicle(job[1], _WORKER[0], _WORKER[1])


def _candidate_job(job):
    from .candidates import rank_segment

    return job[0]["trip_id_performed"], rank_segment(job[0], job[1], *_WORKER)


def code_identity(root=ROOT):
    root = Path(root)
    files = []
    for name in (
        "tools/experiment05",
        "tools/merge_review/gtfs_matching",
        "src/ttc_tides",
        "schemas/experiment05",
    ):
        files.extend(
            path
            for path in (root / name).rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix in {".py", ".json", ".html", ".css", ".js", ".toml"}
        )
    files.extend(
        root / name
        for name in (
            "pyproject.toml",
            "uv.lock",
            ".python-version",
            "experiment05.sh",
            "experiments/apc_avl_coverage_study/raw_audit.py",
            "experiments/apc_avl_coverage_study/__init__.py",
            "tools/merge_review/paths.py",
            "tools/merge_review/__init__.py",
        )
    )
    pins = {str(path.relative_to(root)): sha256(path) for path in sorted(set(files))}
    digest = hashlib.sha256(json.dumps(pins, sort_keys=True).encode()).hexdigest()
    return f"{PIPELINE_VERSION}+{digest[:16]}", pins


def environment_identity():
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "packages": {
            dist.metadata["Name"].lower().replace("_", "-"): dist.version
            for dist in importlib.metadata.distributions()
            if dist.metadata.get("Name")
        },
    }


def availability(root=ROOT, config_path=DEFAULT_CONFIG):
    config, config_hash = load_config(config_path)
    days = available_dates(root, config)
    import csv

    manifest_path = Path(root) / config["inputs"]["feed_manifest_path"]
    if not manifest_path.is_file():
        manifest = []
    else:
        with manifest_path.open(newline="", encoding="utf-8-sig") as stream:
            manifest = list(csv.DictReader(stream))
    for row in days:
        feeds = [
            item
            for item in manifest
            if item.get("calendar_min", "")
            <= row["service_date"]
            <= item.get("calendar_max", "")
            and item.get("status")
            in (
                {"approved", "candidate"}
                if config["matcher"].get("feed_allow_candidate")
                else {"approved"}
            )
            and (
                Path(root) / config["inputs"]["feed_gtfs_root"] / item["relative_path"]
            ).is_file()
        ]
        row["gtfs_available"] = bool(feeds)
        row["available"] = row["raw_available"] and row["gtfs_available"]
        row["reason"] = (
            None
            if row["available"]
            else "MISSING_RAW_INPUT"
            if not row["raw_available"]
            else "MISSING_GTFS_FEED"
        )
        row["feed_identifiers"] = sorted(item["feed_id"] for item in feeds)
    return {
        "dates": days,
        "route_ids": config["allowed_routes"],
        "match_modes": list(MODES),
        "config_hash": config_hash,
        "availability_note": (
            "Route-specific raw rows and archive presence; "
            "active service and hashes verified before run."
        ),
    }


def _validate_modes(modes):
    modes = (
        list(MODES)
        if modes is None
        else [modes]
        if isinstance(modes, str)
        else list(modes)
    )
    if not modes or len(modes) != len(set(modes)) or not set(modes).issubset(MODES):
        raise ValueError(
            "match_modes must select global, early, middle, late without duplicates"
        )
    return [mode for mode in MODES if mode in modes]


def _output_root(root):
    return reject_symlinks(Path(root).resolve() / "outputs/experiment_05/runs")


def verify_completed(run_dir):
    folder = reject_symlinks(run_dir)
    manifest = read_json(folder / "run_manifest.json")
    if manifest.get("status") != "complete" or not manifest.get("validation_ok"):
        raise ValueError("Run is not a validated successful cache entry")
    for name, expected in manifest["output_sha256"].items():
        path = reject_symlinks(folder / name)
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(folder.resolve())
            or sha256(path) != expected
        ):
            raise ValueError(f"Published output missing or changed: {name}")
    return manifest


def initialize_run(
    service_date,
    route_id="29",
    match_modes=None,
    force=False,
    root=ROOT,
    config_path=DEFAULT_CONFIG,
    output_dir=None,
):
    parse_date(service_date)
    root = Path(root).resolve()
    config, config_hash = load_config(config_path)
    if str(route_id) not in config["allowed_routes"]:
        raise ValueError("Only route_id 29 is enabled in this deployment")
    if not isinstance(force, bool):
        raise ValueError("force must be boolean")
    modes = _validate_modes(match_modes)
    paths = source_paths(root, config)
    inputs = {source: sha256(path) for source, path in paths.items()}
    day = next(
        (
            row
            for row in available_dates(root, config)
            if row["service_date"] == service_date
        ),
        None,
    )
    if not day or not day["raw_available"]:
        raise ValueError(
            f"Missing AVL or APC input for {service_date}, route {route_id}"
        )
    bundle = load_active_feed(root, service_date, route_id, config)
    version, code_pins = code_identity(root)
    environment = environment_identity()
    key_fields = {
        "service_date": service_date,
        "route_id": str(route_id),
        "match_modes": modes,
        "pipeline_version": version,
        "config_hash": config_hash,
        "input_hashes": inputs,
        "gtfs_feed_hash": bundle["archive_sha256"],
        "feed_manifest_hash": bundle["manifest_sha256"],
        "environment_hash": hashlib.sha256(
            json.dumps(environment, sort_keys=True).encode()
        ).hexdigest(),
    }
    cache_key = hashlib.sha256(
        json.dumps(key_fields, sort_keys=True).encode()
    ).hexdigest()
    parent = _output_root(root)
    run_id = f"run-{service_date}-{cache_key[:16]}"
    target = parent / run_id
    if target.is_dir() and not force and output_dir is None:
        existing = verify_completed(target)
        if existing.get("cache_key") != cache_key:
            raise ValueError("Existing output directory cache identity mismatch")
        return {
            "run_dir": str(target),
            "run_manifest": existing,
            "summary": read_json(target / "summary.json"),
            "cached": True,
        }
    if force:
        run_id += "-" + uuid.uuid4().hex[:8]
        target = parent / run_id
    if output_dir is not None:
        target = reject_symlinks(output_dir)
        if target.parent.resolve() != parent.resolve():
            raise ValueError(
                f"Explicit output_dir must be a new direct child of {parent}"
            )
        run_id = target.name
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id) is None:
        raise ValueError("Output directory name must be a Web-compatible stable run ID")
    if target.exists() or target.is_symlink():
        raise ValueError(
            "Output already exists but is not a successful reusable run; "
            "resume stages or use force"
        )
    target.mkdir(parents=True, mode=0o700)
    shutil.copyfile(config_path, target / "config.toml")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    manifest = {
        "schema": "experiment05-run-v1",
        "run_id": run_id,
        "status": "initialized",
        **key_fields,
        "cache_key": cache_key,
        "code_sha256": code_pins,
        "git_commit": commit,
        "git_worktree_hash_pinned": True,
        "environment": environment,
        "gtfs_route_id": bundle["route_id"],
        "route_short_name": bundle["route_short_name"],
        "input_paths": {source: str(path) for source, path in paths.items()},
        "gtfs_feed": {
            "identifier": bundle["feed_version"],
            "path": bundle["feed_path"],
            "sha256": bundle["archive_sha256"],
            "validity_period": bundle["validity_period"],
            "metadata_sha256": bundle["manifest_sha256"],
        },
        "stage_order": list(STAGES),
        "stages": {},
        "stage_row_counts": {},
        "created_at": datetime.now(UTC).isoformat(),
        "validation_ok": False,
        "date_policy": config["normalization"]["observation_window"],
        "comparison_to_04c_requested": False,
    }
    write_json(target / "run_manifest.json", manifest)
    return {"run_dir": str(target), "run_manifest": manifest, "cached": False}


def _parallel(jobs, worker, canonical, config, modes, emit, consume=None):
    workers = config["execution"]["workers"]
    if workers == 1 or len(jobs) <= 1:
        _worker_init(canonical, config, modes)
        results = []
        for index, job in enumerate(jobs, 1):
            result = worker(job)
            results.append(consume(result) if consume else result)
            emit(completed=index, total=len(jobs))
        return sorted(results, key=lambda pair: pair[0])
    # Polars must not be forked after initializing its native thread pool.
    import multiprocessing

    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_worker_init,
        initargs=(canonical, config, modes),
    ) as executor:
        queued = iter(jobs)
        futures = {
            executor.submit(worker, next(queued))
            for _ in range(min(workers, len(jobs)))
        }
        results = []
        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                # A completed Future owns its result until released. Bound
                # submissions too, so fast workers cannot outrun the sink.
                futures.remove(future)
                result = future.result()
                results.append(consume(result) if consume else result)
                emit(completed=len(results), total=len(jobs))
                job = next(queued, None)
                if job is not None:
                    futures.add(executor.submit(worker, job))
    return sorted(results, key=lambda pair: pair[0])


def _stage(name, folder, manifest, config, root, emit):
    def table(name):
        return folder / name

    if name == "load_raw":
        from .sources import read_raw_day

        rows, counts = read_raw_day(
            root, manifest["service_date"], config, manifest["input_hashes"]
        )
        write_rows(table("stages/raw_observations.parquet"), rows)
        return {"observations": len(rows), **counts}
    if name == "normalize":
        from .sources import normalize_sources

        rows = normalize_sources(
            read_rows(table("stages/raw_observations.parquet")),
            manifest["service_date"],
            config,
        )
        write_rows(table("stages/normalized_observations.parquet"), rows)
        return {
            "observations": len(rows),
            "invalid_time": sum(row["t"] is None for row in rows),
        }
    if name == "event_timeline":
        from .sources import event_timeline

        rows = event_timeline(
            read_rows(table("stages/normalized_observations.parquet"))
        )
        write_rows(table("stages/event_timeline.parquet"), rows)
        return {
            "observations": len(rows),
            "duplicates": sum(bool(row["duplicate_of"]) for row in rows),
            "route_evidence": sum(row["route_eligible"] for row in rows),
        }
    if name == "project_avl":
        from .projection import combine_projection, projection_jobs

        bundle = load_active_feed(
            root, manifest["service_date"], manifest["route_id"], config
        )
        if bundle["archive_sha256"] != manifest["gtfs_feed_hash"]:
            raise ValueError("GTFS feed changed since run initialization")
        canonical = build_references(bundle, config["matcher"])
        write_json(table("gtfs/bundle.json"), bundle)
        write_json(table("gtfs/canonical.json"), canonical)
        rows = read_rows(table("stages/event_timeline.parquet"))
        results = _parallel(
            projection_jobs(rows, config),
            _project_job,
            canonical,
            config,
            manifest["match_modes"],
            emit,
        )
        projected = [result for _, result in results]
        combined = combine_projection(rows, projected)
        write_rows(table("stages/projected_observations.parquet"), combined)
        write_json(
            table("projection_diagnostics.json"),
            {vehicle: result["diagnostics"] for vehicle, result in results},
        )
        return {
            "observations": len(combined),
            "valid_projected": sum(
                row["projection_status"] == "ON_REFERENCE" for row in combined
            ),
            "off_reference": sum(
                row["projection_status"] == "OFF_REFERENCE" for row in combined
            ),
            "canonical_references": len(canonical["references"]),
        }
    if name == "segment_trips":
        from .segmentation import segment_trips

        segments, rows, diagnostics = segment_trips(
            read_rows(table("stages/projected_observations.parquet")),
            read_json(table("gtfs/canonical.json")),
            manifest["service_date"],
            config,
        )
        if not segments:
            raise ValueError(
                "No performed segments from available raw inputs; "
                "refusing an empty successful run"
            )
        write_rows(table("performed_trip_segments.parquet"), segments)
        write_rows(table("stages/segmented_observations.parquet"), rows)
        write_rows(
            table("platform_diagnostics.parquet"),
            diagnostics,
            columns=("trip_id_performed", "state"),
        )
        return {
            "observations": len(rows),
            "performed_trips": len(segments),
            "right_censored_trips": sum(
                bool(row.get("censor_reason")) for row in segments
            ),
        }
    if name == "attach_apc":
        from .apc import attach_apc

        rows, visits, audit = attach_apc(
            read_rows(table("performed_trip_segments.parquet")),
            read_rows(table("stages/segmented_observations.parquet")),
            read_json(table("gtfs/canonical.json")),
            config,
        )
        write_rows(table("fused_projected_observations.parquet"), rows)
        write_rows(
            table("stop_visits_evidence.parquet"),
            visits,
            columns=("trip_id_performed", "stop_visit_id"),
        )
        write_rows(
            table("apc_attachment_audit.parquet"),
            audit,
            columns=("rid", "trip_id_performed", "reason"),
        )
        return {
            "observations": len(rows),
            "stop_visits": len(visits),
            "attached_apc": sum(
                row.get("apc_evidence_included", False) for row in rows
            ),
        }
    if name == "generate_candidates":
        from .candidates import scheduled_lookup

        canonical = read_json(table("gtfs/canonical.json"))
        segments = read_rows(table("performed_trip_segments.parquet"))
        members = collections.defaultdict(list)
        for row in read_rows(table("fused_projected_observations.parquet")):
            if row.get("trip_id_performed"):
                members[row["trip_id_performed"]].append(row)
        jobs = [
            (segment, members[segment["trip_id_performed"]]) for segment in segments
        ]
        layout = {
            "rankings": (
                "candidate_rankings.parquet",
                ("trip_id_performed", "match_mode", "candidate_rank", "score"),
            ),
            "windows": (
                "local_window_evidence.parquet",
                ("trip_id_performed", "match_mode"),
            ),
            "mode_results": (
                "mode_results.parquet",
                ("trip_id_performed", "match_mode"),
            ),
        }

        def consume(result):
            identity, payload = result
            key = hashlib.sha256(identity.encode()).hexdigest()
            target = table(f"stages/candidate_shards/{key}")
            for name, (_, columns) in layout.items():
                write_rows(target / f"{name}.parquet", payload[name], columns=columns)
            # Only this small descriptor survives in the parent process.
            return identity, str(target)

        results = _parallel(
            jobs,
            _candidate_job,
            canonical,
            config,
            manifest["match_modes"],
            emit,
            consume=consume,
        )
        counts = {
            name: concatenate_rows(
                table(destination),
                [Path(target) / f"{name}.parquet" for _, target in results],
                columns=columns,
            )
            for name, (destination, columns) in layout.items()
        }
        write_rows(table("scheduled_lookup.parquet"), scheduled_lookup(canonical))
        return {
            "candidates": counts["rankings"],
            "local_windows": counts["windows"],
            "mode_decisions": counts["mode_results"],
        }
    if name == "assign_scheduled":
        from .assignment import assign_trips

        before = sha256(table("candidate_rankings.parquet"))
        result = assign_trips(
            read_rows(table("performed_trip_segments.parquet")),
            read_rows(table("candidate_rankings.parquet")),
            read_rows(table("scheduled_lookup.parquet")),
            manifest["match_modes"],
        )
        write_rows(table("assignments.parquet"), result["assignments"])
        write_rows(
            table("assignment_audit.parquet"),
            result["audit"],
            columns=("trip_id_performed", "match_mode", "assignment_round"),
        )
        write_json(table("assignment_summary.json"), result["summary"])
        if sha256(table("candidate_rankings.parquet")) != before:
            raise ValueError("Assignment mutated frozen candidate rankings")
        return {
            "assignments": len(result["assignments"]),
            "matched": sum(
                row["trip_id_scheduled"] is not None for row in result["assignments"]
            ),
            "proposal_audits": len(result["audit"]),
        }
    if name == "map_tides":
        from .tides import publish_tides, read_completed_tides

        segments = read_rows(table("performed_trip_segments.parquet"))
        observations = [
            row
            for row in read_rows(table("fused_projected_observations.parquet"))
            if row.get("route_eligible")
        ]
        visits = read_rows(table("stop_visits_evidence.parquet"))
        assignments = read_rows(table("assignments.parquet"))
        bundle = read_json(table("gtfs/bundle.json"))
        reports = {}
        for mode in manifest["match_modes"]:
            target = table(f"tides/{mode}")
            binding = hashlib.sha256(
                json.dumps(
                    {
                        "run_id": manifest["run_id"],
                        "cache_key": manifest["cache_key"],
                        "pipeline_version": manifest["pipeline_version"],
                        "config_hash": manifest["config_hash"],
                        "mode": mode,
                        "upstream": {
                            stage: manifest["stages"][stage]["output_sha256"]
                            for stage in STAGES[: STAGES.index("map_tides")]
                        },
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            reports[mode] = (
                read_completed_tides(target, mode, expected_binding=binding)
                if target.is_dir()
                else publish_tides(
                    target,
                    mode,
                    segments,
                    observations,
                    visits,
                    assignments,
                    bundle,
                    config,
                    run_binding=binding,
                )
            )
        write_json(table("tides_reports.json"), reports)
        return {"packages": len(reports), "performed_trip_mode_rows": len(assignments)}
    if name == "validate_publish":
        from .validation import validate_pipeline

        report = validate_pipeline(folder, manifest, config)
        write_json(table("validation_report.json"), report)
        if not report["valid"]:
            raise ValueError(
                "Pipeline invariant validation failed: " + json.dumps(report["errors"])
            )
        return {"invariant_checks": len(report["checks"]), "validation_errors": 0}
    raise ValueError(f"Unknown stage: {name}")


def run_stage(stage, run_dir, root=ROOT, progress=None):
    if stage not in STAGES:
        raise ValueError(f"Unknown stage {stage}; choose {', '.join(STAGES)}")
    folder = reject_symlinks(run_dir)
    if folder.parent != _output_root(root).resolve():
        raise ValueError("Stage run directory is outside the pipeline output root")
    manifest = read_json(folder / "run_manifest.json")
    config, digest = load_config(folder / "config.toml")
    if (
        digest != manifest["config_hash"]
        or code_identity(root)[0] != manifest["pipeline_version"]
    ):
        raise ValueError("Pinned pipeline code/config changed; initialize a new run")
    if environment_identity() != manifest["environment"]:
        raise ValueError("Pinned dependency environment changed; initialize a new run")
    for previous in STAGES[: STAGES.index(stage)]:
        if manifest["stages"].get(previous, {}).get("status") != "complete":
            raise ValueError(f"Missing prerequisite stage: {previous}")
        for name, expected in manifest["stages"][previous]["output_sha256"].items():
            if sha256(folder / name) != expected:
                raise ValueError(f"Prerequisite stage output changed: {name}")
    existing = manifest["stages"].get(stage)
    if existing and existing.get("status") == "complete":
        for name, expected in existing["output_sha256"].items():
            if sha256(folder / name) != expected:
                raise ValueError(f"Completed stage output changed: {name}")
        return existing
    started = time.perf_counter()
    manifest["status"] = "running"
    manifest["stages"][stage] = {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
    }

    def emit(**details):
        manifest["progress"] = {"stage": stage, **details}
        write_json(folder / "run_manifest.json", manifest)
        if progress:
            progress({"stage": stage, "state": "running", **details})

    emit()
    try:
        counts = _stage(stage, folder, manifest, config, Path(root), emit)
        outputs = _stage_output_pins(folder, stage)
        manifest["stages"][stage].update(
            status="complete",
            completed_at=datetime.now(UTC).isoformat(),
            duration_s=time.perf_counter() - started,
            row_counts=counts,
            output_sha256=outputs,
        )
        manifest["stage_row_counts"][stage] = counts
        if stage == STAGES[-1]:
            _publish_summary(folder, manifest)
            manifest.update(
                status="complete",
                validation_ok=True,
                completed_at=datetime.now(UTC).isoformat(),
            )
            manifest["output_sha256"] = {
                str(path.relative_to(folder)): sha256(path)
                for path in folder.rglob("*")
                if path.is_file()
                and path.name != "run_manifest.json"
                and not path.name.endswith(".writing")
            }
        write_json(folder / "run_manifest.json", manifest)
        if progress:
            progress({"stage": stage, "state": "complete", "row_counts": counts})
        return manifest["stages"][stage]
    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["stages"][stage].update(status="failed", error=str(exc))
        manifest["error"] = str(exc)
        write_json(folder / "run_manifest.json", manifest)
        raise


def _publish_summary(folder, manifest):
    assignments = read_rows(folder / "assignments.parquet")
    reports = read_json(folder / "tides_reports.json")
    modes = {}
    for mode in manifest["match_modes"]:
        selected = [row for row in assignments if row["match_mode"] == mode]
        reasons = collections.Counter(
            reason
            for row in selected
            if row["trip_id_scheduled"] is None
            for reason in row.get("unmatched_reasons", ["UNKNOWN"])
        )
        modes[mode] = {
            "matched_count": sum(
                row["trip_id_scheduled"] is not None for row in selected
            ),
            "unmatched_count": sum(
                row["trip_id_scheduled"] is None for row in selected
            ),
            "unmatched_reasons": dict(reasons),
            "reason_counts_are_nonexclusive": True,
            "tides_path": reports[mode]["path"],
            "tides_validation_passed": reports[mode]["validation"].get("valid", False),
            "table_row_counts": reports[mode]["summary"]["rows"],
            "tides_omission_counts": reports[mode]["summary"]["omission_counts"],
        }
    summary = {
        "service_date": manifest["service_date"],
        "route_id": manifest["route_id"],
        "match_modes": manifest["match_modes"],
        "pipeline_version": manifest["pipeline_version"],
        "performed_trip_count": manifest["stage_row_counts"]["segment_trips"][
            "performed_trips"
        ],
        "source_counts": manifest["stage_row_counts"]["load_raw"],
        "stage_row_counts": manifest["stage_row_counts"],
        "modes": modes,
        "validation_ok": True,
        "output_locations": {mode: f"tides/{mode}" for mode in manifest["match_modes"]},
        "interpretation": (
            "Experimental model assignments, not independently calibrated ground truth."
        ),
    }
    write_json(folder / "summary.json", summary)


def run_pipeline(
    service_date,
    route_id="29",
    match_modes=None,
    force=False,
    root=ROOT,
    config_path=DEFAULT_CONFIG,
    output_dir=None,
    progress=None,
):
    run = initialize_run(
        service_date, route_id, match_modes, force, root, config_path, output_dir
    )
    if run["cached"]:
        return run
    for stage in STAGES:
        run_stage(stage, run["run_dir"], root, progress)
    return {
        "run_dir": run["run_dir"],
        "run_manifest": verify_completed(run["run_dir"]),
        "summary": read_json(Path(run["run_dir"]) / "summary.json"),
        "cached": False,
    }
