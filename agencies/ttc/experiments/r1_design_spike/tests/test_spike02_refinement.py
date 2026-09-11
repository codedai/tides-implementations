"""Spike-02 one-pass refinement tests (required tests 5-8, 16-22, 39)."""

from __future__ import annotations

from r1spike.boundary_refinement import (
    RefinementConfig,
    refine_run,
    residual_boundary_diagnostic,
    spanning_roles,
)
from r1spike.spike02_contracts import (
    BoundaryStatus,
    PingState,
    Reason02,
)
from r1spike.terminal_episodes import EpisodeConfig, detect_episodes

from .spike02_helpers import (
    CLUSTERS,
    apc_record,
    make_run,
    trip_pings,
)
from .test_spike02_episodes import _sig, standard_scenario

CFG = RefinementConfig()
EP_CFG = EpisodeConfig()


def _detect(run, pings, edges, signals):
    return detect_episodes(run, pings, CLUSTERS, edges, signals, cfg=EP_CFG)


def _refine(variant, scenario_kwargs=None):
    run, pings, edges, signals = standard_scenario(**(scenario_kwargs or {}))
    episodes = _detect(run, pings, edges, signals)
    result = refine_run(run, pings, episodes, variant=variant, cfg=CFG)
    return run, pings, episodes, result


class TestApcPrecedence:
    def test_5_q3_apc_narrows_the_coarse_buffer(self):
        _, _, _, v1 = _refine("v1_avl_only")
        _, _, episodes, v2 = _refine("v2_conditional")
        ta1 = next(t for t in v1.turnarounds if t.cluster_id == "cl-north")
        ta2 = next(t for t in v2.turnarounds if t.cluster_id == "cl-north")
        # APC cut [service_end, service_start) sits inside the physical
        # buffer, so the V2 turnaround is strictly narrower.
        assert ta2.start_instant_s >= ta1.start_instant_s
        assert ta2.end_instant_s <= ta1.end_instant_s
        assert len(ta2.record_ids) < len(ta1.record_ids)
        applied = [
            d
            for d in v2.boundary_decisions
            if d.status == BoundaryStatus.APPLIED
            and Reason02.BOUNDARY_APPLIED_APC in d.reason_codes
        ]
        assert applied

    def test_6_low_quality_apc_does_not_override(self):
        # avg_sample=2 makes both APC signals Q1: no service anchors.
        _, _, _, v1 = _refine("v1_avl_only")
        _, _, _, v2 = _refine("v2_conditional", {"apc_avg_sample": 2})
        ta1 = next(t for t in v1.turnarounds if t.cluster_id == "cl-north")
        ta2 = next(t for t in v2.turnarounds if t.cluster_id == "cl-north")
        assert ta2.record_ids == ta1.record_ids  # physical buffer retained
        assert all(
            Reason02.BOUNDARY_APPLIED_APC not in d.reason_codes
            for d in v2.boundary_decisions
            if d.status == BoundaryStatus.APPLIED
        )

    def test_7_conflicting_apc_and_avl_retain_hypotheses(self):
        # Alighting evidence beyond the conflict tolerance of the
        # physical visit (but still inside the evidence window).
        scenario = {"alighting_arrival": 2200, "boarding_departure": 3140}
        run, pings, edges, signals = standard_scenario(**scenario)
        episodes = _detect(run, pings, edges, signals)
        north = next(e for e in episodes if e.cluster_id == "cl-north")
        assert Reason02.EPISODE_APC_CONFLICT in north.reason_codes
        v2 = refine_run(run, pings, episodes, variant="v2_conditional", cfg=CFG)
        assert not any(
            d.status == BoundaryStatus.APPLIED
            for d in v2.boundary_decisions
            if _episode_of(d, episodes) is north
        )
        assert v2.ambiguous_boundary_ids
        # The AVL-only variant is unaffected by the APC conflict.
        v1 = refine_run(run, pings, episodes, variant="v1_avl_only", cfg=CFG)
        assert any(
            d.status == BoundaryStatus.APPLIED
            for d in v1.boundary_decisions
            if _episode_of(d, episodes) is north
        )

    def test_8_no_apc_falls_back_to_identical_avl_evidence(self):
        run, pings, edges, signals = standard_scenario(with_apc=False)
        episodes = _detect(run, pings, edges, signals)
        v1 = refine_run(run, pings, episodes, variant="v1_avl_only", cfg=CFG)
        v2 = refine_run(run, pings, episodes, variant="v2_conditional", cfg=CFG)
        assert [s.record_ids for s in v1.refined_segments] == [
            s.record_ids for s in v2.refined_segments
        ]

    def test_39_conditional_and_unconditional_diverge_on_weak_evidence(self):
        # A geofence transit with a single weak (Q1) APC record: V2
        # refuses; VX force-splits. This is the adversarial case. The
        # outbound trip's ping at the terminal itself is dropped so the
        # visit is a bare single-ping transit (no dwell).
        pings = (
            trip_pings("in", 1000, northbound=True)
            + trip_pings("out", 2860, northbound=False)[1:]
        )
        run = make_run(pings)
        records = [apc_record("wk", 2790, 2800)]
        signals = {"wk": _sig("wk", boarding=2, avg_sample=2)}
        pings_by_id = {p.record_id: p for p in pings}
        from r1spike.provisional_alignment import align_apc_records

        edges = align_apc_records(
            records,
            [run],
            pings_by_id,
            signals,
            CLUSTERS,
            max_time_gap_s=900,
            interpolation_max_gap_s=300,
        )
        episodes = _detect(run, pings, edges, signals)
        v2 = refine_run(run, pings, episodes, variant="v2_conditional", cfg=CFG)
        vx = refine_run(run, pings, episodes, variant="vx_unconditional", cfg=CFG)
        north_eps = [e for e in episodes if e.cluster_id == "cl-north"]
        assert north_eps
        assert len(vx.applied_boundary_ids) > len(v2.applied_boundary_ids)


def _episode_of(decision, episodes):
    from r1spike.boundary_refinement import _boundary_id

    for e in episodes:
        for variant in ("v1_avl_only", "v2_conditional", "vx_unconditional"):
            if _boundary_id(e.run_id, e.episode_id + ":" + variant) == (
                decision.boundary_id
            ):
                return e
    return None


class TestSpanningRoles:
    def test_16_spanning_row_creates_both_evidence_roles(self):
        run, pings, edges, signals = standard_scenario()
        # one long record spanning the whole terminal visit
        from r1spike.provisional_alignment import align_apc_records

        span = apc_record("span", 2750, 3150)
        records_signals = dict(signals)
        records_signals["span"] = _sig("span", boarding=5, alighting=3)
        pings_by_id = {p.record_id: p for p in pings}
        edges2 = align_apc_records(
            [span],
            [run],
            pings_by_id,
            records_signals,
            CLUSTERS,
            max_time_gap_s=900,
            interpolation_max_gap_s=300,
        )
        episodes = _detect(run, pings, edges + edges2, records_signals)
        result = refine_run(run, pings, episodes, variant="v2_conditional", cfg=CFG)
        roles = spanning_roles(result, episodes)
        span_roles = [r for r in roles if r.apc_source_record_id == "span"]
        assert span_roles
        role = span_roles[0]
        assert role.boundary_role == "spanning_terminal_visit"
        assert role.dual_role
        assert role.arrival_role_hypothesis == "inbound_service_end_evidence"
        assert role.departure_role_hypothesis == ("outbound_service_start_evidence")
        assert role.arrival_segment_candidate != role.departure_segment_candidate

    def test_17_spanning_row_never_duplicates_source_identity(self):
        run, pings, edges, signals = standard_scenario()
        episodes = _detect(run, pings, edges, signals)
        result = refine_run(run, pings, episodes, variant="v2_conditional", cfg=CFG)
        roles = spanning_roles(result, episodes)
        per_record: dict[str, int] = {}
        for r in roles:
            key = r.apc_source_record_id + ":" + r.episode_id
            per_record[key] = per_record.get(key, 0) + 1
        assert all(n == 1 for n in per_record.values())
        # the role structure carries no passenger quantities at all
        for r in roles:
            for field in r.__dataclass_fields__:
                assert "board" not in field and "alight" not in field


class TestConservation:
    def test_18_turnaround_pings_have_exactly_one_state(self):
        _, pings, _, result = _refine("v2_conditional")
        assert set(result.ping_states) == {p.record_id for p in pings}
        turnaround_ids = {rid for t in result.turnarounds for rid in t.record_ids}
        for rid in turnaround_ids:
            assert result.ping_states[rid] == PingState.TURNAROUND.value

    def test_19_20_no_ping_in_two_segments_and_no_overlap(self):
        _, _, _, result = _refine("v2_conditional")
        seen: set[str] = set()
        for seg in result.refined_segments:
            for rid in seg.record_ids:
                assert rid not in seen
                seen.add(rid)
        ordered = sorted(result.refined_segments, key=lambda s: s.start_instant_s)
        for a, b in zip(ordered, ordered[1:], strict=False):
            assert a.end_instant_s <= b.start_instant_s

    def test_21_ambiguous_boundary_is_not_applied(self):
        scenario = {"alighting_arrival": 3100, "boarding_departure": 2820}
        run, pings, edges, signals = standard_scenario(**scenario)
        episodes = _detect(run, pings, edges, signals)
        result = refine_run(run, pings, episodes, variant="v2_conditional", cfg=CFG)
        assert result.ambiguous_boundary_ids
        applied_eps = {
            _episode_of(d, episodes)
            for d in result.boundary_decisions
            if d.status == BoundaryStatus.APPLIED
        }
        north = next(e for e in episodes if e.cluster_id == "cl-north")
        assert north not in applied_eps

    def test_22_exactly_one_refinement_pass(self):
        run, pings, edges, signals = standard_scenario()
        episodes = _detect(run, pings, edges, signals)
        first = refine_run(run, pings, episodes, variant="v2_conditional", cfg=CFG)
        second = refine_run(run, pings, episodes, variant="v2_conditional", cfg=CFG)
        # Idempotent, not iterative: identical frozen output, and the
        # diagnostic only reports — it cannot apply anything.
        assert [s.record_ids for s in first.refined_segments] == [
            s.record_ids for s in second.refined_segments
        ]
        diag = residual_boundary_diagnostic(first, episodes)
        assert set(diag) == {
            "applied_boundaries",
            "ambiguous_boundaries",
            "selected_episodes_without_applied_boundary",
        }
        import r1spike.boundary_refinement as br

        assert not hasattr(br, "refine_again")
        assert not hasattr(br, "iterate_refinement")


class TestInstantaneousAndTouchingCuts:
    def test_applied_instantaneous_cut_splits_the_run(self):
        """Regression (adversarial review): an APPLIED boundary whose cut
        interval owns no ping index must still split the run into two
        segments carrying the boundary id, with an empty turnaround."""
        from .spike02_helpers import LAT_PER_M, SOUTH_LAT, dwell_pings

        mid_lat = SOUTH_LAT + 2500 * LAT_PER_M
        up = [
            p
            for p in trip_pings("up", 1000, northbound=True, n=15)
            if p.latitude <= mid_lat
        ]
        dw = dwell_pings("sd", up[-1].instant_s + 60, mid_lat, n=6)
        down = [
            p
            for p in trip_pings("down", dw[-1].instant_s + 60, northbound=False, n=15)
            if p.latitude <= mid_lat
        ]
        pings = up + dw + down
        run = make_run(pings)
        along = {p.record_id: (p.latitude - SOUTH_LAT) / LAT_PER_M for p in pings}
        from r1spike.terminal_episodes import detect_episodes

        episodes = detect_episodes(
            run, pings, CLUSTERS, [], {}, cfg=EP_CFG, along_by_id=along
        )
        turns = [e for e in episodes if e.kind == "short_turn_candidate"]
        assert turns and turns[0].physical_arrival_s == (turns[0].physical_departure_s)
        result = refine_run(run, pings, episodes, variant="v2_conditional", cfg=CFG)
        applied = set(result.applied_boundary_ids)
        # find the short-turn boundary among applied decisions
        st = _episode_of_kind(result, episodes, "short_turn_candidate")
        assert st is not None, "short-turn boundary must be applied"
        before = [s for s in result.refined_segments if s.boundary_id_after == st]
        after = [s for s in result.refined_segments if s.boundary_id_before == st]
        assert before and after, "instantaneous cut must split the run"
        assert st in applied

    def test_touching_half_open_cuts_are_not_conflicts(self):
        from r1spike.boundary_refinement import _decide_boundaries
        from r1spike.spike02_contracts import (
            BoundaryCandidate,
            BoundaryStatus,
            EpisodeStatus,
        )
        from r1spike.terminal_episodes import detect_episodes as _de

        from .test_spike02_episodes import standard_scenario

        run, pings, edges, signals = standard_scenario()
        episodes = _de(run, pings, CLUSTERS, edges, signals, cfg=EP_CFG)
        ep = next(e for e in episodes if e.status == EpisodeStatus.SELECTED)
        eps_by_id = {ep.episode_id: ep}

        def cand(bid, start, end):
            return BoundaryCandidate(
                boundary_id=bid,
                run_id=run.run_id,
                episode_id=ep.episode_id,
                source="terminal_episode",
                cut_start_s=start,
                cut_end_s=end,
                cut_index_first_inside=0,
                cut_index_first_after=1,
                evidence_families=("terminal_geofence", "sustained_dwell"),
                apc_precedence_used=False,
                quality=None,
                score_q=500_000,
            )

        decisions = _decide_boundaries(
            [cand("b1", 100, 200), cand("b2", 200, 300)],
            eps_by_id,
            variant="v2_conditional",
            cfg=CFG,
        )
        assert all(d.status == BoundaryStatus.APPLIED for d in decisions)


def _episode_of_kind(result, episodes, kind):
    from r1spike.boundary_refinement import _boundary_id

    for e in episodes:
        if e.kind != kind:
            continue
        bid = _boundary_id(e.run_id, e.episode_id + ":" + result.variant)
        if bid in result.applied_boundary_ids:
            return bid
    return None
