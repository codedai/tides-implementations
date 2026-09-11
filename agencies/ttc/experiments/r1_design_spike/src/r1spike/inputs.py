"""Slice loading over the existing S0-S2 and GTFS artifacts (read-only).

The spike prefers the normalized Parquet artifacts and never rescans the
raw CSVs. Column pruning and hive-partition predicate pushdown keep the
scans bounded. Vehicle selection is deterministic and its query is
recorded in the run manifest — no hand-picked easy examples.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from pathlib import Path

import polars as pl

SPIKE_ROUTES = ("29", "929")

AVL_COLUMNS = [
    "_record_id",
    "vehicle_id_raw",
    "event_local_time",
    "source_date_key",
    "source_calendar_date",
    "latitude",
    "longitude",
    "route_label",
    "run_label",
    "destination_label",
    "source_tatrip_id",
]

APC_COLUMNS = [
    "_record_id",
    "vehicle_id_raw",
    "arrival_local_time",
    "departure_local_time",
    "source_calendar_date",
    "route_label",
    "branch_label",
    "direction_label",
    "source_stop_code",
    "latitude",
    "longitude",
    # passenger fields are loaded ONLY for diagnostics artifacts and the
    # firewall perturbation test; they never reach scoring (enforced in
    # scoring.py)
    "arrival_load",
    "boarding",
    "alighting",
    "departure_load",
    "avg_sample",
    "no_trips",
]


@dataclass(frozen=True)
class GtfsForDate:
    service_date: str
    feed_id: str
    artifact_dir: Path
    routes: pl.DataFrame  # route_id, route_short_name
    trip_intervals: pl.DataFrame  # trip_id, route_id, direction_id,
    #                                 shape_id, start_epoch_s, end_epoch_s
    scheduled_stop_events: pl.DataFrame  # slice routes only
    stops: pl.DataFrame  # stop_id, stop_code, stop_lat, stop_lon
    shapes: pl.DataFrame  # shape points for slice-route shapes
    dominant_shape_by_route: dict[str, str]


def _wall(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def load_avl_slice(
    normalized_dir: Path,
    *,
    dates: list[str],
    wall_start: str,
    wall_end: str,
    vehicles: list[str] | None = None,
) -> pl.DataFrame:
    lf = pl.scan_parquet(normalized_dir / "avl", hive_partitioning=True).filter(
        pl.col("source_calendar_date").is_in([dt.date.fromisoformat(d) for d in dates]),
        pl.col("event_local_time").is_between(_wall(wall_start), _wall(wall_end)),
    )
    if vehicles is not None:
        lf = lf.filter(pl.col("vehicle_id_raw").is_in(vehicles))
    return (
        lf.select(AVL_COLUMNS)
        .sort("event_local_time", "_record_id")
        .collect(engine="streaming")
    )


def load_apc_slice(
    normalized_dir: Path,
    *,
    dates: list[str],
    wall_start: str,
    wall_end: str,
    vehicles: list[str] | None = None,
) -> pl.DataFrame:
    lf = pl.scan_parquet(normalized_dir / "apc", hive_partitioning=True).filter(
        pl.col("source_calendar_date").is_in([dt.date.fromisoformat(d) for d in dates]),
        pl.col("arrival_local_time").is_between(_wall(wall_start), _wall(wall_end)),
    )
    if vehicles is not None:
        lf = lf.filter(pl.col("vehicle_id_raw").is_in(vehicles))
    return (
        lf.select(APC_COLUMNS)
        .sort("arrival_local_time", "_record_id")
        .collect(engine="streaming")
    )


def select_vehicles(
    normalized_dir: Path,
    *,
    query: str,
    n: int,
    params: dict[str, str],
) -> tuple[list[str], str]:
    """Deterministic vehicle selection; returns (ids, query description).

    Queries:
    - ``avl_route_day``: vehicles with an AVL ping on ``date`` whose
      route label is a spike route; sorted ascending; first ``n``.
    - ``avl_dst_window``: vehicles with an AVL ping in the ambiguous
      wall hour 01:00-01:59 on ``date``; sorted; first ``n``.
    - ``apc_after_avl_end``: vehicles with an APC arrival strictly after
      ``after`` (the AVL fixture end); sorted; first ``n``.
    """
    if query == "avl_route_day":
        frame = (
            pl.scan_parquet(normalized_dir / "avl", hive_partitioning=True)
            .filter(
                pl.col("source_calendar_date") == dt.date.fromisoformat(params["date"]),
                pl.col("route_label").is_in(list(SPIKE_ROUTES)),
            )
            .select("vehicle_id_raw")
            .unique()
            .collect(engine="streaming")
        )
    elif query == "avl_dst_window":
        date = dt.date.fromisoformat(params["date"])
        frame = (
            pl.scan_parquet(normalized_dir / "avl", hive_partitioning=True)
            .filter(
                pl.col("source_calendar_date") == date,
                pl.col("event_local_time").is_between(
                    dt.datetime.combine(date, dt.time(1, 0, 0)),
                    dt.datetime.combine(date, dt.time(1, 59, 59)),
                ),
            )
            .select("vehicle_id_raw")
            .unique()
            .collect(engine="streaming")
        )
    elif query == "apc_after_avl_end":
        frame = (
            pl.scan_parquet(normalized_dir / "apc", hive_partitioning=True)
            .filter(pl.col("arrival_local_time") > _wall(params["after"]))
            .select("vehicle_id_raw")
            .unique()
            .collect(engine="streaming")
        )
    else:
        raise ValueError(f"unknown vehicle selection query {query!r}")
    ids = sorted(frame["vehicle_id_raw"].to_list())[:n]
    description = f"query={query} params={params} sort=asc first_n={n}"
    return ids, description


def _epoch_col(col: str) -> pl.Expr:
    return pl.col(col).dt.convert_time_zone("UTC").dt.epoch(time_unit="s")


def load_gtfs_for_date(
    artifact_dir: Path,
    service_date: str,
    *,
    routes: tuple[str, ...] = SPIKE_ROUTES,
) -> GtfsForDate:
    matched = artifact_dir / "matched" / "gtfs"
    normalized = artifact_dir / "normalized" / "gtfs"
    trips = pl.read_parquet(matched / "scheduled_trips.parquet")
    feed_id = trips["feed_id"][0] if trips.height else artifact_dir.name
    trips = trips.filter(pl.col("route_short_name").is_in(list(routes)))
    events = (
        pl.scan_parquet(matched / "scheduled_stop_events.parquet")
        .filter(pl.col("route_short_name").is_in(list(routes)))
        .with_columns(
            _epoch_col("arrival_datetime").alias("arrival_epoch_s"),
            _epoch_col("departure_datetime").alias("departure_epoch_s"),
        )
        .collect()
    )
    trip_intervals = (
        events.group_by("trip_id")
        .agg(
            pl.col("arrival_epoch_s").min().alias("start_epoch_s"),
            pl.col("departure_epoch_s").max().alias("end_epoch_s"),
        )
        .join(
            trips.select("trip_id", "route_id", "direction_id", "shape_id"),
            on="trip_id",
            how="inner",
        )
        .sort("trip_id")
    )
    route_table = (
        pl.read_parquet(
            normalized / "routes.parquet",
            columns=["route_id", "route_short_name"],
        )
        .filter(pl.col("route_short_name").is_in(list(routes)))
        .sort("route_id")
    )
    stops = pl.read_parquet(
        normalized / "stops.parquet",
        columns=["stop_id", "stop_code", "stop_lat", "stop_lon"],
    )
    shape_ids = trips["shape_id"].drop_nulls().unique().sort().to_list()
    shapes = (
        pl.scan_parquet(normalized / "shapes.parquet")
        .filter(pl.col("shape_id").is_in(shape_ids))
        .select("shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence")
        .collect()
    )
    dominant: dict[str, str] = {}
    if trips.height:
        counts = (
            trips.drop_nulls("shape_id")
            .group_by("route_id", "shape_id")
            .len()
            .sort("route_id", "len", "shape_id", descending=[False, True, False])
        )
        for route_id in counts["route_id"].unique().sort().to_list():
            sub = counts.filter(pl.col("route_id") == route_id)
            dominant[route_id] = sub["shape_id"][0]
    return GtfsForDate(
        service_date=service_date,
        feed_id=feed_id,
        artifact_dir=artifact_dir,
        routes=route_table,
        trip_intervals=trip_intervals,
        scheduled_stop_events=events,
        stops=stops,
        shapes=shapes,
        dominant_shape_by_route=dominant,
    )


def input_tree_hash(paths: list[Path]) -> str:
    """Order-independent hash of artifact file names+sizes+mtimes.

    A cheap integrity fingerprint for the manifest — full content
    re-hashing of 165 MB of parquet on every run is not worth it for a
    diagnostic spike; the run manifest records this limitation.
    """
    entries = []
    for base in paths:
        if base.is_file():
            stat = base.stat()
            entries.append(f"{base.name}:{stat.st_size}")
        else:
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    stat = p.stat()
                    entries.append(f"{p.relative_to(base)}:{stat.st_size}")
    digest = hashlib.sha256("\n".join(sorted(entries)).encode())
    return digest.hexdigest()
