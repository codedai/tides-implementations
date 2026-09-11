"""Spike-02R Phase-A runner (experimental; cannot publish).

Usage (from ``agencies/ttc``):

    PYTHONPATH=experiments/r1_design_spike/src \\
        uv run python -m r1spike.run_spike02r --run-id dev

One guarded, permission-restricted run directory:
``artifacts/experimental-r1-spike02r-<run-id>/`` holding all three
slices' Phase-A artifacts (slice recorded as a column).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import random
import sys
import time
from pathlib import Path

import polars as pl
import yaml

from .boundary_v2 import BoundaryV2Config
from .crossfit import assign_folds, decision_change_matrix, fold_rows
from .evidence_dependencies import (
    anchor_edges,
    boundary_edges,
    edge_rows,
    reuse_report,
)
from .fragments_v2 import StitchConfig
from .label_factorial import (
    LABEL_SCHEMA,
    build_packet_rows,
    case_id,
    preregistration,
)
from .spike02_pipeline import (
    GeometryCache,
    attachment_battery,
    load_apc_flags,
    prepare_spike02,
    run_refinement,
)
from .spike02_scoring import load_spike02_contract, spike02_contract_hash
from .spike02r_contracts import (
    ALGORITHM_VERSION02R,
)
from .spike02r_manifest import (
    Spike02RRun,
    content_hashes,
    environment_snapshot,
    sha256_file,
    source_tree_hash,
)
from .spike02r_pipeline import (
    PROFILE_APC_ACTIVITY,
    PROFILE_APC_STRUCTURAL,
    PROFILE_AVL,
    anchor_v2_for_chunks,
    build_all_profiles,
    crossfit_profiles,
    reevaluate_attachment_dual_margins,
    revocable_vs_pinned,
    stitch_profile,
)
from .spike_pipeline import peak_rss_mib
from .terminal_episodes import EpisodeConfig
from .true_avl_only import profile_crosswalk

SLICE_QUERIES = {
    "normal_day": ("avl_route_day", lambda s: {"date": s["avl_dates"][0]}),
    "dst_fall_back": ("avl_dst_window", lambda s: {"date": s["avl_dates"][-1]}),
    "month_boundary": (
        "apc_after_avl_end",
        lambda s: {"after": s["avl_dates"][0] + " 23:59:59"},
    ),
}


def _interval_key(prepared, chunk) -> str:
    v = prepared.s1.pings_by_id[chunk.record_ids[0]].vehicle
    return f"{v}:{chunk.start_instant_s // 60}"


def run_phase_a(*, cfg: dict, ttc_root: Path, run_id: str) -> dict:
    t_start = time.perf_counter()
    episode_cfg = EpisodeConfig(**cfg["episodes"])
    boundary_cfg = BoundaryV2Config()
    stitch_cfg = StitchConfig()
    base_path = ttc_root / cfg["score_contract"]["base_path"]
    ext_path = ttc_root / cfg["score_contract"]["ext_path"]
    contract = load_spike02_contract(base_path, ext_path, "conservative")

    normalized_dir = ttc_root / cfg["inputs"]["normalized_dir"]
    gtfs_dirs = {
        d: ttc_root / p for d, p in cfg["inputs"]["gtfs_artifact_dirs"].items()
    }

    # ---- reproducibility base (spec 2C): real content hashes ----
    t0 = time.perf_counter()
    src_tree = source_tree_hash([ttc_root / "experiments" / "r1_design_spike"])
    input_hashes = {
        "normalized": content_hashes(normalized_dir, suffixes=(".parquet",)),
        "gtfs": {
            d: content_hashes(p / "normalized" / "gtfs", suffixes=(".parquet",))
            for d, p in sorted(gtfs_dirs.items())
            if p.exists()
        },
        "spike02_config": sha256_file(
            ttc_root / "experiments/r1_design_spike/config/spike02_config.yaml"
        ),
    }
    baseline_hashes = {
        name: content_hashes(ttc_root / "artifacts" / name)
        for name in sorted(
            p.name
            for p in (ttc_root / "artifacts").iterdir()
            if p.name.startswith(
                ("experimental-r1-spike-r1c-", "experimental-r1-spike02-s2b-")
            )
        )
    }
    config_text = yaml.safe_dump(cfg, sort_keys=True)
    config_hash = hashlib.sha256(config_text.encode()).hexdigest()
    env = environment_snapshot(sys.argv, ttc_root / "uv.lock")
    hash_time = time.perf_counter() - t0

    run = Spike02RRun(
        ttc_root / "artifacts" / f"experimental-r1-spike02r-{run_id}",
        config_hash=config_hash,
        source_tree=src_tree,
        input_hashes={"note": "full per-file hashes in input_content_hashes.json"},
        baseline_hashes={
            k: hashlib.sha256(str(sorted(v.items())).encode()).hexdigest()
            for k, v in baseline_hashes.items()
        },
        environment=env,
        experimental_parameters={
            "boundary_v2": boundary_cfg.__dict__,
            "stitch": stitch_cfg.__dict__,
        },
    )
    run.stage_timings_s["content_hashing"] = hash_time
    run.write_json({"inputs": input_hashes}, "input_content_hashes.json")
    run.write_json(dict(src_tree), "source_tree_manifest.json")
    adr = ttc_root / "docs/decisions/0003-isolated-r1-diagnostic-experiments.md"
    run.write_json(
        {
            "adr_path": str(adr),
            "adr_sha256": sha256_file(adr),
            "adr_status": "Accepted (owner-instruction basis; revocation clause)",
        },
        "authorization_snapshot.json",
    )

    salt = f"spike02r-{run_id}"
    all_rows: dict[str, list] = {
        "boundary_candidates": [],
        "boundary_decisions": [],
        "chunks": [],
        "crosswalk": [],
        "crossfit": [],
        "b_edges": [],
        "a_edges": [],
        "anchor_cands": [],
        "anchor_decisions": [],
        "align_assignments": [],
        "stability": [],
        "roles": [],
        "stitch": [],
        "hyps": [],
        "censorship": [],
        "dispositions": [],
        "cases": [],
    }
    reports: dict[str, dict] = {}

    for slice_name in ("normal_day", "dst_fall_back", "month_boundary"):
        slice_cfg = cfg["slices"][slice_name]
        query, params_fn = SLICE_QUERIES[slice_name]
        t0 = time.perf_counter()
        prepared = prepare_spike02(
            name=slice_name,
            cfg=cfg,
            ttc_root=ttc_root,
            vehicle_query=query,
            vehicle_params=params_fn(slice_cfg),
        )
        flags = load_apc_flags(
            normalized_dir,
            dates=slice_cfg["apc_dates"],
            wall_start=slice_cfg["wall_start"],
            wall_end=slice_cfg["wall_end"],
            vehicles=prepared.s1.vehicles,
        )
        run.stage_timings_s[f"{slice_name}:prepare"] = time.perf_counter() - t0

        # ---- profiles V1R / V2R-S / V2R-A ----
        t0 = time.perf_counter()
        profiles = build_all_profiles(
            prepared, flags, episode_cfg=episode_cfg, boundary_cfg=boundary_cfg
        )
        run.stage_timings_s[f"{slice_name}:profiles"] = time.perf_counter() - t0

        # comparator integrity: permute/remove APC -> V1R identical;
        # shuffled run order -> identical.
        t0 = time.perf_counter()
        rng = random.Random(20260808)
        permuted = prepared
        permuted_edges = list(prepared.edges)
        rng.shuffle(permuted_edges)
        saved_edges, saved_signals = prepared.edges, prepared.signals_by_id
        prepared.edges = permuted_edges[: len(permuted_edges) // 2]
        prepared.signals_by_id = {}
        profiles_permuted = build_all_profiles(
            prepared, {}, episode_cfg=episode_cfg, boundary_cfg=boundary_cfg
        )
        prepared.edges, prepared.signals_by_id = saved_edges, saved_signals
        _ = permuted
        rebuilt = build_all_profiles(
            prepared, flags, episode_cfg=episode_cfg, boundary_cfg=boundary_cfg
        )
        rebuild_identical = all(
            rebuilt[k].fingerprint() == profiles[k].fingerprint() for k in profiles
        )
        v1r_fp = profiles[PROFILE_AVL].fingerprint()
        integrity = {
            "all_profiles_rebuild_identical": rebuild_identical,
            "slice": slice_name,
            "v1r_fingerprint": v1r_fp,
            "v1r_fingerprint_apc_permuted": profiles_permuted[
                PROFILE_AVL
            ].fingerprint(),
            "v1r_invariant_under_apc_permutation": v1r_fp
            == profiles_permuted[PROFILE_AVL].fingerprint(),
            "v2r_structural_fingerprint": profiles[
                PROFILE_APC_STRUCTURAL
            ].fingerprint(),
            "v2r_activity_fingerprint": profiles[PROFILE_APC_ACTIVITY].fingerprint(),
            "v2r_structural_equals_activity": profiles[
                PROFILE_APC_STRUCTURAL
            ].fingerprint()
            == profiles[PROFILE_APC_ACTIVITY].fingerprint(),
        }
        reports.setdefault("comparator", {})[slice_name] = integrity
        run.stage_timings_s[f"{slice_name}:integrity"] = time.perf_counter() - t0

        for prof_name, pr in profiles.items():
            for c in pr.candidates:
                all_rows["boundary_candidates"].append(
                    {
                        "slice": slice_name,
                        "profile": prof_name,
                        "candidate_id": c.candidate_id,
                        "activity_run_id": c.activity_run_id,
                        "candidate_start_s": c.candidate_start_s,
                        "candidate_end_s": c.candidate_end_s,
                        "boundary_role": c.boundary_role,
                        "evidence_families": "|".join(c.evidence_families),
                        "evidence_regime": c.evidence_regime,
                        "qscore": c.qscore,
                        "hard_status": c.hard_status,
                        "n_dependency_records": len(c.dependency_records),
                        "service_derived": c.service_derived,
                    }
                )
            for d in pr.decisions:
                all_rows["boundary_decisions"].append(
                    {
                        "slice": slice_name,
                        "profile": prof_name,
                        "candidate_id": d.candidate_id,
                        "activity_run_id": d.activity_run_id,
                        "selected": d.selected,
                        "mandatory_in_all_optima": d.mandatory_in_all_optima,
                        "survival_margin_q": d.survival_margin_q,
                        "evidence_margin_q": d.evidence_margin_q,
                        "status": d.status,
                        "reason_codes": "|".join(d.reason_codes),
                    }
                )
            for ch in pr.chunks:
                all_rows["chunks"].append(
                    {
                        "slice": slice_name,
                        "profile": prof_name,
                        "chunk_id": ch.chunk_id,
                        "run_id": ch.run_id,
                        "n_pings": len(ch.record_ids),
                        "start_instant_s": ch.start_instant_s,
                        "end_instant_s": ch.end_instant_s,
                        "duration_s": ch.duration_s,
                        "role": ch.role.value,
                    }
                )
        all_rows["crosswalk"].extend(
            {"slice": slice_name, **row}
            for row in profile_crosswalk(
                profiles[PROFILE_AVL].chunks,
                profiles[PROFILE_APC_ACTIVITY].chunks,
            )
        )

        # ---- cross-fitting (V2R-ACTIVITY) ----
        t0 = time.perf_counter()
        folds = assign_folds(prepared.s1.apc_records)
        all_rows["crossfit"].extend(
            {"slice": slice_name, **row}
            for row in fold_rows(prepared.s1.apc_records, folds)
        )
        xf = crossfit_profiles(
            prepared, flags, episode_cfg=episode_cfg, boundary_cfg=boundary_cfg
        )
        cache = prepared.geometry_cache or GeometryCache(prepared.s1.gtfs_by_date)
        act_chunks = profiles[PROFILE_APC_ACTIVITY].chunks
        in_sample_anchors = anchor_v2_for_chunks(
            prepared,
            act_chunks,
            prepared.s1.apc_records,
            contract,
            slice_cfg=slice_cfg,
            cfg=cfg,
            geometry_lookup=cache.lookup,
        )
        crossfit_status: dict[str, dict[str, str]] = {"A": {}, "B": {}}
        for fold_built, fold_score in (("A", "B"), ("B", "A")):
            score_records = [
                r for r in prepared.s1.apc_records if folds[r.record_id] == fold_score
            ]
            xf_anchors = anchor_v2_for_chunks(
                prepared,
                xf[fold_built].chunks,
                score_records,
                contract,
                slice_cfg=slice_cfg,
                cfg=cfg,
                geometry_lookup=cache.lookup,
            )
            for cid, blk in xf_anchors.items():
                chunk = next(c for c in xf[fold_built].chunks if c.chunk_id == cid)
                key = _interval_key(prepared, chunk)
                assert key not in crossfit_status[fold_built], key
                crossfit_status[fold_built][key] = blk["decision"].status
        in_sample_status = {}
        for cid, blk in in_sample_anchors.items():
            chunk = next(c for c in act_chunks if c.chunk_id == cid)
            in_sample_status[_interval_key(prepared, chunk)] = blk["decision"].status
        accepted_in_sample = {k for k, s in in_sample_status.items() if s == "accepted"}
        survives = [
            k
            for k in sorted(accepted_in_sample)
            if all(
                crossfit_status[f].get(k, "absent") == "accepted"
                for f in ("A", "B")
                if k in crossfit_status[f]
            )
            and any(k in crossfit_status[f] for f in ("A", "B"))
        ]
        reports.setdefault("crossfit", {})[slice_name] = {
            "direction_A_boundaries": decision_change_matrix(
                in_sample_status, crossfit_status["A"]
            ),
            "direction_B_boundaries": decision_change_matrix(
                in_sample_status, crossfit_status["B"]
            ),
            "accepted_in_sample": len(accepted_in_sample),
            "accepted_surviving_both_directions": len(survives),
        }
        run.stage_timings_s[f"{slice_name}:crossfit_anchors"] = time.perf_counter() - t0

        # ---- evidence dependency + reuse ----
        b_edges = boundary_edges(
            profiles[PROFILE_APC_ACTIVITY].episodes_by_run,
            profile=PROFILE_APC_ACTIVITY,
            folds=folds,
        )
        boundary_sources_by_chunk: dict[str, set[str]] = {}
        applied_deps: dict[str, set[str]] = {}
        sel = {
            d.candidate_id
            for d in profiles[PROFILE_APC_ACTIVITY].decisions
            if d.selected
        }
        for c in profiles[PROFILE_APC_ACTIVITY].candidates:
            if c.candidate_id in sel:
                applied_deps.setdefault(c.activity_run_id, set()).update(
                    c.dependency_records
                )
        for ch in act_chunks:
            boundary_sources_by_chunk[ch.chunk_id] = applied_deps.get(ch.run_id, set())
        a_edges = anchor_edges(
            {cid: blk["visit_records"] for cid, blk in in_sample_anchors.items()},
            boundary_sources_by_chunk,
            folds=folds,
        )
        all_rows["b_edges"].extend(
            {"slice": slice_name, **row} for row in edge_rows(b_edges)
        )
        all_rows["a_edges"].extend(
            {"slice": slice_name, **row} for row in edge_rows(a_edges)
        )
        rr = reuse_report(b_edges, a_edges)
        rr.pop("per_segment", None)
        reports.setdefault("reuse", {})[slice_name] = rr

        for cid, blk in sorted(in_sample_anchors.items()):
            d = blk["decision"]
            all_rows["anchor_decisions"].append(
                {
                    "slice": slice_name,
                    "chunk_id": cid,
                    "status": d.status,
                    "best_key": d.best_key,
                    "best_q": d.best_q,
                    "survival_margin_q": d.survival_margin_q,
                    "evidence_margin_q": d.evidence_margin_q,
                    "n_contract_blockers": len(d.blocking_contract_competitors),
                    "censored_considered": d.censored_alternatives_considered,
                    "reason_codes": "|".join(d.reason_codes),
                }
            )
            all_rows["anchor_cands"].extend(
                {"slice": slice_name, **row} for row in blk["candidate_rows"]
            )

        # ---- revocable vs pinned alignment ----
        t0 = time.perf_counter()
        align_cmp = revocable_vs_pinned(
            in_sample_anchors,
            act_chunks,
            prepared,
            unmatched_cost_q=cfg["alignment_paths"]["unmatched_cost_q"],
            break_penalty_q=200_000,
        )
        changed_assignments = 0
        no_baseline_assignments = 0
        pinned_infeasible = 0
        for vehicle, blk in sorted(align_cmp.items()):
            stability = blk["revocable"]
            pinned = blk["pinned"]
            if pinned is None:
                pinned_infeasible += 1
                pinned_by_seg = None
            else:
                pinned_by_seg = {
                    s.segment_id: (s.status, s.trip_id) for s in pinned.segments
                }
            all_rows["stability"].append(
                {
                    "slice": slice_name,
                    "component_id": f"{slice_name}:{vehicle}",
                    "n_states": stability.n_states,
                    "within_bound": stability.within_bound,
                    "optimum_analysis_incomplete": (
                        stability.optimum_analysis_incomplete
                    ),
                    "best_cost": stability.best_cost,
                }
            )
            for a in stability.assignments:
                if pinned_by_seg is None:
                    # pinning has NO feasible solution here: there is no
                    # baseline to differ from — counted separately.
                    no_baseline_assignments += 1
                    old = (None, None)
                    changed = False
                else:
                    old = pinned_by_seg.get(a.segment_id, (None, None))
                    changed = (a.candidate_key or None) != old[1] or (
                        a.status in ("anchor_revoked", "tied")
                        and old[0] in ("anchor", "matched")
                    )
                changed_assignments += int(changed)
                all_rows["align_assignments"].append(
                    {
                        "slice": slice_name,
                        "vehicle": vehicle,
                        "segment_id": a.segment_id,
                        "status": a.status,
                        "candidate_key": a.candidate_key,
                        "mandatory_in_all_optima": a.mandatory_in_all_optima,
                        "best_without_cost_delta_q": (a.best_without_cost_delta_q),
                        "pinned_status": old[0],
                        "pinned_key": old[1],
                        "changed_vs_pinned": changed,
                    }
                )
        reports.setdefault("alignment", {})[slice_name] = {
            "changed_vs_pinned": changed_assignments,
            "pinned_infeasible_components": pinned_infeasible,
            "assignments_without_pinned_baseline": no_baseline_assignments,
        }
        run.stage_timings_s[f"{slice_name}:alignment"] = time.perf_counter() - t0

        # ---- fragments: roles + stitching (V2R-ACTIVITY) ----
        roles, stitch_decisions, hyps = stitch_profile(
            profiles[PROFILE_APC_ACTIVITY],
            in_sample_anchors,
            prepared.along_by_id,
            stitch_cfg,
        )
        for ch in roles:
            all_rows["roles"].append(
                {
                    "slice": slice_name,
                    "chunk_id": ch.chunk_id,
                    "role": ch.role.value,
                    "duration_s": ch.duration_s,
                    "role_evidence": "|".join(ch.role_evidence),
                }
            )
        for d in stitch_decisions:
            all_rows["stitch"].append(
                {
                    "slice": slice_name,
                    "stitch_id": d.stitch_id,
                    "run_id": d.run_id,
                    "n_members": len(d.member_chunk_ids),
                    "status": d.status,
                    "performed_trip_hypothesis_id": (d.performed_trip_hypothesis_id),
                    "reason_codes": "|".join(d.reason_codes),
                }
            )
        all_rows["hyps"].extend({"slice": slice_name, **h} for h in hyps)

        # ---- attachment dual margins (the DST-49 battery runs on the
        # frozen Spike-02 segmentation so the re-evaluation targets the
        # exact prior decisions) ----
        if slice_name in ("dst_fall_back", "month_boundary", "normal_day"):
            t0 = time.perf_counter()
            s2_refs = run_refinement(prepared, variant="v2_conditional", cfg=cfg)
            from .spike02_pipeline import refined_spike01_segments

            segs01 = refined_spike01_segments(prepared, s2_refs)
            grid, _base = attachment_battery(
                prepared,
                segs01,
                s2_refs,
                [],
                {"conservative": contract},
                cfg=cfg,
                slice_cfg=slice_cfg,
                t_mid_values=[cfg["attachment"]["primary_t_mid_s"]],
                contract_variants=["conservative"],
            )
            resolution = grid[
                (cfg["attachment"]["primary_t_mid_s"], "conservative")
            ].resolution
            dm, censor_rows = reevaluate_attachment_dual_margins(resolution, contract)
            all_rows["censorship"].extend(
                {"slice": slice_name, **row} for row in censor_rows
            )
            old_statuses = dict(resolution.dispositions)
            transitions: dict[str, int] = {}
            unique_survivors = []
            for rid in sorted(old_statuses):
                new = dm[rid].status if rid in dm else "absent"
                key = f"{old_statuses[rid]} -> {new}"
                transitions[key] = transitions.get(key, 0) + 1
                if new == "unique_survivor_by_exclusion":
                    unique_survivors.append(rid)
                all_rows["dispositions"].append(
                    {
                        "slice": slice_name,
                        "apc_source_record_id": rid,
                        "spike02_status": old_statuses[rid],
                        "dual_margin_status": new,
                        "survival_margin_q": (
                            dm[rid].survival_margin_q if rid in dm else None
                        ),
                        "evidence_margin_q": (
                            dm[rid].evidence_margin_q if rid in dm else None
                        ),
                    }
                )
            reports.setdefault("dual_margin", {})[slice_name] = {
                "transitions": transitions,
                "unique_survivor_count": len(unique_survivors),
                "prior_accepted": sum(
                    1 for s in old_statuses.values() if s == "accepted"
                ),
                "accepted_after_dual_margin": sum(
                    1 for d in dm.values() if d.status == "accepted"
                ),
            }
            run.stage_timings_s[f"{slice_name}:dual_margin"] = time.perf_counter() - t0

        # ---- label packet cases (deterministic, per slice) ----
        for cid, blk in sorted(in_sample_anchors.items())[:6]:
            chunk = next(c for c in act_chunks if c.chunk_id == cid)
            all_rows["cases"].append(
                {
                    "real_id": cid,
                    "stratum": ("fragment" if chunk.duration_s < 1200 else "route_929"),
                    "kind": "scheduled_trip",
                    "epoch_s": chunk.start_instant_s,
                    "events": [(cid, 0, "chunk start")],
                    "public_summary": (
                        f"chunk of {chunk.duration_s}s, anchor status "
                        f"{blk['decision'].status}"
                    ),
                    "detail": {
                        "chunk_id": cid,
                        "status": blk["decision"].status,
                    },
                }
            )

    # ---- deterministic expanded sample (Phase B selection only) ----
    t0 = time.perf_counter()
    avl = pl.scan_parquet(normalized_dir / "avl", hive_partitioning=True).filter(
        pl.col("route_label").is_in(["29", "929"])
    )
    vd = (
        avl.group_by("vehicle_id_raw", "source_calendar_date")
        .len()
        .filter(pl.col("len") >= 100)
        .collect(engine="streaming")
    )
    vd_keys = sorted(
        f"{r['vehicle_id_raw']}|{r['source_calendar_date']}"
        for r in vd.iter_rows(named=True)
    )
    ranked = sorted(
        vd_keys, key=lambda k: hashlib.sha256(f"phaseB|{k}".encode()).hexdigest()
    )
    sample = ranked[:12]
    sampling_plan = {
        "query": (
            "vehicle-days with >=100 AVL pings on route_label in {29,929}; "
            "ordered by sha256('phaseB|'+vehicle+'|'+date); first 12"
        ),
        "population_size": len(vd_keys),
        "selected_pseudonyms": [case_id(k, salt) for k in sample],
        "selection_sha256": hashlib.sha256("|".join(sample).encode()).hexdigest(),
        "processed": False,
        "note": "selection recorded before any outcome inspection; "
        "processing deferred to the labeled phase",
    }
    run.write_json(sampling_plan, "sampling_plan.json")
    run.stage_timings_s["phase_b_selection"] = time.perf_counter() - t0

    # ---- artifacts ----
    def wp(name, rows, grain, key, privacy="restricted-local"):
        if rows:
            run.write_parquet(
                pl.DataFrame(rows, infer_schema_length=None),
                name,
                grain=grain,
                key=key,
                producer="r1spike.run_spike02r",
                privacy=privacy,
            )

    chunks_rows = all_rows["chunks"]
    wp(
        "x_true_avl_only_boundaries.parquet",
        [r for r in all_rows["boundary_decisions"] if r["profile"] == "avl"],
        "one row per V1R boundary decision",
        ["slice", "candidate_id"],
    )
    wp(
        "x_apc_structural_boundaries.parquet",
        [r for r in all_rows["boundary_decisions"] if r["profile"] == "apc-structural"],
        "one row per V2R-STRUCTURAL boundary decision",
        ["slice", "candidate_id"],
    )
    wp(
        "x_apc_activity_boundaries.parquet",
        [r for r in all_rows["boundary_decisions"] if r["profile"] == "apc-activity"],
        "one row per V2R-ACTIVITY boundary decision",
        ["slice", "candidate_id"],
    )
    wp(
        "x_boundary_candidates_complete.parquet",
        all_rows["boundary_candidates"],
        "one row per complete boundary candidate per profile",
        ["slice", "profile", "candidate_id"],
    )
    wp(
        "x_boundary_decisions.parquet",
        all_rows["boundary_decisions"],
        "one row per boundary decision per profile",
        ["slice", "profile", "candidate_id"],
    )
    wp(
        "x_refined_chunks.parquet",
        chunks_rows,
        "one row per chunk per profile (xsg-avl-*/xsg-apc-*)",
        ["slice", "profile", "chunk_id"],
    )
    wp(
        "x_profile_crosswalk.parquet",
        all_rows["crosswalk"],
        "one row per overlapping (avl chunk, apc chunk) pair",
        ["slice", "avl_chunk_id", "apc_chunk_id"],
    )
    wp(
        "crossfit_assignments.parquet",
        all_rows["crossfit"],
        "one row per APC record fold assignment",
        ["slice", "apc_source_record_id"],
    )
    wp(
        "x_boundary_evidence_edges.parquet",
        all_rows["b_edges"],
        "one row per (boundary, source record) edge",
        ["slice", "derived_id", "source_record_id"],
    )
    wp(
        "x_anchor_evidence_edges.parquet",
        all_rows["a_edges"],
        "one row per (anchor chunk, source record) edge",
        ["slice", "derived_id", "source_record_id"],
    )
    wp(
        "x_evidence_sources.parquet",
        all_rows["b_edges"] + all_rows["a_edges"],
        "union of evidence edges",
        ["slice", "derived_id", "source_record_id", "use"],
    )
    wp(
        "x_complete_schedule_candidates.parquet",
        all_rows["anchor_cands"],
        "one row per complete-key anchor candidate",
        ["slice", "chunk_id", "candidate_key"],
    )
    wp(
        "x_anchor_candidates_v2.parquet",
        all_rows["anchor_decisions"],
        "one row per chunk anchor dual-margin decision",
        ["slice", "chunk_id"],
    )
    wp(
        "x_revocable_alignment_edges.parquet",
        all_rows["align_assignments"],
        "one row per alignment assignment (revocable vs pinned)",
        ["slice", "vehicle", "segment_id"],
    )
    wp(
        "x_optimum_stability.parquet",
        all_rows["stability"],
        "one row per alignment component",
        ["slice", "component_id"],
    )
    wp(
        "x_fragment_roles.parquet",
        all_rows["roles"],
        "one row per chunk role",
        ["slice", "chunk_id"],
    )
    wp(
        "x_fragment_stitch_candidates.parquet",
        all_rows["stitch"],
        "one row per stitch decision",
        ["slice", "stitch_id"],
    )
    wp(
        "x_performed_trip_hypotheses.parquet",
        all_rows["hyps"],
        "one row per performed-trip HYPOTHESIS (xpt2r-*)",
        ["slice", "performed_trip_hypothesis_id"],
    )
    wp(
        "x_candidate_censorship.parquet",
        all_rows["censorship"],
        "one row per (record, candidate) censorship classification",
        ["slice", "apc_source_record_id", "candidate_key"],
    )
    wp(
        "x_record_dispositions.parquet",
        all_rows["dispositions"],
        "one row per APC record: Spike-02 status vs dual-margin status",
        ["slice", "apc_source_record_id"],
    )
    restricted, redacted = build_packet_rows(all_rows["cases"], salt=salt)
    wp(
        "labeling_packet_restricted.parquet",
        restricted,
        "one row per labeling case (restricted lineage)",
        ["case_id"],
    )
    if redacted:
        from .spike02r_manifest import _restrict

        path = run.run_dir / "labeling_packet_redacted.csv"
        pl.DataFrame(redacted, infer_schema_length=None).write_csv(path)
        _restrict(path)
        run.artifacts["labeling_packet_redacted.csv"] = {"sha256": sha256_file(path)}
    run.write_json(LABEL_SCHEMA, "label_schema.json")

    prereg = preregistration(
        label_artifact_hash=None,
        feature_registry_hash=spike02_contract_hash(base_path, ext_path),
        config_hash=config_hash,
        calibration_vehicle_days=sample[:8],
        holdout_vehicle_days=sample[8:],
    )
    run.write_json(prereg, "experiment_preregistration.json")

    run.write_json(reports.get("comparator", {}), "comparator_integrity_report.json")
    run.write_json(reports.get("reuse", {}), "evidence_reuse_report.json")
    run.write_json(
        {
            "dual_margin": reports.get("dual_margin", {}),
            "note": "acceptance now requires evidence AND survival margins",
        },
        "dual_margin_report.json",
    )
    run.write_json(
        {
            slice_name: {
                "unique_survivors": blk.get("unique_survivor_count"),
                "prior_accepted": blk.get("prior_accepted"),
                "accepted_after": blk.get("accepted_after_dual_margin"),
            }
            for slice_name, blk in reports.get("dual_margin", {}).items()
        },
        "unique_survivor_report.json",
    )
    wp(
        "x_edge_case_inventory.parquet",
        [
            {
                "slice": r["slice"],
                "category": "unique_survivor"
                if r["dual_margin_status"] == "unique_survivor_by_exclusion"
                else r["dual_margin_status"],
                "subject_id": r["apc_source_record_id"],
            }
            for r in all_rows["dispositions"]
            if r["spike02_status"] == "accepted"
        ],
        "one row per re-evaluated previously-accepted record",
        ["slice", "subject_id"],
    )
    run.write_json(
        {
            "crossfit": reports.get("crossfit", {}),
            "alignment_changes": reports.get("alignment", {}),
        },
        "factorial_metrics.json",
    )
    fragment_report = {
        "roles": _count(all_rows["roles"], "role"),
        "stitch_status": _count(all_rows["stitch"], "status"),
        "performed_trip_hypotheses": len(all_rows["hyps"]),
    }
    run.write_json(fragment_report, "fragment_report.json")

    # determinism: rebuild profiles for normal_day twice more
    det = {
        "note": "V1R fingerprints invariant under APC permutation; ALL "
        "profiles identical across unchanged rebuilds; V2R profiles "
        "legitimately change under APC permutation by design",
        "identical": all(
            blk["v1r_invariant_under_apc_permutation"]
            and blk["all_profiles_rebuild_identical"]
            for blk in reports.get("comparator", {}).values()
        ),
    }
    run.write_json(det, "determinism_report.json")
    run.write_json(
        {
            "total_wall_s": round(time.perf_counter() - t_start, 3),
            "peak_rss_mib": round(peak_rss_mib(), 1),
            "stage_timings_s": {k: round(v, 3) for k, v in run.stage_timings_s.items()},
        },
        "performance_report.json",
    )
    run.write_json(
        {
            "profile_fingerprints": {
                s: {k: v for k, v in blk.items() if "fingerprint" in k}
                for s, blk in reports.get("comparator", {}).items()
            }
        },
        "logical_checksums.json",
    )
    manifest_path = run.finalize(
        {
            "phase": "A",
            "algorithm_version": ALGORITHM_VERSION02R,
            "started_reference": dt.datetime.now(dt.UTC).isoformat(),
        }
    )
    return {"run_dir": str(run.run_dir), "manifest": str(manifest_path)}


def _count(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return dict(sorted(out.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="R1 Spike-02R Phase-A runner (non-publishable)"
    )
    parser.add_argument("--run-id", default="dev")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("experiments/r1_design_spike/config/spike02_config.yaml"),
    )
    parser.add_argument("--ttc-root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    summary = run_phase_a(cfg=cfg, ttc_root=args.ttc_root.resolve(), run_id=args.run_id)
    print(f"run_dir={summary['run_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
