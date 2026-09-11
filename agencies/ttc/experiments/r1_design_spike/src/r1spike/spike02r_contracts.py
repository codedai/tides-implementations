"""Spike-02R contracts: kernel repair, evidence decoupling, labeling prep.

EXPERIMENTAL, DIAGNOSTIC, NON-PUBLISHABLE. Spike 02R repairs validity
defects in the Spike-02 experimental kernel before any architecture
conclusion may be drawn. It selects no architecture, closes nothing,
and claims no human correctness (no reviewed labels exist).

Reason codes are namespaced ``XR2R.<area>.<code>``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

PROFILE02R = "diagnostic-experimental-r1-spike-02r"
MARKERS02R = {
    "profile": PROFILE02R,
    "publishable": False,
    "tides_conformance_claimed": False,
    "normative_contract_claimed": False,
    "r1_closed": False,
    "architecture_selected": False,
    "human_correctness_claimed": False,
}
ALGORITHM_VERSION02R = "x-r1-spike02r-1"

# Experimental identity namespaces (spec section 7). These are never
# production identifiers.
CHUNK_ID_AVL = "xsg-avl"
CHUNK_ID_APC = "xsg-apc"
PERFORMED_HYPOTHESIS_ID = "xpt2r"  # NOT trip_id_performed

NO_BOUNDARY = "XNULL:NO_BOUNDARY"


class Reason02R:
    NOB_WINS = "XR2R.BOUNDARY.NO_BOUNDARY_WINS"
    BOUNDARY_TIE = "XR2R.BOUNDARY.TIED_OPTIMA"
    BOUNDARY_FRAG_PENALTY = "XR2R.BOUNDARY.FRAGMENTATION_PENALIZED"
    BOUNDARY_NOT_MANDATORY = "XR2R.BOUNDARY.NOT_IN_ALL_OPTIMA"
    CENSOR_PROOF_INVALID = "XR2R.CENSOR.PROOF_INVALID"
    CENSOR_PLAUSIBLE = "XR2R.CENSOR.PLAUSIBLE_BUT_CENSORED"
    CENSOR_CONTRACT = "XR2R.CENSOR.CONTRACT_UNRESOLVED"
    CENSOR_COVERAGE = "XR2R.CENSOR.COVERAGE_MISSING"
    CENSOR_POLICY = "XR2R.CENSOR.POLICY_BLOCKED"
    UNIQUE_SURVIVOR = "XR2R.MARGIN.UNIQUE_SURVIVOR_BY_EXCLUSION"
    EVIDENCE_MARGIN_FAIL = "XR2R.MARGIN.EVIDENCE_MARGIN_BELOW_BAR"
    CONTRACT_COMPETITOR = "XR2R.MARGIN.CONTRACT_UNRESOLVED_COMPETITOR"
    REGIME_RECLASSIFIED = "XR2R.REGIME.RECLASSIFIED_APC_EVIDENCE"
    CROSSFIT_OUT_OF_FOLD = "XR2R.CROSSFIT.OUT_OF_FOLD"
    CROSSFIT_SELF_REUSE = "XR2R.CROSSFIT.SAME_RECORD_REUSE_BLOCKED"
    ANCHOR_REVOKED = "XR2R.ANCHOR.REVOKED_BY_GLOBAL_PATH"
    ANCHOR_RETAINED = "XR2R.ANCHOR.RETAINED"
    OPTIMUM_INCOMPLETE = "XR2R.OPTIMUM.ANALYSIS_INCOMPLETE"
    OPTIMUM_NOT_MANDATORY = "XR2R.OPTIMUM.ASSIGNMENT_NOT_MANDATORY"
    PATH_NULL_WINS = "XR2R.PATH.NULL_PATH_WINS"
    PATH_LOW_QUALITY = "XR2R.PATH.ABSOLUTE_QUALITY_LOW"
    PATH_LOW_MARGIN = "XR2R.PATH.MARGIN_LOW"
    STITCH_APPLIED = "XR2R.STITCH.APPLIED"
    STITCH_TIED = "XR2R.STITCH.TIED_OPTIMA_ABSTAIN"
    STITCH_REFUSED = "XR2R.STITCH.NO_STITCH_SELECTED"
    FRAGMENT_UNRESOLVED = "XR2R.FRAGMENT.UNRESOLVED"
    TERMINAL_SCOPE = "XR2R.TERMINAL.FEED_DATE_SCOPE_ENFORCED"


class CensorClass(enum.StrEnum):
    PROOF_INVALID = "proof_invalid"
    HARD_VALID = "hard_valid"
    PLAUSIBLE_BUT_CENSORED = "plausible_but_censored"
    CONTRACT_UNRESOLVED = "contract_unresolved"
    COVERAGE_MISSING = "coverage_missing"
    POLICY_BLOCKED = "policy_blocked"


class ChunkRole(enum.StrEnum):
    TRIP_CORE_CANDIDATE = "trip_core_candidate"
    ARRIVAL_FRAGMENT = "arrival_fragment"
    DEPARTURE_FRAGMENT = "departure_fragment"
    TERMINAL_TURNAROUND = "terminal_turnaround"
    BOUNDARY_CENSORED_CHUNK = "boundary_censored_chunk"
    UNRESOLVED_FRAGMENT = "unresolved_fragment"


class EvidenceUse(enum.StrEnum):
    BOUNDARY_GENERATION = "boundary_generation"
    ATTACHMENT_SCORING = "attachment_scoring"
    ANCHOR_SCORING = "anchor_scoring"
    PATH_SCORING = "path_scoring"


@dataclass(frozen=True, slots=True)
class CandidateKey:
    """Complete GTFS candidate identity (spec section 14). Never collapse
    to bare trip_id."""

    service_date: str
    feed_id: str | None
    route_mapping_id: str
    scheduled_trip_id: str | None  # None == no-scheduled-trip alternative
    polarity_hypothesis_id: str | None  # pol_a | pol_b | None

    def as_tuple(self) -> tuple:
        return (
            self.service_date,
            self.feed_id,
            self.route_mapping_id,
            self.scheduled_trip_id,
            self.polarity_hypothesis_id,
        )

    def serialize(self) -> str:
        return "|".join("" if p is None else str(p) for p in self.as_tuple())


@dataclass(frozen=True, slots=True)
class EvidenceEdge:
    """One provenance edge: a derived entity depends on a source record."""

    derived_id: str
    derived_kind: str  # boundary | anchor | attachment | path | feature
    source_record_id: str
    source_type: str  # avl | apc | gtfs | config
    evidence_family: str
    use: str  # EvidenceUse value
    fold: str | None = None  # A | B | None (not cross-fitted)
    dependency_generation: int = 1
    independent_confirmation: bool = False
    overlaps_boundary_evidence: bool = False
    reuse_reason: str | None = None


@dataclass(frozen=True)
class BoundaryCandidateV2:
    """Complete boundary candidate (spec section 11)."""

    candidate_id: str
    activity_run_id: str
    candidate_start_s: int
    candidate_end_s: int
    boundary_role: str  # terminal | short_turn | context
    evidence_families: tuple[str, ...]
    evidence_regime: str  # avl_only | apc_evidence
    qscore: int
    hard_status: str  # CensorClass value
    dependency_records: tuple[str, ...] = ()  # source record ids
    service_derived: bool = False  # cut interval from APC service anchors
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoundaryDecisionV2:
    candidate_id: str  # or NO_BOUNDARY sentinel for region nulls
    activity_run_id: str
    selected: bool
    mandatory_in_all_optima: bool | None
    survival_margin_q: int | None
    evidence_margin_q: int | None
    margin_vs_no_boundary_q: int | None
    status: str  # applied | no_boundary | tied_abstain | censored_out | ...
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DualMarginDecision:
    """One scored decision carrying both margins (spec section 12)."""

    subject_id: str
    status: str  # accepted | unique_survivor_by_exclusion | abstain states
    best_key: str | None
    best_q: int | None
    survival_margin_q: int | None
    evidence_margin_q: int | None
    blocking_contract_competitors: tuple[str, ...] = ()
    censored_alternatives_considered: int = 0
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChunkRecord:
    """A refined chunk with its experimental profile identity."""

    chunk_id: str  # xsg-avl-* or xsg-apc-*
    profile: str  # avl | apc-structural | apc-activity
    run_id: str
    record_ids: tuple[str, ...]
    start_instant_s: int
    end_instant_s: int
    role: ChunkRole
    role_evidence: tuple[str, ...] = ()
    censored_start: bool = False
    censored_end: bool = False

    @property
    def duration_s(self) -> int:
        return self.end_instant_s - self.start_instant_s


@dataclass(frozen=True)
class StitchDecision:
    stitch_id: str
    run_id: str
    member_chunk_ids: tuple[str, ...]
    status: str  # stitched | no_stitch | tied_abstain
    performed_trip_hypothesis_id: str | None
    score_q: int | None
    margin_q: int | None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlignmentAssignment:
    """Revocable-anchor alignment output per chunk (spec sections 15-17)."""

    segment_id: str
    status: str  # matched | anchor_retained | anchor_revoked | unmatched |
    #              tied | unresolved_incomplete_optimum | null_path
    candidate_key: str | None
    mandatory_in_all_optima: bool | None
    best_without_cost_delta_q: int | None
    break_penalty_applied: bool = False
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OptimumStability:
    component_id: str
    n_states: int
    within_bound: bool
    optimum_analysis_incomplete: bool
    best_cost: int | None
    assignments: tuple[AlignmentAssignment, ...] = ()
    reason_codes: tuple[str, ...] = ()


@dataclass
class Spike02RAssumptions:
    items: list[str] = field(default_factory=lambda: ASSUMPTIONS02R)


ASSUMPTIONS02R = [
    "ADR 0003 acceptance is attributed to the invoking project owner's "
    "recorded instruction; revocation clause applies if that attribution "
    "is incorrect",
    "cross-fitting removes direct same-record reuse only; it is not "
    "independent ground truth",
    "no reviewed human labels exist: no precision/recall/architecture "
    "correctness is computed or claimed",
    "censorship classification of blocked alternatives is itself an "
    "experimental taxonomy (e.g. fold-heuristic blocks are counted as "
    "plausible_but_censored)",
    "terminal clusters remain experimental; 1652=Wilson is a D-10-open "
    "hypothesis and is classified contract_unresolved for acceptance "
    "purposes",
    "all thresholds, penalties, and caps remain uncalibrated experimental "
    "hypotheses (D-14)",
    "naive America/Toronto wall-time interpretation (G0.3/D-16 open)",
    "candidate GTFS manifest override (D-15 open)",
]

UNRESOLVED02R = [
    "R1-B01..R1-B11 (R1 remains open)",
    "D-06 APC count semantics; D-07 visit-key collisions; D-10 stop 1652; "
    "D-11 direction semantics; D-14 calibration; D-15 GTFS approval",
    "XR2R: boundary global-objective penalties are unapproved",
    "XR2R: stitch/split objective is unapproved",
    "XR2R: label schema unreviewed by a second party",
]
