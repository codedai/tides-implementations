"""Spike-02 runner CLI (experimental; cannot publish).

Usage (from ``agencies/ttc``):

    PYTHONPATH=experiments/r1_design_spike/src \\
        uv run python -m r1spike.run_spike02 --slice normal_day --run-id dev

One guarded run directory per (run-id, slice):
``artifacts/experimental-r1-spike02-<run-id>-<slice>/``.
"""

from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

import polars as pl
import yaml

from .anchor_alignment import AlignmentInput, AlignmentPathResult, align
from .boundary_refinement import RefinementConfig, propose_boundaries
from .contracts import NO_SCHEDULED_TRIP, UNRESOLVED_SERVICE_DATE
from .diagnostics import fingerprint_checksum
from .fixedpoint import SCORE_SCALE
from .inputs import input_tree_hash
from .provisional_runs import ordered_run_pings
from .refined_segments import membership_rows
from .spike02_artifacts import Spike02Run, hash_config_text
from .spike02_contracts import EpisodeStatus, Reason02
from .spike02_diagnostics import (
    EXPERIMENT_MATRIX,
    alignment_metrics,
    anchor_metrics,
    attachment_summary,
    episode_metrics,
    multiplicity_before_after,
    refinement_fingerprint,
    refinement_metrics,
    reproduce_r1c_baseline,
)
from .spike02_pipeline import (
    Spike02Prepared,
    anchors_for_refinement,
    attachment_battery,
    prepare_spike02,
    refined_spike01_segments,
    run_refinement,
    spanning_roles_for,
)
from .spike02_scoring import load_spike02_contract, spike02_contract_hash
from .spike_pipeline import peak_rss_mib

SLICE_QUERIES = {
    "normal_day": ("avl_route_day", lambda s: {"date": s["avl_dates"][0]}),
    "dst_fall_back": ("avl_dst_window", lambda s: {"date": s["avl_dates"][-1]}),
    "month_boundary": (
        "apc_after_avl_end",
        lambda s: {"after": s["avl_dates"][0] + " 23:59:59"},
    ),
}

ASSUMPTIONS02 = [
    "naive America/Toronto wall-time interpretation (G0.3/D-16 open)",
    "candidate GTFS manifest override (D-15 open; no approved feed exists)",
    "APC count-field POSITIVITY drives derived activity signals only; "
    "count semantics remain unconfirmed (D-06); raw values never summed, "
    "repaired, or scored",
    "terminal clusters are versioned experimental hypotheses from GTFS "
    "pattern endpoints; the 1652=Wilson Station association is a D-10 OPEN "
    "hypothesis with recorded provenance, not an approved crosswalk",
    "the four-timestamp terminal model and Q0-Q3 quality tiers have no "
    "prior contract anywhere in the plan; they are invented by this spike "
    "to be falsified",
    "APC-assisted boundary refinement CONFLICTS with the approved acyclic "
    "architecture (plan Appendix C); it is tested here strictly as a "
    "candidate plan revision under a one-pass restriction",
    "direction evidence is dual-polarity hypothesis only (D-11 open)",
    "all refinement/attachment/anchor thresholds are uncalibrated "
    "experimental hypotheses (D-14)",
    "dominant-shape-per-(date,route) geometry (Spike-01 simplification)",
]

UNRESOLVED_DESIGN_CONTRACTS02 = [
    "R1-B01..R1-B11 (unchanged from Spike 01; R1 remains open)",
    "D-06 APC row grain / count semantics",
    "D-07 visit-key collision explanation",
    "D-10 stop-code 1652 crosswalk",
    "D-11 direction/branch/run/tatripId semantics",
    "D-14 calibration protocol for every threshold used here",
    "XR2: terminal episode contract (four timestamps, evidence families)",
    "XR2: one-pass refinement as a plan revision to the acyclic order",
    "XR2: turnaround ownership projection (departure vs physical truth)",
]


def _episode_rows(prepared: Spike02Prepared) -> list[dict]:
    rows = []
    for e in prepared.episodes:
        rows.append(
            {
                "episode_id": e.episode_id,
                "run_id": e.run_id,
                "cluster_id": e.cluster_id,
                "kind": e.kind,
                "physical_arrival_s": e.physical_arrival_s,
                "service_end_s": e.service_end_s,
                "service_start_s": e.service_start_s,
                "physical_departure_s": e.physical_departure_s,
                "evidence_families": "|".join(e.evidence_families),
                "physical_evidence_present": e.physical_evidence_present,
                "n_apc_records": len(e.apc_record_ids),
                "n_spanning_records": len(e.spanning_apc_record_ids),
                "ping_index_first": e.ping_index_first,
                "ping_index_last": e.ping_index_last,
                "ordering_valid": e.ordering_valid,
                "status": e.status.value,
                "apc_quality": e.apc_quality.value if e.apc_quality else None,
                "reason_codes": "|".join(e.reason_codes),
            }
        )
    return rows


def _time_hypothesis_rows(prepared: Spike02Prepared) -> list[dict]:
    rows = []
    for e in prepared.episodes:
        for name, value in (
            ("physical_arrival", e.physical_arrival_s),
            ("service_end", e.service_end_s),
            ("service_start", e.service_start_s),
            ("physical_departure", e.physical_departure_s),
        ):
            rows.append(
                {
                    "episode_id": e.episode_id,
                    "hypothesis": name,
                    "epoch_s": value,
                    "present": value is not None,
                    "ordering_valid": e.ordering_valid,
                    "status": e.status.value,
                }
            )
        for name, value in e.alternative_timestamps:
            rows.append(
                {
                    "episode_id": e.episode_id,
                    "hypothesis": f"alternative:{name}",
                    "epoch_s": value,
                    "present": True,
                    "ordering_valid": e.ordering_valid,
                    "status": e.status.value,
                }
            )
    return rows


def _wilson_report(
    prepared: Spike02Prepared,
    refinements,
    roles,
    attachment_runs,
    anchor_results,
    alignments: dict[str, AlignmentPathResult],
    *,
    primary_t_mid: int,
) -> dict:
    """Timeline for the mandatory vehicle-7237 Wilson (stop 1652)
    regression cases. Times and distances only — no raw coordinates and
    no passenger quantities."""
    targets = [
        r
        for r in prepared.s1.apc_records
        if r.vehicle == "7237" and str(r.stop_code) == "1652"
    ]
    targets.sort(key=lambda r: r.record_id)
    seg_by_record: dict[str, tuple[str, str]] = {}
    for res in refinements:
        for seg in res.refined_segments:
            for rid in seg.record_ids:
                seg_by_record[rid] = ("segment", seg.refined_segment_id)
        for ta in res.turnarounds:
            for rid in ta.record_ids:
                seg_by_record[rid] = ("turnaround", ta.turnaround_id)
    roles_by_record = {r.apc_source_record_id: r for r in roles}
    primary = attachment_runs.get((primary_t_mid, "conservative"))
    cases = []
    for record in targets:
        signal = prepared.signals_by_id.get(record.record_id)
        edges = [
            e for e in prepared.edges if e.apc_source_record_id == record.record_id
        ]
        episode = next(
            (e for e in prepared.episodes if record.record_id in e.apc_record_ids),
            None,
        )
        role = roles_by_record.get(record.record_id)
        disposition = (
            primary.resolution.dispositions.get(record.record_id) if primary else None
        )
        arrival_owner = None
        departure_owner = None
        if record.arrival_candidates:
            a0 = record.arrival_candidates[0][1]
            a1 = (
                max(e for _f, e in record.departure_candidates)
                if record.departure_candidates
                else a0
            )
            for res in refinements:
                for seg in res.refined_segments:
                    if seg.start_instant_s <= a0 <= seg.end_instant_s:
                        arrival_owner = seg.refined_segment_id
                    if seg.start_instant_s <= a1 <= seg.end_instant_s:
                        departure_owner = seg.refined_segment_id
                for ta in res.turnarounds:
                    if ta.start_instant_s <= a0 <= ta.end_instant_s:
                        arrival_owner = arrival_owner or ta.turnaround_id
                    if ta.start_instant_s <= a1 <= ta.end_instant_s:
                        departure_owner = departure_owner or ta.turnaround_id
        anchor_status = {}
        for owner in (arrival_owner, departure_owner):
            if owner and owner in anchor_results:
                r = anchor_results[owner]
                anchor_status[owner] = {
                    "status": r.status,
                    "is_anchor": r.is_anchor,
                    "margin_q": r.margin_q,
                }
        alignment_status = {}
        for res in alignments.values():
            for seg in res.segments:
                if seg.segment_id in (arrival_owner, departure_owner):
                    alignment_status[seg.segment_id] = {
                        "status": seg.status,
                        "trip_id": seg.trip_id,
                    }
        cases.append(
            {
                "apc_source_record_id": record.record_id,
                "arrival_wall": record.arrival_wall,
                "departure_wall": record.departure_wall,
                "interval_s": (
                    (
                        max(e for _f, e in record.departure_candidates)
                        - record.arrival_candidates[0][1]
                    )
                    if record.departure_candidates and record.arrival_candidates
                    else None
                ),
                "signal_state": signal.state.value if signal else None,
                "signal_quality": signal.quality.value if signal else None,
                "n_alignment_edges": len(edges),
                "aligned_run_ids": sorted({e.run_id for e in edges}),
                "episode_id": episode.episode_id if episode else None,
                "episode_status": episode.status.value if episode else None,
                "episode_timestamps": (
                    {
                        "physical_arrival_s": episode.physical_arrival_s,
                        "service_end_s": episode.service_end_s,
                        "service_start_s": episode.service_start_s,
                        "physical_departure_s": episode.physical_departure_s,
                    }
                    if episode
                    else None
                ),
                "spanning_dual_role": role is not None,
                "arrival_segment_candidate": (
                    role.arrival_segment_candidate if role else arrival_owner
                ),
                "departure_segment_candidate": (
                    role.departure_segment_candidate if role else departure_owner
                ),
                "arrival_instant_owner": arrival_owner,
                "departure_instant_owner": departure_owner,
                "final_disposition": disposition,
                "anchor_status_of_owners": anchor_status,
                "alignment_status_of_owners": alignment_status,
            }
        )
    return {"vehicle": "7237", "stop_code_hypothesis": "1652", "cases": cases}


def _edge_case_rows(
    prepared: Spike02Prepared,
    refinements,
    attachment_runs,
    anchor_results,
    alignments,
    *,
    slice_name: str,
    primary_t_mid: int,
    cap_per_category: int = 5,
) -> list[dict]:
    rows: list[dict] = []
    counts: dict[str, int] = {}

    def add(category: str, subject_id: str, summary: str, why: str) -> None:
        if counts.get(category, 0) >= cap_per_category:
            return
        counts[category] = counts.get(category, 0) + 1
        rows.append(
            {
                "slice": slice_name,
                "category": category,
                "subject_id": subject_id,
                "summary": summary,
                "why_it_matters": why,
            }
        )

    for res in refinements:
        for d in res.boundary_decisions:
            if d.status.value == "applied":
                why = (
                    "APC precedence"
                    if (Reason02.BOUNDARY_APPLIED_APC in d.reason_codes)
                    else "AVL evidence"
                )
                add(
                    "successful_terminal_refinement",
                    d.boundary_id,
                    f"boundary applied ({why})",
                    "shows the refinement path producing a trip-scale cut",
                )
                if Reason02.BOUNDARY_APPLIED_APC in d.reason_codes:
                    add(
                        "apc_overrides_coarse_buffer",
                        d.boundary_id,
                        "Q3 APC narrowed the physical buffer",
                        "tests conditional precedence (RQ4)",
                    )
            elif d.status.value == "ambiguous_not_applied":
                add(
                    "boundary_unresolved",
                    d.boundary_id,
                    "ambiguous boundary retained, run not split",
                    "ambiguity must survive, not be tie-broken",
                )
    for e in prepared.episodes:
        if Reason02.EPISODE_APC_CONFLICT in e.reason_codes:
            add(
                "apc_avl_conflict",
                e.episode_id,
                "APC service times far outside physical visit",
                "conflicting evidence must retain hypotheses",
            )
        if e.status == EpisodeStatus.INSUFFICIENT and (
            "source_direction_transition" in e.evidence_families
        ):
            add(
                "false_direction_change_rejected",
                e.episode_id,
                "direction-only evidence refused",
                "direction alone must never split",
            )
        if e.kind == "short_turn_candidate":
            add(
                "short_turn_candidate",
                e.episode_id,
                "progress reversal outside terminal clusters",
                "short turns need multi-family evidence",
            )
        if e.spanning_apc_record_ids:
            add(
                "spanning_terminal_interval",
                e.episode_id,
                f"{len(e.spanning_apc_record_ids)} spanning APC record(s)",
                "dual-role evidence without count duplication",
            )
    for rid, signal in sorted(prepared.signals_by_id.items()):
        if signal.quality.value == "q1_weak":
            add(
                "apc_rejected_as_weak",
                rid,
                "|".join(signal.reason_codes[1:]) or "weak signal",
                "Q1 must never create a boundary alone",
            )
    primary = attachment_runs.get((primary_t_mid, "conservative"))
    if primary:
        for rid, status in sorted(primary.resolution.dispositions.items()):
            if status == "no_candidate":
                add(
                    "no_candidate",
                    rid,
                    "no non-null candidate",
                    "missing AVL coverage stays unresolved",
                )
        for rid in primary.pruned_nearest_candidate_records:
            add(
                "tmid_pruned_best_candidate",
                rid,
                f"nearest segment pruned at T_mid={primary.t_mid_s}s",
                "bound that removes the plausible owner is a failure",
            )
    for sid, r in sorted(anchor_results.items()):
        if r.is_anchor:
            add(
                "anchor_accepted",
                sid,
                f"margin_q={r.margin_q}",
                "H6: anchors must rest on terminal/stop evidence",
            )
        elif r.status == "low_margin":
            add(
                "anchor_margin_still_low",
                sid,
                f"margin_q={r.margin_q}",
                "close-headway ambiguity persisting despite anchors",
            )
    for vehicle, res in sorted(alignments.items()):
        for seg in res.segments:
            if seg.status == "tied":
                add(
                    "anchor_and_align_tied",
                    seg.segment_id,
                    f"vehicle {vehicle} tied optimum",
                    "evidence-equal paths must abstain",
                )
            elif seg.status == "matched":
                add(
                    "anchor_and_align_matched",
                    seg.segment_id,
                    f"vehicle {vehicle} -> {seg.trip_id}",
                    "order-preserving match without greedy propagation",
                )
    if slice_name == "dst_fall_back":
        for e in prepared.episodes:
            if Reason02.BOUNDARY_FOLD_UNRESOLVED in e.reason_codes:
                add(
                    "dst_unresolved_preserved",
                    e.episode_id,
                    "fold-unresolved visit produced no physical timestamps",
                    "boundary logic must not invent folds",
                )
    if slice_name == "month_boundary" and primary:
        for rid, status in sorted(primary.resolution.dispositions.items()):
            if status == "no_candidate":
                add(
                    "month_censored",
                    rid,
                    "right-censored record stays no_candidate",
                    "censoring must not be repaired by refinement",
                )
    return rows


def _alignment_for(
    prepared: Spike02Prepared,
    refinements,
    anchor_results,
    *,
    unmatched_cost_q: int,
    max_optima: int,
    allow_many_to_one: bool,
) -> dict[str, AlignmentPathResult]:
    """One alignment problem per (vehicle, feed/date scope)."""
    out: dict[str, AlignmentPathResult] = {}
    segs_by_vehicle: dict[str, list] = {}
    for res in refinements:
        for seg in res.refined_segments:
            vehicle = prepared.s1.pings_by_id[seg.record_ids[0]].vehicle
            segs_by_vehicle.setdefault(vehicle, []).append(seg)
    for vehicle in sorted(segs_by_vehicle):
        segs = sorted(
            segs_by_vehicle[vehicle],
            key=lambda s: (s.start_instant_s, s.refined_segment_id),
        )
        candidate_costs: dict[str, dict[str, int]] = {}
        anchors: dict[str, str] = {}
        trip_starts: dict[str, int] = {}
        for seg in segs:
            result = anchor_results.get(seg.refined_segment_id)
            if result is None:
                continue
            costs: dict[str, int] = {}
            for cand in result.candidates:
                trip = cand.base.scheduled_trip_hypothesis
                if (
                    trip == NO_SCHEDULED_TRIP
                    or cand.base.service_date_hypothesis == UNRESOLVED_SERVICE_DATE
                    or cand.hard_invalid
                    or cand.qscore is None
                ):
                    continue
                cost = SCORE_SCALE - cand.qscore
                if trip not in costs or cost < costs[trip]:
                    costs[trip] = cost
                start = cand.base.feature_vector.get("seg_sched_start_residual_s")
                _ = start
            candidate_costs[seg.refined_segment_id] = costs
            if result.is_anchor and result.selected_key is not None:
                anchors[seg.refined_segment_id] = result.selected_key[4]
        # Ordered date-valid trips across the slice's GTFS scope.
        for date in sorted(prepared.s1.gtfs_by_date):
            gtfs = prepared.s1.gtfs_by_date[date]
            for row in gtfs.trip_intervals.iter_rows(named=True):
                trip_starts.setdefault(row["trip_id"], row["start_epoch_s"])
        used_trips = {t for costs in candidate_costs.values() for t in costs} | set(
            anchors.values()
        )
        trips = tuple(sorted(used_trips, key=lambda t: (trip_starts.get(t, 0), t)))
        problem = AlignmentInput(
            segment_ids=tuple(s.refined_segment_id for s in segs),
            trip_ids=trips,
            candidate_costs=candidate_costs,
            unmatched_cost=unmatched_cost_q,
            anchors=anchors,
            allow_many_to_one=allow_many_to_one,
        )
        out[vehicle] = align(problem, max_optima=max_optima)
    return out


def run_one_slice02(*, slice_name: str, cfg: dict, ttc_root: Path, run_id: str) -> dict:
    t_start = time.perf_counter()
    slice_cfg = cfg["slices"][slice_name]
    query, params_fn = SLICE_QUERIES[slice_name]
    base_path = ttc_root / cfg["score_contract"]["base_path"]
    ext_path = ttc_root / cfg["score_contract"]["ext_path"]
    if not base_path.exists():
        base_path = ttc_root.parent.parent / cfg["score_contract"]["base_path"]
        ext_path = ttc_root.parent.parent / cfg["score_contract"]["ext_path"]
    variants = cfg["score_contract"]["variants"]
    contracts = {v: load_spike02_contract(base_path, ext_path, v) for v in variants}

    prepared = prepare_spike02(
        name=slice_name,
        cfg=cfg,
        ttc_root=ttc_root,
        vehicle_query=query,
        vehicle_params=params_fn(slice_cfg),
    )
    is_primary = slice_name == "normal_day"

    # --- refinement (all variants) + determinism ---
    t0 = time.perf_counter()
    refinement_variants = cfg["refinement"]["variants"]
    refinements = {
        v: run_refinement(prepared, variant=v, cfg=cfg) for v in refinement_variants
    }
    ref_fps = {v: refinement_fingerprint(refinements[v]) for v in refinements}
    rng = random.Random(cfg["windows"]["shuffle_seed"])
    shuffled = list(range(len(prepared.runs)))
    rng.shuffle(shuffled)
    primary_ref = cfg["refinement"]["primary_variant"]
    ref_determinism = {
        "double_run_identical": refinement_fingerprint(
            run_refinement(prepared, variant=primary_ref, cfg=cfg)
        )
        == ref_fps[primary_ref],
        "shuffled_order_identical": refinement_fingerprint(
            run_refinement(prepared, variant=primary_ref, cfg=cfg, run_order=shuffled)
        )
        == ref_fps[primary_ref],
    }
    prepared.stage_timings_s["refinement"] = time.perf_counter() - t0

    roles = spanning_roles_for(refinements[primary_ref], prepared.episodes)

    # --- segment sets per variant ---
    segment_sets = {"v0": prepared.s1.segments_x4b}
    for v in refinement_variants:
        segment_sets[v] = refined_spike01_segments(prepared, refinements[v])

    # --- attachment battery ---
    t0 = time.perf_counter()
    primary_t_mid = cfg["attachment"]["primary_t_mid_s"]
    t_mid_values = (
        cfg["attachment"]["t_mid_variants_s"] if is_primary else [primary_t_mid]
    )
    contract_variants = (
        ["conservative", "forced_diagnostic"] if is_primary else ["conservative"]
    )
    attach_by_segset: dict[str, dict] = {}
    base_cands_by_segset: dict[str, dict] = {}
    for name, segs in segment_sets.items():
        refs = refinements.get(name, [])
        t_mids = t_mid_values if name == primary_ref else [primary_t_mid]
        runs_grid, base_cands = attachment_battery(
            prepared,
            segs,
            refs,
            roles if name == primary_ref else [],
            contracts,
            cfg=cfg,
            slice_cfg=slice_cfg,
            t_mid_values=t_mids,
            contract_variants=contract_variants,
        )
        attach_by_segset[name] = runs_grid
        base_cands_by_segset[name] = base_cands
    prepared.stage_timings_s["attachment"] = time.perf_counter() - t0

    # --- resolution-layout determinism on the primary combination ---
    primary_grid = attach_by_segset[primary_ref]
    primary_run = primary_grid[(primary_t_mid, "conservative")]
    determinism: dict[str, object] = {
        "refinement": ref_determinism,
        "resolution_primary": fingerprint_checksum(primary_run.resolution),
    }
    if is_primary:
        mismatches = []
        for label, hours, order in (
            ("reverse_24h", cfg["windows"]["window_hours"], "reverse"),
            ("shuffled_24h", cfg["windows"]["window_hours"], "shuffled"),
            ("forward_alt_hours", cfg["windows"]["window_hours_alt"], "forward"),
        ):
            grid, _ = attachment_battery(
                prepared,
                segment_sets[primary_ref],
                refinements[primary_ref],
                roles,
                contracts,
                cfg=cfg,
                slice_cfg=slice_cfg,
                t_mid_values=[primary_t_mid],
                contract_variants=["conservative"],
                window_hours=hours,
                order=order,
            )
            checksum = fingerprint_checksum(
                grid[(primary_t_mid, "conservative")].resolution
            )
            determinism[label] = checksum
            if checksum != determinism["resolution_primary"]:
                mismatches.append(label)
        determinism["mismatches"] = mismatches
        determinism["identical"] = not mismatches and all(ref_determinism.values())
    else:
        determinism["identical"] = all(ref_determinism.values())

    # --- V4: anchors + anchor-and-align (primary slice only) ---
    anchor_results: dict = {}
    anchor_results_nodir: dict = {}
    alignments: dict[str, AlignmentPathResult] = {}
    alignments_m2o: dict[str, AlignmentPathResult] = {}
    if is_primary:
        t0 = time.perf_counter()
        anchor_results = anchors_for_refinement(
            prepared,
            refinements[primary_ref],
            contracts["conservative"],
            cfg=cfg,
            slice_cfg=slice_cfg,
        )
        anchor_results_nodir = anchors_for_refinement(
            prepared,
            refinements[primary_ref],
            contracts["conservative"],
            cfg=cfg,
            slice_cfg=slice_cfg,
            enable_direction=False,
        )
        alignments = _alignment_for(
            prepared,
            refinements[primary_ref],
            anchor_results,
            unmatched_cost_q=cfg["alignment_paths"]["unmatched_cost_q"],
            max_optima=cfg["alignment_paths"]["max_optima"],
            allow_many_to_one=False,
        )
        if cfg["alignment_paths"]["run_many_to_one_variant"]:
            alignments_m2o = _alignment_for(
                prepared,
                refinements[primary_ref],
                anchor_results,
                unmatched_cost_q=cfg["alignment_paths"]["unmatched_cost_q"],
                max_optima=cfg["alignment_paths"]["max_optima"],
                allow_many_to_one=True,
            )
        align_double = _alignment_for(
            prepared,
            refinements[primary_ref],
            anchor_results,
            unmatched_cost_q=cfg["alignment_paths"]["unmatched_cost_q"],
            max_optima=cfg["alignment_paths"]["max_optima"],
            allow_many_to_one=False,
        )
        determinism["alignment_double_run_identical"] = alignment_metrics(
            align_double
        ) == alignment_metrics(alignments)
        prepared.stage_timings_s["anchors_alignment"] = time.perf_counter() - t0

    # --- metrics ---
    variant_metrics: dict[str, dict] = {}
    for name in segment_sets:
        block: dict = {}
        if name in refinements:
            block["segmentation"] = refinement_metrics(
                prepared.runs, refinements[name], prepared.episodes
            )
        else:
            segs = segment_sets[name]
            block["segmentation"] = {
                "refined_segments": len(segs),
                "refined_duration_s": {
                    "median": sorted(s.duration_s for s in segs)[len(segs) // 2]
                    if segs
                    else None,
                    "max": max((s.duration_s for s in segs), default=None),
                },
                "note": "Spike-01 x4b baseline segmentation (V0)",
            }
        for (t_mid, cvariant), run in sorted(attach_by_segset[name].items()):
            key = f"attachment_tmid{t_mid}_{cvariant}"
            block[key] = attachment_summary(run.resolution, run.multiplicity)
            block[key]["pruned_nearest_candidate_records"] = len(
                run.pruned_nearest_candidate_records
            )
        variant_metrics[name] = block
    variant_metrics["episodes"] = episode_metrics(prepared.episodes)
    if is_primary:
        variant_metrics["v4_anchors"] = anchor_metrics(anchor_results)
        variant_metrics["v4_anchors_no_direction"] = anchor_metrics(
            anchor_results_nodir
        )
        variant_metrics["v4_alignment_paths"] = alignment_metrics(alignments)
        if alignments_m2o:
            variant_metrics["v4_alignment_many_to_one"] = alignment_metrics(
                alignments_m2o
            )
        variant_metrics["multiplicity_v0_vs_primary"] = {
            "v0": multiplicity_before_after(
                base_cands_by_segset["v0"], base_cands_by_segset["v0"]
            )["before"],
            primary_ref: multiplicity_before_after(
                base_cands_by_segset[primary_ref],
                base_cands_by_segset[primary_ref],
            )["before"],
        }

    threshold_sensitivity = {}
    if is_primary:
        for (t_mid, cvariant), run in sorted(attach_by_segset[primary_ref].items()):
            threshold_sensitivity[f"tmid_{t_mid}_{cvariant}"] = {
                "multiplicity": run.multiplicity,
                "dispositions": attachment_summary(run.resolution, run.multiplicity)[
                    "dispositions"
                ],
                "pruned_candidates": len(run.prune_rows),
                "pruned_nearest_candidate_records": len(
                    run.pruned_nearest_candidate_records
                ),
            }

    ablation = {}
    if is_primary:
        ablation["direction_off_vs_on"] = {
            "anchors_on": anchor_metrics(anchor_results),
            "anchors_off": anchor_metrics(anchor_results_nodir),
        }
        ablation["apc_signal"] = {
            "v1_avl_only": refinement_metrics(
                prepared.runs, refinements["v1_avl_only"], prepared.episodes
            ),
            "v2_conditional": refinement_metrics(
                prepared.runs, refinements["v2_conditional"], prepared.episodes
            ),
            "vx_unconditional": refinement_metrics(
                prepared.runs, refinements["vx_unconditional"], prepared.episodes
            ),
        }
        # physical-only vs service-state cut intervals on shared episodes
        deltas = []
        v1_by_ep = {
            c.episode_id: c
            for c in _all_candidates(prepared, refinements, "v1_avl_only", cfg)
        }
        for cand in _all_candidates(prepared, refinements, "v2_conditional", cfg):
            other = v1_by_ep.get(cand.episode_id)
            if other is not None and cand.apc_precedence_used:
                deltas.append(
                    {
                        "episode_id": cand.episode_id,
                        "cut_start_shift_s": cand.cut_start_s - other.cut_start_s,
                        "cut_end_shift_s": cand.cut_end_s - other.cut_end_s,
                    }
                )
        ablation["service_vs_physical_boundary_shifts"] = deltas

    # --- artifacts ---
    run_dir = ttc_root / "artifacts" / f"experimental-r1-spike02-{run_id}-{slice_name}"
    normalized_dir = ttc_root / cfg["inputs"]["normalized_dir"]
    gtfs_dirs = {
        d: ttc_root / p for d, p in cfg["inputs"]["gtfs_artifact_dirs"].items()
    }
    run = Spike02Run(
        run_dir,
        config_hash=hash_config_text(yaml.safe_dump(cfg, sort_keys=True)),
        contract_hash=spike02_contract_hash(base_path, ext_path),
        input_hashes={
            "normalized_tree": input_tree_hash([normalized_dir]),
            **{
                f"gtfs_{d}": input_tree_hash([p])
                for d, p in sorted(gtfs_dirs.items())
                if p.exists()
            },
        },
        assumptions=ASSUMPTIONS02,
        unresolved_design_contracts=UNRESOLVED_DESIGN_CONTRACTS02,
        experimental_thresholds={
            "refinement": cfg["refinement"],
            "episodes": cfg["episodes"],
            "attachment": cfg["attachment"],
            "anchors": cfg["anchors"],
            "clusters": {
                k: v
                for k, v in cfg["clusters"].items()
                if k != "experimental_stop_code_hypotheses"
            },
        },
    )
    producer = "r1spike.run_spike02"

    run.write_parquet(
        pl.DataFrame(
            [
                {
                    "run_id": r.run_id,
                    "vehicle": r.vehicle,
                    "n_pings": len(r.record_ids),
                    "first_record_id": r.record_ids[0],
                    "last_record_id": r.record_ids[-1],
                    "start_instant_s": r.start_instant_s,
                    "end_instant_s": r.end_instant_s,
                    "duration_s": r.duration_s,
                    "time_uncertain": r.time_uncertain,
                    "n_unresolved": r.n_unresolved,
                    "local_dates": "|".join(r.local_dates),
                    "censored_start": r.boundary_censored_start,
                    "censored_end": r.boundary_censored_end,
                }
                for r in prepared.runs
            ],
            infer_schema_length=None,
        ),
        "x_provisional_activity_runs.parquet",
        grain="one row per provisional vehicle activity run",
        key=["run_id"],
        producer=producer,
    )
    run.write_parquet(
        pl.DataFrame(
            [
                {
                    "apc_source_record_id": e.apc_source_record_id,
                    "run_id": e.run_id,
                    "fold": e.fold,
                    "arrival_epoch_s": e.arrival_epoch_s,
                    "departure_epoch_s": e.departure_epoch_s,
                    "time_gap_to_run_s": e.time_gap_to_run_s,
                    "prev_ping_record_id": e.prev_ping_record_id,
                    "next_ping_record_id": e.next_ping_record_id,
                    "prev_gap_s": e.prev_gap_s,
                    "next_gap_s": e.next_gap_s,
                    "ping_to_stop_m": e.ping_to_stop_m,
                    "terminal_cluster_id": e.terminal_cluster_id,
                    "stop_in_cluster": e.stop_in_cluster,
                    "route_label_agrees": e.route_label_agrees,
                    "direction_label": e.direction_label,
                    "signal_state": e.signal_state,
                    "signal_quality": e.signal_quality,
                    "hard_valid": e.hard_valid,
                    "reason_codes": "|".join(e.reason_codes),
                }
                for e in prepared.edges
            ],
            infer_schema_length=None,
        ),
        "x_provisional_apc_alignment.parquet",
        grain="one row per (APC record, activity run, fold) hypothesis",
        key=["apc_source_record_id", "run_id", "fold"],
        producer=producer,
    )
    run.write_parquet(
        pl.DataFrame(
            [
                {
                    "cluster_id": c.cluster_id,
                    "name_hypothesis": c.name_hypothesis,
                    "feed_id": c.feed_id,
                    "service_dates": "|".join(c.service_dates),
                    "geofence_radius_m": c.geofence_radius_m,
                    "n_member_stops": len(c.member_stop_ids),
                    "member_stop_codes": "|".join(c.member_stop_codes),
                    "pattern_endpoint_route_ids": "|".join(
                        c.pattern_endpoint_route_ids
                    ),
                    "evidence_sources": "|".join(c.evidence_sources),
                    "status": c.status.value,
                    "config_version": c.config_version,
                    "reason_codes": "|".join(c.reason_codes),
                }
                for c in prepared.clusters
            ],
            infer_schema_length=None,
        ),
        "x_terminal_clusters.parquet",
        grain="one row per versioned terminal cluster hypothesis",
        key=["cluster_id"],
        producer=producer,
    )
    run.write_parquet(
        pl.DataFrame(_episode_rows(prepared), infer_schema_length=None),
        "x_terminal_episode_candidates.parquet",
        grain="one row per terminal/short-turn episode candidate",
        key=["episode_id"],
        producer=producer,
    )
    run.write_parquet(
        pl.DataFrame(_time_hypothesis_rows(prepared), infer_schema_length=None),
        "x_terminal_time_hypotheses.parquet",
        grain="one row per (episode, timestamp hypothesis)",
        key=["episode_id", "hypothesis"],
        producer=producer,
    )
    boundary_rows = []
    decision_rows = []
    for v in refinement_variants:
        for cand in _all_candidates(prepared, refinements, v, cfg):
            boundary_rows.append(
                {
                    "boundary_id": cand.boundary_id,
                    "refinement_variant": v,
                    "run_id": cand.run_id,
                    "episode_id": cand.episode_id,
                    "source": cand.source,
                    "cut_start_s": cand.cut_start_s,
                    "cut_end_s": cand.cut_end_s,
                    "evidence_families": "|".join(cand.evidence_families),
                    "apc_precedence_used": cand.apc_precedence_used,
                    "quality": cand.quality.value if cand.quality else None,
                    "score_q": cand.score_q,
                    "reason_codes": "|".join(cand.reason_codes),
                }
            )
        for res in refinements[v]:
            for d in res.boundary_decisions:
                decision_rows.append(
                    {
                        "boundary_id": d.boundary_id,
                        "refinement_variant": v,
                        "run_id": d.run_id,
                        "status": d.status.value,
                        "score_q": d.score_q,
                        "margin_q": d.margin_q,
                        "reason_codes": "|".join(d.reason_codes),
                    }
                )
    run.write_parquet(
        pl.DataFrame(boundary_rows, infer_schema_length=None),
        "x_boundary_candidates.parquet",
        grain="one row per boundary candidate per refinement variant",
        key=["boundary_id", "refinement_variant"],
        producer=producer,
    )
    run.write_parquet(
        pl.DataFrame(decision_rows, infer_schema_length=None),
        "x_boundary_decisions.parquet",
        grain="one row per boundary decision per refinement variant",
        key=["boundary_id", "refinement_variant"],
        producer=producer,
    )
    run.write_parquet(
        pl.DataFrame(
            [
                {
                    "turnaround_id": t.turnaround_id,
                    "refinement_variant": res.variant,
                    "run_id": t.run_id,
                    "episode_id": t.episode_id,
                    "cluster_id": t.cluster_id,
                    "start_instant_s": t.start_instant_s,
                    "end_instant_s": t.end_instant_s,
                    "n_pings": len(t.record_ids),
                }
                for v in refinement_variants
                for res in refinements[v]
                for t in res.turnarounds
            ],
            infer_schema_length=None,
        ),
        "x_turnaround_intervals.parquet",
        grain="one row per turnaround interval per refinement variant",
        key=["turnaround_id", "refinement_variant"],
        producer=producer,
    )
    run.write_parquet(
        pl.DataFrame(
            [
                {
                    "refined_segment_id": s.refined_segment_id,
                    "refinement_variant": res.variant,
                    "run_id": s.run_id,
                    "n_pings": len(s.record_ids),
                    "start_instant_s": s.start_instant_s,
                    "end_instant_s": s.end_instant_s,
                    "duration_s": s.duration_s,
                    "segment_type": s.segment_type.value,
                    "boundary_id_before": s.boundary_id_before,
                    "boundary_id_after": s.boundary_id_after,
                    "time_uncertain": s.time_uncertain,
                    "local_dates": "|".join(s.local_dates),
                    "censored_start": s.censored_start,
                    "censored_end": s.censored_end,
                }
                for v in refinement_variants
                for res in refinements[v]
                for s in res.refined_segments
            ],
            infer_schema_length=None,
        ),
        "x_refined_segments.parquet",
        grain="one row per refined segment per refinement variant",
        key=["refined_segment_id", "refinement_variant"],
        producer=producer,
    )
    all_membership = []
    for v in refinement_variants:
        all_membership.extend(membership_rows(refinements[v]))
    run.write_parquet(
        pl.DataFrame(all_membership, infer_schema_length=None),
        "x_refined_segment_membership.parquet",
        grain="one row per (refinement variant, AVL ping)",
        key=["refinement_variant", "avl_record_id"],
        producer=producer,
    )
    run.write_parquet(
        pl.DataFrame(
            [
                {
                    "apc_source_record_id": r.apc_source_record_id,
                    "refinement_variant": primary_ref,
                    "run_id": r.run_id,
                    "episode_id": r.episode_id,
                    "boundary_role": r.boundary_role,
                    "arrival_role_hypothesis": r.arrival_role_hypothesis,
                    "departure_role_hypothesis": r.departure_role_hypothesis,
                    "arrival_segment_candidate": r.arrival_segment_candidate,
                    "departure_segment_candidate": r.departure_segment_candidate,
                    "dual_role": r.dual_role,
                    "reason_codes": "|".join(r.reason_codes),
                }
                for r in roles
            ],
            infer_schema_length=None,
        ),
        "x_spanning_apc_roles.parquet",
        grain="one row per boundary-spanning APC record role assignment",
        key=["apc_source_record_id", "episode_id"],
        producer=producer,
    )
    if primary_run.resolution.scored_apc_rows:
        run.write_parquet(
            pl.DataFrame(
                primary_run.resolution.scored_apc_rows, infer_schema_length=None
            ),
            "x_final_apc_candidates.parquet",
            grain="one row per final attachment hypothesis (primary combo)",
            key=[
                "apc_source_record_id",
                "time_fold_hypothesis",
                "service_date_hypothesis",
                "feed_id",
                "route_mapping_hypothesis",
                "stop_mapping_hypothesis",
                "candidate_segment_id",
            ],
            producer=producer,
        )
    run.write_parquet(
        pl.DataFrame(
            [
                {
                    "apc_source_record_id": rid,
                    "status": d.status,
                    "selected_key": (
                        "|".join(str(p) for p in d.selected_key)
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
                for rid, d in sorted(primary_run.resolution.apc_decisions.items())
            ],
            infer_schema_length=None,
        ),
        "x_final_apc_decisions.parquet",
        grain="one row per APC record final decision (primary combo)",
        key=["apc_source_record_id"],
        producer=producer,
    )
    if anchor_results:
        anchor_cand_rows = []
        for sid, result in sorted(anchor_results.items()):
            for cand in result.candidates:
                row = {
                    "segment_id": sid,
                    "service_date_hypothesis": (cand.base.service_date_hypothesis),
                    "route_mapping_hypothesis": (cand.base.route_mapping_hypothesis),
                    "scheduled_trip_hypothesis": (cand.base.scheduled_trip_hypothesis),
                    "polarity": cand.polarity,
                    "qscore": cand.qscore,
                    "hard_invalid": cand.hard_invalid,
                    "n_aligned": cand.n_aligned,
                    "n_observed": cand.n_observed,
                    "n_alignment_optima": cand.n_alignment_optima,
                    "reason_codes": "|".join(cand.reason_codes),
                }
                for feat, value in sorted(cand.base.feature_vector.items()):
                    if feat.startswith("anchor_"):
                        row[f"f_{feat}"] = value
                anchor_cand_rows.append(row)
        run.write_parquet(
            pl.DataFrame(anchor_cand_rows, infer_schema_length=None),
            "x_gtfs_anchor_candidates.parquet",
            grain="one row per (segment, trip, polarity) anchor candidate",
            key=["segment_id", "scheduled_trip_hypothesis", "polarity"],
            producer=producer,
        )
        run.write_parquet(
            pl.DataFrame(
                [
                    {
                        "segment_id": sid,
                        "status": r.status,
                        "selected_key": (
                            "|".join(str(p) for p in r.selected_key)
                            if r.selected_key
                            else None
                        ),
                        "best_q": r.best_q,
                        "margin_q": r.margin_q,
                        "is_anchor": r.is_anchor,
                        "reason_codes": "|".join(r.reason_codes),
                    }
                    for sid, r in sorted(anchor_results.items())
                ],
                infer_schema_length=None,
            ),
            "x_gtfs_anchors.parquet",
            grain="one row per refined segment anchor decision",
            key=["segment_id"],
            producer=producer,
        )
    if alignments:
        run.write_parquet(
            pl.DataFrame(
                [
                    {
                        "vehicle": vehicle,
                        "segment_id": seg.segment_id,
                        "status": seg.status,
                        "trip_id": seg.trip_id,
                        "optimal_cost": res.optimal_cost,
                        "path_margin": res.path_margin,
                        "n_optimal": res.n_optimal,
                        "truncated": res.truncated,
                        "many_to_one": False,
                        "reason_codes": "|".join(seg.reason_codes),
                    }
                    for vehicle, res in sorted(alignments.items())
                    for seg in res.segments
                ]
                + [
                    {
                        "vehicle": vehicle,
                        "segment_id": seg.segment_id,
                        "status": seg.status,
                        "trip_id": seg.trip_id,
                        "optimal_cost": res.optimal_cost,
                        "path_margin": res.path_margin,
                        "n_optimal": res.n_optimal,
                        "truncated": res.truncated,
                        "many_to_one": True,
                        "reason_codes": "|".join(seg.reason_codes),
                    }
                    for vehicle, res in sorted(alignments_m2o.items())
                    for seg in res.segments
                ],
                infer_schema_length=None,
            ),
            "x_anchor_alignment_paths.parquet",
            grain="one row per (vehicle, segment, monotonicity variant)",
            key=["vehicle", "segment_id", "many_to_one"],
            producer=producer,
        )
    run.write_parquet(
        pl.DataFrame(
            [
                {
                    "apc_source_record_id": rid,
                    "disposition": status,
                    "reason_codes": "|".join(
                        primary_run.resolution.disposition_reasons.get(rid, ())
                    ),
                }
                for rid, status in sorted(primary_run.resolution.dispositions.items())
            ],
            infer_schema_length=None,
        ),
        "x_record_dispositions.parquet",
        grain="exactly one row per normalized APC record in the slice",
        key=["apc_source_record_id"],
        producer=producer,
    )
    edge_rows = _edge_case_rows(
        prepared,
        refinements[primary_ref],
        primary_grid,
        anchor_results,
        alignments,
        slice_name=slice_name,
        primary_t_mid=primary_t_mid,
    )
    if edge_rows:
        run.write_parquet(
            pl.DataFrame(edge_rows, infer_schema_length=None),
            "x_edge_case_inventory.parquet",
            grain="one row per (category, subject)",
            key=["slice", "category", "subject_id"],
            producer=producer,
        )

    run.write_json({"matrix": EXPERIMENT_MATRIX}, "experiment_matrix.json")
    run.write_json(variant_metrics, "variant_metrics.json")
    run.write_json(threshold_sensitivity, "threshold_sensitivity.json")
    run.write_json(ablation, "ablation_report.json")
    run.write_json(determinism, "determinism_report.json")
    run.write_json(
        {
            "refinement_fingerprints": ref_fps,
            "resolution_primary": determinism["resolution_primary"],
        },
        "logical_checksums.json",
    )
    run.write_json(
        {
            "total_wall_s": round(time.perf_counter() - t_start, 3),
            "peak_rss_mib": round(peak_rss_mib(), 1),
            "stage_timings_s": {
                k: round(v, 3) for k, v in prepared.stage_timings_s.items()
            },
        },
        "performance_report.json",
    )
    run.write_json(
        {"contract": contracts[cfg["score_contract"]["primary_variant"]].raw},
        "spike02_score_contract.json",
    )
    if is_primary:
        r1c_dir = ttc_root / (cfg["inputs"]["spike01_r1c_prefix"] + slice_name)
        if r1c_dir.exists():
            run.write_json(
                reproduce_r1c_baseline(r1c_dir), "baseline_reproduction.json"
            )
        run.write_json(
            _wilson_report(
                prepared,
                refinements[primary_ref],
                roles,
                primary_grid,
                anchor_results,
                alignments,
                primary_t_mid=primary_t_mid,
            ),
            "wilson_case_report.json",
        )
    run.write_manifest(
        {
            "slice": slice_name,
            "slice_config": slice_cfg,
            "vehicle_query": prepared.s1.vehicle_query,
            "vehicles": prepared.s1.vehicles,
            "refinement_variants": refinement_variants,
            "primary_refinement_variant": primary_ref,
            "primary_t_mid_s": primary_t_mid,
            "determinism_identical": determinism["identical"],
        }
    )
    return {
        "slice": slice_name,
        "run_dir": str(run_dir),
        "determinism_identical": determinism["identical"],
        "variant_metrics": variant_metrics,
    }


def _all_candidates(prepared: Spike02Prepared, refinements, variant: str, cfg):
    ref_cfg = RefinementConfig(
        score_bar_q=cfg["refinement"]["score_bar_q"],
        margin_q=cfg["refinement"]["margin_q"],
    )
    out = []
    for run in sorted(prepared.runs, key=lambda r: r.run_id):
        out.extend(
            propose_boundaries(
                run,
                ordered_run_pings(run, prepared.s1.pings_by_id),
                [e for e in prepared.episodes if e.run_id == run.run_id],
                variant=variant,
                cfg=ref_cfg,
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="R1 Spike-02 runner (boundary refinement; non-publishable)"
    )
    parser.add_argument("--slice", choices=[*SLICE_QUERIES, "all"], default="all")
    parser.add_argument("--run-id", default="dev")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("experiments/r1_design_spike/config/spike02_config.yaml"),
    )
    parser.add_argument("--ttc-root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    slices = list(SLICE_QUERIES) if args.slice == "all" else [args.slice]
    for slice_name in slices:
        summary = run_one_slice02(
            slice_name=slice_name,
            cfg=cfg,
            ttc_root=args.ttc_root.resolve(),
            run_id=args.run_id,
        )
        print(
            f"[{slice_name}] run_dir={summary['run_dir']} "
            f"determinism_identical={summary['determinism_identical']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
