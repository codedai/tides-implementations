"""Experiment 05 source/feed contracts on synthetic CSVs and real tiny ZIPs."""

import copy
import csv
import io
from datetime import UTC, date, datetime

import pytest

from tests.conftest import make_feed_files, write_gtfs_zip
from tests.unit.test_source_normalize import (
    APC_HEADER,
    AVL_HEADER,
    apc_row,
    avl_row,
)
from tools.experiment05 import sources
from tools.experiment05.config import load_config
from tools.experiment05.io import sha256
from tools.experiment05.projection import matching_input
from ttc_tides.reconcile.schedule import service_day_anchor


@pytest.fixture
def config():
    return load_config()[0]


def raw_record(source, content, ordinal=1, digest=None):
    header = AVL_HEADER if source == "avl" else APC_HEADER
    raw = next(csv.DictReader(io.StringIO(header + "\n" + content + "\n")))
    digest = digest or ("a" if source == "avl" else "b") * 64
    return {
        "source": source,
        "source_row": ordinal,
        "source_file_sha256": digest,
        "rid": f"{source}:{digest}:{ordinal}",
        "raw": raw,
    }


def avl(ts="2024-04-01 09:00:00.000", **kwargs):
    return avl_row(
        ts=ts,
        date_key=ts[:10].replace("-", ""),
        vehicle="0007",
        dest="NORTH - synthetic",
        tatrip="source-only",
        **kwargs,
    )


def apc(arrive="2024-04-01 09:00:10", depart="2024-04-01 09:00:20", **kwargs):
    return apc_row(
        arrive=arrive,
        depart=depart,
        vehicle="0007",
        stop="code-a",
        branch="branch-a",
        **kwargs,
    )


def test_independent_normalization_preserves_original_rows_and_raw_fields(config):
    raw = [raw_record("avl", avl(), 412), raw_record("apc", apc(), 99)]
    before = copy.deepcopy(raw)
    rows = sources.normalize_sources(raw, "2024-04-01", config)
    assert {r["rid"] for r in rows} == {r["rid"] for r in raw}
    assert {r["source_row"] for r in rows} == {99, 412}
    assert all(r["vehicle_id"] == "0007" and r["device_id"] is None for r in rows)
    assert all(r["trip_id_performed"] is None for r in rows)
    assert all(isinstance(r["payload_hash"], str) for r in rows)
    assert raw == before
    avl_row = next(r for r in rows if r["source"] == "avl")
    assert avl_row["t"] == datetime(2024, 4, 1, 13, tzinfo=UTC).timestamp()
    assert avl_row["source_trip"] == "source-only"
    apc_row = next(r for r in rows if r["source"] == "apc")
    assert "avg_otp" not in apc_row and apc_row["audit_avg_otp"] == -270.0
    assert apc_row["route_name"] == "DUFFERIN"
    assert apc_row["stop"] == "code-a"


def test_exact_duplicates_annotated_without_collapsing_apc_or_conflicting_avl(config):
    same = avl()
    raw = [
        raw_record("avl", same, 20),
        raw_record("avl", same, 30),
        raw_record("avl", avl(lat="43.66"), 40),
        raw_record("apc", apc(), 50),
        raw_record("apc", apc(), 60),
    ]
    normalized = sources.normalize_sources(raw, "2024-04-01", config)
    before = copy.deepcopy(normalized)
    timeline = sources.event_timeline(normalized)
    assert len(timeline) == 5
    assert sum(r["duplicate_of"] is not None for r in timeline) == 1
    assert sum(r["matching_eligible"] for r in timeline) == 4
    assert all(r["duplicate_of"] is None for r in timeline if r["source"] == "apc")
    assert normalized == before
    assert sources.event_timeline(list(reversed(normalized))) == timeline
    with pytest.raises(ValueError, match="source-row identities"):
        sources.event_timeline(normalized + normalized[:1])


@pytest.mark.parametrize(
    "value,status,expected",
    [
        (None, "MISSING", None),
        ("bad-clock", "INVALID", None),
        ("2024-11-03 01:30:00", "AMBIGUOUS", None),
        ("2024-03-10 02:30:00", "NONEXISTENT", None),
        ("2024-11-03T01:30:00-04:00", "EXPLICIT_OFFSET", "2024-11-03T05:30:00+00:00"),
        ("2024-11-03T01:30:00-05:00", "EXPLICIT_OFFSET", "2024-11-03T06:30:00+00:00"),
    ],
)
def test_utc_clock_does_not_guess_dst_fold(value, status, expected):
    instant, observed_status = sources.utc_clock(value, "America/Toronto")
    assert observed_status == status
    assert instant == (
        datetime.fromisoformat(expected).timestamp() if expected else None
    )


def test_apc_departure_ambiguity_is_not_erased_by_unique_arrival(config):
    raw = [raw_record("apc", apc("2024-11-03 00:59:00", "2024-11-03 01:01:00"))]
    row = sources.normalize_sources(raw, "2024-11-03", config)[0]
    assert row["arrival_timestamp_status"] == "UNIQUE_LOCAL_TIME"
    assert row["departure_timestamp_status"] == "AMBIGUOUS"
    assert row["departure"] is None
    assert row["time_ambiguous"] is True


def test_raw_clock_and_service_date_hypotheses_are_not_scheduled_assignments(config):
    raw = [
        raw_record("avl", avl("2024-04-01 01:20:00.000"), 47),
        raw_record("avl", avl("2024-04-01 14:20:00.000"), 48),
    ]
    rows = sources.normalize_sources(raw, "2024-04-01", config)
    assert rows[0]["service_date_hypotheses"] == ["2024-03-31", "2024-04-01"]
    assert rows[1]["service_date_hypotheses"] == ["2024-04-01"]
    assert all(r.get("trip_id_scheduled") is None for r in rows)


def test_matching_allowlist_excludes_raw_schedule_identity_features(config):
    left = sources.normalize_sources(
        [raw_record("apc", apc(avg_otp="123"))], "2024-04-01", config
    )[0]
    right = sources.normalize_sources(
        [raw_record("apc", apc(avg_otp="-90000"))], "2024-04-01", config
    )[0]
    assert left["raw_payload_sha256"] != right["raw_payload_sha256"]
    assert matching_input(left) == matching_input(right)
    assert "audit_avg_otp" not in matching_input(left)
    assert "raw_payload" not in matching_input(left)


def test_original_csv_day_filter_preserves_ordinals_invalid_avl_and_all_routes(
    tmp_path, config
):
    avl_path, apc_path = tmp_path / "avl.csv", tmp_path / "apc.csv"
    invalid = avl_row(ts="invalid", date_key="20240401", vehicle="0007")
    avl_path.write_text(
        AVL_HEADER
        + "\n"
        + "\n".join(
            [
                avl("2024-03-31 23:59:00.000"),
                avl(),
                avl(route="929"),
                invalid,
                avl("2024-04-02 00:01:00.000"),
            ]
        )
        + "\n"
    )
    apc_path.write_text(
        APC_HEADER
        + "\n"
        + "\n".join(
            [
                apc("2024-03-31 23:59:00", "2024-03-31 23:59:10"),
                apc(),
                apc("bad", "2024-04-01 10:01:00"),
            ]
        )
        + "\n"
    )
    config["inputs"].update(avl_path="avl.csv", apc_path="apc.csv")
    pins = {"avl": sha256(avl_path), "apc": sha256(apc_path)}
    raw, counts = sources.read_raw_day(tmp_path, "2024-04-01", config, pins)
    assert counts == {"avl": 3, "apc": 2}
    assert [r["source_row"] for r in raw if r["source"] == "avl"] == [2, 3, 4]
    assert [r["source_row"] for r in raw if r["source"] == "apc"] == [2, 3]
    timeline = sources.event_timeline(
        sources.normalize_sources(raw, "2024-04-01", config)
    )
    assert len(timeline) == 5
    assert sum(not r["route_eligible"] for r in timeline) == 1
    assert sum(r["t"] is None for r in timeline) == 1
    assert {"avl": sha256(avl_path), "apc": sha256(apc_path)} == pins
    available = sources.available_dates(tmp_path, config)
    assert any(
        r["service_date"] == "2024-04-01" and r["raw_available"] for r in available
    )
    with pytest.raises(ValueError, match="changed before"):
        sources.read_raw_day(tmp_path, "2024-04-01", config, pins | {"avl": "0" * 64})


@pytest.fixture
def dated_feed_root(tmp_path, config):
    rows = []
    for index, (start, end) in enumerate(
        [("20241101", "20241103"), ("20241104", "20241106")]
    ):
        route = f"route-{index}"
        path = tmp_path / f"feed-{index}.zip"
        files = make_feed_files(
            routes_txt="route_id,agency_id,route_short_name,route_long_name,route_type\n"
            + f"{route},1,29,Synthetic route,3\n",
            calendar_txt="service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            + f"S,1,1,1,1,1,1,1,{start},{end}\n",
            calendar_dates_txt=None,
            trips_txt="route_id,service_id,trip_id,direction_id,shape_id,block_id\n"
            + f"{route},S,trip-{index},0,SH1,B\n",
            stop_times_txt="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            + f"trip-{index},25:00:00,25:00:10,S1,1\n"
            + f"trip-{index},25:10:00,25:10:00,S2,2\n",
        )
        write_gtfs_zip(path, files)
        rows.append(
            {
                "feed_id": f"feed-{index}",
                "relative_path": path.name,
                "archive_sha256": sha256(path),
                "priority": 1,
                "status": "approved",
                "calendar_min": datetime.strptime(start, "%Y%m%d").date().isoformat(),
                "calendar_max": datetime.strptime(end, "%Y%m%d").date().isoformat(),
            }
        )
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    config["inputs"].update(feed_manifest_path="manifest.csv", feed_gtfs_root=".")
    return tmp_path, config


@pytest.mark.parametrize("day,index", [("2024-11-03", 0), ("2024-11-04", 1)])
def test_date_active_real_zip_feed_and_after_midnight_utc(dated_feed_root, day, index):
    root, config = dated_feed_root
    bundle = sources.load_active_feed(root, day, "29", config)
    assert bundle["feed_version"] == f"feed-{index}"
    assert bundle["route_id"] == f"route-{index}"
    assert {trip["service_date"] for trip in bundle["trips"]} == {day}
    stop = bundle["trips"][0]["stop_times"][0]
    assert stop["arrival_seconds"] == 90000
    anchor = service_day_anchor(date.fromisoformat(day), "America/Toronto").timestamp()
    assert stop["arrival_t"] == anchor + 90000
    assert stop["departure_t"] - stop["arrival_t"] == 10
    assert "UTC epoch" in bundle["clock"]
    assert bundle["validity_period"]["start"] <= day <= bundle["validity_period"]["end"]


def test_no_active_feed_fails_instead_of_reusing_a_previous_date(dated_feed_root):
    root, config = dated_feed_root
    with pytest.raises(ValueError, match="active feed"):
        sources.load_active_feed(root, "2024-11-07", "29", config)
