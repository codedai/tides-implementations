"""Shared synthetic GTFS fixtures.

All fixtures are generated in-memory or under ``tmp_path``; no real TTC data
is used (see tests/README.md). The default feed intentionally exercises the
audited real-world properties: unpadded times, times past 24:00, stop_codes
distinct from stop_ids, and version-specific route_ids under a stable
route_short_name.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from ttc_tides.ingest.gtfs import inventory_archive

# November 2023 layout used across tests:
# 2023-11-05 is a Sunday (and the America/Toronto DST fall-back date),
# 2023-11-06 is a Monday, 2023-11-11 is a Saturday.

DEFAULT_FILES: dict[str, str] = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "1,TTC,http://www.ttc.ca,America/Toronto\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "WD,1,1,1,1,1,0,0,20231101,20231130\n"
        "SUN,0,0,0,0,0,0,1,20231101,20231130\n"
    ),
    "calendar_dates.txt": (
        "service_id,date,exception_type\n"
        "WD,20231111,1\n"  # adds weekday service on a Saturday
        "WD,20231106,2\n"  # removes weekday service on a Monday
    ),
    "routes.txt": (
        "route_id,agency_id,route_short_name,route_long_name,route_type\n"
        "70291,1,29,DUFFERIN,3\n"
        "70416,1,929,DUFFERIN EXPRESS,3\n"
    ),
    "stops.txt": (
        "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
        "S1,1001,First Stop,43.70,-79.40\n"
        "S2,1002,Second Stop,43.71,-79.41\n"
        "S3,1003,Third Stop,43.72,-79.42\n"
    ),
    "trips.txt": (
        "route_id,service_id,trip_id,trip_headsign,direction_id,block_id,shape_id\n"
        "70291,WD,T1,NORTH,0,B1,SH1\n"
        "70291,WD,T2,SOUTH,1,B1,SH1\n"
        "70291,SUN,T3,NORTH - LATE,0,B2,SH1\n"
    ),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,9:00:00,9:00:30,S1,1\n"
        "T1,9:10:00,9:10:30,S2,2\n"
        "T1,9:20:00,9:20:00,S3,3\n"
        "T2,10:00:00,10:00:00,S3,1\n"
        "T2,10:10:00,10:10:00,S2,2\n"
        "T2,10:20:00,10:20:00,S1,3\n"
        "T3,25:30:00,25:30:00,S1,1\n"  # post-midnight service
        "T3,25:40:00,25:40:00,S2,2\n"
        "T3,25:50:00,25:50:00,S3,3\n"
    ),
    "shapes.txt": (
        "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
        "SH1,43.70,-79.40,1\n"
        "SH1,43.72,-79.42,2\n"
    ),
}


def make_feed_files(**overrides: str | None) -> dict[str, str]:
    """The default synthetic feed with member contents replaced or removed.

    Pass ``member_txt="..."`` to replace ``member.txt`` and ``member_txt=None``
    to drop the member entirely.
    """
    files = dict(DEFAULT_FILES)
    for key, content in overrides.items():
        member = key.replace("_txt", ".txt")
        if content is None:
            files.pop(member, None)
        else:
            files[member] = content
    return files


def write_gtfs_zip(
    path: Path,
    files: dict[str, str],
    *,
    reverse_member_order: bool = False,
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    names = sorted(files, reverse=reverse_member_order)
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name in names:
            zf.writestr(name, files[name])
    return path


@pytest.fixture
def gtfs_zip_factory(tmp_path):
    """Build a synthetic GTFS zip; returns its path."""

    def make(name: str = "feed.zip", files: dict[str, str] | None = None, **kwargs):
        files = files if files is not None else make_feed_files()
        return write_gtfs_zip(tmp_path / name, files, **kwargs)

    return make


@pytest.fixture
def default_archive(gtfs_zip_factory):
    return inventory_archive(gtfs_zip_factory())
