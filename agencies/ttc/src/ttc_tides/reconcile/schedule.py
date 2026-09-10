"""Daily service expansion, scheduled trips/stop-time events, and patterns.

Python equivalents of the WMATA warehouse models:

- :func:`expand_daily_services` mirrors ``int_gtfs_calendar_long`` +
  ``int_gtfs_daily_services`` (calendar expanded to daily rows, with
  ``calendar_dates.txt`` exceptions taking precedence);
- :func:`scheduled_trips_for_date` and :func:`scheduled_stop_events_for_date`
  mirror ``fct_scheduled_trips`` / ``fct_scheduled_stop_times``, including the
  GTFS "noon minus 12 hours" anchor so post-midnight and DST-transition
  service resolves to correct wall-clock datetimes;
- :func:`derive_patterns` mirrors ``int_gtfs_patterns``: a deterministic
  ``pattern_id`` from route, direction, and the ordered stop path
  (``shape_id`` is never treated as a pattern synonym).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from zoneinfo import ZoneInfo

import polars as pl

from ..ingest.gtfs import FatalGtfsError
from ..normalize.gtfs import NormalizedFeed

DEFAULT_TIMEZONE = "America/Toronto"

#: Version prefix baked into every pattern identifier. Bump when the
#: derivation algorithm changes.
PATTERN_ID_VERSION = "p2"

_UTC = dt.UTC

_WEEKDAY_COLUMNS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def service_day_anchor(service_date: dt.date, timezone: str) -> dt.datetime:
    """GTFS time origin for a service date: local noon minus 12 hours (UTC).

    Using noon avoids the ambiguity of local midnight on daylight-saving
    transition days; GTFS times are elapsed seconds from this instant.
    """
    try:
        zone = ZoneInfo(timezone)
    except (KeyError, ValueError) as exc:
        raise FatalGtfsError(f"unknown IANA timezone: {timezone!r}") from exc
    noon = dt.datetime.combine(service_date, dt.time(12, 0), tzinfo=zone)
    return noon.astimezone(_UTC) - dt.timedelta(hours=12)


def expand_daily_services(
    calendar: pl.DataFrame | None, calendar_dates: pl.DataFrame | None
) -> pl.DataFrame:
    """Expand calendar rows to (service_id, service_date) and apply exceptions.

    ``calendar_dates.txt`` takes precedence over ``calendar.txt``: type 1
    adds service on a date, type 2 removes it.
    """
    if calendar is None or calendar.is_empty():
        weekly = pl.DataFrame(schema={"service_id": pl.String, "service_date": pl.Date})
    else:
        weekly = (
            calendar.filter(pl.col("start_date") <= pl.col("end_date"))
            .with_columns(
                pl.date_ranges("start_date", "end_date", interval="1d").alias(
                    "service_date"
                )
            )
            .explode("service_date", empty_as_null=True)
        )
        dow = pl.col("service_date").dt.weekday()  # ISO: 1 = Mon .. 7 = Sun
        has_service = pl.lit(False)
        for iso_dow, column in enumerate(_WEEKDAY_COLUMNS, start=1):
            has_service = (
                pl.when(dow == iso_dow).then(pl.col(column) == 1).otherwise(has_service)
            )
        weekly = weekly.filter(has_service).select("service_id", "service_date")

    if calendar_dates is None or calendar_dates.is_empty():
        return weekly.unique().sort("service_id", "service_date")

    added = calendar_dates.filter(pl.col("exception_type") == 1).select(
        "service_id", pl.col("date").alias("service_date")
    )
    removed = calendar_dates.filter(pl.col("exception_type") == 2).select(
        "service_id", pl.col("date").alias("service_date")
    )
    return (
        pl.concat([weekly, added])
        .unique()
        .join(removed, on=["service_id", "service_date"], how="anti")
        .sort("service_id", "service_date")
    )


def feed_daily_services(feed: NormalizedFeed) -> pl.DataFrame:
    """Expanded daily services for one normalized feed, tagged with feed_id."""
    calendar = feed.table("calendar.txt") if "calendar.txt" in feed.members else None
    calendar_dates = (
        feed.members["calendar_dates.txt"].normalized
        if "calendar_dates.txt" in feed.members
        else None
    )
    return expand_daily_services(calendar, calendar_dates).with_columns(
        pl.lit(feed.archive.feed_id).alias("feed_id")
    )


def scheduled_trips_for_date(
    feed: NormalizedFeed,
    service_date: dt.date,
    *,
    daily_services: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Trips scheduled to operate on one service date in one feed.

    Route context is joined in so downstream matching can use the public
    ``route_short_name`` with the version-specific ``route_id``.
    """
    services = (
        daily_services if daily_services is not None else feed_daily_services(feed)
    )
    active = services.filter(pl.col("service_date") == service_date).select(
        "service_id"
    )
    trips = feed.table("trips.txt")
    routes = feed.table("routes.txt").select(
        "route_id", "route_short_name", "route_long_name", "route_type"
    )
    return (
        trips.join(active, on="service_id", how="inner")
        .join(routes, on="route_id", how="left")
        .with_columns(
            pl.lit(feed.archive.feed_id).alias("feed_id"),
            pl.lit(service_date).alias("service_date"),
        )
        .select(
            "feed_id",
            "service_date",
            "trip_id",
            "service_id",
            "route_id",
            "route_short_name",
            "route_long_name",
            "route_type",
            "direction_id",
            "block_id",
            "shape_id",
            "trip_headsign",
        )
        .sort("trip_id")
    )


def scheduled_stop_events_for_date(
    feed: NormalizedFeed,
    service_date: dt.date,
    *,
    timezone: str = DEFAULT_TIMEZONE,
    daily_services: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Stop-level scheduled events for one service date, with wall-clock times.

    Arrival/departure datetimes are the service-day anchor (local noon minus
    12 h) plus the parsed GTFS seconds, evaluated in UTC and presented in the
    agency timezone — correct across DST transitions and for times >= 24:00.
    """
    trips = scheduled_trips_for_date(
        feed, service_date, daily_services=daily_services
    ).select("trip_id", "service_id", "route_id", "route_short_name", "direction_id")
    stop_times = feed.table("stop_times.txt")
    stops = feed.table("stops.txt").select("stop_id", "stop_code", "stop_name")

    anchor = service_day_anchor(service_date, timezone)
    anchor_expr = (
        pl.lit(anchor.replace(tzinfo=None))
        .cast(pl.Datetime("us"))
        .dt.replace_time_zone("UTC")
    )

    def event_time(seconds_column: str, alias: str) -> pl.Expr:
        return (
            (anchor_expr + pl.duration(seconds=pl.col(seconds_column)))
            .dt.convert_time_zone(timezone)
            .alias(alias)
        )

    return (
        stop_times.join(trips, on="trip_id", how="inner")
        .join(stops, on="stop_id", how="left")
        .with_columns(
            pl.lit(feed.archive.feed_id).alias("feed_id"),
            pl.lit(service_date).alias("service_date"),
            event_time("arrival_time_secs", "arrival_datetime"),
            event_time("departure_time_secs", "departure_datetime"),
        )
        .select(
            "feed_id",
            "service_date",
            "trip_id",
            "route_id",
            "route_short_name",
            "direction_id",
            "stop_sequence",
            "stop_id",
            "stop_code",
            "stop_name",
            "arrival_time",
            "departure_time",
            "arrival_time_secs",
            "departure_time_secs",
            "arrival_datetime",
            "departure_datetime",
        )
        .sort("trip_id", "stop_sequence")
    )


def _pattern_id(route_identity: str, direction_key: str, path_key: str) -> str:
    key = json.dumps(
        [route_identity, direction_key, path_key],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # A 128-bit prefix keeps identifiers compact while making accidental
    # collision risk negligible even when patterns accumulate across feeds.
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"{PATTERN_ID_VERSION}-{digest}"


def _encoded_string(column: str) -> pl.Expr:
    """Length-prefix a nullable string so concatenated keys cannot collide."""
    value = pl.col(column)
    return (
        pl.when(value.is_null())
        .then(pl.lit("-"))
        .otherwise(
            pl.concat_str([value.str.len_bytes().cast(pl.String), pl.lit(":"), value])
        )
    )


def derive_patterns(feed: NormalizedFeed) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Deterministic stop patterns for every trip in a feed.

    Returns ``(patterns, trip_patterns)``. A pattern is the ordered stop path
    of a trip within its route/direction context; repeated stops (loops) are
    preserved. The identifier is a versioned content hash, so identical
    patterns get identical IDs across feeds and reruns.
    """
    stop_times = feed.table("stop_times.txt")
    routes = feed.table("routes.txt").select(
        "route_id", "agency_id", "route_short_name", "route_long_name"
    )
    trips = (
        feed.table("trips.txt")
        .select("trip_id", "route_id", "direction_id")
        .join(routes, on="route_id", how="left")
    )

    trip_paths = (
        stop_times.with_columns(_encoded_string("stop_id").alias("_stop_token"))
        .sort("trip_id", "stop_sequence")
        .group_by("trip_id", maintain_order=True)
        .agg(
            pl.col("stop_id").alias("stop_path"),
            pl.col("_stop_token").str.join("").alias("stop_path_key"),
        )
        # Orphan stop_times remain visible in the quality report but cannot
        # become a route-less matched pattern.
        .join(trips, on="trip_id", how="inner")
        .with_columns(
            pl.when(
                pl.col("route_short_name").is_not_null()
                | pl.col("route_long_name").is_not_null()
            )
            .then(
                pl.concat_str(
                    [
                        pl.lit("public:"),
                        _encoded_string("agency_id"),
                        _encoded_string("route_short_name"),
                        _encoded_string("route_long_name"),
                    ]
                )
            )
            .otherwise(
                pl.concat_str([pl.lit("route-id:"), _encoded_string("route_id")])
            )
            .alias("route_identity"),
            pl.when(pl.col("direction_id").is_null())
            .then(pl.lit("null"))
            .otherwise(
                pl.concat_str([pl.lit("int:"), pl.col("direction_id").cast(pl.String)])
            )
            .alias("direction_key"),
        )
    )

    pattern_keys = trip_paths.select(
        "route_identity", "direction_key", "stop_path_key"
    ).unique()
    pattern_keys = pattern_keys.with_columns(
        pl.struct("route_identity", "direction_key", "stop_path_key")
        .map_elements(
            lambda s: _pattern_id(
                s["route_identity"], s["direction_key"], s["stop_path_key"]
            ),
            return_dtype=pl.String,
        )
        .alias("pattern_id")
    )
    trip_patterns = trip_paths.join(
        pattern_keys,
        on=["route_identity", "direction_key", "stop_path_key"],
        how="left",
    ).with_columns(pl.lit(feed.archive.feed_id).alias("feed_id"))

    patterns = (
        trip_patterns.group_by("pattern_id", "route_identity", "direction_key")
        .agg(
            pl.len().alias("trip_count"),
            pl.col("stop_path").first().alias("stop_path"),
            pl.col("stop_path_key").first().alias("stop_path_key"),
            pl.col("route_id").first().alias("route_id"),
            pl.col("route_id").unique().sort().alias("route_ids"),
            pl.col("direction_id").first().alias("direction_id"),
        )
        .with_columns(
            pl.col("stop_path").list.len().alias("stop_count"),
            pl.lit(feed.archive.feed_id).alias("feed_id"),
            pl.lit(PATTERN_ID_VERSION).alias("pattern_algorithm_version"),
        )
        .sort("pattern_id")
    )
    return patterns, trip_patterns.select(
        "feed_id",
        "trip_id",
        "pattern_id",
        "route_id",
        "direction_id",
        "route_identity",
    ).sort("trip_id")
