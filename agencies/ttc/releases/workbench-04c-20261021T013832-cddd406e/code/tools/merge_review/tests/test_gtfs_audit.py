"""Mutations must be caught without rerunning a matcher or writing an audit."""

import copy
import gzip
import hashlib
import json
import shutil
from datetime import datetime, timedelta

import polars as pl
import pytest

from tools.merge_review.gtfs_matching.audit import _normal, audit_artifact, audit_run

T = datetime(2023, 11, 6)
CASE = "direction:v:0000"


def _refresh_fit(whole):
    rows = whole["delay_process"]
    for row in rows:
        row.update(
            raw_delay_s=row["delay_s"],
            constraint_adjustment_s=0,
            delay_fit_status="MONOTONE_ALREADY_FEASIBLE",
        )
    whole["delay_fit_fragments"] = [
        dict(
            id="whole_fragment:0000",
            source_ids=[row["source_id"] for row in rows],
            start=rows[0]["t"],
            end=rows[-1]["t"],
            status="MONOTONE_ALREADY_FEASIBLE",
            fallback=False,
            method="weighted_isotonic_projection_of_soft_viterbi_clock",
            knots=[
                dict(
                    t=row["t"],
                    raw_delay_s=row["delay_s"],
                    delay_s=row["delay_s"],
                    raw_corrected_t=row["corrected_t"],
                    corrected_t=row["corrected_t"],
                    constraint_adjustment_s=0,
                    weight=1,
                )
                for row in (rows[0], rows[-1])
            ],
        )
    ]


def fixture():
    originals = [
        dict(
            source="avl",
            source_rid=f"v{i}",
            rid=f"v{i}",
            vehicle="v",
            route="29",
            time=T + timedelta(seconds=i * 20),
            candidate_segment_ids=[CASE],
        )
        for i in range(3)
    ]
    originals.append(
        dict(
            source="apc",
            source_rid="a",
            rid="a",
            vehicle="v",
            route="29",
            arrival=T + timedelta(seconds=10),
            departure=T + timedelta(seconds=30),
            stop="1652",
            candidate_segment_ids=[CASE],
        )
    )
    observations = []
    for original in originals:
        row = _normal(original)
        instant = row.get("time", row.get("arrival"))
        row.update(
            t=instant,
            reference_id="r1",
            s_m=100,
            lateral_m=5,
            projection_status="ON_REFERENCE",
            quality=1,
            projection_alternatives=[
                dict(
                    reference_id="r1",
                    projection_status="ON_REFERENCE",
                    s_m=100,
                    lateral_m=5,
                ),
                dict(
                    reference_id="r2",
                    projection_status="OFF_REFERENCE",
                    s_m=None,
                    lateral_m=105,
                ),
            ],
        )
        if row["source"] == "apc":
            row.update(
                interval_start=row["arrival"],
                interval_end=row["departure"],
                trajectory_fabricated=False,
            )
        observations.append(row)
    references = [
        dict(reference_id=r, feed_version="feed", route_id="route")
        for r in ["r1", "r2"]
    ]
    trips = [
        dict(
            feed_version="feed",
            route_id="route",
            service_date="2023-11-06",
            trip_id="trip",
            reference_id="r1",
            scheduled=[dict(t=_normal(T), s_m=0)],
        )
    ]
    key = "feed|2023-11-06|trip"
    ids = [f"{r['source']}|{r['rid']}" for r in originals]
    fragment = dict(
        sampled_source_ids=ids[:3],
        source_ids=ids[:3],
        source_start=_normal(T),
        source_end=_normal(T) + 40,
        aligned_duration_s=40,
        original_points=3,
        dtw_scored_points=3,
        all_original_points_dtw_validated=True,
    )
    metric = dict(status="EVIDENCE", score=0.1, fragments=[fragment])
    candidate = dict(
        trips[0],
        candidate_id=key,
        probability=None,
        calibrated=False,
        coverage=1,
        schedule_aligned_point_ratio=1,
        schedule_aligned_duration_ratio=1,
        schedule_supported_window_weight_ratio=1,
        scheduled_trajectory=copy.deepcopy(trips[0]["scheduled"]),
        whole_alignment=copy.deepcopy(metric),
    )
    candidate["whole_alignment"]["delay_process"] = [
        dict(
            source_id=ids[i],
            source_rid=f"v{i}",
            t=_normal(T) + i * 20,
            delay_s=10,
            corrected_t=_normal(T) + i * 20 - 10,
            fragment_id="whole_fragment:0000",
            supporting_window_ids=["atomic:0"],
            status="SUPPORTED",
            schedule_aligned=True,
        )
        for i in range(3)
    ]
    _refresh_fit(candidate["whole_alignment"])
    window = dict(
        id="atomic:0",
        kind="atomic",
        start=_normal(T),
        end=_normal(T) + 60,
        observation_ids=ids,
        weights=dict.fromkeys(ids, 1.0),
        weight=4.0,
    )
    result = dict(
        status="MATCHED",
        candidates=[candidate],
        all_candidates=[copy.deepcopy(candidate)],
        calibration=dict(calibrated=False, probability=None),
        local_scores={key: [dict(metric, window_id="atomic:0")]},
        windows=dict(windows=[window], observation_weights=dict.fromkeys(ids, 1.0)),
        retrieval={},
        whole_trip={},
    )
    artifact = dict(
        case_id=CASE,
        config=dict(
            max_projection_distance_m=100,
            aggregation_min_coverage=0.35,
            window_min_quality=0.2,
            score_max_gap_s=90,
            score_backtrack_tolerance_m=25,
        ),
        projection=dict(
            points=observations[:3], apc_evidence=copy.deepcopy(observations[3:])
        ),
        apc_evidence=observations[3:],
        result=result,
    )
    return artifact, originals, references, trips


def test_pure_artifact_audit_does_not_mutate_or_score(monkeypatch):
    from tools.merge_review.gtfs_matching import projection, scoring

    def forbidden(*args, **kwargs):
        raise AssertionError("Auditor must never recompute projections/scores")

    monkeypatch.setattr(projection, "project_sequence", forbidden)
    monkeypatch.setattr(scoring, "score_window", forbidden)
    values = fixture()
    original = copy.deepcopy(values)
    result = audit_artifact(*values)
    assert result["validation_ok"], result
    assert values == original
    assert result["accuracy"] is None
    assert result["scores_recomputed"] is False


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda a: a["apc_evidence"].clear(), "case_source_identity_changed"),
        (
            lambda a: a["projection"]["points"][0].update(route="929"),
            "non_route_29_in_case",
        ),
        (
            lambda a: a["projection"]["points"][0].update(lateral_m=101),
            "on_reference_gate_failed",
        ),
        (
            lambda a: a["projection"]["points"][0]["projection_alternatives"][1].update(
                projection_status="ON_REFERENCE"
            ),
            "on_reference_gate_failed",
        ),
        (lambda a: a["apc_evidence"][0].update(interval_end=0), "apc_interval_changed"),
        (
            lambda a: a["result"]["candidates"][0].update(probability=0.9),
            "uncalibrated_candidate_probability",
        ),
        (
            lambda a: a["result"]["candidates"][0].update(coverage=1.2),
            "ratio_out_of_bounds",
        ),
        (
            lambda a: a["result"]["candidates"][0].update(service_date="2023-11-07"),
            "candidate_identity_changed",
        ),
        (
            lambda a: a["result"]["candidates"][0]["whole_alignment"].update(
                status="NO_EVIDENCE"
            ),
            "matched_without_whole_evidence",
        ),
        (
            lambda a: a["result"]["candidates"][0].update(
                schedule_aligned_duration_ratio=0.1
            ),
            "matched_below_required_coverage",
        ),
    ],
)
def test_mutation_rejected(mutation, code):
    artifact, originals, refs, trips = fixture()
    mutation(artifact)
    result = audit_artifact(artifact, originals, refs, trips)
    assert result["violations"].get(code, 0) > 0, result


def test_scored_point_must_pass_candidate_specific_reference():
    artifact, originals, refs, trips = fixture()
    trips[0]["reference_id"] = "r2"
    for candidate in (
        artifact["result"]["candidates"] + artifact["result"]["all_candidates"]
    ):
        candidate["reference_id"] = "r2"
    result = audit_artifact(artifact, originals, refs, trips)
    assert result["violations"]["rejected_or_foreign_reference_point_scored"] > 0


def test_persisted_overlapping_scale_weights_cannot_exceed_one():
    artifact, originals, refs, trips = fixture()
    window = copy.deepcopy(artifact["result"]["windows"]["windows"][0])
    window.update(id="context:1", kind="context_300")
    artifact["result"]["windows"]["windows"].append(window)
    result = audit_artifact(artifact, originals, refs, trips)
    assert result["violations"]["source_window_weight_exceeds_one"] == 4


def test_unknown_schema_marks_checks_incomplete_instead_of_pass():
    artifact, originals, refs, trips = fixture()
    artifact["result"]["windows"] = []  # browser aliases are not raw audit bundles
    result = audit_artifact(artifact, originals, refs, trips)
    assert not result["checks_complete"]
    assert not result["validation_ok"]
    assert result["schema_warnings"]


def test_evidence_without_persisted_trace_is_incomplete():
    artifact, originals, refs, trips = fixture()
    artifact["result"]["local_scores"]["feed|2023-11-06|trip"][0].pop("fragments")
    result = audit_artifact(artifact, originals, refs, trips)
    assert not result["checks_complete"]
    assert result["schema_warnings"]["unknown_or_missing_schema"]


def test_full_dtw_trace_cannot_hide_rejected_points_outside_metric_sample():
    artifact, originals, refs, trips = fixture()
    metric = artifact["result"]["local_scores"]["feed|2023-11-06|trip"][0]
    metric["fragments"][0]["sampled_source_ids"] = ["avl|v0", "avl|v1"]
    artifact["projection"]["points"][2]["projection_status"] = "OFF_REFERENCE"
    result = audit_artifact(artifact, originals, refs, trips)
    assert result["violations"]["rejected_or_foreign_reference_point_scored"]


def _whole(artifact):
    return artifact["result"]["all_candidates"][0]["whole_alignment"]


def test_shared_delay_cannot_reverse_time_even_when_whole_has_no_evidence():
    artifact, originals, refs, trips = fixture()
    whole = _whole(artifact)
    whole.update(status="NO_EVIDENCE", fragments=[])
    for row in whole["delay_process"]:
        row["schedule_aligned"] = False
    row = whole["delay_process"][1]
    row.update(delay_s=100, corrected_t=row["t"] - 100)
    result = audit_artifact(artifact, originals, refs, trips)
    assert result["violations"]["shared_delay_corrected_time_regression"] == 1


@pytest.mark.parametrize(
    "mutation,code",
    [
        (
            lambda w: w["delay_process"][1].update(corrected_t=0),
            "delay_corrected_timestamp_pair_changed",
        ),
        (
            lambda w: w["delay_process"][1].update(t=0),
            "delay_process_source_time_pair_changed",
        ),
        (
            lambda w: w["delay_process"][1].update(supporting_window_ids=["foreign"]),
            "delay_support_window_pair_changed",
        ),
        (
            lambda w: w["delay_process"][1].update(schedule_aligned=False),
            "delay_process_alignment_membership_changed",
        ),
        (
            lambda w: w["fragments"][0].update(source_start=0),
            "whole_alignment_source_timestamp_pair_changed",
        ),
        (
            lambda w: w["fragments"][0].update(dtw_scored_points=2),
            "whole_alignment_not_full_source_validated",
        ),
    ],
)
def test_shared_delay_source_and_alignment_pairs_are_preserved(mutation, code):
    artifact, originals, refs, trips = fixture()
    mutation(_whole(artifact))
    result = audit_artifact(artifact, originals, refs, trips)
    assert result["violations"].get(code), result


def test_flat_corrected_time_is_monotone_and_valid_not_resorted():
    artifact, originals, refs, trips = fixture()
    for row in _whole(artifact)["delay_process"]:
        row.update(delay_s=row["t"] - _normal(T), corrected_t=_normal(T))
    _refresh_fit(_whole(artifact))
    result = audit_artifact(artifact, originals, refs, trips)
    assert result["validation_ok"], result


def test_whole_alignment_cannot_hide_off_reference_middle_row():
    artifact, originals, refs, trips = fixture()
    artifact["projection"]["points"][1]["projection_status"] = "OFF_REFERENCE"
    whole = _whole(artifact)
    row = whole["delay_process"][1]
    row.update(
        delay_s=None,
        corrected_t=None,
        fragment_id=None,
        status="EXCLUDED_PROJECTION",
        supporting_window_ids=[],
        schedule_aligned=False,
    )
    fragment = whole["fragments"][0]
    fragment.update(
        source_ids=["avl|v0", "avl|v2"],
        sampled_source_ids=["avl|v0", "avl|v2"],
        original_points=2,
        dtw_scored_points=2,
    )
    result = audit_artifact(artifact, originals, refs, trips)
    assert result["violations"]["whole_alignment_crosses_source_break"] == 1


@pytest.mark.parametrize("separator", ["scope", "quality", "fragment", "gap"])
def test_whole_alignment_must_respect_other_source_separators(separator):
    artifact, originals, refs, trips = fixture()
    point = artifact["projection"]["points"][1]
    if separator == "scope":
        point["scope_separator_before"] = True
    elif separator == "quality":
        point["quality"] = 0.1
    elif separator == "fragment":
        point["fragment_id"] = "another"
    else:
        artifact["config"]["score_max_gap_s"] = 10
    result = audit_artifact(artifact, originals, refs, trips)
    assert result["violations"]["whole_alignment_crosses_source_break"] == 1


def test_scheduled_trajectory_cannot_shift_its_timestamp_pairs():
    artifact, originals, refs, trips = fixture()
    artifact["result"]["all_candidates"][0]["scheduled_trajectory"][0]["t"] += 60
    result = audit_artifact(artifact, originals, refs, trips)
    assert result["violations"]["candidate_scheduled_timestamp_pairs_changed"]


def test_new_delay_fit_schema_cannot_silently_disappear():
    artifact, originals, refs, trips = fixture()
    _whole(artifact).pop("delay_fit_fragments")
    result = audit_artifact(artifact, originals, refs, trips)
    assert not result["checks_complete"]
    assert result["schema_warnings"]["unknown_or_missing_schema"]


@pytest.mark.parametrize(
    "mutation,code",
    [
        (
            lambda w: w["delay_fit_fragments"][0]["knots"][1].update(corrected_t=0),
            "delay_fit_knot_clock_not_monotone",
        ),
        (
            lambda w: w["delay_fit_fragments"][0]["knots"][0].update(raw_delay_s=0),
            "delay_fit_knot_timestamp_pair_changed",
        ),
        (
            lambda w: w["delay_fit_fragments"][0]["source_ids"].pop(),
            "delay_fit_source_conservation_failed",
        ),
        (
            lambda w: w["delay_process"][1].update(
                delay_s=9, corrected_t=_normal(T) + 11
            ),
            "delay_process_not_paired_to_fitted_clock",
        ),
    ],
)
def test_fitted_clock_knots_and_source_pairs_are_audited(mutation, code):
    artifact, originals, refs, trips = fixture()
    mutation(_whole(artifact))
    result = audit_artifact(artifact, originals, refs, trips)
    assert result["violations"].get(code), result


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if str(path).endswith(".gz"):
        with gzip.open(path, "wt") as stream:
            json.dump(value, stream)
    else:
        path.write_text(json.dumps(value))


def completed(tmp_path):
    artifact, originals, refs, trips = fixture()
    parent, run = tmp_path / "parent", tmp_path / "run"
    parent.mkdir()
    run.mkdir()
    selection = [dict(vehicle_case="vehicle-v", raw_vehicle="v")]
    source = pl.DataFrame(originals, infer_schema_length=None)
    for folder in [parent, run]:
        (folder / "observations").mkdir()
        source.write_parquet(folder / "observations/vehicle-v.parquet")
        _write(folder / "selection.json", selection)
    for name in ("manifest", "config", "summary", "completion"):
        _write(parent / f"{name}.json", {})
    boundaries = dict(
        segments=[
            dict(
                id=CASE,
                start=T.isoformat(),
                end=(T + timedelta(seconds=60)).isoformat(),
            )
        ]
    )
    _write(parent / "direction/vehicle-v.json", boundaries)
    parent_hashes = {
        p.relative_to(parent).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in parent.rglob("*")
        if p.is_file()
    }
    manifest = dict(
        representation="gtfs_trip_candidates",
        debug_date="2023-11-06",
        direction_parent_path=str(parent),
        parent_sha256=parent_hashes,
        parent_observations_sha256={},
        gtfs_feed_version="feed",
        route_id="route",
        case_artifact_suffix=".json.gz",
        code_sha256={"scoring.py": "1" * 64},
    )
    _write(run / "manifest.json", manifest)
    _write(run / "config.json", dict(gtfs=artifact["config"]))
    _write(run / "summary.json", {})
    _write(run / "gtfs/feed.json", {})
    _write(run / "gtfs/canonical.json", dict(references=refs, trips=trips))
    _write(run / "scope/vehicle-v.json", {})
    _write(
        run / "catalog/vehicle-v.json",
        [
            dict(
                id=CASE,
                start=_normal(T),
                end=_normal(T) + 60,
                source_rids=[dict(source=r["source"], rid=r["rid"]) for r in originals],
            )
        ],
    )
    # Parquet has a union schema. Keep the corresponding null canonical fields
    # in persisted projection rows as the real runner does.
    merged = {f"{r['source']}|{r['rid']}": r for r in source.to_dicts()}
    for row in artifact["projection"]["points"] + artifact["apc_evidence"]:
        for key, value in merged[f"{row['source']}|{row['rid']}"].items():
            row.setdefault(key, _normal(value))
    artifact["projection"]["apc_evidence"] = copy.deepcopy(artifact["apc_evidence"])
    relative = "cases/" + hashlib.sha256(CASE.encode()).hexdigest()[:24] + ".json.gz"
    _write(run / relative, artifact)
    outputs = {
        p.relative_to(run).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in run.rglob("*")
        if p.is_file()
    }
    _write(run / "output_sha256.json", outputs)
    _write(run / "run.json", dict(status="complete"))
    _write(run / "completion.json", dict(validation_ok=True))
    return run, parent


def test_completed_gzip_run_hashes_and_read_only(tmp_path):
    run, parent = completed(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = audit_run(run)
    assert result["validation_ok"], result
    assert result["examined"]["unique_avl"] == 3
    assert (
        len(result["audit_script_sha256"]) == len(result["run_manifest_sha256"]) == 64
    )
    assert before == {
        str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    _write(parent / "direction/vehicle-v.json", dict(segments=[]))
    assert audit_run(run)["violations"]["parent_hash_changed"] == 1


def test_unfinished_run_refused(tmp_path):
    run, _ = completed(tmp_path)
    _write(run / "run.json", dict(status="running"))
    with pytest.raises(ValueError, match="completed"):
        audit_run(run)


def test_changed_output_hash_and_missing_inventory_pin_detected(tmp_path):
    run, _ = completed(tmp_path)
    _write(run / "summary.json", dict(changed=True))
    result = audit_run(run)
    assert result["violations"]["output_hash_changed"] == 1
    inventory = json.loads((run / "output_sha256.json").read_text())
    del inventory["catalog/vehicle-v.json"]
    _write(run / "output_sha256.json", inventory)
    assert audit_run(run)["violations"]["required_output_hash_missing"] == 1


def _repin(run, relative):
    inventory = json.loads((run / "output_sha256.json").read_text())
    inventory[relative] = hashlib.sha256((run / relative).read_bytes()).hexdigest()
    _write(run / "output_sha256.json", inventory)


def replayed(tmp_path):
    source, _ = completed(tmp_path)
    replay = tmp_path / "replay"
    shutil.copytree(source, replay)
    manifest = json.loads((source / "manifest.json").read_text())
    pinned = json.loads((source / "output_sha256.json").read_text())
    pinned.update(
        {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in ("output_sha256.json", "completion.json", "run.json")
        }
    )
    manifest.update(
        decision_replay=True,
        parent_run_id=source.name,
        decision_source_run_id=source.name,
        decision_source_path=str(source),
        decision_source_sha256=pinned,
        decision_code_sha256={"decision.py": "2" * 64, "redecide.py": "3" * 64},
        stage_provenance=dict(
            upstream_code_sha256=manifest["code_sha256"],
            upstream_evidence_run_id=source.name,
            recomputed_stages=["final_decision"],
            heavy_scores_recomputed=False,
        ),
    )
    _write(replay / "manifest.json", manifest)
    _repin(replay, "manifest.json")
    relative = "cases/" + hashlib.sha256(CASE.encode()).hexdigest()[:24] + ".json.gz"
    with gzip.open(replay / relative, "rt") as stream:
        artifact = json.load(stream)
    artifact["result"]["status"] = "AMBIGUOUS"
    _write(replay / relative, artifact)
    _repin(replay, relative)
    return source, replay, relative


def test_replay_final_status_change_keeps_heavy_evidence_read_only(tmp_path):
    _, run, _ = replayed(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = audit_run(run)
    assert result["validation_ok"], result
    assert result["decision_replay"] is True
    assert result["examined"]["replay_cases_heavy_equality"] == 1
    assert result["distributions"]["replay_decision_transitions"] == {
        "MATCHED -> AMBIGUOUS": 1
    }
    assert before == {
        str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize(
    "field", ["all_candidates", "local_scores", "windows", "retrieval", "whole_trip"]
)
def test_replay_cannot_change_heavy_result_even_with_fresh_output_hash(tmp_path, field):
    _, run, relative = replayed(tmp_path)
    with gzip.open(run / relative, "rt") as stream:
        artifact = json.load(stream)
    if field == "all_candidates":
        artifact["result"][field][0]["score"] = 999
    else:
        artifact["result"][field]["replayed_mutation"] = []
    _write(run / relative, artifact)
    _repin(run, relative)
    result = audit_run(run)
    assert result["violations"]["replay_heavy_scoring_evidence_changed"] == 1


def test_replay_cannot_change_projection_payload(tmp_path):
    _, run, relative = replayed(tmp_path)
    with gzip.open(run / relative, "rt") as stream:
        artifact = json.load(stream)
    artifact["projection"]["diagnostic_changed"] = True
    _write(run / relative, artifact)
    _repin(run, relative)
    result = audit_run(run)
    assert result["violations"]["replay_case_source_or_projection_changed"] == 1


def test_replay_source_hash_change_is_detected(tmp_path):
    source, run, _ = replayed(tmp_path)
    _write(source / "summary.json", dict(changed=True))
    result = audit_run(run)
    assert result["violations"]["decision_source_hash_changed"] == 1


@pytest.mark.parametrize(
    "field", ["code_sha256", "stage_provenance", "decision_source_sha256"]
)
def test_replay_must_keep_upstream_execution_and_hash_inventory(tmp_path, field):
    _, run, _ = replayed(tmp_path)
    manifest = json.loads((run / "manifest.json").read_text())
    manifest[field] = {}
    _write(run / "manifest.json", manifest)
    _repin(run, "manifest.json")
    result = audit_run(run)
    code = (
        "decision_source_hash_inventory_changed"
        if field == "decision_source_sha256"
        else "decision_stage_provenance_changed"
    )
    assert result["violations"][code] == 1


def test_replay_copied_gtfs_must_not_change_even_with_fresh_hash(tmp_path):
    _, run, _ = replayed(tmp_path)
    _write(run / "gtfs/feed.json", dict(changed=True))
    _repin(run, "gtfs/feed.json")
    result = audit_run(run)
    assert result["violations"]["replay_copied_evidence_hash_changed"] == 1
