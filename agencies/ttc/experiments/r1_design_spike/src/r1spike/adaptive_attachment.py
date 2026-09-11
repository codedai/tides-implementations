"""Final APC attachment with adaptive temporal bounds (Spike 02, V3).

Runs AFTER refined boundaries are frozen; nothing here may move a
boundary. Two separate paths:

A. Mid-route: a candidate contained in (overlapping) one refined
   segment is always retained; non-containing neighbours that qualify
   only by temporal gap are compared under ``T_mid`` in {20, 30, 45,
   60} minutes. No final production value is chosen.

B. Terminal/turnback: records aligned to a terminal episode bypass the
   ordinary ``T_mid`` rule; their candidates for the episode's
   arrival-side and departure-side refined segments are retained (up to
   the generation prefilter), and boundary-spanning records carry both
   roles. A temporal bound that removes the plausible owner is a
   FAILURE even if multiplicity falls — pruned plausible owners are
   counted and reported.

Experimental stop rescue: records whose source stop code has no GTFS
mapping (e.g. 1652 / Wilson Station, D-10 OPEN) but whose coordinates
fall inside a versioned terminal cluster receive additional candidates
with an explicit cluster-hypothesis stop mapping
(``XCLUSTER:<cluster_id>``) at a configured, deliberately reduced
confidence. This is an experimental hypothesis, never an approved
crosswalk.
"""

from __future__ import annotations

from dataclasses import dataclass

from .candidate_generation import ApcRecord, _apc_features, generate_apc_candidates
from .contracts import ApcCandidate, HardValidity, Reason
from .inputs import GtfsForDate
from .segmentation_baselines import Segment, SegPing
from .spike02_contracts import (
    Reason02,
    RefinementResult,
    SpanningApcRole,
    TerminalCluster,
    TerminalEpisode,
)
from .terminal_clusters import point_cluster

UNMAPPED_STOP = "XR1:STOP_UNMAPPED"


@dataclass(frozen=True)
class AttachmentConfig:
    """Experimental, unapproved attachment parameters."""

    t_mid_variants_s: tuple[int, ...] = (1200, 1800, 2700, 3600)
    cluster_stop_confidence_millis: int = 500
    cluster_rescue_enabled: bool = True


@dataclass(frozen=True)
class TerminalContext:
    """Terminal-path context for one APC record."""

    episode_id: str
    cluster_id: str | None
    arrival_segment_id: str | None
    departure_segment_id: str | None
    spanning: bool


def build_terminal_context(
    episodes: list[TerminalEpisode],
    refinements: list[RefinementResult],
    roles: list[SpanningApcRole],
) -> dict[str, TerminalContext]:
    """record_id -> terminal context, from episode lineage and applied
    boundaries. Deterministic; a record touched by several episodes
    keeps the earliest episode (sorted by episode_id)."""
    seg_after: dict[str, str] = {}
    seg_before: dict[str, str] = {}
    applied_by_run: dict[str, set[str]] = {}
    for res in refinements:
        applied_by_run[res.run_id] = set(res.applied_boundary_ids)
        for seg in res.refined_segments:
            if seg.boundary_id_after is not None:
                seg_after[seg.boundary_id_after] = seg.refined_segment_id
            if seg.boundary_id_before is not None:
                seg_before[seg.boundary_id_before] = seg.refined_segment_id

    variant = refinements[0].variant if refinements else "v2_conditional"
    from .boundary_refinement import _boundary_id

    context: dict[str, TerminalContext] = {}
    spanning_ids = {r.apc_source_record_id for r in roles}
    for ep in sorted(episodes, key=lambda e: e.episode_id):
        bid = _boundary_id(ep.run_id, ep.episode_id + ":" + variant)
        if bid not in applied_by_run.get(ep.run_id, set()):
            continue
        for rid in ep.apc_record_ids:
            context.setdefault(
                rid,
                TerminalContext(
                    episode_id=ep.episode_id,
                    cluster_id=ep.cluster_id,
                    arrival_segment_id=seg_after.get(bid),
                    departure_segment_id=seg_before.get(bid),
                    spanning=rid in spanning_ids,
                ),
            )
    return context


def _cluster_rescue(
    record: ApcRecord,
    candidates: list[ApcCandidate],
    segments: list[Segment],
    pings_by_id: dict[str, SegPing],
    gtfs_by_date: dict[str, GtfsForDate],
    geometry_lookup,
    clusters: list[TerminalCluster],
    *,
    service_date_candidates: list[str],
    candidate_prefilter_gap_s: int,
    interpolation_max_gap_s: int,
    confidence_millis: int,
) -> list[ApcCandidate]:
    """Extra candidates with an experimental cluster stop hypothesis for
    an unmapped stop code observed inside a cluster geofence."""
    cluster = point_cluster(clusters, record.latitude, record.longitude)
    if cluster is None:
        return []
    if record.stop_code is None or (
        str(record.stop_code) not in cluster.member_stop_codes
    ):
        return []
    unmapped = [
        c
        for c in candidates
        if c.stop_mapping_hypothesis == UNMAPPED_STOP
        and c.hard_validity == HardValidity.HARD_INVALID
    ]
    if not unmapped:
        return []
    rescue_reasons: list[str] = [Reason.STOP_UNMAPPED]
    if str(record.stop_code) == "1652":
        rescue_reasons.append(Reason02.CLUSTER_EXPERIMENTAL_1652)
    rescue_reasons.append(Reason02.ATTACH_CLUSTER_RESCUE)

    extras: list[ApcCandidate] = []
    seen: set[tuple] = set()
    for base in unmapped:
        key = (
            base.time_fold_hypothesis,
            base.service_date_hypothesis,
            base.feed_id,
            base.route_mapping_hypothesis,
            base.candidate_segment_id,
        )
        if key in seen:
            continue
        seen.add(key)
        seg = next(
            (s for s in segments if s.segment_id == base.candidate_segment_id),
            None,
        )
        if seg is None:
            continue
        fold = base.time_fold_hypothesis
        arr = [e for f, e in record.arrival_candidates if f == fold]
        if not arr:
            continue
        a0 = arr[0]
        dep = [e for f, e in record.departure_candidates if f == fold]
        a1 = max(dep) if dep else a0
        if a1 < a0:
            a1 = a0
        route_conf = 0
        gtfs = gtfs_by_date.get(base.service_date_hypothesis)
        if gtfs is not None and base.route_mapping_hypothesis in set(
            gtfs.routes["route_id"].to_list()
        ):
            route_conf = 1000 // max(1, gtfs.routes.height)
        features = _apc_features(
            record,
            seg,
            pings_by_id,
            a0,
            a1,
            base.service_date_hypothesis,
            base.route_mapping_hypothesis,
            route_conf,
            confidence_millis,
            cluster.centroid_lat,
            cluster.centroid_lon,
            geometry_lookup,
            interpolation_max_gap_s=interpolation_max_gap_s,
        )
        extras.append(
            ApcCandidate(
                apc_source_record_id=record.record_id,
                time_fold_hypothesis=fold,
                service_date_hypothesis=base.service_date_hypothesis,
                feed_id=base.feed_id,
                vehicle_mapping_hypothesis=record.vehicle,
                route_mapping_hypothesis=base.route_mapping_hypothesis,
                stop_mapping_hypothesis=f"XCLUSTER:{cluster.cluster_id}",
                candidate_segment_id=base.candidate_segment_id,
                feature_vector=features,
                hard_validity=HardValidity.VALID,
                reason_codes=tuple(rescue_reasons),
            )
        )
    return extras


def generate_candidates02(
    records: list[ApcRecord],
    segments: list[Segment],
    pings_by_id: dict[str, SegPing],
    gtfs_by_date: dict[str, GtfsForDate],
    geometry_lookup,
    clusters: list[TerminalCluster],
    *,
    service_date_candidates: list[str],
    candidate_prefilter_gap_s: int,
    interpolation_max_gap_s: int,
    cfg: AttachmentConfig,
) -> dict[str, list[ApcCandidate]]:
    """Spike-01 candidate generation plus the experimental cluster stop
    rescue; identical candidate sets for every downstream T_mid variant
    so pruning is measured, never silently baked in."""
    out: dict[str, list[ApcCandidate]] = {}
    for record in sorted(records, key=lambda r: r.record_id):
        cands = generate_apc_candidates(
            record,
            segments,
            pings_by_id,
            gtfs_by_date,
            geometry_lookup,
            service_date_candidates=service_date_candidates,
            candidate_prefilter_gap_s=candidate_prefilter_gap_s,
            interpolation_max_gap_s=interpolation_max_gap_s,
        )
        if cfg.cluster_rescue_enabled:
            cands = cands + _cluster_rescue(
                record,
                cands,
                segments,
                pings_by_id,
                gtfs_by_date,
                geometry_lookup,
                clusters,
                service_date_candidates=service_date_candidates,
                candidate_prefilter_gap_s=candidate_prefilter_gap_s,
                interpolation_max_gap_s=interpolation_max_gap_s,
                confidence_millis=cfg.cluster_stop_confidence_millis,
            )
        out[record.record_id] = cands
    return out


def apply_t_mid(
    candidates_by_record: dict[str, list[ApcCandidate]],
    terminal_context: dict[str, TerminalContext],
    *,
    t_mid_s: int,
) -> tuple[dict[str, list[ApcCandidate]], list[dict]]:
    """Filter non-containing, gap-only mid-route candidates beyond
    ``t_mid_s``. Terminal-context records use path B instead: their
    episode-adjacent segments are always retained. Returns the filtered
    sets and one prune-log row per removed candidate."""
    filtered: dict[str, list[ApcCandidate]] = {}
    prune_rows: list[dict] = []
    for rid in sorted(candidates_by_record):
        ctx = terminal_context.get(rid)
        kept: list[ApcCandidate] = []
        for cand in candidates_by_record[rid]:
            if cand.is_null_alternative():
                kept.append(cand)
                continue
            overlap = cand.feature_vector.get("att_interval_overlap_millis")
            gap = cand.feature_vector.get("att_interval_gap_s")
            contained = overlap is not None and overlap > 0
            terminal_keep = ctx is not None and cand.candidate_segment_id in {
                ctx.arrival_segment_id,
                ctx.departure_segment_id,
            }
            if contained or terminal_keep or gap is None or gap <= t_mid_s:
                if terminal_keep and not contained:
                    kept.append(
                        ApcCandidate(
                            apc_source_record_id=cand.apc_source_record_id,
                            time_fold_hypothesis=cand.time_fold_hypothesis,
                            service_date_hypothesis=cand.service_date_hypothesis,
                            feed_id=cand.feed_id,
                            vehicle_mapping_hypothesis=(
                                cand.vehicle_mapping_hypothesis
                            ),
                            route_mapping_hypothesis=cand.route_mapping_hypothesis,
                            stop_mapping_hypothesis=cand.stop_mapping_hypothesis,
                            candidate_segment_id=cand.candidate_segment_id,
                            feature_vector=cand.feature_vector,
                            hard_validity=cand.hard_validity,
                            reason_codes=tuple(
                                dict.fromkeys(
                                    (
                                        *cand.reason_codes,
                                        Reason02.ATTACH_TERMINAL_PATH,
                                    )
                                )
                            ),
                        )
                    )
                else:
                    kept.append(cand)
                continue
            prune_rows.append(
                {
                    "apc_source_record_id": rid,
                    "t_mid_s": t_mid_s,
                    "candidate_segment_id": cand.candidate_segment_id,
                    "att_interval_gap_s": gap,
                    "hard_validity": cand.hard_validity.value,
                    "reason": Reason02.ATTACH_TMID_PRUNED,
                }
            )
        filtered[rid] = kept
    return filtered, prune_rows


def multiplicity_distribution(
    candidates_by_record: dict[str, list[ApcCandidate]],
) -> dict[str, int]:
    """Distinct hard-valid candidate SEGMENTS per record (not tuples) —
    the quantity the spike's H4 hypothesis is about."""
    dist: dict[str, int] = {}
    for cands in candidates_by_record.values():
        segs = {
            c.candidate_segment_id
            for c in cands
            if not c.is_null_alternative() and c.hard_validity == HardValidity.VALID
        }
        key = str(min(len(segs), 5)) + ("+" if len(segs) > 5 else "")
        dist[key] = dist.get(key, 0) + 1
    return dict(sorted(dist.items()))
