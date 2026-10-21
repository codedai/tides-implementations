"""Independent accounting/witness checks; no agency source data required."""

from copy import deepcopy
from datetime import datetime, timedelta

import polars as pl
import pytest

from experiments.apc_avl_direct_evidence.evaluate import (
    baseline_eligible,
    combine_metrics,
    load_frozen_segments,
    summarize_vehicle,
    time_coordinate,
    transition,
    validate_assignments,
)

T = datetime(2023, 11, 8, 10)
CFG = {"time_tolerance_s": 60, "radius_m": 100, "interpolation_gap_s": 90}


def fixture():
    p = {"rid": "p", "vehicle": "v", "time": T, "lat": 43.65, "lon": -79.4}
    a = {
        "rid": "a",
        "vehicle": "v",
        "arrival": T,
        "departure": T,
        "lat": 43.65,
        "lon": -79.4,
    }
    seg = {
        "segments": [{"segment_id": "s", "ping_ids": ["p"]}],
        "ping_segment": {"p": "s"},
    }
    result = {
        "apc_rid": "a",
        "status": "direct",
        "reason": "test",
        "support_level": "instant",
        "segment_id": "s",
        "segment_ids": ["s"],
        "context_status": "single_segment",
        "ping_ids": ["p"],
        "witnesses": [
            {
                "kind": "point",
                "ping_ids": ["p"],
                "support_time": time_coordinate(T),
                "target_time": time_coordinate(T),
                "target": "arrival",
                "gap_s": 0,
                "distance_m": 0,
            }
        ],
    }
    return [p], [a], seg, [result]


@pytest.mark.parametrize("field", ["invalid", "time_ambiguous", "time_nonexistent"])
def test_common_eligibility_preserves_prior_time_rules(field):
    _, apc, _, _ = fixture()
    assert baseline_eligible(apc[0])
    apc[0][field] = True
    assert not baseline_eligible(apc[0])


def test_unknown_grain_is_not_silently_removed_from_common_denominator():
    _, apc, _, _ = fixture()
    apc[0]["grain_uncertain"] = True
    assert baseline_eligible(apc[0])
    apc[0]["departure"] = T + timedelta(seconds=1801)
    assert not baseline_eligible(apc[0])


def test_null_owner_equality_is_not_a_verified_same_owner():
    assert (
        transition(
            {"status": "direct", "segment_id": None},
            {"status": "direct", "segment_id": None, "segment_ids": []},
        )
        == "direct_new_context_unmapped"
    )


def test_old_owner_inside_multiple_contexts_is_distinguished():
    assert (
        transition(
            {"status": "direct", "segment_id": "s"},
            {"status": "direct", "segment_id": None, "segment_ids": ["s", "t"]},
        )
        == "direct_old_chunk_in_multiple_contexts"
    )


def test_partial_support_is_not_counted_as_new_direct():
    assert (
        transition({"status": "direct"}, {"status": "partial"})
        == "old_direct_to_partial"
    )


def test_repeated_membership_is_not_silently_overwritten(tmp_path):
    path = tmp_path / "chunks.parquet"
    pl.from_dicts(
        [{"segment_id": "s", "ping_ids": ["p"]}, {"segment_id": "t", "ping_ids": ["p"]}]
    ).write_parquet(path)
    with pytest.raises(ValueError, match="repeated AVL membership"):
        load_frozen_segments(path)


def test_valid_instant_witness_and_accounting_pass():
    avl, apc, seg, results = fixture()
    assert not any(
        validate_assignments(avl, apc, seg, results, CFG)["violations"].values()
    )
    metrics, paired = summarize_vehicle(avl, apc, seg, deepcopy(results), results, CFG)
    assert metrics["new_common_eligible_direct"] == 1
    assert paired[0]["transition"] == "direct_retained_same_single_chunk"


@pytest.mark.parametrize(
    ("corruption", "rule"),
    [
        ("time", "point_support_time_not_observed"),
        ("vehicle", "witness_vehicle_mismatch"),
        ("source_id", "witness_missing_source_ping"),
        ("position", "witness_outside_spatial_radius"),
        ("lineage", "assignment_witness_lineage_mismatch"),
        ("scope", "bounded_interval_missing_endpoint_witness"),
    ],
)
def test_independent_checks_detect_corrupted_witnesses(corruption, rule):
    avl, apc, seg, results = fixture()
    if corruption == "time":
        results[0]["witnesses"][0]["support_time"] += 1
    elif corruption == "vehicle":
        avl[0]["vehicle"] = "other"
    elif corruption == "source_id":
        results[0]["witnesses"][0]["ping_ids"] = ["unknown"]
    elif corruption == "position":
        avl[0]["lat"] += 1
    elif corruption == "lineage":
        results[0]["ping_ids"] = ["other"]
    elif corruption == "scope":
        results[0]["support_level"] = "bounded_interval"
    assert validate_assignments(avl, apc, seg, results, CFG)["violations"][rule] == 1


def test_interpolation_cannot_bridge_frozen_contexts():
    avl, apc, seg, results = fixture()
    avl[0]["time"] = T - timedelta(seconds=10)
    avl.append(dict(avl[0], rid="q", time=T + timedelta(seconds=10)))
    seg["segments"].append({"segment_id": "t", "ping_ids": ["q"]})
    seg["ping_segment"]["q"] = "t"
    results[0]["ping_ids"] = ["p", "q"]
    results[0]["witnesses"][0].update(kind="interpolation", ping_ids=["p", "q"])
    checks = validate_assignments(avl, apc, seg, results, CFG)
    assert checks["violations"]["interpolation_crosses_frozen_chunk"] == 1


def test_common_denominator_rates_are_pooled_not_vehicle_averages():
    rows = [
        {
            "apc_rows": 1,
            "common_eligible_apc_rows": 1,
            "new_statuses": {"direct": 1},
            "new_common_eligible_direct": 1,
        },
        {
            "apc_rows": 9,
            "common_eligible_apc_rows": 9,
            "new_statuses": {"direct": 0},
            "new_common_eligible_direct": 0,
        },
    ]
    result = combine_metrics(rows)
    assert result["new_direct_rate_common_eligible"] == 0.1
    assert result["accuracy"] is None


def test_baseline_missing_a_source_row_blocks_paired_comparison():
    avl, apc, seg, results = fixture()
    with pytest.raises(ValueError, match="Frozen baseline does not conserve"):
        summarize_vehicle(avl, apc, seg, [], results, CFG)
