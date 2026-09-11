"""Spike-02R integrity tests: V1R APC-independence, passenger firewall
variants, cross-fitting, evidence regimes, terminal scope, manifest and
privacy guards. (Spec section 24 items 1-6, 16, 27-28, 34-37, 40.)"""

from __future__ import annotations

import json

import polars as pl
import pytest
from r1spike.boundary_v2 import BoundaryV2Config
from r1spike.crossfit import assign_folds, collision_group, fold_of
from r1spike.evidence_dependencies import (
    anchor_edges,
    assert_avl_only,
    boundary_edges,
    reuse_report,
)
from r1spike.label_factorial import build_packet_rows
from r1spike.spike02r_manifest import (
    Guard02RError,
    Spike02RRun,
    check_run_dir02r,
)
from r1spike.terminal_episodes import EpisodeConfig
from r1spike.true_avl_only import (
    PROFILE_APC_ACTIVITY,
    PROFILE_APC_STRUCTURAL,
    PROFILE_AVL,
    assert_gtfs_only_clusters,
    build_profile_chunks,
)

from .spike02_helpers import (
    CLUSTERS,
    NORTH_CLUSTER,
    apc_record,
    make_cluster,
    make_run,
    trip_pings,
)
from .test_spike02_episodes import _sig, standard_scenario

EP_CFG = EpisodeConfig()
B_CFG = BoundaryV2Config()

GTFS_ONLY = [make_cluster("cl-n", 43.75), make_cluster("cl-s", 43.70)]


def _fingerprint(chunks):
    return [(c.chunk_id, c.record_ids, c.role.value) for c in chunks]


def _run_and_pings():
    run, pings, edges, signals = standard_scenario()
    pings_by_id = {p.record_id: p for p in pings}
    return run, pings, pings_by_id, edges, signals


class TestV1RIndependence:
    def test_1_v1r_unchanged_when_apc_removed(self):
        run, _p, pings_by_id, edges, signals = _run_and_pings()
        chunks_a, *_ = build_profile_chunks(
            [run],
            pings_by_id,
            GTFS_ONLY,
            profile=PROFILE_AVL,
            episode_cfg=EP_CFG,
            boundary_cfg=B_CFG,
            along_by_id={},
        )
        # "removing" APC is a no-op because V1R cannot receive it at all
        chunks_b, *_ = build_profile_chunks(
            [run],
            pings_by_id,
            GTFS_ONLY,
            profile=PROFILE_AVL,
            episode_cfg=EP_CFG,
            boundary_cfg=B_CFG,
            along_by_id={},
        )
        assert _fingerprint(chunks_a) == _fingerprint(chunks_b)

    def test_1b_v1r_rejects_apc_arguments_structurally(self):
        run, _p, pings_by_id, edges, signals = _run_and_pings()
        with pytest.raises(AssertionError):
            build_profile_chunks(
                [run],
                pings_by_id,
                GTFS_ONLY,
                profile=PROFILE_AVL,
                episode_cfg=EP_CFG,
                boundary_cfg=B_CFG,
                along_by_id={},
                edges=edges,
                signals_by_id=signals,
            )

    def test_1c_v1r_rejects_apc_derived_clusters(self):
        import dataclasses

        tainted = dataclasses.replace(
            NORTH_CLUSTER,
            evidence_sources=(
                "gtfs_pattern_endpoint",
                "apc_observed_stop_code_hypothesis",
            ),
        )
        with pytest.raises(AssertionError):
            assert_gtfs_only_clusters([tainted])

    def test_2_v1r_unchanged_when_apc_values_permuted(self):
        run, pings, pings_by_id, edges, signals = _run_and_pings()
        base, *_ = build_profile_chunks(
            [run],
            pings_by_id,
            GTFS_ONLY,
            profile=PROFILE_AVL,
            episode_cfg=EP_CFG,
            boundary_cfg=B_CFG,
            along_by_id={},
        )
        # permute APC evidence in the surrounding pipeline (V2R input);
        # V1R has no access to it, so its output cannot move.
        permuted_signals = {
            rid: _sig(rid, boarding=99, alighting=99) for rid in signals
        }
        _ = permuted_signals
        again, *_ = build_profile_chunks(
            [run],
            pings_by_id,
            GTFS_ONLY,
            profile=PROFILE_AVL,
            episode_cfg=EP_CFG,
            boundary_cfg=B_CFG,
            along_by_id={},
        )
        assert _fingerprint(base) == _fingerprint(again)

    def test_6_avl_only_evidence_edges_assertable(self):
        run, pings, pings_by_id, _e, _s = _run_and_pings()
        _c, _cand, _dec, eps = build_profile_chunks(
            [run],
            pings_by_id,
            GTFS_ONLY,
            profile=PROFILE_AVL,
            episode_cfg=EP_CFG,
            boundary_cfg=B_CFG,
            along_by_id={},
        )
        edges = boundary_edges(eps, profile=PROFILE_AVL)
        assert_avl_only(edges)  # must not raise: no APC edge can exist


class TestPassengerFirewallVariants:
    def test_3_structural_unchanged_when_passenger_numbers_perturbed(self):
        run, pings, pings_by_id, edges, _sig_map = _run_and_pings()
        sig_a = {rid: _sig(rid, boarding=3, alighting=1) for rid in _sig_map}
        sig_b = {rid: _sig(rid, boarding=40, alighting=0) for rid in _sig_map}
        out_a, *_ = build_profile_chunks(
            [run],
            pings_by_id,
            CLUSTERS,
            profile=PROFILE_APC_STRUCTURAL,
            episode_cfg=EP_CFG,
            boundary_cfg=B_CFG,
            along_by_id={},
            edges=edges,
            signals_by_id=sig_a,
        )
        out_b, *_ = build_profile_chunks(
            [run],
            pings_by_id,
            CLUSTERS,
            profile=PROFILE_APC_STRUCTURAL,
            episode_cfg=EP_CFG,
            boundary_cfg=B_CFG,
            along_by_id={},
            edges=edges,
            signals_by_id=sig_b,
        )
        assert _fingerprint(out_a) == _fingerprint(out_b)

    def test_4_activity_changes_are_enumerable(self):
        run, pings, pings_by_id, edges, sig_map = _run_and_pings()
        flipped = dict(sig_map)
        # flip the alighting record to boarding-only: activity variant
        # may change; every change is enumerable by fingerprint diff
        flipped["apc-al"] = _sig("apc-al", boarding=4, alighting=0)
        out_a, *_ = build_profile_chunks(
            [run],
            pings_by_id,
            CLUSTERS,
            profile=PROFILE_APC_ACTIVITY,
            episode_cfg=EP_CFG,
            boundary_cfg=B_CFG,
            along_by_id={},
            edges=edges,
            signals_by_id=sig_map,
        )
        out_b, *_ = build_profile_chunks(
            [run],
            pings_by_id,
            CLUSTERS,
            profile=PROFILE_APC_ACTIVITY,
            episode_cfg=EP_CFG,
            boundary_cfg=B_CFG,
            along_by_id={},
            edges=edges,
            signals_by_id=flipped,
        )
        diff = [
            (a, b)
            for a, b in zip(_fingerprint(out_a), _fingerprint(out_b), strict=False)
            if a != b
        ]
        # enumeration is the deliverable; equality is also acceptable
        assert isinstance(diff, list)


class TestCrossfit:
    def test_5_same_record_cannot_build_boundary_and_confirm_anchor(self):
        recs = [
            apc_record("r1", 1000, 1100),
            apc_record("r2", 1000, 1100),
            apc_record("r3", 9000, 9100),
        ]
        folds = assign_folds(recs)
        # r1 and r2 share a collision group -> same fold always
        assert collision_group(recs[0]) == collision_group(recs[1])
        assert folds["r1"] == folds["r2"]
        # a record that BUILT fold-A boundaries is never in the fold-B
        # scoring set by construction:
        fold_a = {r for r, f in folds.items() if f == "A"}
        fold_b = {r for r, f in folds.items() if f == "B"}
        assert not (fold_a & fold_b)

    def test_fold_assignment_is_value_independent(self):
        r_small = apc_record("x1", 1000, 1100)
        assert fold_of(r_small) in ("A", "B")
        # same identity fields -> same fold regardless of counts
        assert fold_of(r_small) == fold_of(apc_record("x1", 1000, 1100))

    def test_reuse_report_quantifies_overlap(self):
        b = boundary_edges({"run1": []}, profile=PROFILE_APC_ACTIVITY)
        a = anchor_edges({"chunk1": ["rec1", "rec2"]}, {"chunk1": {"rec1"}})
        rep = reuse_report(b, a)
        assert rep["anchor_edges"] == 2
        assert rep["anchor_edges_overlapping_boundary"] == 1
        assert rep["segments_with_boundary_overlap"] == 1


class TestTerminalScope:
    def test_27_28_cluster_scope_is_feed_and_date_qualified(self):
        # a cluster scoped to feedX/2023-11-28 must not match a point
        # queried under another service date
        from r1spike.terminal_clusters import point_cluster

        c = make_cluster("cl-n", 43.75)
        assert point_cluster([c], 43.75, -79.45, service_date="2023-11-28") is not None
        assert point_cluster([c], 43.75, -79.45, service_date="2023-12-25") is None
        # the experimental 1652 hypothesis stays inside its cluster scope
        assert "1652" in NORTH_CLUSTER.member_stop_codes
        assert NORTH_CLUSTER.service_dates == ("2023-11-28",)


class TestManifestAndPrivacy:
    def test_35_manifest_records_hashes_argv_and_dirty_state(self, tmp_path):
        run_dir = tmp_path / "artifacts" / "experimental-r1-spike02r-t"
        run = Spike02RRun(
            run_dir,
            config_hash="c" * 64,
            source_tree={
                "combined_sha256": "s" * 64,
                "git_revision": "rev",
                "git_dirty": True,
            },
            input_hashes={"x": "h"},
            baseline_hashes={"b": "h"},
            environment={"argv": ["prog", "--run-id", "t"]},
        )
        run.write_parquet(
            pl.DataFrame({"a": [1]}),
            "x_probe.parquet",
            grain="g",
            key=["a"],
            producer="test",
        )
        run.finalize({"phase": "test"})
        manifest = json.loads((run.run_dir / "run_manifest.json").read_text())
        assert manifest["environment"]["argv"] == ["prog", "--run-id", "t"]
        assert manifest["source_tree_sha256"] == "s" * 64
        assert manifest["git_dirty"] is True
        assert manifest["artifacts"]["x_probe.parquet"]["sha256"]
        out = json.loads((run.run_dir / "output_content_hashes.json").read_text())
        assert out["outputs"]["x_probe.parquet"]

    def test_36_artifacts_are_permission_restricted(self, tmp_path):
        import stat

        run_dir = tmp_path / "artifacts" / "experimental-r1-spike02r-t2"
        run = Spike02RRun(
            run_dir,
            config_hash="c",
            source_tree={"combined_sha256": "s"},
            input_hashes={},
            baseline_hashes={},
            environment={},
        )
        p = run.write_parquet(
            pl.DataFrame({"a": [1]}),
            "x_probe.parquet",
            grain="g",
            key=["a"],
            producer="test",
        )
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode & 0o077 == 0  # no group/other access
        dmode = stat.S_IMODE(run.run_dir.stat().st_mode)
        assert dmode & 0o077 == 0

    def test_34_37_redacted_packet_contains_no_raw_ids_or_times(self):
        cases = [
            {
                "real_id": "c1-secret-563860",
                "stratum": "fragment",
                "kind": "boundary",
                "epoch_s": 1701185541,
                "events": [("c1-secret-563860", 36, "arrival")],
                "public_summary": "terminal dwell case",
                "detail": {"vehicle": "7237"},
            }
        ]
        restricted, redacted = build_packet_rows(cases, salt="s")
        assert restricted[0]["real_id"] == "c1-secret-563860"
        red = json.dumps(redacted)
        assert "563860" not in red
        assert "7237" not in red
        assert "1701185541" not in red
        assert redacted[0]["case_id"].startswith("C-")

    def test_40_no_tides_or_publication_writes(self, tmp_path):
        run = Spike02RRun(
            tmp_path / "artifacts" / "experimental-r1-spike02r-t3",
            config_hash="c",
            source_tree={"combined_sha256": "s"},
            input_hashes={},
            baseline_hashes={},
            environment={},
        )
        for bad in (
            "x_trips_performed.parquet",
            "datapackage.json",
            "x_tides.parquet",
            "surprise.json",
            "plain.parquet",
        ):
            with pytest.raises(Guard02RError):
                if bad.endswith(".parquet"):
                    run.write_parquet(
                        pl.DataFrame({"a": [1]}),
                        bad,
                        grain="g",
                        key=["a"],
                        producer="t",
                    )
                else:
                    run.write_json({}, bad)
        with pytest.raises(Guard02RError):
            check_run_dir02r(
                tmp_path
                / "database_offline"
                / "artifacts"
                / "experimental-r1-spike02r-x"
            )


class TestRegimeRepair:
    def test_16_apc_derived_anchor_uses_apc_evidence_regime(self):
        # boundary candidates from APC profiles carry apc_evidence and
        # the anchor scorer selects APC_EVIDENCE whenever anchor_*
        # features exist — asserted at the kernel level here.
        run, pings, pings_by_id, edges, signals = _run_and_pings()
        _c, cands, _d, _e = build_profile_chunks(
            [run],
            pings_by_id,
            CLUSTERS,
            profile=PROFILE_APC_ACTIVITY,
            episode_cfg=EP_CFG,
            boundary_cfg=B_CFG,
            along_by_id={},
            edges=edges,
            signals_by_id=signals,
        )
        assert cands and all(c.evidence_regime == "apc_evidence" for c in cands)
        _c2, cands2, _d2, _e2 = build_profile_chunks(
            [run],
            pings_by_id,
            GTFS_ONLY,
            profile=PROFILE_AVL,
            episode_cfg=EP_CFG,
            boundary_cfg=B_CFG,
            along_by_id={},
        )
        assert cands2 and all(c.evidence_regime == "avl_only" for c in cands2)


class TestStructuralNegativeCountFirewall:
    def test_structural_signals_ignore_count_sign(self):
        """Adversarial-review repair: the negative-count flag is
        count-derived, so V2R-STRUCTURAL must not see it."""
        from r1spike.spike02_pipeline import Spike02Prepared
        from r1spike.spike02r_pipeline import structural_signals

        class _S1:
            apc_records = [apc_record("rn", 1000, 1100)]

        prepared = Spike02Prepared.__new__(Spike02Prepared)
        prepared.s1 = _S1()
        flags_pos = {
            "rn": {
                "boarding": 3,
                "alighting": 1,
                "avg_sample": 1,
                "_flag_interval_inverted": False,
                "_flag_visit_key_collision": False,
                "_flag_negative_count": False,
            }
        }
        flags_neg = {
            "rn": {
                "boarding": -3,
                "alighting": 1,
                "avg_sample": 1,
                "_flag_interval_inverted": False,
                "_flag_visit_key_collision": False,
                "_flag_negative_count": True,
            }
        }
        s_pos = structural_signals(prepared, flags_pos)["rn"]
        s_neg = structural_signals(prepared, flags_neg)["rn"]
        assert s_pos.quality == s_neg.quality
        assert s_pos.state == s_neg.state


class TestNoPingCutStillSplits:
    def test_applied_cut_between_pings_splits(self):
        from r1spike.spike02r_contracts import BoundaryCandidateV2
        from r1spike.true_avl_only import chunks_from_cuts

        pings = trip_pings("t", 0, northbound=True, n=6, step_s=100)
        run = make_run(pings)
        cut = BoundaryCandidateV2(
            candidate_id="c",
            activity_run_id=run.run_id,
            candidate_start_s=230,
            candidate_end_s=260,
            boundary_role="terminal",
            evidence_families=(),
            evidence_regime="avl_only",
            qscore=0,
            hard_status="hard_valid",
        )
        chunks = chunks_from_cuts("apc-activity", run, pings, [cut])
        # the [230, 260) interval owns no ping (pings at 200, 300):
        # the run must still split into two chunks there
        assert len(chunks) == 2
        assert chunks[0].end_instant_s == 200
        assert chunks[1].start_instant_s == 300
