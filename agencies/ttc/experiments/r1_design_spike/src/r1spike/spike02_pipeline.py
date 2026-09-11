"""Spike-02 end-to-end pipeline (bounded, deterministic, one pass).

Wires: Spike-01 prepared slice -> source signals -> provisional
activity runs -> terminal clusters -> trip-agnostic APC alignment ->
terminal episodes -> ONE refinement pass per variant -> frozen refined
segments -> adaptive final attachment -> GTFS anchors ->
anchor-and-align. Every stage is callable from tests; ``run_spike02``
is the thin CLI.
"""

from __future__ import annotations

import datetime as dt
import time
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from .adaptive_attachment import (
    AttachmentConfig,
    apply_t_mid,
    build_terminal_context,
    generate_candidates02,
    multiplicity_distribution,
)
from .boundary_refinement import (
    RefinementConfig,
    refine_run,
    spanning_roles,
)
from .boundary_signals import derive_signal
from .candidate_generation import (
    generate_segment_candidates,
)
from .contracts import ApcCandidate, SegmentCandidate
from .geometry import coords_structurally_valid, project_point
from .gtfs_anchors import (
    AnchorConfig,
    SegmentAnchorResult,
    anchor_candidates_for_segment,
    decide_anchor,
)
from .provisional_alignment import align_apc_records
from .provisional_runs import build_activity_runs, ordered_run_pings
from .refined_segments import to_spike01_segments
from .resolution import ResolutionConfig, ResolutionResult, resolve
from .scoring import ScoreContract
from .segmentation_baselines import Segment
from .spike02_contracts import (
    AlignmentEdge,
    ApcBoundarySignal,
    RefinementResult,
    SpanningApcRole,
    TerminalCluster,
    TerminalEpisode,
    VehicleActivityRun,
)
from .spike_pipeline import GeometryCache, PreparedSlice, prepare_slice
from .terminal_clusters import build_terminal_clusters
from .terminal_episodes import EpisodeConfig, detect_episodes
from .windows import build_windows, window_order

APC_FLAG_COLUMNS = [
    "_record_id",
    "boarding",
    "alighting",
    "avg_sample",
    "_flag_interval_inverted",
    "_flag_visit_key_collision",
    "_flag_negative_count",
]


@dataclass
class Spike02Prepared:
    name: str
    s1: PreparedSlice
    signals_by_id: dict[str, ApcBoundarySignal] = field(default_factory=dict)
    runs: list[VehicleActivityRun] = field(default_factory=list)
    clusters: list[TerminalCluster] = field(default_factory=list)
    edges: list[AlignmentEdge] = field(default_factory=list)
    along_by_id: dict[str, float] = field(default_factory=dict)
    episodes: list[TerminalEpisode] = field(default_factory=list)
    geometry_cache: GeometryCache | None = None
    stage_timings_s: dict[str, float] = field(default_factory=dict)


def _wall_epoch(wall_text: str, timezone: str) -> int:
    tz = zoneinfo.ZoneInfo(timezone)
    return int(dt.datetime.fromisoformat(wall_text).replace(tzinfo=tz).timestamp())


def load_apc_flags(
    normalized_dir: Path,
    *,
    dates: list[str],
    wall_start: str,
    wall_end: str,
    vehicles: list[str],
) -> dict[str, dict]:
    lf = pl.scan_parquet(normalized_dir / "apc", hive_partitioning=True).filter(
        pl.col("source_calendar_date").is_in([dt.date.fromisoformat(d) for d in dates]),
        pl.col("arrival_local_time").is_between(
            dt.datetime.fromisoformat(wall_start),
            dt.datetime.fromisoformat(wall_end),
        ),
        pl.col("vehicle_id_raw").is_in(vehicles),
    )
    frame = lf.select(APC_FLAG_COLUMNS).collect(engine="streaming")
    return {row["_record_id"]: row for row in frame.iter_rows(named=True)}


def derive_signals(
    prepared: PreparedSlice, flags_by_id: dict[str, dict]
) -> dict[str, ApcBoundarySignal]:
    signals: dict[str, ApcBoundarySignal] = {}
    for record in prepared.apc_records:
        flags = flags_by_id.get(record.record_id, {})
        signals[record.record_id] = derive_signal(
            record.record_id,
            boarding=flags.get("boarding"),
            alighting=flags.get("alighting"),
            avg_sample=flags.get("avg_sample"),
            interval_inverted=bool(flags.get("_flag_interval_inverted")),
            visit_key_collision=bool(flags.get("_flag_visit_key_collision")),
            negative_count=bool(flags.get("_flag_negative_count")),
            vehicle_present=record.vehicle is not None,
            arrival_time_present=bool(record.arrival_candidates),
            fold_resolved=len(record.arrival_candidates) == 1,
        )
    return signals


def compute_along(
    prepared: PreparedSlice,
    runs: list[VehicleActivityRun],
    cache: GeometryCache,
) -> dict[str, float]:
    """Along-shape metres per resolved ping against the run's
    dominant-route geometry (dominant shape per (date, route) — the
    same documented Spike-01 simplification, D-11 still open)."""
    along: dict[str, float] = {}
    for run in sorted(runs, key=lambda r: r.run_id):
        if not run.route_label_counts:
            continue
        top = max(run.route_label_counts.values())
        label = min(k for k, n in run.route_label_counts.items() if n == top)
        geom = None
        for date in run.local_dates:
            gtfs = prepared.gtfs_by_date.get(date)
            if gtfs is None:
                continue
            route_rows = gtfs.routes.filter(
                gtfs.routes["route_short_name"] == label
            ).sort("route_id")
            if route_rows.height == 0:
                continue
            geom = cache.lookup(date, route_rows["route_id"][0], None)
            if geom is not None:
                break
        if geom is None:
            continue
        for rid in run.record_ids:
            ping = prepared.pings_by_id[rid]
            if not coords_structurally_valid(ping.latitude, ping.longitude):
                continue
            along[rid] = project_point(geom, ping.latitude, ping.longitude).along_m
    return along


def prepare_spike02(
    *,
    name: str,
    cfg: dict,
    ttc_root: Path,
    vehicle_query: str,
    vehicle_params: dict[str, str],
) -> Spike02Prepared:
    slice_cfg = cfg["slices"][name]
    normalized_dir = ttc_root / cfg["inputs"]["normalized_dir"]
    gtfs_dirs = {
        d: ttc_root / p for d, p in cfg["inputs"]["gtfs_artifact_dirs"].items()
    }
    t0 = time.perf_counter()
    s1 = prepare_slice(
        name=name,
        slice_cfg=slice_cfg,
        normalized_dir=normalized_dir,
        gtfs_dirs=gtfs_dirs,
        seg_cfg={
            "max_continuity_gap_s": cfg["provisional"]["max_continuity_gap_s"],
            "x4b_debounce_pings_n": 5,
            "x4b_debounce_span_s": 120,
        },
        time_cfg={"fold_max_gap_s": 1800},
        vehicle_query=vehicle_query,
        vehicle_params=vehicle_params,
    )
    out = Spike02Prepared(name=name, s1=s1)
    out.stage_timings_s["prepare_s1"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    flags = load_apc_flags(
        normalized_dir,
        dates=slice_cfg["apc_dates"],
        wall_start=slice_cfg["wall_start"],
        wall_end=slice_cfg["wall_end"],
        vehicles=s1.vehicles,
    )
    out.signals_by_id = derive_signals(s1, flags)
    out.stage_timings_s["signals"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    timezone = cfg.get("timezone", "America/Toronto")
    out.runs = build_activity_runs(
        s1.pings_by_vehicle,
        max_continuity_gap_s=cfg["provisional"]["max_continuity_gap_s"],
        slice_wall_start_epoch_s=_wall_epoch(slice_cfg["wall_start"], timezone),
        slice_wall_end_epoch_s=_wall_epoch(slice_cfg["wall_end"], timezone),
        censor_margin_s=cfg["provisional"]["censor_margin_s"],
    )
    out.stage_timings_s["activity_runs"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    out.clusters = build_terminal_clusters(
        s1.gtfs_by_date,
        geofence_radius_m=cfg["clusters"]["geofence_radius_m"],
        merge_radius_m=cfg["clusters"]["merge_radius_m"],
        config_version=cfg["clusters"]["config_version"],
        extra_stop_code_hypotheses=cfg["clusters"].get(
            "experimental_stop_code_hypotheses"
        ),
    )
    out.stage_timings_s["clusters"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    out.geometry_cache = GeometryCache(s1.gtfs_by_date)
    out.along_by_id = compute_along(s1, out.runs, out.geometry_cache)
    out.stage_timings_s["along_progress"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    out.edges = align_apc_records(
        s1.apc_records,
        out.runs,
        s1.pings_by_id,
        out.signals_by_id,
        out.clusters,
        max_time_gap_s=cfg["apc_alignment"]["max_time_gap_s"],
        interpolation_max_gap_s=cfg["apc_alignment"]["interpolation_max_gap_s"],
    )
    out.stage_timings_s["apc_alignment"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    episode_cfg = EpisodeConfig(**cfg["episodes"])
    for run in sorted(out.runs, key=lambda r: r.run_id):
        out.episodes.extend(
            detect_episodes(
                run,
                ordered_run_pings(run, s1.pings_by_id),
                out.clusters,
                out.edges,
                out.signals_by_id,
                cfg=episode_cfg,
                along_by_id=out.along_by_id,
            )
        )
    out.stage_timings_s["episodes"] = time.perf_counter() - t0
    return out


def run_refinement(
    prepared: Spike02Prepared,
    *,
    variant: str,
    cfg: dict,
    run_order: list[int] | None = None,
) -> list[RefinementResult]:
    """One refinement pass per provisional run. ``run_order`` permutes
    PROCESSING order only; logical outputs must be identical for any
    permutation (asserted by the determinism matrix)."""
    ref_cfg = RefinementConfig(
        score_bar_q=cfg["refinement"]["score_bar_q"],
        margin_q=cfg["refinement"]["margin_q"],
    )
    runs = sorted(prepared.runs, key=lambda r: r.run_id)
    order = run_order if run_order is not None else list(range(len(runs)))
    results: dict[str, RefinementResult] = {}
    for idx in order:
        run = runs[idx]
        results[run.run_id] = refine_run(
            run,
            ordered_run_pings(run, prepared.s1.pings_by_id),
            [e for e in prepared.episodes if e.run_id == run.run_id],
            variant=variant,
            cfg=ref_cfg,
        )
    return [results[r.run_id] for r in runs]


def refined_spike01_segments(
    prepared: Spike02Prepared, refinements: list[RefinementResult]
) -> list[Segment]:
    refined = [s for res in refinements for s in res.refined_segments]
    return to_spike01_segments(refined, prepared.s1.pings_by_id)


@dataclass
class AttachmentRun:
    t_mid_s: int
    contract_variant: str
    resolution: ResolutionResult
    multiplicity: dict[str, int]
    prune_rows: list[dict]
    pruned_nearest_candidate_records: list[str]


def attachment_battery(
    prepared: Spike02Prepared,
    segments01: list[Segment],
    refinements: list[RefinementResult],
    roles: list[SpanningApcRole],
    contracts: dict[str, ScoreContract],
    *,
    cfg: dict,
    slice_cfg: dict,
    t_mid_values: list[int],
    contract_variants: list[str],
    window_hours: int | None = None,
    order: str = "forward",
) -> tuple[dict[tuple[int, str], AttachmentRun], dict[str, list[ApcCandidate]]]:
    """Candidate generation once, then (T_mid x contract-variant) grid.

    Candidate sets are identical across T_mid variants at generation
    time; pruning is applied afterward and logged, never baked in.
    """
    s1 = prepared.s1
    attach_cfg = AttachmentConfig(
        t_mid_variants_s=tuple(t_mid_values),
        cluster_stop_confidence_millis=cfg["attachment"][
            "cluster_stop_confidence_millis"
        ],
        cluster_rescue_enabled=cfg["attachment"]["cluster_rescue_enabled"],
    )
    cache = prepared.geometry_cache or GeometryCache(s1.gtfs_by_date)
    base_candidates = generate_candidates02(
        s1.apc_records,
        segments01,
        s1.pings_by_id,
        s1.gtfs_by_date,
        cache.lookup,
        prepared.clusters,
        service_date_candidates=slice_cfg["service_date_candidates"],
        candidate_prefilter_gap_s=cfg["temporal"]["candidate_prefilter_gap_s"],
        interpolation_max_gap_s=cfg["temporal"]["interpolation_max_gap_s"],
        cfg=attach_cfg,
    )
    terminal_ctx = build_terminal_context(prepared.episodes, refinements, roles)

    segment_candidates: dict[str, list[SegmentCandidate]] = {}
    for seg in sorted(segments01, key=lambda s: s.segment_id):
        segment_candidates[seg.segment_id] = generate_segment_candidates(
            seg,
            s1.pings_by_id,
            s1.gtfs_by_date,
            cache.lookup,
            service_date_candidates=slice_cfg["service_date_candidates"],
            schedule_window_expansion_s=cfg["candidates"][
                "schedule_window_expansion_s"
            ],
            soft_bound_m=cfg["geometry"]["soft_bound_m"],
        )

    hours = window_hours or cfg["windows"]["window_hours"]
    span_start = (
        int(
            dt.datetime.fromisoformat(slice_cfg["wall_start"])
            .replace(tzinfo=dt.UTC)
            .timestamp()
        )
        - 24 * 3600
    )
    span_end = (
        int(
            dt.datetime.fromisoformat(slice_cfg["wall_end"])
            .replace(tzinfo=dt.UTC)
            .timestamp()
        )
        + 24 * 3600
    )
    windows = build_windows(span_start, span_end, hours=hours)
    schedule = window_order(len(windows), order, seed=cfg["windows"]["shuffle_seed"])

    results: dict[tuple[int, str], AttachmentRun] = {}
    for t_mid in t_mid_values:
        filtered, prune_rows = apply_t_mid(base_candidates, terminal_ctx, t_mid_s=t_mid)
        # Records whose NEAREST-GAP prefilter candidate was pruned: a
        # conservative proxy for the plausible-owner-removed failure
        # signal (candidate scores are deliberately not consulted here).
        pruned_records: set[str] = set()
        pruned_by_rid: dict[str, set[str]] = {}
        for row in prune_rows:
            pruned_by_rid.setdefault(row["apc_source_record_id"], set()).add(
                row["candidate_segment_id"]
            )
        for rid, segs in pruned_by_rid.items():
            best_gap = None
            best_seg = None
            for cand in base_candidates[rid]:
                if cand.is_null_alternative():
                    continue
                gap = cand.feature_vector.get("att_interval_gap_s")
                if gap is not None and (best_gap is None or gap < best_gap):
                    best_gap = gap
                    best_seg = cand.candidate_segment_id
            if best_seg in segs:
                pruned_records.add(rid)
        for variant in contract_variants:
            contract = contracts[variant]
            res_cfg = ResolutionConfig(
                contract=contract,
                stage1_policy=cfg["stage1"]["policy"],
                policy_b_min_support_records=cfg["stage1"][
                    "policy_b_min_support_records"
                ],
                segment_hypothesis_cap=cfg["candidates"]["segment_hypothesis_cap"],
                apc_candidate_cap=cfg["candidates"]["apc_candidate_cap"],
                component_record_cap=cfg["candidates"]["component_record_cap"],
            )
            resolution = resolve(
                segments=segments01,
                pings_by_id=s1.pings_by_id,
                apc_records=s1.apc_records,
                segment_candidates=segment_candidates,
                apc_candidates=filtered,
                gtfs_by_date=s1.gtfs_by_date,
                cfg=res_cfg,
                windows=windows,
                schedule=schedule,
            )
            results[(t_mid, variant)] = AttachmentRun(
                t_mid_s=t_mid,
                contract_variant=variant,
                resolution=resolution,
                multiplicity=multiplicity_distribution(filtered),
                prune_rows=prune_rows,
                pruned_nearest_candidate_records=sorted(pruned_records),
            )
    return results, base_candidates


def episodes_bounding_segment(
    seg_boundary_before: str | None,
    seg_boundary_after: str | None,
    refinement_variant: str,
    episodes: list[TerminalEpisode],
) -> tuple[TerminalEpisode | None, TerminalEpisode | None]:
    from .boundary_refinement import _boundary_id

    by_bid = {
        _boundary_id(e.run_id, e.episode_id + ":" + refinement_variant): e
        for e in episodes
    }
    return (
        by_bid.get(seg_boundary_before or ""),
        by_bid.get(seg_boundary_after or ""),
    )


def anchors_for_refinement(
    prepared: Spike02Prepared,
    refinements: list[RefinementResult],
    contract: ScoreContract,
    *,
    cfg: dict,
    slice_cfg: dict,
    enable_direction: bool = True,
) -> dict[str, SegmentAnchorResult]:
    """Anchor decisions for every refined segment of one refinement
    variant (V4 evidence layer)."""
    s1 = prepared.s1
    cache = prepared.geometry_cache or GeometryCache(s1.gtfs_by_date)
    anchor_cfg = AnchorConfig(
        stop_align_skip_cost=cfg["anchors"]["stop_align_skip_cost"],
        stop_residual_cap_s=cfg["anchors"]["stop_residual_cap_s"],
        visit_pad_s=cfg["anchors"]["visit_pad_s"],
        min_aligned_stops_for_anchor=cfg["anchors"]["min_aligned_stops_for_anchor"],
        enable_direction=enable_direction and cfg["anchors"]["enable_direction"],
    )
    null_q = contract.null_scores["segment_selection"]["XNULL:UNRESOLVED_SERVICE_DATE"]
    endpoint_cache: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    results: dict[str, SegmentAnchorResult] = {}
    for res in refinements:
        for refined in res.refined_segments:
            seg01 = to_spike01_segments([refined], s1.pings_by_id)[0]
            eps = episodes_bounding_segment(
                refined.boundary_id_before,
                refined.boundary_id_after,
                res.variant,
                prepared.episodes,
            )
            candidates = anchor_candidates_for_segment(
                seg01,
                s1.apc_records,
                s1.pings_by_id,
                s1.gtfs_by_date,
                cache.lookup,
                eps,
                contract,
                service_date_candidates=slice_cfg["service_date_candidates"],
                schedule_window_expansion_s=cfg["candidates"][
                    "schedule_window_expansion_s"
                ],
                soft_bound_m=cfg["geometry"]["soft_bound_m"],
                cfg=anchor_cfg,
                clusters=prepared.clusters,
                endpoint_cache=endpoint_cache,
            )
            results[refined.refined_segment_id] = decide_anchor(
                refined.refined_segment_id,
                candidates,
                contract,
                cfg=anchor_cfg,
                null_score_q=null_q,
            )
    return results


def spanning_roles_for(
    refinements: list[RefinementResult], episodes: list[TerminalEpisode]
) -> list[SpanningApcRole]:
    roles: list[SpanningApcRole] = []
    for res in refinements:
        roles.extend(spanning_roles(res, episodes))
    return roles
