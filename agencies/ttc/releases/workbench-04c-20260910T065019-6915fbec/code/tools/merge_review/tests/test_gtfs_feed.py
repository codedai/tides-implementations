"""Pinned feed selection, active calendars, and extended-hour provenance."""

import csv
import io
import zipfile

import pytest

from tools.merge_review.gtfs_matching.feed import file_sha256, gtfs_seconds, load_feed


def fixture(
    tmp_path, *, status="approved", exceptions="service_id,date,exception_type\n"
):
    tables = {
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
            "start_date,end_date\ns,1,1,1,1,1,1,1,20231101,20231130\n"
        ),
        "calendar_dates.txt": exceptions,
        "routes.txt": "route_id,route_short_name\nr,29\nx,929\n",
        "trips.txt": (
            "route_id,service_id,trip_id,direction_id,shape_id\n"
            "r,s,t,1,h\nx,s,other,1,h\n"
        ),
        "stops.txt": (
            "stop_id,stop_code,stop_lat,stop_lon\n"
            "a,1652,43.65,-79.4\nb,1652,43.66,-79.4\n"
        ),
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "t,25:00:00,25:01:00,a,1\nt,25:10:00,25:10:00,b,2\n"
            "other,12:00:00,12:00:00,a,1\n"
        ),
        "shapes.txt": (
            "shape_id,shape_pt_sequence,shape_pt_lat,shape_pt_lon\n"
            "h,1,43.65,-79.4\nh,2,43.66,-79.4\n"
        ),
    }
    archive = tmp_path / "feed.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, value in tables.items():
            zf.writestr(name, value)
    manifest = tmp_path / "manifest.csv"
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "feed_id",
            "relative_path",
            "archive_sha256",
            "calendar_min",
            "calendar_max",
            "priority",
            "status",
        ],
    )
    writer.writeheader()
    writer.writerow(
        dict(
            feed_id="test-feed",
            relative_path="feed.zip",
            archive_sha256=file_sha256(archive),
            calendar_min="2023-11-01",
            calendar_max="2023-11-30",
            priority=1,
            status=status,
        )
    )
    manifest.write_text(buffer.getvalue())
    return dict(feed_manifest_path=str(manifest), feed_gtfs_root=str(tmp_path)), archive


def test_active_feed_route_identity_tail_and_stop_code_ambiguity(tmp_path):
    cfg, _ = fixture(tmp_path)
    result = load_feed("2023-11-06", "29", cfg)
    assert result["route_id"] == "r"
    assert result["active_service_ids"] == ["s"]
    assert len(result["trips"]) == 2
    assert {t["service_date"] for t in result["trips"]} == {"2023-11-05", "2023-11-06"}
    assert all(t["trip_id"] == "t" for t in result["trips"])
    assert result["stop_code_crosswalk"] == {"1652": ["a", "b"]}
    current, previous = result["trips"]
    assert current["stop_times"][0]["t"] - previous["stop_times"][0]["t"] == 86400
    assert current["stop_times"][0]["arrival_seconds"] == 90000
    assert current["trip_key"] != previous["trip_key"]


def test_draft_requires_opt_in_and_is_never_promoted(tmp_path):
    cfg, _ = fixture(tmp_path, status="candidate")
    with pytest.raises(ValueError, match="opt-in"):
        load_feed("2023-11-06", config=cfg)
    cfg["feed_allow_candidate"] = True
    result = load_feed("2023-11-06", config=cfg)
    assert result["selection"]["provenance_unreviewed"] is True


def test_calendar_exception_removes_service(tmp_path):
    cfg, _ = fixture(
        tmp_path, exceptions="service_id,date,exception_type\ns,20231106,2\n"
    )
    with pytest.raises(ValueError, match="No permitted"):
        load_feed("2023-11-06", config=cfg)


def test_archive_pin_change_rejected(tmp_path):
    cfg, archive = fixture(tmp_path)
    with archive.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="SHA-256"):
        load_feed("2023-11-06", config=cfg)


@pytest.mark.parametrize("clock", ["25:60:00", "-1:00:00", "12:30", "NaN"])
def test_invalid_gtfs_clock_not_silently_wrapped(clock):
    with pytest.raises(ValueError):
        gtfs_seconds(clock)


def test_extended_clock_and_missing_clock():
    assert gtfs_seconds("49:02:03") == 176523
    assert gtfs_seconds("") is None
