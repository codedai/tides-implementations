"""Independent persisted-score checks and intentional corruption cases."""

import copy
import math
from pathlib import Path

import pytest

from tools.merge_review.gtfs_matching import score_replay_audit as audit
from tools.merge_review.gtfs_matching.partial_audit import Report
from tools.merge_review.gtfs_matching.tests.test_partial_audit import fixture
from tools.merge_review.tests.test_gtfs_multimode_run import source_run  # noqa: F401


def evidence():
    local = [
        {"score": 1.0, "weight": 2.0, "estimated_delay_s": 120},
        {"score": 2.0, "weight": 1.0, "estimated_delay_s": 180},
        {"score": None, "weight": 1.0, "quality_eligible": True},
    ]
    candidate = {
        "candidate_id": "candidate",
        "weighted_mean": 2.0,
        "weighted_median": 1.0,
        "weighted_p90": 4.0,
        "estimated_delay_s": 120,
        "delay_mad_s": 0,
        "coverage": 0.75,
        "schedule_supported_window_weight_ratio": 0.75,
        "schedule_aligned_point_ratio": 0.75,
        "schedule_aligned_duration_ratio": 0.75,
        "whole_alignment": {"score": 1.0, "status": "EVIDENCE"},
        "apc_support": {"support": 0.8},
        "probability": None,
        "calibrated": False,
    }
    config = {"score_delay_prior_weight": 1.0}
    # Hand-computed formula, not the production aggregator.
    candidate["score"] = 1 + 0.3 + 0.8 + 0.5 + 120 / 3600 + 0.2 + 0.03
    candidate["support_score"] = math.exp(-candidate["score"])
    return candidate, local, config


def test_recompute_formula_including_failed_local_window():
    candidate, local, config = evidence()
    report = Report()
    audit.audit_candidate(candidate, local, config, report, "test")
    assert report.output()["validation_ok"], report.output()
    assert report.examined["aggregate_candidates"] == 1
    assert audit.score_terms(candidate, config)["absolute_delay"] == 120 / 3600


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("weighted_mean", 1.0, "aggregate_statistic_wrong"),
        ("weighted_median", 2.0, "aggregate_statistic_wrong"),
        ("weighted_p90", 2.0, "aggregate_statistic_wrong"),
        ("estimated_delay_s", 180, "aggregate_statistic_wrong"),
        ("delay_mad_s", 60, "aggregate_statistic_wrong"),
        ("schedule_supported_window_weight_ratio", 1, "aggregate_statistic_wrong"),
        ("score", 1.0, "aggregate_cost_wrong"),
        ("support_score", 0.8, "aggregate_support_wrong"),
        ("probability", 0.8, "aggregate_falsely_calibrated"),
    ],
)
def test_independent_arithmetic_detects_corruption(field, value, code):
    candidate, local, config = evidence()
    candidate[field] = value
    report = Report()
    audit.audit_candidate(candidate, local, config, report, "test")
    assert report.violations[code]


def test_missing_whole_or_apc_evidence_costs():
    candidate, _, config = evidence()
    candidate["whole_alignment"] = {"status": "NO_EVIDENCE", "score": None}
    candidate["apc_support"] = {"support": None}
    terms = audit.score_terms(candidate, config)
    assert terms["whole_alignment"] == 1.0
    assert terms["apc"] == 0.0
    assert not audit.gate_values(candidate, config)["structural_evidence_passed"]


def test_delay_prior_is_symmetric_and_historical_default_retained():
    candidate, _, config = evidence()
    positive = audit.score_terms(candidate, config)
    candidate["estimated_delay_s"] *= -1
    assert audit.score_terms(candidate, config) == positive
    assert audit.score_terms(candidate, {})["absolute_delay"] == pytest.approx(
        positive["absolute_delay"] / 20
    )


def test_production_fixture_passes_independent_decision_and_scope_audit():
    artifact, _, config = fixture()
    report = Report()
    audit.audit_case(copy.deepcopy(artifact), artifact, config, report)
    assert report.output()["validation_ok"], report.output()
    assert report.examined["cases"] == 1
    assert report.examined["mode_results"] == 4


@pytest.mark.parametrize(
    "corruption,code",
    [
        ("projection", "upstream_case_evidence_changed"),
        ("selection", "decision_scope_changed"),
        ("global", "top_level_global_result_stale"),
        ("config", "case_config_stale"),
    ],
)
def test_case_preservation_corruption(corruption, code):
    artifact, _, config = fixture()
    old = copy.deepcopy(artifact)
    if corruption == "projection":
        artifact["projection"]["points"][0]["quality"] = 0.8
    elif corruption == "selection":
        artifact["matching_modes"]["early"]["selection"]["status"] = "changed"
    elif corruption == "global":
        artifact["result"] = {}
    else:
        artifact["config"] = {"score_delay_prior_weight": 9}
    report = Report()
    audit.audit_case(old, artifact, config, report)
    assert report.violations[code]


def test_new_matching_result_need_not_equal_historical_global():
    artifact, _, config = fixture()
    old = copy.deepcopy(artifact)
    old["result"] = {"status": "historical status"}
    old["matching_modes"]["global"]["result"] = old["result"]
    report = Report()
    audit.audit_case(old, artifact, config, report)
    assert report.output()["validation_ok"], report.output()


@pytest.mark.parametrize(
    "corruption,code",
    [
        ("gate", "displayed_gate_wrong"),
        ("display", "displayed_candidate_evidence_changed"),
        ("margin", "decision_margin_wrong"),
        ("identity", "decision_identity_stale"),
    ],
)
def test_decision_corruption(corruption, code):
    artifact, _, config = fixture()
    result = copy.deepcopy(artifact["result"])
    if corruption == "gate":
        row = result["candidates"][0]
        row["decision_gate"]["absolute_cost_passed"] = not row["decision_gate"][
            "absolute_cost_passed"
        ]
    elif corruption == "display":
        result["candidates"][0]["score"] += 0.1
    elif corruption == "margin":
        result["top1_top2_margin"] = 0.9
    else:
        result["reported_candidate_id"] = "not-the-best"
    report = Report()
    audit.audit_decision(result, config, report, "test")
    assert report.violations[code]


def test_no_production_matching_or_decision_call(monkeypatch):
    from tools.merge_review.gtfs_matching import decision, integration

    candidate, local, config = evidence()
    monkeypatch.setattr(
        integration,
        "aggregate_candidate",
        lambda *a, **k: pytest.fail("aggregator called"),
    )
    monkeypatch.setattr(
        decision, "apply_decision_guard", lambda *a, **k: pytest.fail("guard called")
    )
    report = Report()
    audit.audit_candidate(candidate, local, config, report, "test")
    assert report.output()["validation_ok"]


def test_full_persisted_scoring_replay_audit(source_run):  # noqa: F811
    from tools.merge_review.gtfs_matching.multimode import create_multimode_run

    workspace, source, _ = source_run
    partial = create_multimode_run(workspace, source.name, "source modes", workers=1)
    replay = create_multimode_run(
        workspace,
        partial["run_id"],
        "score replay",
        workers=1,
        scoring_overrides={"score_delay_prior_weight": 1.0},
    )
    target = Path(replay["path"])
    result = audit.audit_run(target)
    assert result["validation_ok"], result
    assert result["examined"]["cases"] == 1
    assert result["examined"]["overview_cases"] == 4
    assert result["scores_recomputed"] is True
    assert result["alignment_distances_recomputed"] is False
    assert result["global_result_preservation_expected"] is False
    assert result["source_delay_prior_weight"] == 0.05
    assert result["delay_prior_weight"] == 1.0


def test_audit_rejects_path_escape(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        audit._path(tmp_path, "../outside")
