"""Task-specific promotion gates, agreement, readiness, and factorial gate.

There is deliberately no global ``gold`` flag: labels promote only into
task-specific reviewed sets, and only through the mechanical gates of the
frozen promotion contract. The factorial gate is a report, never a trigger:
even a fully green report requires a separate explicit human authorization
before anything runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .human_label_parser import sha256_file
from .labeling_v2_contracts import (
    ACTIVITY_CASES,
    ALL_PILOT_CASES,
    BOUNDARY_SPANNING_CASES,
    FACTORIAL_STATUS_NO_GO,
    FRAGMENT_CASES,
    INSUFFICIENT_CONTEXT_CASES,
    MULTI_TRIP_CASES,
    PROTOCOL_VERSION,
    QUALITY_CASES,
    SCHEDULED_SECOND_PASS_CASES,
    TASK_SPECIFIC_LABEL_SETS,
    UI_VERSION,
)


def _structured_rationale_present(save: dict[str, Any]) -> bool:
    flags = save.get("evidence_flags") or []
    text = (save.get("rationale_text") or "").strip()
    return bool(flags) and len(text) >= 8


def _versions_match(save: dict[str, Any], protocol_sha256: str) -> bool:
    return (
        save.get("protocol_version") == PROTOCOL_VERSION
        and save.get("protocol_sha256") == protocol_sha256
        and save.get("ui_version") == UI_VERSION
    )


def _latest_adjudication(state) -> dict[str, Any] | None:
    return state.adjudications[-1] if state.adjudications else None


def _resolved_for_promotion(state) -> bool:
    """The blind save may promote only when nothing contests it.

    A disagreement, grain change, or unavailable comparison resolves in the
    blind save's favor only through an explicit ``second_pass_upheld``
    adjudication; ``deferred``, ``first_pass_upheld``, and ``new_judgment``
    never promote the blind save.
    """

    if "REQUIRES_ADJUDICATION" in state.states:
        return False
    contested = any(
        outcome in state.states
        for outcome in (
            "FIRST_SECOND_DISAGREE",
            "NOT_EVALUABLE_AT_CURRENT_GRAIN",
            "COMPARISON_UNAVAILABLE",
        )
    )
    if not contested:
        return True
    adjudication = _latest_adjudication(state)
    return (
        adjudication is not None
        and adjudication.get("resolution") == "second_pass_upheld"
    )


def _universe_hash_matches(store, case_id: str, save: dict[str, Any]) -> bool:
    """The save's universe hash must equal the frozen per-case hash —
    presence alone is not versioning."""

    import polars as pl

    rows = store.alias_map.filter(pl.col("case_id") == case_id.split("::")[0])
    if rows.is_empty():
        return False
    return save.get("candidate_universe_hash") == rows["candidate_universe_hash"][0]


def _notes_conflict_free(save: dict[str, Any]) -> bool:
    """Mechanical notes-label conflict check: the notes must not cite a
    pipe-serialized candidate key different from the selected one."""

    import re

    notes = f"{save.get('notes') or ''} {save.get('rationale_text') or ''}"
    cited = set(re.findall(r"\d{4}-\d{2}-\d{2}\|[^|\s]*\|[^|\s]*\|[^|\s]*\|\S*", notes))
    if not cited:
        return True
    selected = save.get("selected_candidate_key")
    return cited <= {selected}


def build_task_specific_sets(store) -> dict[str, Any]:
    states = store.all_states()
    sets: dict[str, list[str]] = {name: [] for name in TASK_SPECIFIC_LABEL_SETS}
    for case_id, state in sorted(states.items()):
        if "REQUIRES_ADJUDICATION" in state.states:
            sets["pending_adjudication"].append(case_id)
        if (
            "NOT_EVALUABLE_AT_CURRENT_GRAIN" in state.states
            and "ADJUDICATED" not in state.states
        ):
            sets["not_evaluable"].append(case_id)
        save = state.blind_save
        if save is None:
            continue
        gates_ok = (
            _structured_rationale_present(save)
            and not save.get("diagnostics_revealed_before_save")
            and save.get("first_pass_hidden", False)
            and _versions_match(save, store.protocol_sha256)
            and _universe_hash_matches(store, case_id, save)
            and _notes_conflict_free(save)
            and _resolved_for_promotion(state)
        )
        if not gates_ok:
            continue
        grain = save.get("case_grain")
        judgment = save.get("scheduled_judgment")
        if (
            grain == "single_trip_candidate"
            and save.get("reviewability") == "reviewable_for_schedule_match"
            and not state.is_child
        ):
            # child subtrip matches are segmentation evidence, never
            # whole-trip schedule gold
            if judgment == "matched" and save.get("selected_candidate_key"):
                sets["schedule_gold"].append(case_id)
            elif judgment == "no_scheduled_match":
                sets["schedule_abstention_gold"].append(case_id)
        elif grain == "multi_trip_chunk":
            child_ids = [
                row["child_case_id"]
                for row in store.children.iter_rows(named=True)
                if row["parent_case_id"] == case_id
            ]
            if child_ids and all(
                states[c].second_pass_saved and _resolved_for_promotion(states[c])
                for c in child_ids
            ):
                sets["segmentation_gold"].append(case_id)
        elif grain == "boundary_spanning":
            sets["boundary_gold"].append(case_id)
        elif grain == "local_fragment" and save.get("fragment_role"):
            sets["fragment_gold"].append(case_id)
        elif grain == "data_quality_invalid":
            sets["quality_gold"].append(case_id)
        elif grain == "turnaround_or_idle" and save.get("activity_state"):
            sets["activity_gold"].append(case_id)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "no_global_gold_field": True,
        "promotion_is_mechanical_and_task_specific": True,
        "confidence_alone_never_promotes": True,
        "development_pilot": True,
        "independent_holdout_eligible": False,
        "pilot_case_ids": list(ALL_PILOT_CASES),
        "sets": sets,
        "set_counts": {name: len(members) for name, members in sets.items()},
    }


def build_agreement_report(store) -> dict[str, Any]:
    states = store.all_states()
    compared = []
    for case_id, state in sorted(states.items()):
        if state.is_child or state.comparison is None:
            continue
        compared.append(
            {
                "case_id": case_id,
                "outcome": state.comparison.get("outcome"),
                "basis": state.comparison.get("basis"),
                "second_pass_grain": (state.blind_save or {}).get("case_grain"),
            }
        )
    outcomes = [row["outcome"] for row in compared]
    agree = outcomes.count("FIRST_SECOND_AGREE")
    disagree = outcomes.count("FIRST_SECOND_DISAGREE")
    not_evaluable = outcomes.count("NOT_EVALUABLE_AT_CURRENT_GRAIN")
    by_grain: dict[str, dict[str, int]] = {}
    for row in compared:
        grain = row["second_pass_grain"] or "unknown"
        bucket = by_grain.setdefault(
            grain, {"agree": 0, "disagree": 0, "not_evaluable": 0}
        )
        if row["outcome"] == "FIRST_SECOND_AGREE":
            bucket["agree"] += 1
        elif row["outcome"] == "FIRST_SECOND_DISAGREE":
            bucket["disagree"] += 1
        else:
            bucket["not_evaluable"] += 1
    evaluable = agree + disagree
    return {
        "protocol_version": PROTOCOL_VERSION,
        "comparison_is_intra_reviewer": True,
        "neither_pass_is_automatically_preferred": True,
        "compared_case_count": len(compared),
        "agree_count": agree,
        "disagree_count": disagree,
        "not_evaluable_at_current_grain_count": not_evaluable,
        "agreement_rate_over_evaluable": (
            round(agree / evaluable, 4) if evaluable else None
        ),
        "by_second_pass_grain": by_grain,
        "cases": compared,
    }


def build_readiness_report(store, task_sets: dict[str, Any]) -> dict[str, Any]:
    states = store.all_states()
    scheduled_completed = [
        case_id
        for case_id in SCHEDULED_SECOND_PASS_CASES
        if states[case_id].second_pass_saved
    ]
    child_states = [states[c] for c in store.children["child_case_id"].to_list()]
    children_completed = [s.case_id for s in child_states if s.second_pass_saved]
    pending_adjudication = task_sets["sets"]["pending_adjudication"]
    return {
        "protocol_version": PROTOCOL_VERSION,
        "denominator": len(ALL_PILOT_CASES),
        "routing_baseline": {
            "scheduled_blind_second_pass": list(SCHEDULED_SECOND_PASS_CASES),
            "segmentation_multi_trip": list(MULTI_TRIP_CASES),
            "boundary_spanning_dual_role": list(BOUNDARY_SPANNING_CASES),
            "fragment_role_and_stitching": list(FRAGMENT_CASES),
            "insufficient_context": list(INSUFFICIENT_CONTEXT_CASES),
            "source_time_quality": list(QUALITY_CASES),
            "no_match_activity": list(ACTIVITY_CASES),
            "baseline_is_editable_by_the_human": True,
        },
        "blind_second_pass": {
            "required_count": len(SCHEDULED_SECOND_PASS_CASES),
            "completed_count": len(scheduled_completed),
            "completed_case_ids": scheduled_completed,
            "first_pass_labels_must_remain_hidden": True,
        },
        "all_second_pass": {
            "parents_saved": sum(
                1 for s in states.values() if not s.is_child and s.second_pass_saved
            ),
            "children_total": len(child_states),
            "children_saved": len(children_completed),
        },
        "pending_adjudication_count": len(pending_adjudication),
        "task_specific_set_counts": task_sets["set_counts"],
        "pilot_cases_are_holdout_ineligible": True,
        "architecture_verdict": "INCONCLUSIVE — NO REVIEWED HOLDOUT LABELS",
    }


def _find_ttc_root(start: Path) -> Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / "pyproject.toml").exists() and (
            candidate / "experiments"
        ).is_dir():
            return candidate
    return None


def _preregistration_unchanged(store) -> bool | None:
    frozen = json.loads(
        (store.run_dir / "input_hashes.json").read_text(encoding="utf-8")
    )
    recorded = None
    for entry in frozen.get("files", []):
        if entry["path"].endswith("experiment_preregistration.json"):
            recorded = entry
            break
    if recorded is None:
        return None
    ttc_root = _find_ttc_root(store.run_dir) or _find_ttc_root(Path.cwd())
    if ttc_root is None:
        return None
    path = ttc_root / recorded["path"]
    if not path.exists():
        return False
    return sha256_file(path) == recorded["sha256"]


def build_go_no_go(store, task_sets, readiness) -> dict[str, Any]:
    states = store.all_states()
    disagreements_adjudicated = not task_sets["sets"]["pending_adjudication"]
    scheduled_done = (
        readiness["blind_second_pass"]["completed_count"]
        == readiness["blind_second_pass"]["required_count"]
    )
    children_done = (
        readiness["all_second_pass"]["children_total"] > 0
        and readiness["all_second_pass"]["children_saved"]
        == readiness["all_second_pass"]["children_total"]
    )
    matched_saves = [
        (state.case_id, state.blind_save)
        for state in states.values()
        if state.blind_save and state.blind_save.get("scheduled_judgment") == "matched"
    ]

    def _key_in_universe(case_id: str, key: str | None) -> bool:
        import polars as pl

        if not key:
            return False
        rows = store.alias_map.filter(
            (pl.col("case_id") == case_id.split("::")[0])
            & (pl.col("candidate_key") == key)
        )
        return not rows.is_empty()

    keys_verified = bool(matched_saves) and all(
        _key_in_universe(case_id, save.get("selected_candidate_key"))
        for case_id, save in matched_saves
    )
    prereg_unchanged = _preregistration_unchanged(store)
    conditions = {
        "v2_schema_frozen": True,
        "ui_contract_frozen": True,
        "candidate_presentation_contract_frozen": True,
        "blind_protocol_frozen": True,
        "child_case_rules_frozen": True,
        "all_seven_scheduled_second_passes_completed": scheduled_done,
        "required_child_reviews_completed": children_done,
        "disagreements_adjudicated": disagreements_adjudicated,
        "task_specific_label_sets_materialized": True,
        "full_candidate_keys_verified": keys_verified,
        "phase_b_cases_labeled_under_frozen_protocol": False,
        "calibration_holdout_membership_validated": False,
        "holdout_unopened": True,
        "preregistration_unchanged": bool(prereg_unchanged),
    }
    all_pass = all(conditions.values())
    return {
        "protocol_version": PROTOCOL_VERSION,
        "factorial_status": FACTORIAL_STATUS_NO_GO,
        "conditions": conditions,
        "all_conditions_pass": all_pass,
        "status_rule": (
            "FACTORIAL_STATUS stays NO_GO until every condition passes, and "
            "a green report never runs anything: a separate explicit human "
            "authorization is required before the factorial may execute"
        ),
        "automatic_execution_forbidden": True,
        "thresholds_untouched": True,
        "holdout_untouched": True,
        "preregistration_untouched": bool(prereg_unchanged),
    }


def build_live_reports(store) -> dict[str, dict[str, Any]]:
    task_sets = build_task_specific_sets(store)
    agreement = build_agreement_report(store)
    readiness = build_readiness_report(store, task_sets)
    go_no_go = build_go_no_go(store, task_sets, readiness)
    return {
        "task_specific_label_sets.json": task_sets,
        "intra_reviewer_agreement.json": agreement,
        "label_readiness_report_v2.json": readiness,
        "factorial_go_no_go.json": go_no_go,
    }


def post_review_analysis(store) -> dict[str, Any]:
    """Descriptive-only post-review analysis (no accuracy, no architecture)."""

    states = store.all_states()
    events = store.events()
    reveal_events = [e for e in events if e.get("event") == "diagnostics_reveal"]
    filter_events = [e for e in events if e.get("event") == "candidate_filter_applied"]
    routing_changes = [e for e in events if e.get("event") == "routing_change"]
    saves = [s.blind_save for s in states.values() if s.blind_save]
    task_sets = build_task_specific_sets(store)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "descriptive_only": True,
        "no_model_precision_or_architecture_claim": True,
        "agreement": build_agreement_report(store),
        "rationale_completeness": {
            "saves": len(saves),
            "with_structured_rationale": sum(
                1 for s in saves if _structured_rationale_present(s)
            ),
        },
        "diagnostics_reveal_usage": {
            "reveal_events": len(reveal_events),
            "revealed_before_save": sum(
                1 for s in saves if s.get("diagnostics_revealed_before_save")
            ),
        },
        "candidate_filter_usage": len(filter_events),
        "adjacent_context_usage": sum(
            1 for s in saves if s.get("adjacent_context_used")
        ),
        "routing_changes": len(routing_changes),
        "task_specific_label_counts": task_sets["set_counts"],
        "remaining_adjudication": task_sets["sets"]["pending_adjudication"],
        "readiness_funnel": build_readiness_report(store, task_sets),
        "factorial_go_no_go": build_go_no_go(
            store, task_sets, build_readiness_report(store, task_sets)
        ),
    }


def empty_live_report_payloads(run_dir: Path | None = None) -> dict[str, dict]:
    """Honest empty-state payloads written at setup, before any human review."""

    empty_sets = {name: [] for name in TASK_SPECIFIC_LABEL_SETS}
    return {
        "task_specific_label_sets.json": {
            "protocol_version": PROTOCOL_VERSION,
            "no_global_gold_field": True,
            "promotion_is_mechanical_and_task_specific": True,
            "confidence_alone_never_promotes": True,
            "development_pilot": True,
            "independent_holdout_eligible": False,
            "pilot_case_ids": list(ALL_PILOT_CASES),
            "sets": empty_sets,
            "set_counts": {name: 0 for name in TASK_SPECIFIC_LABEL_SETS},
            "status": "no_second_pass_labels_exist_yet",
        },
        "intra_reviewer_agreement.json": {
            "protocol_version": PROTOCOL_VERSION,
            "comparison_is_intra_reviewer": True,
            "neither_pass_is_automatically_preferred": True,
            "compared_case_count": 0,
            "agree_count": 0,
            "disagree_count": 0,
            "not_evaluable_at_current_grain_count": 0,
            "agreement_rate_over_evaluable": None,
            "by_second_pass_grain": {},
            "cases": [],
            "status": "no_second_pass_labels_exist_yet",
        },
        "label_readiness_report_v2.json": {
            "protocol_version": PROTOCOL_VERSION,
            "denominator": len(ALL_PILOT_CASES),
            "blind_second_pass": {
                "required_count": len(SCHEDULED_SECOND_PASS_CASES),
                "completed_count": 0,
                "completed_case_ids": [],
                "first_pass_labels_must_remain_hidden": True,
            },
            "status": "protocol_frozen_human_review_not_started",
            "pilot_cases_are_holdout_ineligible": True,
            "architecture_verdict": "INCONCLUSIVE — NO REVIEWED HOLDOUT LABELS",
        },
        "factorial_go_no_go.json": {
            "protocol_version": PROTOCOL_VERSION,
            "factorial_status": FACTORIAL_STATUS_NO_GO,
            "conditions": {
                "v2_schema_frozen": True,
                "ui_contract_frozen": True,
                "candidate_presentation_contract_frozen": True,
                "blind_protocol_frozen": True,
                "child_case_rules_frozen": True,
                "all_seven_scheduled_second_passes_completed": False,
                "required_child_reviews_completed": False,
                "disagreements_adjudicated": False,
                "task_specific_label_sets_materialized": False,
                "full_candidate_keys_verified": False,
                "phase_b_cases_labeled_under_frozen_protocol": False,
                "calibration_holdout_membership_validated": False,
                "holdout_unopened": True,
                "preregistration_unchanged": True,
            },
            "all_conditions_pass": False,
            "automatic_execution_forbidden": True,
            "status_rule": (
                "FACTORIAL_STATUS stays NO_GO until every condition passes, "
                "and a green report never runs anything: a separate explicit "
                "human authorization is required before the factorial may "
                "execute"
            ),
        },
    }
