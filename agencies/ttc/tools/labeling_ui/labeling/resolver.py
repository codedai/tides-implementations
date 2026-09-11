"""Assemble everything reviewable about one case into a CaseBundle.

The resolver degrades gracefully: each stage that cannot run records a note
in ``bundle.availability`` and leaves the corresponding field None. The UI
renders whatever is present.

Identity recovery (the packet is pseudonymous by design):
  case_id -> restricted packet -> chunk_id -> x_refined_chunks (time window)
  chunk_id -> anchor evidence edges -> APC record ids -> vehicle_id
  fallback: the vehicle whose AVL pings best fill the chunk window.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

import polars as pl

from . import adapters, evidence
from .adapters import ArtifactStore
from .parsing import (
    CandidateKey,
    dates_spanned,
    parse_candidate_key,
    parse_chunk_duration_s,
)

log = logging.getLogger(__name__)

TOP_K_CANDIDATES = 5
APC_WINDOW_PAD_S = 1800
CONTEXT_CHUNKS = 1  # neighbors on each side


@dataclass
class ChunkInfo:
    chunk_id: str
    start_epoch: int | None
    end_epoch: int | None
    n_pings: int | None = None
    role: str | None = None
    slice: str | None = None
    profile: str | None = None
    run_id: str | None = None
    pings: pl.DataFrame | None = None


@dataclass
class CandidateBundle:
    letter: str
    key: CandidateKey
    trip_meta: dict = field(default_factory=dict)  # headsign, direction_id, ...
    sched_events: pl.DataFrame | None = None  # stop ladder with epochs + lat/lon
    shape: pl.DataFrame | None = None
    metrics: evidence.CandidateMetrics = field(
        default_factory=evidence.CandidateMetrics
    )
    diagnostics: dict = field(default_factory=dict)  # qscore etc. — reveal-only


@dataclass
class CaseBundle:
    case: dict  # row of the cases CSV
    chunk: ChunkInfo | None = None
    prev_chunk: ChunkInfo | None = None
    next_chunk: ChunkInfo | None = None
    vehicle_id: str | None = None
    vehicle_source: str = "unavailable"
    avl_route_labels: list[str] = field(default_factory=list)
    apc_events: pl.DataFrame | None = None  # vehicle APC in padded window
    candidates: list[CandidateBundle] = field(default_factory=list)
    alternates: pl.DataFrame | None = None  # all candidate keys, neutral order
    anchor_diag: dict | None = None  # reveal-only
    fragment_role_diag: dict | None = None  # reveal-only
    availability: dict[str, str] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return self.case.get("case_id", "?")

    @property
    def anchor_best_key(self) -> str | None:
        if self.anchor_diag:
            return self.anchor_diag.get("best_key") or None
        return None

    def letter_of_key(self, key: str | None) -> str | None:
        for c in self.candidates:
            if c.key.raw == key:
                return c.letter
        return None


# ---------------------------------------------------------------------------


def build_bundle(store: ArtifactStore, case: dict) -> CaseBundle:
    b = CaseBundle(case=dict(case))
    case_id = b.case_id

    chunk_id, case_epoch = _identify_chunk(store, case_id, b.availability)
    b.chunk = _chunk_info(store, chunk_id, case_epoch, case, b.availability)
    if b.chunk is not None:
        b.prev_chunk, b.next_chunk = _neighbor_chunks(store, b.chunk)

    _resolve_vehicle(store, b)
    _attach_pings(store, b)
    _attach_apc(store, b)
    _build_candidates(store, b)
    _attach_diagnostics(store, b)
    return b


# ---- identity --------------------------------------------------------------


def _identify_chunk(
    store: ArtifactStore, case_id: str, availability: dict
) -> tuple[str | None, int | None]:
    packet = adapters.load_restricted_packet(store)
    if packet is None:
        availability["restricted_packet"] = (
            "labeling_packet_restricted.parquet not available — cannot map the "
            "pseudonymous case to a chunk; only CSV metadata is shown"
        )
        return None, None
    row = packet.filter(pl.col("case_id") == case_id)
    if row.is_empty():
        availability["restricted_packet"] = f"case {case_id} not in restricted packet"
        return None, None
    r = row.row(0, named=True)
    chunk_id = r.get("real_id")
    epoch = r.get("case_epoch_s")
    if not chunk_id and r.get("detail"):
        try:
            chunk_id = json.loads(r["detail"]).get("chunk_id")
        except (json.JSONDecodeError, TypeError):
            pass
    return chunk_id, epoch


def _chunk_info(
    store: ArtifactStore,
    chunk_id: str | None,
    case_epoch: int | None,
    case: dict,
    availability: dict,
) -> ChunkInfo | None:
    if chunk_id is None:
        return None
    chunks = adapters.load_refined_chunks(store)
    if chunks is not None:
        row = chunks.filter(pl.col("chunk_id") == chunk_id)
        if not row.is_empty():
            r = row.row(0, named=True)
            return ChunkInfo(
                chunk_id=chunk_id,
                start_epoch=r.get("start_instant_s"),
                end_epoch=r.get("end_instant_s"),
                n_pings=r.get("n_pings"),
                role=r.get("role"),
                slice=r.get("slice"),
                profile=r.get("profile"),
                run_id=r.get("run_id"),
            )
    availability["refined_chunks"] = (
        "chunk not found in x_refined_chunks.parquet — window reconstructed "
        "from case epoch + summary duration"
    )
    dur = parse_chunk_duration_s(case.get("summary"))
    if case_epoch is None:
        return ChunkInfo(chunk_id=chunk_id, start_epoch=None, end_epoch=None)
    end = case_epoch + dur if dur is not None else None
    return ChunkInfo(chunk_id=chunk_id, start_epoch=case_epoch, end_epoch=end)


def _neighbor_chunks(
    store: ArtifactStore, chunk: ChunkInfo
) -> tuple[ChunkInfo | None, ChunkInfo | None]:
    chunks = adapters.load_refined_chunks(store)
    if chunks is None or chunk.run_id is None:
        return None, None
    sibs = chunks.filter(
        (pl.col("run_id") == chunk.run_id) & (pl.col("profile") == chunk.profile)
    ).sort("start_instant_s")
    ids = sibs["chunk_id"].to_list()
    if chunk.chunk_id not in ids:
        return None, None
    i = ids.index(chunk.chunk_id)

    def info(j: int) -> ChunkInfo | None:
        if j < 0 or j >= sibs.height:
            return None
        r = sibs.row(j, named=True)
        return ChunkInfo(
            chunk_id=r["chunk_id"],
            start_epoch=r.get("start_instant_s"),
            end_epoch=r.get("end_instant_s"),
            n_pings=r.get("n_pings"),
            role=r.get("role"),
            slice=r.get("slice"),
            profile=r.get("profile"),
            run_id=r.get("run_id"),
        )

    return info(i - 1), info(i + 1)


def _resolve_vehicle(store: ArtifactStore, b: CaseBundle) -> None:
    if b.chunk is None:
        b.availability["vehicle"] = "no chunk identity — vehicle unknown"
        return

    # primary: APC evidence records carry the vehicle
    edges = adapters.load_anchor_evidence_edges(store)
    if edges is not None:
        rec_ids = (
            edges.filter(
                (pl.col("derived_id") == b.chunk.chunk_id)
                & (pl.col("source_type") == "apc")
            )["source_record_id"]
            .unique()
            .to_list()
        )
        if rec_ids:
            vehicle = _vehicle_from_apc_records(store, b, rec_ids)
            if vehicle is not None:
                b.vehicle_id = vehicle
                b.vehicle_source = "apc-evidence"
                return

    # fallback: which vehicle's AVL pings best fit the chunk window edge-to-edge
    vehicle = _vehicle_from_avl_window(store, b)
    if vehicle is not None:
        b.vehicle_id = vehicle
        b.vehicle_source = "avl-window"
        return
    b.availability["vehicle"] = (
        "vehicle could not be resolved (no APC evidence edges and no AVL "
        "window match) — spatial/temporal panels unavailable"
    )


def _search_days(b: CaseBundle) -> list:
    if b.chunk is None or b.chunk.start_epoch is None or b.chunk.end_epoch is None:
        return []
    # pad so late-evening chunks also look at the neighboring calendar date
    return dates_spanned(b.chunk.start_epoch - 6 * 3600, b.chunk.end_epoch + 6 * 3600)


def _vehicle_from_apc_records(
    store: ArtifactStore, b: CaseBundle, rec_ids: list[str]
) -> str | None:
    for day in _search_days(b):
        apc = adapters.load_apc_day(store, day)
        if apc is None:
            continue
        hit = apc.filter(pl.col("_record_id").is_in(rec_ids))
        if not hit.is_empty():
            vehicles = hit["vehicle_id_raw"].drop_nulls()
            if not vehicles.is_empty():
                return str(vehicles.mode().sort().first())
    return None


def _vehicle_from_avl_window(store: ArtifactStore, b: CaseBundle) -> str | None:
    chunk = b.chunk
    if chunk is None or chunk.start_epoch is None or chunk.end_epoch is None:
        return None
    frames = []
    for day in _search_days(b):
        df = adapters.load_avl_day(store, day)
        if df is not None:
            frames.append(df)
    if not frames:
        return None
    avl = pl.concat(frames)
    w = avl.filter(pl.col("epoch_s").is_between(chunk.start_epoch, chunk.end_epoch))
    if w.is_empty():
        return None
    # the owning vehicle's pings span the window edge to edge
    score = (
        w.group_by("vehicle_id_raw")
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
        # vehicle id as final key: exact ties must resolve deterministically
        .sort(
            ["edge_gap", "n", "vehicle_id_raw"],
            descending=[False, True, False],
            maintain_order=True,
        )
    )
    best = score.row(0, named=True)
    if best["edge_gap"] > 300:  # nobody actually fills the window
        return None
    return str(best["vehicle_id_raw"])


# ---- traces ----------------------------------------------------------------


def _vehicle_avl(store: ArtifactStore, b: CaseBundle) -> pl.DataFrame | None:
    if b.vehicle_id is None:
        return None
    frames = []
    for day in _search_days(b):
        df = adapters.load_avl_day(store, day)
        if df is not None:
            frames.append(df.filter(pl.col("vehicle_id_raw") == b.vehicle_id))
    if not frames:
        return None
    return pl.concat(frames).sort("epoch_s")


def _attach_pings(store: ArtifactStore, b: CaseBundle) -> None:
    day_avl = _vehicle_avl(store, b)
    if day_avl is None:
        if b.vehicle_id is not None:
            b.availability["avl"] = "normalized AVL not available for these dates"
        return

    def window(ci: ChunkInfo | None) -> None:
        if ci is None or ci.start_epoch is None or ci.end_epoch is None:
            return
        ci.pings = day_avl.filter(
            pl.col("epoch_s").is_between(ci.start_epoch, ci.end_epoch)
        )

    window(b.chunk)
    window(b.prev_chunk)
    window(b.next_chunk)
    if b.chunk is not None and b.chunk.pings is not None:
        b.avl_route_labels = sorted(
            b.chunk.pings["route_label"].drop_nulls().unique().to_list()
        )


def _attach_apc(store: ArtifactStore, b: CaseBundle) -> None:
    if b.vehicle_id is None or b.chunk is None or b.chunk.start_epoch is None:
        return
    frames = []
    for day in _search_days(b):
        df = adapters.load_apc_day(store, day)
        if df is not None:
            frames.append(df.filter(pl.col("vehicle_id_raw") == b.vehicle_id))
    if not frames:
        b.availability["apc"] = "normalized APC not available for these dates"
        return
    lo = b.chunk.start_epoch - APC_WINDOW_PAD_S
    hi = (b.chunk.end_epoch or b.chunk.start_epoch) + APC_WINDOW_PAD_S
    b.apc_events = (
        pl.concat(frames)
        .filter(pl.col("arrival_epoch_s").is_between(lo, hi))
        .sort("arrival_epoch_s")
    )


# ---- candidates ------------------------------------------------------------


def _build_candidates(store: ArtifactStore, b: CaseBundle) -> None:
    if b.chunk is None:
        return
    table = adapters.load_schedule_candidates(store)
    if table is None:
        b.availability["candidates"] = (
            "x_complete_schedule_candidates.parquet not available — no "
            "candidate cards; labeling is still possible"
        )
        return
    mine = table.filter(pl.col("chunk_id") == b.chunk.chunk_id)
    if mine.is_empty():
        b.availability["candidates"] = "no schedule candidates recorded for this chunk"
        return

    anchors = adapters.load_anchor_candidates(store)
    anchor_key = None
    if anchors is not None:
        arow = anchors.filter(pl.col("chunk_id") == b.chunk.chunk_id)
        if not arow.is_empty():
            anchor_key = arow.row(0, named=True).get("best_key")

    # shortlist: the anchor's winner (if any) + highest-qscore keys.
    # NOTE: qscore picks *which* candidates appear but never their order or
    # any visible ranking (see README, bias controls). candidate_key is the
    # tiebreak so equal qscores shortlist deterministically.
    by_score = (
        mine.sort(
            ["qscore", "candidate_key"],
            descending=[True, False],
            maintain_order=True,
        )["candidate_key"].unique(maintain_order=True)
    ).to_list()
    shortlist: list[str] = []
    for key in ([anchor_key] if anchor_key else []) + by_score:
        if key and key not in shortlist:
            shortlist.append(key)
        if len(shortlist) >= TOP_K_CANDIDATES:
            break

    built = []
    for key in shortlist:
        try:
            built.append(_build_candidate(store, b, mine, key))
        except Exception as exc:  # one bad candidate must not sink the case
            log.warning("candidate %s failed to build: %s", key, exc)
            ck = parse_candidate_key(key) or CandidateKey(
                key, None, None, None, None, None
            )
            broken = CandidateBundle(letter="?", key=ck)
            broken.diagnostics["error"] = f"candidate build failed: {exc}"
            built.append(broken)

    # neutral, deterministic letters: scheduled start time then key
    built.sort(
        key=lambda c: (
            c.metrics.sched_start_epoch is None,
            c.metrics.sched_start_epoch or 0,
            c.key.raw,
        )
    )
    for i, c in enumerate(built):
        c.letter = chr(ord("A") + i)
    b.candidates = built

    b.alternates = _alternate_listing(store, b, mine, shortlist)


def _build_candidate(
    store: ArtifactStore, b: CaseBundle, mine: pl.DataFrame, key: str
) -> CandidateBundle:
    ck = parse_candidate_key(key) or CandidateKey(key, None, None, None, None, None)
    cand = CandidateBundle(letter="?", key=ck)

    gtfs_dir = adapters.gtfs_dir_for(store, ck.service_date, ck.feed_id)
    if gtfs_dir is None:
        cand.diagnostics["gtfs"] = "no GTFS snapshot for this service date/feed"

    trips = adapters.load_scheduled_trips(gtfs_dir)
    shape_id = None
    if trips is not None and ck.trip_id is None and ck.route_id is not None:
        # date-level candidate (route only, no specific trip)
        rrow = trips.filter(pl.col("route_id") == ck.route_id)
        if not rrow.is_empty():
            cand.trip_meta = {
                "route_short_name": rrow.row(0, named=True).get("route_short_name"),
                "route_level_only": True,
            }
    if trips is not None and ck.trip_id is not None:
        trow = trips.filter(pl.col("trip_id") == ck.trip_id)
        if ck.service_date is not None and "service_date" in trow.columns:
            trow = trow.filter(pl.col("service_date") == ck.service_date)
        if not trow.is_empty():
            t = trow.row(0, named=True)
            shape_id = t.get("shape_id")
            cand.trip_meta = {
                "route_short_name": t.get("route_short_name"),
                "route_long_name": t.get("route_long_name"),
                "direction_id": t.get("direction_id"),
                "trip_headsign": t.get("trip_headsign"),
                "shape_id": shape_id,
                "block_id": t.get("block_id"),
            }

    sched = adapters.load_stop_events_for_trip(gtfs_dir, ck.service_date, ck.trip_id)
    if sched is not None:
        stops = adapters.load_stops(gtfs_dir)
        if stops is not None and {"stop_id", "stop_lat", "stop_lon"} <= set(
            stops.columns
        ):
            sched = sched.join(
                stops.select("stop_id", "stop_lat", "stop_lon"),
                on="stop_id",
                how="left",
            )
        cand.sched_events = sched
        if (
            not cand.trip_meta.get("route_short_name")
            and "route_short_name" in sched.columns
        ):
            cand.trip_meta["route_short_name"] = sched["route_short_name"].first()

    cand.shape = adapters.load_shape(gtfs_dir, shape_id)

    chunk = b.chunk
    cand.metrics = evidence.compute_metrics(
        chunk.start_epoch if chunk else None,
        chunk.end_epoch if chunk else None,
        chunk.pings if chunk else None,
        b.avl_route_labels,
        b.apc_events,
        cand.sched_events,
        cand.shape,
        cand.trip_meta.get("route_short_name"),
    )

    rows = mine.filter(pl.col("candidate_key") == key)
    if not rows.is_empty():
        r = rows.row(0, named=True)
        cand.diagnostics.update(
            {
                "candidate_key": key,
                "qscore": r.get("qscore"),
                "regime": r.get("regime"),
                "censor_class": r.get("censor_class"),
                "n_aligned": r.get("n_aligned"),
                "n_observed": r.get("n_observed"),
            }
        )
    return cand


def _alternate_listing(
    store: ArtifactStore, b: CaseBundle, mine: pl.DataFrame, shortlist: list[str]
) -> pl.DataFrame | None:
    """Every recorded candidate key (beyond the cards), in neutral order —
    parsed fields only, no scores."""
    keys = mine["candidate_key"].unique().to_list()
    rows = []
    for key in keys:
        ck = parse_candidate_key(key)
        if ck is None:
            continue
        rows.append(
            {
                "candidate_key": key,
                "service_date": str(ck.service_date) if ck.service_date else None,
                "route_id": ck.route_id,
                "trip_id": ck.trip_id,
                "polarity": ck.polarity,
                "on_card": b.letter_of_key(key),
            }
        )
    if not rows:
        return None
    return pl.DataFrame(rows).sort(["service_date", "route_id", "trip_id"])


# ---- diagnostics -----------------------------------------------------------


def _attach_diagnostics(store: ArtifactStore, b: CaseBundle) -> None:
    if b.chunk is None:
        return
    anchors = adapters.load_anchor_candidates(store)
    if anchors is not None:
        row = anchors.filter(pl.col("chunk_id") == b.chunk.chunk_id)
        if not row.is_empty():
            b.anchor_diag = row.row(0, named=True)
    roles = adapters.load_fragment_roles(store)
    if roles is not None:
        row = roles.filter(pl.col("chunk_id") == b.chunk.chunk_id)
        if not row.is_empty():
            b.fragment_role_diag = row.row(0, named=True)
