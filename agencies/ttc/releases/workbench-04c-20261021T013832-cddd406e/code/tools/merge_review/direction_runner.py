"""Append-only experiment 02 orchestration; no APC–AVL observation matcher."""

from __future__ import annotations

import collections
import copy
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from tools.merge_review import avl_dedup, direction_trips, vehicle_stream


def create_run(workspace, config, label, parent_run_id, progress=None):
    # Imported here to keep the runner/code-fingerprint dependency acyclic.
    from tools.merge_review.debug import (
        VERSION,
        _case,
        _digest,
        _directory,
        _json,
        _parquet,
        _rows,
        _save,
        _write,
    )

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

    emit("preparing_direction_day_fixture")
    code = workspace._code_hashes()
    if parent_run_id is not None:
        parent = workspace.get_run(parent_run_id)
        parent_manifest = _json(parent / "manifest.json")
        if parent_manifest.get("debug_date") != workspace.date or parent_manifest.get(
            "scope"
        ) != {"start": workspace.start.isoformat(), "end": workspace.end.isoformat()}:
            raise ValueError("Parent experiment must use the same local calendar day")
        fixture = Path(parent_manifest["audit"])
        if not fixture.is_absolute():
            fixture = (parent / fixture).resolve()
        fixture_manifest = _json(fixture / "manifest.json")
        workspace._fixture_valid(fixture, fixture_manifest)
        if (
            workspace._inputs()[0] != fixture_manifest["input_sha256"]
            or parent_manifest.get("canonical_input_sha256")
            != fixture_manifest["fixture_sha256"]
        ):
            raise ValueError("Parent day fixture or raw input identity changed")
    else:
        fixture = workspace.ensure_fixture()
        fixture_manifest = _json(fixture / "manifest.json")
    frames = {s: pl.read_parquet(fixture / f"{s}.parquet") for s in ("avl", "apc")}
    emit("verifying_exact_original_avl_fields")
    raw_fields, dedup_definition = avl_dedup.load_raw_avl_fields(
        workspace.audit, frames["avl"]
    )
    groups = raw_fields.group_by(list(avl_dedup.RAW_FIELDS)).len()
    repeated = groups.filter(pl.col("len") > 1)
    removed_duplicates = frames["avl"].height - groups.height
    # The normalized route is used only for this explicit filter. No substring
    # matching, APC Branch test or Destination-null proxy is allowed.
    unique_raw = raw_fields.unique(subset=list(avl_dedup.RAW_FIELDS), keep="first")
    pi_po = unique_raw.filter(
        pl.col("Route").str.strip_chars().str.to_uppercase().is_in(["PI", "PO"])
    )
    retained_counts = {"avl": groups.height - pi_po.height, "apc": frames["apc"].height}
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
    vehicles = sorted(set(frames["avl"]["vehicle"]) | set(frames["apc"]["vehicle"]))
    selection = [{"vehicle_case": _case(v), "raw_vehicle": v} for v in vehicles]
    _write(folder / "selection.json", selection)
    _write(folder / "config.json", config)
    raw_path = folder / "deduplication" / "raw_avl_fields.parquet"
    _parquet(raw_path, raw_fields)
    dedup_definition.update(
        raw_fields_path="deduplication/raw_avl_fields.parquet",
        raw_fields_sha256=avl_dedup.file_sha256(raw_path),
    )
    manifest = {
        "version": VERSION,
        "run_id": run_id,
        "debug_date": workspace.date,
        "scope": fixture_manifest["scope"],
        "created_at": created,
        "audit": str(fixture),
        "baseline": str(folder),
        "methods": [direction_trips.METHOD],
        "segmentation_methods": [direction_trips.METHOD],
        "representation": direction_trips.REPRESENTATION,
        "trip_segmentation_used": True,
        "observation_matching_used": False,
        "observation_ordering": vehicle_stream.ORDERING,
        "direction_segmentation": copy.deepcopy(direction_trips.DEFINITION),
        "code_sha256": code,
        "canonical_input_sha256": fixture_manifest["fixture_sha256"],
        "fixture_identity": fixture_manifest["identity"],
        "fixture_origin_run_id": parent_run_id,
        "parent_run_id": parent_run_id,
        "source_counts": fixture_manifest["source_counts"],
        "retained_counts": retained_counts,
        "deduplicated_counts": {"avl": groups.height, "apc": frames["apc"].height},
        "removed_avl": removed_duplicates,
        "filtered_avl_pi_po": pi_po.height,
        "filter_counts": dict(pi_po.group_by("Route").len().iter_rows()),
        "avl_deduplication": True,
        "deduplication": dedup_definition,
        "filtering": {
            "source": "avl",
            "field": "route",
            "values": ["PI", "PO"],
            "normalization": "strip whitespace, uppercase, exact equality",
            "after_exact_deduplication": True,
            "apc_filtered": False,
            "audit_path": "direction/{vehicle_case}.json",
        },
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
    violations, examined, totals = (
        collections.Counter(),
        collections.Counter(),
        collections.Counter(),
    )
    direction_counts = collections.Counter()
    parent_hashes = {}
    if parent_run_id is not None:
        parent = workspace.get_run(parent_run_id)
        parent_hashes = {
            name: avl_dedup.file_sha256(parent / name)
            for name in (
                "manifest.json",
                "summary.json",
                "config.json",
                "completion.json",
            )
        }
        if (
            _json(parent / "manifest.json").get("canonical_input_sha256")
            != manifest["canonical_input_sha256"]
        ):
            raise ValueError(
                "Parent experiment must use the same canonical day fixture"
            )
    try:
        for index, item in enumerate(selection, 1):
            vehicle, case = item["raw_vehicle"], item["vehicle_case"]
            avl, apc = (
                frames[s].filter(pl.col("vehicle") == vehicle).to_dicts()
                for s in ("avl", "apc")
            )
            before = _digest({"avl": avl, "apc": apc})
            raw_rows = raw_fields.filter(pl.col("vehicle") == vehicle).to_dicts()
            retained, mapping = avl_dedup.deduplicate_avl(avl, raw_rows)
            check = avl_dedup.validate_avl_deduplication(
                avl, raw_rows, retained, mapping
            )
            violations.update({"dedup_" + k: v for k, v in check["violations"].items()})
            result = direction_trips.build_direction_trips(retained, apc)
            check = direction_trips.validate_direction_trips(
                retained, apc, result, workspace.start, workspace.end
            )
            violations.update(check["violations"])
            examined.update(check["examined"])
            violations["source_mutation"] += before != _digest({"avl": avl, "apc": apc})
            if any(violations.values()):
                raise ValueError(
                    "Direction segmentation source/evidence validation failed"
                )
            observation_path = folder / "observations" / f"{case}.parquet"
            if result["observations"]:
                _save(
                    observation_path,
                    result["observations"],
                    dict(frames["avl"].schema) | dict(frames["apc"].schema),
                )
            else:
                _parquet(
                    observation_path,
                    pl.DataFrame(schema={"source": pl.String, "rid": pl.String}),
                )
            metadata = {
                key: value for key, value in result.items() if key != "observations"
            }
            metadata_path = folder / "direction" / f"{case}.json"
            _write(metadata_path, metadata)
            mapping_path = folder / "deduplication" / f"{case}.parquet"
            if mapping:
                _save(mapping_path, mapping, avl_dedup.MAPPING_SCHEMA)
            else:
                _parquet(mapping_path, pl.DataFrame(schema=avl_dedup.MAPPING_SCHEMA))
            persisted = direction_trips.restore_metadata(_json(metadata_path))
            persisted["observations"] = _rows(observation_path)
            check = direction_trips.validate_direction_trips(
                retained, apc, persisted, workspace.start, workspace.end
            )
            violations.update(
                {"persisted_" + k: v for k, v in check["violations"].items()}
            )
            check = avl_dedup.validate_avl_deduplication(
                avl, raw_rows, retained, _rows(mapping_path)
            )
            violations.update(
                {"persisted_dedup_" + k: v for k, v in check["violations"].items()}
            )
            if any(violations.values()):
                raise ValueError("Persisted direction segmentation validation failed")
            totals["observations"] += len(result["observations"])
            totals["segments"] += len(result["segments"])
            totals["conflicts"] += len(result["conflicts"])
            totals["filtered_avl_pi_po"] += len(result["filtered_avl"])
            totals["vehicles_with_segments"] += bool(result["segments"])
            for key in (
                "conflict_duration_s",
                "unknown_duration_s",
                "unresolved_observations",
                "candidate_boundary_overlap_count",
            ):
                totals[key] += result.get("summary", {}).get(key, 0)
            totals["conflict_observations"] += sum(
                bool(r.get("direction_conflict_ids")) for r in result["observations"]
            )
            totals["conflict_apc"] += sum(
                r["source"] == "apc" and bool(r.get("direction_conflict_ids"))
                for r in result["observations"]
            )
            totals["conflict_avl"] += sum(
                r["source"] == "avl" and bool(r.get("direction_conflict_ids"))
                for r in result["observations"]
            )
            direction_counts.update(
                result.get("summary", {}).get("direction_counts", {})
            )
            emit(
                "segmenting_direction_conflicts", index, len(selection), vehicle=vehicle
            )
        workspace._fixture_valid(fixture, fixture_manifest)
        violations["global_observation_count"] = abs(
            totals["observations"] - sum(retained_counts.values())
        )
        violations["global_pi_po_count"] = abs(
            totals["filtered_avl_pi_po"] - pi_po.height
        )
        violations["raw_csv_changed"] = (
            avl_dedup.file_sha256(dedup_definition["raw_csv_path"])
            != dedup_definition["raw_csv_sha256"]
        )
        violations["raw_sidecar_changed"] = (
            avl_dedup.file_sha256(raw_path) != dedup_definition["raw_fields_sha256"]
        )
        if parent_run_id is not None:
            violations["parent_run_changed"] = sum(
                avl_dedup.file_sha256(parent / name) != digest
                for name, digest in parent_hashes.items()
            )
        if (
            workspace._code_hashes() != code
            or workspace._inputs()[0] != fixture_manifest["input_sha256"]
        ):
            raise ValueError("Algorithm source or raw input changed during experiment")
        if any(violations.values()):
            raise ValueError("Direction experiment global validation failed")
        summary = {
            **dict(totals),
            "source_counts": manifest["source_counts"],
            "conflict_windows": totals["conflicts"],
            "direction_counts": dict(direction_counts),
            "retained_counts": retained_counts,
            "deduplicated_counts": manifest["deduplicated_counts"],
            "removed_avl": removed_duplicates,
            "filter_counts": manifest["filter_counts"],
            "exact_duplicate_groups": repeated.height,
            "duplicate_involving_rows": int(repeated["len"].sum() or 0),
            "vehicles": len(selection),
            "representation": direction_trips.REPRESENTATION,
            "status_counts": {"not_evaluated": frames["apc"].height},
            "trip_segmentation_used": True,
            "observation_matching_used": False,
            "avl_deduplication": True,
            "validation_ok": True,
            "violations": dict(violations),
            "examined": dict(examined),
            "accuracy": None,
            "publishable": False,
            "elapsed_s": round(time.perf_counter() - started, 3),
            "validation_scope": (
                "Exact original AVL deduplication, explicit PI/PO audit, source/field "
                "conservation, temporal direction evidence and candidate membership; "
                "checked in memory and after persistence. Candidate trips are not "
                "independently verified. No observation matcher, clock correction, "
                "GTFS or passenger aggregation."
            ),
        }
        _write(folder / "summary.json", summary)
        record = dict(
            run_id=run_id,
            label=label,
            parent_run_id=parent_run_id,
            date=workspace.date,
            method=direction_trips.METHOD,
            representation=direction_trips.REPRESENTATION,
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
                publishable=False,
            ),
        )
        emit("complete", len(selection), len(selection))
        return record
    except Exception as exc:
        _write(
            folder / "failure.json",
            dict(error=str(exc), violations=dict(violations), publishable=False),
        )
        raise


def compare_runs(base, current, labels, vehicle, bm, cm):
    """Compare retained source identities, not fabricated matching accuracy."""
    from tools.merge_review.debug import _json, _rows

    summary = collections.Counter()
    counts = {"base": collections.Counter(), "current": collections.Counter()}
    changes = []
    apc_unchanged = True
    fields_unchanged = True
    for case, label in sorted(labels.items()):
        sides, filtered = [], []
        for name, folder in (("base", base), ("current", current)):
            rows = _rows(folder / "observations" / f"{case}.parquet")
            sides.append({(r["source"], r["rid"]): r for r in rows})
            counts[name].update(r["source"] for r in rows)
            meta_path = folder / "direction" / f"{case}.json"
            metadata = _json(meta_path) if meta_path.is_file() else {}
            filtered.append({r["rid"] for r in metadata.get("filtered_avl", [])})
            summary[name + "_segments"] += len(metadata.get("segments", []))
            summary[name + "_conflicts"] += len(metadata.get("conflicts", []))
            summary[name + "_conflict_observations"] += sum(
                bool(r.get("direction_conflict_ids")) for r in rows
            )
        left, right = sides
        for key in sorted(left.keys() | right.keys()):
            source, rid = key
            if key not in right:
                explained = source == "avl" and rid in filtered[1]
                summary[
                    "avl_removed_by_pi_po_filter" if explained else "unexpected_removed"
                ] += 1
                apc_unchanged &= source != "apc"
                changes.append(
                    dict(
                        vehicle=case,
                        vehicle_label=label,
                        source=source,
                        rid=rid,
                        change="PI_PO_FILTER" if explained else "REMOVED",
                    )
                )
            elif key not in left:
                explained = source == "avl" and rid in filtered[0]
                summary[
                    "avl_restored_from_pi_po_filter"
                    if explained
                    else "unexpected_added"
                ] += 1
                apc_unchanged &= source != "apc"
                changes.append(
                    dict(
                        vehicle=case,
                        vehicle_label=label,
                        source=source,
                        rid=rid,
                        change="PI_PO_RESTORED" if explained else "ADDED",
                    )
                )
            else:
                # Original source fields only. Candidate annotations and stream
                # ordinals are allowed to differ between representations.
                ignored = vehicle_stream.DERIVED_FIELDS | set(
                    direction_trips.DERIVED_FIELDS
                )
                lraw = {
                    k: v
                    for k, v in left[key].items()
                    if k not in ignored and v is not None
                }
                rraw = {
                    k: v
                    for k, v in right[key].items()
                    if k not in ignored and v is not None
                }
                equal = vehicle_stream._same(lraw, rraw)
                summary["original_fields_changed"] += not equal
                fields_unchanged &= equal
                if source == "apc":
                    apc_unchanged &= equal
    for key in (
        "avl_removed_by_pi_po_filter",
        "avl_restored_from_pi_po_filter",
        "unexpected_removed",
        "unexpected_added",
        "original_fields_changed",
    ):
        summary.setdefault(key, 0)
    compatible = bm.get("canonical_input_sha256") == cm.get("canonical_input_sha256")
    return {
        "base_run_id": base.name,
        "run_id": current.name,
        "vehicle": vehicle,
        "comparison_status": "direction_segmentation_comparison",
        "comparable": True,
        "representation": cm.get("representation"),
        "base_representation": bm.get("representation"),
        "summary": {
            **dict(summary),
            "apc_unchanged": apc_unchanged,
            "source_lineage_conserved": compatible
            and fields_unchanged
            and not summary["unexpected_removed"]
            and not summary["unexpected_added"],
        },
        "retained_counts": {k: dict(v) for k, v in counts.items()},
        "source_compatible": compatible,
        "changes": changes,
        "boundaries": {"added": [], "removed": [], "changed": []},
        "accuracy": None,
        "publishable": False,
        "note": (
            "Source conservation and explicit filter differences only. Candidate "
            "counts are not confirmed trips or matching gains; overlapping APC can "
            "belong to multiple review candidates without duplicating stored "
            "observations."
        ),
    }
