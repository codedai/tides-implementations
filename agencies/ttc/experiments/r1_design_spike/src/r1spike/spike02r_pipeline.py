"""Spike-02R Phase-A pipeline: repaired kernels over the known slices.

Builds on the frozen Spike-02 preparation (runs, clusters, edges,
signals, along-progress) and runs, per slice:

- V1R true AVL-only chunks (GTFS-only clusters, no APC anywhere);
- V2R-STRUCTURAL and V2R-ACTIVITY chunks (separate identities);
- cross-fitted V2R-ACTIVITY (fold-A boundaries / fold-B anchor visits
  and vice versa);
- evidence-dependency edges and reuse quantification;
- anchor scoring v2 (complete keys, corrected regimes, dual margins);
- dual-margin re-evaluation of Stage-2 attachment decisions (the DST
  unique-survivor battery);
- revocable alignment vs the frozen hard-pinned aligner;
- fragment roles and stitching.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .anchor_alignment import AlignmentInput, align
from .boundary_v2 import BoundaryV2Config
from .candidate_generation import ApcRecord
from .censorship_margins import (
    MarginCandidate,
    classify_candidate,
    dual_margin_decide,
    score_ignoring_hard_blocks,
)
from .contracts import (
    NO_SCHEDULED_TRIP,
    UNRESOLVED_SERVICE_DATE,
    EvidenceRegime,
)
from .crossfit import assign_folds
from .fragments_v2 import PairContext, StitchConfig, classify_roles, stitch_run
from .gtfs_anchors import AnchorConfig, anchor_candidates_for_segment
from .revocable_alignment import RevocableInput, solve_revocable
from .scoring import ScoreContract
from .segmentation_baselines import Segment
from .spike02_pipeline import Spike02Prepared, derive_signals
from .spike02r_contracts import (
    CandidateKey,
    CensorClass,
    ChunkRecord,
    ChunkRole,
)
from .terminal_clusters import build_terminal_clusters
from .terminal_episodes import EpisodeConfig
from .true_avl_only import (
    PROFILE_APC_ACTIVITY,
    PROFILE_APC_STRUCTURAL,
    PROFILE_AVL,
    build_profile_chunks,
)


@dataclass
class ProfileResult:
    profile: str
    chunks: list[ChunkRecord]
    candidates: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    episodes_by_run: dict = field(default_factory=dict)

    def fingerprint(self) -> str:
        payload = {
            "chunks": [(c.chunk_id, c.record_ids, c.role.value) for c in self.chunks],
            "decisions": [
                (d.candidate_id, d.status, d.selected, d.survival_margin_q)
                for d in self.decisions
            ],
        }
        return hashlib.sha256(json.dumps(payload, default=str).encode()).hexdigest()


def structural_signals(prepared: Spike02Prepared, flags_by_id: dict) -> dict:
    """Signals with EVERY passenger-derived value withheld: counts and
    AvgSample are never seen, so positivity states cannot exist."""
    neutered = {
        rid: {
            **flags,
            "boarding": None,
            "alighting": None,
            "avg_sample": None,
            # count-derived flag: its SIGN encodes a passenger value
            "_flag_negative_count": False,
        }
        for rid, flags in flags_by_id.items()
    }
    return derive_signals(prepared.s1, neutered)


def build_all_profiles(
    prepared: Spike02Prepared,
    flags_by_id: dict,
    *,
    episode_cfg: EpisodeConfig,
    boundary_cfg: BoundaryV2Config,
    gtfs_only_clusters=None,
) -> dict[str, ProfileResult]:
    if gtfs_only_clusters is None:
        gtfs_only_clusters = build_terminal_clusters(
            prepared.s1.gtfs_by_date,
            geofence_radius_m=prepared.clusters[0].geofence_radius_m
            if prepared.clusters
            else 150.0,
            merge_radius_m=250.0,
            config_version="xtc2r-gtfs-only",
            extra_stop_code_hypotheses=None,
        )
    out: dict[str, ProfileResult] = {}
    chunks, cands, decs, eps = build_profile_chunks(
        prepared.runs,
        prepared.s1.pings_by_id,
        gtfs_only_clusters,
        profile=PROFILE_AVL,
        episode_cfg=episode_cfg,
        boundary_cfg=boundary_cfg,
        along_by_id=prepared.along_by_id,
    )
    out[PROFILE_AVL] = ProfileResult(PROFILE_AVL, chunks, cands, decs, eps)

    s_signals = structural_signals(prepared, flags_by_id)
    chunks, cands, decs, eps = build_profile_chunks(
        prepared.runs,
        prepared.s1.pings_by_id,
        prepared.clusters,
        profile=PROFILE_APC_STRUCTURAL,
        episode_cfg=episode_cfg,
        boundary_cfg=boundary_cfg,
        along_by_id=prepared.along_by_id,
        edges=prepared.edges,
        signals_by_id=s_signals,
    )
    out[PROFILE_APC_STRUCTURAL] = ProfileResult(
        PROFILE_APC_STRUCTURAL, chunks, cands, decs, eps
    )

    chunks, cands, decs, eps = build_profile_chunks(
        prepared.runs,
        prepared.s1.pings_by_id,
        prepared.clusters,
        profile=PROFILE_APC_ACTIVITY,
        episode_cfg=episode_cfg,
        boundary_cfg=boundary_cfg,
        along_by_id=prepared.along_by_id,
        edges=prepared.edges,
        signals_by_id=prepared.signals_by_id,
    )
    out[PROFILE_APC_ACTIVITY] = ProfileResult(
        PROFILE_APC_ACTIVITY, chunks, cands, decs, eps
    )
    return out


def crossfit_profiles(
    prepared: Spike02Prepared,
    flags_by_id: dict,
    *,
    episode_cfg: EpisodeConfig,
    boundary_cfg: BoundaryV2Config,
) -> dict[str, ProfileResult]:
    """V2R-ACTIVITY with boundaries built from ONE fold's APC records
    only (edges and signals restricted), for each fold."""
    folds = assign_folds(prepared.s1.apc_records)
    out: dict[str, ProfileResult] = {}
    for fold in ("A", "B"):
        keep = {rid for rid, f in folds.items() if f == fold}
        edges = [e for e in prepared.edges if e.apc_source_record_id in keep]
        signals = {rid: s for rid, s in prepared.signals_by_id.items() if rid in keep}
        chunks, cands, decs, eps = build_profile_chunks(
            prepared.runs,
            prepared.s1.pings_by_id,
            prepared.clusters,
            profile=PROFILE_APC_ACTIVITY,
            episode_cfg=episode_cfg,
            boundary_cfg=boundary_cfg,
            along_by_id=prepared.along_by_id,
            edges=edges,
            signals_by_id=signals,
        )
        out[fold] = ProfileResult(
            f"{PROFILE_APC_ACTIVITY}-boundaries-fold-{fold}",
            chunks,
            cands,
            decs,
            eps,
        )
    return out


def chunk_as_segment(chunk: ChunkRecord, pings_by_id) -> Segment:
    vehicle = pings_by_id[chunk.record_ids[0]].vehicle
    route_counts: dict[str, int] = {}
    for rid in chunk.record_ids:
        lbl = pings_by_id[rid].route_label
        if lbl is not None:
            route_counts[lbl] = route_counts.get(lbl, 0) + 1
    return Segment(
        segment_id=chunk.chunk_id,
        variant=f"x02r-{chunk.profile}",
        vehicle=vehicle,
        record_ids=chunk.record_ids,
        start_instant_s=chunk.start_instant_s,
        end_instant_s=chunk.end_instant_s,
        time_uncertain=False,
        n_unresolved=0,
        local_dates=tuple(
            sorted({pings_by_id[r].local_date for r in chunk.record_ids})
        ),
        route_label_counts=route_counts,
    )


def anchor_v2_for_chunks(
    prepared: Spike02Prepared,
    chunks: list[ChunkRecord],
    records: list[ApcRecord],
    contract: ScoreContract,
    *,
    slice_cfg: dict,
    cfg: dict,
    geometry_lookup,
) -> dict[str, dict]:
    """Complete-key anchor candidates + dual-margin decisions per chunk.

    Regime repair: any candidate whose feature vector carries APC-derived
    per-stop/terminal features is scored APC_EVIDENCE (never AVL_ONLY).
    Visits come ONLY from ``records`` (the caller controls the fold).
    Returns chunk_id -> {decision, candidates, visit_records}.
    """
    anchor_cfg = AnchorConfig(
        stop_align_skip_cost=cfg["anchors"]["stop_align_skip_cost"],
        stop_residual_cap_s=cfg["anchors"]["stop_residual_cap_s"],
        visit_pad_s=cfg["anchors"]["visit_pad_s"],
        min_aligned_stops_for_anchor=cfg["anchors"]["min_aligned_stops_for_anchor"],
        enable_direction=False,  # direction weight 0 in primary (spec 21)
        shortlist_overlap_required=True,
    )
    thresholds = contract.thresholds["segment_selection"]
    null_q = contract.null_scores["segment_selection"]["XNULL:UNRESOLVED_SERVICE_DATE"]
    out: dict[str, dict] = {}
    for chunk in sorted(chunks, key=lambda c: c.chunk_id):
        if chunk.role == ChunkRole.TERMINAL_TURNAROUND:
            continue
        seg = chunk_as_segment(chunk, prepared.s1.pings_by_id)
        cands = anchor_candidates_for_segment(
            seg,
            records,
            prepared.s1.pings_by_id,
            prepared.s1.gtfs_by_date,
            geometry_lookup,
            (None, None),
            contract,
            service_date_candidates=slice_cfg["service_date_candidates"],
            schedule_window_expansion_s=cfg["candidates"][
                "schedule_window_expansion_s"
            ],
            soft_bound_m=cfg["geometry"]["soft_bound_m"],
            cfg=anchor_cfg,
            clusters=prepared.clusters,
        )
        margin_cands: list[MarginCandidate] = []
        rows = []
        # The visits ACTUALLY usable by per-stop scoring (unique-mapped,
        # fold-resolved, in-window), per candidate feed scope.
        from .gtfs_anchors import observed_visits_for_segment

        visit_ids: set[str] = set()
        for date in slice_cfg["service_date_candidates"]:
            gtfs = prepared.s1.gtfs_by_date.get(date)
            if gtfs is None:
                continue
            visits, _exf, _exs = observed_visits_for_segment(
                seg, records, gtfs, pad_s=anchor_cfg.visit_pad_s
            )
            visit_ids.update(v.visit_id for v in visits)
        for cand in cands:
            base = cand.base
            key = CandidateKey(
                service_date=base.service_date_hypothesis,
                feed_id=base.feed_id,
                route_mapping_id=base.route_mapping_hypothesis,
                scheduled_trip_id=(
                    None
                    if base.scheduled_trip_hypothesis == NO_SCHEDULED_TRIP
                    else base.scheduled_trip_hypothesis
                ),
                polarity_hypothesis_id=cand.polarity,
            ).serialize()
            is_null = base.service_date_hypothesis == UNRESOLVED_SERVICE_DATE
            has_apc_features = any(f.startswith("anchor_") for f in base.feature_vector)
            regime = (
                EvidenceRegime.APC_EVIDENCE
                if has_apc_features
                else EvidenceRegime.AVL_ONLY
            )
            if is_null:
                q = null_q
                censor = CensorClass.HARD_VALID
            elif cand.hard_invalid:
                censor = classify_candidate(cand.reason_codes, "hard_invalid")
                q = score_ignoring_hard_blocks(
                    contract, "segment_selection", base.feature_vector, regime
                )
            else:
                q, _fam, _r, invalid = contract.score_candidate(
                    "segment_selection", dict(base.feature_vector), regime
                )
                censor = classify_candidate(
                    cand.reason_codes,
                    "hard_invalid" if invalid else "valid",
                )
            margin_cands.append(
                MarginCandidate(key=key, qscore=q, censor_class=censor, is_null=is_null)
            )
            rows.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "candidate_key": key,
                    "qscore": q,
                    "regime": regime.value,
                    "censor_class": censor.value,
                    "n_aligned": cand.n_aligned,
                    "n_observed": cand.n_observed,
                }
            )

        decision = dual_margin_decide(
            chunk.chunk_id,
            margin_cands,
            absolute_q=thresholds.absolute_q,
            survival_margin_bar_q=thresholds.margin_q,
            evidence_margin_bar_q=thresholds.margin_q,
        )
        out[chunk.chunk_id] = {
            "decision": decision,
            "candidate_rows": rows,
            "visit_records": sorted(visit_ids),
        }
    return out


def reevaluate_attachment_dual_margins(
    resolution,
    contract: ScoreContract,
) -> tuple[dict[str, object], list[dict]]:
    """Re-evaluate every Stage-2 attachment decision with dual margins,
    scoring censored alternatives with hard blocks clamped. Returns
    (record_id -> DualMarginDecision, censorship rows)."""
    thresholds = contract.thresholds["apc_attachment"]
    null_q = contract.null_scores["apc_attachment"]["XNULL:NO_ATTACHMENT"]
    by_record: dict[str, list[dict]] = {}
    for row in resolution.scored_apc_rows:
        by_record.setdefault(row["apc_source_record_id"], []).append(row)
    decisions: dict[str, object] = {}
    censor_rows: list[dict] = []
    for rid in sorted(by_record):
        cands: list[MarginCandidate] = []
        for row in by_record[rid]:
            if row["candidate_segment_id"] == "XNULL:NO_ATTACHMENT":
                cands.append(
                    MarginCandidate(
                        key="XNULL:NO_ATTACHMENT",
                        qscore=null_q,
                        censor_class=CensorClass.HARD_VALID,
                        is_null=True,
                    )
                )
                continue
            reasons = tuple(r for r in (row.get("reason_codes") or "").split("|") if r)
            fv = {
                k[2:]: v for k, v in row.items() if k.startswith("f_") and v is not None
            }
            key = "|".join(
                str(row.get(c))
                for c in (
                    "service_date_hypothesis",
                    "feed_id",
                    "route_mapping_hypothesis",
                    "stop_mapping_hypothesis",
                    "candidate_segment_id",
                )
            )
            if row["score_kind"] == "scored" and row["qscore"] is not None:
                censor = classify_candidate(reasons, "valid")
                q = row["qscore"]
            else:
                censor = classify_candidate(reasons, "hard_invalid")
                q = score_ignoring_hard_blocks(
                    contract,
                    "apc_attachment",
                    fv,
                    EvidenceRegime.APC_EVIDENCE,
                )
            cands.append(MarginCandidate(key=key, qscore=q, censor_class=censor))
            censor_rows.append(
                {
                    "apc_source_record_id": rid,
                    "candidate_key": key,
                    "censor_class": censor.value,
                    "qscore": q,
                }
            )
        decisions[rid] = dual_margin_decide(
            rid,
            cands,
            absolute_q=thresholds.absolute_q,
            survival_margin_bar_q=thresholds.margin_q,
            evidence_margin_bar_q=thresholds.margin_q,
        )
    return decisions, censor_rows


def revocable_vs_pinned(
    anchor_results: dict[str, dict],
    chunks: list[ChunkRecord],
    prepared: Spike02Prepared,
    *,
    unmatched_cost_q: int,
    break_penalty_q: int,
    scale: int = 1_000_000,
):
    """Build one revocable alignment problem per vehicle from the
    anchor-v2 results, plus the equivalent hard-pinned problem for
    comparison."""
    from .fixedpoint import SCORE_SCALE

    _ = scale
    by_vehicle: dict[str, list[ChunkRecord]] = {}
    for c in chunks:
        if c.role == ChunkRole.TERMINAL_TURNAROUND:
            continue
        v = prepared.s1.pings_by_id[c.record_ids[0]].vehicle
        by_vehicle.setdefault(v, []).append(c)

    results = {}
    for vehicle in sorted(by_vehicle):
        ordered = sorted(
            by_vehicle[vehicle], key=lambda c: (c.start_instant_s, c.chunk_id)
        )
        seg_ids = tuple(c.chunk_id for c in ordered)
        key_start: dict[str, int] = {}
        costs: dict[str, dict[str, int]] = {}
        anchors: dict[str, str] = {}
        for c in ordered:
            blk = anchor_results.get(c.chunk_id)
            if blk is None:
                continue
            kmap: dict[str, int] = {}
            for row in blk["candidate_rows"]:
                key = row["candidate_key"]
                if key.endswith("||") or "|" not in key:
                    continue
                parts = key.split("|")
                if not parts[3]:
                    continue  # no-scheduled-trip alternative: not a DP node
                if row["censor_class"] != CensorClass.HARD_VALID.value:
                    continue
                cost = SCORE_SCALE - row["qscore"]
                if key not in kmap or cost < kmap[key]:
                    kmap[key] = cost
                key_start.setdefault(key, 0)
            costs[c.chunk_id] = kmap
            d = blk["decision"]
            if d.status == "accepted" and d.best_key and d.best_key in kmap:
                anchors[c.chunk_id] = d.best_key
        # order keys by scheduled start where known (fall back to key)
        gtfs_starts: dict[str, int] = {}
        for date, g in prepared.s1.gtfs_by_date.items():
            for row in g.trip_intervals.iter_rows(named=True):
                gtfs_starts[(date, row["trip_id"])] = row["start_epoch_s"]

        def start_of(key: str, starts=gtfs_starts) -> tuple:
            parts = key.split("|")
            return (starts.get((parts[0], parts[3]), 0), key)

        key_order = tuple(sorted(key_start, key=start_of))
        inp = RevocableInput(
            component_id=f"{vehicle}",
            segment_ids=seg_ids,
            key_order=key_order,
            candidate_costs=costs,
            unmatched_cost=unmatched_cost_q,
            anchor_prefs=anchors,
            anchor_break_penalty=break_penalty_q,
        )
        stability = solve_revocable(inp)
        try:
            pinned = align(
                AlignmentInput(
                    segment_ids=seg_ids,
                    trip_ids=key_order,
                    candidate_costs=costs,
                    unmatched_cost=unmatched_cost_q,
                    anchors=anchors,
                    allow_many_to_one=False,
                ),
                max_optima=64,
            )
        except ValueError:
            # The hard-pinning pathology the repair targets: two anchors
            # preferring order-conflicting complete keys make the pinned
            # DP infeasible. Recorded as an outcome, never patched over.
            pinned = None
        results[vehicle] = {"revocable": stability, "pinned": pinned}
    return results


def pair_contexts_for_run(
    run_chunks: list[ChunkRecord],
    cut_candidates,
    anchor_results: dict[str, dict],
    along_by_id: dict[str, float],
) -> tuple[list[ChunkRecord], list[PairContext]]:
    """Non-turnaround chunks of one run in order, plus junction
    contexts. A junction is "crossed" when ANY applied cut interval or
    turnaround chunk lies between the two chunks (adversarial-review
    repair: the earlier service-derived-only test let chains span real
    terminal visits)."""
    all_cuts = [
        (c.candidate_start_s, c.candidate_end_s) for c in cut_candidates
    ]
    ta_spans = [
        (c.start_instant_s, c.end_instant_s)
        for c in run_chunks
        if c.role == ChunkRole.TERMINAL_TURNAROUND
    ]
    separators = all_cuts + ta_spans
    seq = [c for c in run_chunks if c.role != ChunkRole.TERMINAL_TURNAROUND]
    seq.sort(key=lambda c: c.start_instant_s)
    contexts: list[PairContext] = []
    for a, b in zip(seq, seq[1:], strict=False):
        gap = b.start_instant_s - a.end_instant_s
        crossed = any(
            a.end_instant_s <= s and e <= b.start_instant_s
            for s, e in separators
        )
        ka = anchor_results.get(a.chunk_id, {}).get("decision")
        kb = anchor_results.get(b.chunk_id, {}).get("decision")
        same = bool(
            ka is not None
            and kb is not None
            and ka.best_key
            and ka.best_key == kb.best_key
        )
        da = _direction_sign(a, along_by_id)
        db = _direction_sign(b, along_by_id)
        contexts.append(
            PairContext(
                gap_s=gap,
                crossed_service_turnaround=crossed,
                same_top_key=same,
                direction_consistent=(None if da is None or db is None else da == db),
            )
        )
    return seq, contexts


def _direction_sign(chunk: ChunkRecord, along_by_id) -> bool | None:
    vals = [along_by_id[r] for r in chunk.record_ids if r in along_by_id]
    if len(vals) < 2 or vals[-1] == vals[0]:
        return None
    return vals[-1] > vals[0]


def stitch_profile(
    profile_result: ProfileResult,
    anchor_results: dict[str, dict],
    along_by_id: dict[str, float],
    cfg: StitchConfig,
):
    role_chunks: list[ChunkRecord] = []
    decisions = []
    hyps = []
    by_run: dict[str, list[ChunkRecord]] = {}
    for c in profile_result.chunks:
        by_run.setdefault(c.run_id, []).append(c)
    cut_by_run: dict[str, list] = {}
    sel = {
        d.candidate_id
        for d in profile_result.decisions
        if getattr(d, "selected", False)
    }
    for cand in profile_result.candidates:
        if cand.candidate_id in sel:
            cut_by_run.setdefault(cand.activity_run_id, []).append(cand)
    for run_id in sorted(by_run):
        ordered = sorted(by_run[run_id], key=lambda c: c.start_instant_s)
        classified = classify_roles(ordered, cfg)
        role_chunks.extend(classified)
        seq, contexts = pair_contexts_for_run(
            classified,
            cut_by_run.get(run_id, []),
            anchor_results,
            along_by_id,
        )
        d, h = stitch_run(run_id, seq, contexts, cfg)
        decisions.extend(d)
        hyps.extend(h)
    return role_chunks, decisions, hyps
