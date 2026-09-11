"""Decision-semantics tests for the experimental score contract.

Covers required core tests 1-6: null alternatives can win, sole weak
candidates abstain, exact ties abstain, and candidate identity is never
evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from r1spike.contracts import (
    NO_SCHEDULED_TRIP,
    EvidenceRegime,
    HardValidity,
    ScoredCandidate,
)
from r1spike.scoring import Thresholds, decide, load_contract

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "experimental_score_contract.yaml"
)

STATUS_MAP = {
    "accepted": "accepted",
    "null_winner": "null_winner",
    "low_score": "low_score",
    "low_margin": "low_margin",
    "evidence_tie": "evidence_tie",
    "no_candidate": "no_candidate",
    "all_hard_invalid": "no_candidate",
}


@pytest.fixture(scope="module")
def contract():
    return load_contract(CONTRACT_PATH, "conservative")


def scored(key, q, *, is_null=False, hard=False, reasons=()):
    return ScoredCandidate(
        key=key,
        qscore=q,
        is_null=is_null,
        hard_validity=(HardValidity.HARD_INVALID if hard else HardValidity.VALID),
        regime=EvidenceRegime.AVL_ONLY,
        family_qscores={},
        reason_codes=tuple(reasons),
    )


def seg_features(
    *,
    start_res=100,
    end_res=100,
    overlap=950,
    ratio=950,
    mapping=1000,
    date=1000,
):
    return {
        "seg_sched_start_residual_s": start_res,
        "seg_sched_end_residual_s": end_res,
        "seg_interval_overlap_millis": overlap,
        "seg_duration_ratio_millis": ratio,
        "seg_route_mapping_confidence_millis": mapping,
        "seg_date_key_agreement_millis": date,
    }


def no_trip_features(*, mapping=1000, date=1000):
    return {
        "seg_route_mapping_confidence_millis": mapping,
        "seg_date_key_agreement_millis": date,
    }


class TestNullAlternativesCanWin:
    def test_no_scheduled_trip_tuple_beats_an_implausible_trip(self, contract):
        """Required test 1: explicit NO_SCHEDULED_TRIP may win."""
        bad_trip_q, _, _, invalid = contract.score_candidate(
            "segment_selection",
            seg_features(start_res=3500, end_res=3500, overlap=100, ratio=200),
            EvidenceRegime.AVL_ONLY,
        )
        assert not invalid
        no_trip_q, _, _, _ = contract.score_candidate(
            "segment_selection",
            no_trip_features(),
            EvidenceRegime.AVL_ONLY,
        )
        assert no_trip_q > bad_trip_q
        null_q = contract.null_scores["segment_selection"][
            "XNULL:UNRESOLVED_SERVICE_DATE"
        ]
        decision = decide(
            "seg1",
            [
                scored(("seg1", "d", "f", "r", "trip1"), bad_trip_q),
                scored(("seg1", "d", "f", "r", NO_SCHEDULED_TRIP), no_trip_q),
                scored(("seg1", "null"), null_q, is_null=True),
            ],
            contract.thresholds["segment_selection"],
            status_map=STATUS_MAP,
        )
        assert decision.status == "accepted"
        assert decision.selected_key[-1] == NO_SCHEDULED_TRIP

    def test_no_attachment_wins_over_weak_non_null_candidates(self, contract):
        """Required test 2: NO_ATTACHMENT beats weak real candidates."""
        null_q = contract.null_scores["apc_attachment"]["XNULL:NO_ATTACHMENT"]
        decision = decide(
            "rec1",
            [
                scored(("rec1", 0, "d", "f", "r", "s", "segA"), null_q - 50_000),
                scored(("rec1", 0, "d", "f", "r", "s", "segB"), null_q - 80_000),
                scored(("rec1", "null"), null_q, is_null=True),
            ],
            contract.thresholds["apc_attachment"],
            status_map=STATUS_MAP,
        )
        assert decision.status == "null_winner"
        assert decision.selected_key is None


class TestAbstention:
    def test_sole_candidate_below_absolute_threshold_abstains(self, contract):
        """Required test 3: one low-scoring candidate must abstain."""
        thresholds = contract.thresholds["apc_attachment"]
        null_q = contract.null_scores["apc_attachment"]["XNULL:NO_ATTACHMENT"]
        weak_q = thresholds.absolute_q - 1
        assert weak_q > null_q  # beats the null but not the bar
        decision = decide(
            "rec1",
            [
                scored(("rec1", "only"), weak_q),
                scored(("rec1", "null"), null_q, is_null=True),
            ],
            thresholds,
            status_map=STATUS_MAP,
        )
        assert decision.status == "low_score"
        assert decision.selected_key is None

    def test_sole_candidate_margin_uses_null_as_runner_up(self, contract):
        thresholds = Thresholds(absolute_q=500_000, margin_q=100_000)
        decision = decide(
            "rec1",
            [
                scored(("rec1", "only"), 520_000),
                scored(("rec1", "null"), 450_000, is_null=True),
            ],
            thresholds,
            status_map=STATUS_MAP,
        )
        assert decision.status == "low_margin"
        assert decision.margin_q == 70_000

    def test_exact_evidence_tie_abstains_with_zero_margin(self, contract):
        """Required test 4: an exact tie is never resolved by identity."""
        thresholds = contract.thresholds["apc_attachment"]
        q = thresholds.absolute_q + 200_000
        decision = decide(
            "rec1",
            [
                scored(("rec1", "candA"), q),
                scored(("rec1", "candB"), q),
                scored(("rec1", "null"), 450_000, is_null=True),
            ],
            thresholds,
            status_map=STATUS_MAP,
        )
        assert decision.status == "evidence_tie"
        assert decision.margin_q == 0
        assert decision.selected_key is None


class TestIdentityIsNotEvidence:
    def test_swapping_candidate_keys_does_not_change_tie_outcome(self, contract):
        """Required test 5 (tie half): key content cannot break a tie."""
        thresholds = contract.thresholds["apc_attachment"]
        q = thresholds.absolute_q + 100_000
        for keys in [("aaa", "zzz"), ("zzz", "aaa")]:
            decision = decide(
                "rec1",
                [
                    scored((keys[0],), q),
                    scored((keys[1],), q),
                    scored(("null",), 400_000, is_null=True),
                ],
                thresholds,
                status_map=STATUS_MAP,
            )
            assert decision.status == "evidence_tie"

    def test_selection_follows_evidence_when_keys_swap(self, contract):
        """Required test 5 (winner half): the winner is the score's
        owner, whichever key carries it."""
        thresholds = Thresholds(absolute_q=500_000, margin_q=50_000)
        for winner_key, loser_key in [("aaa", "zzz"), ("zzz", "aaa")]:
            decision = decide(
                "rec1",
                [
                    scored((winner_key,), 800_000),
                    scored((loser_key,), 600_000),
                    scored(("null",), 400_000, is_null=True),
                ],
                thresholds,
                status_map=STATUS_MAP,
            )
            assert decision.status == "accepted"
            assert decision.selected_key == (winner_key,)

    def test_same_segment_fold0_fold1_equal_evidence_stays_tied(self, contract):
        """Required test 6: fold hypotheses on one segment remain
        distinct candidates and tie when evidence is identical."""
        features = {
            "att_interval_gap_s": 0,
            "att_interval_overlap_millis": 1000,
            "att_nearest_ping_gap_s": 30,
            "att_route_agreement_millis": 1000,
            "att_stop_mapping_confidence_millis": 1000,
            "att_date_key_agreement_millis": 1000,
        }
        q0, _, _, _ = contract.score_candidate(
            "apc_attachment", dict(features), EvidenceRegime.APC_EVIDENCE
        )
        q1, _, _, _ = contract.score_candidate(
            "apc_attachment", dict(features), EvidenceRegime.APC_EVIDENCE
        )
        assert q0 == q1
        decision = decide(
            "rec1",
            [
                scored(("rec1", 0, "d", "f", "r", "s", "segA"), q0),
                scored(("rec1", 1, "d", "f", "r", "s", "segA"), q1),
                scored(("rec1", "null"), 450_000, is_null=True),
            ],
            contract.thresholds["apc_attachment"],
            status_map=STATUS_MAP,
        )
        assert decision.status == "evidence_tie"


class TestAllHardInvalidClassification:
    def test_incidental_annotations_cannot_outrank_the_blocking_reason(self, contract):
        """An annotation reason (stop ambiguity) that is not mapped as a
        hard_invalid:* status must not become the dominant reason and
        change the disposition of an all-hard-invalid record."""
        status_map = dict(STATUS_MAP)
        status_map["hard_invalid:XR1.TEMPORAL.INCOMPATIBLE_DATE"] = "policy_blocked"
        cands = [
            scored(
                ("rec1", "candA"),
                0,
                hard=True,
                reasons=(
                    "XR1.STOP.AMBIGUOUS",
                    "XR1.TEMPORAL.INCOMPATIBLE_DATE",
                ),
            ),
            scored(
                ("rec1", "candB"),
                0,
                hard=True,
                reasons=(
                    "XR1.STOP.AMBIGUOUS",
                    "XR1.TEMPORAL.INCOMPATIBLE_DATE",
                ),
            ),
            scored(("rec1", "null"), 450_000, is_null=True),
        ]
        decision = decide(
            "rec1",
            cands,
            contract.thresholds["apc_attachment"],
            status_map=status_map,
        )
        assert decision.status == "policy_blocked"


class TestRegimeSeparation:
    def test_missing_regime_switch_feature_raises_not_renormalizes(self, contract):
        with pytest.raises(ValueError, match="not renormalize"):
            contract.score_candidate(
                "stage1_support",
                {"seg_apc_date_support_millis": None},
                EvidenceRegime.APC_EVIDENCE,
            )

    def test_hard_bound_rejects_stale_gap_via_contract(self, contract):
        """Required test 9 (contract half): the legacy 9,083 s stale
        match is hard-rejected."""
        q, families, reasons, hard_invalid = contract.score_candidate(
            "apc_attachment",
            {
                "att_interval_gap_s": 9083,
                "att_interval_overlap_millis": 0,
                "att_nearest_ping_gap_s": 100,
                "att_route_agreement_millis": 1000,
                "att_stop_mapping_confidence_millis": 1000,
                "att_date_key_agreement_millis": 1000,
            },
            EvidenceRegime.APC_EVIDENCE,
        )
        assert hard_invalid
        assert "XR1.TEMPORAL.STALE" in reasons

    def test_geometry_hard_contradiction_rejects_good_time(self, contract):
        """Required test 7: good time but hard-invalid geometry rejects."""
        q, families, reasons, hard_invalid = contract.score_candidate(
            "apc_attachment",
            {
                "att_interval_gap_s": 0,
                "att_interval_overlap_millis": 1000,
                "att_nearest_ping_gap_s": 10,
                "att_stop_to_path_residual_dm": 1500.0,  # 1.5 km
                "att_route_agreement_millis": 1000,
                "att_stop_mapping_confidence_millis": 1000,
                "att_date_key_agreement_millis": 1000,
            },
            EvidenceRegime.APC_EVIDENCE,
        )
        assert hard_invalid
        assert "XR1.GEOM.HARD_CONTRADICTION" in reasons

    def test_good_geometry_but_stale_time_rejects(self, contract):
        """Required test 8."""
        q, families, reasons, hard_invalid = contract.score_candidate(
            "apc_attachment",
            {
                "att_interval_gap_s": 5000,
                "att_interval_overlap_millis": 0,
                "att_nearest_ping_gap_s": 5000,
                "att_stop_to_path_residual_dm": 5.0,
                "att_route_agreement_millis": 1000,
                "att_stop_mapping_confidence_millis": 1000,
                "att_date_key_agreement_millis": 1000,
            },
            EvidenceRegime.APC_EVIDENCE,
        )
        assert hard_invalid
        assert "XR1.TEMPORAL.STALE" in reasons
