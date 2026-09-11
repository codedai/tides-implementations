"""Independent persisted direction fixtures; no segmentation/matcher imports."""

import copy
import json
import os
import time
from datetime import timedelta

import polars as pl
import pytest

from tools.merge_review.data import DIRECTION_METHOD, ReviewData, wall_seconds
from tools.merge_review.tests.test_data import (
    ORIGIN,
    configure_dedup,
    refresh_fields_hash,
    write_rows,
)
from tools.merge_review.tests.test_data import (
    run as legacy_run_fixture,
)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, default=str))


def key(source, rid):
    return {"source": source, "rid": rid}


@pytest.fixture
def direction_run(tmp_path):
    run = legacy_run_fixture.__wrapped__(tmp_path)
    configure_dedup(run)
    audit = run.parent / "audit"
    avl = pl.read_parquet(audit / "avl.parquet").to_dicts()
    apc = pl.read_parquet(audit / "apc.parquet").to_dicts()
    for row in avl:
        row["destination"] = (
            "SOUTH - test"
            if row["rid"] == "b1"
            else "#N/A"
            if row["rid"] == "outside"
            else "NORTH - test"
        )
        if row["rid"] in {"a0", "b0"}:
            row["route"] = "PI" if row["rid"] == "a0" else " po "
    for row in apc:
        row["direction"] = (
            None
            if row["rid"] == "p6"
            else "SOUTH"
            if row["rid"] in {"p2", "p4"}
            else "NORTH"
        )
        row.update(route_name="DUFFERIN", branch="29Dcon")
        if row["rid"] == "p2":
            row.update(route="PI", branch="PO")  # APC must never be PI/PO-filtered.
        if row["rid"] == "p4":
            row["departure"] = ORIGIN + timedelta(seconds=160)
        if row["rid"] == "p5":
            row["arrival"] = ORIGIN + timedelta(seconds=1000)
            row["departure"] = ORIGIN + timedelta(seconds=1005)
    write_rows(audit / "avl.parquet", avl)
    write_rows(audit / "apc.parquet", apc)
    raw_path = run / "deduplication" / "raw_avl_fields.parquet"
    raw = pl.read_parquet(raw_path).to_dicts()
    originals = {row["rid"]: row for row in avl}
    for row in raw:
        original = originals[row["source_rid"]]
        row.update(Destination=original["destination"], Route=original["route"])
    write_rows(raw_path, raw)
    refresh_fields_hash(run)
    manifest = json.loads((run / "manifest.json").read_text())
    manifest.update(
        methods=[DIRECTION_METHOD],
        representation="direction_candidate_segments",
        trip_segmentation_used=True,
        observation_matching_used=False,
        filtering={
            "field": "canonical AVL route",
            "normalization": "strip+upper",
            "excluded": ["PI", "PO"],
        },
    )
    save_json(run / "manifest.json", manifest)
    excluded = [
        dict(
            row,
            filter_reason="avl_route_pi_po",
            filter_normalized_route=row["route"].strip().upper(),
        )
        for row in avl
        if row["rid"] in {"a0", "b0"}
    ]
    focus = {
        "north": [key("avl", rid) for rid in ("a1", "a2", "format-a1")]
        + [key("apc", rid) for rid in ("p1", "p2", "p3", "p4", "p7")],
        "south": [key("avl", "b1"), key("apc", "p4")],
        "apc-tail": [key("apc", "p5")],
        "unknown": [key("avl", "outside")],
    }
    specifications = [
        ("north", "NORTH", 0, 150),
        ("south", "SOUTH", 150, 200),
        ("apc-tail", "NORTH", 900, 1100),
        ("unknown", None, 1100, 39600),
    ]
    segments = [
        {
            "id": name,
            "direction": direction,
            "start": ORIGIN + timedelta(seconds=start),
            "end": ORIGIN + timedelta(seconds=end),
            "first_definite_at": ORIGIN + timedelta(seconds=start)
            if direction
            else None,
            "boundary_reason": "direction_change"
            if name != "north"
            else "day_scope_start",
            "boundary_phase": "right_limit",
            "boundary_evidence": focus[name][:1],
            "source_rids": focus[name],
            "conflict_ids": ["c1"] if name in {"north", "south"} else [],
            "unknown_duration_s": 0,
            "conflict_duration_s": 0,
            "candidate_only": True,
            "complete_trip": False,
        }
        for name, direction, start, end in specifications
    ]
    conflict = {
        "id": "c1",
        "start": ORIGIN + timedelta(seconds=150),
        "end": ORIGIN + timedelta(seconds=150),
        "kind": "point",
        "start_inclusive": True,
        "end_inclusive": True,
        "types": ["avl_same_time_opposite"],
        "source_rids": [key("avl", "a2"), key("avl", "b1")],
        "north_source_rids": [key("avl", "a2")],
        "south_source_rids": [key("avl", "b1")],
    }
    document = {
        "segments": segments,
        "conflicts": [conflict],
        "filtered_avl": excluded,
        "evidence": [],
        "policy": {"candidate_only": True},
    }
    save_json(run / "direction" / "V-test.json", document)
    observations = []
    for source, rows in (("avl", avl), ("apc", apc)):
        for original in rows:
            if source == "avl" and original["rid"] in {"copy-a1", "a0", "b0"}:
                continue
            row = copy.deepcopy(original)
            instant = (
                row["time"]
                if source == "avl"
                else row.get("arrival") or row.get("departure")
            )
            label = (
                row["destination"].split()[0] if source == "avl" else row["direction"]
            )
            label = label if label in {"NORTH", "SOUTH"} else None
            memberships = [
                name for name, ids in focus.items() if key(source, row["rid"]) in ids
            ]
            row.update(
                source=source,
                source_rid=row["rid"],
                event_time=instant,
                event_time_basis="missing"
                if instant is None
                else "avl.time"
                if source == "avl"
                else "apc.arrival",
                direction_label=label,
                direction_basis="avl.destination_first_word"
                if source == "avl"
                else "apc.direction",
                direction_interval_kind="missing_time"
                if instant is None
                else "avl_last_point"
                if source == "avl"
                else "apc_interval",
                direction_evidence_start=instant,
                direction_evidence_end=row.get("departure")
                if source == "apc"
                else instant,
                direction_end_inclusive=True,
                direction_evidence_eligible=bool(label and instant),
                direction_exclusion_reasons=[]
                if label and instant
                else ["unknown_or_missing"],
                direction_day_censored=False,
                candidate_segment_ids=memberships,
                candidate_boundary_overlap=len(memberships) > 1,
                direction_conflict_ids=["c1"] if row["rid"] in {"a2", "b1"} else [],
            )
            observations.append(row)
    observations.sort(
        key=lambda row: (
            row["event_time"] is None,
            row["event_time"] or ORIGIN,
            row["source"] != "avl",
            row.get("source_row") is None,
            row.get("source_row") or 0,
            row["rid"],
        )
    )
    for ordinal, row in enumerate(observations):
        row["display_order"] = ordinal
    write_rows(run / "observations" / "V-test.parquet", observations)
    return run


def detail(run, case_id="south", context_s=0):
    return ReviewData(run).case(
        DIRECTION_METHOD, "V-test", case_id, context_s=context_s
    )


def test_new_metadata_and_independent_filter_dedup_counts(direction_run):
    data = ReviewData(direction_run)
    metadata = data.metadata()
    assert metadata["representation"] == "direction_candidate_segments"
    assert metadata["methods"] == [DIRECTION_METHOD]
    assert (
        metadata["trip_segmentation_used"] and not metadata["observation_matching_used"]
    )
    assert metadata["segmentation_used"] and not metadata["matching_used"]
    result = detail(direction_run)
    c = result["counts"]
    assert (
        c["source_avl_total"],
        c["deduplicated_avl_total"],
        c["retained_avl_total"],
    ) == (8, 7, 5)
    assert c["removed_avl_duplicates"] == 1 and c["filtered_avl_pi_po"] == 2
    assert c["source_apc_total"] == c["retained_apc_total"] == 7
    assert len(result["deduplication"]) == 8
    assert {r["rid"] for r in result["filtered_avl"]} == {"a0", "b0"}
    assert all(
        r["deduplication"]["representative_rid"] == r["rid"]
        for r in result["filtered_avl"]
    )
    assert c["source_lineage_preserved"] and not c["source_rows_preserved"]
    assert not result["provenance"]["matcher_recomputed"]
    json.dumps(result, allow_nan=False)


def test_all_retained_sources_have_review_focus_including_unknown_and_untimed(
    direction_run,
):
    data = ReviewData(direction_run)
    catalog = data.catalog(DIRECTION_METHOD, "V-test")
    focused = set()
    for case in catalog:
        result = data.case(DIRECTION_METHOD, "V-test", case["id"], context_s=0)
        focused.update(
            (row["source"], row["rid"])
            for row in result["observations"]
            if row["in_focus"]
        )
        assert data.has_case(DIRECTION_METHOD, "V-test", case["id"])
    assert focused == {
        ("avl", r) for r in ("a1", "a2", "format-a1", "b1", "outside")
    } | {("apc", f"p{i}") for i in range(1, 8)}
    unresolved = next(row for row in catalog if row["kind"] == "direction_unresolved")
    result = data.case(DIRECTION_METHOD, "V-test", unresolved["id"], context_s=0)
    p6 = next(row for row in result["observations"] if row["rid"] == "p6")
    assert p6["in_focus"] and not p6["is_candidate_member"]
    assert p6["candidate_segment_ids"] == [] and p6["event_time"] is None
    unknown = next(row for row in catalog if row["id"] == "unknown")
    assert unknown["direction"] is None and unknown["direction_label"] == "UNKNOWN"


def test_conflict_source_outside_window_retained_as_context_not_member(direction_run):
    result = detail(direction_run)
    byid = {row["rid"]: row for row in result["observations"]}
    assert byid["a2"]["is_conflict_context"] and not byid["a2"]["in_focus"]
    assert not byid["a2"]["is_candidate_member"] and not byid["a2"]["in_window"]
    assert byid["b1"]["is_candidate_member"] and byid["b1"]["in_focus"]
    assert result["counts"]["avl_member_count"] == 1
    assert result["counts"]["context_avl_count"] == 1
    conflict = result["direction_conflicts"][0]
    assert (
        conflict["start"]
        == conflict["end"]
        == wall_seconds(ORIGIN + timedelta(seconds=150))
    )
    assert conflict["source_rids"] == [key("avl", "a2"), key("avl", "b1")]
    assert conflict["directions"] == ["NORTH", "SOUTH"]
    assert [row["display_order"] for row in result["observations"]] == sorted(
        row["display_order"] for row in result["observations"]
    )


def test_apc_candidate_overlap_not_matching_or_progress_projection(direction_run):
    result = detail(direction_run)
    p4 = next(row for row in result["apc"] if row["rid"] == "p4")
    assert p4["candidate_segment_ids"] == p4["candidate_ids"] == ["north", "south"]
    assert p4["candidate_boundary_overlap"] and p4["is_candidate_member"]
    assert p4["status"] == "not_evaluated" and p4["progress_m"] is None
    assert p4["route_name"] == "DUFFERIN" and p4["branch"] == "29Dcon"
    assert not {"score", "witnesses", "ping_ids", "segment_ids"} & p4["decision"].keys()
    assert p4["source_fields"]["departure"] == wall_seconds(
        ORIGIN + timedelta(seconds=160)
    )
    assert p4["source_fields"]["boarding"] == 2.5
    north = detail(direction_run, "north")
    p2 = next(row for row in north["apc"] if row["rid"] == "p2")
    assert p2["route"] == "PI" and p2["branch"] == "PO"


def test_apc_only_candidate_visible_without_invented_avl(direction_run):
    result = detail(direction_run, "apc-tail")
    assert result["case"]["kind"] == "direction_segment"
    assert result["case"]["avl_member_count"] == 0
    assert result["case"]["apc_rids"] == ["p5"]
    assert result["avl"] == [] and len(result["apc"]) == 1
    assert result["apc"][0]["progress_m"] is None


def test_candidate_bounds_do_not_replace_actual_member_coverage(direction_run):
    result = detail(direction_run, "unknown")
    summary = result["case"]
    assert (
        summary["source_start"]
        == summary["source_end"]
        == wall_seconds(ORIGIN + timedelta(seconds=3600))
    )
    assert summary["end"] > summary["source_end"]
    assert summary["scope_boundary_end"] and not summary["complete_trip"]
    assert any("actual member observation" in warning for warning in result["warnings"])


def test_progress_breaks_across_filtered_original_representative(direction_run):
    result = detail(direction_run)
    byid = {row["rid"]: row for row in result["avl"]}
    assert byid["b1"]["break_before"]
    assert byid["b1"]["break_reason"] == "display_or_filter_boundary"
    assert byid["b1"]["progress_m"] == 0
    assert byid["a2"]["progress_run"] != byid["b1"]["progress_run"]


def test_direction_boundary_source_outside_window_is_explicit_context(direction_run):
    path = direction_run / "direction" / "V-test.json"
    document = json.loads(path.read_text())
    segment = next(s for s in document["segments"] if s["id"] == "apc-tail")
    segment["previous_definite_at"] = str(ORIGIN + timedelta(seconds=150))
    segment["previous_direction_evidence"] = [key("avl", "b1")]
    save_json(path, document)
    result = detail(direction_run, "apc-tail")
    byid = {row["rid"]: row for row in result["observations"]}
    assert byid["b1"]["is_direction_boundary_context"]
    assert not byid["b1"]["is_conflict_context"]
    assert not byid["b1"]["in_focus"] and not byid["b1"]["in_window"]
    selected = next(s for s in result["segments"] if s["id"] == "apc-tail")
    assert selected["previous_definite_at"] == wall_seconds(
        ORIGIN + timedelta(seconds=150)
    )


def test_direction_json_clocks_independent_of_host_timezone(direction_run):
    previous = os.environ.get("TZ")
    try:
        outputs = []
        for zone in ("UTC", "America/Toronto", "Pacific/Auckland"):
            os.environ["TZ"] = zone
            time.tzset()
            result = detail(direction_run)
            outputs.append(
                (
                    result["case"]["start"],
                    result["direction_conflicts"][0]["start"],
                    result["observations"][0]["event_time"],
                )
            )
        assert outputs[0] == outputs[1] == outputs[2]
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_opaque_nested_original_fields_not_interpreted_as_timestamps(direction_run):
    path = direction_run.parent / "audit" / "avl.parquet"
    rows = pl.read_parquet(path).to_dicts()
    opaque = {"time": "2023-11-06T01:02:03", "end": "opaque text"}
    for row in rows:
        row["vendor_payload"] = opaque if row["rid"] == "a0" else None
    write_rows(path, rows)
    metadata_path = direction_run / "direction" / "V-test.json"
    metadata = json.loads(metadata_path.read_text())
    for row in metadata["filtered_avl"]:
        row["vendor_payload"] = opaque if row["rid"] == "a0" else None
    save_json(metadata_path, metadata)
    stream_path = direction_run / "observations" / "V-test.parquet"
    rows = pl.read_parquet(stream_path).to_dicts()
    for row in rows:
        row["vendor_payload"] = None
    write_rows(stream_path, rows)
    result = detail(direction_run)
    first = next(row for row in result["filtered_avl"] if row["rid"] == "a0")
    assert first["vendor_payload"] == opaque


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_filter",
        "changed_filter_payload",
        "wrong_filter_reason",
        "non_pi_filtered",
        "apc_missing",
        "duplicate_observation",
        "wrong_membership",
        "missing_conflict",
        "wrong_conflict_side",
        "direction_changed",
        "fabricated_match",
    ],
)
def test_corrupt_persisted_results_fail_closed(direction_run, corruption):
    metadata_path = direction_run / "direction" / "V-test.json"
    document = json.loads(metadata_path.read_text())
    stream_path = direction_run / "observations" / "V-test.parquet"
    observations = pl.read_parquet(stream_path).to_dicts()
    if corruption == "missing_filter":
        document["filtered_avl"].pop()
    elif corruption == "changed_filter_payload":
        document["filtered_avl"][0]["lat"] = 12.3
    elif corruption == "wrong_filter_reason":
        document["filtered_avl"][0]["filter_reason"] = "wrong"
    elif corruption == "non_pi_filtered":
        document["filtered_avl"][0]["rid"] = "a1"
    elif corruption == "apc_missing":
        observations = [row for row in observations if row["rid"] != "p5"]
    elif corruption == "duplicate_observation":
        observations.append(copy.deepcopy(observations[0]))
    elif corruption == "wrong_membership":
        next(row for row in observations if row["rid"] == "p4")[
            "candidate_segment_ids"
        ] = ["north"]
    elif corruption == "missing_conflict":
        document["conflicts"] = []
    elif corruption == "wrong_conflict_side":
        document["conflicts"][0]["north_source_rids"] = [key("avl", "b1")]
    elif corruption == "direction_changed":
        next(row for row in observations if row["rid"] == "p4")["direction_label"] = (
            "NORTH"
        )
    elif corruption == "fabricated_match":
        next(row for row in observations if row["rid"] == "p4")["score"] = 1.0
    save_json(metadata_path, document)
    write_rows(stream_path, observations)
    with pytest.raises(ValueError):
        ReviewData(direction_run).catalog(DIRECTION_METHOD, "V-test")


@pytest.mark.parametrize("keep", ["apc_only", "avl_only", "filtered_only"])
def test_single_source_and_all_filtered_vehicle_audits(direction_run, keep):
    audit = direction_run.parent / "audit"
    avl = pl.read_parquet(audit / "avl.parquet")
    apc = pl.read_parquet(audit / "apc.parquet")
    if keep == "apc_only":
        avl = avl.head(0)
    if keep == "filtered_only":
        avl = avl.filter(pl.col("rid").is_in(["a0", "b0"]))
    if keep != "apc_only":
        apc = apc.head(0)
    avl.write_parquet(audit / "avl.parquet")
    apc.write_parquet(audit / "apc.parquet")
    ids = set(avl["rid"])
    for filename in ("raw_avl_fields.parquet", "V-test.parquet"):
        path = direction_run / "deduplication" / filename
        frame = pl.read_parquet(path)
        frame.filter(pl.col("source_rid").is_in(ids)).write_parquet(path)
    refresh_fields_hash(direction_run)
    stream_path = direction_run / "observations" / "V-test.parquet"
    frame = pl.read_parquet(stream_path).filter(
        pl.col("rid").is_in(ids | set(apc["rid"]))
    )
    rows = frame.to_dicts()
    present = {(r["source"], r["rid"]) for r in rows}
    path = direction_run / "direction" / "V-test.json"
    document = json.loads(path.read_text())
    document["filtered_avl"] = [r for r in document["filtered_avl"] if r["rid"] in ids]
    document["conflicts"] = [
        c
        for c in document["conflicts"]
        if all((r["source"], r["rid"]) in present for r in c["source_rids"])
    ]
    conflicts = {c["id"] for c in document["conflicts"]}
    for segment in document["segments"]:
        for field in ("source_rids", "boundary_evidence"):
            segment[field] = [
                r for r in segment[field] if (r["source"], r["rid"]) in present
            ]
        segment["conflict_ids"] = [
            key for key in segment["conflict_ids"] if key in conflicts
        ]
    document["segments"] = [s for s in document["segments"] if s["source_rids"]]
    for ordinal, row in enumerate(rows):
        row["display_order"] = ordinal
        row["direction_conflict_ids"] = [
            key for key in row["direction_conflict_ids"] if key in conflicts
        ]
    if rows:
        write_rows(stream_path, rows)
    else:
        frame.write_parquet(stream_path)
    save_json(path, document)
    data = ReviewData(direction_run)
    cases = data.catalog(DIRECTION_METHOD, "V-test")
    assert cases
    if keep == "filtered_only":
        assert len(cases) == 1 and not cases[0]["candidate_only"]
        result = data.case(DIRECTION_METHOD, "V-test", cases[0]["id"])
        assert result["observations"] == result["segments"] == []
        assert len(result["filtered_avl"]) == len(result["deduplication"]) == 2
        assert result["counts"]["retained_avl_total"] == 0
    else:
        focused = {
            r["source"]
            for c in cases
            for r in data.case(DIRECTION_METHOD, "V-test", c["id"], context_s=0)[
                "observations"
            ]
            if r["in_focus"]
        }
        assert focused == {"apc" if keep == "apc_only" else "avl"}
