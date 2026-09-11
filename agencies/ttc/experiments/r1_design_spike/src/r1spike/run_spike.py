"""Spike runner CLI (experimental; cannot publish).

Usage (from ``agencies/ttc``):

    PYTHONPATH=experiments/r1_design_spike/src \\
        uv run python -m r1spike.run_spike --slice normal_day --run-id dev

One guarded run directory per (run-id, slice):
``artifacts/experimental-r1-spike-<run-id>-<slice>/``.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import polars as pl
import yaml

from . import ALGORITHM_VERSION
from .artifacts_io import SpikeRun, hash_config_text
from .baselines import b1_b2_agreement, b1_resolve, check_b0_availability
from .candidate_generation import dominant_route_label
from .diagnostics import (
    decision_changes,
    edge_case_inventory,
    fingerprint_checksum,
    metrics_payload,
)
from .inputs import input_tree_hash
from .scoring import contract_hash, load_contract
from .spike_pipeline import (
    generate_candidates,
    peak_rss_mib,
    prepare_slice,
    run_resolution,
    segment_report,
)

SLICE_QUERIES = {
    "normal_day": ("avl_route_day", lambda s: {"date": s["avl_dates"][0]}),
    "dst_fall_back": ("avl_dst_window", lambda s: {"date": s["avl_dates"][-1]}),
    "month_boundary": (
        "apc_after_avl_end",
        lambda s: {"after": s["avl_dates"][0] + " 23:59:59"},
    ),
}

ASSUMPTIONS = [
    "naive America/Toronto wall-time interpretation (G0.3/D-16 open)",
    "candidate GTFS manifest override via --allow-candidate (D-15 open; "
    "no approved feed exists)",
    "source_date_key equals wall calendar date on every fixture row "
    "(verified by S0-S2 metrics) and stands in for date-key agreement",
    "APC spatial path residuals use the dominant shape per (date, route); "
    "branch/pattern-scoped geometry blocked by unconfirmed semantics (D-11)",
    "score-contract numeric parameters are uncalibrated hypotheses",
    "XT-FOLD-A uniform-fold rule is experimental and known to be coarse "
    "for double-traversal vehicles without exact wall-time collisions",
    "X4 segmentation thresholds are experimental",
    "Stage-3 segment weakening simplified: segment demoted only when every "
    "attachment is demoted (recompute-and-rescore deferred)",
    "input integrity fingerprint covers file names+sizes, not full content "
    "re-hash of the normalized parquet tree",
]

UNRESOLVED_DESIGN_CONTRACTS = [
    "R1-B01 objective-function structure (units/normalization/combination)",
    "R1-B02 avl-only vs apc-evidence regime separation",
    "R1-B03 Stage-1 APC date-consistency summary semantics",
    "R1-B04 graph class, staged decomposition, and candidate caps",
    "R1-B05 fixed-point determinism and canonical aggregation",
    "R1-B06 complete candidate tuples with null alternatives and margins",
    "R1-B07 fold-resolution hand-off and rollback",
    "R1-B08 window ownership and execution-order invariance",
    "R1-B09 geometry evidence contract (residuals, states, scoping)",
    "R1-B10 temporal evidence contract (gaps, overlap denominator, stale)",
    "R1-B11 sequence validation with tied demotion sets",
]


def run_one_slice(
    *,
    slice_name: str,
    cfg: dict,
    ttc_root: Path,
    run_id: str,
) -> dict:
    slice_cfg = cfg["slices"][slice_name]
    query, params_fn = SLICE_QUERIES[slice_name]
    normalized_dir = ttc_root / cfg["inputs"]["normalized_dir"]
    gtfs_dirs = {
        d: ttc_root / p for d, p in cfg["inputs"]["gtfs_artifact_dirs"].items()
    }
    contract_path = (
        ttc_root.parent.parent / cfg["score_contract"]["path"]
        if not (ttc_root / cfg["score_contract"]["path"]).exists()
        else ttc_root / cfg["score_contract"]["path"]
    )
    t_start = time.perf_counter()

    prepared = prepare_slice(
        name=slice_name,
        slice_cfg=slice_cfg,
        normalized_dir=normalized_dir,
        gtfs_dirs=gtfs_dirs,
        seg_cfg=cfg["segmentation"],
        time_cfg=cfg["time"],
        vehicle_query=query,
        vehicle_params=params_fn(slice_cfg),
    )
    resolution_variant = cfg["segmentation"].get("resolution_variant", "x4a")
    generate_candidates(
        prepared,
        slice_cfg=slice_cfg,
        geometry_cfg=cfg["geometry"],
        temporal_cfg=cfg["temporal"],
        candidates_cfg=cfg["candidates"],
        resolution_variant=resolution_variant,
    )

    variants = cfg["score_contract"]["variants"]
    primary_variant = cfg["score_contract"]["primary_variant"]
    contracts = {v: load_contract(contract_path, v) for v in variants}
    policy = cfg["stage1"]["policy"]

    def resolve_with(variant: str, pol: str, hours: int, order: str):
        t0 = time.perf_counter()
        res = run_resolution(
            prepared,
            contract=contracts[variant],
            stage1_policy=pol,
            candidates_cfg=cfg["candidates"],
            stage1_cfg=cfg["stage1"],
            slice_cfg=slice_cfg,
            window_hours=hours,
            order=order,
            shuffle_seed=cfg["windows"]["shuffle_seed"],
            resolution_variant=resolution_variant,
        )
        return res, time.perf_counter() - t0

    primary, primary_s = resolve_with(
        primary_variant, policy, cfg["windows"]["window_hours"], "forward"
    )
    prepared.stage_timings_s["resolution_primary"] = primary_s

    # Window-order / window-size / double-run determinism.
    determinism: dict[str, object] = {"primary": fingerprint_checksum(primary)}
    hours_main = cfg["windows"]["window_hours"]
    hours_alt = cfg["windows"]["window_hours_alt"]
    layouts = [
        ("reverse_24h", primary_variant, policy, hours_main, "reverse"),
        ("shuffled_24h", primary_variant, policy, hours_main, "shuffled"),
        ("forward_alt_hours", primary_variant, policy, hours_alt, "forward"),
    ]
    mismatches = []
    for label, v, p, hours, order in layouts:
        res, _ = resolve_with(v, p, hours, order)
        checksum = fingerprint_checksum(res)
        determinism[label] = checksum
        if checksum != determinism["primary"]:
            mismatches.append(
                {"layout": label, "changes": decision_changes(primary, res)}
            )
    rerun, _ = resolve_with(
        primary_variant, policy, cfg["windows"]["window_hours"], "forward"
    )
    determinism["double_run"] = fingerprint_checksum(rerun)
    if determinism["double_run"] != determinism["primary"]:
        mismatches.append(
            {"layout": "double_run", "changes": decision_changes(primary, rerun)}
        )
    determinism["mismatches"] = mismatches
    determinism["identical"] = not mismatches

    # Score-variant sensitivity (rates per variant, plus deltas).
    from .diagnostics import disposition_rates, segment_status_rates

    variant_deltas: dict[str, dict] = {}
    for v in variants:
        if v == primary_variant:
            continue
        res, _ = resolve_with(v, policy, cfg["windows"]["window_hours"], "forward")
        variant_deltas[v] = {
            "changes": decision_changes(primary, res),
            "disposition_rates": disposition_rates(res),
            "segment_status_rates": segment_status_rates(res),
            "sequence_validated_segments": len(res.sequence_results),
            "sequence_demoted_records": sum(
                1
                for status in res.dispositions.values()
                if status == "sequence_demoted"
            ),
            "fold_rollbacks": sorted(res.fold_rollbacks),
        }

    # Stage-1 policy sensitivity.
    policy_deltas: dict[str, dict] = {}
    for pol in ("policy_a", "policy_b", "policy_c"):
        if pol == policy:
            continue
        res, _ = resolve_with(
            primary_variant, pol, cfg["windows"]["window_hours"], "forward"
        )
        policy_deltas[pol] = decision_changes(primary, res)

    # B0 availability, B1 baseline, agreement.
    b0 = check_b0_availability(
        [ttc_root / "database_offline" / "avl", ttc_root / "database_offline" / "apc"]
    )
    from .spike_pipeline import resolution_segments_for

    b_segments = resolution_segments_for(prepared, resolution_variant)
    route_labels = {s.segment_id: dominant_route_label(s)[0] for s in b_segments}
    stop_unique: dict[str, bool] = {}
    for rid, cands in prepared.apc_candidates.items():
        stops = {
            c.stop_mapping_hypothesis
            for c in cands
            if not c.is_null_alternative()
            and c.stop_mapping_hypothesis != "XR1:STOP_UNMAPPED"
        }
        stop_unique[rid] = len(stops) == 1
    b1_decisions = {
        r.record_id: b1_resolve(
            r,
            b_segments,
            max_gap_s=cfg["temporal"]["candidate_prefilter_gap_s"] // 2,
            stop_mapping_unique=stop_unique.get(r.record_id, False),
            route_labels_by_segment=route_labels,
        )
        for r in prepared.apc_records
    }
    b2_selected = {
        rid: (d.selected_key[6] if d.selected_key else None)
        for rid, d in primary.apc_decisions.items()
    }
    baseline_payload = {
        "b0": {
            "available": b0.available,
            "note": (
                "legacy clean artifacts not present in this workspace; B0 "
                "prepared as code (b0_join) and exercised on synthetic "
                "fixtures only; B0 agreement is never ground truth"
            ),
            "checked_paths": b0.checked_paths,
            "sha256": b0.sha256,
        },
        "b1_status_counts": {
            status: sum(1 for d in b1_decisions.values() if d.status == status)
            for status in (
                "accepted_unique",
                "abstain_none",
                "abstain_multiple",
            )
        },
        "b1_b2_agreement": b1_b2_agreement(
            b1_decisions, primary.dispositions, b2_selected
        ),
        "human_label_agreement": "no labels exist; not evaluated",
        "tides_conformance": "not evaluated and not claimed",
    }

    # --- artifacts ---
    run_dir = ttc_root / "artifacts" / f"experimental-r1-spike-{run_id}-{slice_name}"
    config_text = yaml.safe_dump(cfg, sort_keys=True)
    run = SpikeRun(
        run_dir,
        config_hash=hash_config_text(config_text),
        contract_hash=contract_hash(contract_path),
        input_hashes={
            "normalized_tree": input_tree_hash([normalized_dir]),
            **{
                f"gtfs_{d}": input_tree_hash([p])
                for d, p in sorted(gtfs_dirs.items())
                if p.exists()
            },
        },
        assumptions=ASSUMPTIONS,
        unresolved_design_contracts=UNRESOLVED_DESIGN_CONTRACTS,
    )

    apc_time_rows = []
    for rec in prepared.apc_records:
        n_folds = len(rec.arrival_candidates)
        selected_fold = primary.fold_selected_by_attachment.get(rec.record_id)
        rolled_back = rec.record_id in primary.fold_rollbacks
        for fold, epoch in rec.arrival_candidates:
            if n_folds == 1:
                status = "selected"
                method = "unambiguous"
            elif rolled_back:
                status = "retained-unresolved"
                method = "attachment-rollback"
            elif selected_fold is None:
                status = "retained-unresolved"
                method = "no-attachment-selection"
            else:
                status = "selected" if fold == selected_fold else "rejected"
                method = "selected-by-attachment"
            apc_time_rows.append(
                {
                    "source_record_id": rec.record_id,
                    "source_type": "apc",
                    "wall_text": rec.arrival_wall,
                    "fold": fold,
                    "utc_epoch_s": epoch,
                    "utc_offset_s": None,
                    "ambiguous": n_folds > 1,
                    "valid": True,
                    "candidate_status": status,
                    "method": method,
                    "margin_s": None,
                    "reason_codes": (
                        "XR1.TIME.FOLD_ROLLED_BACK" if rolled_back else ""
                    ),
                    "algorithm_version": ALGORITHM_VERSION,
                }
            )
    run.write_parquet(
        pl.DataFrame(
            [*prepared.avl_time_rows, *apc_time_rows],
            infer_schema_length=None,
        ),
        "x_time_resolutions.parquet",
        grain="one row per (source record, time-fold candidate)",
        key=["source_record_id", "fold"],
    )
    seg_rows = [
        {
            "segment_id": s.segment_id,
            "variant": s.variant,
            "vehicle": s.vehicle,
            "n_pings": len(s.record_ids),
            "first_record_id": s.record_ids[0],
            "last_record_id": s.record_ids[-1],
            "start_instant_s": s.start_instant_s,
            "end_instant_s": s.end_instant_s,
            "duration_s": s.duration_s,
            "time_uncertain": s.time_uncertain,
            "n_unresolved": s.n_unresolved,
            "local_dates": "|".join(s.local_dates),
            "route_purity_millis": s.route_purity_millis,
            "boundary_reasons": "|".join(s.boundary_reasons),
        }
        for s in [*prepared.segments_x4a, *prepared.segments_x4b]
    ]
    run.write_parquet(
        pl.DataFrame(seg_rows, infer_schema_length=None),
        "x_segments.parquet",
        grain="one row per experimental segment per variant",
        key=["segment_id"],
    )
    if primary.scored_segment_rows:
        run.write_parquet(
            pl.DataFrame(primary.scored_segment_rows, infer_schema_length=None),
            "x_schedule_candidates.parquet",
            grain="one row per complete segment hypothesis",
            key=[
                "segment_id",
                "service_date_hypothesis",
                "route_mapping_hypothesis",
                "scheduled_trip_hypothesis",
            ],
        )
    if primary.scored_apc_rows:
        run.write_parquet(
            pl.DataFrame(primary.scored_apc_rows, infer_schema_length=None),
            "x_apc_candidates.parquet",
            grain="one row per complete APC attachment hypothesis",
            key=[
                "apc_source_record_id",
                "time_fold_hypothesis",
                "service_date_hypothesis",
                "stop_mapping_hypothesis",
                "candidate_segment_id",
            ],
        )
    seg_decision_rows = [
        {
            "segment_id": sid,
            "status": d.status,
            "selected_key": (
                "|".join(str(part) for part in d.selected_key)
                if d.selected_key
                else None
            ),
            "best_q": d.best_q,
            "runner_up_q": d.runner_up_q,
            "null_q": d.null_q,
            "margin_q": d.margin_q,
            "n_candidates": d.n_candidates,
            "n_hard_valid": d.n_hard_valid,
            "regime": primary.segment_regimes.get(sid),
            "reason_codes": "|".join(d.reason_codes),
        }
        for sid, d in sorted(primary.segment_decisions.items())
    ]
    run.write_parquet(
        pl.DataFrame(seg_decision_rows, infer_schema_length=None),
        "x_segment_decisions.parquet",
        grain="one row per segment Stage-1 decision",
        key=["segment_id"],
    )
    apc_decision_rows = [
        {
            "apc_source_record_id": rid,
            "status": d.status,
            "selected_key": (
                "|".join(str(part) for part in d.selected_key)
                if d.selected_key
                else None
            ),
            "best_q": d.best_q,
            "runner_up_q": d.runner_up_q,
            "null_q": d.null_q,
            "margin_q": d.margin_q,
            "n_candidates": d.n_candidates,
            "n_hard_valid": d.n_hard_valid,
            "reason_codes": "|".join(d.reason_codes),
        }
        for rid, d in sorted(primary.apc_decisions.items())
    ]
    run.write_parquet(
        pl.DataFrame(apc_decision_rows, infer_schema_length=None),
        "x_apc_decisions.parquet",
        grain="one row per APC Stage-2 decision",
        key=["apc_source_record_id"],
    )
    seq_rows = [
        {
            "segment_id": sid,
            "optimal_cost": r.optimal_cost,
            "n_optimal": r.n_optimal,
            "truncated": r.truncated,
            "demoted": "|".join(sorted(r.demoted)),
            "ambiguous_demotions": "|".join(sorted(r.ambiguous_demotions)),
            "fold_rollbacks": "|".join(sorted(r.fold_rollback_visit_ids)),
            "n_progress_violations": len(r.progress_violations),
            "reason_codes": "|".join(r.reason_codes),
        }
        for sid, r in sorted(primary.sequence_results.items())
    ]
    run.write_parquet(
        pl.DataFrame(
            seq_rows,
            schema={
                "segment_id": pl.String,
                "optimal_cost": pl.Int64,
                "n_optimal": pl.Int64,
                "truncated": pl.Boolean,
                "demoted": pl.String,
                "ambiguous_demotions": pl.String,
                "fold_rollbacks": pl.String,
                "n_progress_violations": pl.Int64,
                "reason_codes": pl.String,
            },
        ),
        "x_sequence_diagnostics.parquet",
        grain="one row per validated segment",
        key=["segment_id"],
    )
    disposition_rows = [
        {
            "apc_source_record_id": rid,
            "disposition": status,
            "reason_codes": "|".join(primary.disposition_reasons.get(rid, ())),
            "fold_selected_by_attachment": (
                primary.fold_selected_by_attachment.get(rid)
            ),
            "fold_rolled_back": rid in primary.fold_rollbacks,
        }
        for rid, status in sorted(primary.dispositions.items())
    ]
    run.write_parquet(
        pl.DataFrame(disposition_rows, infer_schema_length=None),
        "x_record_dispositions.parquet",
        grain="exactly one row per normalized APC record in the slice",
        key=["apc_source_record_id"],
    )
    inventory = edge_case_inventory(prepared, primary, slice_name=slice_name)
    if inventory:
        run.write_parquet(
            pl.DataFrame(inventory, infer_schema_length=None),
            "x_edge_case_inventory.parquet",
            grain="one row per (category, subject)",
            key=["slice", "category", "subject_id"],
        )

    run.write_json(
        {"contract": contracts[primary_variant].raw},
        "experimental_score_contract.json",
    )
    run.write_json(segment_report(prepared), "segment_variant_report.json")
    metrics = metrics_payload(
        prepared,
        primary,
        slice_name=slice_name,
        variant_deltas=variant_deltas,
        policy_deltas=policy_deltas,
    )
    run.write_json(metrics, "metrics.json")
    run.write_json(determinism, "determinism_report.json")
    run.write_json(baseline_payload, "baseline_report.json")
    run.write_json(
        {
            "total_wall_s": round(time.perf_counter() - t_start, 3),
            "peak_rss_mib": round(peak_rss_mib(), 1),
            "stage_timings_s": {
                k: round(v, 3) for k, v in prepared.stage_timings_s.items()
            },
            "candidate_counts": primary.candidate_counts,
        },
        "performance_report.json",
    )
    run.write_manifest(
        {
            "slice": slice_name,
            "slice_config": slice_cfg,
            "vehicle_query": prepared.vehicle_query,
            "vehicles": prepared.vehicles,
            "algorithm_version": ALGORITHM_VERSION,
            "primary_variant": primary_variant,
            "stage1_policy": policy,
            "resolution_segmentation_variant": resolution_variant,
            "determinism_identical": determinism["identical"],
        }
    )
    return {
        "slice": slice_name,
        "run_dir": str(run_dir),
        "metrics": metrics,
        "determinism_identical": determinism["identical"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="R1 design-validation spike runner (non-publishable)"
    )
    parser.add_argument(
        "--slice",
        choices=[*SLICE_QUERIES, "all"],
        default="all",
    )
    parser.add_argument("--run-id", default="dev")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("experiments/r1_design_spike/config/spike_config.yaml"),
    )
    parser.add_argument(
        "--ttc-root",
        type=Path,
        default=Path("."),
        help="agencies/ttc directory",
    )
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    slices = list(SLICE_QUERIES) if args.slice == "all" else [args.slice]
    for slice_name in slices:
        summary = run_one_slice(
            slice_name=slice_name,
            cfg=cfg,
            ttc_root=args.ttc_root.resolve(),
            run_id=args.run_id,
        )
        print(
            f"[{slice_name}] run_dir={summary['run_dir']} "
            f"determinism_identical={summary['determinism_identical']}"
        )
        print(f"[{slice_name}] dispositions={summary['metrics']['disposition_rates']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
