"""Corruption tests for an auditor independent of production decision code."""

import copy
from pathlib import Path

import pytest

from tools.merge_review.gtfs_matching import multimode, partial_audit
from tools.merge_review.gtfs_matching.partial import evaluate_matching_modes
from tools.merge_review.gtfs_matching.tests.test_matching_evidence import points, trip
from tools.merge_review.tests.test_gtfs_multimode_run import source_run  # noqa: F401


def fixture():
    projection = {"points": points(37), "apc_evidence": []}
    canonical = {
        "references": [{"reference_id": "r", "direction_label": "NORTH"}],
        "route_id": "29",
        "trips": [trip()],
    }
    config = {"window_context_s": [300]}
    modes = evaluate_matching_modes(projection, canonical, config)
    return (
        {
            "case_id": "case",
            "projection": projection,
            "apc_evidence": [],
            "matching_modes": modes,
            "result": modes["global"]["result"],
            "config": config,
        },
        canonical,
        config,
    )


def test_mode_audit_does_not_need_production_partitioner(monkeypatch):
    from tools.merge_review.gtfs_matching import partial

    artifact, _, config = fixture()
    monkeypatch.setattr(
        partial,
        "partition_projection",
        lambda *a, **k: pytest.fail("Production selector called"),
    )
    report = partial_audit.Report()
    partial_audit.audit_modes(artifact, config, report, "case")
    assert report.output()["validation_ok"]
    assert report.examined["mode_results"] == 4


@pytest.mark.parametrize(
    "corruption,code",
    [
        ("window", "outside_mode_window_sources"),
        ("delay", "outside_mode_alignment_source"),
        ("cap", "candidate_cap_exceeded"),
        ("probability", "uncalibrated_probability"),
        ("boundary", "incorrect_third_time_ranges"),
        ("distance", "incorrect_partition_distance"),
        ("duplicate", "incorrect_avl_selection"),
    ],
)
def test_mode_audit_catches_cross_third_evidence_and_selection_tampering(
    corruption, code
):
    artifact, _, config = fixture()
    mode = artifact["matching_modes"]["early"]
    if corruption == "window":
        mode["result"]["windows"]["windows"][0]["observation_ids"].append("avl|a36")
    elif corruption == "delay":
        mode["result"]["all_candidates"][0]["whole_alignment"]["delay_process"][0][
            "source_id"
        ] = "avl|a36"
    elif corruption == "cap":
        mode["result"]["candidates"] = [{}] * 4
    elif corruption == "probability":
        mode["result"]["all_candidates"][0]["probability"] = 0.8
    elif corruption == "boundary":
        mode["selection"]["time_ranges"][0]["end"] += 10
    elif corruption == "distance":
        mode["selection"]["total_effective_progress_m"] += 100
    elif corruption == "duplicate":
        mode["selection"]["selected_avl_ids"].append(
            mode["selection"]["selected_avl_ids"][0]
        )
    report = partial_audit.Report()
    partial_audit.audit_modes(artifact, config, report, "case")
    assert report.violations[code]


def test_independent_apc_clip_audit():
    artifact, canonical, config = fixture()
    apc = {
        "source": "apc",
        "rid": "p",
        "t": 0,
        "interval_start": 0,
        "interval_end": 400,
        "arrival": 0,
        "departure": 400,
        "projection_status": "STOP_INTERVAL_ONLY",
    }
    artifact["projection"]["apc_evidence"] = [apc]
    artifact["matching_modes"] = evaluate_matching_modes(
        artifact["projection"], canonical, config
    )
    report = partial_audit.Report()
    partial_audit.audit_modes(artifact, config, report, "case")
    assert report.output()["validation_ok"]
    artifact["matching_modes"]["early"]["selection"]["apc_interval_clips"][0][
        "interval_end"
    ] = 999
    report = partial_audit.Report()
    partial_audit.audit_modes(artifact, config, report, "case")
    assert report.violations["apc_clip_outside_scope"]


def test_overview_audit_checks_original_gtfs_knots_and_fallback_honesty():
    from tools.merge_review.gtfs_matching.overview import overview_case

    artifact, canonical, config = fixture()
    summary = {"id": "case", "direction": "NORTH", "start": 0, "end": 540}
    record = overview_case(artifact, summary, "bus", "bus", canonical, "early")
    report = partial_audit.Report()
    partial_audit.audit_overview_case(
        record, artifact, canonical, "early", config, report
    )
    assert report.output()["validation_ok"]
    corrupted = copy.deepcopy(record)
    corrupted["candidate"]["trajectory"][0]["t"] += 600
    corrupted["candidate_source"] = (
        "fallback" if record["candidate_source"] == "accepted" else "accepted"
    )
    report = partial_audit.Report()
    partial_audit.audit_overview_case(
        corrupted, artifact, canonical, "early", config, report
    )
    assert report.violations["overview_schedule_shifted_or_fabricated"]
    assert report.violations["overview_candidate_source_dishonest"]


def test_overview_audit_rejects_bridged_off_reference_gap():
    from tools.merge_review.gtfs_matching.overview import overview_case

    artifact, canonical, config = fixture()
    artifact["projection"]["points"][18].update(
        projection_status="OFF_REFERENCE", s_m=None
    )
    artifact["matching_modes"] = evaluate_matching_modes(
        artifact["projection"], canonical, config
    )
    record = overview_case(artifact, {"id": "case"}, "bus", "bus", canonical, "global")
    assert len(record["fragments"]) >= 2
    record["fragments"][0]["points"].extend(record["fragments"].pop()["points"])
    report = partial_audit.Report()
    partial_audit.audit_overview_case(
        record, artifact, canonical, "global", config, report
    )
    assert report.violations["overview_bridged_separator"]


def test_full_persisted_audit_and_hash_tampering(source_run):  # noqa: F811
    workspace, source, _ = source_run
    record = multimode.create_multimode_run(
        workspace, source.name, "audit fixture", workers=1
    )
    target = Path(record["path"])
    report = partial_audit.audit_run(target)
    assert report["validation_ok"], report
    assert report["examined"]["cases"] == 1
    assert report["examined"]["overview_cases"] == 4
    with (target / "observations/vehicle-test.json").open("ab") as stream:
        stream.write(b" ")
    report = partial_audit.audit_run(target)
    assert report["violations"]["new_output_hash"]
    assert report["accuracy"] is None and not report["scores_recomputed"]


def test_audit_rejects_path_escape(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        partial_audit._path(tmp_path, "../foreign")
