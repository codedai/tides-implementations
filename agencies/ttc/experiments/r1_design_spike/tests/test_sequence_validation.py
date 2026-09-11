"""Tests for the experimental Stage-3 sequence validation kernel.

Each test exercises one design question the spike must expose: multiple
optima, equal-cost demotion sets, unbreakable ties, fold rollback of
demoted attachments, and how the conservative stable-set rule degrades
under enumeration truncation.
"""

from __future__ import annotations

import pytest
from r1spike.contracts import Reason
from r1spike.sequence_validation import (
    OPTIMA_TRUNCATED_REASON,
    Alignment,
    ObservedVisit,
    ScheduledOccurrence,
    validate_sequence,
)


def occ(i: int, stop_id: str, t: int) -> ScheduledOccurrence:
    return ScheduledOccurrence(occ_index=i, stop_id=stop_id, scheduled_time_s=t)


def visit(
    vid: str,
    stop_id: str,
    t: int,
    along: float | None = None,
    fold: bool = False,
) -> ObservedVisit:
    return ObservedVisit(
        visit_id=vid,
        stop_id=stop_id,
        observed_time_s=t,
        along_m=along,
        fold_selected_by_attachment=fold,
    )


def five_stop_schedule() -> list[ScheduledOccurrence]:
    return [
        occ(0, "A", 100),
        occ(1, "B", 200),
        occ(2, "C", 300),
        occ(3, "D", 400),
        occ(4, "E", 500),
    ]


def tied_out_of_order_inputs(
    *, c_fold: bool = False, swap_labels: bool = False
) -> tuple[list[ScheduledOccurrence], list[ObservedVisit]]:
    """Scheduled A,B,C at 100,200,300; observed A(95), C(240), B(260).

    Matching all three violates monotone occurrence order. Demoting C
    (keep A res 5, B res 60) and demoting B (keep A res 5, C res 60)
    cost exactly the same: two optima by construction.
    """
    scheduled = [occ(0, "A", 100), occ(1, "B", 200), occ(2, "C", 300)]
    c_id, b_id = ("vB", "vC") if swap_labels else ("vC", "vB")
    observed = [
        visit("vA", "A", 95),
        visit(c_id, "C", 240, fold=c_fold),
        visit(b_id, "B", 260),
    ]
    return scheduled, observed


def test_clean_monotone_case_matches_everything_with_single_optimum() -> None:
    observed = [visit("vA", "A", 101), visit("vC", "C", 299), visit("vE", "E", 502)]
    result = validate_sequence(five_stop_schedule(), observed)
    assert result.optimal_cost == 1 + 1 + 2  # skipped B and D cost nothing
    assert result.n_optimal == 1
    assert not result.truncated
    assert result.stable_matches == (("vA", 0), ("vC", 2), ("vE", 4))
    assert result.stable_kept == frozenset({"vA", "vC", "vE"})
    assert result.demoted == frozenset()
    assert result.ambiguous_demotions == frozenset()
    assert result.certain_demotions == frozenset()
    assert result.reason_codes == ()


def test_repeated_stop_time_residual_selects_second_occurrence() -> None:
    scheduled = [occ(0, "A", 100), occ(1, "B", 200), occ(2, "A", 300), occ(3, "C", 400)]
    observed = [visit("vA", "A", 295), visit("vC", "C", 405)]
    result = validate_sequence(scheduled, observed)
    assert result.n_optimal == 1
    assert result.optimal_cost == 5 + 5
    assert result.stable_matches == (("vA", 2), ("vC", 3))  # occ 2, not occ 0
    assert result.demoted == frozenset()


def test_extra_observed_stop_is_certain_demotion_in_every_optimum() -> None:
    scheduled = [occ(0, "A", 100), occ(1, "B", 200), occ(2, "C", 300)]
    observed = [visit("vA", "A", 100), visit("vX", "X", 200), visit("vC", "C", 300)]
    result = validate_sequence(scheduled, observed)
    assert result.n_optimal == 1
    assert result.stable_kept == frozenset({"vA", "vC"})
    assert result.certain_demotions == frozenset({"vX"})
    assert result.demoted == frozenset({"vX"})
    assert all("vX" in a.demoted for a in result.alignments)
    assert Reason.SEQ_INCONSISTENT in result.reason_codes
    assert Reason.SEQ_MULTIPLE_OPTIMA not in result.reason_codes


def test_out_of_order_tie_yields_two_optima_and_ambiguous_demotions() -> None:
    scheduled, observed = tied_out_of_order_inputs()
    result = validate_sequence(scheduled, observed)
    assert result.optimal_cost == 5 + 60 + 1_000  # kept residuals + one demotion
    assert result.n_optimal == 2
    assert not result.truncated
    assert result.stable_kept == frozenset({"vA"})
    assert result.demoted == frozenset({"vB", "vC"})  # conservative rule
    assert result.ambiguous_demotions == frozenset({"vB", "vC"})
    assert result.certain_demotions == frozenset()
    assert Reason.SEQ_MULTIPLE_OPTIMA in result.reason_codes
    assert Reason.SEQ_AMBIGUOUS_DEMOTION in result.reason_codes
    assert Reason.SEQ_INCONSISTENT not in result.reason_codes


def test_ambiguous_demotion_rolls_back_attachment_selected_fold() -> None:
    scheduled, observed = tied_out_of_order_inputs(c_fold=True)
    result = validate_sequence(scheduled, observed)
    assert result.ambiguous_demotions == frozenset({"vB", "vC"})
    # C's fold was selected solely by the attachment; its demotion is
    # only ambiguous, but rollback is conservative and includes it.
    assert result.fold_rollback_visit_ids == frozenset({"vC"})
    assert "vB" not in result.fold_rollback_visit_ids


def test_progress_violation_reported_for_decreasing_along_m() -> None:
    scheduled = [occ(0, "A", 100), occ(1, "B", 200), occ(2, "C", 300)]
    observed = [
        visit("vA", "A", 100, along=100.0),
        visit("vB", "B", 200, along=200.0),
        visit("vC", "C", 300, along=150.0),
    ]
    result = validate_sequence(scheduled, observed)
    assert result.stable_kept == frozenset({"vA", "vB", "vC"})
    assert result.progress_violations == (("vB", "vC"),)
    # Diagnostic only: the violation demotes nothing in this spike.
    assert result.demoted == frozenset()


def test_unknown_stop_is_never_resurrected_into_a_match() -> None:
    scheduled = [occ(0, "A", 100), occ(1, "B", 200)]
    observed = [visit("vA", "A", 100), visit("vZ", "Z", 150)]
    result = validate_sequence(scheduled, observed)
    assert "vZ" in result.demoted
    assert "vZ" in result.certain_demotions
    for alignment in result.alignments:
        assert all(vid != "vZ" for vid, _ in alignment.matches)


def test_empty_schedule_demotes_every_observed_visit() -> None:
    observed = [visit("v1", "A", 100), visit("v2", "B", 200)]
    result = validate_sequence([], observed)
    assert result.optimal_cost == 2 * 1_000
    assert result.n_optimal == 1  # demoting everything IS the single optimum
    assert result.demoted == frozenset({"v1", "v2"})
    assert result.certain_demotions == frozenset({"v1", "v2"})
    assert result.ambiguous_demotions == frozenset()
    assert Reason.SEQ_INCONSISTENT in result.reason_codes
    assert Reason.SEQ_MULTIPLE_OPTIMA not in result.reason_codes


def test_empty_observed_yields_trivial_clean_result() -> None:
    result = validate_sequence(five_stop_schedule(), [])
    assert result.optimal_cost == 0
    assert result.n_optimal == 1
    assert not result.truncated
    assert result.alignments == (Alignment(matches=(), demoted=frozenset(), cost=0),)
    assert result.stable_matches == ()
    assert result.stable_kept == frozenset()
    assert result.demoted == frozenset()
    assert result.reason_codes == ()
    assert result.progress_violations == ()


def test_identical_inputs_produce_identical_results() -> None:
    scheduled, observed = tied_out_of_order_inputs()
    first = validate_sequence(scheduled, observed)
    second = validate_sequence(scheduled, observed)
    assert first == second


def test_decision_structure_is_symmetric_under_label_swap() -> None:
    # Swap the visit_id labels of the two tied visits: the decision
    # structure must not depend on which label sorts first.
    scheduled, observed = tied_out_of_order_inputs(swap_labels=True)
    result = validate_sequence(scheduled, observed)
    assert result.n_optimal == 2
    assert result.optimal_cost == 5 + 60 + 1_000
    assert result.stable_kept == frozenset({"vA"})
    assert result.ambiguous_demotions == frozenset({"vB", "vC"})
    assert result.certain_demotions == frozenset()


def test_truncated_enumeration_caps_optima_and_flags_reason() -> None:
    # 7 identical-stop visits vs 1 occurrence, equal residuals: 7 optima,
    # each keeping a different visit. Cap at 4.
    scheduled = [occ(0, "A", 1_000)]
    observed = [visit(f"v{i}", "A", 900) for i in range(7)]
    result = validate_sequence(scheduled, observed, max_optima=4)
    assert result.optimal_cost == 100 + 6 * 1_000
    assert result.truncated
    assert result.n_optimal == 4  # lower bound on the true count (7)
    assert OPTIMA_TRUNCATED_REASON in result.reason_codes
    assert Reason.SEQ_MULTIPLE_OPTIMA in result.reason_codes
    # Stable sets are computed over the enumerated 4 optima ONLY.
    assert result.stable_kept == frozenset()
    assert result.demoted == frozenset(f"v{i}" for i in range(7))
    # Caveat under test: the deterministic enumeration order sees the
    # optima keeping v0..v3, so v4..v6 are misclassified as CERTAIN
    # demotions even though each is kept in a non-enumerated optimum.
    assert result.ambiguous_demotions == frozenset({"v0", "v1", "v2", "v3"})
    assert result.certain_demotions == frozenset({"v4", "v5", "v6"})


def test_visit_kept_everywhere_but_at_different_occurrences() -> None:
    # Exposed design question: kept in every optimum, but occ_index
    # differs, so it is in stable_kept yet absent from stable_matches
    # (scheduled-time attribution is undefined downstream).
    scheduled = [occ(0, "A", 100), occ(1, "A", 300)]
    observed = [visit("vA", "A", 200)]
    result = validate_sequence(scheduled, observed)
    assert result.n_optimal == 2
    assert result.stable_kept == frozenset({"vA"})
    assert result.stable_matches == ()
    assert result.demoted == frozenset()
    assert result.reason_codes == (Reason.SEQ_MULTIPLE_OPTIMA,)


def test_duplicate_visit_ids_are_rejected() -> None:
    observed = [visit("dup", "A", 100), visit("dup", "B", 200)]
    with pytest.raises(ValueError, match="unique"):
        validate_sequence(five_stop_schedule(), observed)
