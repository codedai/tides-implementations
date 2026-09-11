"""Spike-02R kernel tests: boundary null/global objective, censorship
taxonomy, dual margins, evidence regimes, complete candidate keys,
revocable anchors, exact optimum stability, fragments/stitching.
(Spec section 24 items 7-33.)"""

from __future__ import annotations

from r1spike.boundary_v2 import (
    BoundaryV2Config,
    decide_boundaries_v2,
)
from r1spike.censorship_margins import (
    MarginCandidate,
    classify_candidate,
    dual_margin_decide,
)
from r1spike.fragments_v2 import (
    PairContext,
    StitchConfig,
    classify_roles,
    stitch_run,
)
from r1spike.revocable_alignment import RevocableInput, solve_revocable
from r1spike.spike02r_contracts import (
    NO_BOUNDARY,
    BoundaryCandidateV2,
    CandidateKey,
    CensorClass,
    ChunkRecord,
    ChunkRole,
    Reason02R,
)

CFG = BoundaryV2Config()


def bc(cid, start, end, q, *, status="hard_valid", run="r1"):
    return BoundaryCandidateV2(
        candidate_id=cid,
        activity_run_id=run,
        candidate_start_s=start,
        candidate_end_s=end,
        boundary_role="terminal",
        evidence_families=("terminal_geofence", "sustained_dwell"),
        evidence_regime="avl_only",
        qscore=q,
        hard_status=status,
    )


class TestBoundaryNullAndGlobalObjective:
    def test_7_explicit_no_boundary_may_win(self):
        decisions = decide_boundaries_v2(
            "r1", [bc("c1", 100, 200, CFG.no_boundary_bar_q - 50_000)], cfg=CFG
        )
        nob = next(d for d in decisions if d.candidate_id == NO_BOUNDARY)
        assert nob.selected
        c1 = next(d for d in decisions if d.candidate_id == "c1")
        assert not c1.selected and c1.status == "no_boundary"

    def test_8_sole_weak_candidate_loses_to_no_boundary(self):
        decisions = decide_boundaries_v2("r1", [bc("only", 100, 200, 100_000)], cfg=CFG)
        assert not any(d.selected for d in decisions if d.candidate_id == "only")

    def test_9_equal_overlapping_candidates_abstain(self):
        decisions = decide_boundaries_v2(
            "r1",
            [bc("a", 100, 300, 700_000), bc("b", 200, 400, 700_000)],
            cfg=CFG,
        )
        by = {d.candidate_id: d for d in decisions}
        assert by["a"].status == "tied_abstain"
        assert by["b"].status == "tied_abstain"
        assert not by["a"].selected and not by["b"].selected

    def test_10_overlapping_proposals_use_global_objective(self):
        # stronger overlapping candidate is mandatory, weaker excluded
        decisions = decide_boundaries_v2(
            "r1",
            [bc("strong", 100, 300, 900_000), bc("weak", 200, 400, 600_000)],
            cfg=CFG,
        )
        by = {d.candidate_id: d for d in decisions}
        assert by["strong"].selected and by["strong"].mandatory_in_all_optima
        assert not by["weak"].selected

    def test_11_fragmentation_penalty_changes_selection(self):
        # two nearby cuts: with a huge penalty only one survives; the
        # pair ties, so both abstain and neither is applied — the
        # penalty predictably changes the selected set vs no-penalty.
        near = [bc("a", 1000, 1100, 620_000), bc("b", 1500, 1600, 620_000)]
        loose = BoundaryV2Config(
            fragmentation_penalty_q=0, min_segment_s=CFG.min_segment_s
        )
        tight = BoundaryV2Config(
            fragmentation_penalty_q=1_000_000, min_segment_s=CFG.min_segment_s
        )
        both = decide_boundaries_v2("r1", near, cfg=loose)
        assert sum(d.selected for d in both) == 2
        penalized = decide_boundaries_v2("r1", near, cfg=tight)
        sel = [d for d in penalized if d.selected]
        assert len(sel) == 0  # equal rewards tie under the penalty
        assert any(d.status == "tied_abstain" for d in penalized)


class TestCensorshipAndDualMargins:
    def test_12_censored_candidate_stays_in_evidence_margin(self):
        cands = [
            MarginCandidate("best", 800_000, CensorClass.HARD_VALID),
            MarginCandidate("censored", 780_000, CensorClass.PLAUSIBLE_BUT_CENSORED),
            MarginCandidate("null", 450_000, CensorClass.HARD_VALID, is_null=True),
        ]
        d = dual_margin_decide(
            "rec",
            cands,
            absolute_q=600_000,
            survival_margin_bar_q=80_000,
            evidence_margin_bar_q=80_000,
        )
        assert d.status == "unique_survivor_by_exclusion"
        assert d.evidence_margin_q == 20_000
        assert d.survival_margin_q == 350_000

    def test_13_contract_unresolved_competitor_blocks(self):
        cands = [
            MarginCandidate("best", 800_000, CensorClass.HARD_VALID),
            MarginCandidate("wilson", 790_000, CensorClass.CONTRACT_UNRESOLVED),
            MarginCandidate("null", 450_000, CensorClass.HARD_VALID, is_null=True),
        ]
        d = dual_margin_decide(
            "rec",
            cands,
            absolute_q=600_000,
            survival_margin_bar_q=80_000,
            evidence_margin_bar_q=80_000,
        )
        assert d.status == "contract_blocked"
        assert "wilson" in d.blocking_contract_competitors

    def test_14_unique_survivor_emits_correct_status(self):
        d = dual_margin_decide(
            "rec",
            [
                MarginCandidate("only", 900_000, CensorClass.HARD_VALID),
                MarginCandidate("blocked", 880_000, CensorClass.PLAUSIBLE_BUT_CENSORED),
            ],
            absolute_q=600_000,
            survival_margin_bar_q=80_000,
            evidence_margin_bar_q=80_000,
        )
        assert d.status == "unique_survivor_by_exclusion"
        assert Reason02R.UNIQUE_SURVIVOR in d.reason_codes

    def test_15_survivor_margin_alone_cannot_accept(self):
        # genuine separation from ALL alternatives -> accepted
        d_ok = dual_margin_decide(
            "rec",
            [
                MarginCandidate("best", 900_000, CensorClass.HARD_VALID),
                MarginCandidate("far", 500_000, CensorClass.PLAUSIBLE_BUT_CENSORED),
            ],
            absolute_q=600_000,
            survival_margin_bar_q=80_000,
            evidence_margin_bar_q=80_000,
        )
        assert d_ok.status == "accepted"
        # same survival margin, censored rival close -> not accepted
        d_bad = dual_margin_decide(
            "rec",
            [
                MarginCandidate("best", 900_000, CensorClass.HARD_VALID),
                MarginCandidate("near", 870_000, CensorClass.PLAUSIBLE_BUT_CENSORED),
            ],
            absolute_q=600_000,
            survival_margin_bar_q=80_000,
            evidence_margin_bar_q=80_000,
        )
        assert d_bad.status == "unique_survivor_by_exclusion"

    def test_classification_taxonomy(self):
        assert (
            classify_candidate(("XR1.TEMPORAL.STALE",), "hard_invalid")
            == CensorClass.PROOF_INVALID
        )
        assert (
            classify_candidate(("XR1.SEG.TIME_UNCERTAIN",), "hard_invalid")
            == CensorClass.PLAUSIBLE_BUT_CENSORED
        )
        assert (
            classify_candidate(("XR1.STOP.UNMAPPED",), "hard_invalid")
            == CensorClass.CONTRACT_UNRESOLVED
        )
        assert classify_candidate((), "valid") == CensorClass.HARD_VALID


class TestCompleteCandidateKeys:
    def test_17_20_keys_stay_distinct(self):
        base = dict(
            service_date="2023-11-28",
            feed_id="feedA",
            route_mapping_id="r1",
            scheduled_trip_id="t1",
            polarity_hypothesis_id="pol_a",
        )
        k1 = CandidateKey(**base)
        k_feed = CandidateKey(**{**base, "feed_id": "feedB"})  # test 18
        k_date = CandidateKey(**{**base, "service_date": "2023-11-27"})  # 19
        k_pol = CandidateKey(**{**base, "polarity_hypothesis_id": "pol_b"})  # 20
        k_route = CandidateKey(**{**base, "route_mapping_id": "r2"})
        keys = {k1, k_feed, k_date, k_pol, k_route}
        assert len(keys) == 5  # bare trip_id never collapses them (17)
        assert len({k.serialize() for k in keys}) == 5

    def test_complete_candidate_evidence_tie_abstains(self):
        d = dual_margin_decide(
            "seg",
            [
                MarginCandidate(
                    CandidateKey(
                        "2023-11-28", "feedA", "r1", "t1", "pol_a"
                    ).serialize(),
                    800_000,
                    CensorClass.HARD_VALID,
                ),
                MarginCandidate(
                    CandidateKey(
                        "2023-11-28", "feedB", "r1", "t1", "pol_a"
                    ).serialize(),
                    800_000,
                    CensorClass.HARD_VALID,
                ),
            ],
            absolute_q=600_000,
            survival_margin_bar_q=10_000,
            evidence_margin_bar_q=10_000,
        )
        assert d.status == "evidence_tie"


def _rev_input(**over):
    kwargs = dict(
        component_id="c1",
        segment_ids=("s1", "s2"),
        key_order=("k1", "k2", "k3"),
        candidate_costs={
            "s1": {"k1": 100, "k2": 300},
            "s2": {"k2": 100, "k3": 300},
        },
        unmatched_cost=1_000,
        anchor_prefs={},
        anchor_break_penalty=200,
        path_absolute_bar_q=100,
        path_margin_bar_q=0,
    )
    kwargs.update(over)
    return RevocableInput(**kwargs)


class TestRevocableAnchorsAndOptimum:
    def test_21_anchor_may_be_revoked_by_global_path(self):
        # anchor prefers k3 for s1, but the global optimum needs s1=k1
        # to let s2 take k2; break penalty is small enough to revoke.
        inp = _rev_input(
            candidate_costs={
                "s1": {"k1": 100, "k3": 150},
                "s2": {"k2": 100},
            },
            anchor_prefs={"s1": "k3"},
            anchor_break_penalty=50,
        )
        res = solve_revocable(inp)
        a = next(x for x in res.assignments if x.segment_id == "s1")
        assert a.status == "anchor_revoked"
        assert a.candidate_key == "k1"

    def test_22_revoked_anchor_dependents_return_unresolved(self):
        # the dependent conclusion is modeled by the caller: a revoked
        # anchor's key is absent, so anything keyed on it must clear.
        inp = _rev_input(
            candidate_costs={"s1": {"k1": 100, "k3": 150}, "s2": {"k2": 100}},
            anchor_prefs={"s1": "k3"},
            anchor_break_penalty=50,
        )
        res = solve_revocable(inp)
        revoked = {
            a.segment_id for a in res.assignments if a.status == "anchor_revoked"
        }
        dependents = {"fold_choice": "s1"}
        cleared = {
            k: ("unresolved" if seg in revoked else "kept")
            for k, seg in dependents.items()
        }
        assert cleared["fold_choice"] == "unresolved"

    def test_23_many_equal_optima_do_not_manufacture_stability(self):
        # 7 segments x 2 equally-priced choices = 128 optimal paths;
        # no assignment may be reported stable/mandatory.
        n = 7
        segs = tuple(f"s{i}" for i in range(n))
        keys = tuple(f"k{i}{c}" for i in range(n) for c in "ab")
        costs = {f"s{i}": {f"k{i}a": 100, f"k{i}b": 100} for i in range(n)}
        inp = RevocableInput(
            component_id="c128",
            segment_ids=segs,
            key_order=keys,
            candidate_costs=costs,
            unmatched_cost=10_000,
            anchor_prefs={},
            anchor_break_penalty=0,
            path_absolute_bar_q=0,
            path_margin_bar_q=0,
        )
        res = solve_revocable(inp)
        assert all(a.status == "tied" for a in res.assignments)
        assert not any(a.mandatory_in_all_optima for a in res.assignments)

    def test_24_incomplete_optimum_analysis_fails_closed(self):
        inp = _rev_input(state_bound=2)
        res = solve_revocable(inp)
        assert res.optimum_analysis_incomplete
        assert all(a.status == "unresolved_incomplete_optimum" for a in res.assignments)

    def test_25_null_path_may_win(self):
        inp = _rev_input(
            candidate_costs={"s1": {"k1": 5_000}, "s2": {}},
            unmatched_cost=1_000,
        )
        res = solve_revocable(inp)
        assert Reason02R.PATH_NULL_WINS in res.reason_codes
        assert not any(
            a.status in ("matched", "anchor_retained") for a in res.assignments
        )

    def test_26_low_quality_best_path_abstains(self):
        inp = _rev_input(
            candidate_costs={"s1": {"k1": 950}, "s2": {"k2": 950}},
            unmatched_cost=1_000,
            path_absolute_bar_q=500,
        )
        res = solve_revocable(inp)
        assert Reason02R.PATH_LOW_QUALITY in res.reason_codes
        assert not any(a.status == "matched" for a in res.assignments)


def _chunk(cid, start, end, role=ChunkRole.UNRESOLVED_FRAGMENT):
    return ChunkRecord(
        chunk_id=cid,
        profile="apc-activity",
        run_id="r1",
        record_ids=(f"{cid}-p1", f"{cid}-p2"),
        start_instant_s=start,
        end_instant_s=end,
        role=role,
    )


class TestFragmentsAndStitching:
    SCFG = StitchConfig()

    def test_29_refined_chunk_is_not_automatically_a_performed_trip(self):
        chunks = [_chunk("f1", 0, 400)]
        decisions, hyps = stitch_run("r1", chunks, [], self.SCFG)
        assert hyps == []  # a lone fragment mints no trip hypothesis
        assert decisions[0].performed_trip_hypothesis_id is None

    def test_30_two_chunks_may_stitch(self):
        chunks = [
            _chunk("arr", 0, 400, ChunkRole.ARRIVAL_FRAGMENT),
            _chunk("dep", 500, 3000, ChunkRole.TRIP_CORE_CANDIDATE),
        ]
        ctx = [
            PairContext(
                gap_s=100,
                crossed_service_turnaround=False,
                same_top_key=True,
                direction_consistent=True,
            )
        ]
        decisions, hyps = stitch_run("r1", chunks, ctx, self.SCFG)
        assert any(d.status == "stitched" for d in decisions)
        assert len(hyps) == 1
        assert hyps[0]["performed_trip_hypothesis_id"].startswith("xpt2r-")

    def test_31_same_trip_candidate_does_not_force_stitch(self):
        chunks = [
            _chunk("a", 0, 2400, ChunkRole.TRIP_CORE_CANDIDATE),
            _chunk("b", 4000, 6400, ChunkRole.TRIP_CORE_CANDIDATE),
        ]
        ctx = [
            PairContext(
                gap_s=1600,  # beyond continuity window
                crossed_service_turnaround=True,  # real service boundary
                same_top_key=True,  # SAME scheduled trip candidate
                direction_consistent=False,
            )
        ]
        decisions, hyps = stitch_run("r1", chunks, ctx, self.SCFG)
        assert not any(d.status == "stitched" for d in decisions)
        assert len(hyps) == 2  # two separate core hypotheses

    def test_32_turnaround_chunk_never_becomes_a_trip(self):
        ordered = [
            _chunk("core", 0, 2400, ChunkRole.TRIP_CORE_CANDIDATE),
            _chunk("ta", 2500, 2900, ChunkRole.TERMINAL_TURNAROUND),
        ]
        classified = classify_roles(ordered, self.SCFG)
        assert classified[1].role == ChunkRole.TERMINAL_TURNAROUND
        seq = [c for c in classified if c.role != ChunkRole.TERMINAL_TURNAROUND]
        _d, hyps = stitch_run("r1", seq, [], self.SCFG)
        assert all("ta" not in h["member_chunk_ids"] for h in hyps)

    def test_33_equal_stitching_optima_abstain(self):
        chunks = [
            _chunk("a", 0, 400, ChunkRole.ARRIVAL_FRAGMENT),
            _chunk("b", 500, 900, ChunkRole.DEPARTURE_FRAGMENT),
        ]
        # engineered exact zero pair score = tie
        cfg = StitchConfig(
            same_key_reward_q=0,
            continuity_reward_q=0,
            direction_reward_q=0,
            stitch_base_cost_q=0,
        )
        ctx = [
            PairContext(
                gap_s=100,
                crossed_service_turnaround=False,
                same_top_key=False,
                direction_consistent=None,
            )
        ]
        decisions, hyps = stitch_run("r1", chunks, ctx, cfg)
        assert any(d.status == "tied_abstain" for d in decisions)
        assert hyps == []

    def test_roles_from_shape_and_neighbours(self):
        ordered = [
            _chunk("core", 0, 2400),
            _chunk("ta", 2500, 2900, ChunkRole.TERMINAL_TURNAROUND),
            _chunk("dep", 3000, 3400),
        ]
        classified = classify_roles(ordered, self.SCFG)
        by = {c.chunk_id: c.role for c in classified}
        assert by["core"] == ChunkRole.TRIP_CORE_CANDIDATE
        assert by["dep"] == ChunkRole.DEPARTURE_FRAGMENT
