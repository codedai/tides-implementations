"""Complete candidate generation (experimental; spike §9).

Segment candidates are complete tuples
``(segment, service_date, feed, route_mapping, trip-or-none)`` with
explicit null alternatives; APC candidates are complete tuples over
``(record, fold, date, feed, vehicle, route, stop, segment)`` plus
exactly one ``NO_ATTACHMENT``. Hypotheses differing in fold, stop,
route, or date stay distinct.

Documented experimental simplifications (findings, not decisions):

- ``source_date_key`` equals the wall calendar date on every fixture row
  (verified in S0-S2 metrics), so the wall date stands in for the date
  key in agreement features;
- APC spatial path residuals use the *dominant* shape per
  ``(service_date, route_id)`` — branch/pattern-scoped geometry needs
  semantics (branch mappings, D-11) the data does not confirm;
- candidate generation prefilters segment pairs whose interval gap
  exceeds ``candidate_prefilter_gap_s`` (wider than every score
  variant's hard bound, so variants see identical candidate sets).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from fractions import Fraction

from .contracts import (
    NO_ATTACHMENT,
    NO_ROUTE_MAPPING,
    NO_SCHEDULED_TRIP,
    UNRESOLVED_SERVICE_DATE,
    ApcCandidate,
    EvidenceRegime,
    HardValidity,
    Reason,
    SegmentCandidate,
)
from .fixedpoint import quantize
from .geometry import ShapeGeometry, coords_structurally_valid, project_point
from .inputs import GtfsForDate
from .segmentation_baselines import Segment, SegPing
from .temporal_features import (
    interpolate_between_pings,
    interval_gap_s,
    interval_overlap_millis,
    nearest_event_gap_s,
)

EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True, slots=True)
class ApcRecord:
    record_id: str
    vehicle: str
    arrival_wall: str  # ISO local wall text
    departure_wall: str | None
    local_date: str  # ISO date of arrival wall (evidence)
    route_label: str | None
    branch_label: str | None
    direction_label: str | None
    stop_code: str | None
    latitude: float | None
    longitude: float | None
    arrival_candidates: tuple[tuple[int, int], ...]  # (fold, epoch_s)
    departure_candidates: tuple[tuple[int, int], ...]


def _dist_m(lat0: float, lon0: float, lat1: float, lon1: float) -> float:
    lat_ref = math.radians((lat0 + lat1) / 2.0)
    dx = EARTH_RADIUS_M * math.cos(lat_ref) * math.radians(lon1 - lon0)
    dy = EARTH_RADIUS_M * math.radians(lat1 - lat0)
    return math.hypot(dx, dy)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def dominant_route_label(segment: Segment) -> tuple[str | None, int]:
    """(label, count) with deterministic tie handling: a tie yields the
    lexicographically smallest label but is reported by the caller via
    route_label_counts — ties are rare and visible, never silent."""
    if not segment.route_label_counts:
        return None, 0
    top_count = max(segment.route_label_counts.values())
    best = min(k for k, n in segment.route_label_counts.items() if n == top_count)
    return best, top_count


def _segment_spatial_features(
    segment: Segment,
    pings_by_id: dict[str, SegPing],
    geom: ShapeGeometry | None,
    *,
    soft_bound_m: float,
    sample_cap: int = 200,
) -> dict[str, float | int]:
    if geom is None or not geom.is_projectable():
        return {}
    ids = list(segment.record_ids)
    stride = max(1, len(ids) // sample_cap)
    residuals: list[float] = []
    for rid in ids[::stride]:
        ping = pings_by_id[rid]
        if not coords_structurally_valid(ping.latitude, ping.longitude):
            continue
        proj = project_point(geom, ping.latitude, ping.longitude)
        residuals.append(proj.residual_m)
    if not residuals:
        return {}
    offroute = sum(1 for r in residuals if r > soft_bound_m)
    return {
        "seg_median_shape_residual_dm": _median(residuals),
        "seg_offroute_frac_millis": offroute * 1000 // len(residuals),
    }


def _date_key_agreement_millis(
    segment: Segment, pings_by_id: dict[str, SegPing], service_date: str
) -> int:
    matching = sum(
        1 for rid in segment.record_ids if pings_by_id[rid].local_date == service_date
    )
    return matching * 1000 // len(segment.record_ids)


def generate_segment_candidates(
    segment: Segment,
    pings_by_id: dict[str, SegPing],
    gtfs_by_date: dict[str, GtfsForDate],
    geometry_lookup,
    *,
    service_date_candidates: list[str],
    schedule_window_expansion_s: int,
    soft_bound_m: float,
) -> list[SegmentCandidate]:
    """All complete Stage-1 tuples for one segment, nulls included.

    ``geometry_lookup(service_date, route_id, shape_id)`` returns a
    ShapeGeometry or None; shape_id None requests the dominant shape.
    """
    candidates: list[SegmentCandidate] = []

    local_dates: set[str] = set()
    for rid in (segment.record_ids[0], segment.record_ids[-1]):
        d = pings_by_id[rid].local_date
        local_dates.add(d)
        prev = (dt.date.fromisoformat(d) - dt.timedelta(days=1)).isoformat()
        local_dates.add(prev)
    dates = sorted(local_dates & set(service_date_candidates))

    label, _label_count = dominant_route_label(segment)

    for d in dates:
        gtfs = gtfs_by_date.get(d)
        if gtfs is None:
            candidates.append(
                SegmentCandidate(
                    segment_id=segment.segment_id,
                    service_date_hypothesis=d,
                    feed_id=None,
                    route_mapping_hypothesis=NO_ROUTE_MAPPING,
                    scheduled_trip_hypothesis=NO_SCHEDULED_TRIP,
                    evidence_regime=EvidenceRegime.AVL_ONLY,
                    feature_vector={},
                    hard_validity=HardValidity.HARD_INVALID,
                    reason_codes=(Reason.DATE_NO_FEED,),
                )
            )
            continue
        date_agreement = _date_key_agreement_millis(segment, pings_by_id, d)
        route_rows = (
            gtfs.routes.filter(gtfs.routes["route_short_name"] == label).sort(
                "route_id"
            )
            if label is not None
            else gtfs.routes.head(0)
        )
        if route_rows.height == 0:
            # PO/PI/unmappable labels: a real candidate whose mapping
            # evidence is zero — not hard-invalid, measurably weak.
            candidates.append(
                SegmentCandidate(
                    segment_id=segment.segment_id,
                    service_date_hypothesis=d,
                    feed_id=gtfs.feed_id,
                    route_mapping_hypothesis=NO_ROUTE_MAPPING,
                    scheduled_trip_hypothesis=NO_SCHEDULED_TRIP,
                    evidence_regime=EvidenceRegime.AVL_ONLY,
                    feature_vector={
                        "seg_route_mapping_confidence_millis": 0,
                        "seg_date_key_agreement_millis": date_agreement,
                    },
                    reason_codes=(Reason.ROUTE_UNMAPPED,),
                )
            )
            continue
        confidence = 1000 // route_rows.height
        mapping_reasons = (Reason.ROUTE_AMBIGUOUS,) if route_rows.height > 1 else ()
        for route_id in route_rows["route_id"].to_list():
            dominant_geom = geometry_lookup(d, route_id, None)
            spatial_dom = _segment_spatial_features(
                segment, pings_by_id, dominant_geom, soft_bound_m=soft_bound_m
            )
            base_features: dict[str, float | int | None] = {
                "seg_route_mapping_confidence_millis": confidence,
                "seg_date_key_agreement_millis": date_agreement,
            }
            trips = gtfs.trip_intervals.filter(
                (gtfs.trip_intervals["route_id"] == route_id)
                & (
                    gtfs.trip_intervals["start_epoch_s"]
                    <= segment.end_instant_s + schedule_window_expansion_s
                )
                & (
                    gtfs.trip_intervals["end_epoch_s"]
                    >= segment.start_instant_s - schedule_window_expansion_s
                )
            ).sort("trip_id")
            for trip in trips.iter_rows(named=True):
                trip_geom = geometry_lookup(d, route_id, trip["shape_id"])
                spatial = _segment_spatial_features(
                    segment, pings_by_id, trip_geom, soft_bound_m=soft_bound_m
                )
                seg_len = segment.duration_s
                trip_len = trip["end_epoch_s"] - trip["start_epoch_s"]
                ratio = min(seg_len, trip_len) * 1000 // max(1, max(seg_len, trip_len))
                candidates.append(
                    SegmentCandidate(
                        segment_id=segment.segment_id,
                        service_date_hypothesis=d,
                        feed_id=gtfs.feed_id,
                        route_mapping_hypothesis=route_id,
                        scheduled_trip_hypothesis=trip["trip_id"],
                        evidence_regime=EvidenceRegime.AVL_ONLY,
                        feature_vector={
                            **base_features,
                            **spatial,
                            "seg_sched_start_residual_s": abs(
                                segment.start_instant_s - trip["start_epoch_s"]
                            ),
                            "seg_sched_end_residual_s": abs(
                                segment.end_instant_s - trip["end_epoch_s"]
                            ),
                            "seg_interval_overlap_millis": interval_overlap_millis(
                                segment.start_instant_s,
                                segment.end_instant_s,
                                trip["start_epoch_s"],
                                trip["end_epoch_s"],
                            ),
                            "seg_duration_ratio_millis": ratio,
                        },
                        reason_codes=mapping_reasons,
                    )
                )
            # NO_SCHEDULED_TRIP alternative for this (date, feed, route)
            candidates.append(
                SegmentCandidate(
                    segment_id=segment.segment_id,
                    service_date_hypothesis=d,
                    feed_id=gtfs.feed_id,
                    route_mapping_hypothesis=route_id,
                    scheduled_trip_hypothesis=NO_SCHEDULED_TRIP,
                    evidence_regime=EvidenceRegime.AVL_ONLY,
                    feature_vector={**base_features, **spatial_dom},
                    reason_codes=mapping_reasons,
                )
            )

    # The explicit fully-null alternative (scored via null_scores).
    candidates.append(
        SegmentCandidate(
            segment_id=segment.segment_id,
            service_date_hypothesis=UNRESOLVED_SERVICE_DATE,
            feed_id=None,
            route_mapping_hypothesis=NO_ROUTE_MAPPING,
            scheduled_trip_hypothesis=NO_SCHEDULED_TRIP,
            evidence_regime=EvidenceRegime.AVL_ONLY,
            feature_vector={},
            reason_codes=(Reason.DATE_UNRESOLVED,),
        )
    )
    return candidates


def generate_apc_candidates(
    record: ApcRecord,
    segments: list[Segment],
    pings_by_id: dict[str, SegPing],
    gtfs_by_date: dict[str, GtfsForDate],
    geometry_lookup,
    *,
    service_date_candidates: list[str],
    candidate_prefilter_gap_s: int,
    interpolation_max_gap_s: int,
) -> list[ApcCandidate]:
    """All complete attachment tuples for one APC record, plus the
    single NO_ATTACHMENT alternative."""
    candidates: list[ApcCandidate] = []
    own_segments = sorted(
        (s for s in segments if s.vehicle == record.vehicle),
        key=lambda s: (s.start_instant_s, s.segment_id),
    )

    for fold, a0 in record.arrival_candidates:
        dep = [e for f, e in record.departure_candidates if f == fold]
        a1 = max(dep) if dep else a0
        if a1 < a0:
            a1 = a0  # inverted intervals stay visible upstream via flags
        # Candidate service dates: the wall calendar date (which is
        # fold-independent) and the previous service day, bounded by the
        # configured slice candidates.
        wall_date = dt.date.fromisoformat(record.local_date)
        date_set = {
            record.local_date,
            (wall_date - dt.timedelta(days=1)).isoformat(),
        }
        dates = sorted(date_set & set(service_date_candidates))
        for d in dates:
            gtfs = gtfs_by_date.get(d)
            if gtfs is None:
                continue
            route_rows = (
                gtfs.routes.filter(
                    gtfs.routes["route_short_name"] == record.route_label
                ).sort("route_id")
                if record.route_label is not None
                else gtfs.routes.head(0)
            )
            route_ids: list[tuple[str, int]] = [
                (rid, 1000 // route_rows.height)
                for rid in route_rows["route_id"].to_list()
            ] or [(NO_ROUTE_MAPPING, 0)]
            stop_rows = (
                gtfs.stops.filter(gtfs.stops["stop_code"] == record.stop_code).sort(
                    "stop_id"
                )
                if record.stop_code is not None
                else gtfs.stops.head(0)
            )
            if stop_rows.height:
                stop_hyps = [
                    (
                        row["stop_id"],
                        1000 // stop_rows.height,
                        row["stop_lat"],
                        row["stop_lon"],
                    )
                    for row in stop_rows.iter_rows(named=True)
                ]
                stop_reasons = (Reason.STOP_AMBIGUOUS,) if stop_rows.height > 1 else ()
            else:
                stop_hyps = [("XR1:STOP_UNMAPPED", None, None, None)]
                stop_reasons = (Reason.STOP_UNMAPPED,)

            for route_id, route_conf in route_ids:
                for stop_id, stop_conf, stop_lat, stop_lon in stop_hyps:
                    for seg in own_segments:
                        gap = interval_gap_s(
                            a0, a1, seg.start_instant_s, seg.end_instant_s
                        )
                        if gap > candidate_prefilter_gap_s:
                            continue
                        features = _apc_features(
                            record,
                            seg,
                            pings_by_id,
                            a0,
                            a1,
                            d,
                            route_id,
                            route_conf,
                            stop_conf,
                            stop_lat,
                            stop_lon,
                            geometry_lookup,
                            interpolation_max_gap_s=interpolation_max_gap_s,
                        )
                        hard = (
                            HardValidity.HARD_INVALID
                            if stop_conf is None
                            else HardValidity.VALID
                        )
                        candidates.append(
                            ApcCandidate(
                                apc_source_record_id=record.record_id,
                                time_fold_hypothesis=fold,
                                service_date_hypothesis=d,
                                feed_id=gtfs.feed_id,
                                vehicle_mapping_hypothesis=record.vehicle,
                                route_mapping_hypothesis=route_id,
                                stop_mapping_hypothesis=stop_id,
                                candidate_segment_id=seg.segment_id,
                                feature_vector=features,
                                hard_validity=hard,
                                reason_codes=stop_reasons,
                            )
                        )

    candidates.append(
        ApcCandidate(
            apc_source_record_id=record.record_id,
            time_fold_hypothesis=None,
            service_date_hypothesis=UNRESOLVED_SERVICE_DATE,
            feed_id=None,
            vehicle_mapping_hypothesis=None,
            route_mapping_hypothesis=NO_ROUTE_MAPPING,
            stop_mapping_hypothesis=NO_ATTACHMENT,
            candidate_segment_id=NO_ATTACHMENT,
            feature_vector={},
        )
    )
    return candidates


def _apc_features(
    record: ApcRecord,
    seg: Segment,
    pings_by_id: dict[str, SegPing],
    a0: int,
    a1: int,
    service_date: str,
    route_id: str,
    route_conf: int,
    stop_conf: int | None,
    stop_lat: float | None,
    stop_lon: float | None,
    geometry_lookup,
    *,
    interpolation_max_gap_s: int,
) -> dict[str, float | int | None]:
    seg_pings = [pings_by_id[rid] for rid in seg.record_ids]
    resolved = [
        (p.instant_s, p.latitude, p.longitude)
        for p in seg_pings
        if p.instant_s is not None
        and coords_structurally_valid(p.latitude, p.longitude)
    ]
    resolved.sort()
    instants = [t for t, _, _ in resolved]
    nearest = None
    g0 = nearest_event_gap_s(a0, instants)
    g1 = nearest_event_gap_s(a1, instants)
    if g0 is not None or g1 is not None:
        nearest = min(g for g in (g0, g1) if g is not None)

    features: dict[str, float | int | None] = {
        "att_interval_gap_s": interval_gap_s(
            a0, a1, seg.start_instant_s, seg.end_instant_s
        ),
        "att_interval_overlap_millis": interval_overlap_millis(
            a0, a1, seg.start_instant_s, seg.end_instant_s
        ),
        "att_nearest_ping_gap_s": nearest,
        "att_route_agreement_millis": (
            1000 if dominant_route_label(seg)[0] == record.route_label else 0
        ),
        "att_date_key_agreement_millis": (
            1000 if record.local_date == service_date else 0
        ),
    }
    if stop_conf is not None:
        features["att_stop_mapping_confidence_millis"] = stop_conf
    if stop_lat is not None and stop_lon is not None:
        geom = geometry_lookup(service_date, route_id, None)
        if geom is not None and geom.is_projectable():
            proj = project_point(geom, stop_lat, stop_lon)
            features["att_stop_to_path_residual_dm"] = proj.residual_m
        interp = interpolate_between_pings(
            a0,
            resolved,
            max_gap_s=interpolation_max_gap_s,
        )
        if interp is not None:
            features["att_stop_to_ping_interp_dm"] = _dist_m(
                interp.lat, interp.lon, stop_lat, stop_lon
            )
    return features


def quantize_feature_preview(value: float, quantum: str) -> int:
    """Test helper: expose fixedpoint quantization for feature values."""
    return quantize(value, Fraction(quantum))
