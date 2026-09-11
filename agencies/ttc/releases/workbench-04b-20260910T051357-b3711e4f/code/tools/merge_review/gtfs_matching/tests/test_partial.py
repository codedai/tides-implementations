"""Decision-scope experiments, not causal or operational ground-truth claims."""

import copy
import math

import pytest

from tools.merge_review.gtfs_matching import partial
from tools.merge_review.gtfs_matching.tests.test_matching_evidence import points, trip
from tools.merge_review.gtfs_matching.windows import observation_key


def bundle(rows=None, apc=None):
    return {"points": points(37) if rows is None else rows, "apc_evidence": apc or []}


def canonical():
    return {"references": [], "trips": [trip()], "route_id": "29"}


def apc_row(identifier="stop", start=0, end=400):
    return {
        "source": "apc",
        "rid": identifier,
        "source_rid": identifier,
        "t": start,
        "arrival": start,
        "departure": end,
        "interval_start": start,
        "interval_end": end,
        "interval_valid": True,
        "projection_status": "STOP_INTERVAL_ONLY",
        "trajectory_fabricated": False,
        "boarding": 4,
        "alighting": 2,
        "canonical_stop_intervals": [],
    }


def test_thirds_use_distance_not_time_and_snap_without_synthetic_points():
    rows = points(10)
    for row, t in zip(rows, [0, 10, 20, 30, 40, 80, 120, 160, 200, 240], strict=True):
        row["t"] = t
    result = partial.partition_projection(bundle(rows))
    assert [b["t"] for b in result["early"]["selection"]["boundaries"]] == [30, 120]
    assert result["early"]["selection"]["total_effective_progress_m"] == 1350
    owners = [
        key
        for mode in partial.MODES[1:]
        for key in result[mode]["selection"]["selected_avl_ids"]
    ]
    assert len(owners) == len(set(owners)) == len(rows)
    assert result["early"]["selection"]["selected_avl_ids"] == [
        "avl|a0",
        "avl|a1",
        "avl|a2",
    ]
    assert result["middle"]["selection"]["selected_avl_ids"] == [
        "avl|a3",
        "avl|a4",
        "avl|a5",
    ]
    assert result["late"]["selection"]["selected_avl_ids"] == [
        "avl|a6",
        "avl|a7",
        "avl|a8",
        "avl|a9",
    ]
    assert (
        sum(
            r["selection"]["effective_progress_m"]
            for m, r in result.items()
            if m != "global"
        )
        < 1350
    )
    assert all(not r["selection"]["online_causal_decision"] for r in result.values())


def test_off_reference_excludes_unknown_distance_but_stays_in_denominator():
    rows = points(10)
    rows[2].update(projection_status="OFF_REFERENCE", s_m=None, quality=0)
    result = partial.partition_projection(bundle(rows))
    # The two edges into/out of a rejected observation cannot add known travel.
    assert result["global"]["selection"]["total_effective_progress_m"] == 1050
    early = result["early"]
    assert "avl|a2" in early["selection"]["selected_avl_ids"]
    assert "avl|a2" not in early["selection"]["selected_valid_avl_ids"]
    assert early["projection"]["points"][2]["projection_status"] == "OFF_REFERENCE"
    assert early["selection"]["invalid_projection_retained_in_denominator"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"reference_id": "other", "s_m": 10000}, "REFERENCE_CHANGE"),
        ({"fragment_id": "f2", "s_m": 10000}, "FRAGMENT_CHANGE"),
        ({"scope_separator_before": True, "s_m": 10000}, "EXPLICIT_SEPARATOR"),
        ({"sequence_restart": True, "s_m": 10000}, "EXPLICIT_SEPARATOR"),
        ({"t": 500, "s_m": 10000}, "TIME_GAP_OR_NONINCREASING_TIME"),
    ],
)
def test_no_distance_across_reference_fragment_gap_or_explicit_boundary(change, reason):
    rows = points(2)
    rows[1].update(change)
    result = partial.partition_projection(bundle(rows))
    selection = result["global"]["selection"]
    assert selection["total_effective_progress_m"] == 0
    assert selection["excluded_length_edges"][reason] == 1


def test_small_backtracking_does_not_count_recovery_twice():
    rows = points(5)
    for row, progress in zip(rows, [0, 100, 90, 100, 200], strict=True):
        row["s_m"] = progress
    result = partial.partition_projection(bundle(rows))
    assert result["global"]["selection"]["total_effective_progress_m"] == 200


def test_duplicate_timestamp_boundary_has_one_owner():
    rows = points(10)
    rows[4]["t"] = rows[3]["t"]
    result = partial.partition_projection(bundle(rows))
    memberships = {
        key: mode
        for mode in partial.MODES[1:]
        for key in result[mode]["selection"]["selected_avl_ids"]
    }
    assert memberships["avl|a3"] == memberships["avl|a4"]
    assert len(memberships) == len(rows)


def test_apc_interval_clipping_preserves_raw_and_never_fabricates_trajectory():
    projection = bundle(points(10), [apc_row()])
    before = copy.deepcopy(projection)
    result = partial.partition_projection(projection)
    early, middle, late = [
        result[mode]["projection"]["apc_evidence"][0] for mode in partial.MODES[1:]
    ]
    assert (early["interval_start"], early["interval_end"]) == (0, 45)
    assert (middle["interval_start"], middle["interval_end"]) == (45, 90)
    assert (late["interval_start"], late["interval_end"]) == (90, 400)
    assert all(row["t"] == row["interval_start"] for row in [early, middle, late])
    assert all(
        row["boarding"] == 4 and not row["trajectory_fabricated"]
        for row in [early, middle, late]
    )
    assert math.isclose(
        sum(
            row["decision_interval_clip"]["overlap_fraction"]
            for row in [early, middle, late]
        ),
        1,
    )
    assert projection == before


def test_zero_duration_apc_boundary_belongs_only_to_later_third():
    result = partial.partition_projection(
        bundle(points(10), [apc_row(start=45, end=45)])
    )
    assert result["early"]["selection"]["selected_apc_ids"] == []
    assert result["middle"]["selection"]["selected_apc_ids"] == ["apc|stop"]
    assert result["late"]["selection"]["selected_apc_ids"] == []


def test_invalid_apc_interval_cannot_leak_outside_scope():
    row = apc_row(start=20, end=200)
    row.update(interval_start=200, interval_end=20, interval_valid=False)
    result = partial.partition_projection(bundle(points(10), [row]))
    evidence = result["early"]["projection"]["apc_evidence"][0]
    assert evidence["interval_start"] == evidence["interval_end"] == 20
    assert (
        evidence["decision_interval_clip"]["reason"]
        == "INVALID_INTERVAL_FINITE_ANCHOR_ONLY"
    )
    assert not result["middle"]["projection"]["apc_evidence"]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        points(1),
        [dict(row, s_m=0) for row in points(10)],
        [dict(row, projection_status="OFF_REFERENCE") for row in points(10)],
    ],
)
def test_no_travel_never_borrows_global_or_apc_evidence(rows):
    projection = bundle(rows, [apc_row()])
    sentinel = {"status": "PATTERN_ONLY", "candidates": [], "all_candidates": []}
    result = partial.evaluate_matching_modes(
        projection, canonical(), global_result=sentinel
    )
    assert result["global"]["result"] is sentinel
    for mode in partial.MODES[1:]:
        assert result[mode]["selection"]["status"] == "NO_EVIDENCE"
        assert (
            result[mode]["selection"]["reason"] == "NO_POSITIVE_VALID_PROJECTED_TRAVEL"
        )
        assert result[mode]["result"]["evidence_status"] == "NO_EVIDENCE"
        assert result[mode]["result"]["candidates"] == []
        assert result[mode]["result"]["retrieval"]["candidate_ids"] == []
    assert partial.validate_matching_modes(projection, result)["status"] == "PASS"


def test_sparse_long_edge_can_leave_empty_third_without_enlarging_it():
    result = partial.partition_projection(bundle(points(2)))
    assert result["middle"]["selection"]["selected_avl_ids"] == []
    assert result["middle"]["selection"]["status"] == "NO_EVIDENCE"


def test_untimed_avl_explicitly_unassigned_and_global_keeps_it():
    projection = bundle(
        points(10) + [dict(points(1)[0], rid="untimed", source_rid="untimed", t=None)]
    )
    result = partial.partition_projection(projection)
    assert "avl|untimed" in result["global"]["selection"]["selected_avl_ids"]
    assert all(
        part["selection"]["unassigned_untimed_avl_ids"] == ["avl|untimed"]
        for mode, part in result.items()
        if mode != "global"
    )
    assert all(
        "avl|untimed" not in part["selection"]["selected_avl_ids"]
        for mode, part in result.items()
        if mode != "global"
    )


def test_duplicate_source_identity_rejected():
    with pytest.raises(ValueError, match="Duplicate AVL"):
        partial.partition_projection(bundle(points(2) + points(1)))


def test_independent_matcher_receives_all_gtfs_trips_and_only_selected_rows(
    monkeypatch,
):
    projection = bundle(apc=[apc_row()])
    before = copy.deepcopy(projection)
    sentinel = {
        "status": "UNMATCHED",
        "candidates": [],
        "all_candidates": [],
        "retrieval": {"candidate_ids": []},
    }
    calls = []
    canonical_data = canonical()

    def matcher(selected, apc, refs, trips, config, **kwargs):
        calls.append((copy.deepcopy(selected), copy.deepcopy(apc), trips))
        return {"status": "UNMATCHED", "candidates": [], "all_candidates": []}

    monkeypatch.setattr(partial, "match_candidate_segment", matcher)
    result = partial.evaluate_matching_modes(
        projection, canonical_data, global_result=sentinel
    )
    assert len(calls) == 3
    assert result["global"]["result"] is sentinel
    for mode, (selected, selected_apc, supplied_trips) in zip(
        partial.MODES[1:], calls, strict=True
    ):
        assert supplied_trips is canonical_data["trips"]
        assert [observation_key(row) for row in selected["points"]] == result[mode][
            "selection"
        ]["selected_avl_ids"]
        assert [observation_key(row) for row in selected_apc] == result[mode][
            "selection"
        ]["selected_apc_ids"]
        assert not result[mode]["result"]["diagnostics"]["global_candidates_reused"]
        assert not result[mode]["result"]["diagnostics"]["global_delay_reused"]
    assert projection == before
    assert partial.validate_matching_modes(projection, result)["status"] == "PASS"


def test_pipeline_reretrieves_and_keeps_alternative_reference():
    rows = points(37, reference="display")
    for row in rows:
        row["projection_alternatives"] = [
            {
                "reference_id": "r",
                "s_m": row["s_m"],
                "projection_status": "ON_REFERENCE",
                "quality": 1,
                "family_id": "f",
            }
        ]
    projection = bundle(rows)
    sentinel = {
        "status": "UNMATCHED",
        "candidates": [],
        "all_candidates": [],
        "retrieval": {"candidate_ids": []},
    }
    config = {"window_context_s": [300], "score_max_points": 30}
    result = partial.evaluate_matching_modes(projection, canonical(), config, sentinel)
    assert result["global"]["result"] is sentinel
    for mode in partial.MODES[1:]:
        match = result[mode]["result"]
        assert match["evidence_status"] == "EVIDENCE"
        assert match["retrieval"]["candidate_ids"] == ["f|2023-11-06|T"]
        assert match["all_candidates"][0]["reference_id"] == "r"
        allowed = set(result[mode]["selection"]["selected_avl_ids"])
        assert all(
            set(window["observation_ids"]) <= allowed
            for window in match["windows"]["windows"]
        )
        assert all(
            row["source_id"] in allowed
            for row in match["all_candidates"][0]["whole_alignment"]["delay_process"]
        )
    assert (
        partial.validate_matching_modes(projection, result, config)["status"] == "PASS"
    )


def test_validation_rejects_scope_leak_and_fake_probabilities(monkeypatch):
    monkeypatch.setattr(
        partial,
        "match_candidate_segment",
        lambda *a, **k: {"status": "UNMATCHED", "candidates": [], "all_candidates": []},
    )
    projection = bundle()
    result = partial.evaluate_matching_modes(projection, canonical())
    result["early"]["result"]["windows"] = {
        "windows": [{"observation_ids": ["avl|a36"]}]
    }
    with pytest.raises(ValueError, match="Out-of-scope"):
        partial.validate_matching_modes(projection, result)
    result["early"]["result"].pop("windows")
    result["late"]["result"]["all_candidates"] = [{"probability": 0.9}]
    with pytest.raises(ValueError, match="Uncalibrated probability"):
        partial.validate_matching_modes(projection, result)
