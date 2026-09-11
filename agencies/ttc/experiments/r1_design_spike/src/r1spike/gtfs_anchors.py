"""GTFS terminal and per-stop anchor candidates (Spike 02, V4).

For each refined segment, every date-valid scheduled trip candidate is
extended with terminal-residual and per-stop alignment features — the
discriminative evidence Spike 01 showed interval/start/end residuals
lack. Passenger counts NEVER enter these features (identity firewall).

Direction evidence stays hypothesis-shaped (D-11 OPEN): candidates are
generated under BOTH polarity mappings (``pol_a``: NORTH->0/SOUTH->1;
``pol_b``: reversed) plus a no-direction row; equal evidence between
polarities produces a tie and an abstention, never a silent choice.

Per-stop alignment reuses the occurrence-based sequence DP
(``sequence_validation``) against the trip's scheduled occurrences;
only stable (present-in-every-optimum) matches feed features. Records
with unresolved DST folds or ambiguous stop mappings are excluded from
alignment and counted, not guessed.

An experimental high-confidence anchor additionally requires terminal
or stop-occurrence evidence and no direction/geometry contradiction.
Rejected anchor candidates are persisted with reasons.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from .candidate_generation import ApcRecord, generate_segment_candidates
from .contracts import (
    NO_SCHEDULED_TRIP,
    UNRESOLVED_SERVICE_DATE,
    EvidenceRegime,
    HardValidity,
    SegmentCandidate,
)
from .inputs import GtfsForDate
from .scoring import ScoreContract, decide
from .segmentation_baselines import Segment, SegPing
from .sequence_validation import (
    ObservedVisit,
    ScheduledOccurrence,
    validate_sequence,
)
from .spike02_contracts import Reason02, TerminalCluster, TerminalEpisode
from .terminal_clusters import point_cluster

POLARITY_MAPS = {
    "pol_a": {"NORTH": 0, "SOUTH": 1, "EAST": 0, "WEST": 1},
    "pol_b": {"NORTH": 1, "SOUTH": 0, "EAST": 1, "WEST": 0},
}

ANCHOR_STATUS_MAP = {
    "accepted": "accepted",
    "null_winner": "null_winner",
    "low_score": "low_score",
    "low_margin": "low_margin",
    "evidence_tie": "evidence_tie",
    "no_candidate": "no_candidate",
    "all_hard_invalid": "no_candidate",
}


@dataclass(frozen=True)
class AnchorConfig:
    """Experimental, unapproved anchor parameters."""

    stop_align_skip_cost: int = 1_000
    stop_residual_cap_s: int = 3_600
    visit_pad_s: int = 60
    min_aligned_stops_for_anchor: int = 5
    enable_direction: bool = True
    shortlist_overlap_required: bool = True


@dataclass(frozen=True)
class AnchorCandidate:
    """One extended trip candidate for one refined segment."""

    base: SegmentCandidate
    polarity: str | None  # pol_a | pol_b | None (no direction evidence)
    qscore: int | None
    hard_invalid: bool
    n_aligned: int
    n_observed: int
    n_alignment_optima: int
    reason_codes: tuple[str, ...]

    def key(self) -> tuple:
        return (*self.base.candidate_key(), self.polarity)


@dataclass(frozen=True)
class SegmentAnchorResult:
    segment_id: str
    status: str
    selected_key: tuple | None
    best_q: int | None
    margin_q: int | None
    is_anchor: bool
    reason_codes: tuple[str, ...]
    candidates: tuple[AnchorCandidate, ...]


def observed_visits_for_segment(
    segment: Segment,
    records: list[ApcRecord],
    gtfs: GtfsForDate,
    *,
    pad_s: int,
) -> tuple[list[ObservedVisit], int, int]:
    """(usable visits, n_excluded_fold, n_excluded_stop) for one segment.

    Only fold-unambiguous records with a UNIQUE GTFS stop mapping become
    visits; everything else is excluded and counted. Uses containment in
    the segment interval — deliberately independent of any scored
    attachment decision so anchor evidence is not laundered through it.
    """
    code_to_stops: dict[str, list[str]] = {}
    for row in gtfs.stops.iter_rows(named=True):
        if row["stop_code"] is not None:
            code_to_stops.setdefault(str(row["stop_code"]), []).append(row["stop_id"])
    visits: list[ObservedVisit] = []
    excluded_fold = 0
    excluded_stop = 0
    lo = segment.start_instant_s - pad_s
    hi = segment.end_instant_s + pad_s
    for record in records:
        if record.vehicle != segment.vehicle:
            continue
        if len(record.arrival_candidates) != 1:
            if any(lo <= e <= hi for _f, e in record.arrival_candidates):
                excluded_fold += 1
            continue
        _fold, arrival = record.arrival_candidates[0]
        if not lo <= arrival <= hi:
            continue
        stop_ids = code_to_stops.get(
            str(record.stop_code) if record.stop_code is not None else ""
        )
        if not stop_ids or len(stop_ids) != 1:
            excluded_stop += 1
            continue
        visits.append(
            ObservedVisit(
                visit_id=record.record_id,
                stop_id=stop_ids[0],
                observed_time_s=arrival,
                along_m=None,
            )
        )
    visits.sort(key=lambda v: (v.observed_time_s, v.visit_id))
    return visits, excluded_fold, excluded_stop


def _scheduled_occurrences(
    gtfs: GtfsForDate, trip_id: str
) -> list[ScheduledOccurrence]:
    events = gtfs.scheduled_stop_events.filter(pl.col("trip_id") == trip_id).sort(
        "stop_sequence"
    )
    return [
        ScheduledOccurrence(
            occ_index=i,
            stop_id=row["stop_id"],
            scheduled_time_s=row["arrival_epoch_s"],
        )
        for i, row in enumerate(events.iter_rows(named=True))
    ]


def _median_int(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _p90_int(values: list[int]) -> int:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, (len(ordered) * 9) // 10)
    return ordered[idx]


def _direction_majority(records: list[ApcRecord], segment: Segment) -> str | None:
    counts: dict[str, int] = {}
    for r in records:
        if r.vehicle != segment.vehicle or r.direction_label is None:
            continue
        if len(r.arrival_candidates) != 1:
            continue
        _f, arrival = r.arrival_candidates[0]
        if segment.start_instant_s <= arrival <= segment.end_instant_s:
            counts[r.direction_label] = counts.get(r.direction_label, 0) + 1
    if not counts:
        return None
    top = max(counts.values())
    winners = [k for k, n in counts.items() if n == top]
    return winners[0] if len(winners) == 1 else None


def _trip_endpoint_clusters(
    gtfs: GtfsForDate,
    trip_id: str,
    clusters: list[TerminalCluster],
    cache: dict[tuple[str, str], tuple[str | None, str | None]],
) -> tuple[str | None, str | None]:
    """Cluster ids containing the trip's first/last scheduled stops."""
    key = (gtfs.feed_id, trip_id)
    if key in cache:
        return cache[key]
    events = gtfs.scheduled_stop_events.filter(pl.col("trip_id") == trip_id).sort(
        "stop_sequence"
    )
    result: tuple[str | None, str | None] = (None, None)
    if events.height:
        stops_by_id = {
            row["stop_id"]: (row["stop_lat"], row["stop_lon"])
            for row in gtfs.stops.iter_rows(named=True)
        }
        first_stop = events["stop_id"][0]
        last_stop = events["stop_id"][-1]
        ids: list[str | None] = []
        for stop_id in (first_stop, last_stop):
            coords = stops_by_id.get(stop_id)
            cl = point_cluster(clusters, coords[0], coords[1]) if coords else None
            ids.append(cl.cluster_id if cl else None)
        result = (ids[0], ids[1])
    cache[key] = result
    return result


def anchor_candidates_for_segment(
    segment: Segment,
    records: list[ApcRecord],
    pings_by_id: dict[str, SegPing],
    gtfs_by_date: dict[str, GtfsForDate],
    geometry_lookup,
    episodes_before_after: tuple[TerminalEpisode | None, TerminalEpisode | None],
    contract: ScoreContract,
    *,
    service_date_candidates: list[str],
    schedule_window_expansion_s: int,
    soft_bound_m: float,
    cfg: AnchorConfig,
    clusters: list[TerminalCluster] | None = None,
    endpoint_cache: dict[tuple[str, str], tuple[str | None, str | None]] | None = None,
) -> list[AnchorCandidate]:
    """All extended anchor candidates for one refined segment."""
    ep_before, ep_after = episodes_before_after
    base_candidates = generate_segment_candidates(
        segment,
        pings_by_id,
        gtfs_by_date,
        geometry_lookup,
        service_date_candidates=service_date_candidates,
        schedule_window_expansion_s=schedule_window_expansion_s,
        soft_bound_m=soft_bound_m,
    )
    trip_info: dict[tuple[str, str], dict] = {}
    for date in service_date_candidates:
        gtfs = gtfs_by_date.get(date)
        if gtfs is None:
            continue
        for row in gtfs.trip_intervals.iter_rows(named=True):
            trip_info[(date, row["trip_id"])] = row

    out: list[AnchorCandidate] = []
    visits_cache: dict[tuple[str, str], tuple[list[ObservedVisit], int, int]] = {}
    for cand in base_candidates:
        is_real_trip = (
            cand.scheduled_trip_hypothesis != NO_SCHEDULED_TRIP
            and cand.service_date_hypothesis != UNRESOLVED_SERVICE_DATE
            and cand.hard_validity == HardValidity.VALID
        )
        if not is_real_trip:
            q = None
            hard_invalid = cand.hard_validity == HardValidity.HARD_INVALID
            if not hard_invalid and cand.service_date_hypothesis != (
                UNRESOLVED_SERVICE_DATE
            ):
                q, _fam, _reasons, hard_invalid = contract.score_candidate(
                    "segment_selection",
                    cand.feature_vector,
                    EvidenceRegime.AVL_ONLY,
                )
            out.append(
                AnchorCandidate(
                    base=cand,
                    polarity=None,
                    qscore=q,
                    hard_invalid=hard_invalid,
                    n_aligned=0,
                    n_observed=0,
                    n_alignment_optima=0,
                    reason_codes=cand.reason_codes,
                )
            )
            continue

        gtfs = gtfs_by_date[cand.service_date_hypothesis]
        trip = trip_info.get(
            (cand.service_date_hypothesis, cand.scheduled_trip_hypothesis)
        )
        features = dict(cand.feature_vector)
        reasons: list[str] = list(cand.reason_codes)

        # Terminal residual features from bounding episode hypotheses.
        if trip is not None:
            start_hyp = None
            if ep_before is not None:
                start_hyp = (
                    ep_before.service_start_s
                    if ep_before.service_start_s is not None
                    else ep_before.physical_departure_s
                )
            end_hyp = None
            if ep_after is not None:
                end_hyp = (
                    ep_after.service_end_s
                    if ep_after.service_end_s is not None
                    else ep_after.physical_arrival_s
                )
            if start_hyp is not None:
                features["anchor_term_start_residual_s"] = abs(
                    start_hyp - trip["start_epoch_s"]
                )
            if end_hyp is not None:
                features["anchor_term_end_residual_s"] = abs(
                    end_hyp - trip["end_epoch_s"]
                )
            if start_hyp is None and end_hyp is None:
                reasons.append(Reason02.ANCHOR_NO_TERMINAL_EVIDENCE)
            if clusters:
                trip_first_cl, trip_last_cl = _trip_endpoint_clusters(
                    gtfs,
                    cand.scheduled_trip_hypothesis,
                    clusters,
                    endpoint_cache if endpoint_cache is not None else {},
                )
                seg_start_cl = ep_before.cluster_id if ep_before is not None else None
                seg_end_cl = ep_after.cluster_id if ep_after is not None else None
                pair = 0
                n_checked = 0
                if seg_start_cl is not None and trip_first_cl is not None:
                    n_checked += 1
                    pair += 500 if seg_start_cl == trip_first_cl else 0
                if seg_end_cl is not None and trip_last_cl is not None:
                    n_checked += 1
                    pair += 500 if seg_end_cl == trip_last_cl else 0
                if n_checked:
                    features["anchor_terminal_pair_millis"] = (
                        pair if n_checked == 2 else pair * 2
                    )

        # Per-stop alignment features (schedule vs observed visits).
        overlap = cand.feature_vector.get("seg_interval_overlap_millis")
        run_alignment = (not cfg.shortlist_overlap_required) or (
            overlap is not None and overlap > 0
        )
        n_aligned = 0
        n_observed = 0
        n_optima = 0
        if run_alignment:
            cache_key = (segment.segment_id, gtfs.feed_id)
            if cache_key not in visits_cache:
                visits_cache[cache_key] = observed_visits_for_segment(
                    segment, records, gtfs, pad_s=cfg.visit_pad_s
                )
            visits, _exf, _exs = visits_cache[cache_key]
            n_observed = len(visits)
            if visits:
                scheduled = _scheduled_occurrences(gtfs, cand.scheduled_trip_hypothesis)
                seq = validate_sequence(
                    scheduled,
                    visits,
                    skip_cost=cfg.stop_align_skip_cost,
                    time_residual_cost_per_s=1,
                    time_residual_cap_s=cfg.stop_residual_cap_s,
                )
                n_optima = seq.n_optimal
                stable = dict(seq.stable_matches)
                occ_time = {o.occ_index: o.scheduled_time_s for o in scheduled}
                residuals = [
                    abs(
                        next(v.observed_time_s for v in visits if v.visit_id == vid)
                        - occ_time[occ]
                    )
                    for vid, occ in stable.items()
                ]
                n_aligned = len(stable)
                features["anchor_stop_aligned_n"] = n_aligned
                features["anchor_stop_aligned_frac_millis"] = (
                    n_aligned * 1000 // max(1, n_observed)
                )
                if residuals:
                    features["anchor_stop_residual_median_s"] = _median_int(residuals)
                    features["anchor_stop_residual_p90_s"] = _p90_int(residuals)
                extra = n_observed - len(seq.stable_kept)
                features["anchor_extra_stop_frac_millis"] = (
                    extra * 1000 // max(1, n_observed)
                )
                if stable:
                    span = max(stable.values()) - min(stable.values()) + 1
                    skipped_in_span = span - len(set(stable.values()))
                    features["anchor_skipped_sched_frac_millis"] = (
                        skipped_in_span * 1000 // max(1, span)
                    )

        # Direction polarity hypotheses (D-11 open: both retained).
        majority = (
            _direction_majority(records, segment) if cfg.enable_direction else None
        )
        polarity_rows: list[tuple[str | None, dict]] = []
        if (
            majority is not None
            and trip is not None
            and (trip["direction_id"] is not None)
        ):
            for pol in sorted(POLARITY_MAPS):
                mapped = POLARITY_MAPS[pol].get(majority)
                agree = 1000 if mapped == trip["direction_id"] else 0
                polarity_rows.append(
                    (pol, {"anchor_direction_agreement_millis": agree})
                )
        else:
            polarity_rows.append((None, {}))

        for polarity, dir_features in polarity_rows:
            fv = {**features, **dir_features}
            q, _families, score_reasons, hard_invalid = contract.score_candidate(
                "segment_selection", fv, EvidenceRegime.AVL_ONLY
            )
            out.append(
                AnchorCandidate(
                    base=SegmentCandidate(
                        segment_id=cand.segment_id,
                        service_date_hypothesis=cand.service_date_hypothesis,
                        feed_id=cand.feed_id,
                        route_mapping_hypothesis=cand.route_mapping_hypothesis,
                        scheduled_trip_hypothesis=cand.scheduled_trip_hypothesis,
                        evidence_regime=cand.evidence_regime,
                        feature_vector=fv,
                        hard_validity=(
                            HardValidity.HARD_INVALID
                            if hard_invalid
                            else HardValidity.VALID
                        ),
                        reason_codes=tuple(dict.fromkeys((*reasons, *score_reasons))),
                    ),
                    polarity=polarity,
                    qscore=None if hard_invalid else q,
                    hard_invalid=hard_invalid,
                    n_aligned=n_aligned,
                    n_observed=n_observed,
                    n_alignment_optima=n_optima,
                    reason_codes=tuple(dict.fromkeys((*reasons, *score_reasons))),
                )
            )
    return out


def decide_anchor(
    segment_id: str,
    candidates: list[AnchorCandidate],
    contract: ScoreContract,
    *,
    cfg: AnchorConfig,
    null_score_q: int,
) -> SegmentAnchorResult:
    """Threshold+margin decision over the extended candidate set, then
    the extra experimental anchor gate (terminal/stop evidence and no
    direction contradiction)."""
    from .contracts import ScoredCandidate

    scored: list[ScoredCandidate] = []
    by_key: dict[tuple, AnchorCandidate] = {}
    for cand in candidates:
        key = cand.key()
        by_key[key] = cand
        if cand.base.service_date_hypothesis == UNRESOLVED_SERVICE_DATE:
            scored.append(
                ScoredCandidate(
                    key=key,
                    qscore=null_score_q,
                    is_null=True,
                    hard_validity=HardValidity.VALID,
                    regime=EvidenceRegime.AVL_ONLY,
                    family_qscores={},
                    reason_codes=cand.reason_codes,
                )
            )
            continue
        scored.append(
            ScoredCandidate(
                key=key,
                qscore=cand.qscore if cand.qscore is not None else 0,
                is_null=False,
                hard_validity=(
                    HardValidity.HARD_INVALID
                    if cand.hard_invalid
                    else HardValidity.VALID
                ),
                regime=EvidenceRegime.AVL_ONLY,
                family_qscores={},
                reason_codes=cand.reason_codes,
            )
        )
    decision = decide(
        segment_id,
        scored,
        contract.thresholds["segment_selection"],
        status_map=ANCHOR_STATUS_MAP,
    )
    is_anchor = False
    reasons = list(decision.reason_codes)
    if decision.status == "accepted" and decision.selected_key is not None:
        winner = by_key[decision.selected_key]
        has_terminal = (
            "anchor_term_start_residual_s" in winner.base.feature_vector
            or "anchor_term_end_residual_s" in winner.base.feature_vector
        )
        has_stops = winner.n_aligned >= cfg.min_aligned_stops_for_anchor
        if not (has_terminal or has_stops):
            reasons.append(Reason02.ANCHOR_NO_TERMINAL_EVIDENCE)
        # Direction contradiction: the same trip under the OTHER polarity
        # scoring strictly higher would be a contradiction; equal scores
        # already produced an evidence tie upstream.
        contradiction = False
        if winner.polarity is not None:
            for other in candidates:
                if (
                    other.polarity is not None
                    and other.polarity != winner.polarity
                    and other.base.candidate_key() == winner.base.candidate_key()
                    and other.qscore is not None
                    and winner.qscore is not None
                    and other.qscore > winner.qscore
                ):
                    contradiction = True
        if contradiction:
            reasons.append(Reason02.ANCHOR_DIRECTION_CONFLICT)
        is_anchor = (has_terminal or has_stops) and not contradiction
    return SegmentAnchorResult(
        segment_id=segment_id,
        status=decision.status,
        selected_key=decision.selected_key,
        best_q=decision.best_q,
        margin_q=decision.margin_q,
        is_anchor=is_anchor,
        reason_codes=tuple(dict.fromkeys(reasons)),
        candidates=tuple(candidates),
    )


def topology_fallback(
    segment_id: str,
    target_date: str,
    adjacent_gtfs: GtfsForDate,
    route_id: str,
) -> dict:
    """Adjacent-date TOPOLOGY-ONLY fallback: borrows stop sequence /
    shape / terminal-pair topology, never identity. The scheduled trip
    id stays null; this can never convert a missing scheduled trip into
    a matched one."""
    return {
        "segment_id": segment_id,
        "target_service_date": target_date,
        "scheduled_trip_id": None,
        "status": "topology_only",
        "topology_source_feed": adjacent_gtfs.feed_id,
        "topology_source_date": adjacent_gtfs.service_date,
        "borrowed_route_id": route_id,
        "confidence": "reduced",
        "provenance": "adjacent-date feed topology; identity prohibited",
        "reason_codes": Reason02.TOPOLOGY_ONLY,
    }
