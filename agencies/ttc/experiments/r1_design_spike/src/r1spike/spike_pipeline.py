"""End-to-end slice pipeline for the spike (bounded, deterministic).

Wires: normalized artifacts -> time hypotheses -> X4 segmentation ->
geometry -> candidates -> staged resolution -> diagnostics. Each step is
callable from tests; ``run_spike.py`` is the thin CLI.
"""

from __future__ import annotations

import datetime as dt
import resource
import time
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from . import ALGORITHM_VERSION
from .candidate_generation import (
    ApcRecord,
    generate_apc_candidates,
    generate_segment_candidates,
)
from .contracts import ApcCandidate, SegmentCandidate
from .geometry import GeometryScope, ShapeGeometry, build_shape_geometry
from .inputs import GtfsForDate, load_gtfs_for_date
from .resolution import ResolutionConfig, ResolutionResult, resolve
from .scoring import ScoreContract
from .segmentation_baselines import (
    Segment,
    SegPing,
    segment_vehicle,
    variant_comparison,
)
from .time_hypotheses import (
    FoldResolution,
    PingObservation,
    apc_time_candidates,
    resolve_avl_folds,
)
from .windows import build_windows, window_order


@dataclass
class PreparedSlice:
    name: str
    vehicles: list[str]
    vehicle_query: str
    pings_by_vehicle: dict[str, list[SegPing]] = field(default_factory=dict)
    pings_by_id: dict[str, SegPing] = field(default_factory=dict)
    fold_resolutions: dict[str, FoldResolution] = field(default_factory=dict)
    apc_records: list[ApcRecord] = field(default_factory=list)
    avl_time_rows: list[dict] = field(default_factory=list)
    apc_df: pl.DataFrame | None = None
    avl_rows: int = 0
    gtfs_by_date: dict[str, GtfsForDate] = field(default_factory=dict)
    segments_x4a: list[Segment] = field(default_factory=list)
    segments_x4b: list[Segment] = field(default_factory=list)
    segment_candidates: dict[str, list[SegmentCandidate]] = field(default_factory=dict)
    apc_candidates: dict[str, list[ApcCandidate]] = field(default_factory=dict)
    stage_timings_s: dict[str, float] = field(default_factory=dict)


class GeometryCache:
    """Scoped shape geometries; dominant-shape lookup per (date, route)."""

    def __init__(self, gtfs_by_date: dict[str, GtfsForDate]):
        self._gtfs = gtfs_by_date
        self._cache: dict[tuple[str, str, str], ShapeGeometry | None] = {}

    def lookup(
        self, service_date: str, route_id: str, shape_id: str | None
    ) -> ShapeGeometry | None:
        gtfs = self._gtfs.get(service_date)
        if gtfs is None:
            return None
        if shape_id is None:
            shape_id = gtfs.dominant_shape_by_route.get(route_id)
            if shape_id is None:
                return None
        cache_key = (gtfs.feed_id, route_id, shape_id)
        if cache_key not in self._cache:
            scope = GeometryScope(
                feed_id=gtfs.feed_id,
                route_id=route_id,
                direction_id=None,
                shape_id=shape_id,
            )
            geom = build_shape_geometry(gtfs.shapes, scope)
            self._cache[cache_key] = geom if geom.is_projectable() else None
        return self._cache[cache_key]


def prepare_avl(
    avl_df: pl.DataFrame, *, fold_max_gap_s: int
) -> tuple[dict[str, list[SegPing]], dict[str, FoldResolution], list[dict]]:
    """Per-vehicle time hypotheses + fold resolution + SegPing frames."""
    pings_by_vehicle: dict[str, list[SegPing]] = {}
    resolutions: dict[str, FoldResolution] = {}
    time_rows: list[dict] = []
    for vehicle in sorted(avl_df["vehicle_id_raw"].unique().to_list()):
        sub = avl_df.filter(pl.col("vehicle_id_raw") == vehicle).sort(
            "event_local_time", "_record_id"
        )
        observations = [
            PingObservation(record_id=row["_record_id"], wall=row["event_local_time"])
            for row in sub.iter_rows(named=True)
        ]
        fold_results = resolve_avl_folds(observations, max_gap_s=fold_max_gap_s)
        by_id = {fr.record_id: fr for fr in fold_results}
        resolutions.update(by_id)
        pings: list[SegPing] = []
        for row in sub.iter_rows(named=True):
            fr = by_id[row["_record_id"]]
            valid_instants = tuple(
                c.utc_epoch_s
                for c in fr.candidates
                if c.valid and c.utc_epoch_s is not None
            )
            if not valid_instants:
                continue
            pings.append(
                SegPing(
                    record_id=row["_record_id"],
                    vehicle=vehicle,
                    instant_s=fr.selected_utc_epoch_s,
                    candidate_instants=valid_instants,
                    fold_status=fr.status,
                    local_date=row["event_local_time"].date().isoformat(),
                    route_label=row["route_label"],
                    destination_label=row["destination_label"],
                    run_label=row["run_label"],
                    tatrip_id=row["source_tatrip_id"],
                    latitude=row["latitude"],
                    longitude=row["longitude"],
                )
            )
            for cand in fr.candidates:
                selected = fr.status == "resolved" and fr.selected_fold == cand.fold
                time_rows.append(
                    {
                        "source_record_id": fr.record_id,
                        "source_type": "avl",
                        "wall_text": cand.wall_text,
                        "fold": cand.fold,
                        "utc_epoch_s": cand.utc_epoch_s,
                        "utc_offset_s": cand.utc_offset_s,
                        "ambiguous": cand.ambiguous,
                        "valid": cand.valid,
                        "candidate_status": (
                            "selected"
                            if selected
                            else (
                                "rejected"
                                if fr.status == "resolved" and cand.valid
                                else "retained-unresolved"
                            )
                        ),
                        "method": fr.method,
                        "margin_s": fr.margin_s,
                        "reason_codes": "|".join(
                            sorted({*fr.reason_codes, *cand.reason_codes})
                        ),
                        "algorithm_version": ALGORITHM_VERSION,
                    }
                )
        pings_by_vehicle[vehicle] = pings
    return pings_by_vehicle, resolutions, time_rows


def prepare_apc(apc_df: pl.DataFrame) -> list[ApcRecord]:
    records: list[ApcRecord] = []
    for row in apc_df.iter_rows(named=True):
        arrival = row["arrival_local_time"]
        departure = row["departure_local_time"]
        cands = apc_time_candidates(row["_record_id"], arrival, departure)
        arrival_cands = tuple(
            (c.fold, c.utc_epoch_s)
            for c in cands["arrival"]
            if c.valid and c.utc_epoch_s is not None
        )
        departure_cands = tuple(
            (c.fold, c.utc_epoch_s)
            for c in cands["departure"]
            if c.valid and c.utc_epoch_s is not None
        )
        records.append(
            ApcRecord(
                record_id=row["_record_id"],
                vehicle=row["vehicle_id_raw"],
                arrival_wall=arrival.isoformat(sep=" "),
                departure_wall=(departure.isoformat(sep=" ") if departure else None),
                local_date=arrival.date().isoformat(),
                route_label=row["route_label"],
                branch_label=row["branch_label"],
                direction_label=row["direction_label"],
                stop_code=row["source_stop_code"],
                latitude=row["latitude"],
                longitude=row["longitude"],
                arrival_candidates=arrival_cands,
                departure_candidates=departure_cands,
            )
        )
    return records


def prepare_slice(
    *,
    name: str,
    slice_cfg: dict,
    normalized_dir: Path,
    gtfs_dirs: dict[str, Path],
    seg_cfg: dict,
    time_cfg: dict,
    vehicle_query: str,
    vehicle_params: dict[str, str],
) -> PreparedSlice:
    from .inputs import load_apc_slice, load_avl_slice, select_vehicles

    t0 = time.perf_counter()
    vehicles, query_desc = select_vehicles(
        normalized_dir,
        query=vehicle_query,
        n=slice_cfg["vehicles_n"],
        params=vehicle_params,
    )
    prepared = PreparedSlice(name=name, vehicles=vehicles, vehicle_query=query_desc)
    avl = load_avl_slice(
        normalized_dir,
        dates=slice_cfg["avl_dates"],
        wall_start=slice_cfg["wall_start"],
        wall_end=slice_cfg["wall_end"],
        vehicles=vehicles,
    )
    apc = load_apc_slice(
        normalized_dir,
        dates=slice_cfg["apc_dates"],
        wall_start=slice_cfg["wall_start"],
        wall_end=slice_cfg["wall_end"],
        vehicles=vehicles,
    )
    prepared.avl_rows = avl.height
    prepared.apc_df = apc
    prepared.stage_timings_s["load"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    pings_by_vehicle, resolutions, time_rows = prepare_avl(
        avl, fold_max_gap_s=time_cfg["fold_max_gap_s"]
    )
    prepared.pings_by_vehicle = pings_by_vehicle
    prepared.fold_resolutions = resolutions
    prepared.avl_time_rows = time_rows
    prepared.pings_by_id = {
        p.record_id: p for pings in pings_by_vehicle.values() for p in pings
    }
    prepared.apc_records = prepare_apc(apc)
    prepared.stage_timings_s["time_hypotheses"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _vehicle, pings in sorted(prepared.pings_by_vehicle.items()):
        if not pings:
            continue
        prepared.segments_x4a.extend(
            segment_vehicle(
                pings,
                variant="x4a",
                max_continuity_gap_s=seg_cfg["max_continuity_gap_s"],
            )
        )
        prepared.segments_x4b.extend(
            segment_vehicle(
                pings,
                variant="x4b",
                max_continuity_gap_s=seg_cfg["max_continuity_gap_s"],
                x4b_debounce_pings_n=seg_cfg["x4b_debounce_pings_n"],
                x4b_debounce_span_s=seg_cfg["x4b_debounce_span_s"],
            )
        )
    prepared.stage_timings_s["segmentation"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    for date in slice_cfg["service_date_candidates"]:
        gtfs_dir = gtfs_dirs.get(date)
        if gtfs_dir is not None and gtfs_dir.exists():
            prepared.gtfs_by_date[date] = load_gtfs_for_date(gtfs_dir, date)
    prepared.stage_timings_s["gtfs_load"] = time.perf_counter() - t0
    return prepared


def resolution_segments_for(prepared: PreparedSlice, variant: str) -> list[Segment]:
    if variant == "x4a":
        return prepared.segments_x4a
    if variant == "x4b":
        return prepared.segments_x4b
    raise ValueError(f"unknown resolution variant {variant!r}")


def generate_candidates(
    prepared: PreparedSlice,
    *,
    slice_cfg: dict,
    geometry_cfg: dict,
    temporal_cfg: dict,
    candidates_cfg: dict,
    resolution_variant: str = "x4a",
) -> None:
    cache = GeometryCache(prepared.gtfs_by_date)
    t0 = time.perf_counter()
    resolution_segments = resolution_segments_for(prepared, resolution_variant)
    for seg in sorted(resolution_segments, key=lambda s: s.segment_id):
        prepared.segment_candidates[seg.segment_id] = generate_segment_candidates(
            seg,
            prepared.pings_by_id,
            prepared.gtfs_by_date,
            cache.lookup,
            service_date_candidates=slice_cfg["service_date_candidates"],
            schedule_window_expansion_s=candidates_cfg["schedule_window_expansion_s"],
            soft_bound_m=geometry_cfg["soft_bound_m"],
        )
    prepared.stage_timings_s["segment_candidates"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    for record in sorted(prepared.apc_records, key=lambda r: r.record_id):
        prepared.apc_candidates[record.record_id] = generate_apc_candidates(
            record,
            resolution_segments,
            prepared.pings_by_id,
            prepared.gtfs_by_date,
            cache.lookup,
            service_date_candidates=slice_cfg["service_date_candidates"],
            candidate_prefilter_gap_s=temporal_cfg["candidate_prefilter_gap_s"],
            interpolation_max_gap_s=temporal_cfg["interpolation_max_gap_s"],
        )
    prepared.stage_timings_s["apc_candidates"] = time.perf_counter() - t0


def run_resolution(
    prepared: PreparedSlice,
    *,
    contract: ScoreContract,
    stage1_policy: str,
    candidates_cfg: dict,
    stage1_cfg: dict,
    slice_cfg: dict,
    window_hours: int,
    order: str,
    shuffle_seed: int,
    resolution_variant: str = "x4a",
) -> ResolutionResult:
    cfg = ResolutionConfig(
        contract=contract,
        stage1_policy=stage1_policy,
        policy_b_min_support_records=stage1_cfg["policy_b_min_support_records"],
        segment_hypothesis_cap=candidates_cfg["segment_hypothesis_cap"],
        apc_candidate_cap=candidates_cfg["apc_candidate_cap"],
        component_record_cap=candidates_cfg["component_record_cap"],
    )
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
    windows = build_windows(span_start, span_end, hours=window_hours)
    schedule = window_order(len(windows), order, seed=shuffle_seed)
    return resolve(
        segments=resolution_segments_for(prepared, resolution_variant),
        pings_by_id=prepared.pings_by_id,
        apc_records=prepared.apc_records,
        segment_candidates=prepared.segment_candidates,
        apc_candidates=prepared.apc_candidates,
        gtfs_by_date=prepared.gtfs_by_date,
        cfg=cfg,
        windows=windows,
        schedule=schedule,
    )


def segment_report(prepared: PreparedSlice) -> dict:
    def variant_stats(segments: list[Segment]) -> dict:
        if not segments:
            return {"count": 0}
        durations = sorted(s.duration_s for s in segments)
        pings = sorted(len(s.record_ids) for s in segments)
        return {
            "count": len(segments),
            "duration_s_min": durations[0],
            "duration_s_median": durations[len(durations) // 2],
            "duration_s_max": durations[-1],
            "pings_min": pings[0],
            "pings_median": pings[len(pings) // 2],
            "pings_max": pings[-1],
            "cross_midnight": sum(1 for s in segments if s.crosses_midnight),
            "time_uncertain": sum(1 for s in segments if s.time_uncertain),
            "route_purity_millis_min": min(
                (
                    s.route_purity_millis
                    for s in segments
                    if s.route_purity_millis is not None
                ),
                default=None,
            ),
        }

    return {
        "x4a": variant_stats(prepared.segments_x4a),
        "x4b": variant_stats(prepared.segments_x4b),
        "disagreement": variant_comparison(
            prepared.segments_x4a, prepared.segments_x4b
        ),
        "candidate_explosion": {
            "segment_candidates_total": sum(
                len(v) for v in prepared.segment_candidates.values()
            ),
            "apc_candidates_total": sum(
                len(v) for v in prepared.apc_candidates.values()
            ),
        },
    }


def peak_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
