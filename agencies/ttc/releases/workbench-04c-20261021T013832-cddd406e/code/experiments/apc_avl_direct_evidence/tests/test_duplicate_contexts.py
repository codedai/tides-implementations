"""Corruption/negative controls for measurement-alias context replay."""

from datetime import datetime

from experiments.apc_avl_direct_evidence.validation.duplicate_contexts import (
    audit_vehicle,
)


def case(kind="point"):
    row = {
        "rid": "p",
        "time": datetime(2023, 11, 8, 10),
        "lat": 43.65,
        "lon": -79.4,
    }
    avl = [row, dict(row, rid="alias")]
    result = {
        "apc_rid": "a",
        "status": "direct",
        "support_level": "instant",
        "segment_ids": ["s"],
        "context_status": "single_segment",
        "witnesses": [{"kind": kind, "ping_ids": ["p"], "target": "arrival"}],
    }
    return avl, [result], {"p": "s", "alias": "t"}


def test_representative_does_not_hide_split_context_alias_in_audit():
    avl, results, mapping = case()
    metrics, affected = audit_vehicle(avl, results, mapping)
    assert metrics["accepted_assignments_with_split_context_aliases"] == 1
    assert metrics["accepted_declared_alias_owner_set_violations"] == 1
    assert metrics["accepted_declared_alias_context_status_violations"] == 1
    assert affected[0]["problematic_witnesses"][0]["aliases"][0][
        "all_alias_segment_ids"
    ] == ["s", "t"]
    results[0].update(segment_ids=["s", "t"], context_status="multiple_segments")
    repaired, _ = audit_vehicle(avl, results, mapping)
    assert not any(
        value for key, value in repaired.items() if key.endswith("_violations")
    )


def test_identical_alias_context_is_not_a_false_alarm():
    avl, results, mapping = case()
    mapping["alias"] = "s"
    metrics, affected = audit_vehicle(avl, results, mapping)
    assert metrics["eligible_duplicated_measurements"] == 1
    assert not affected


def test_interpolation_is_counted_separately():
    metrics, _ = audit_vehicle(*case("interpolation"))
    assert metrics["accepted_interpolations_with_split_context_aliases"] == 1
    assert metrics["accepted_interpolation_alias_owner_violations"] == 1


def test_quarantined_dst_measurements_do_not_inflate_eligible_alias_count():
    avl, results, mapping = case()
    avl[1]["time_ambiguous"] = True
    metrics, affected = audit_vehicle(avl, results, mapping)
    assert not metrics.get("eligible_duplicated_measurements")
    assert not affected


def test_unaccepted_candidates_do_not_count_as_accepted_witnesses():
    avl, results, mapping = case()
    results[0]["status"] = "ambiguous"
    metrics, affected = audit_vehicle(avl, results, mapping)
    assert metrics["duplicated_measurements_with_multiple_frozen_contexts"] == 1
    assert not metrics.get("accepted_assignments_examined")
    assert not affected
