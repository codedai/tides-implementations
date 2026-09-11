"""Synthetic, deterministic tests for the read-only review adapter."""

import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from tools.merge_review.data import (
    METHODS,
    RAW_AVL_EXACT_FIELDS,
    ReviewData,
    _decorate_progress,
    json_safe,
    wall_seconds,
)

ORIGIN = datetime(2023, 11, 2, 13)


@pytest.mark.parametrize("value", [2**53, 2**64 - 1, -(2**53), -(2**63)])
def test_large_integer_json_encoding_keeps_exact_decimal_digits(value):
    result = json_safe({"hash": value, "nested": [value, True, 23, 2**53 - 1]})
    assert result["hash"] == result["nested"][0] == str(value)
    assert result["nested"][1:] == [True, 23, 2**53 - 1]
    assert json.loads(json.dumps(result)) == result


def ping(rid, seconds, *, lat=43.65, lon=-79.4, vehicle="9027", **fields):
    return {
        "rid": rid,
        "vehicle": vehicle,
        "time": ORIGIN + timedelta(seconds=seconds) if seconds is not None else None,
        "lat": lat,
        "lon": lon,
        "route": "29",
        "invalid": False,
        "time_ambiguous": False,
        "time_nonexistent": False,
        **fields,
    }


def apc(rid, seconds, *, vehicle="9027", **fields):
    instant = ORIGIN + timedelta(seconds=seconds) if seconds is not None else None
    return {
        "rid": rid,
        "vehicle": vehicle,
        "arrival": instant,
        "departure": instant,
        "lat": 43.65,
        "lon": -79.4,
        "route": "29",
        "direction": "N",
        "stop": "example stop",
        "boarding": 2.5,
        "alighting": 1.5,
        "arrival_load": 7.5,
        "departure_load": 8.5,
        "grain_uncertain": True,
        "invalid": False,
        **fields,
    }


def decision(rid, *, status="direct", owners=("A",), candidate_owners=(), **fields):
    return {
        "apc_rid": rid,
        "status": status,
        "reason": "FIXTURE_ONLY",
        "support_level": "instant" if status == "direct" else None,
        "segment_ids": list(owners),
        "segment_id": owners[0] if len(owners) == 1 else None,
        "context_status": "single_segment" if len(owners) == 1 else "multiple_segments",
        "candidate_owners": list(candidate_owners),
        "ping_ids": [],
        "witnesses": [],
        "counter_witnesses": [],
        "candidates": [],
        "trip_identity_verified": False,
        **fields,
    }


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.from_dicts(rows, infer_schema_length=None).write_parquet(path)


@pytest.fixture
def run(tmp_path):
    audit, baseline, result = (
        tmp_path / "audit",
        tmp_path / "baseline",
        tmp_path / "run",
    )
    result.mkdir()
    avl = [
        ping("a0", 0),
        ping("a1", 30, lat=43.6501),
        ping("a2", 60),
        ping("b0", 120),
        ping("b1", 150),
        ping("outside", 3600),
    ]
    rows = [
        apc("p1", 30),
        apc("p2", 45),
        apc("p3", 55),
        apc("p4", 70),
        apc("p5", 75),
        apc("p6", None, invalid=True),
        apc("p7", 80),
    ]
    records = [
        decision(
            "p1",
            witnesses=[
                {
                    "kind": "point",
                    "ping_ids": ["a1", "outside"],
                    "target_time": wall_seconds(ORIGIN + timedelta(seconds=30)),
                }
            ],
        ),
        decision("p2", status="unmatched", owners=()),
        decision(
            "p3",
            status="ambiguous",
            owners=(),
            candidate_owners=("A",),
            candidates=[{"segment_ids": ["A"], "ping_ids": ["a2"]}],
        ),
        decision("p4", owners=("A", "B")),
        decision("p5", owners=("B",)),
        decision("p6", status="ineligible", owners=()),
        decision("p7", owners=(), context_status="unmapped"),
    ]
    write_rows(audit / "avl.parquet", avl)
    write_rows(audit / "apc.parquet", rows)
    chunks = [
        {
            "segment_id": "A",
            "start": ORIGIN,
            "end": ORIGIN + timedelta(seconds=60),
            "ping_ids": ["a0", "a1", "a2"],
        },
        {
            "segment_id": "B",
            "start": ORIGIN + timedelta(seconds=120),
            "end": ORIGIN + timedelta(seconds=150),
            "ping_ids": ["b0", "b1"],
        },
    ]
    for method in METHODS:
        write_rows(result / "assignments" / method / "V-test.parquet", records)
        write_rows(baseline / "chunks" / method / "V-test.parquet", chunks)
    (result / "manifest.json").write_text(
        json.dumps({"audit": str(audit), "baseline": str(baseline), "gtfs_used": False})
    )
    (result / "selection.json").write_text(
        json.dumps([{"vehicle_case": "V-test", "raw_vehicle": "9027"}])
    )
    (result / "summary.json").write_text(json.dumps({"avl_context": {"apc_rows": 7}}))
    return result


def test_lazy_metadata_and_default_vehicle(run):
    data = ReviewData(run)
    metadata = data.metadata()
    assert metadata["default_vehicle"] == "V-test"
    assert metadata["default_method"] == "avl_context"
    assert metadata["methods"] == list(METHODS)
    assert "NOT UTC" in metadata["time_basis"]
    assert "not verified" in metadata["warning"]
    assert not data._cache
    metadata["vehicles"].clear()
    assert data.metadata()["vehicles"]


def test_context_is_not_accepted_and_candidates_are_distinct(run):
    data = ReviewData(run)
    result = data.case("avl_context", "V-test", "A", context_s=180)
    apcs = {r["rid"]: r for r in result["apc"]}
    assert apcs["p1"]["relation"] == "accepted"
    assert apcs["p3"]["relation"] == "candidate"
    assert apcs["p4"]["relation"] == "accepted"
    assert apcs["p4"]["decision"]["segment_ids"] == ["A", "B"]
    for rid in ("p2", "p5", "p7"):
        assert apcs[rid]["relation"] == "context"
        assert not apcs[rid]["in_focus"]
    assert result["case"]["apc_count"] == 3
    assert result["case"]["accepted_count"] == 2
    assert result["case"]["candidate_count"] == 1
    assert result["case"]["status_counts"] == {"direct": 2, "ambiguous": 1}


def test_every_apc_has_a_focus_case_including_orphan_and_untimed(run):
    data = ReviewData(run)
    catalog = data.catalog("avl_context", "V-test")
    assert {r["kind"] for r in catalog} == {"segment", "apc_only", "unresolved"}
    focused = set()
    unresolved = set()
    for summary in catalog:
        case = data.case("avl_context", "V-test", summary["id"], context_s=0)
        rows = {r["rid"] for r in case["apc"] if r["in_focus"]}
        focused.update(rows)
        if summary["kind"] == "unresolved":
            unresolved.update(rows)
    assert focused == {f"p{i}" for i in range(1, 8)}
    assert {"p3", "p4", "p6", "p7"}.issubset(unresolved)
    untimed = next(r for r in catalog if r["start"] is None)
    result = data.case("avl_context", "V-test", untimed["id"])
    assert result["window"] == {"start": None, "end": None}
    assert result["counts"]["apc_unplottable"] == 1
    assert not result["avl"]


def test_all_witnesses_retained_outside_window_and_nested_decision_unchanged(run):
    data = ReviewData(run)
    result = data.case("avl_context", "V-test", "A", context_s=0)
    pings = {r["rid"]: r for r in result["avl"]}
    assert pings["outside"]["is_witness"]
    assert not pings["outside"]["in_window"]
    assert not pings["outside"]["in_focus"]
    assert pings["outside"]["break_before"]
    assert pings["outside"]["progress_m"] == 0
    assert result["counts"]["avl_witnesses_outside_window"] == 1
    p1 = next(r for r in result["apc"] if r["rid"] == "p1")
    assert p1["decision"]["witnesses"][0]["ping_ids"] == ["a1", "outside"]
    assert p1["boarding"] == 2.5
    assert result["provenance"]["counts_aggregated"] is False
    json.dumps(result, allow_nan=False)
    p1["decision"]["witnesses"].clear()
    again = data.case("avl_context", "V-test", "A")
    assert next(r for r in again["apc"] if r["rid"] == "p1")["decision"]["witnesses"]


@pytest.mark.parametrize(
    "method,vehicle,case",
    [
        ("../avl_context", "V-test", "A"),
        ("avl_context", "../V-test", "A"),
        ("avl_context", "V-test", "../../manifest.json"),
    ],
)
def test_allowlists_reject_path_traversal(run, method, vehicle, case):
    data = ReviewData(run)
    assert not data.has_case(method, vehicle, case)
    with pytest.raises(ValueError):
        data.case(method, vehicle, case)


@pytest.mark.parametrize("context", [-1, 3601, float("nan"), float("inf"), "180"])
def test_context_bounds(run, context):
    with pytest.raises(ValueError):
        ReviewData(run).case("avl_context", "V-test", "A", context_s=context)


def test_naive_wall_time_is_independent_of_host_timezone(run):
    original = os.environ.get("TZ")
    snapshots = []
    try:
        for zone in ("UTC", "America/Toronto", "Asia/Shanghai"):
            os.environ["TZ"] = zone
            time.tzset()
            result = ReviewData(run).case("avl_context", "V-test", "A")
            snapshots.append(
                (result["case"]["start"], result["window"], result["avl"][0]["t"])
            )
        assert snapshots[0] == snapshots[1] == snapshots[2]
        assert snapshots[0][0] == (ORIGIN - datetime(1970, 1, 1)).total_seconds()
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()
    with pytest.raises(ValueError, match="naive"):
        wall_seconds(ORIGIN.replace(tzinfo=UTC))


def test_progress_breaks_gap_speed_invalid_and_conflicting_duplicates():
    rows = [
        ping("a", 0),
        ping("b", 30, lat=43.6501),
        ping("duplicate", 30, lat=43.6501),
        ping("gap", 180, lat=43.6502),
        ping("speed", 181, lat=44),
        ping("invalid", 190, invalid=True),
        ping("restart", 200),
        ping("conflict1", 220),
        ping("conflict2", 220, lat=44),
        ping("restart2", 240),
        ping("no-time", None),
    ]
    for row in rows:
        row["t"] = wall_seconds(row["time"])
    _decorate_progress(rows)
    by_id = {r["rid"]: r for r in rows}
    assert by_id["b"]["progress_m"] > 0
    assert by_id["duplicate"]["progress_m"] == by_id["b"]["progress_m"]
    assert by_id["gap"]["break_reason"] == "gap_over_90s"
    assert by_id["gap"]["progress_m"] == 0
    assert by_id["speed"]["break_reason"] == "speed_over_130kph"
    assert by_id["speed"]["speed_kph"] > 130
    for rid in ("invalid", "conflict1", "conflict2", "no-time"):
        assert not by_id[rid]["valid"]
        assert by_id[rid]["progress_m"] is None
    assert by_id["restart"]["break_before"]
    assert by_id["restart2"]["break_before"]


def test_duplicate_invalid_alias_invalidates_whole_instant():
    rows = [ping("good", 0), ping("bad-copy", 0, time_ambiguous=True), ping("next", 10)]
    for row in rows:
        row["t"] = wall_seconds(row["time"])
    _decorate_progress(rows)
    assert not rows[0]["valid"] and not rows[1]["valid"]
    assert rows[2]["break_before"]


def test_zero_zero_placeholder_is_not_plottable_valid_gps():
    row = ping("zero", 0, lat=0.0, lon=0.0)
    row["t"] = wall_seconds(row["time"])
    _decorate_progress([row])
    assert not row["valid"]
    assert row["progress_m"] is None


def test_long_chunk_warning_and_unverified_source_trip_are_retained(run):
    manifest = json.loads((run / "manifest.json").read_text())
    path = run.parent / "audit" / "avl.parquet"
    frame = pl.read_parquet(path).with_columns(
        pl.lit("vendor-label").alias("source_trip")
    )
    frame.write_parquet(path)
    chunk_path = run.parent / "baseline" / "chunks" / "avl_context" / "V-test.parquet"
    chunks = pl.read_parquet(chunk_path).to_dicts()
    chunks[0]["end"] = ORIGIN + timedelta(hours=3)
    write_rows(chunk_path, chunks)
    result = ReviewData(run).case("avl_context", "V-test", "A")
    assert "long_chunk_over_2h_not_verified_trip" in result["case"]["segment_flags"]
    assert result["case"]["source_trip_labels"] == ["vendor-label"]
    assert result["avl"][0]["source_trip"] == "vendor-label"
    assert any("multiple performed trips" in warning for warning in result["warnings"])
    assert manifest["gtfs_used"] is False


def test_decision_coverage_is_checked(run):
    path = run / "assignments" / "avl_context" / "V-test.parquet"
    frame = pl.read_parquet(path).head(1)
    frame.write_parquet(path)
    with pytest.raises(ValueError, match="every APC"):
        ReviewData(run).catalog("avl_context", "V-test")


def test_avl_only_vehicle_has_no_assignment_file_but_retains_segments(run):
    path = run.parent / "audit" / "apc.parquet"
    pl.read_parquet(path).head(0).write_parquet(path)
    (run / "assignments" / "avl_context" / "V-test.parquet").unlink()
    data = ReviewData(run)
    catalog = data.catalog("avl_context", "V-test")
    assert len(catalog) == 2
    assert all(row["kind"] == "segment" and row["apc_count"] == 0 for row in catalog)
    assert data.case("avl_context", "V-test", "A")["avl"]
    assert not data.case("avl_context", "V-test", "A")["apc"]


def test_apc_only_vehicle_without_frozen_chunks_retains_every_row(run):
    path = run.parent / "audit" / "avl.parquet"
    pl.read_parquet(path).head(0).write_parquet(path)
    (run.parent / "baseline" / "chunks" / "avl_context" / "V-test.parquet").unlink()
    data = ReviewData(run)
    catalog = data.catalog("avl_context", "V-test")
    assert all(row["kind"] != "segment" for row in catalog)
    assert sum(row["apc_count"] for row in catalog) == 7


def test_vehicle_lru_never_retains_more_than_two(run):
    selection = [{"vehicle_case": f"V-{i}", "raw_vehicle": str(i)} for i in range(3)]
    (run / "selection.json").write_text(json.dumps(selection))
    data = ReviewData(run)
    for item in selection:
        data._vehicle(item["vehicle_case"])
        assert len(data._cache) <= 2
    assert list(data._cache) == ["V-1", "V-2"]
    data._vehicle("V-1")
    assert list(data._cache) == ["V-2", "V-1"]


def test_catalog_is_deterministic_and_not_mutable_through_response(run):
    data = ReviewData(run)
    first = data.catalog("avl_context", "V-test")
    second = data.catalog("avl_context", "V-test")
    assert first == second
    assert first[-1]["start"] is None
    first[0]["status_counts"].clear()
    assert data.catalog("avl_context", "V-test") == second


def configure_debug(run, *, methods=None, debug_date="2023-11-02", scope=None):
    manifest = json.loads((run / "manifest.json").read_text())
    manifest.update(methods=methods or ["gap_only"], debug_date=debug_date)
    if scope is not None:
        manifest["scope"] = scope
    (run / "manifest.json").write_text(json.dumps(manifest))
    (run / "config.json").write_text(
        json.dumps(
            {
                "method": "gap_only",
                "segmentation": {"gap_s": 120},
                "merge": {"radius_m": 75},
            }
        )
    )


def test_single_method_manifest_is_actual_allowlist_and_keeps_full_config(run):
    configure_debug(run)
    data = ReviewData(run)
    meta = data.metadata()
    assert meta["methods"] == ["gap_only"]
    assert meta["default_method"] == "gap_only"
    assert meta["debug_date"] == "2023-11-02"
    assert meta["scope"]["end"] - meta["scope"]["start"] == 86400
    assert meta["config"]["segmentation"]["gap_s"] == 120
    assert meta["merge_config"] == {"radius_m": 75}
    assert "not verified trip" in meta["day_boundary_warning"]
    assert data.has_case("gap_only", "V-test", "A")
    assert not data.has_case("avl_context", "V-test", "A")
    with pytest.raises(ValueError, match="method"):
        data.catalog("avl_context", "V-test")


@pytest.mark.parametrize("methods", [[], ["bad"], ["gap_only", "gap_only"], "gap_only"])
def test_invalid_manifest_methods_rejected(run, methods):
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["methods"] = methods
    (run / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="methods"):
        ReviewData(run)


def test_route_name_branch_and_all_source_fields_retained(run):
    path = run.parent / "audit" / "apc.parquet"
    source = pl.read_parquet(path).with_columns(
        pl.lit("Dufferin Express").alias("route_name"),
        pl.lit("B").alias("branch"),
        pl.lit("do-not-drop").alias("vendor_extra"),
    )
    source.write_parquet(path)
    result = ReviewData(run).case("avl_context", "V-test", "A")
    row = next(r for r in result["apc"] if r["rid"] == "p1")
    assert row["route_name"] == "Dufferin Express"
    assert row["branch"] == "B"
    assert row["source"]["vendor_extra"] == "do-not-drop"
    assert set(source.columns).issubset(row["source"])
    assert row["source"]["boarding"] == 2.5
    source.rename({"route_name": "RouteName", "branch": "Branch"}).write_parquet(path)
    aliased = ReviewData(run).case("avl_context", "V-test", "A")["apc"][0]
    assert aliased["route_name"] == "Dufferin Express"
    assert aliased["branch"] == "B"


def test_orphan_window_gps_is_visible_context_not_accepted_membership(run):
    path = run.parent / "audit" / "apc.parquet"
    rows = pl.read_parquet(path).to_dicts()
    row = next(r for r in rows if r["rid"] == "p2")
    row["arrival"] = ORIGIN + timedelta(seconds=25)
    row["departure"] = ORIGIN + timedelta(seconds=35)
    write_rows(path, rows)
    data = ReviewData(run)
    summary = next(
        r for r in data.catalog("avl_context", "V-test") if r["kind"] == "apc_only"
    )
    assert summary["avl_count"] == summary["avl_member_count"] == 0
    assert summary["window_avl_count"] == summary["context_avl_count"] == 1
    assert "不表示没有 GPS" in summary["title"]
    assert summary["apc_rids"] == ["p2"]
    detail = data.case("avl_context", "V-test", summary["id"], context_s=180)
    gps = next(r for r in detail["avl"] if r["rid"] == "a1")
    assert gps["in_focus"] and gps["in_core_window"]
    assert gps["relation"] == gps["membership_relation"] == "context"
    assert not gps["is_segment_member"]
    assert gps["segment_id"] == "A"
    assert detail["counts"]["avl_member_count"] == 0
    assert detail["counts"]["window_avl_count"] == 1
    assert all(r["relation"] != "accepted" for r in detail["apc"])
    assert any("does not mean accepted" in warning for warning in detail["warnings"])


def test_day_window_clipping_preserves_source_timestamps_and_witnesses(run):
    day_start = datetime(2023, 11, 6)
    day_end = day_start + timedelta(days=1)
    configure_debug(
        run,
        debug_date="2023-11-06",
        scope={
            "start": day_start.isoformat(),
            "end": day_end.isoformat(),
        },
    )
    path = run.parent / "audit" / "apc.parquet"
    rows = pl.read_parquet(path).to_dicts()
    for row in rows:
        if row["arrival"] is not None:
            row["arrival"] = day_start + timedelta(seconds=10)
            row["departure"] = day_start + timedelta(seconds=20)
    rows[0]["arrival"] = day_start - timedelta(seconds=10)
    rows[0]["departure"] = day_end + timedelta(seconds=10)
    write_rows(path, rows)
    path = run.parent / "audit" / "avl.parquet"
    gps = pl.read_parquet(path).to_dicts()
    for row in gps:
        row["time"] = day_start + timedelta(seconds=15)
    next(row for row in gps if row["rid"] == "outside")["time"] = day_end + timedelta(
        seconds=30
    )
    write_rows(path, gps)
    chunk_path = run.parent / "baseline" / "chunks" / "gap_only" / "V-test.parquet"
    chunks = pl.read_parquet(chunk_path).to_dicts()
    chunks[0]["start"] = day_start - timedelta(seconds=60)
    chunks[0]["end"] = day_end + timedelta(seconds=60)
    write_rows(chunk_path, chunks)
    data = ReviewData(run)
    result = data.case("gap_only", "V-test", "A", context_s=180)
    assert result["window"] == {
        "start": wall_seconds(day_start),
        "end": wall_seconds(day_end),
    }
    assert result["date_clipping"] == {"start": True, "end": True}
    assert result["case"]["date_clipped_start"]
    assert result["case"]["date_clipped_end"]
    assert result["case"]["scope_limited"]
    assert result["case"]["window_avl_count"] == 5
    source = next(row for row in result["apc"] if row["rid"] == "p1")
    assert source["arrival"] == wall_seconds(day_start - timedelta(seconds=10))
    assert source["departure"] == wall_seconds(day_end + timedelta(seconds=10))
    outside = next(row for row in result["avl"] if row["rid"] == "outside")
    assert outside["is_witness"] and not outside["in_window"]
    assert result["counts"]["missing_witness_ping_ids"] == 0
    assert any("data-scope boundaries" in warning for warning in result["warnings"])
    catalog = data.catalog("gap_only", "V-test")
    assert set().union(*(set(row["apc_rids"]) for row in catalog)) == {
        row["rid"] for row in rows
    }
    assert result["provenance"]["debug_date"] == "2023-11-06"


def configure_stream(run):
    configure_debug(run, methods=["vehicle_only"])
    manifest = json.loads((run / "manifest.json").read_text())
    manifest.update(
        representation="vehicle_observation_stream",
        trip_segmentation_used=False,
        observation_matching_used=False,
    )
    (run / "manifest.json").write_text(json.dumps(manifest))
    rows = []
    for source in ("avl", "apc"):
        for row in pl.read_parquet(
            run.parent / "audit" / f"{source}.parquet"
        ).to_dicts():
            if source == "avl":
                instant, basis = row.get("time"), "avl.time"
            elif row.get("arrival") is not None:
                instant, basis = row["arrival"], "apc.arrival"
            else:
                instant, basis = row.get("departure"), "apc.departure_fallback"
            rows.append(
                {
                    **row,
                    "source": source,
                    "source_rid": row["rid"],
                    "event_time": instant,
                    "event_time_basis": basis if instant is not None else "missing",
                }
            )
    rows.sort(
        key=lambda row: (
            row["event_time"] is None,
            row["event_time"] or datetime.max,
            row["source"] != "avl",
            row.get("source_row") is None,
            row.get("source_row") or 0,
            row["rid"],
        )
    )
    for index, row in enumerate(rows):
        row["display_order"] = index
    write_rows(run / "observations" / "V-test.parquet", rows)


def test_vehicle_stream_has_one_day_case_no_chunks_or_matching(run, monkeypatch):
    configure_stream(run)
    original_read = pl.read_parquet

    def read_no_old_evidence(path, *args, **kwargs):
        assert "chunks" not in str(path) and "assignments" not in str(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(pl, "read_parquet", read_no_old_evidence)
    data = ReviewData(run)
    meta = data.metadata()
    assert meta["methods"] == ["vehicle_only"]
    assert meta["default_method"] == "vehicle_only"
    assert meta["representation"] == "vehicle_observation_stream"
    assert not meta["trip_segmentation_used"] and not meta["observation_matching_used"]
    catalog = data.catalog("vehicle_only", "V-test")
    assert len(catalog) == 1
    case = catalog[0]
    assert case["kind"] == "vehicle_stream"
    assert case["end"] - case["start"] == 86400
    assert case["avl_member_count"] == 0
    assert case["source_counts"] == {"avl": 6, "apc": 7}
    assert "trip" not in case["title"].lower()
    assert data.has_case("vehicle_only", "V-test", case["id"])
    detail = data.case("vehicle_only", "V-test", case["id"])
    assert detail["segments"] == []
    assert len(detail["observations"]) == 13
    assert all(
        row["in_focus"] and not row["is_segment_member"]
        for row in detail["avl"] + detail["apc"]
    )
    assert all(
        row["membership_relation"] == "context" for row in detail["avl"] + detail["apc"]
    )
    for row in detail["apc"]:
        assert row["status"] == "not_evaluated"
        assert row["progress_m"] is None
        assert row["decision"]["mode"] == "vehicle_group_only"
        assert (
            not {"witnesses", "score", "segment_ids", "ping_ids"}
            & row["decision"].keys()
        )
    assert detail["counts"]["source_rows_preserved"]
    assert detail["counts"]["duplicate_source_identities"] == 0
    assert detail["provenance"]["deduplication_performed"] is False
    json.dumps(detail, allow_nan=False)


def test_vehicle_stream_source_order_ties_untimed_and_conservation(run):
    configure_stream(run)
    data = ReviewData(run)
    case = data.catalog("vehicle_only", "V-test")[0]
    detail = data.case("vehicle_only", "V-test", case["id"])
    stream = detail["observations"]
    assert [row["display_order"] for row in stream] == list(range(13))
    assert [
        (row["source"], row["rid"])
        for row in stream
        if row["event_time"] == wall_seconds(ORIGIN + timedelta(seconds=30))
    ] == [("avl", "a1"), ("apc", "p1")]
    assert stream[-1]["rid"] == "p6"
    assert stream[-1]["event_time"] is None
    assert stream[-1]["time_basis"] == stream[-1]["event_time_basis"] == "missing"
    for source in ("avl", "apc"):
        returned = [row for row in stream if row["source"] == source]
        assert {row["rid"] for row in returned} == {
            row["rid"] for row in detail[source]
        }
        assert len(returned) == len(detail[source])
        assert all(row["source_rid"] == row["rid"] for row in returned)


def test_vehicle_stream_apc_tail_and_departure_fallback_not_limited_by_gps(run):
    path = run.parent / "audit" / "apc.parquet"
    rows = pl.read_parquet(path).to_dicts()
    tail = next(row for row in rows if row["rid"] == "p2")
    tail.update(
        arrival=None,
        departure=ORIGIN + timedelta(hours=1, minutes=13),
        route_name="DUFFERIN",
        branch="29Dcon",
    )
    write_rows(path, rows)
    configure_stream(run)
    data = ReviewData(run)
    key = data.catalog("vehicle_only", "V-test")[0]["id"]
    short = data.case("vehicle_only", "V-test", key, context_s=0)
    wide = data.case("vehicle_only", "V-test", key, context_s=1800)
    assert short["window"] == wide["window"] == data.metadata()["scope"]
    assert short["observations"] == wide["observations"]
    row = next(row for row in short["observations"] if row["rid"] == "p2")
    assert row["event_time_basis"] == "apc.departure_fallback"
    assert row["event_time"] > max(row["t"] for row in short["avl"])
    assert row["arrival"] is None
    assert row["departure"] == wall_seconds(tail["departure"])
    assert row["source_fields"]["boarding"] == 2.5
    apc_row = next(row for row in short["apc"] if row["rid"] == "p2")
    assert apc_row["route_name"] == "DUFFERIN" and apc_row["branch"] == "29Dcon"
    assert apc_row["in_focus"] and apc_row["progress_m"] is None


def test_vehicle_stream_invalid_untimed_avl_retained_and_progress_has_breaks(run):
    path = run.parent / "audit" / "avl.parquet"
    rows = pl.read_parquet(path).to_dicts()
    rows.append(ping("invalid-untimed", None, lat=None, lon=None, invalid=True))
    next(row for row in rows if row["rid"] == "a1")["invalid"] = True
    write_rows(path, rows)
    configure_stream(run)
    data = ReviewData(run)
    case = data.catalog("vehicle_only", "V-test")[0]
    result = data.case("vehicle_only", "V-test", case["id"])
    by_id = {row["rid"]: row for row in result["avl"]}
    assert len(by_id) == case["source_counts"]["avl"] == 7
    assert by_id["invalid-untimed"]["in_focus"]
    assert not by_id["invalid-untimed"]["valid"]
    assert by_id["invalid-untimed"]["progress_m"] is None
    assert by_id["a1"]["progress_m"] is None
    assert by_id["a2"]["break_before"]
    assert by_id["outside"]["break_reason"] == "gap_over_90s"
    source = next(
        row for row in result["observations"] if row["rid"] == "invalid-untimed"
    )
    assert source["source_fields"]["lat"] is None
    assert source["time_basis"] == "missing"


def test_vehicle_stream_same_time_numbered_source_row_precedes_missing(run):
    path = run.parent / "audit" / "avl.parquet"
    rows = pl.read_parquet(path).to_dicts()
    for row in rows:
        row["source_row"] = None
    rows.append(ping("z-numbered", 0, source_row=7))
    write_rows(path, rows)
    configure_stream(run)
    data = ReviewData(run)
    case = data.catalog("vehicle_only", "V-test")[0]
    result = data.case("vehicle_only", "V-test", case["id"])
    first = [
        row
        for row in result["observations"]
        if row["event_time"] == wall_seconds(ORIGIN)
    ]
    assert [row["rid"] for row in first] == ["z-numbered", "a0"]
    assert first[0]["source_row"] == 7 and first[1]["source_row"] is None
    # These identical GPS payloads remain distinct raw source identities.
    assert case["source_counts"]["avl"] == 7


def configure_dedup(run):
    path = run.parent / "audit" / "avl.parquet"
    avl = pl.read_parquet(path).to_dicts()
    for index, row in enumerate(avl, 1):
        row["source_row"] = index
    duplicate = dict(avl[1], rid="copy-a1", source_row=7)
    different_format = dict(avl[1], rid="format-a1", source_row=8)
    avl.extend([duplicate, different_format])
    write_rows(path, avl)
    configure_stream(run)
    fields = []
    for row in avl:
        fields.append(
            {
                "source_rid": row["rid"],
                "source_row": row["source_row"],
                "vehicle": row["vehicle"],
                "Date_Key": "20231102",
                "Message_DateTime": row["time"].isoformat(),
                "Vehicle": row["vehicle"],
                "Latitude": str(row["lat"]),
                "Longitude": str(row["lon"]),
                "Heading": "0",
                "Route": row["route"],
                "Run": "1",
                "Destination": "D",
                "OffRoute": "false",
                "Delayed": None,
                "KPH": "0",
                "tatripId": "v",
            }
        )
    next(row for row in fields if row["source_rid"] == "format-a1")["Latitude"] += "0"
    field_path = run / "deduplication" / "raw_avl_fields.parquet"
    write_rows(field_path, fields)
    mapping = []
    for row in avl:
        representative = "a1" if row["rid"] == "copy-a1" else row["rid"]
        mapping.append(
            {
                "source": "avl",
                "source_rid": row["rid"],
                "representative_rid": representative,
                "is_representative": row["rid"] == representative,
                "duplicate_group_size": 2 if representative == "a1" else 1,
                "group_id": "group-" + representative,
                "source_row": row["source_row"],
                "representative_source_row": 2
                if representative == "a1"
                else row["source_row"],
            }
        )
    write_rows(run / "deduplication" / "V-test.parquet", mapping)
    stream_path = run / "observations" / "V-test.parquet"
    observations = [
        row
        for row in pl.read_parquet(stream_path).to_dicts()
        if row["rid"] != "copy-a1"
    ]
    for index, row in enumerate(observations):
        row["display_order"] = index
    write_rows(stream_path, observations)
    source_audit = run.parent / "source_audit"
    source_audit.mkdir()
    inventory = source_audit / "inventory.json"
    inventory.write_text(json.dumps({"sources": {"avl": {"sha256": "1" * 64}}}))
    (run.parent / "audit" / "manifest.json").write_text(
        json.dumps(
            {
                "source_audit": str(source_audit),
                "input_sha256": {
                    "inventory.json": hashlib.sha256(inventory.read_bytes()).hexdigest()
                },
            }
        )
    )
    manifest = json.loads((run / "manifest.json").read_text())
    manifest.update(
        methods=["vehicle_only_dedup"],
        avl_deduplication=True,
        deduplication={
            "rule": "raw_avl_all_13_fields_exact_min_source_row",
            "fields": list(RAW_AVL_EXACT_FIELDS),
            "raw_csv_path": str(source_audit / "raw.csv"),
            "raw_csv_sha256": "1" * 64,
            "raw_fields_path": "deduplication/raw_avl_fields.parquet",
            "raw_fields_sha256": hashlib.sha256(field_path.read_bytes()).hexdigest(),
            "source_row_origin": 1,
            "keep": "minimum_source_row",
            "comparison": "parsed CSV strings; no normalization",
        },
    )
    (run / "manifest.json").write_text(json.dumps(manifest))


def refresh_fields_hash(run):
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    path = run / "deduplication" / "raw_avl_fields.parquet"
    manifest["deduplication"]["raw_fields_sha256"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest))


def test_dedup_only_exact_raw_string_copies_removed_with_complete_lineage(run):
    configure_dedup(run)
    data = ReviewData(run)
    assert data.metadata()["methods"] == ["vehicle_only_dedup"]
    assert data.metadata()["avl_deduplication"]
    case = data.catalog("vehicle_only_dedup", "V-test")[0]
    detail = data.case("vehicle_only_dedup", "V-test", case["id"])
    assert case["source_counts"] == {"avl": 8, "apc": 7}
    assert case["retained_counts"] == {"avl": 7, "apc": 7}
    assert case["removed_avl"] == 1
    assert len(detail["deduplication"]) == 8
    assert len(detail["observations"]) == 14
    assert {row["rid"] for row in detail["avl"]} == {
        "a0",
        "a1",
        "a2",
        "b0",
        "b1",
        "outside",
        "format-a1",
    }
    assert "copy-a1" not in {row["rid"] for row in detail["observations"]}
    rep = next(row for row in detail["avl"] if row["rid"] == "a1")
    assert rep["deduplication"]["source_rids"] == ["a1", "copy-a1"]
    assert rep["deduplication"]["duplicate_group_size"] == 2
    event = next(row for row in detail["observations"] if row["rid"] == "a1")
    assert event["deduplication"] == rep["deduplication"]
    assert detail["counts"]["source_avl_total"] == 8
    assert detail["counts"]["retained_avl_total"] == 7
    assert detail["counts"]["removed_avl_duplicates"] == 1
    assert not detail["counts"]["source_rows_preserved"]
    assert detail["counts"]["source_lineage_preserved"]
    assert all(row["status"] == "not_evaluated" for row in detail["apc"])
    assert {row["rid"] for row in detail["apc"]} == {f"p{i}" for i in range(1, 8)}
    assert detail["provenance"]["deduplication_performed"]
    assert "not a display of every" in detail["warnings"][0]
    json.dumps(detail, allow_nan=False)


@pytest.mark.parametrize(
    "corruption",
    [
        "omit_mapping",
        "wrong_rep",
        "wrong_size",
        "wrong_row",
        "merge_nonduplicate",
        "raw_checksum",
        "inventory_identity",
        "drop_apc",
        "extra_copy",
    ],
)
def test_dedup_rejects_incomplete_or_incorrect_grouping(run, corruption):
    configure_dedup(run)
    mapping_path = run / "deduplication" / "V-test.parquet"
    mapping = pl.read_parquet(mapping_path).to_dicts()
    if corruption == "omit_mapping":
        write_rows(mapping_path, mapping[:-1])
    elif corruption in {"wrong_rep", "wrong_size", "wrong_row", "merge_nonduplicate"}:
        row = next(row for row in mapping if row["source_rid"] == "copy-a1")
        if corruption == "wrong_rep":
            row["representative_rid"] = "copy-a1"
        elif corruption == "wrong_size":
            row["duplicate_group_size"] = 3
        elif corruption == "wrong_row":
            row["source_row"] = 200
        else:
            row = next(row for row in mapping if row["source_rid"] == "format-a1")
            row.update(
                representative_rid="a1",
                is_representative=False,
                duplicate_group_size=3,
                group_id="group-a1",
                representative_source_row=2,
            )
        write_rows(mapping_path, mapping)
    elif corruption == "raw_checksum":
        path = run / "deduplication" / "raw_avl_fields.parquet"
        pl.read_parquet(path).head(1).write_parquet(path)
    elif corruption == "inventory_identity":
        path = run.parent / "source_audit" / "inventory.json"
        path.write_text(json.dumps({"sources": {"avl": {"sha256": "2" * 64}}}))
    else:
        path = run / "observations" / "V-test.parquet"
        rows = pl.read_parquet(path).to_dicts()
        if corruption == "drop_apc":
            rows = [row for row in rows if row["rid"] != "p1"]
        else:
            rows.append(
                dict(
                    next(row for row in rows if row["rid"] == "a1"),
                    rid="copy-a1",
                    source_rid="copy-a1",
                    source_row=7,
                )
            )
        write_rows(path, rows)
    with pytest.raises(ValueError):
        ReviewData(run).catalog("vehicle_only_dedup", "V-test")


def test_dedup_null_empty_and_whitespace_are_not_equal_raw_fields(run):
    configure_dedup(run)
    fields_path = run / "deduplication" / "raw_avl_fields.parquet"
    fields = pl.read_parquet(fields_path).to_dicts()
    next(row for row in fields if row["source_rid"] == "copy-a1")["Delayed"] = ""
    write_rows(fields_path, fields)
    refresh_fields_hash(run)
    with pytest.raises(ValueError, match="raw.field groups"):
        ReviewData(run).catalog("vehicle_only_dedup", "V-test")
    next(row for row in fields if row["source_rid"] == "copy-a1")["Delayed"] = " "
    write_rows(fields_path, fields)
    refresh_fields_hash(run)
    with pytest.raises(ValueError, match="raw.field groups"):
        ReviewData(run).catalog("vehicle_only_dedup", "V-test")


def test_dedup_apc_only_vehicle_allows_absent_empty_mapping(run):
    configure_dedup(run)
    source = run.parent / "audit" / "avl.parquet"
    pl.read_parquet(source).head(0).write_parquet(source)
    fields_path = run / "deduplication" / "raw_avl_fields.parquet"
    pl.read_parquet(fields_path).head(0).write_parquet(fields_path)
    refresh_fields_hash(run)
    (run / "deduplication" / "V-test.parquet").unlink()
    obs_path = run / "observations" / "V-test.parquet"
    observations = (
        pl.read_parquet(obs_path).filter(pl.col("source") == "apc").to_dicts()
    )
    for index, row in enumerate(observations):
        row["display_order"] = index
    write_rows(obs_path, observations)
    data = ReviewData(run)
    case = data.catalog("vehicle_only_dedup", "V-test")[0]
    result = data.case("vehicle_only_dedup", "V-test", case["id"])
    assert result["avl"] == [] and len(result["apc"]) == 7
    assert result["deduplication"] == []
    assert case["removed_avl"] == 0


def test_dedup_avl_only_vehicle_preserves_representatives_without_apc(run):
    configure_dedup(run)
    path = run.parent / "audit" / "apc.parquet"
    pl.read_parquet(path).head(0).write_parquet(path)
    path = run / "observations" / "V-test.parquet"
    observations = pl.read_parquet(path).filter(pl.col("source") == "avl").to_dicts()
    for index, row in enumerate(observations):
        row["display_order"] = index
    write_rows(path, observations)
    data = ReviewData(run)
    case = data.catalog("vehicle_only_dedup", "V-test")[0]
    result = data.case("vehicle_only_dedup", "V-test", case["id"])
    assert len(result["avl"]) == 7 and result["apc"] == []
    assert case["removed_avl"] == 1


def test_vehicle_stream_source_inspector_preserves_large_hash_digits(run):
    path = run.parent / "audit" / "avl.parquet"
    rows = pl.read_parquet(path).to_dicts()
    for row in rows:
        row["payload_hash"] = 2**60 + 3
    write_rows(path, rows)
    configure_stream(run)
    data = ReviewData(run)
    case = data.catalog("vehicle_only", "V-test")[0]
    result = data.case("vehicle_only", "V-test", case["id"])
    source = next(row for row in result["observations"] if row["source"] == "avl")
    assert source["payload_hash"] == str(2**60 + 3)
    assert source["source_fields"]["payload_hash"] == str(2**60 + 3)
    assert result["avl"][0]["payload_hash"] == str(2**60 + 3)
    assert pl.read_parquet(path)["payload_hash"][0] == 2**60 + 3
    assert "decimal strings" in data.metadata()["json_integer_encoding"]
    assert "decimal strings" in result["provenance"]["json_integer_encoding"]


@pytest.mark.parametrize("corruption", ["duplicate", "time", "count", "order"])
def test_vehicle_stream_rejects_missing_lineage_and_source_changes(run, corruption):
    configure_stream(run)
    path = run / "observations" / "V-test.parquet"
    rows = pl.read_parquet(path).to_dicts()
    if corruption == "duplicate":
        rows.append(rows[0])
    elif corruption == "time":
        rows[0]["event_time"] += timedelta(seconds=1)
    elif corruption == "count":
        next(row for row in rows if row["source"] == "apc")["boarding"] = 999
    else:
        rows.reverse()
        for index, row in enumerate(rows):
            row["display_order"] = index
    write_rows(path, rows)
    with pytest.raises(ValueError, match="source|stream"):
        ReviewData(run).catalog("vehicle_only", "V-test")
