"""Raw-to-day rebuilds must not depend on any historical audit or experiment."""

import json
import stat
from datetime import datetime

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from experiments.apc_avl_coverage_study.raw_audit import normalize_raw
from tools.merge_review import raw_bootstrap
from tools.merge_review.avl_dedup import file_sha256, load_raw_avl_fields
from tools.merge_review.debug import DebugWorkspace


@pytest.fixture
def pinned_raw(tmp_path):
    avl = {
        "Date_Key": "20231106",
        "Message_DateTime": "2023-11-06 00:00:01.000",
        "Vehicle": "V1",
        "Latitude": "43.65",
        "Longitude": "-79.4",
        "Heading": "0",
        "Route": "29",
        "Run": "1",
        "Destination": "NORTH - synthetic",
        "OffRoute": "false",
        "Delayed": "false",
        "KPH": "10",
        "tatripId": "audit-only-id",
    }
    avl_rows = [
        dict(avl, Message_DateTime="2023-11-05 23:59:59.999"),
        dict(avl),
        dict(avl),
        dict(avl, Message_DateTime="2023-11-07 00:00:00.000"),
        dict(avl, Message_DateTime="not a clock"),
        dict(
            avl,
            Message_DateTime=" 2023-11-06 23:59:59.123 ",
            Latitude="not a coordinate",
            Route="929",
        ),
    ]
    apc = {
        "": "0",
        "DAYTYPE": "Mo",
        "Route": "29",
        "RouteName": "Original route name",
        "Branch": "TEST",
        "VehicleID": "V1",
        "RouteDirection": "NORTH",
        "StopID": "STOP",
        "ONSTREET": "A",
        "ATSTREET": "B",
        "Lat": "43.65",
        "Lon": "-79.4",
        "PeriodID": "Night",
        "StopArrivalTime": "2023-11-06 23:59:59",
        "StopDepartureTime": "2023-11-07 00:00:02",
        "NoTrips": "1",
        "AvgDwell": "3",
        "AvgOTP": "234.5",
        "AvgSample": "1",
        "Arrload": "0",
        "Boarding": "1.5",
        "Alighting": "0",
        "Depload": "1.5",
    }
    apc_rows = [
        dict(
            apc,
            StopArrivalTime="2023-11-05 23:59:59",
            StopDepartureTime="2023-11-06 00:00:02",
        ),
        dict(apc),
        dict(apc, StopArrivalTime=None, StopDepartureTime="2023-11-06 00:01:00"),
        dict(
            apc,
            StopArrivalTime="malformed",
            StopDepartureTime="2023-11-06 00:02:00",
            Route="929",
        ),
        dict(apc, StopDepartureTime="malformed"),
        dict(apc, StopArrivalTime="2023-11-07 00:00:00"),
    ]
    paths = {source: tmp_path / f"{source}.csv" for source in ("avl", "apc")}
    for source, rows in (("avl", avl_rows), ("apc", apc_rows)):
        pl.from_dicts(rows).write_csv(paths[source])
    return paths, {source: file_sha256(path) for source, path in paths.items()}


def bootstrap(tmp_path, pinned_raw, **kwargs):
    paths, hashes = pinned_raw
    return raw_bootstrap.bootstrap_raw_day(
        tmp_path / "new-audit",
        paths["avl"],
        paths["apc"],
        expected_hashes=hashes,
        **kwargs,
    )


def test_day_selection_equals_full_csv_normalization_then_filter(tmp_path, pinned_raw):
    paths, hashes = pinned_raw
    result = bootstrap(tmp_path, pinned_raw)
    assert result["source_counts"] == {"avl": 3, "apc": 4}
    start, end = datetime(2023, 11, 6), datetime(2023, 11, 7)
    for source, path in paths.items():
        raw = pl.read_csv(path, infer_schema=False, try_parse_dates=False)
        full = normalize_raw(raw, source, hashes[source])
        membership = (
            pl.col("time") if source == "avl" else pl.coalesce("arrival", "departure")
        )
        expected = full.filter(membership.is_between(start, end, closed="left"))
        actual = pl.read_parquet(tmp_path / "new-audit" / f"{source}.parquet")
        assert_frame_equal(actual, expected)
        assert actual["source_row"].dtype == pl.UInt32
        assert actual["payload_hash"].dtype == pl.UInt64
        assert actual["source_row"].to_list() == (
            [2, 3, 6] if source == "avl" else [2, 3, 4, 5]
        )
        assert "929" in actual["route"].to_list()
        assert file_sha256(path) == hashes[source]
    inventory = result["inventory"]
    assert inventory["validation_ok"]
    assert inventory["normalization"]["polars_version"] == pl.__version__
    assert stat.S_IMODE((tmp_path / "new-audit").stat().st_mode) == 0o700
    for name, digest in inventory["output_sha256"].items():
        output = tmp_path / "new-audit" / name
        assert file_sha256(output) == digest
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_fresh_audit_supports_fixture_route_names_and_exact_dedup(tmp_path, pinned_raw):
    result = bootstrap(tmp_path, pinned_raw)
    workspace = DebugWorkspace(tmp_path / "debug", result["path"])
    fixture = workspace.ensure_fixture()
    avl = pl.read_parquet(fixture / "avl.parquet")
    apc = pl.read_parquet(fixture / "apc.parquet")
    assert apc["route_name"].to_list() == ["Original route name"] * 4
    assert apc["avg_otp"].to_list() == [234.5] * 4
    assert apc["departure"].max() == datetime(2023, 11, 7, 0, 0, 2)
    raw, definition = load_raw_avl_fields(workspace.audit, avl)
    assert raw["source_row"].to_list() == [2, 3, 6]
    assert definition["raw_csv_sha256"] == pinned_raw[1]["avl"]
    direction = workspace.create_run({"method": "direction_destination"})
    assert direction["summary"]["validation_ok"]
    assert direction["summary"]["deduplicated_counts"]["avl"] == 2


@pytest.mark.parametrize("date", ["2023-11-05", "2023-11-07", "invalid", None])
def test_rejects_dates_outside_current_workbench_scope(tmp_path, pinned_raw, date):
    with pytest.raises(ValueError, match="currently supports"):
        bootstrap(tmp_path, pinned_raw, date=date)
    assert not (tmp_path / "new-audit").exists()


@pytest.mark.parametrize("pins", [None, {}, {"avl": "bad", "apc": "0" * 64}])
def test_requires_both_valid_hash_pins(tmp_path, pinned_raw, pins):
    paths, _ = pinned_raw
    with pytest.raises(ValueError, match="SHA-256"):
        raw_bootstrap.bootstrap_raw_day(
            tmp_path / "new-audit", paths["avl"], paths["apc"], expected_hashes=pins
        )
    assert not (tmp_path / "new-audit").exists()


def test_wrong_hash_fails_before_any_output(tmp_path, pinned_raw):
    paths, hashes = pinned_raw
    with pytest.raises(ValueError, match="differs from its pinned"):
        bootstrap(tmp_path, (paths, hashes | {"apc": "0" * 64}))
    assert not (tmp_path / "new-audit").exists()


def test_existing_output_is_never_overwritten(tmp_path, pinned_raw):
    result = bootstrap(tmp_path, pinned_raw)
    original = (tmp_path / "new-audit" / "inventory.json").read_bytes()
    with pytest.raises(ValueError, match="new directory"):
        bootstrap(tmp_path, pinned_raw)
    assert (tmp_path / "new-audit" / "inventory.json").read_bytes() == original
    assert json.loads(original) == result["inventory"]


def test_source_mutation_does_not_publish_valid_inventory(
    tmp_path, pinned_raw, monkeypatch
):
    original = raw_bootstrap._normalize_day

    def mutate(path, source, *args):
        result = original(path, source, *args)
        if source == "apc":
            with path.open("a") as stream:
                stream.write("\n")
        return result

    monkeypatch.setattr(raw_bootstrap, "_normalize_day", mutate)
    with pytest.raises(ValueError, match="changed during bootstrap"):
        bootstrap(tmp_path, pinned_raw)
    assert not (tmp_path / "new-audit" / "inventory.json").exists()


def test_empty_day_preserves_canonical_schema(tmp_path, pinned_raw):
    paths, _ = pinned_raw
    for path in paths.values():
        raw = pl.read_csv(path, infer_schema=False).head(1)
        raw.write_csv(path)
    hashes = {source: file_sha256(path) for source, path in paths.items()}
    result = bootstrap(tmp_path, (paths, hashes))
    assert result["source_counts"] == {"avl": 0, "apc": 0}
    for source in paths:
        frame = pl.read_parquet(tmp_path / "new-audit" / f"{source}.parquet")
        assert frame["rid"].dtype == pl.String
        assert frame["source_row"].dtype == pl.UInt32
