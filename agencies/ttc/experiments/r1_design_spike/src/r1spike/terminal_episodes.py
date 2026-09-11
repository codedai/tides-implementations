"""Terminal / short-turn episode detection inside provisional runs.

EXPERIMENTAL (Spike 02). Episodes carry FOUR distinct timestamp
hypotheses — physical arrival, inbound service end, outbound service
start, physical departure — none of which is forced to exist, and the
required ordering (arrival <= service_end <= service_start <= departure,
equality allowed) is checked, never repaired.

Evidence families (>= 2 required, at least one physical; a direction
change alone NEVER creates an episode):

- terminal geofence entry/exit (cluster visit);
- sustained low movement / dwell;
- source direction transition (APC ``direction_label`` and/or sustained
  AVL destination-label change);
- geometry progress reversal (along-shape);
- APC alighting/mixed source signal before the boundary;
- APC boarding/mixed source signal after the boundary;
- sustained outbound motion after the candidate boundary.

APC signal quality: only records the episode context can fully check
are promoted to Q3 (``boundary_signals.upgrade_to_q3``); the episode's
``apc_quality`` is the maximum promoted quality among used evidence.
Fold-unresolved regions never yield physical timestamps — a DST-window
visit stays ambiguous rather than guessing a fold.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from .boundary_signals import upgrade_to_q3
from .segmentation_baselines import SegPing
from .spike02_contracts import (
    ALGORITHM_VERSION02,
    EPISODE_ID_VERSION,
    AlignmentEdge,
    ApcBoundarySignal,
    CountSignalState,
    EpisodeStatus,
    EvidenceFamily,
    Reason02,
    SignalQuality,
    TerminalCluster,
    TerminalEpisode,
    VehicleActivityRun,
)
from .terminal_clusters import point_cluster

EARTH_RADIUS_M = 6_371_008.8

_QUALITY_ORDER = {
    SignalQuality.Q0_UNUSABLE: 0,
    SignalQuality.Q1_WEAK: 1,
    SignalQuality.Q2_USABLE: 2,
    SignalQuality.Q3_STRONG: 3,
}

_ALIGHTING_STATES = {
    CountSignalState.ALIGHTING_SIGNAL_ONLY.value,
    CountSignalState.MIXED_COUNT_SIGNAL.value,
}
_BOARDING_STATES = {
    CountSignalState.BOARDING_SIGNAL_ONLY.value,
    CountSignalState.MIXED_COUNT_SIGNAL.value,
}


@dataclass(frozen=True)
class EpisodeConfig:
    """Experimental, unapproved thresholds (spike configuration)."""

    visit_blip_max_pings: int = 2
    visit_blip_max_s: int = 120
    dwell_low_move_m: float = 30.0
    dwell_min_s: int = 120
    outbound_window_pings_n: int = 5
    outbound_min_move_m: float = 200.0
    apc_pad_s: int = 600
    direction_pad_s: int = 900
    direction_window_pings_n: int = 5
    progress_window_pings_n: int = 10
    progress_min_delta_m: float = 500.0
    spanning_min_interval_s: int = 300
    min_evidence_families: int = 2
    apc_conflict_tolerance_s: int = 300
    min_visit_span_for_outbound_s: int = 300


def _dist_m(lat0: float, lon0: float, lat1: float, lon1: float) -> float:
    lat_ref = math.radians((lat0 + lat1) / 2.0)
    dx = EARTH_RADIUS_M * math.cos(lat_ref) * math.radians(lon1 - lon0)
    dy = EARTH_RADIUS_M * math.radians(lat1 - lat0)
    return math.hypot(dx, dy)


def _episode_id(run_id: str, anchor: str, first_record_id: str) -> str:
    key = json.dumps(
        [EPISODE_ID_VERSION, ALGORITHM_VERSION02, run_id, anchor, first_record_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{EPISODE_ID_VERSION}-{hashlib.sha256(key.encode()).hexdigest()[:32]}"


def _cluster_visits(
    pings: list[SegPing],
    clusters: list[TerminalCluster],
    cfg: EpisodeConfig,
) -> list[tuple[str, int, int]]:
    """Maximal (cluster_id, first_index, last_index) visits, with short
    interior blips (few pings, short span) tolerated."""
    membership: list[str | None] = []
    for p in pings:
        cluster = point_cluster(clusters, p.latitude, p.longitude)
        membership.append(cluster.cluster_id if cluster else None)

    visits: list[tuple[str, int, int]] = []
    i = 0
    n = len(pings)
    while i < n:
        cid = membership[i]
        if cid is None:
            i += 1
            continue
        j = i
        last_inside = i
        while j + 1 < n:
            nxt = j + 1
            if membership[nxt] == cid:
                j = nxt
                last_inside = nxt
                continue
            # blip tolerance: a short run of non-member pings inside a visit
            k = nxt
            blip = 0
            while k < n and membership[k] != cid and blip < cfg.visit_blip_max_pings:
                k += 1
                blip += 1
            if (
                k < n
                and membership[k] == cid
                and pings[k].ordering_instant - pings[j].ordering_instant
                <= cfg.visit_blip_max_s
            ):
                j = k
                last_inside = k
                continue
            break
        visits.append((cid, i, last_inside))
        i = last_inside + 1
    return visits


def _max_dwell_span_s(pings: list[SegPing], cfg: EpisodeConfig) -> int:
    """Longest contiguous low-movement span within the given pings."""
    best = 0
    span_start: int | None = None
    prev: SegPing | None = None
    for p in pings:
        if prev is not None and None not in (
            p.latitude,
            p.longitude,
            prev.latitude,
            prev.longitude,
        ):
            step = _dist_m(prev.latitude, prev.longitude, p.latitude, p.longitude)
            if step < cfg.dwell_low_move_m:
                if span_start is None:
                    span_start = prev.ordering_instant
                best = max(best, p.ordering_instant - span_start)
            else:
                span_start = None
        prev = p
    return best


def _mode_label(pings: list[SegPing], fld: str) -> str | None:
    counts: dict[str, int] = {}
    for p in pings:
        v = getattr(p, fld)
        if v is not None:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    top = max(counts.values())
    return min(k for k, n in counts.items() if n == top)


def _sustained_outbound(
    after: list[SegPing],
    centroid: tuple[float, float],
    cfg: EpisodeConfig,
) -> tuple[bool, int | None]:
    """Displacement from the visit centroid grows monotonically over the
    outbound window and clears the minimum move."""
    window = [
        p
        for p in after[: cfg.outbound_window_pings_n]
        if p.latitude is not None and p.longitude is not None
    ]
    if len(window) < 2:
        return False, None
    dists = [_dist_m(p.latitude, p.longitude, centroid[0], centroid[1]) for p in window]
    monotone = all(b >= a for a, b in zip(dists, dists[1:], strict=False))
    if monotone and dists[-1] - dists[0] >= cfg.outbound_min_move_m:
        return True, window[0].ordering_instant
    return False, None


def _progress_slope(
    pings: list[SegPing], along_by_id: dict[str, float]
) -> float | None:
    values = [along_by_id[p.record_id] for p in pings if p.record_id in along_by_id]
    if len(values) < 2:
        return None
    return values[-1] - values[0]


def detect_episodes(
    run: VehicleActivityRun,
    pings: list[SegPing],
    clusters: list[TerminalCluster],
    edges: list[AlignmentEdge],
    signals_by_id: dict[str, ApcBoundarySignal],
    *,
    cfg: EpisodeConfig,
    along_by_id: dict[str, float] | None = None,
    service_anchor_mode: str = "activity_signals",
) -> list[TerminalEpisode]:
    """All terminal + short-turn episode candidates for one run.

    ``service_anchor_mode`` is Spike-02R version selection on a shared
    module (spec section 4): the default ``"activity_signals"`` is the
    FROZEN Spike-02 behaviour (service anchors gated by count-signal
    states); ``"structural"`` derives service anchors from APC interval
    structure only and never consults count-signal states.

    ``edges`` must be this run's provisional alignment edges;
    ``along_by_id`` optionally maps ping record ids to along-shape
    metres against the run's dominant-route geometry.
    """
    along_by_id = along_by_id or {}
    run_edges = sorted(
        (e for e in edges if e.run_id == run.run_id),
        key=lambda e: (e.arrival_epoch_s, e.apc_source_record_id, e.fold),
    )
    clusters_by_id = {c.cluster_id: c for c in clusters}
    episodes: list[TerminalEpisode] = []

    for cid, first, last in _cluster_visits(pings, clusters, cfg):
        cluster = clusters_by_id[cid]
        visit = pings[first : last + 1]
        visit_start = visit[0].ordering_instant
        visit_end = visit[-1].ordering_instant
        fold_unresolved = any(p.fold_status == "unresolved" for p in visit)

        families: set[str] = {EvidenceFamily.GEOFENCE.value}
        reasons: list[str] = []

        dwell_present = _max_dwell_span_s(visit, cfg) >= cfg.dwell_min_s
        if dwell_present:
            families.add(EvidenceFamily.DWELL.value)

        after = pings[last + 1 :]
        outbound, outbound_start_s = _sustained_outbound(
            after, (cluster.centroid_lat, cluster.centroid_lon), cfg
        )
        # Departure motion is only evidence of a boundary when the visit
        # was a real stop: a drive-through transit trivially "departs",
        # so outbound motion alone must never co-sign a transit.
        real_stop = dwell_present or (
            visit_end - visit_start >= cfg.min_visit_span_for_outbound_s
        )
        if outbound and real_stop:
            families.add(EvidenceFamily.OUTBOUND_MOTION.value)

        # Source direction transition: APC direction labels around the
        # visit, plus sustained AVL destination-label change across it.
        before_edges = [
            e
            for e in run_edges
            if e.arrival_epoch_s < visit_start
            and visit_start - e.arrival_epoch_s <= cfg.direction_pad_s
            and e.direction_label is not None
        ]
        after_edges = [
            e
            for e in run_edges
            if e.arrival_epoch_s > visit_end
            and e.arrival_epoch_s - visit_end <= cfg.direction_pad_s
            and e.direction_label is not None
        ]
        apc_dir_change = bool(
            before_edges
            and after_edges
            and before_edges[-1].direction_label != after_edges[0].direction_label
        )
        w = cfg.direction_window_pings_n
        dest_before = _mode_label(pings[max(0, first - w) : first], "destination_label")
        dest_after = _mode_label(pings[last + 1 : last + 1 + w], "destination_label")
        dest_change = (
            dest_before is not None
            and dest_after is not None
            and dest_before != dest_after
        )
        if apc_dir_change or dest_change:
            families.add(EvidenceFamily.DIRECTION_TRANSITION.value)

        w = cfg.progress_window_pings_n
        slope_before = _progress_slope(pings[max(0, first - w) : first], along_by_id)
        slope_after = _progress_slope(pings[last + 1 : last + 1 + w], along_by_id)
        if (
            slope_before is not None
            and slope_after is not None
            and abs(slope_before) >= cfg.progress_min_delta_m
            and abs(slope_after) >= cfg.progress_min_delta_m
            and (slope_before > 0) != (slope_after > 0)
        ):
            families.add(EvidenceFamily.PROGRESS_REVERSAL.value)

        # APC evidence at this cluster within the padded visit window.
        window_edges: dict[str, AlignmentEdge] = {}
        for e in run_edges:
            if e.terminal_cluster_id != cid:
                continue
            if not (
                visit_start - cfg.apc_pad_s
                <= e.arrival_epoch_s
                <= visit_end + cfg.apc_pad_s
            ):
                continue
            window_edges.setdefault(e.apc_source_record_id, e)

        used_qualities: list[SignalQuality] = []
        service_end_s: int | None = None
        service_start_s: int | None = None
        alighting_present = False
        boarding_present = False
        spanning_ids: list[str] = []
        structural = service_anchor_mode == "structural"
        for rid in sorted(window_edges):
            e = window_edges[rid]
            signal = signals_by_id.get(rid)
            if signal is None or signal.quality == SignalQuality.Q0_UNUSABLE:
                continue
            direction_ctx = (
                EvidenceFamily.DIRECTION_TRANSITION.value in families
                or EvidenceFamily.PROGRESS_REVERSAL.value in families
            )
            promoted = upgrade_to_q3(
                signal,
                cluster_compatible=True,
                direction_progress_compatible=direction_ctx,
                stop_context_compatible=(
                    e.stop_in_cluster or e.terminal_cluster_id == cid
                ),
                state_consistent_with_episode=(
                    True
                    if structural
                    else signal.state.value in (_ALIGHTING_STATES | _BOARDING_STATES)
                ),
            )
            usable = _QUALITY_ORDER[promoted.quality] >= 2
            if structural:
                # Spike-02R V2R-STRUCTURAL: service anchors from the APC
                # interval structure only — count-signal STATES are never
                # consulted, and no activity evidence family is added.
                if usable:
                    service_end_s = (
                        e.arrival_epoch_s
                        if service_end_s is None
                        else max(service_end_s, e.arrival_epoch_s)
                    )
                    service_start_s = (
                        e.departure_epoch_s
                        if service_start_s is None
                        else min(service_start_s, e.departure_epoch_s)
                    )
            else:
                if promoted.state.value in _ALIGHTING_STATES and usable:
                    alighting_present = True
                    service_end_s = (
                        e.arrival_epoch_s
                        if service_end_s is None
                        else max(service_end_s, e.arrival_epoch_s)
                    )
                if promoted.state.value in _BOARDING_STATES and usable:
                    boarding_present = True
                    service_start_s = (
                        e.departure_epoch_s
                        if service_start_s is None
                        else min(service_start_s, e.departure_epoch_s)
                    )
            if usable:
                used_qualities.append(promoted.quality)
            if (
                e.departure_epoch_s - e.arrival_epoch_s >= cfg.spanning_min_interval_s
                and e.arrival_epoch_s <= visit_end
                and e.departure_epoch_s >= visit_start
            ):
                spanning_ids.append(rid)

        if alighting_present:
            families.add(EvidenceFamily.APC_ALIGHTING_BEFORE.value)
        if boarding_present:
            families.add(EvidenceFamily.APC_BOARDING_AFTER.value)

        physical_arrival_s: int | None = None
        physical_departure_s: int | None = None
        if not fold_unresolved:
            physical_arrival_s = visit_start
            physical_departure_s = (
                outbound_start_s if outbound_start_s is not None else visit_end
            )
        else:
            reasons.append(Reason02.BOUNDARY_FOLD_UNRESOLVED)

        # APC-vs-AVL conflict: service evidence far outside the physical
        # visit is a disagreement, not a license to reorder.
        conflict = False
        for ts in (service_end_s, service_start_s):
            if ts is None:
                continue
            if (
                ts < visit_start - cfg.apc_conflict_tolerance_s
                or ts > visit_end + cfg.apc_conflict_tolerance_s
            ):
                conflict = True
        if conflict:
            reasons.append(Reason02.EPISODE_APC_CONFLICT)

        ordered = [
            t
            for t in (
                physical_arrival_s,
                service_end_s,
                service_start_s,
                physical_departure_s,
            )
            if t is not None
        ]
        ordering_valid = all(b >= a for a, b in zip(ordered, ordered[1:], strict=False))
        alternatives: list[tuple[str, int]] = []
        if not ordering_valid:
            reasons.append(Reason02.EPISODE_ORDER_VIOLATION)
            for name, value in (
                ("physical_arrival_s", physical_arrival_s),
                ("service_end_s", service_end_s),
                ("service_start_s", service_start_s),
                ("physical_departure_s", physical_departure_s),
            ):
                if value is not None:
                    alternatives.append((name, value))

        n_times = len(ordered)
        if 0 < n_times < 4:
            reasons.append(Reason02.EPISODE_PARTIAL_TIMES)

        physical_present = bool(
            {
                f.value
                for f in (
                    EvidenceFamily.GEOFENCE,
                    EvidenceFamily.DWELL,
                    EvidenceFamily.PROGRESS_REVERSAL,
                    EvidenceFamily.OUTBOUND_MOTION,
                )
            }
            & families
        )
        sufficient = len(families) >= cfg.min_evidence_families and physical_present
        if not sufficient:
            reasons.append(Reason02.EPISODE_INSUFFICIENT_EVIDENCE)
            status = EpisodeStatus.INSUFFICIENT
        elif not ordering_valid or conflict or fold_unresolved:
            status = EpisodeStatus.AMBIGUOUS
        else:
            status = EpisodeStatus.SELECTED

        apc_quality = max(used_qualities, key=lambda q: _QUALITY_ORDER[q], default=None)
        episodes.append(
            TerminalEpisode(
                episode_id=_episode_id(run.run_id, cid, visit[0].record_id),
                run_id=run.run_id,
                cluster_id=cid,
                kind="terminal",
                physical_arrival_s=physical_arrival_s,
                service_end_s=service_end_s,
                service_start_s=service_start_s,
                physical_departure_s=physical_departure_s,
                evidence_families=tuple(sorted(families)),
                physical_evidence_present=physical_present,
                apc_record_ids=tuple(sorted(window_edges)),
                spanning_apc_record_ids=tuple(sorted(set(spanning_ids))),
                ping_index_first=first,
                ping_index_last=last,
                ordering_valid=ordering_valid,
                status=status,
                apc_quality=apc_quality,
                alternative_timestamps=tuple(alternatives),
                reason_codes=tuple(dict.fromkeys(reasons)),
            )
        )

    episodes.extend(
        _short_turn_candidates(
            run, pings, clusters, run_edges, cfg, along_by_id, episodes
        )
    )
    episodes.sort(key=lambda e: (e.ping_index_first, e.episode_id))
    return episodes


def _short_turn_candidates(
    run: VehicleActivityRun,
    pings: list[SegPing],
    clusters: list[TerminalCluster],
    run_edges: list[AlignmentEdge],
    cfg: EpisodeConfig,
    along_by_id: dict[str, float],
    existing: list[TerminalEpisode],
) -> list[TerminalEpisode]:
    """Deterministic discovery of turnback candidates OUTSIDE clusters:
    a geometry progress-reversal extremum plus at least one more family
    (dwell or direction). Direction alone is refused by the same gate."""
    if not along_by_id:
        return []
    covered = set()
    for ep in existing:
        covered.update(range(ep.ping_index_first, ep.ping_index_last + 1))

    values: list[tuple[int, float]] = [
        (i, along_by_id[p.record_id])
        for i, p in enumerate(pings)
        if p.record_id in along_by_id
    ]
    episodes: list[TerminalEpisode] = []
    w = cfg.progress_window_pings_n
    for pos in range(1, len(values) - 1):
        i, along = values[pos]
        if i in covered:
            continue
        before = [v for _, v in values[max(0, pos - w) : pos]]
        after = [v for _, v in values[pos + 1 : pos + 1 + w]]
        if len(before) < 2 or len(after) < 2:
            continue
        d_before = along - before[0]
        d_after = after[-1] - along
        if (
            abs(d_before) < cfg.progress_min_delta_m
            or abs(d_after) < cfg.progress_min_delta_m
            or (d_before > 0) == (d_after > 0)
        ):
            continue
        ping = pings[i]
        if point_cluster(clusters, ping.latitude, ping.longitude) is not None:
            continue
        families: set[str] = {EvidenceFamily.PROGRESS_REVERSAL.value}
        lo = max(0, i - cfg.direction_window_pings_n)
        hi = min(len(pings), i + cfg.direction_window_pings_n + 1)
        if _max_dwell_span_s(pings[lo:hi], cfg) >= cfg.dwell_min_s:
            families.add(EvidenceFamily.DWELL.value)
        t = ping.ordering_instant
        before_dir = [
            e.direction_label
            for e in run_edges
            if e.arrival_epoch_s < t
            and t - e.arrival_epoch_s <= cfg.direction_pad_s
            and e.direction_label is not None
        ]
        after_dir = [
            e.direction_label
            for e in run_edges
            if e.arrival_epoch_s > t
            and e.arrival_epoch_s - t <= cfg.direction_pad_s
            and e.direction_label is not None
        ]
        if before_dir and after_dir and before_dir[-1] != after_dir[0]:
            families.add(EvidenceFamily.DIRECTION_TRANSITION.value)
        reasons: list[str] = []
        fold_unresolved = ping.fold_status == "unresolved"
        sufficient = len(families) >= cfg.min_evidence_families
        if not sufficient:
            continue  # not even a candidate worth recording
        if fold_unresolved:
            reasons.append(Reason02.BOUNDARY_FOLD_UNRESOLVED)
        status = EpisodeStatus.AMBIGUOUS if fold_unresolved else EpisodeStatus.SELECTED
        reasons.append(Reason02.EPISODE_PARTIAL_TIMES)
        episodes.append(
            TerminalEpisode(
                episode_id=_episode_id(run.run_id, "short_turn", ping.record_id),
                run_id=run.run_id,
                cluster_id=None,
                kind="short_turn_candidate",
                physical_arrival_s=None if fold_unresolved else t,
                service_end_s=None,
                service_start_s=None,
                physical_departure_s=None if fold_unresolved else t,
                evidence_families=tuple(sorted(families)),
                physical_evidence_present=True,
                apc_record_ids=(),
                spanning_apc_record_ids=(),
                ping_index_first=i,
                ping_index_last=i,
                ordering_valid=True,
                status=status,
                apc_quality=None,
                alternative_timestamps=(),
                reason_codes=tuple(dict.fromkeys(reasons)),
            )
        )
        covered.update(range(max(0, i - 1), min(len(pings), i + 2)))
    return episodes
