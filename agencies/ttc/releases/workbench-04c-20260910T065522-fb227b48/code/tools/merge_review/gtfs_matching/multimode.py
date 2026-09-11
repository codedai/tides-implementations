"""Append-only matching experiments over pinned existing projections.

By default global is copied verbatim and the three partial modes are evaluated.
An explicit scoring override recomputes all four modes, including global; raw
data, segmentation and projection remain unchanged in both workflows.
"""

from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import json
import multiprocessing
import shutil
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from . import METHOD, REPRESENTATION
from .feed import file_sha256
from .redecide import checked_path
from .runner import delay_clock_violations, mode_summaries, read_case, write_case

MODES = ("global", "early", "middle", "late")
CONTINUOUS_SCORING_OVERRIDE_KEYS = frozenset(
    {
        "continuous_absolute_delay_weight",
        "continuous_delay_dynamics_weight",
        "continuous_occurrence_displacement_weight",
        "continuous_match_cost_max",
        "continuous_local_support_cost_max",
        "continuous_calibration_max_score",
        "continuous_boundary_penalty",
        "continuous_boundary_gain_min",
        "continuous_local_missing_cost",
        "continuous_apc_support_cost_max",
        "sequence_local_score_weight",
    }
)
NUMERIC_SCORING_OVERRIDE_KEYS = CONTINUOUS_SCORING_OVERRIDE_KEYS | {
    "score_delay_prior_weight"
}
BOOLEAN_SCORING_OVERRIDE_KEYS = {
    "continuous_matching_enabled",
    "continuous_occurrence_model_enabled",
}
SCORING_OVERRIDE_KEYS = NUMERIC_SCORING_OVERRIDE_KEYS | BOOLEAN_SCORING_OVERRIDE_KEYS
_CONTEXT = None


def validate_modes(projection, global_result, modes, config=None):
    """Publication gates for scope isolation, source identity and original clocks."""
    from .partial import validate_matching_modes
    from .windows import observation_key

    validate_matching_modes(projection, modes, config)

    if set(modes) != set(MODES) or modes["global"]["result"] != global_result:
        raise ValueError("Four-mode experiment changed the supplied global result")
    all_avl = {observation_key(row) for row in projection.get("points", [])}
    all_apc = {observation_key(row) for row in projection.get("apc_evidence", [])}
    assigned = set()
    for mode, entry in modes.items():
        result, selection = entry["result"], entry["selection"]
        selected = set(selection.get("selected_avl_ids", []))
        selected_apc = set(selection.get("selected_apc_ids", []))
        if not selected <= all_avl or not selected_apc <= all_apc:
            raise ValueError("Decision slice fabricated a source observation")
        if mode != "global":
            if selected & assigned:
                raise ValueError("Partial decisions overlap in AVL source ownership")
            assigned |= selected
            permitted = selected | selected_apc
            bundle = result.get("windows", {})
            for window in bundle.get("windows", []):
                if not set(window.get("observation_ids", [])) <= permitted:
                    raise ValueError("Outside-third evidence leaked into a window")
            for candidate in result.get("all_candidates", []):
                for row in candidate.get("whole_alignment", {}).get(
                    "delay_process", []
                ):
                    if row.get("source_id") not in selected:
                        raise ValueError(
                            "Outside-third evidence leaked into delay fitting"
                        )
        if len(result.get("candidates", [])) > 3:
            raise ValueError("Partial decision exceeded the final candidate cap")
        if any(
            c.get("probability") is not None for c in result.get("candidates", [])
        ) and not (
            result.get("scorer") == "continuous_passage_v1"
            and result.get("calibration", {}).get("calibrated") is True
        ):
            raise ValueError(
                "Uncalibrated partial decision must not publish probabilities"
            )
        if delay_clock_violations(result, projection.get("points", [])):
            raise ValueError("Partial decision altered or reversed a source clock")


def _initialize(canonical, config, recompute_global=False):
    global _CONTEXT
    _CONTEXT = canonical, config, recompute_global


def _case(source_path, target_path, summary):
    from .continuous.serialization import shadow_row
    from .decision import apply_decision_guard
    from .integration import match_candidate_segment
    from .overview import overview_case
    from .partial import evaluate_matching_modes

    canonical, config, recompute_global = _CONTEXT
    original = read_case(Path(source_path))
    evidence_fields = ("projection", "apc_evidence", "parent_boundary")
    unchanged_fields = evidence_fields + (() if recompute_global else ("result",))

    def signature(artifact, fields=unchanged_fields):
        return hashlib.sha256(
            json.dumps(
                {field: artifact.get(field) for field in fields},
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()

    original_signature = signature(original)
    complete_source_signature = signature(original, tuple(original))
    global_result = original["result"]
    if recompute_global:
        global_result = apply_decision_guard(
            match_candidate_segment(
                original["projection"],
                original["projection"].get("apc_evidence", []),
                canonical["references"],
                canonical["trips"],
                config,
                service_date=None,
                route_id=canonical["route_id"],
            ),
            config,
        )
    supplied_global_signature = signature({"result": global_result}, ("result",))
    modes = evaluate_matching_modes(
        original["projection"], canonical, config, global_result=global_result
    )
    validate_modes(original["projection"], global_result, modes, config)
    if signature({"result": global_result}, ("result",)) != supplied_global_signature:
        raise ValueError("Partial evaluation mutated the supplied global result")
    if signature(original, tuple(original)) != complete_source_signature:
        raise ValueError("Partial evaluation mutated upstream evidence in place")
    updated = {
        **original,
        "result": global_result,
        "matching_modes": modes,
        "config": config,
    }
    write_case(Path(target_path), updated)
    persisted = read_case(Path(target_path))
    if persisted != updated:
        raise ValueError("Persisted partial matching result differs from evaluation")
    if signature(persisted) != original_signature:
        raise ValueError("Partial matching replay changed original evidence")
    return {
        "case_id": original["case_id"],
        "matching_modes": mode_summaries(modes),
        "sha256": file_sha256(Path(target_path)),
        "overview": {
            mode: overview_case(
                updated,
                summary,
                summary["vehicle"],
                summary["vehicle_label"],
                canonical,
                mode,
            )
            for mode in MODES
        },
        "shadow": {
            mode: {
                "old": shadow_row(
                    original.get("matching_modes", {})
                    .get(mode, {})
                    .get("result", original["result"]),
                    summary,
                    canonical,
                    mode,
                ),
                "new": shadow_row(modes[mode]["result"], summary, canonical, mode),
            }
            for mode in MODES
        }
        if config.get("continuous_matching_enabled")
        else {},
    }


def create_multimode_run(
    workspace,
    source_run_id,
    label,
    progress=None,
    workers=8,
    *,
    scoring_overrides=None,
):
    """Reuse pinned projections; explicit scoring overrides rerun all four modes.

    Only the supported scoring parameters may change. No default configuration
    migration, projection, raw observation rewrite, or source-run mutation occurs.
    Passing the existing weight explicitly is a full-matching control replay.
    """
    from tools.merge_review.debug import _json, _write

    from .adapter import GTFSReviewData
    from .config import defaults, validate
    from .overview import build_day_overview

    if (
        isinstance(workers, bool)
        or not isinstance(workers, int)
        or not 1 <= workers <= 8
    ):
        raise ValueError("workers must be an integer between 1 and 8")
    if scoring_overrides is not None and (
        not isinstance(scoring_overrides, dict)
        or not scoring_overrides
        or set(scoring_overrides) - SCORING_OVERRIDE_KEYS
    ):
        raise ValueError("Only explicit supported scoring overrides are permitted")
    overrides = {}
    if scoring_overrides is not None:
        # Validate numeric bounds now, but validate cross-parameter constraints
        # only after merging with the pinned source configuration below.
        numeric = {
            key: value
            for key, value in scoring_overrides.items()
            if key not in BOOLEAN_SCORING_OVERRIDE_KEYS
        }
        validated = validate(numeric)
        overrides = {key: validated[key] for key in numeric}
        for key in BOOLEAN_SCORING_OVERRIDE_KEYS & scoring_overrides.keys():
            enabled = scoring_overrides[key]
            if not isinstance(enabled, bool):
                raise ValueError(f"{key} must be boolean")
            overrides[key] = enabled
    recompute_global = bool(overrides)
    started = time.perf_counter()
    source = workspace.get_run(source_run_id)
    manifest = _json(source / "manifest.json")
    if manifest.get("representation") != REPRESENTATION or manifest.get(
        "route_scope"
    ) != ["29"]:
        raise ValueError("Partial matching requires a completed route-29 GTFS run")
    if manifest.get("debug_date") != workspace.date:
        raise ValueError("Partial matching must retain the original debug date")
    GTFSReviewData(source)
    pinned = _json(source / "output_sha256.json")
    pinned.update(
        {
            name: file_sha256(source / name)
            for name in (
                "output_sha256.json",
                "completion.json",
                "run.json",
            )
        }
    )
    for relative, digest in pinned.items():
        if file_sha256(checked_path(source, relative)) != digest:
            raise ValueError(
                "Completed source evidence changed before partial matching"
            )
    config = _json(source / "config.json")
    source_defaults = defaults()
    source_scoring_values = {
        key: config["gtfs"].get(key, source_defaults[key]) for key in overrides
    }
    continuous_enabled = overrides.get(
        "continuous_matching_enabled",
        config["gtfs"].get("continuous_matching_enabled", False),
    )
    if not continuous_enabled and overrides.keys() & CONTINUOUS_SCORING_OVERRIDE_KEYS:
        raise ValueError("Continuous scoring overrides require the continuous scorer")
    if overrides.get("continuous_occurrence_model_enabled") and not continuous_enabled:
        raise ValueError("Occurrence matching requires the continuous scorer")
    if continuous_enabled:
        from .continuous.config import defaults as continuous_defaults
        from .continuous.config import get_config

        config["gtfs"] = continuous_defaults() | config["gtfs"]
    config["gtfs"]["matching_partial_modes_enabled"] = True
    config["gtfs"].update(overrides)
    if continuous_enabled:
        # Check interactions against the source configuration, not just defaults.
        get_config(config["gtfs"])
    code_paths = {
        name: Path(__file__).with_name(name + ".py")
        for name in (
            "partial",
            "multimode",
            "integration",
            "scoring",
            "windows",
            "retrieval",
            "decision",
            "calibration",
            "overview",
            "diagnostic_candidates",
            "runner",
            "config",
        )
    }
    if continuous_enabled:
        code_paths.update(
            {
                "continuous/" + path.stem: path
                for path in Path(__file__).parent.joinpath("continuous").glob("*.py")
            }
        )
    code = {name: file_sha256(path) for name, path in code_paths.items()}
    canonical = _json(source / "gtfs/canonical.json")
    created = datetime.now(UTC)
    run_id = (
        "run-"
        + workspace.date.replace("-", "")
        + "-"
        + created.strftime("%Y%m%dT%H%M%S")
        + "-"
        + uuid.uuid4().hex[:12]
    )
    folder = workspace.root / "runs" / run_id
    folder.mkdir(mode=0o700)
    outputs, entries = {}, {}
    overview_records = collections.defaultdict(list)
    statuses = {mode: collections.Counter() for mode in MODES}
    shadow_rows = {mode: {"old": [], "new": []} for mode in MODES}
    executor = None
    try:
        for relative in pinned:
            if relative.startswith(
                ("cases/", "catalog/", "overview/", "shadow/")
            ) or relative in {
                "manifest.json",
                "config.json",
                "summary.json",
                "completion.json",
                "run.json",
                "output_sha256.json",
            }:
                continue
            target = checked_path(folder, relative)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(checked_path(source, relative), target)
            target.chmod(0o600)
            outputs[relative] = file_sha256(target)
            if outputs[relative] != pinned[relative]:
                raise ValueError("Copied input changed during partial matching")
        catalogs = {
            relative: _json(source / relative)
            for relative in sorted(pinned)
            if relative.startswith("catalog/")
        }
        by_id = {row["id"]: row for rows in catalogs.values() for row in rows}
        from .runner import artifact_key

        jobs = [
            (f"cases/{artifact_key(case_id)}.json.gz", row)
            for case_id, row in by_id.items()
        ]
        if {relative for relative, _ in jobs} != {
            name for name in pinned if name.startswith("cases/")
        }:
            raise ValueError("Case files do not match the pinned catalog")
        if workers > 1:
            executor = ProcessPoolExecutor(
                max_workers=workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_initialize,
                initargs=(canonical, config["gtfs"], recompute_global),
            )
            pending = {
                executor.submit(
                    _case, str(source / relative), str(folder / relative), row
                ): relative
                for relative, row in jobs
            }
            results = ((pending[f], f.result()) for f in as_completed(pending))
        else:
            _initialize(canonical, config["gtfs"], recompute_global)
            results = (
                (relative, _case(str(source / relative), str(folder / relative), row))
                for relative, row in jobs
            )
        for index, (relative, entry) in enumerate(results, 1):
            outputs[relative] = entry["sha256"]
            entries[entry["case_id"]] = entry["matching_modes"]
            for mode in MODES:
                statuses[mode][entry["matching_modes"][mode]["gtfs_status"]] += 1
                overview_records[mode].append(entry["overview"][mode])
                if entry.get("shadow"):
                    for side in ("old", "new"):
                        shadow_rows[mode][side].append(entry["shadow"][mode][side])
            if progress:
                progress(
                    dict(
                        stage=(
                            "four_mode_scoring_replay"
                            if recompute_global
                            else "partial_matching"
                        ),
                        completed=index,
                        total=len(jobs),
                        case_id=entry["case_id"],
                        run_id=run_id,
                        elapsed_s=round(time.perf_counter() - started, 3),
                    )
                )
        for relative, rows in catalogs.items():
            for row in rows:
                row["matching_modes"] = entries[row["id"]]
                global_summary = entries[row["id"]]["global"]
                row.update(
                    {
                        key: copy.deepcopy(global_summary[key])
                        for key in (
                            "gtfs_status",
                            "gtfs_candidate_count",
                            "pattern_confidence",
                            "trip_confidence",
                        )
                    }
                )
            _write(folder / relative, rows)
            outputs[relative] = file_sha256(folder / relative)
        for mode in MODES:
            relative = f"overview/{mode}.json"
            _write(
                folder / relative,
                build_day_overview(
                    sorted(
                        overview_records[mode], key=lambda row: row.get("case_id", "")
                    ),
                    canonical,
                    run_id=run_id,
                    day=workspace.date,
                    matching_mode=mode,
                    available_matching_modes=list(MODES),
                ),
            )
            outputs[relative] = file_sha256(folder / relative)
        if continuous_enabled:
            from .continuous.evaluation import evaluate_shadow
            from .continuous.sequence import decode_vehicle_day

            for mode in MODES:
                for side in ("old", "new"):
                    shadow_rows[mode][side].sort(key=lambda row: row["case_id"])
                for relative, value in (
                    (f"shadow/{mode}-episodes.json", shadow_rows[mode]),
                    (
                        f"shadow/{mode}-report.json",
                        evaluate_shadow(
                            **{
                                side + "_rows": rows
                                for side, rows in shadow_rows[mode].items()
                            }
                        ),
                    ),
                    (
                        f"shadow/{mode}-vehicle-sequence.json",
                        decode_vehicle_day(
                            shadow_rows[mode]["new"],
                            dict(config["gtfs"], sequence_enabled=True),
                        ),
                    ),
                ):
                    _write(folder / relative, value)
                    outputs[relative] = file_sha256(folder / relative)
        if any(
            file_sha256(checked_path(source, name)) != digest
            for name, digest in pinned.items()
        ):
            raise ValueError("Pinned source changed during partial matching")
        if any(
            file_sha256(code_paths[name]) != digest for name, digest in code.items()
        ):
            raise ValueError("Partial matching code changed during computation")
        GTFSReviewData(source)._verify_parent()
        new_manifest = copy.deepcopy(manifest)
        new_manifest.update(
            run_id=run_id,
            baseline=str(folder),
            created_at=created.isoformat(),
            parent_run_id=source.name,
            available_matching_modes=list(MODES),
            partial_matching_replay=True,
            matching_scoring_replay=recompute_global,
            continuous_matching_replay=bool(continuous_enabled),
            scoring_overrides=overrides,
            source_scoring_values=source_scoring_values,
            partial_source_run_id=source.name,
            partial_source_path=str(source),
            partial_source_sha256=pinned,
            partial_code_sha256=code,
            stage_provenance={
                "upstream_code_sha256": manifest["code_sha256"],
                "upstream_evidence_run_id": source.name,
                "upstream_stage_provenance": manifest.get("stage_provenance"),
                "global_result_reused_unchanged": not recompute_global,
                "projection_reused_unchanged": True,
                "raw_observations_reused_unchanged": True,
                "segmentation_reused_unchanged": True,
                "canonical_reused_unchanged": True,
                "matching_modes_recomputed": list(
                    MODES if recompute_global else MODES[1:]
                ),
                "recomputed_stages": (
                    [
                        "global_windows",
                        "global_retrieval",
                        "global_scoring",
                        "global_integration",
                        "global_final_decision",
                    ]
                    if recompute_global
                    else []
                )
                + [
                    "partial_selection",
                    "partial_windows",
                    "partial_retrieval",
                    "partial_scoring",
                    "partial_integration",
                    "partial_final_decision",
                ],
                "online_causal_decision": False,
            },
        )
        summary = _json(source / "summary.json")
        summary.update(
            status_counts=dict(statuses["global"]),
            matching_mode_status_counts={
                mode: dict(counts) for mode, counts in statuses.items()
            },
            available_matching_modes=list(MODES),
            partial_matching_replay=True,
            matching_scoring_replay=recompute_global,
            scoring_overrides=overrides,
            source_scoring_values=source_scoring_values,
            upstream_processing_elapsed_s=summary["elapsed_s"],
            elapsed_s=round(time.perf_counter() - started, 3),
            probability_calibrated=False,
            scorer="continuous_passage_v1" if continuous_enabled else "legacy_dtw",
            shadow_evaluation_reports=[f"shadow/{mode}-report.json" for mode in MODES]
            if continuous_enabled
            else [],
            accuracy=None,
            publishable=False,
            validation_scope=(
                (
                    "Pinned raw observations, segmentation and projection preserved; "
                    "all four matching modes recomputed with explicit scoring "
                    "overrides; "
                )
                if recompute_global
                else "Pinned projection/global result preserved; "
            )
            + ("isolated partial evidence and original clocks; not matching accuracy."),
        )
        summary.setdefault("violations", {}).update(
            partial_source_changed=0,
            projection_changed=0,
            partial_scope_leakage=0,
            partial_clock_violations=0,
        )
        if recompute_global:
            summary["violations"].pop("global_result_changed", None)
            summary["violations"]["unexpected_global_result_change"] = 0
        else:
            summary["violations"]["global_result_changed"] = 0
        for relative, value in (
            ("manifest.json", new_manifest),
            ("config.json", config),
            ("summary.json", summary),
        ):
            _write(folder / relative, value)
            outputs[relative] = file_sha256(folder / relative)
        _write(folder / "output_sha256.json", outputs)
        record = dict(
            run_id=run_id,
            label=label,
            parent_run_id=source.name,
            date=workspace.date,
            method=METHOD,
            representation=REPRESENTATION,
            created_at=created.isoformat(),
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
                available_matching_modes=list(MODES),
            ),
        )
        return record
    except BaseException as exc:
        _write(folder / "failure.json", {"error": str(exc), "publishable": False})
        raise
    finally:
        if executor:
            executor.shutdown(wait=True, cancel_futures=True)


def main():
    from tools.merge_review.debug import DebugWorkspace
    from tools.merge_review.server import DEFAULT_AUDIT, DEFAULT_DEBUG

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--debug-dir", type=Path, default=DEFAULT_DEBUG)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--continuous",
        action="store_true",
        help=(
            "Experiment 04: unsampled continuous-passage scorer; "
            "retain source projections"
        ),
    )
    parser.add_argument(
        "--occurrence-model",
        action="store_true",
        help=(
            "04c: separate operational prior, local occurrence displacement "
            "and retrieval"
        ),
    )
    parser.add_argument(
        "--score-delay-prior-weight",
        type=float,
        help="Recompute all four modes with this absolute-delay cost per hour",
    )
    for key in sorted(CONTINUOUS_SCORING_OVERRIDE_KEYS):
        parser.add_argument(
            "--" + key.replace("_", "-"),
            type=float,
            help=(
                "Recompute all four modes with this continuous-scorer setting; "
                "requires a continuous source run or --continuous"
            ),
        )
    parser.add_argument(
        "--label", default="实验 03c · Route 29 · 全局／前期／中期／后期决策"
    )
    args = parser.parse_args()
    result = create_multimode_run(
        DebugWorkspace(args.debug_dir, args.audit),
        args.source,
        args.label,
        workers=args.workers,
        scoring_overrides=(
            {
                **{
                    key: getattr(args, key)
                    for key in NUMERIC_SCORING_OVERRIDE_KEYS
                    if getattr(args, key) is not None
                },
                **({"continuous_matching_enabled": True} if args.continuous else {}),
                **(
                    {"continuous_occurrence_model_enabled": True}
                    if args.occurrence_model
                    else {}
                ),
            }
            or None
        ),
        progress=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
