"""Replay only the final decision over hash-pinned, completed heavy evidence."""

from __future__ import annotations

import argparse
import collections
import copy
import json
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import METHOD, REPRESENTATION
from .feed import file_sha256
from .runner import artifact_key, delay_clock_violations, read_case, write_case

HEAVY_RESULT_FIELDS = (
    "all_candidates",
    "local_scores",
    "windows",
    "retrieval",
    "whole_trip",
)


def checked_path(root, relative):
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Evidence path escapes its completed run")
    return path


def create_redecision(workspace, source_run_id, label, progress=None):
    """No new schedule/projection/scoring work and no source artifacts changed.

    The historical code hashes continue to identify upstream computation. New
    decision-stage code hashes are separate: a replay never claims that old
    projection or DTW evidence was computed by the new decision implementation.
    """
    from tools.merge_review.debug import _json, _write

    from . import decision
    from .adapter import GTFSReviewData

    started = time.perf_counter()
    source = workspace.get_run(source_run_id)
    old_manifest = _json(source / "manifest.json")
    if old_manifest.get("representation") != REPRESENTATION or old_manifest.get(
        "route_scope"
    ) != ["29"]:
        raise ValueError("Decision replay requires a completed route-29 GTFS run")
    if old_manifest.get("decision_replay"):
        raise ValueError(
            "Replay the original heavy-evidence run, not an earlier decision replay"
        )
    GTFSReviewData(source)  # Verifies the original experiment-02 parent lineage.
    pinned = _json(source / "output_sha256.json")
    pinned.update(
        {
            name: file_sha256(source / name)
            for name in ("output_sha256.json", "completion.json", "run.json")
        }
    )
    for relative, digest in pinned.items():
        if file_sha256(checked_path(source, relative)) != digest:
            raise ValueError("Completed decision source changed")
    code_paths = {
        "decision.py": Path(decision.__file__),
        "redecide.py": Path(__file__),
        "runner.py": Path(__file__).with_name("runner.py"),
        "case_storage.py": Path(__file__).with_name("case_storage.py"),
    }
    code = {name: file_sha256(path) for name, path in code_paths.items()}
    run_id = (
        "run-20231106-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        + "-"
        + uuid.uuid4().hex[:12]
    )
    folder = workspace.root / "runs" / run_id
    folder.mkdir(mode=0o700)
    created = datetime.now(UTC).isoformat()
    outputs, statuses, transitions = {}, collections.Counter(), collections.Counter()
    cases = {}
    try:
        for relative in pinned:
            if relative.startswith(("cases/", "catalog/")) or relative in {
                "manifest.json",
                "summary.json",
                "completion.json",
                "run.json",
                "output_sha256.json",
            }:
                continue
            target = checked_path(folder, relative)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(checked_path(source, relative), target)
            target.chmod(0o600)
            outputs[relative] = file_sha256(target)
            if outputs[relative] != pinned[relative]:
                raise ValueError("Copied heavy evidence differs from source")
        case_files = sorted(name for name in pinned if name.startswith("cases/"))
        for index, relative in enumerate(case_files, 1):
            old = read_case(checked_path(source, relative))
            result = decision.apply_decision_guard(
                copy.deepcopy(old["result"]), copy.deepcopy(old["config"])
            )
            if any(
                result.get(key) != old["result"].get(key) for key in HEAVY_RESULT_FIELDS
            ):
                raise ValueError("Decision guard changed heavy matching evidence")
            if delay_clock_violations(result, old["projection"]["points"]):
                raise ValueError("Replayed evidence contains a nonmonotone delay clock")
            if len(result.get("candidates", [])) > 3:
                raise ValueError("Decision guard exceeded final candidate cap")
            updated = {**old, "result": result}
            write_case(checked_path(folder, relative), updated)
            if read_case(folder / relative) != updated:
                raise ValueError("Persisted decision differs from evaluated result")
            outputs[relative] = file_sha256(folder / relative)
            cases[old["case_id"]] = {
                key: copy.deepcopy(result[key])
                for key in (
                    "status",
                    "candidates",
                    "pattern_confidence",
                    "trip_confidence",
                )
            }
            statuses[result["status"]] += 1
            transitions[f"{old['result']['status']} -> {result['status']}"] += 1
            if progress:
                progress(
                    {
                        "stage": "final_decision_replay",
                        "completed": index,
                        "total": len(case_files),
                        "elapsed_s": round(time.perf_counter() - started, 3),
                    }
                )
        for relative in sorted(name for name in pinned if name.startswith("catalog/")):
            catalog = _json(source / relative)
            for row in catalog:
                result = cases[row["id"]]
                row.update(
                    gtfs_status=result["status"],
                    gtfs_candidate_count=len(result["candidates"]),
                    pattern_confidence=result["pattern_confidence"],
                    trip_confidence=result["trip_confidence"],
                )
                if f"cases/{artifact_key(row['id'])}.json.gz" not in outputs:
                    raise ValueError("Catalog has no redecided case")
            _write(folder / relative, catalog)
            outputs[relative] = file_sha256(folder / relative)
        source_changed = any(
            file_sha256(checked_path(source, name)) != digest
            for name, digest in pinned.items()
        )
        code_changed = any(
            file_sha256(code_paths[name]) != digest for name, digest in code.items()
        )
        if source_changed or code_changed:
            raise ValueError("Decision source or decision code changed during replay")
        GTFSReviewData(source)._verify_parent()
        manifest = copy.deepcopy(old_manifest)
        manifest.update(
            run_id=run_id,
            baseline=str(folder),
            created_at=created,
            parent_run_id=source.name,
            decision_replay=True,
            decision_source_run_id=source.name,
            decision_source_path=str(source),
            decision_source_sha256=pinned,
            decision_code_sha256=code,
            stage_provenance={
                "upstream_code_sha256": old_manifest["code_sha256"],
                "upstream_evidence_run_id": source.name,
                "recomputed_stages": ["final_decision"],
                "heavy_scores_recomputed": False,
            },
        )
        summary = _json(source / "summary.json")
        summary.update(
            status_counts=dict(statuses),
            decision_replay=True,
            decision_transitions=dict(transitions),
            upstream_processing_elapsed_s=summary["elapsed_s"],
            elapsed_s=round(time.perf_counter() - started, 3),
            accuracy=None,
            probability_calibrated=False,
            publishable=False,
            validation_scope=(
                "Pinned upstream evidence and unchanged heavy scores; "
                "final decision replay only, not matching accuracy."
            ),
        )
        summary["violations"].update(
            decision_source_changed=False,
            heavy_evidence_changed=0,
            decision_code_changed=False,
        )
        _write(folder / "manifest.json", manifest)
        _write(folder / "summary.json", summary)
        for name in ("manifest.json", "summary.json"):
            outputs[name] = file_sha256(folder / name)
        _write(folder / "output_sha256.json", outputs)
        record = dict(
            run_id=run_id,
            label=label,
            parent_run_id=source.name,
            date=workspace.date,
            method=METHOD,
            representation=REPRESENTATION,
            created_at=created,
            config=_json(folder / "config.json"),
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
        return record
    except BaseException as exc:
        _write(folder / "failure.json", {"error": str(exc), "publishable": False})
        raise


def main():
    from tools.merge_review.debug import DebugWorkspace
    from tools.merge_review.server import DEFAULT_AUDIT, DEFAULT_DEBUG

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--debug-dir", type=Path, default=DEFAULT_DEBUG)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument(
        "--label",
        default="Experiment 03 · route 29 · final ambiguity-protected version",
    )
    args = parser.parse_args()
    result = create_redecision(
        DebugWorkspace(args.debug_dir, args.audit),
        args.source,
        args.label,
        progress=lambda p: print(json.dumps(p), flush=True),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
