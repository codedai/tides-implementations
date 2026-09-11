"""Spike-02 contracts: boundary refinement, terminal episodes, anchors.

EXPERIMENTAL, DIAGNOSTIC, NON-PUBLISHABLE (Spike 02). Nothing here is a
production contract, none of these semantics are confirmed TTC source
semantics, and none of this closes R1 or any milestone.

Architecture caveat (load-bearing): the approved merge plan is ACYCLIC —
S7 never feeds back into S4, and the plan's Appendix C explicitly flags
"APC refines segment boundaries" as conflicting with the approved order.
This spike tests that idea as a *candidate plan revision* under a
one-pass restriction; it must never be described as consistent with the
currently approved architecture.

Spike-02 reason codes are namespaced ``XR2.<area>.<code>`` so they can
never be confused with Spike-01 (``XR1.*``) or production codes.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

PROFILE02 = "diagnostic-experimental-r1-spike-02"
PUBLISHABLE02 = False
TIDES_CONFORMANCE_CLAIMED02 = False
NORMATIVE_CONTRACT_CLAIMED02 = False
R1_CLOSED02 = False
PRODUCTION_MODULES_MODIFIED02 = False
AUTHORIZATION_BASIS02 = "user-requested isolated design-validation experiment"
ALGORITHM_VERSION02 = "x-r1-spike02-1"

RUN_ID_VERSION = "xar2"  # provisional vehicle activity runs
REFINED_ID_VERSION = "xrs2"  # refined trip-scale segment candidates
CLUSTER_ID_VERSION = "xtc2"  # terminal/turnback cluster hypotheses
EPISODE_ID_VERSION = "xte2"  # terminal/turnback episode candidates
BOUNDARY_ID_VERSION = "xbd2"  # boundary candidates


class Reason02:
    """Stable Spike-02 reason codes (``XR2.<area>.<code>``)."""

    SIGNAL_SEMANTICS_UNCONFIRMED = "XR2.SIGNAL.COUNT_SEMANTICS_UNCONFIRMED"
    SIGNAL_AVG_SAMPLE_MULTI = "XR2.SIGNAL.AVG_SAMPLE_MULTI"
    SIGNAL_VISIT_KEY_COLLISION = "XR2.SIGNAL.VISIT_KEY_COLLISION"
    SIGNAL_INTERVAL_INVERTED = "XR2.SIGNAL.INTERVAL_INVERTED"
    SIGNAL_NEGATIVE_COUNT = "XR2.SIGNAL.NEGATIVE_COUNT"
    SIGNAL_TIME_MISSING = "XR2.SIGNAL.TIME_MISSING"
    SIGNAL_TIME_UNRESOLVED_FOLD = "XR2.SIGNAL.TIME_UNRESOLVED_FOLD"
    SIGNAL_SPATIAL_CONTRADICTION = "XR2.SIGNAL.SPATIAL_CONTRADICTION"
    SIGNAL_SPATIAL_WEAK = "XR2.SIGNAL.SPATIAL_WEAK"
    SIGNAL_STOP_AMBIGUOUS = "XR2.SIGNAL.STOP_AMBIGUOUS"
    SIGNAL_VEHICLE_UNRESOLVED = "XR2.SIGNAL.VEHICLE_UNRESOLVED"
    ALIGN_NO_RUN = "XR2.ALIGN.NO_ACTIVITY_RUN"
    ALIGN_TIME_FAR = "XR2.ALIGN.TIME_BEYOND_BOUND"
    ALIGN_NO_BRACKET = "XR2.ALIGN.NO_BRACKETING_PINGS"
    CLUSTER_AMBIGUOUS = "XR2.CLUSTER.AMBIGUOUS"
    CLUSTER_MISSING = "XR2.CLUSTER.MISSING"
    CLUSTER_EXPERIMENTAL_1652 = "XR2.CLUSTER.STOP_1652_HYPOTHESIS"
    EPISODE_ORDER_VIOLATION = "XR2.EPISODE.TIMESTAMP_ORDER_VIOLATION"
    EPISODE_INSUFFICIENT_EVIDENCE = "XR2.EPISODE.INSUFFICIENT_EVIDENCE"
    EPISODE_DIRECTION_ONLY = "XR2.EPISODE.DIRECTION_ONLY_REFUSED"
    EPISODE_PARTIAL_TIMES = "XR2.EPISODE.PARTIAL_TIMESTAMPS"
    EPISODE_APC_CONFLICT = "XR2.EPISODE.APC_AVL_CONFLICT"
    BOUNDARY_APPLIED_APC = "XR2.BOUNDARY.APPLIED_APC_PRECEDENCE"
    BOUNDARY_APPLIED_AVL = "XR2.BOUNDARY.APPLIED_AVL_EVIDENCE"
    BOUNDARY_AMBIGUOUS = "XR2.BOUNDARY.AMBIGUOUS_NOT_APPLIED"
    BOUNDARY_TIE = "XR2.BOUNDARY.EVIDENCE_TIE"
    BOUNDARY_LOW_SCORE = "XR2.BOUNDARY.BELOW_BAR"
    BOUNDARY_CONFLICT = "XR2.BOUNDARY.CONFLICTING_CANDIDATES"
    BOUNDARY_FOLD_UNRESOLVED = "XR2.BOUNDARY.FOLD_UNRESOLVED_REGION"
    SPAN_DUAL_ROLE = "XR2.SPAN.TERMINAL_VISIT_DUAL_ROLE"
    ATTACH_TMID_PRUNED = "XR2.ATTACH.MID_ROUTE_BOUND_PRUNED"
    ATTACH_TERMINAL_PATH = "XR2.ATTACH.TERMINAL_PATH"
    ATTACH_CLUSTER_RESCUE = "XR2.ATTACH.CLUSTER_RESCUE"
    ANCHOR_LOW_SCORE = "XR2.ANCHOR.BELOW_BAR"
    ANCHOR_LOW_MARGIN = "XR2.ANCHOR.LOW_MARGIN"
    ANCHOR_NO_TERMINAL_EVIDENCE = "XR2.ANCHOR.NO_TERMINAL_EVIDENCE"
    ANCHOR_DIRECTION_CONFLICT = "XR2.ANCHOR.DIRECTION_GEOMETRY_CONFLICT"
    ALIGNMENT_TIED = "XR2.ALIGNPATH.TIED_OPTIMA"
    ALIGNMENT_UNMATCHED_SEGMENT = "XR2.ALIGNPATH.UNMATCHED_SEGMENT"
    ALIGNMENT_SKIPPED_TRIP = "XR2.ALIGNPATH.SKIPPED_SCHEDULED_TRIP"
    TOPOLOGY_ONLY = "XR2.TOPOLOGY.ADJACENT_DATE_FALLBACK"
    CENSORED = "XR2.CENSOR.RIGHT_CENSORED"


class CountSignalState(enum.StrEnum):
    """Derived source-level activity signal. NOT confirmed passenger-event
    semantics (D-06 open); the state names deliberately say "signal"."""

    BOARDING_SIGNAL_ONLY = "boarding_signal_only"
    ALIGHTING_SIGNAL_ONLY = "alighting_signal_only"
    MIXED_COUNT_SIGNAL = "mixed_count_signal"
    NO_COUNT_SIGNAL = "no_count_signal"


class SignalQuality(enum.StrEnum):
    """Experimental APC boundary-signal quality tier (spike §6)."""

    Q0_UNUSABLE = "q0_unusable"
    Q1_WEAK = "q1_weak"
    Q2_USABLE = "q2_usable"
    Q3_STRONG = "q3_strong"


class PingState(enum.StrEnum):
    """Exactly one internal state per AVL ping inside a refined run."""

    SEGMENT = "segment"
    TURNAROUND = "terminal_turnaround"
    BOUNDARY_UNRESOLVED = "boundary_unresolved"


class EpisodeStatus(enum.StrEnum):
    SELECTED = "selected"
    AMBIGUOUS = "ambiguous"
    INSUFFICIENT = "insufficient_evidence"


class BoundaryStatus(enum.StrEnum):
    APPLIED = "applied"
    AMBIGUOUS_NOT_APPLIED = "ambiguous_not_applied"
    BELOW_BAR = "below_bar"
    CONFLICT_SUPPRESSED = "conflict_suppressed"


class SegmentTypeHypothesis(enum.StrEnum):
    REVENUE_CANDIDATE = "revenue_candidate"
    DEADHEAD_OR_NONREVENUE_UNCONFIRMED = "deadhead_or_nonrevenue_unconfirmed"
    UNRESOLVED = "unresolved"


class ClusterStatus(enum.StrEnum):
    CONFIRMED_EXPERIMENTAL = "confirmed_for_this_experimental_run_only"
    AMBIGUOUS = "ambiguous"
    MISSING = "missing"


# Evidence family identifiers for terminal/boundary evidence. A terminal
# or short-turn episode normally requires >= 2 independent families with
# at least one PHYSICAL family (geofence/dwell/progress/outbound motion).
class EvidenceFamily(enum.StrEnum):
    GEOFENCE = "terminal_geofence"
    DWELL = "sustained_dwell"
    DIRECTION_TRANSITION = "source_direction_transition"
    PROGRESS_REVERSAL = "geometry_progress_reversal"
    STOP_SEQUENCE_RESTART = "stop_sequence_restart"
    APC_ALIGHTING_BEFORE = "apc_alighting_signal_before"
    APC_BOARDING_AFTER = "apc_boarding_signal_after"
    OUTBOUND_MOTION = "sustained_outbound_motion"


PHYSICAL_FAMILIES = frozenset(
    {
        EvidenceFamily.GEOFENCE,
        EvidenceFamily.DWELL,
        EvidenceFamily.PROGRESS_REVERSAL,
        EvidenceFamily.OUTBOUND_MOTION,
    }
)


@dataclass(frozen=True, slots=True)
class ApcBoundarySignal:
    """Source-level diagnostic signal for one APC record.

    Raw counts are preserved upstream and NEVER summed, rounded, or
    repaired here; only their positivity enters the derived state.
    """

    record_id: str
    boarding_field_positive: bool | None  # None when field missing
    alighting_field_positive: bool | None
    state: CountSignalState
    avg_sample: int | None
    quality: SignalQuality
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class VehicleActivityRun:
    """One coarse, GTFS-independent provisional vehicle activity run.

    This is NOT a performed trip and must never mint a trip id.
    """

    run_id: str
    vehicle: str
    record_ids: tuple[str, ...]  # ordered AVL source lineage
    start_instant_s: int
    end_instant_s: int
    time_uncertain: bool
    n_unresolved: int
    local_dates: tuple[str, ...]
    route_label_counts: dict[str, int] = field(hash=False)
    boundary_censored_start: bool = False
    boundary_censored_end: bool = False

    @property
    def duration_s(self) -> int:
        return self.end_instant_s - self.start_instant_s


@dataclass(frozen=True, slots=True)
class AlignmentEdge:
    """One trip-agnostic APC-record x activity-run compatibility edge.

    Evidence for boundary refinement ONLY. It never states a final
    segment owner, service date, scheduled trip, or stop occurrence, and
    it must not be reused later as proof the refined segment was correct
    (evidence-independence caveat, spike §24.D).
    """

    apc_source_record_id: str
    run_id: str
    fold: int
    arrival_epoch_s: int
    departure_epoch_s: int
    time_gap_to_run_s: int  # 0 when inside the run interval
    prev_ping_record_id: str | None
    next_ping_record_id: str | None
    prev_gap_s: int | None
    next_gap_s: int | None
    ping_to_stop_m: float | None  # interpolated AVL position vs APC coords
    terminal_cluster_id: str | None
    stop_in_cluster: bool
    route_label_agrees: bool | None
    direction_label: str | None
    signal_state: str
    signal_quality: str
    hard_valid: bool
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TerminalCluster:
    """Versioned experimental terminal/turnback cluster hypothesis.

    Never approved publication truth: the Wilson Station / stop-code
    1652 mapping is an open D-10 decision, so any cluster covering it
    carries ``Reason02.CLUSTER_EXPERIMENTAL_1652`` and status
    ``CONFIRMED_EXPERIMENTAL`` at best.
    """

    cluster_id: str
    name_hypothesis: str  # diagnostic label only
    feed_id: str
    service_dates: tuple[str, ...]
    centroid_lat: float
    centroid_lon: float
    geofence_radius_m: float
    member_stop_ids: tuple[str, ...]
    member_stop_codes: tuple[str, ...]
    pattern_endpoint_route_ids: tuple[str, ...]
    evidence_sources: tuple[str, ...]  # e.g. gtfs_pattern_endpoint
    status: ClusterStatus
    config_version: str
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TerminalEpisode:
    """One terminal/turnback episode candidate inside one activity run.

    Four distinct timestamp hypotheses; none is forced to exist. The
    required ordering (arrival <= service_end <= service_start <=
    departure, equality allowed) is CHECKED, never repaired: a violation
    marks the episode ambiguous with alternatives retained.
    """

    episode_id: str
    run_id: str
    cluster_id: str | None
    kind: str  # "terminal" | "short_turn_candidate"
    physical_arrival_s: int | None
    service_end_s: int | None
    service_start_s: int | None
    physical_departure_s: int | None
    evidence_families: tuple[str, ...]
    physical_evidence_present: bool
    apc_record_ids: tuple[str, ...]  # aligned APC evidence (lineage only)
    spanning_apc_record_ids: tuple[str, ...]
    ping_index_first: int  # index range within the run's ordered pings
    ping_index_last: int
    ordering_valid: bool
    status: EpisodeStatus
    apc_quality: SignalQuality | None
    alternative_timestamps: tuple[tuple[str, int], ...] = ()
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoundaryCandidate:
    """One candidate trip boundary inside one activity run.

    ``cut_start_s``/``cut_end_s`` bound the turnaround/unresolved
    interval the boundary would carve out; for an instantaneous cut they
    are equal. ``cut_index`` is the index (within the run's ordered
    pings) of the first ping AFTER the boundary interval.
    """

    boundary_id: str
    run_id: str
    episode_id: str | None
    source: str  # "terminal_episode" | "context_transition"
    cut_start_s: int
    cut_end_s: int
    cut_index_first_inside: int  # first ping index inside the cut interval
    cut_index_first_after: int  # first ping index after the cut interval
    evidence_families: tuple[str, ...]
    apc_precedence_used: bool
    quality: SignalQuality | None
    score_q: int
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoundaryDecision:
    boundary_id: str
    run_id: str
    status: BoundaryStatus
    score_q: int | None
    margin_q: int | None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnaroundInterval:
    """The interval between inbound service end and outbound service
    start. Never silently described as revenue movement; a downstream
    ``ownership_projection = departure`` experiment is reported
    separately and is not physical truth."""

    turnaround_id: str
    run_id: str
    episode_id: str | None
    cluster_id: str | None
    start_instant_s: int
    end_instant_s: int
    record_ids: tuple[str, ...]  # AVL pings in state terminal_turnaround


@dataclass(frozen=True)
class RefinedSegment:
    """One refined trip-scale segment candidate. NOT a confirmed
    performed trip; no production ``trip_id_performed`` is minted."""

    refined_segment_id: str
    run_id: str
    variant: str  # refinement variant that produced it (v0/v1/v2/vx)
    record_ids: tuple[str, ...]
    start_instant_s: int
    end_instant_s: int
    segment_type: SegmentTypeHypothesis
    boundary_id_before: str | None
    boundary_id_after: str | None
    time_uncertain: bool
    n_unresolved: int
    local_dates: tuple[str, ...]
    route_label_counts: dict[str, int] = field(hash=False)
    censored_start: bool = False
    censored_end: bool = False
    reason_codes: tuple[str, ...] = ()

    @property
    def duration_s(self) -> int:
        return self.end_instant_s - self.start_instant_s


@dataclass(frozen=True)
class SpanningApcRole:
    """Dual evidence roles for one boundary-spanning APC source record.

    One source record, two derived evidence anchors. Passenger fields
    are never duplicated or summed; no second passenger event exists.
    """

    apc_source_record_id: str
    run_id: str
    episode_id: str
    boundary_role: str  # always "spanning_terminal_visit"
    arrival_role_hypothesis: str  # "inbound_service_end_evidence"
    departure_role_hypothesis: str  # "outbound_service_start_evidence"
    arrival_segment_candidate: str | None  # refined segment id or None
    departure_segment_candidate: str | None
    dual_role: bool = True
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RefinementResult:
    """Frozen output of the single refinement pass over one run."""

    run_id: str
    variant: str
    refined_segments: tuple[RefinedSegment, ...]
    turnarounds: tuple[TurnaroundInterval, ...]
    unresolved_record_ids: tuple[str, ...]
    ping_states: dict[str, str] = field(hash=False)  # record_id -> state
    boundary_decisions: tuple[BoundaryDecision, ...] = ()
    applied_boundary_ids: tuple[str, ...] = ()
    ambiguous_boundary_ids: tuple[str, ...] = ()
    was_split: bool = False
