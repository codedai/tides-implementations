"""Complete candidate contracts and disposition taxonomy for the spike.

Design intent (R1-B06 in the spike's blocker reconstruction): candidates
are *complete tuples*, never bare segment IDs. Null alternatives are
first-class candidates that compete in the same scored decision. Every
normalized APC source record in a slice receives exactly one disposition.

Nothing here is a production S7 contract. Field names carry an ``x_``
mindset: they exist to be falsified by the experiment.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

# --- Null-hypothesis sentinels (explicit, comparable, serializable) ---

UNRESOLVED_SERVICE_DATE = "XNULL:UNRESOLVED_SERVICE_DATE"
NO_ROUTE_MAPPING = "XNULL:NO_ROUTE_MAPPING"
NO_SCHEDULED_TRIP = "XNULL:NO_SCHEDULED_TRIP"
NO_ATTACHMENT = "XNULL:NO_ATTACHMENT"


class EvidenceRegime(enum.StrEnum):
    AVL_ONLY = "avl_only"
    APC_EVIDENCE = "apc_evidence"


class SpatialEvidence(enum.StrEnum):
    """Three-state spatial evidence (never a forced nearest-shape match)."""

    PRESENT = "spatial_present"
    MISSING = "spatial_missing"
    WEAK = "spatial_weak"
    HARD_CONTRADICTION = "spatial_hard_contradiction"


class HardValidity(enum.StrEnum):
    VALID = "valid"
    HARD_INVALID = "hard_invalid"


class DispositionStatus(enum.StrEnum):
    """Exactly one of these per APC source record in the slice."""

    ACCEPTED = "accepted"
    NO_CANDIDATE = "no_candidate"
    NO_ATTACHMENT = "no_attachment"  # explicit null won
    AMBIGUOUS = "ambiguous"
    LOW_SCORE = "low_score"
    LOW_MARGIN = "low_margin"
    EVIDENCE_TIE = "evidence_tie"
    CAP_OVERFLOW = "cap_overflow"
    STALE = "stale"
    TIME_UNRESOLVED = "time_unresolved"
    GEOMETRY_CONTRADICTION = "geometry_contradiction"
    SEQUENCE_DEMOTED = "sequence_demoted"
    POLICY_BLOCKED = "policy_blocked"


class SegmentDecisionStatus(enum.StrEnum):
    """Stage 1 segment decision outcomes (diagnostic, not S7)."""

    ACCEPTED = "accepted"
    NULL_WINNER = "null_winner"
    AMBIGUOUS = "ambiguous"
    LOW_SCORE = "low_score"
    LOW_MARGIN = "low_margin"
    EVIDENCE_TIE = "evidence_tie"
    CAP_OVERFLOW = "cap_overflow"
    TIME_UNRESOLVED = "time_unresolved"
    NO_CANDIDATE = "no_candidate"
    SEQUENCE_DEMOTED = "sequence_demoted"


# --- Reason codes (stable strings; namespaced XR1.<area>.<code>) ---


class Reason:
    TIME_AMBIGUOUS_FOLD = "XR1.TIME.AMBIGUOUS_FOLD"
    TIME_UNRESOLVED_FOLD = "XR1.TIME.UNRESOLVED_FOLD"
    TIME_NONEXISTENT_LOCAL = "XR1.TIME.NONEXISTENT_LOCAL"
    TIME_PARSE_INVALID = "XR1.TIME.PARSE_INVALID"
    TIME_FOLD_SEQUENCE_RESOLVED = "XR1.TIME.FOLD_SEQUENCE_RESOLVED"
    TIME_FOLD_ROLLED_BACK = "XR1.TIME.FOLD_ROLLED_BACK"
    SEG_GAP_SPLIT = "XR1.SEG.GAP_SPLIT"
    SEG_CONTEXT_SPLIT = "XR1.SEG.CONTEXT_SPLIT"
    SEG_TIME_UNCERTAIN = "XR1.SEG.TIME_UNCERTAIN"
    DATE_UNRESOLVED = "XR1.DATE.UNRESOLVED"
    DATE_NO_FEED = "XR1.DATE.NO_FEED"
    ROUTE_UNMAPPED = "XR1.ROUTE.UNMAPPED"
    ROUTE_AMBIGUOUS = "XR1.ROUTE.AMBIGUOUS"
    STOP_UNMAPPED = "XR1.STOP.UNMAPPED"
    STOP_AMBIGUOUS = "XR1.STOP.AMBIGUOUS"
    VEHICLE_UNMAPPED = "XR1.VEHICLE.UNMAPPED"
    GEOM_MISSING = "XR1.GEOM.MISSING"
    GEOM_WEAK = "XR1.GEOM.WEAK"
    GEOM_HARD_CONTRADICTION = "XR1.GEOM.HARD_CONTRADICTION"
    GEOM_INVALID_COORDS = "XR1.GEOM.INVALID_COORDS"
    TEMPORAL_STALE = "XR1.TEMPORAL.STALE"
    TEMPORAL_INCOMPATIBLE_FOLD = "XR1.TEMPORAL.INCOMPATIBLE_FOLD"
    TEMPORAL_INCOMPATIBLE_DATE = "XR1.TEMPORAL.INCOMPATIBLE_DATE"
    SCORE_LOW = "XR1.SCORE.BELOW_THRESHOLD"
    SCORE_LOW_MARGIN = "XR1.SCORE.LOW_MARGIN"
    SCORE_EVIDENCE_TIE = "XR1.SCORE.EVIDENCE_TIE"
    NULL_WINNER = "XR1.SCORE.NULL_WINNER"
    CAP_SEGMENT_OVERFLOW = "XR1.CAP.SEGMENT_HYPOTHESIS_OVERFLOW"
    CAP_APC_OVERFLOW = "XR1.CAP.APC_CANDIDATE_OVERFLOW"
    CAP_COMPONENT_OVERFLOW = "XR1.CAP.COMPONENT_RECORD_OVERFLOW"
    SEQ_INCONSISTENT = "XR1.SEQ.INCONSISTENT"
    SEQ_AMBIGUOUS_DEMOTION = "XR1.SEQ.AMBIGUOUS_DEMOTION"
    SEQ_MULTIPLE_OPTIMA = "XR1.SEQ.MULTIPLE_OPTIMA"
    NO_SEGMENT_CANDIDATE = "XR1.ATTACH.NO_SEGMENT_CANDIDATE"
    ATTACH_SEGMENT_UNRESOLVED = "XR1.ATTACH.SEGMENT_UNRESOLVED"
    POLICY_BLOCKED = "XR1.POLICY.BLOCKED"


# --- Candidate tuples ---


@dataclass(frozen=True, slots=True)
class TimeCandidate:
    """One interpretation of one naive local wall time."""

    source_record_id: str
    wall_text: str
    timezone: str
    fold: int  # 0 or 1; 0 for unambiguous times
    utc_epoch_s: int | None  # None when structurally invalid
    utc_offset_s: int | None
    ambiguous: bool
    valid: bool
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SegmentCandidate:
    """One complete Stage-1 hypothesis for one experimental segment.

    Null alternatives use the XNULL sentinels in the corresponding
    fields; a fully-null candidate row is the segment's explicit
    "no defensible complete tuple" alternative.
    """

    segment_id: str
    service_date_hypothesis: str  # ISO date or UNRESOLVED_SERVICE_DATE
    feed_id: str | None
    route_mapping_hypothesis: str  # route_id or NO_ROUTE_MAPPING
    scheduled_trip_hypothesis: str  # trip_id or NO_SCHEDULED_TRIP
    evidence_regime: EvidenceRegime
    feature_vector: dict[str, float | int | None] = field(hash=False)
    hard_validity: HardValidity = HardValidity.VALID
    reason_codes: tuple[str, ...] = ()

    def candidate_key(self) -> tuple:
        """Identity of the hypothesis (used for serialization only —
        never as evidence or as a tie-breaker that manufactures a
        winner)."""
        return (
            self.segment_id,
            self.service_date_hypothesis,
            self.feed_id,
            self.route_mapping_hypothesis,
            self.scheduled_trip_hypothesis,
        )

    def is_null_alternative(self) -> bool:
        return self.service_date_hypothesis == UNRESOLVED_SERVICE_DATE or (
            self.route_mapping_hypothesis == NO_ROUTE_MAPPING
            and self.scheduled_trip_hypothesis == NO_SCHEDULED_TRIP
        )


@dataclass(frozen=True, slots=True)
class ApcCandidate:
    """One complete Stage-2 attachment hypothesis for one APC record.

    Hypotheses that share ``candidate_segment_id`` but differ in fold,
    date, route, or stop are distinct candidates and stay distinct for
    scoring and margins.
    """

    apc_source_record_id: str
    time_fold_hypothesis: int | None  # None only on the NO_ATTACHMENT row
    service_date_hypothesis: str
    feed_id: str | None
    vehicle_mapping_hypothesis: str | None
    route_mapping_hypothesis: str
    stop_mapping_hypothesis: str  # stop_id or STOP sentinel via reason
    candidate_segment_id: str  # segment id or NO_ATTACHMENT
    feature_vector: dict[str, float | int | None] = field(hash=False)
    hard_validity: HardValidity = HardValidity.VALID
    reason_codes: tuple[str, ...] = ()

    def candidate_key(self) -> tuple:
        return (
            self.apc_source_record_id,
            self.time_fold_hypothesis,
            self.service_date_hypothesis,
            self.feed_id,
            self.route_mapping_hypothesis,
            self.stop_mapping_hypothesis,
            self.candidate_segment_id,
        )

    def is_null_alternative(self) -> bool:
        return self.candidate_segment_id == NO_ATTACHMENT


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """A candidate with its integer qscore and score breakdown."""

    key: tuple
    qscore: int
    is_null: bool
    hard_validity: HardValidity
    regime: EvidenceRegime
    family_qscores: dict[str, int] = field(hash=False)
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Decision:
    """Outcome of one scored decision over a complete candidate set."""

    subject_id: str  # segment_id or apc_source_record_id
    status: str  # SegmentDecisionStatus or DispositionStatus value
    selected_key: tuple | None
    best_q: int | None
    runner_up_q: int | None
    null_q: int | None
    margin_q: int | None
    n_candidates: int
    n_hard_valid: int
    reason_codes: tuple[str, ...]
    regime: EvidenceRegime | None = None


EXCLUDED_IDENTITY_FEATURES = frozenset(
    {
        # Passenger/legacy firewall (spike §10): these may appear in
        # diagnostics but must never influence B1/B2 identity, date,
        # schedule, or attachment scores.
        "boarding",
        "alighting",
        "arrload",
        "depload",
        "avgsample",
        "notrips",
        "legacy_tripid",
        "legacy_trip_boundary",
        "legacy_passenger_load",
    }
)
