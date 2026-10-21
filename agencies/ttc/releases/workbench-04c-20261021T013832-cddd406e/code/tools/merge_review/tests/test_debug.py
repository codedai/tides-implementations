"""Bounded synthetic tests for the immutable single-day experiment workspace."""

import copy
import json
from datetime import datetime, timedelta

import polars as pl
import pytest

from tools.merge_review import debug

T = datetime(2023, 11, 6)


def ping(rid, offset, **changes):
    return {
        "rid": rid,
        "vehicle": "9027",
        "time": T + timedelta(seconds=offset),
        "lat": 43.65,
        "lon": -79.4,
        "route": "29",
        "run": "1",
        "invalid": False,
        "time_ambiguous": False,
        "time_nonexistent": False,
        **changes,
    }


def apc(number, arrival, departure=None, **changes):
    instant = None if arrival is None else T + timedelta(seconds=arrival)
    return {
        "rid": f"apc:{number}",
        "source_row": number,
        "vehicle": "9027",
        "arrival": instant,
        "departure": instant if departure is None else T + timedelta(seconds=departure),
        "lat": 43.65,
        "lon": -79.4,
        "route": "29",
        "direction": "NORTH",
        "branch": "29Dcon",
        "stop": "s",
        "boarding": 2.5,
        "alighting": 1.5,
        "arrival_load": 5.5,
        "departure_load": 6.5,
        "invalid": False,
        "time_ambiguous": False,
        "time_nonexistent": False,
        "grain_uncertain": True,
        **changes,
    }


@pytest.fixture
def workspace(tmp_path):
    audit = tmp_path / "audit"
    audit.mkdir()
    avl = [
        ping("before", -10),
        ping("p0", 0),
        ping("p1", 30),
        ping("p2", 60),
        ping("bad", 120, invalid=True),
        ping("avl-only", 30, vehicle="avl-only"),
        ping("after", 86400),
    ]
    rows = [
        apc(1, 30),
        apc(2, 86395, 86415),
        apc(3, None, 3600, invalid=True),
        apc(4, 30, vehicle="apc-only"),
        apc(5, -30, 20),
        apc(6, None, invalid=True),
        apc(7, 86400),
    ]
    pl.from_dicts(avl).write_parquet(audit / "avl.parquet")
    pl.from_dicts(rows).write_parquet(audit / "apc.parquet")
    return debug.DebugWorkspace(tmp_path / "debug", audit)


def test_day_fixture_is_strict_and_preserves_cross_midnight_and_ids(workspace):
    folder = workspace.ensure_fixture()
    a, b = (pl.read_parquet(folder / f"{s}.parquet") for s in ("avl", "apc"))
    assert set(a["rid"]) == {"p0", "p1", "p2", "bad", "avl-only"}
    assert set(b["rid"]) == {"apc:1", "apc:2", "apc:3", "apc:4"}
    tail = b.filter(pl.col("rid") == "apc:2").to_dicts()[0]
    assert tail["departure"] == T + timedelta(seconds=86415)
    assert tail["debug_day_censored_end"]
    fallback = b.filter(pl.col("rid") == "apc:3").to_dicts()[0]
    assert fallback["arrival"] is None and fallback["debug_membership_uses_departure"]
    assert b["route_name"].null_count() == 4
    assert workspace.ensure_fixture() == folder
    metadata = workspace.metadata()
    assert metadata["fixture"]["neighbouring_day_gps"] == 0
    assert not metadata["fixture"]["service_date_inferred"]
    assert metadata["fixture"]["cross_midnight_apc"] == 1


def test_raw_route_name_is_verbatim_and_hash_verified(workspace):
    raw = workspace.audit / "source.csv"
    pl.DataFrame({"RouteName": [" Original Name "] * 7}).write_csv(raw)
    (workspace.audit / "inventory.json").write_text(
        json.dumps(
            {
                "sources": {
                    "apc": {"path": str(raw), "sha256": debug.evaluate.file_hash(raw)}
                }
            }
        )
    )
    fixture = workspace.ensure_fixture()
    assert (
        pl.read_parquet(fixture / "apc.parquet")["route_name"].to_list()
        == [" Original Name "] * 4
    )
    raw.write_text("RouteName\nchanged\n")
    with pytest.raises(ValueError, match="hash"):
        workspace.ensure_fixture()


@pytest.mark.parametrize(
    "config",
    [
        None,
        [],
        {"date": "2023-11-07"},
        {"method": "unknown"},
        {"method": []},
        {"merge": {"unknown": 1}},
        {"segmentation": []},
        {"merge": {"radius_m": True}},
        {"merge": {"radius_m": float("nan")}},
        {"merge": {"radius_m": float("inf")}},
        {"merge": {"radius_m": -1}},
        {"merge": {"radius_m": 501}},
        {"merge": {"max_apc_dwell_s": 1801}},
        {"merge": {"route_policy": "force"}},
        {"merge": {"max_episodes": 1.5}},
        {"segmentation": {"context_min_points": 1}},
        {"segmentation": {"context_min_points": True}},
        {"merge": {"radius_m": 300}},
    ],
)
def test_config_whitelist_rejects_bad_values(config):
    with pytest.raises(ValueError):
        debug.validate_config(config)


def test_config_is_a_copy_and_all_defaults_have_schema():
    cfg = debug.validate_config({})
    assert cfg == debug.DEFAULTS
    cfg["merge"]["radius_m"] = 12
    assert debug.DEFAULTS["merge"]["radius_m"] == 100
    for section in ("segmentation", "merge"):
        assert {r["key"] for r in debug.PARAMETERS[section]} == set(
            debug.DEFAULTS[section]
        )


def test_fixed_day_and_source_output_separation(tmp_path):
    with pytest.raises(ValueError, match="fixed"):
        debug.DebugWorkspace(tmp_path / "d", tmp_path / "a", date="2023-11-07")
    with pytest.raises(ValueError, match="separate"):
        debug.DebugWorkspace(tmp_path, tmp_path / "a")


def test_real_algorithms_on_synthetic_day_conserve_all_sources(workspace):
    updates = []
    original = {
        s: debug.evaluate.file_hash(workspace.audit / f"{s}.parquet")
        for s in ("avl", "apc")
    }
    record = workspace.create_run({}, label="first", progress=updates.append)
    assert record["summary"]["validation_ok"]
    assert record["summary"]["source_counts"] == {"avl": 5, "apc": 4}
    assert sum(record["summary"]["status_counts"].values()) == 4
    assert (
        record["summary"]["accuracy"] is None and not record["summary"]["publishable"]
    )
    assert updates[-1]["completed"] == updates[-1]["total"] == 3
    assert updates[-1]["stage"] == "complete"
    folder = workspace.get_run(record["run_id"])
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["debug_date"] == debug.DATE
    assert manifest["baseline"] == str(folder)
    assert manifest["methods"] == ["avl_context"]
    assert all(
        debug.evaluate.file_hash(workspace.audit / f"{s}.parquet") == original[s]
        for s in original
    )
    assert workspace.metadata()["runs"][0]["run_id"] == record["run_id"]


def test_compare_does_not_treat_config_hashed_segment_ids_as_changed_membership(
    workspace,
):
    first = workspace.create_run({})
    second = workspace.create_run(
        {"segmentation": {"context_min_seconds": 35}}, parent_run_id=first["run_id"]
    )
    assert first["run_id"] != second["run_id"]
    compared = workspace.compare(first["run_id"], second["run_id"])
    assert compared["source_compatible"]
    assert compared["summary"]["apc_common"] == 4
    assert compared["summary"]["changed_rows"] == 0
    assert compared["summary"]["owner_membership_changed"] == 0
    case = json.loads(
        (workspace.get_run(first["run_id"]) / "selection.json").read_text()
    )[0]["vehicle_case"]
    assert (
        workspace.compare(first["run_id"], second["run_id"], vehicle=case)["summary"][
            "apc_common"
        ]
        <= 4
    )


def test_compare_status_change_is_aligned_on_source_rid(workspace):
    first = workspace.create_run({})
    second = workspace.create_run({"merge": {"accept_score": 1000, "null_score": 1000}})
    compared = workspace.compare(first["run_id"], second["run_id"], vehicle="9027")
    assert compared["summary"]["apc_added"] == compared["summary"]["apc_removed"] == 0
    assert compared["summary"]["status_changed"] >= 1
    assert any(row["apc_rid"] == "apc:1" for row in compared["changes"])
    assert all(
        isinstance(row["arrival"], (int, float)) or row["arrival"] is None
        for row in compared["changes"]
    )


@pytest.mark.parametrize(
    "run_id",
    ["../elsewhere", "/tmp/x", "run-20231106-../x", "run-20231106-missing", None],
)
def test_run_id_paths_are_confined(workspace, run_id):
    with pytest.raises(ValueError):
        workspace.get_run(run_id)


def test_symlink_run_is_rejected(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    runs = workspace.root / "runs"
    runs.mkdir(parents=True)
    (runs / "run-20231106-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        workspace.get_run("run-20231106-link")


def test_hash_change_does_not_reuse_stale_day_cache(workspace):
    first = workspace.ensure_fixture()
    rows = pl.read_parquet(workspace.audit / "apc.parquet")
    rows.with_columns(pl.lit(7.0).alias("boarding")).write_parquet(
        workspace.audit / "apc.parquet"
    )
    second = workspace.ensure_fixture()
    assert first != second
    assert pl.read_parquet(first / "apc.parquet")["boarding"][0] == 2.5


def test_day_cache_tamper_is_rejected(workspace):
    folder = workspace.ensure_fixture()
    (folder / "apc.parquet").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="content changed"):
        workspace.ensure_fixture()


def test_loaded_source_fingerprint_requires_restart(workspace, monkeypatch):
    changed = copy.deepcopy(debug._LOADED_CODE_SHA256)
    changed["matcher"] = "stale"
    monkeypatch.setattr(debug, "_LOADED_CODE_SHA256", changed)
    with pytest.raises(ValueError, match="restart"):
        workspace.ensure_fixture()


def test_validator_failure_never_produces_completed_run(workspace, monkeypatch):
    monkeypatch.setattr(
        debug.evaluate,
        "validate_assignments",
        lambda *args: {"violations": {"broken": 1}, "examined": {}},
    )
    with pytest.raises(ValueError, match="validation failed"):
        workspace.create_run({})
    assert not list((workspace.root / "runs").glob("*/completion.json"))
    assert len(list((workspace.root / "runs").glob("*/failure.json"))) == 1
    assert workspace.metadata()["runs"] == []


def test_debug_outputs_are_private(workspace):
    workspace.create_run({})
    assert workspace.root.stat().st_mode & 0o777 == 0o700
    for path in workspace.root.rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


def test_compare_reports_real_boundary_and_membership_changes(workspace):
    first = workspace.create_run({"method": "gap_only"})
    second = workspace.create_run(
        {"method": "gap_only", "segmentation": {"gap_seconds": 10}}
    )
    compared = workspace.compare(first["run_id"], second["run_id"])
    assert compared["summary"]["boundaries_added"] == 2
    assert compared["summary"]["boundaries_removed"] == 0
    assert compared["summary"]["owner_membership_changed"] >= 1
    assert all("avl_gap" in b["reasons"] for b in compared["boundaries"]["added"])


def test_compare_new_source_row_is_added_not_reassigned(workspace):
    first = workspace.create_run({})
    source = workspace.audit / "apc.parquet"
    frame = pl.read_parquet(source)
    added = pl.from_dicts([apc(8, 60)], schema=frame.schema)
    pl.concat([frame, added]).write_parquet(source)
    second = workspace.create_run({})
    result = workspace.compare(first["run_id"], second["run_id"])
    assert not result["source_compatible"]
    assert result["summary"]["apc_added"] == 1
    row = next(row for row in result["changes"] if row["apc_rid"] == "apc:8")
    assert row["change_types"] == ["added"] and row["base"] is None


def test_vehicle_only_config_is_explicit_and_has_no_hidden_parameters():
    expected = {"method": "vehicle_only", "segmentation": {}, "merge": {}}
    assert debug.validate_config({"method": "vehicle_only"}) == expected
    assert debug.validate_config(expected) == expected
    assert debug.validate_config({})["method"] == "avl_context"
    for section, values in (
        ("merge", {"radius_m": 100}),
        ("segmentation", {"gap_seconds": 1800}),
        ("merge", None),
    ):
        with pytest.raises(ValueError, match="empty"):
            debug.validate_config({"method": "vehicle_only", section: values})


def test_vehicle_only_never_calls_segmentation_or_matcher(workspace, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No segmentation or observation matching in vehicle_only")

    monkeypatch.setattr(debug.segmentation, "segment_vehicle", forbidden)
    monkeypatch.setattr(debug.matcher, "build_index", forbidden)
    monkeypatch.setattr(debug.matcher, "prepare_candidates", forbidden)
    monkeypatch.setattr(debug.matcher, "match_direct", forbidden)
    monkeypatch.setattr(debug.evaluate, "validate_assignments", forbidden)
    record = workspace.create_run({"method": "vehicle_only"})
    folder = workspace.get_run(record["run_id"])
    summary = record["summary"]
    assert summary["observations"] == 9
    assert summary["vehicle_streams"] == 3
    assert summary["segments"] == 0 and summary["status_counts"] == {}
    assert summary["matched_accuracy"] is None
    assert not summary["trip_segmentation_used"]
    assert not summary["observation_matching_used"]
    assert not (folder / "chunks").exists()
    assert not (folder / "assignments").exists()
    assert len(list((folder / "observations").glob("*.parquet"))) == 3
    rows = [
        r
        for path in (folder / "observations").glob("*.parquet")
        for r in pl.read_parquet(path).to_dicts()
    ]
    assert len(rows) == 9
    assert len({(r["source"], r["source_rid"]) for r in rows}) == 9
    assert all("status" not in r and "witnesses" not in r for r in rows)
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["representation"] == "vehicle_observation_stream"
    assert manifest["segmentation_methods"] == []
    assert not manifest["trip_segmentation_used"]
    assert not manifest["observation_matching_used"]


def test_vehicle_only_preserves_duplicate_source_payload_and_uint64(workspace):
    source = workspace.audit / "avl.parquet"
    frame = pl.read_parquet(source).with_columns(
        pl.lit(2**64 - 1, dtype=pl.UInt64).alias("payload_hash")
    )
    duplicate = frame.filter(pl.col("rid") == "p1").with_columns(
        pl.lit("p1-copy").alias("rid")
    )
    pl.concat([frame, duplicate]).write_parquet(source)
    record = workspace.create_run({"method": "vehicle_only"})
    folder = workspace.get_run(record["run_id"])
    rows = pl.read_parquet(folder / "observations" / f"{debug._case('9027')}.parquet")
    assert rows["payload_hash"].dtype == pl.UInt64
    copies = rows.filter(pl.col("rid").is_in(["p1", "p1-copy"]))
    assert copies.height == 2
    assert copies["payload_hash"].to_list() == [2**64 - 1] * 2
    assert record["summary"]["observations"] == 10


def test_vehicle_only_compare_to_matching_is_not_a_status_change(workspace):
    matched = workspace.create_run({})
    original = workspace.get_run(matched["run_id"]) / "manifest.json"
    before = original.read_bytes()
    stream = workspace.create_run({"method": "vehicle_only"})
    for first, second in ((matched, stream), (stream, matched)):
        result = workspace.compare(first["run_id"], second["run_id"])
        assert result["not_comparable"] and not result["comparable"]
        assert result["comparison_status"] == "not_comparable"
        assert result["summary"] is None
        assert result["changes"] == []
        assert result["source_counts"]["base"] == {"avl": 5, "apc": 4}
    assert original.read_bytes() == before


def test_vehicle_only_compare_two_streams_checks_source_identity_and_order(workspace):
    first = workspace.create_run({"method": "vehicle_only"})
    second = workspace.create_run({"method": "vehicle_only"})
    result = workspace.compare(first["run_id"], second["run_id"])
    assert result["comparable"] and not result["not_comparable"]
    assert result["comparison_status"] == "source_stream_comparison"
    assert result["summary"]["observations_common"] == 9
    assert result["summary"]["source_records_conserved"]
    assert result["summary"]["relative_order_preserved"]
    assert result["summary"]["changed_rows"] == 0
    assert "status_changed" not in result["summary"]


def test_vehicle_only_invalid_validation_never_completes(workspace, monkeypatch):
    monkeypatch.setattr(
        debug.vehicle_stream,
        "validate_vehicle_stream",
        lambda *args: {"violations": {"broken": 1}, "examined": {}},
    )
    with pytest.raises(ValueError, match="conservation"):
        workspace.create_run({"method": "vehicle_only"})
    assert not list((workspace.root / "runs").glob("*/completion.json"))


@pytest.fixture
def dedup_workspace(workspace):
    source = workspace.audit / "avl.parquet"
    frame = pl.read_parquet(source).with_row_index("source_row", offset=1)
    duplicate = frame.filter(pl.col("rid") == "p1").with_columns(
        pl.lit("a-duplicate").alias("rid"),
        pl.lit(8, dtype=pl.UInt32).alias("source_row"),
    )
    frame = pl.concat([frame, duplicate]).sort("source_row")
    frame.write_parquet(source)
    rows = []
    for row in frame.to_dicts():
        raw = dict.fromkeys(debug.avl_dedup.RAW_FIELDS, "0")
        raw.update(
            Date_Key=row["time"].strftime("%Y%m%d"),
            Message_DateTime=row["time"].strftime("%Y-%m-%d %H:%M:%S"),
            Vehicle=row["vehicle"],
            Latitude=str(row["lat"]),
            Longitude=str(row["lon"]),
            Route=row["route"],
        )
        rows.append(raw)
    csv = workspace.audit / "original-avl.csv"
    pl.from_dicts(rows).write_csv(csv)
    (workspace.audit / "inventory.json").write_text(
        json.dumps(
            {
                "sources": {
                    "avl": {
                        "path": str(csv),
                        "sha256": debug.avl_dedup.file_sha256(csv),
                    }
                }
            }
        )
    )
    return workspace


def test_dedup_method_has_empty_parameters_and_is_explicit():
    expected = {"method": "vehicle_only_dedup", "segmentation": {}, "merge": {}}
    assert debug.validate_config({"method": "vehicle_only_dedup"}) == expected
    with pytest.raises(ValueError, match="empty"):
        debug.validate_config(expected | {"merge": {"radius_m": 100}})


def test_exact_avl_dedup_stream_keeps_all_apc_and_complete_mapping(
    dedup_workspace, monkeypatch
):
    workspace = dedup_workspace

    def forbidden(*args, **kwargs):
        raise AssertionError("No trip or observation matching")

    monkeypatch.setattr(debug.segmentation, "segment_vehicle", forbidden)
    monkeypatch.setattr(debug.matcher, "build_index", forbidden)
    monkeypatch.setattr(debug.matcher, "match_direct", forbidden)
    before = {
        name: debug.evaluate.file_hash(workspace.audit / name)
        for name in ("avl.parquet", "apc.parquet", "original-avl.csv")
    }
    record = workspace.create_run({"method": "vehicle_only_dedup"})
    folder = workspace.get_run(record["run_id"])
    summary = record["summary"]
    assert summary["source_counts"] == {"avl": 6, "apc": 4}
    assert summary["retained_counts"] == {"avl": 5, "apc": 4}
    assert summary["observations"] == 9 and summary["removed_avl"] == 1
    assert summary["exact_duplicate_groups"] == 1
    assert summary["duplicate_involving_rows"] == 2
    assert not any(summary["violations"].values())
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["avl_deduplication"]
    assert manifest["deduplication"]["fields"] == list(debug.avl_dedup.RAW_FIELDS)
    assert (
        debug.evaluate.file_hash(folder / manifest["deduplication"]["raw_fields_path"])
        == manifest["deduplication"]["raw_fields_sha256"]
    )
    mapping = pl.read_parquet(
        folder / "deduplication" / f"{debug._case('9027')}.parquet"
    )
    removed = mapping.filter(~pl.col("is_representative")).to_dicts()
    assert len(removed) == 1
    assert removed[0]["source_rid"] == "a-duplicate"
    assert removed[0]["representative_rid"] == "p1"
    assert removed[0]["representative_source_row"] == 3
    empty = pl.read_parquet(
        folder / "deduplication" / f"{debug._case('apc-only')}.parquet"
    )
    assert empty.height == 0 and empty.schema == debug.avl_dedup.MAPPING_SCHEMA
    assert all(
        debug.evaluate.file_hash(workspace.audit / name) == digest
        for name, digest in before.items()
    )


def test_stream_to_dedup_comparison_explains_duplicates_not_missing_data(
    dedup_workspace,
):
    workspace = dedup_workspace
    original = workspace.create_run({"method": "vehicle_only"})
    parent = workspace.get_run(original["run_id"])
    parent_files = {
        p: debug.evaluate.file_hash(p) for p in parent.rglob("*") if p.is_file()
    }
    result = workspace.create_run(
        {"method": "vehicle_only_dedup"}, parent_run_id=original["run_id"]
    )
    compared = workspace.compare(original["run_id"], result["run_id"])
    summary = compared["summary"]
    assert compared["avl_deduplication_comparison"]
    assert summary["observations_removed"] == summary["dedup_explained_removals"] == 1
    assert summary["unexplained_removals"] == 0
    assert summary["source_lineage_conserved"] and summary["apc_unchanged"]
    assert summary["relative_order_preserved"]
    assert not summary["source_records_conserved"]
    assert summary["changed_rows"] == 1
    assert compared["changes"][0]["change_types"] == ["exact_avl_duplicate_removed"]
    assert compared["changes"][0]["representative_rid"] == "p1"
    scoped = workspace.compare(original["run_id"], result["run_id"], vehicle="9027")
    assert scoped["retained_counts"] == {
        "base": {"avl": 5, "apc": 3},
        "current": {"avl": 4, "apc": 3},
    }
    assert scoped["global_retained_counts"]["current"] == {"avl": 5, "apc": 4}
    backwards = workspace.compare(result["run_id"], original["run_id"])
    assert backwards["summary"]["dedup_explained_additions"] == 1
    assert backwards["summary"]["source_lineage_conserved"]
    assert all(
        debug.evaluate.file_hash(path) == digest
        for path, digest in parent_files.items()
    )


def test_dedup_without_verified_original_csv_cannot_run(workspace):
    with pytest.raises(ValueError, match="inventory"):
        workspace.create_run({"method": "vehicle_only_dedup"})
    assert not list((workspace.root / "runs").glob("*/completion.json"))
