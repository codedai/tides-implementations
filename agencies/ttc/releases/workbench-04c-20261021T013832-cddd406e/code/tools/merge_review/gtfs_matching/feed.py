"""Read pinned, date-active GTFS without mixing feed or route namespaces."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from tools.merge_review.paths import project_root

ROOT = project_root()
DEFAULTS = {
    "feed_manifest_path": str(ROOT / "artifacts/feed_manifest.draft.csv"),
    "feed_gtfs_root": str(ROOT / "database_offline/gtfs"),
    "feed_allow_candidate": False,
    "feed_id": None,
    "feed_include_previous_day_tail": True,
}
EPOCH = datetime(1970, 1, 1)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows(archive, member):
    if member not in archive.namelist():
        return iter(())
    stream = io.TextIOWrapper(archive.open(member), encoding="utf-8-sig", newline="")
    return csv.DictReader(stream)


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def gtfs_seconds(value):
    """Extended GTFS hours are preserved; 25:00:00 is 90000, never 01:00."""
    if value in (None, ""):
        return None
    match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d)", str(value))
    if match is None:
        raise ValueError(f"Invalid GTFS clock: {value!r}")
    hour, minute, second = map(int, match.groups())
    return hour * 3600 + minute * 60 + second


def active_services(archive, service_date):
    """Expand the weekday calendar and then apply explicit date exceptions."""
    day = _day(service_date)
    day_key = day.strftime("%Y%m%d")
    weekday = day.strftime("%A").lower()
    active = set()
    for row in _rows(archive, "calendar.txt"):
        if row["start_date"] <= day_key <= row["end_date"]:
            if row.get(weekday) == "1":
                active.add(row["service_id"])
    seen = set()
    for row in _rows(archive, "calendar_dates.txt"):
        if row.get("date") != day_key:
            continue
        if row["service_id"] in seen:
            raise ValueError("Duplicate calendar exception for one service/date")
        seen.add(row["service_id"])
        if row.get("exception_type") == "1":
            active.add(row["service_id"])
        elif row.get("exception_type") == "2":
            active.discard(row["service_id"])
        else:
            raise ValueError("Invalid calendar exception_type")
    return sorted(active)


def _safe_archive(root, relative_path):
    root = Path(root).resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Feed path is missing or outside its source root")
    return path


def _unique(rows, key):
    result = {}
    for row in rows:
        identity = row.get(key)
        if not identity or identity in result:
            raise ValueError(f"Missing or duplicate GTFS {key}")
        result[identity] = row
    return result


def load_feed(service_date, route="29", config=None):
    """Load one pinned feed, active trips, and all shapes used by its route.

    Draft manifests require explicit opt-in. Previous-day scheduled tail trips
    are optional schedule evidence, not imported previous-day GPS observations.
    """
    cfg = DEFAULTS | (config or {})
    day = _day(service_date)
    for key in ("feed_allow_candidate", "feed_include_previous_day_tail"):
        if not isinstance(cfg[key], bool):
            raise ValueError(f"{key} must be a boolean")
    manifest_path = Path(cfg["feed_manifest_path"])
    with manifest_path.open(newline="", encoding="utf-8-sig") as stream:
        manifest = list(csv.DictReader(stream))
    candidates = []
    for item in manifest:
        lower, upper = item.get("calendar_min"), item.get("calendar_max")
        if lower and not lower <= day.isoformat() <= upper:
            continue
        if item.get("status") not in {"approved", "candidate"}:
            continue
        path = _safe_archive(cfg["feed_gtfs_root"], item["relative_path"])
        digest = file_sha256(path)
        if digest != item["archive_sha256"]:
            raise ValueError("GTFS archive differs from pinned manifest SHA-256")
        with zipfile.ZipFile(path) as archive:
            if len(archive.namelist()) != len(set(archive.namelist())):
                raise ValueError("Duplicate GTFS ZIP members")
            services = active_services(archive, day)
        if services:
            candidates.append(
                dict(
                    item,
                    path=str(path),
                    priority=int(item["priority"]),
                    active_service_ids=services,
                )
            )
    eligible = [
        r
        for r in candidates
        if r["status"] == "approved" or cfg["feed_allow_candidate"]
    ]
    if cfg["feed_id"] is not None:
        eligible = [r for r in eligible if r["feed_id"] == cfg["feed_id"]]
    if not eligible:
        raise ValueError("No permitted active feed; draft requires explicit opt-in")
    eligible.sort(key=lambda r: (-r["priority"], r["feed_id"]))
    if len(eligible) > 1 and eligible[0]["priority"] == eligible[1]["priority"]:
        raise ValueError("Unresolved GTFS feed priority tie; explicitly pin a feed")
    selected = eligible[0]
    version = selected["feed_id"]
    with zipfile.ZipFile(selected["path"]) as archive:
        required = {
            "routes.txt",
            "trips.txt",
            "stops.txt",
            "stop_times.txt",
            "shapes.txt",
        }
        if not required.issubset(archive.namelist()):
            raise ValueError("GTFS reference feed lacks required tables/shapes")
        routes = [
            r
            for r in _rows(archive, "routes.txt")
            if r.get("route_short_name") == str(route)
        ]
        if len(routes) != 1:
            raise ValueError("Public route must resolve to exactly one feed route_id")
        route_row = routes[0]
        route_id = route_row["route_id"]
        reference_trips = list(
            _unique(
                (r for r in _rows(archive, "trips.txt") if r["route_id"] == route_id),
                "trip_id",
            ).values()
        )
        trip_ids = {r["trip_id"] for r in reference_trips}
        stop_times = {key: [] for key in trip_ids}
        for row in _rows(archive, "stop_times.txt"):
            if row["trip_id"] in trip_ids:
                item = dict(row, stop_sequence=int(row["stop_sequence"]))
                item["arrival_seconds"] = gtfs_seconds(row.get("arrival_time"))
                item["departure_seconds"] = gtfs_seconds(row.get("departure_time"))
                stop_times[row["trip_id"]].append(item)
        for times in stop_times.values():
            times.sort(key=lambda r: r["stop_sequence"])
            if len({r["stop_sequence"] for r in times}) != len(times):
                raise ValueError("Duplicate trip stop_sequence")
        stop_ids = {r["stop_id"] for times in stop_times.values() for r in times}
        stops = list(
            _unique(
                (r for r in _rows(archive, "stops.txt") if r["stop_id"] in stop_ids),
                "stop_id",
            ).values()
        )
        if {r["stop_id"] for r in stops} != stop_ids:
            raise ValueError("Referenced GTFS stop is missing")
        for row in stops:
            row["lat"], row["lon"] = float(row["stop_lat"]), float(row["stop_lon"])
        shape_ids = {r["shape_id"] for r in reference_trips}
        shape_points = {key: [] for key in shape_ids}
        for row in _rows(archive, "shapes.txt"):
            if row["shape_id"] in shape_ids:
                shape_points[row["shape_id"]].append(
                    dict(
                        sequence=int(row["shape_pt_sequence"]),
                        lat=float(row["shape_pt_lat"]),
                        lon=float(row["shape_pt_lon"]),
                    )
                )
        shapes = []
        for shape_id, points in sorted(shape_points.items()):
            points.sort(key=lambda r: r["sequence"])
            if len(points) < 2 or len({p["sequence"] for p in points}) != len(points):
                raise ValueError("Missing or duplicate geometry for referenced shape")
            shapes.append(dict(shape_id=shape_id, points=points))
        days = [day]
        if cfg["feed_include_previous_day_tail"]:
            days.append(day - timedelta(days=1))
        trips = []
        day_start = (datetime.combine(day, datetime.min.time()) - EPOCH).total_seconds()
        service_by_day = {}
        for scheduled_day in days:
            services = set(active_services(archive, scheduled_day))
            service_by_day[scheduled_day.isoformat()] = sorted(services)
            offset = (
                datetime.combine(scheduled_day, datetime.min.time()) - EPOCH
            ).total_seconds()
            for original in reference_trips:
                if original["service_id"] not in services:
                    continue
                scheduled = []
                for stop in stop_times[original["trip_id"]]:
                    a, d = stop["arrival_seconds"], stop["departure_seconds"]
                    scheduled.append(
                        dict(
                            stop,
                            arrival_t=None if a is None else offset + a,
                            departure_t=None if d is None else offset + d,
                            t=offset + a
                            if a is not None
                            else offset + d
                            if d is not None
                            else None,
                        )
                    )
                instants = [
                    r[k]
                    for r in scheduled
                    for k in ("arrival_t", "departure_t")
                    if r[k] is not None
                ]
                if scheduled_day != day and (not instants or max(instants) < day_start):
                    continue
                trips.append(
                    dict(
                        original,
                        feed_version=version,
                        service_date=scheduled_day.isoformat(),
                        trip_key=f"{version}/{scheduled_day.isoformat()}/{original['trip_id']}",
                        stop_times=scheduled,
                    )
                )
        for row in reference_trips:
            row["stop_times"] = stop_times[row["trip_id"]]
    if file_sha256(selected["path"]) != selected["archive_sha256"]:
        raise ValueError("GTFS input changed while reading")
    stop_code_crosswalk = {}
    for stop in stops:
        if stop.get("stop_code"):
            stop_code_crosswalk.setdefault(stop["stop_code"], []).append(
                stop["stop_id"]
            )
    return dict(
        feed_version=version,
        archive_sha256=selected["archive_sha256"],
        feed_path=selected["path"],
        manifest_path=str(manifest_path.resolve()),
        manifest_sha256=file_sha256(manifest_path),
        date=day.isoformat(),
        route_id=route_id,
        route_short_name=str(route),
        route=route_row,
        active_service_ids=selected["active_service_ids"],
        service_by_day=service_by_day,
        trips=trips,
        reference_trips=reference_trips,
        stops=stops,
        shapes=shapes,
        stop_code_crosswalk={k: sorted(v) for k, v in stop_code_crosswalk.items()},
        selection=dict(
            selected_feed=version,
            explicit_pin=cfg["feed_id"],
            candidates=[
                {
                    k: r[k]
                    for k in (
                        "feed_id",
                        "priority",
                        "status",
                        "archive_sha256",
                        "active_service_ids",
                    )
                }
                for r in candidates
            ],
            provenance_unreviewed=selected["status"] != "approved",
        ),
        diagnostics=dict(
            active_trips=len(trips),
            route_shapes=len(shapes),
            previous_day_tail_trips=sum(
                t["service_date"] != day.isoformat() for t in trips
            ),
            direction_id_assumed_north_south=False,
        ),
    )
