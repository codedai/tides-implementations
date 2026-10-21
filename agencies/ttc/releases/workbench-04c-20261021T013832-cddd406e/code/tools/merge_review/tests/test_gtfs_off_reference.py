"""Rejected observations remain inspectable; classifications stay hypotheses."""

from tools.merge_review.gtfs_matching.off_reference import (
    build_fragments,
    classify_off_reference,
)


def point(rid, t, status="ON_REFERENCE", **kwargs):
    return dict(
        rid=rid,
        source_rid=rid,
        source="avl",
        t=t,
        lat=43.65,
        lon=-79.4,
        reference_id="r",
        s_m=t,
        family_id="f",
        projection_status=status,
        **kwargs,
    )


def test_sustained_off_reference_splits_even_when_it_rejoins():
    rows = [
        point("a", 0),
        point("b", 30, "OFF_REFERENCE"),
        point("c", 120, "OFF_REFERENCE"),
        point("d", 150),
    ]
    runs = classify_off_reference(rows)
    assert runs[0]["classification"] == "DETOUR_CANDIDATE"
    assert runs[0]["hypothesis_only"] is True
    assert len(build_fragments(rows, runs)) == 2


def test_explicit_deadhead_and_boundary_are_not_confused_with_gps_outlier():
    rows = [point("a", 0, "OFF_REFERENCE", route="PI"), point("b", 30)]
    assert classify_off_reference(rows)[0]["classification"] == "DEADHEAD_CANDIDATE"
    rows[0]["route"] = "29"
    assert classify_off_reference(rows)[0]["classification"] == "BOUNDARY_CANDIDATE"


def test_missing_coordinates_not_mislabeled_off_reference():
    rows = [point("a", 0), point("b", 30, "UNPROJECTABLE"), point("c", 60)]
    result = classify_off_reference(rows)
    assert result[0]["classification"] == "UNRESOLVED"
    assert len(build_fragments(rows, result)) == 2


def test_original_rejected_rids_are_all_retained_in_run():
    rows = [point("a", 0, "OFF_REFERENCE"), point("b", 10, "SEQUENCE_REJECTED")]
    run = classify_off_reference(rows)[0]
    assert run["source_rids"] == [
        {"source": "avl", "rid": "a"},
        {"source": "avl", "rid": "b"},
    ]
    assert build_fragments(rows, [run]) == []
