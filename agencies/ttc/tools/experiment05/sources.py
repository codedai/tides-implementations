"""Independent source normalization, explicit civil/service clocks, no joins."""

from __future__ import annotations

import collections
import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from experiments.apc_avl_coverage_study.raw_audit import normalize_raw
from tools.merge_review.gtfs_matching.feed import load_feed
from ttc_tides.reconcile.schedule import service_day_anchor

from .io import sha256

ROW = "__experiment05_source_row"


def parse_date(value):
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("service_date must use YYYY-MM-DD")
    return date.fromisoformat(value)


def source_paths(root, config):
    result = {}
    for source in ("avl", "apc"):
        path = Path(root) / config["inputs"][f"{source}_path"]
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing raw {source.upper()} input: {path}")
        result[source] = path.resolve()
    if result["avl"] == result["apc"]:
        raise ValueError("AVL and APC must be independent raw source files")
    return result


def _clock_expr(source):
    fields = (
        ("Message_DateTime",)
        if source == "avl"
        else ("StopArrivalTime", "StopDepartureTime")
    )
    return pl.coalesce(
        *[
            pl.col(field)
            .str.strip_chars()
            .str.to_datetime("%Y-%m-%d %H:%M:%S%.f", strict=False, time_unit="us")
            for field in fields
        ]
    )


def available_dates(root, config):
    """Read route-specific original CSV date columns, not historic run names."""
    sources = {}
    for source, path in source_paths(root, config).items():
        frame = (
            pl.scan_csv(path, infer_schema=False)
            .filter(pl.col("Route").str.strip_chars() == "29")
            .select(_clock_expr(source).dt.date().alias("date"))
            .group_by("date")
            .len()
            .collect(engine="streaming")
        )
        sources[source] = {
            row["date"].isoformat(): row["len"]
            for row in frame.to_dicts()
            if row["date"] is not None
        }
    days = sorted(set(sources["avl"]) | set(sources["apc"]))
    return [
        {
            "service_date": day,
            "avl_rows": sources["avl"].get(day, 0),
            "apc_rows": sources["apc"].get(day, 0),
            "raw_available": day in sources["avl"] and day in sources["apc"],
        }
        for day in days
    ]


def read_raw_day(root, service_date, config, expected_hashes):
    day = parse_date(service_date)
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    records, counts = [], {}
    for source, path in source_paths(root, config).items():
        if sha256(path) != expected_hashes[source]:
            raise ValueError(f"Raw {source} input changed before loading")
        query = pl.scan_csv(
            path, infer_schema=False, row_index_name=ROW, row_index_offset=1
        )
        membership = _clock_expr(source).is_between(start, end, closed="left")
        if source == "avl":
            membership |= _clock_expr(source).is_null() & (
                pl.col("Date_Key").str.strip_chars() == day.strftime("%Y%m%d")
            )
        frame = query.filter(membership).collect(engine="streaming")
        counts[source] = frame.height
        if frame.is_empty():
            raise ValueError(f"No raw {source.upper()} records for {service_date}")
        for row in frame.to_dicts():
            index = row.pop(ROW)
            records.append(
                {
                    "source": source,
                    "source_row": index,
                    "source_file_sha256": expected_hashes[source],
                    "rid": f"{source}:{expected_hashes[source]}:{index}",
                    "raw": row,
                }
            )
        if sha256(path) != expected_hashes[source]:
            raise ValueError(f"Raw {source} input changed during loading")
    return records, counts


def utc_clock(value, timezone):
    """Reject ambiguous/nonexistent civil clocks; never pick an invented fold."""
    if value is None:
        return None, "MISSING"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None, "INVALID"
    if value.tzinfo is not None:
        return value.timestamp(), "EXPLICIT_OFFSET"
    zone = ZoneInfo(timezone)
    alternatives = set()
    for fold in (0, 1):
        localized = value.replace(tzinfo=zone, fold=fold)
        converted = localized.astimezone(UTC)
        if converted.astimezone(zone).replace(tzinfo=None) == value:
            alternatives.add(converted.timestamp())
    if len(alternatives) != 1:
        return None, "AMBIGUOUS" if alternatives else "NONEXISTENT"
    return next(iter(alternatives)), "UNIQUE_LOCAL_TIME"


def _identifier(value):
    if value is None:
        return None
    value = str(value).strip()
    return value if value else None


def normalize_sources(raw_rows, service_date, config):
    timezone = config["normalization"]["timezone"]
    source_groups = collections.defaultdict(list)
    for row in raw_rows:
        source_groups[row["source"]].append(row)
    output = []
    for source, group in sorted(source_groups.items()):
        original = pl.DataFrame([row["raw"] for row in group], infer_schema_length=None)
        normalized = normalize_raw(original, source, group[0]["source_file_sha256"])
        for raw, row in zip(group, normalized.to_dicts(), strict=True):
            # Source row ordinals refer to the entire CSV, never the daily subset.
            row.update(
                {
                    key: raw[key]
                    for key in ("source", "source_row", "rid", "source_file_sha256")
                }
            )
            row["payload_hash"] = str(row["payload_hash"])
            row["raw_payload"] = raw["raw"]
            row["raw_payload_sha256"] = hashlib.sha256(
                json.dumps(raw["raw"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            row["vehicle_id"] = row["vehicle"] = _identifier(row.get("vehicle"))
            row["device_id"] = _identifier(raw["raw"].get("DeviceID"))
            row["timezone"] = timezone
            row["service_date"] = service_date
            row["source_rid"] = row["rid"]
            if source == "avl":
                row["raw_time"] = row["time"].isoformat() if row["time"] else None
                row["t"], row["timestamp_status"] = utc_clock(row["time"], timezone)
                row["time"] = row["event_time"] = row["t"]
                row["speed_kph"] = row.get("speed")
                text = str(row.get("destination") or "").strip().upper()
                label = text.split(maxsplit=1)[0] if text else None
            else:
                row["raw_arrival"] = (
                    row["arrival"].isoformat() if row["arrival"] else None
                )
                row["raw_departure"] = (
                    row["departure"].isoformat() if row["departure"] else None
                )
                row["arrival"], row["arrival_timestamp_status"] = utc_clock(
                    row["arrival"], timezone
                )
                row["departure"], row["departure_timestamp_status"] = utc_clock(
                    row["departure"], timezone
                )
                row["t"] = (
                    row["arrival"] if row["arrival"] is not None else row["departure"]
                )
                row["event_time"] = row["t"]
                row["timestamp_status"] = (
                    row["arrival_timestamp_status"]
                    if row["arrival"] is not None
                    else row["departure_timestamp_status"]
                )
                # Kept only in raw/audit data. Matching libraries never see it.
                row["audit_avg_otp"] = row.pop("avg_otp", None)
                label = str(row.get("direction") or "").strip().upper()
                row["route_name"] = raw["raw"].get("RouteName")
            row["direction_label"] = (
                label
                if label in {"NORTH", "SOUTH", "EAST", "WEST", "INBOUND", "OUTBOUND"}
                else None
            )
            statuses = (
                [row["timestamp_status"]]
                if source == "avl"
                else [
                    row["arrival_timestamp_status"],
                    row["departure_timestamp_status"],
                ]
            )
            row["time_ambiguous"] = "AMBIGUOUS" in statuses
            row["time_nonexistent"] = "NONEXISTENT" in statuses
            local = (
                datetime.fromtimestamp(row["t"], ZoneInfo(timezone))
                if row["t"] is not None
                else None
            )
            hypotheses = [service_date]
            if (
                local
                and local.hour
                < config["normalization"]["service_date_hypothesis_early_hours"]
            ):
                hypotheses.append((local.date() - timedelta(days=1)).isoformat())
            row["service_date_hypotheses"] = sorted(set(hypotheses))
            row["service_date_policy"] = (
                "selected calendar-day observations; "
                "only requested GTFS service date assigned"
            )
            row["route_eligible"] = str(row.get("route") or "").strip() == "29"
            row["trip_id_performed"] = None
            output.append(row)
    return output


def event_timeline(rows):
    """UNION ALL; duplicate AVL rows are annotated rather than erased."""
    output = sorted(
        (dict(row) for row in rows),
        key=lambda row: (
            row.get("vehicle_id") or "",
            row["t"] is None,
            row["t"] or 0,
            row["source"],
            row["rid"],
        ),
    )
    if len({row["rid"] for row in output}) != len(output):
        raise ValueError("Duplicate source-row identities in normalized inputs")
    seen = {}
    for row in output:
        duplicate = None
        if row["source"] == "avl":
            key = row["raw_payload_sha256"]
            duplicate = seen.get(key)
            seen.setdefault(key, row["rid"])
        row["duplicate_of"] = duplicate
        row["matching_eligible"] = bool(
            row["route_eligible"]
            and duplicate is None
            and row.get("vehicle_id")
            and row["t"] is not None
        )
        row["timeline_role"] = (
            "DUPLICATE_DIAGNOSTIC"
            if duplicate
            else ("ROUTE_EVIDENCE" if row["route_eligible"] else "OUT_OF_ROUTE_SCOPE")
        )
    return output


def load_active_feed(root, service_date, route_id, config):
    parse_date(service_date)
    cfg = dict(config["matcher"])
    cfg.update(
        feed_manifest_path=str(Path(root) / config["inputs"]["feed_manifest_path"]),
        feed_gtfs_root=str(Path(root) / config["inputs"]["feed_gtfs_root"]),
        feed_id=None,
        feed_include_previous_day_tail=False,
    )
    bundle = load_feed(service_date, str(route_id), cfg)
    timezone = config["normalization"]["timezone"]
    for trip in bundle["trips"]:
        anchor = service_day_anchor(
            parse_date(trip["service_date"]), timezone
        ).timestamp()
        for row in trip["stop_times"]:
            row["arrival_t"] = (
                None
                if row["arrival_seconds"] is None
                else anchor + row["arrival_seconds"]
            )
            row["departure_t"] = (
                None
                if row["departure_seconds"] is None
                else anchor + row["departure_seconds"]
            )
            row["t"] = (
                row["arrival_t"] if row["arrival_t"] is not None else row["departure_t"]
            )
    if not bundle["trips"]:
        raise ValueError(f"No active GTFS trips for {service_date}, route {route_id}")
    manifest = pl.read_csv(bundle["manifest_path"], infer_schema=False)
    selected = manifest.filter(pl.col("feed_id") == bundle["feed_version"]).to_dicts()[
        0
    ]
    bundle["validity_period"] = {
        "start": selected.get("calendar_min"),
        "end": selected.get("calendar_max"),
    }
    bundle["clock"] = "UTC epoch seconds; GTFS local-noon-minus-12h service anchor"
    return bundle
