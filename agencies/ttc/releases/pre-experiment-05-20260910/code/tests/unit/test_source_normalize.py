"""Provisional S0-S2 diagnostics: AVL/APC landing, normalization, CLI."""

import json

import polars as pl
import pytest

from ttc_tides.cli import main
from ttc_tides.ingest.apc import inventory_apc_file
from ttc_tides.ingest.avl import inventory_avl_file
from ttc_tides.ingest.sources import FatalSourceError
from ttc_tides.normalize.apc import normalize_apc
from ttc_tides.normalize.avl import normalize_avl
from ttc_tides.pipeline import run_diagnostic

AVL_HEADER = (
    "Date_Key,Message_DateTime,Vehicle,Latitude,Longitude,Heading,Route,Run,"
    "Destination,OffRoute,Delayed,KPH,tatripId"
)
APC_HEADER = (
    ",DAYTYPE,Route,RouteName,Branch,VehicleID,RouteDirection,StopID,ONSTREET,"
    "ATSTREET,Lat,Lon,PeriodID,StopArrivalTime,StopDepartureTime,NoTrips,"
    "AvgDwell,AvgOTP,AvgSample,Arrload,Boarding,Alighting,Depload"
)


def avl_row(
    *,
    date_key="20231106",
    ts="2023-11-06 09:00:00.000",
    vehicle="9024",
    lat="43.65",
    lon="-79.40",
    heading="120",
    route="29",
    run="2",
    dest="NORTH - 29 DUFFERIN",
    off_route="false",
    delayed="false",
    kph="25",
    tatrip="45973000",
):
    return (
        f"{date_key},{ts},{vehicle},{lat},{lon},{heading},{route},{run},"
        f"{dest},{off_route},{delayed},{kph},{tatrip}"
    )


def apc_row(
    *,
    idx="0",
    daytype="Mo",
    route="29",
    name="DUFFERIN",
    branch="29Dcon",
    vehicle="9027",
    direction="NORTH",
    stop="14363",
    onstreet="A St",
    atstreet="B St",
    lat="43.65",
    lon="-79.40",
    period="Morning",
    arrive="2023-11-06 09:00:30",
    depart="2023-11-06 09:00:45",
    no_trips="1",
    avg_dwell="0",
    avg_otp="-270",
    avg_sample="1",
    arrload="5.0",
    boarding="2",
    alighting="1",
    depload="6.0",
):
    return (
        f"{idx},{daytype},{route},{name},{branch},{vehicle},{direction},{stop},"
        f"{onstreet},{atstreet},{lat},{lon},{period},{arrive},{depart},"
        f"{no_trips},{avg_dwell},{avg_otp},{avg_sample},{arrload},{boarding},"
        f"{alighting},{depload}"
    )


def write_avl(path, rows, *, bom=True, crlf=True):
    newline = "\r\n" if crlf else "\n"
    body = newline.join([AVL_HEADER, *rows]) + newline
    data = body.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    return path


def write_apc(path, rows):
    path.write_text("\n".join([APC_HEADER, *rows]) + "\n")
    return path


@pytest.fixture
def avl_file(tmp_path):
    def make(rows, name="avl.csv", **kwargs):
        return write_avl(tmp_path / name, rows, **kwargs)

    return make


@pytest.fixture
def apc_file(tmp_path):
    def make(rows, name="apc.csv"):
        return write_apc(tmp_path / name, rows)

    return make


class TestAvlLanding:
    def test_bom_crlf_and_record_identity(self, avl_file):
        path = avl_file([avl_row(), avl_row(ts="2023-11-06 09:00:20.000")])
        source = inventory_avl_file(path)
        result = normalize_avl(source)
        assert result.landing_rows == 2
        ids = result.accepted["_record_id"].to_list()
        assert all(i.startswith(f"a1-{source.sha256[:32]}-") for i in ids)
        assert ids[0].endswith("-1")
        # deterministic across reruns
        again = normalize_avl(inventory_avl_file(path))
        assert again.accepted["_record_id"].to_list() == ids

    def test_missing_required_column_is_fatal(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text("Date_Key,Vehicle\n20231106,9024\n")
        with pytest.raises(FatalSourceError, match="required column"):
            inventory_avl_file(bad)


class TestAvlNormalize:
    def test_timestamps_stay_naive_and_typed(self, avl_file):
        result = normalize_avl(inventory_avl_file(avl_file([avl_row()])))
        schema = result.accepted.schema
        assert schema["event_local_time"] == pl.Datetime("us")  # no timezone
        assert schema["event_local_time"].time_zone is None
        assert schema["source_date_key"] == pl.Date
        assert schema["off_route"] == pl.Boolean
        assert result.accepted["message_datetime_raw"].to_list() == [
            "2023-11-06 09:00:00.000"
        ]

    def test_structural_rejections(self, avl_file):
        rows = [
            avl_row(),
            avl_row(ts="not-a-time"),
            avl_row(date_key="2023-11-06"),
            avl_row(vehicle=" "),
            avl_row(lat="95.0"),
            avl_row(kph="-4"),
        ]
        result = normalize_avl(inventory_avl_file(avl_file(rows)))
        assert result.accepted.height == 1
        assert result.rejected.height == 5
        assert result.reason_counts == {
            "AVL.PARSE.COORD_OUT_OF_RANGE": 1,
            "AVL.PARSE.INVALID_DATE_KEY": 1,
            "AVL.PARSE.INVALID_TIMESTAMP": 1,
            "AVL.PARSE.MISSING_VEHICLE": 1,
            "AVL.PARSE.NEGATIVE_SPEED": 1,
        }
        result.check_conservation()

    def test_unknown_bool_token_flags_not_rejects(self, avl_file):
        result = normalize_avl(
            inventory_avl_file(avl_file([avl_row(off_route="maybe")]))
        )
        assert result.accepted.height == 1
        assert result.accepted["off_route"].to_list() == [None]
        assert result.flag_counts["_flag_unknown_bool_token"] == 1

    def test_envelope_and_speed_flags_are_non_destructive(self, avl_file):
        result = normalize_avl(
            inventory_avl_file(avl_file([avl_row(lat="44.9", lon="-80.0", kph="200")]))
        )
        assert result.accepted.height == 1
        assert result.flag_counts["_flag_speed_implausible"] == 1

    def test_duplicate_payload_annotated_not_dropped(self, avl_file):
        result = normalize_avl(inventory_avl_file(avl_file([avl_row(), avl_row()])))
        assert result.accepted.height == 2
        assert result.accepted["_flag_duplicate_payload"].to_list() == [
            True,
            True,
        ]

    def test_date_key_mismatch_is_a_metric_not_rejection(self, avl_file):
        result = normalize_avl(
            inventory_avl_file(
                avl_file([avl_row(date_key="20231105")])  # ts is Nov 6
            )
        )
        assert result.accepted.height == 1
        assert result.metrics["date_key_equals_calendar_date_violations"] == 1


class TestApcNormalize:
    def test_export_index_preserved_and_typed(self, apc_file):
        result = normalize_apc(inventory_apc_file(apc_file([apc_row(idx="7")])))
        row = result.accepted.row(0, named=True)
        assert row["source_export_id"] == "7"
        assert row["source_stop_code"] == "14363"
        assert row["avg_otp"] == -270.0
        assert result.accepted.schema["arrival_local_time"].time_zone is None
        assert row["_record_id"].startswith("c1-")

    def test_inverted_interval_flagged_not_repaired(self, apc_file):
        result = normalize_apc(
            inventory_apc_file(
                apc_file(
                    [
                        apc_row(
                            arrive="2023-11-06 09:10:00",
                            depart="2023-11-06 09:00:00",
                        )
                    ]
                )
            )
        )
        assert result.accepted.height == 1
        assert result.flag_counts["_flag_interval_inverted"] == 1
        row = result.accepted.row(0, named=True)
        assert row["departure_local_time"] < row["arrival_local_time"]

    def test_negative_count_and_load_equation_flags(self, apc_file):
        result = normalize_apc(
            inventory_apc_file(
                apc_file(
                    [
                        apc_row(boarding="-2"),
                        apc_row(idx="1", depload="99.0"),
                    ]
                )
            )
        )
        assert result.accepted.height == 2
        assert result.flag_counts["_flag_negative_count"] == 1
        assert result.flag_counts["_flag_load_equation_violated"] >= 1

    def test_visit_key_collision_distinct_from_duplicate(self, apc_file):
        rows = [
            apc_row(),
            apc_row(idx="1", boarding="9"),  # same visit key, differing payload
            apc_row(idx="2", vehicle="1111"),
        ]
        result = normalize_apc(inventory_apc_file(apc_file(rows)))
        assert result.accepted.height == 3
        assert result.flag_counts["_flag_visit_key_collision"] == 2
        assert result.flag_counts["_flag_duplicate_payload"] == 0

    def test_missing_stop_code_rejected(self, apc_file):
        result = normalize_apc(inventory_apc_file(apc_file([apc_row(stop="")])))
        assert result.rejected.height == 1
        assert "APC.PARSE.MISSING_STOP_CODE" in result.reason_counts


class TestDiagnosticRun:
    def test_writes_partitioned_artifacts_and_manifest(
        self, avl_file, apc_file, tmp_path
    ):
        avl = avl_file([avl_row(), avl_row(ts="2023-11-07 08:00:00.000")])
        apc = apc_file([apc_row()])
        out = tmp_path / "run1"
        result = run_diagnostic(avl_path=avl, apc_path=apc, out_dir=out)
        manifest = json.loads((out / "run_manifest.json").read_text())
        assert manifest["profile"] == "diagnostic-provisional"
        assert {i["source_type"] for i in manifest["inputs"]} == {"avl", "apc"}
        normalized = pl.read_parquet(out / "normalized" / "avl")
        assert normalized.height == 2
        assert (out / "reports" / "avl_quality_report.json").is_file()
        assert (out / "reports" / "apc_quality_report.json").is_file()
        assert result.sources[0].accepted.height == 2

    def test_refuses_overwrite_and_source_boundary(self, avl_file, tmp_path):
        avl = avl_file([avl_row()])
        out = tmp_path / "run2"
        run_diagnostic(avl_path=avl, apc_path=None, out_dir=out)
        with pytest.raises(FatalSourceError, match="refusing to overwrite"):
            run_diagnostic(avl_path=avl, apc_path=None, out_dir=out)
        boundary = tmp_path / "database_offline" / "generated"
        with pytest.raises(FatalSourceError, match="source-data boundary"):
            run_diagnostic(avl_path=avl, apc_path=None, out_dir=boundary)

    def test_cli_run_and_exit_codes(self, avl_file, apc_file, tmp_path, capsys):
        avl = avl_file([avl_row()])
        apc = apc_file([apc_row()])
        out = tmp_path / "cli-run"
        code = main(
            [
                "diagnostic",
                "run",
                "--avl",
                str(avl),
                "--apc",
                str(apc),
                "--out",
                str(out),
            ]
        )
        assert code == 0
        printed = capsys.readouterr().out
        assert "diagnostic-provisional" in printed
        assert "non-publishable" in printed
        # second run without --force fails cleanly through the CLI
        assert (
            main(
                [
                    "diagnostic",
                    "run",
                    "--avl",
                    str(avl),
                    "--out",
                    str(out),
                ]
            )
            == 1
        )

    def test_rejections_parquet_written(self, avl_file, tmp_path):
        avl = avl_file([avl_row(), avl_row(ts="broken")])
        out = tmp_path / "run3"
        run_diagnostic(avl_path=avl, apc_path=None, out_dir=out)
        rejected = pl.read_parquet(out / "rejections" / "avl.parquet")
        assert rejected.height == 1
        assert rejected["_rejection_reasons"].to_list() == [
            "AVL.PARSE.INVALID_TIMESTAMP"
        ]
