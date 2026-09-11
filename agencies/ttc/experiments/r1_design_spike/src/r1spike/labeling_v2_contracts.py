"""Frozen constants and contract payloads for Human Labeling Protocol v2.

Single source of truth for the v2 label ontology, the candidate-presentation
contract, the child-case generation contract, the promotion contract, and the
blind-review protocol. `labeling_v2_setup` serializes these into the run
directory; the UI and the store verify the serialized copies against the
freeze manifest before accepting a label.
"""

from __future__ import annotations

PROTOCOL_VERSION = "2.0"
SCHEMA_VERSION = "human-label-v2.0"
UI_VERSION = "2.0.0"
PRESENTATION_VERSION = "candidate-presentation-v2.0"
CHILD_CASE_GENERATION_VERSION = "child-case-generation-v2.0"
PROMOTION_CONTRACT_VERSION = "label-promotion-v2.0"
PRODUCER = "r1spike.run_labeling_v2_setup"
PROFILE = "diagnostic-experimental-r1-human-labeling-v2-run01"
GENERATION_TIME_BASIS = (
    "inherited_from_human_label_audit_v1_generated_time_for_reproducibility"
)

ARCHITECTURE_VERDICT = "INCONCLUSIVE — NO REVIEWED HOLDOUT LABELS"
FACTORIAL_STATUS_NO_GO = "NO_GO"

# --- v2 ontology (section 6 of the frozen protocol) -------------------------

CASE_GRAINS = (
    "single_trip_candidate",
    "multi_trip_chunk",
    "local_fragment",
    "boundary_spanning",
    "turnaround_or_idle",
    "data_quality_invalid",
    "coverage_censored",
    "unknown",
)

REVIEWABILITY = (
    "reviewable_for_schedule_match",
    "not_reviewable_at_current_grain",
    "insufficient_context",
    "invalid_source_state",
    "requires_boundary_adjudication",
    "requires_segmentation_adjudication",
)

SCHEDULED_JUDGMENTS = (
    "matched",
    "no_scheduled_match",
    "ambiguous_candidates",
    "insufficient_evidence",
    "not_applicable",
)

FRAGMENT_ROLES = (
    "trip_core",
    "arrival_fragment",
    "departure_fragment",
    "turnaround",
    "stitch_previous",
    "stitch_next",
    "independent_short_trip",
    "ambiguous_fragment",
    "not_applicable",
)

ACTIVITY_STATES = (
    "revenue_trip_candidate",
    "terminal_turnaround",
    "idling",
    "deadhead_or_nonrevenue_unconfirmed",
    "pullin_or_pullout_unconfirmed",
    "coverage_censored",
    "unknown",
    "not_applicable",
)

QUALITY_FLAGS = (
    "simultaneous_positions",
    "impossible_speed",
    "unresolved_dst_fold",
    "trajectory_not_split",
    "missing_avl_coverage",
    "spatial_temporal_conflict",
    "candidate_universe_incomplete",
    "timezone_text_inconsistent",
    "other",
)

TRISTATE = ("true", "false", "ambiguous", "not_applicable")

EVIDENCE_FAMILIES = (
    "interval_time_fit",
    "stop_sequence_fit",
    "spatial_shape_fit",
    "terminal_behavior",
    "apc_signal",
    "direction_or_polarity",
    "schedule_headway_separation",
    "adjacent_context",
    "data_quality_signal",
)

SINGLE_DECISIVE_ANCHOR = "single_decisive_anchor"

CANDIDATE_KEY_FIELDS = (
    "service_date",
    "feed_id",
    "route_mapping_id",
    "scheduled_trip_id",
    "polarity_hypothesis_id",
)

NO_SCHEDULED_MATCH_ALIAS = "NONE"
NO_SCHEDULED_MATCH_KEY = "NO_SCHEDULED_MATCH"

# --- review states and event types (section 20) -----------------------------

REVIEW_STATES = (
    "FIRST_PASS_PRESERVED",
    "SECOND_PASS_NOT_STARTED",
    "SECOND_PASS_SAVED",
    "FIRST_SECOND_AGREE",
    "FIRST_SECOND_DISAGREE",
    "REQUIRES_ADJUDICATION",
    "ADJUDICATED",
    "NOT_EVALUABLE_AT_CURRENT_GRAIN",
)

EVENT_TYPES = (
    "second_pass_save",
    "first_pass_reveal",
    "diagnostics_reveal",
    "post_reveal_confirm",
    "post_reveal_revision",
    "adjudication_save",
    "routing_change",
    "child_case_request",
    "candidate_filter_applied",
    "panel_view",
    "adjacent_context_view",
)

TASK_TYPES = (
    "scheduled_trip_second_pass",
    "segmentation_multi_trip",
    "boundary_spanning_dual_role",
    "fragment_role_and_stitching",
    "insufficient_context",
    "source_time_quality",
    "no_match_activity",
    "scheduled_trip_abstention",
)

CHILD_TASK_TYPES = (
    "subtrip_scheduled_match",
    "boundary_interval",
    "inbound_context",
    "outbound_context",
    "fragment_role",
    "stitch_decision",
    "source_time_quality",
    "activity_state",
    "sequence_review",
)

# A child case's grain is pinned by its manifest task; the store enforces
# this (a child can never be re-graded into e.g. a complete-trip match).
CHILD_GRAIN_BY_TASK = {
    "subtrip_scheduled_match": "single_trip_candidate",
    "boundary_interval": "boundary_spanning",
    "inbound_context": "boundary_spanning",
    "outbound_context": "boundary_spanning",
    "fragment_role": "local_fragment",
    "stitch_decision": "local_fragment",
    "sequence_review": "multi_trip_chunk",
    "source_time_quality": "data_quality_invalid",
    "activity_state": "turnaround_or_idle",
}

AUDIT_CHILD_REQUEST_COUNT = 11

TASK_SPECIFIC_LABEL_SETS = (
    "schedule_gold",
    "schedule_abstention_gold",
    "segmentation_gold",
    "boundary_gold",
    "fragment_gold",
    "quality_gold",
    "activity_gold",
    "pending_adjudication",
    "not_evaluable",
)

# --- mandatory case routing baseline (section 4; audit-derived) -------------

SCHEDULED_SECOND_PASS_CASES = (
    "C-134f2a21",
    "C-179eba6d",
    "C-1b38b137",
    "C-47d3a45a",
    "C-5555da7a",
    "C-88bc09d3",
    "C-b7163d0c",
)
MULTI_TRIP_CASES = ("C-2630793f", "C-ba6b0e92")
BOUNDARY_SPANNING_CASES = ("C-913f7d4c", "C-c953dac7")
FRAGMENT_CASES = ("C-0ffcd227", "C-3eb39b05", "C-7d4b42b8", "C-af359271")
INSUFFICIENT_CONTEXT_CASES = ("C-ba617064",)
QUALITY_CASES = ("C-7d5fcf98",)
ACTIVITY_CASES = ("C-88e48154",)

ALL_PILOT_CASES = tuple(
    sorted(
        SCHEDULED_SECOND_PASS_CASES
        + MULTI_TRIP_CASES
        + BOUNDARY_SPANNING_CASES
        + FRAGMENT_CASES
        + INSUFFICIENT_CONTEXT_CASES
        + QUALITY_CASES
        + ACTIVITY_CASES
    )
)

# --- run-directory artifact allowlists --------------------------------------

FROZEN_ARTIFACTS = (
    "label_schema_v2.0.json",
    "label_ui_contract_v2.0.md",
    "candidate_presentation_contract_v2.0.json",
    "child_case_generation_contract_v2.0.json",
    "label_promotion_contract_v2.0.json",
    "blind_review_protocol_v2.0.md",
    "protocol_freeze_manifest.json",
    "candidate_universe_manifest.parquet",
    "candidate_alias_map.parquet",
    "case_routing_manifest.parquet",
    "child_case_manifest.parquet",
    "blind_review_order.json",
    "ui_run_manifest.json",
    "input_hashes.json",
    "source_tree_manifest.json",
    "logical_checksums.json",
)

LIVE_ARTIFACTS = (
    "second_pass_labels_v2.parquet",
    "review_history.jsonl",
    "adjudication_queue_v3.csv",
    "adjudicated_labels_v2.parquet",
    "task_specific_label_sets.json",
    "intra_reviewer_agreement.json",
    "label_readiness_report_v2.json",
    "factorial_go_no_go.json",
)

PROTOCOL_DOCUMENTS = (
    "label_schema_v2.0.json",
    "label_ui_contract_v2.0.md",
    "candidate_presentation_contract_v2.0.json",
    "child_case_generation_contract_v2.0.json",
    "label_promotion_contract_v2.0.json",
    "blind_review_protocol_v2.0.md",
)

# Second-pass label record columns (the parquet schema, all nullable strings
# unless suffixed otherwise; serialized JSON for structured fields).
SECOND_PASS_COLUMNS = (
    "record_id",
    "case_id",
    "parent_case_id",
    "is_child_case",
    "child_task_type",
    "review_round",
    "reviewer_id",
    "session_id",
    "case_grain",
    "reviewability",
    "scheduled_judgment",
    "selected_candidate_alias",
    "selected_service_date",
    "selected_feed_id",
    "selected_route_mapping_id",
    "selected_scheduled_trip_id",
    "selected_polarity_hypothesis_id",
    "selected_explicit_null_polarity",
    "selected_candidate_key",
    "fragment_role",
    "fragment_confidence",
    "stitch_previous",
    "stitch_next",
    "local_scheduled_context_candidate_key",
    "complete_trip_judgment",
    "activity_state",
    "quality_state",
    "quality_flags",
    "scheduled_match_applicable",
    "subtrip_count",
    "ordered_candidate_sequence",
    "split_intervals_rel_s",
    "split_confidence",
    "unresolved_subsegments",
    "inbound_candidate_key",
    "outbound_candidate_key",
    "boundary_stop_hypothesis",
    "boundary_time_interval_rel_s",
    "boundary_timezone_label_derived",
    "arrival_role",
    "departure_role",
    "dual_role",
    "grain_confidence",
    "match_confidence",
    "evidence_quality",
    "evidence_flags",
    "single_decisive_anchor_family",
    "rationale_text",
    "notes",
    "outside_displayed_chunk_warning",
    "bounds_check_note",
    "adjacent_context_used",
    "panels_viewed",
    "candidate_filters_applied",
    "candidates_hidden_by_filter_at_save",
    "diagnostics_revealed_before_save",
    "diagnostics_revealed_after_save",
    "first_pass_hidden",
    "post_reveal_revision_of",
    "review_started_at",
    "review_saved_at",
    "protocol_version",
    "protocol_sha256",
    "ui_version",
    "candidate_universe_hash",
    "blind_review_order_position",
)

ADJUDICATION_COLUMNS = (
    "record_id",
    "case_id",
    "parent_case_id",
    "adjudicated_by",
    "session_id",
    "resolution",
    "resolved_case_grain",
    "resolved_scheduled_judgment",
    "resolved_candidate_key",
    "resolution_rationale",
    "first_pass_record_ref",
    "second_pass_record_ref",
    "adjudicated_at",
    "protocol_version",
    "protocol_sha256",
)

ADJUDICATION_RESOLUTIONS = (
    "second_pass_upheld",
    "first_pass_upheld",
    "new_judgment",
    "not_evaluable_at_current_grain",
    "deferred",
)


def label_schema_v2_payload() -> dict:
    """The frozen v2 label schema document (label_schema_v2.0.json)."""

    return {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "status": "frozen_for_human_labeling_v2_run01",
        "supersedes": ["xlabel-1", "human-label-v2-proposal"],
        "reviewed_by_human": False,
        "dimensions_are_separate": [
            "case_grain",
            "reviewability",
            "scheduled_judgment",
            "fragment_role",
            "activity_state",
            "quality_state",
            "confidence",
        ],
        "case_grain": {"enum": list(CASE_GRAINS)},
        "reviewability": {"enum": list(REVIEWABILITY)},
        "scheduled_judgment": {
            "enum": list(SCHEDULED_JUDGMENTS),
            "applicability": (
                "only when the human-decided case grain permits a "
                "scheduled-trip decision; never forced by the application"
            ),
            "candidate_key": {
                "fields": list(CANDIDATE_KEY_FIELDS),
                "serialization": "service_date|feed_id|route_mapping_id|"
                "scheduled_trip_id|polarity_hypothesis_id",
                "explicit_null_polarity": (
                    "a trailing empty fifth field is a complete tuple with an "
                    "explicit null polarity hypothesis; it is never silently "
                    "filled"
                ),
                "card_letters_are_display_only": True,
                "no_label_may_serialize_alias_only": True,
            },
            "null_option": {
                "alias": NO_SCHEDULED_MATCH_ALIAS,
                "key": NO_SCHEDULED_MATCH_KEY,
                "always_visible": True,
            },
        },
        "fragment_role": {
            "enum": list(FRAGMENT_ROLES),
            "stitch_previous": {"enum": list(TRISTATE)},
            "stitch_next": {"enum": list(TRISTATE)},
            "local_scheduled_context_is_not_performed_trip_identity": True,
        },
        "activity_state": {"enum": list(ACTIVITY_STATES)},
        "quality_state": {
            "flags": list(QUALITY_FLAGS),
            "multiple_flags_allowed": True,
            "scheduled_match_not_forced_when_invalid": True,
        },
        "multi_trip_representation": {
            "ordered_candidate_sequence": (
                "ordered list of complete candidate keys or nulls, one per subtrip"
            ),
            "split_intervals_rel_s": (
                "list of [lo, hi] integer second intervals relative to the "
                "parent chunk start; intervals, never forced exact seconds"
            ),
            "split_confidence": "ordinal_1_to_5",
            "unresolved_subsegments": "integer count",
        },
        "boundary_spanning_representation": {
            "inbound_candidate_key": "complete key, null, or unresolved",
            "outbound_candidate_key": "complete key, null, or unresolved",
            "boundary_stop_hypothesis": "stop code text or null",
            "boundary_time_interval_rel_s": "[lo, hi] relative seconds or null",
            "arrival_role": "string or null",
            "departure_role": "string or null",
            "dual_role": "boolean",
            "sides_may_differ_and_may_remain_unresolved": True,
            "timezone_label_is_derived_from_timestamp_not_reviewer_text": True,
        },
        "confidence": {
            "grain_confidence": "ordinal_1_to_5",
            "match_confidence": "ordinal_1_to_5_or_null",
            "evidence_quality": "ordinal_1_to_5",
            "high_invalid_confidence_is_not_match_confidence": True,
        },
        "evidence_and_review_metadata": {
            "evidence_flags": {"enum": list(EVIDENCE_FAMILIES)},
            "single_decisive_anchor": {
                "flag": SINGLE_DECISIVE_ANCHOR,
                "requires_identified_family": True,
            },
            "rationale_requirements": {
                "confidence_4_or_5_requires_structured_rationale": True,
                "confidence_4_or_5_requires_free_text_rationale": True,
                "scheduled_match_normally_requires_two_evidence_families": True,
            },
            "fields": [
                "evidence_flags",
                "rationale_text",
                "adjacent_context_used",
                "panels_viewed",
                "diagnostics_revealed_before_save",
                "diagnostics_revealed_after_save",
                "first_pass_hidden",
                "review_round",
                "reviewer_id",
                "review_started_at",
                "review_saved_at",
                "session_id",
                "protocol_version",
                "ui_version",
                "candidate_universe_hash",
            ],
        },
        "second_pass_record_columns": list(SECOND_PASS_COLUMNS),
        "adjudication_record_columns": list(ADJUDICATION_COLUMNS),
        "review_states": list(REVIEW_STATES),
        "first_pass_immutability": {
            "first_pass_artifacts_are_never_mutated": True,
            "audit_suggestions_are_not_labels": True,
            "second_pass_stored_separately": True,
            "adjudication_is_a_third_record": True,
        },
    }


def candidate_presentation_payload() -> dict:
    """candidate_presentation_contract_v2.0.json payload."""

    return {
        "presentation_version": PRESENTATION_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "universe_source": {
            "artifact": "x_complete_schedule_candidates.parquet",
            "join": "labeling_packet_restricted.parquet case_id -> real_id == chunk_id",
            "policy": (
                "every candidate row for the case's chunk is retained, "
                "including explicit no-scheduled-trip alternatives and "
                "contract-unresolved sentinel keys (flagged with a warning); "
                "no candidate is removed for a poor model score"
            ),
            "null_option": {
                "alias": NO_SCHEDULED_MATCH_ALIAS,
                "key": NO_SCHEDULED_MATCH_KEY,
                "always_included": True,
            },
            "score_columns_excluded_from_manifest": [
                "qscore",
                "regime",
                "n_aligned",
                "n_observed",
            ],
        },
        "neutral_ordering": {
            "keys": [
                "service_date (sentinel dates last)",
                "feed_id",
                "route_mapping_id",
                "scheduled_start_epoch_s (null last)",
                "scheduled_trip_id",
                "polarity_hypothesis_id",
            ],
            "forbidden_ordering_inputs": [
                "model score",
                "model rank",
                "acceptance status",
                "margin",
                "anchor status",
            ],
            "display_order_is_persisted": True,
        },
        "aliases": {
            "assignment": (
                "A, B, ... Z, AA, AB, ... assigned strictly after neutral "
                "ordering; immutable for the review round"
            ),
            "mapping_fields": [
                "case_id",
                "candidate_alias",
                "candidate_key",
                "presentation_version",
                "candidate_universe_hash",
                "display_order",
            ],
            "aliases_are_display_only": True,
            "v1_card_letters_do_not_carry_over": True,
        },
        "usability": {
            "filters": [
                "service_date",
                "feed_id",
                "route_mapping_id",
                "polarity_hypothesis_id",
                "scheduled_start_range",
            ],
            "pagination": "chronological by scheduled start",
            "hidden_count_always_displayed": True,
            "reset_filters_control": True,
            "show_all_control": True,
            "reviewer_filters_are_logged": True,
            "model_top_k_never_substitutes_universe": True,
        },
        "candidate_universe_hash": (
            "per-case sha256 of the canonical JSON of the neutrally ordered "
            "candidate_key list"
        ),
    }


def child_case_generation_payload() -> dict:
    """child_case_generation_contract_v2.0.json payload."""

    return {
        "generation_version": CHILD_CASE_GENERATION_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "source": "artifacts/human-label-audit-v1/child_case_requests.csv",
        "child_task_types": list(CHILD_TASK_TYPES),
        "id_scheme": "<parent_case_id>::<slug>-<n> (deterministic)",
        "rules": [
            "fragment_role_case -> 1 fragment_role child",
            "per_subtrip_cases -> N subtrip_scheduled_match children",
            "boundary_cases -> N boundary_interval children",
            "boundary_inbound_outbound_cases -> boundary_interval + "
            "inbound_context + outbound_context children",
            "multi-trip parents additionally receive 1 sequence_review child "
            "(protocol addition beyond the audit's 11 requests; the count "
            "difference is reported, not forced)",
        ],
        "seeding": {
            "parent_note_time_references_are_context_seeds_only": True,
            "seed_status": "human_note_seeded_unverified",
            "no_child_label_is_prefilled": True,
            "parent_labels_are_never_copied_to_children": True,
        },
        "reviewer_initiated_children": {
            "allowed": True,
            "recorded_as": "child_case_request events in review_history.jsonl",
            "generation_reason": "second_pass_reviewer_reclassification",
        },
        "determinism": (
            "identical frozen inputs and contract version produce "
            "byte-identical child_case_manifest.parquet"
        ),
    }


def label_promotion_payload() -> dict:
    """label_promotion_contract_v2.0.json payload."""

    return {
        "promotion_version": PROMOTION_CONTRACT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "no_global_gold_field": True,
        "task_specific_sets": list(TASK_SPECIFIC_LABEL_SETS),
        "schedule_gold_gate": [
            "final case grain is single_trip_candidate",
            "reviewability is reviewable_for_schedule_match",
            "candidate_universe_hash equals the frozen per-case universe hash",
            "selected candidate has a complete five-position key from the "
            "frozen universe",
            "blind second pass complete with a usable first-pass comparison",
            "first/second pass agree, or adjudication upholds the second "
            "pass (deferred or overturned judgments never promote)",
            "structured rationale present",
            "no unresolved notes-label conflict (the notes must not cite a "
            "different candidate key than the selected one)",
            "prohibited diagnostics not revealed before the blind decision",
            "protocol and UI versions match the frozen contract",
        ],
        "confidence_alone_never_promotes": True,
        "task_labels_promote_only_to_their_own_sets": True,
        "pilot_cases": {
            "development_pilot": True,
            "independent_holdout_eligible": False,
            "case_ids": list(ALL_PILOT_CASES),
        },
        "promotion_and_factorial_readiness_are_separate_decisions": True,
    }


def blind_review_protocol_body() -> str:
    """blind_review_protocol_v2.0.md body (repository-safe wording)."""

    return f"""# Blind review protocol v{PROTOCOL_VERSION}

## Scope

Second-pass human review of the 18 pilot cases from the Spike 02R labeling
packet, under the frozen v2 ontology ({SCHEMA_VERSION}). The 18 cases are
pilot, ontology-discovery, UI-regression, and adjudication cases. They are
excluded from independent holdout use permanently. The architecture verdict
remains: {ARCHITECTURE_VERDICT}. The preregistered factorial remains blocked.

## Blinding rules

Before the current primary judgment is saved, the application must not load
into any visible state:

- the first-pass label, card choice, notes, confidence, or reviewer identity;
- the audit routing suggestion or task name (parents see a neutral header
  until they answer the grain question), the audit priority ranking, and
  the audit reason codes (masked in the live queue until the case's save);
- model score, model rank, anchor status, accepted/low_margin status,
  evidence margin, survival margin, unique-survivor status, cross-fit
  results, or any prior architecture-variant result;
- the packet `summary` field (it embeds an anchor status word).

The run directory necessarily stores restricted derived metadata (routing
baseline, note-derived child seeds) on disk for post-save use; the reviewer
must not open run-directory artifacts directly while their own blind pass
is in progress — the application is the only sanctioned view.

The five-card model-informed shortlist of the first pass is replaced by the
complete, neutrally ordered candidate universe (candidate presentation
contract {PRESENTATION_VERSION}).

## Grain-first workflow

The first required question is: what does the current object represent?
Only after that judgment does the application route to a task-specific form.
The audit routing baseline is editable by the human; a routing change is
stored as a new human judgment (`routing_change` event), never silently
applied.

## Case ordering

Cases are presented in a deterministic pseudo-random order seeded from the
frozen protocol hash (see `blind_review_order.json`). Cases are not grouped
by first-pass label; fragments are not presented consecutively unless the
reviewer explicitly filters.

## Rationale requirements

Confidence 4 or 5 requires at least one structured rationale flag and a
short free-text rationale. A scheduled match normally requires at least two
evidence families; a single decisive family is allowed only when explicitly
marked `{SINGLE_DECISIVE_ANCHOR}` with the family identified.

## Logging

Panels viewed, candidate filters applied, adjacent-context views,
diagnostics reveals, save and edit events, and time spent are recorded in
the append-only review history for bias and protocol auditing, not reviewer
surveillance.

## After the blind save

The saved blind judgment is immutable. The application then compares it with
the first pass, records agreement or disagreement, creates an adjudication
item on disagreement, and only then permits revealing the prior judgment and
model diagnostics. A post-reveal change is a new review-history event and a
separate record; it never overwrites the blind judgment. Adjudication is a
third explicit record and overwrites neither pass.

## Versioning

Labels reference the exact protocol hash. If any protocol document changes
after labeling starts, the protocol version must be bumped and labels must
not be mixed across versions without an explicit compatibility review.
"""


def label_ui_contract_body() -> str:
    """label_ui_contract_v2.0.md body (repository-safe wording)."""

    return f"""# Label UI contract v{PROTOCOL_VERSION}

## Common context strip (every task page)

Pseudonymous case ID; current task (for parents the task name is revealed
only after the grain decision — a neutral header is shown before it); chunk
duration; AVL ping count; APC
record count (scope: distinct anchor-evidence APC sources); observed stop
count where derivable; route labels; candidate count; displayed context
before/after; source-coverage and censoring warnings. The original packet
sampling stratum, if shown, is labeled "Original packet sampling stratum —
not a human truth label." The packet fields `n_events` and `span_rel_s`
describe the synthetic packet record and are not shown as operational
counts.

## Required visualizations

1. Route/spatial view: current AVL trace with time-direction, previous and
   next chunk traces in muted styles, APC markers, terminal hypotheses,
   candidate GTFS shapes, scheduled stops, geometry warnings; every layer
   toggleable; raw coordinates stay local.
2. Context timeline: previous/current/next chunk, gaps, dwell, APC
   intervals, terminal episodes, candidate scheduled windows, DST/censoring
   warnings; the current labeling window and adjacent context are visually
   distinct; a decision based on adjacent context sets
   `adjacent_context_used = true`.
3. Time-distance / time-progress plot: AVL progress over time for the
   current and adjacent chunks, direction reversal, dwell, scheduled
   stop-time points, APC events, competing time tracks under DST ambiguity.
4. Stop-occurrence ladder: scheduled stop occurrences in order (repeated
   stop IDs never collapsed), observed anchors, matched/skipped/extra
   occurrences, timing residuals.
5. Candidate comparison table: complete key components, scheduled
   start/end, chunk duration, interval overlap, start/end residuals, stop
   coverage, observed-stop count, direction/polarity hypothesis, terminal
   compatibility, geometry evidence state, coverage warnings. No model
   qscore or rank before save.

## Blind controls

All first-pass fields hidden until the blind save; model diagnostics hidden
until an explicit post-save reveal that is logged; reveals never modify the
saved blind judgment. Tables render as Markdown (a known native-library
fault on this workstation makes dataframe widgets unsafe).

## Task pages

Grain-first form, then: scheduled-trip second pass, multi-trip segmentation,
boundary-spanning dual-role, fragment role and stitching, source/time
quality, and no-match activity forms, per the frozen schema. The null
scheduled option is always visible; a match is never forced when one
candidate remains; timezone labels are derived from timestamps, never from
reviewer text; boundary splits outside the displayed chunk trigger an
explicit warning.
"""


def alias_for_index(index: int) -> str:
    """Deterministic candidate alias: A..Z, AA..AZ, BA.. after neutral sort."""

    if index < 0:
        raise ValueError(f"alias index must be non-negative: {index}")
    letters = ""
    remaining = index
    while True:
        letters = chr(ord("A") + remaining % 26) + letters
        remaining = remaining // 26 - 1
        if remaining < 0:
            return letters
