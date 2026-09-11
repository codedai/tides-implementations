"""Case evidence bundles for the v2 blind pass.

The loader is blind by construction: it never reads the packet CSV (whose
``summary`` embeds an anchor-status word), never loads first-pass label
files, and keeps every model diagnostic out of the bundle. Diagnostics live
behind :func:`load_diagnostics`, which the app may call only after the store
has logged a post-save reveal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import polars as pl

from . import adapters, parsing
from .adapters import ArtifactStoreV2
from .evidence import CandidateMetrics, compute_metrics

APC_WINDOW_PAD_S = 1800
CONTEXT_CHUNKS = 1
VEHICLE_EDGE_GAP_MAX_S = 300


@dataclass
class ChunkWindow:
    chunk_id: str
    start_epoch: int | None
    end_epoch: int | None
    n_pings_recorded: int | None = None
    run_id: str | None = None
    profile: str | None = None
    slice: str | None = None
    pings: pl.DataFrame | None = None

    @property
    def duration_s(self) -> int | None:
        if self.start_epoch is None or self.end_epoch is None:
            return None
        return self.end_epoch - self.start_epoch


@dataclass
class CandidateInfo:
    """One universe candidate, schedule-side facts only (no model fields)."""

    alias: str
    candidate_key: str
    kind: str
    service_date: str | None
    feed_id: str | None
    route_mapping_id: str | None
    scheduled_trip_id: str | None
    polarity_hypothesis_id: str | None
    explicit_null_polarity: bool
    unresolved_warning: str | None
    sched_start_epoch: int | None
    sched_end_epoch: int | None
    sched_n_stops: int | None
    display_order: int
    universe_hash: str

    @property
    def is_null_option(self) -> bool:
        return self.kind == "no_scheduled_match_option"

    def overlap_s(self, chunk: ChunkWindow) -> int | None:
        if (
            self.sched_start_epoch is None
            or self.sched_end_epoch is None
            or chunk.start_epoch is None
            or chunk.end_epoch is None
        ):
            return None
        return max(
            0,
            min(self.sched_end_epoch, chunk.end_epoch)
            - max(self.sched_start_epoch, chunk.start_epoch),
        )

    def start_residual_s(self, chunk: ChunkWindow) -> int | None:
        if self.sched_start_epoch is None or chunk.start_epoch is None:
            return None
        return self.sched_start_epoch - chunk.start_epoch

    def end_residual_s(self, chunk: ChunkWindow) -> int | None:
        if self.sched_end_epoch is None or chunk.end_epoch is None:
            return None
        return self.sched_end_epoch - chunk.end_epoch


@dataclass
class CandidateDetail:
    """Heavy per-candidate evidence, loaded on demand."""

    alias: str
    trip_meta: dict[str, Any]
    sched_events: pl.DataFrame | None
    shape: pl.DataFrame | None
    metrics: CandidateMetrics


@dataclass
class CaseBundleV2:
    case_id: str
    routing: dict[str, Any]
    is_child: bool
    child_row: dict[str, Any] | None
    chunk: ChunkWindow
    prev_chunk: ChunkWindow | None
    next_chunk: ChunkWindow | None
    vehicle_id: str | None
    vehicle_source: str
    avl_route_labels: list[str]
    apc_events: pl.DataFrame | None
    candidates: list[CandidateInfo]
    dst_crossing: bool
    availability: dict[str, str] = field(default_factory=dict)

    @property
    def parent_case_id(self) -> str:
        return self.case_id.split("::")[0]

    @property
    def null_option(self) -> CandidateInfo | None:
        for candidate in self.candidates:
            if candidate.is_null_option:
                return candidate
        return None

    @property
    def real_candidates(self) -> list[CandidateInfo]:
        return [c for c in self.candidates if not c.is_null_option]

    def by_alias(self, alias: str) -> CandidateInfo | None:
        for candidate in self.candidates:
            if candidate.alias == alias:
                return candidate
        return None


def _chunk_from_row(row: dict[str, Any]) -> ChunkWindow:
    return ChunkWindow(
        chunk_id=row["chunk_id"],
        start_epoch=row.get("start_instant_s"),
        end_epoch=row.get("end_instant_s"),
        n_pings_recorded=row.get("n_pings"),
        run_id=row.get("run_id"),
        profile=row.get("profile"),
        slice=row.get("slice"),
    )


def _neighbor_chunks(
    refined: pl.DataFrame | None, chunk: ChunkWindow
) -> tuple[ChunkWindow | None, ChunkWindow | None]:
    if refined is None or chunk.run_id is None or chunk.profile is None:
        return None, None
    siblings = (
        refined.filter(
            (pl.col("run_id") == chunk.run_id) & (pl.col("profile") == chunk.profile)
        )
        .sort("start_instant_s")
        .to_dicts()
    )
    index = next(
        (i for i, row in enumerate(siblings) if row["chunk_id"] == chunk.chunk_id),
        None,
    )
    if index is None:
        return None, None
    prev_row = siblings[index - 1] if index > 0 else None
    next_row = siblings[index + 1] if index + 1 < len(siblings) else None
    return (
        _chunk_from_row(prev_row) if prev_row else None,
        _chunk_from_row(next_row) if next_row else None,
    )


def _search_days(chunk: ChunkWindow) -> list:
    if chunk.start_epoch is None or chunk.end_epoch is None:
        return []
    pad = int(timedelta(hours=6).total_seconds())
    return parsing.dates_spanned(chunk.start_epoch - pad, chunk.end_epoch + pad)


def _resolve_vehicle(
    store: ArtifactStoreV2, chunk: ChunkWindow, availability: dict[str, str]
) -> tuple[str | None, str]:
    edges = adapters.load_anchor_evidence_edges(store)
    record_ids: list[str] = []
    if edges is not None:
        record_ids = (
            edges.filter(
                (pl.col("derived_id") == chunk.chunk_id)
                & (pl.col("source_type") == "apc")
            )["source_record_id"]
            .unique()
            .to_list()
        )
    if record_ids:
        votes: list[str] = []
        for day in _search_days(chunk):
            apc = adapters.load_apc_day(store, day)
            if apc is None:
                continue
            hits = apc.filter(pl.col("_record_id").is_in(record_ids))
            if not hits.is_empty():
                votes.extend(hits["vehicle_id_raw"].drop_nulls().to_list())
        if votes:
            vehicle = sorted(
                {v: votes.count(v) for v in set(votes)}.items(),
                key=lambda item: (-item[1], item[0]),
            )[0][0]
            return vehicle, "apc-evidence"
    vehicle = _vehicle_from_avl_window(store, chunk)
    if vehicle is not None:
        return vehicle, "avl-window"
    availability["vehicle"] = "无法从 anchor 证据或 AVL 窗口中还原出车辆"
    return None, "unavailable"


def _vehicle_from_avl_window(store: ArtifactStoreV2, chunk: ChunkWindow) -> str | None:
    if chunk.start_epoch is None or chunk.end_epoch is None:
        return None
    frames = []
    for day in _search_days(chunk):
        avl = adapters.load_avl_day(store, day)
        if avl is not None:
            frames.append(avl)
    if not frames:
        return None
    stacked = pl.concat(frames, how="vertical_relaxed").filter(
        pl.col("epoch_s").is_between(chunk.start_epoch, chunk.end_epoch)
    )
    if stacked.is_empty():
        return None
    scored = (
        stacked.group_by("vehicle_id_raw")
        .agg(
            pl.len().alias("n"),
            pl.col("epoch_s").min().alias("first"),
            pl.col("epoch_s").max().alias("last"),
        )
        .with_columns(
            (
                (pl.col("first") - chunk.start_epoch).abs()
                + (chunk.end_epoch - pl.col("last")).abs()
            ).alias("edge_gap")
        )
        .sort(["edge_gap", "n", "vehicle_id_raw"], descending=[False, True, False])
    )
    best = scored.row(0, named=True)
    if best["edge_gap"] > VEHICLE_EDGE_GAP_MAX_S:
        return None
    return best["vehicle_id_raw"]


def _attach_pings(
    store: ArtifactStoreV2, chunk: ChunkWindow, vehicle_id: str | None
) -> None:
    if vehicle_id is None or chunk.start_epoch is None or chunk.end_epoch is None:
        return
    frames = []
    for day in _search_days(chunk):
        avl = adapters.load_avl_day(store, day)
        if avl is None:
            continue
        frames.append(
            avl.filter(
                (pl.col("vehicle_id_raw") == vehicle_id)
                & pl.col("epoch_s").is_between(chunk.start_epoch, chunk.end_epoch)
            )
        )
    if frames:
        merged = pl.concat(frames, how="vertical_relaxed").sort("epoch_s")
        chunk.pings = merged if not merged.is_empty() else None


def _attach_apc(
    store: ArtifactStoreV2, chunk: ChunkWindow, vehicle_id: str | None
) -> pl.DataFrame | None:
    if vehicle_id is None or chunk.start_epoch is None or chunk.end_epoch is None:
        return None
    frames = []
    for day in _search_days(chunk):
        apc = adapters.load_apc_day(store, day)
        if apc is None:
            continue
        frames.append(
            apc.filter(
                (pl.col("vehicle_id_raw") == vehicle_id)
                & pl.col("arrival_epoch_s").is_between(
                    chunk.start_epoch - APC_WINDOW_PAD_S,
                    chunk.end_epoch + APC_WINDOW_PAD_S,
                )
            )
        )
    if not frames:
        return None
    merged = pl.concat(frames, how="vertical_relaxed").sort("arrival_epoch_s")
    return merged if not merged.is_empty() else None


def _candidates_for_case(universe: pl.DataFrame, case_id: str) -> list[CandidateInfo]:
    rows = universe.filter(pl.col("case_id") == case_id).sort("display_order")
    return [
        CandidateInfo(
            alias=row["candidate_alias"],
            candidate_key=row["candidate_key"],
            kind=row["candidate_kind"],
            service_date=row["service_date"],
            feed_id=row["feed_id"],
            route_mapping_id=row["route_mapping_id"],
            scheduled_trip_id=row["scheduled_trip_id"],
            polarity_hypothesis_id=row["polarity_hypothesis_id"],
            explicit_null_polarity=bool(row["explicit_null_polarity"]),
            unresolved_warning=row["unresolved_warning"],
            sched_start_epoch=row["sched_start_epoch_s"],
            sched_end_epoch=row["sched_end_epoch_s"],
            sched_n_stops=row["sched_n_stops"],
            display_order=row["display_order"],
            universe_hash=row["candidate_universe_hash"],
        )
        for row in rows.iter_rows(named=True)
    ]


def build_bundle(
    store: ArtifactStoreV2,
    case_id: str,
    *,
    routing_row: dict[str, Any],
    child_row: dict[str, Any] | None = None,
) -> CaseBundleV2:
    """Assemble the blind evidence bundle for a parent or child case."""

    availability: dict[str, str] = {}
    chunk = ChunkWindow(
        chunk_id=routing_row["chunk_id"],
        start_epoch=routing_row.get("chunk_start_epoch_s"),
        end_epoch=routing_row.get("chunk_end_epoch_s"),
        n_pings_recorded=routing_row.get("chunk_n_pings"),
        slice=routing_row.get("slice"),
    )
    refined = adapters.load_refined_chunks(store)
    if refined is not None:
        rows = refined.filter(pl.col("chunk_id") == chunk.chunk_id).sort("profile")
        if not rows.is_empty():
            row = rows.row(0, named=True)
            chunk.run_id = row.get("run_id")
            chunk.profile = row.get("profile")
            if chunk.start_epoch is None:
                chunk.start_epoch = row.get("start_instant_s")
            if chunk.end_epoch is None:
                chunk.end_epoch = row.get("end_instant_s")
    else:
        availability["chunks"] = (
            "x_refined_chunks.parquet unavailable; neighbor context disabled"
        )
    prev_chunk, next_chunk = _neighbor_chunks(refined, chunk)
    vehicle_id, vehicle_source = _resolve_vehicle(store, chunk, availability)
    _attach_pings(store, chunk, vehicle_id)
    for neighbor in (prev_chunk, next_chunk):
        if neighbor is not None:
            _attach_pings(store, neighbor, vehicle_id)
    if chunk.pings is None:
        availability.setdefault("pings", "该 chunk 窗口没有还原出任何 AVL ping")
    apc_events = _attach_apc(store, chunk, vehicle_id)
    route_labels: list[str] = []
    if chunk.pings is not None and "route_label" in chunk.pings.columns:
        route_labels = sorted(
            chunk.pings["route_label"].drop_nulls().unique().to_list()
        )
    universe = adapters.load_universe(store)
    candidates = (
        _candidates_for_case(universe, case_id.split("::")[0])
        if universe is not None
        else []
    )
    if not candidates:
        availability["candidates"] = (
            "candidate universe manifest unavailable for this case"
        )
    return CaseBundleV2(
        case_id=case_id,
        routing=dict(routing_row),
        is_child="::" in case_id,
        child_row=dict(child_row) if child_row else None,
        chunk=chunk,
        prev_chunk=prev_chunk,
        next_chunk=next_chunk,
        vehicle_id=vehicle_id,
        vehicle_source=vehicle_source,
        avl_route_labels=route_labels,
        apc_events=apc_events,
        candidates=candidates,
        dst_crossing=bool(routing_row.get("dst_crossing")),
        availability=availability,
    )


def load_candidate_detail(
    store: ArtifactStoreV2, bundle: CaseBundleV2, alias: str
) -> CandidateDetail | None:
    """Heavy GTFS-backed detail for one candidate (schedule side only)."""

    candidate = bundle.by_alias(alias)
    if candidate is None or candidate.is_null_option:
        return None
    gtfs_dir = None
    service_date = None
    if candidate.service_date and not str(candidate.service_date).startswith("XNULL:"):
        try:
            service_date = date.fromisoformat(str(candidate.service_date))
        except ValueError:
            service_date = None
    if service_date is not None:
        gtfs_dir = adapters.gtfs_dir_for(store, service_date, candidate.feed_id)
    trip_meta: dict[str, Any] = {
        "route_mapping_id": candidate.route_mapping_id,
        "scheduled_trip_id": candidate.scheduled_trip_id,
    }
    sched_events = None
    shape = None
    if gtfs_dir is not None and candidate.scheduled_trip_id:
        trips = adapters.load_scheduled_trips(gtfs_dir)
        if trips is not None:
            match = trips.filter(pl.col("trip_id") == candidate.scheduled_trip_id)
            if service_date is not None and "service_date" in match.columns:
                match = match.filter(pl.col("service_date") == service_date)
            if not match.is_empty():
                trip_meta.update(match.row(0, named=True))
        sched_events = adapters.load_stop_events_for_trip(
            gtfs_dir, service_date, candidate.scheduled_trip_id
        )
        if sched_events is not None:
            stops = adapters.load_stops(gtfs_dir)
            if stops is not None:
                sched_events = sched_events.join(
                    stops.select(["stop_id", "stop_lat", "stop_lon"]),
                    on="stop_id",
                    how="left",
                )
        shape_id = trip_meta.get("shape_id")
        if shape_id:
            shape = adapters.load_shape(gtfs_dir, shape_id)
    metrics = compute_metrics(
        bundle.chunk.start_epoch,
        bundle.chunk.end_epoch,
        bundle.chunk.pings,
        bundle.avl_route_labels,
        bundle.apc_events,
        sched_events,
        shape,
        trip_meta.get("route_short_name"),
    )
    return CandidateDetail(
        alias=alias,
        trip_meta=trip_meta,
        sched_events=sched_events,
        shape=shape,
        metrics=metrics,
    )


def load_diagnostics(store: ArtifactStoreV2, bundle: CaseBundleV2) -> dict[str, Any]:
    """MODEL DIAGNOSTICS — call only after the store logged a reveal.

    Returns the anchor decision row and score-bearing universe columns for
    the case's chunk. Nothing here may be rendered before the blind save.
    """

    diagnostics: dict[str, Any] = {}
    anchor = adapters.load_anchor_candidates(store)
    if anchor is not None:
        rows = anchor.filter(pl.col("chunk_id") == bundle.chunk.chunk_id)
        if not rows.is_empty():
            diagnostics["anchor_decision"] = rows.row(0, named=True)
    if store.spike02r_dir is not None:
        scores = adapters.read_parquet(
            store.spike02r_dir / "x_complete_schedule_candidates.parquet"
        )
        if scores is not None:
            rows = scores.filter(pl.col("chunk_id") == bundle.chunk.chunk_id)
            diagnostics["candidate_scores"] = rows.sort(
                "qscore", descending=True
            ).to_dicts()
    return diagnostics
