"""GTFS preprocessing quality evaluation.

Offline analog of the WMATA Dagster asset checks and dbt tests: after a feed
is normalized, this module measures what the preprocessing kept, dropped, and
should worry about, and emits a machine-readable report.

Severity classes follow the quality contract:

- ``fatal`` findings mean the feed must not be used (structural breakage,
  unexplained row loss);
- ``quarantine`` findings count rows excluded by normalization, by stable
  reason code;
- ``warning`` findings are integrity and plausibility signals that do not
  block processing but must stay visible (referential gaps, non-monotonic
  stop times, zombie services, duplicate stop codes, ...).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import polars as pl

from ..normalize.gtfs import NormalizedFeed
from ..reconcile.schedule import feed_daily_services

Severity = str  # "fatal" | "quarantine" | "warning"

# Warning/fatal finding codes ---------------------------------------------------

CODE_ROW_CONSERVATION = "GTFS_ROW_CONSERVATION_BROKEN"
CODE_MEMBER_EMPTY = "GTFS_MEMBER_EMPTY"
CODE_REF_TRIP_ROUTE = "GTFS_TRIP_ROUTE_MISSING"
CODE_REF_TRIP_SERVICE = "GTFS_TRIP_SERVICE_MISSING"
CODE_REF_TRIP_SHAPE = "GTFS_TRIP_SHAPE_MISSING"
CODE_SHAPES_FILE_MISSING = "GTFS_SHAPES_FILE_MISSING"
CODE_REF_STOP_TIME_TRIP = "GTFS_STOP_TIME_TRIP_MISSING"
CODE_REF_STOP_TIME_STOP = "GTFS_STOP_TIME_STOP_MISSING"
CODE_TRIP_WITHOUT_STOP_TIMES = "GTFS_TRIP_WITHOUT_STOP_TIMES"
CODE_DEPARTURE_BEFORE_ARRIVAL = "GTFS_DEPARTURE_BEFORE_ARRIVAL"
CODE_NON_MONOTONIC_TIMES = "GTFS_NON_MONOTONIC_TIMES_IN_TRIP"
CODE_TIME_TRAVEL_BETWEEN_STOPS = "GTFS_TIME_TRAVEL_BETWEEN_STOPS"
CODE_STOP_SEQUENCE_NEGATIVE = "GTFS_STOP_SEQUENCE_NEGATIVE"
CODE_TERMINAL_TIME_MISSING = "GTFS_TERMINAL_TIME_MISSING"
CODE_DUPLICATE_STOP_CODE = "GTFS_DUPLICATE_STOP_CODE"
CODE_COORD_OUT_OF_RANGE = "GTFS_COORD_OUT_OF_RANGE"
CODE_CALENDAR_RANGE_INVERTED = "GTFS_CALENDAR_RANGE_INVERTED"
CODE_SERVICE_WITHOUT_TRIPS = "GTFS_SERVICE_WITHOUT_TRIPS"
CODE_SERVICE_NEVER_ACTIVE = "GTFS_TRIP_SERVICE_NEVER_ACTIVE"
CODE_DIRECTION_OUT_OF_RANGE = "GTFS_DIRECTION_ID_UNEXPECTED"


@dataclass(frozen=True)
class QualityFinding:
    code: str
    severity: Severity
    count: int
    description: str
    member: str | None = None


@dataclass
class FeedQualityReport:
    feed_id: str
    source_file: str
    archive_sha256: str
    content_hash: str
    member_counts: dict[str, dict[str, Any]] = field(default_factory=dict)
    findings: list[QualityFinding] = field(default_factory=list)

    def add(
        self,
        code: str,
        severity: Severity,
        count: int,
        description: str,
        member: str | None = None,
    ) -> None:
        if count > 0:
            self.findings.append(
                QualityFinding(code, severity, count, description, member)
            )

    @property
    def fatal_findings(self) -> list[QualityFinding]:
        return [f for f in self.findings if f.severity == "fatal"]

    @property
    def quarantined_rows(self) -> int:
        return sum(m["quarantined_rows"] for m in self.member_counts.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "feed_id": self.feed_id,
            "source_file": self.source_file,
            "archive_sha256": self.archive_sha256,
            "content_hash": self.content_hash,
            "members": self.member_counts,
            "findings": [asdict(f) for f in self.findings],
            "summary": {
                "fatal_findings": len(self.fatal_findings),
                "warning_findings": len(
                    [f for f in self.findings if f.severity == "warning"]
                ),
                "quarantined_rows": self.quarantined_rows,
            },
        }


def _anti_count(child: pl.DataFrame, parent: pl.DataFrame, on: str) -> int:
    """Rows in ``child`` whose non-null ``on`` value is absent from ``parent``."""
    return (
        child.filter(pl.col(on).is_not_null())
        .join(parent.select(on).unique(), on=on, how="anti")
        .height
    )


def evaluate_feed(feed: NormalizedFeed) -> FeedQualityReport:
    """Evaluate one normalized feed and return a quality report."""
    archive = feed.archive
    report = FeedQualityReport(
        feed_id=archive.feed_id,
        source_file=archive.source_file,
        archive_sha256=archive.archive_sha256,
        content_hash=archive.content_hash,
    )

    calendar_usable = bool(
        "calendar.txt" in feed.members
        and not feed.members["calendar.txt"].normalized.is_empty()
    )
    calendar_dates_usable = bool(
        "calendar_dates.txt" in feed.members
        and not feed.members["calendar_dates.txt"].normalized.is_empty()
    )

    # Per-member conservation and quarantine accounting -------------------------
    for name, member in sorted(feed.members.items()):
        conserved = (
            member.landing_rows == member.normalized.height + member.quarantined.height
        )
        report.member_counts[name] = {
            "landing_rows": member.landing_rows,
            "normalized_rows": member.normalized.height,
            "quarantined_rows": member.quarantined.height,
            "quarantine_reasons": member.reason_counts,
        }
        if not conserved:
            report.add(
                CODE_ROW_CONSERVATION,
                "fatal",
                1,
                f"{name}: landed rows != normalized + quarantined",
                member=name,
            )
        empty_severity = "fatal"
        if name in ("shapes.txt", "feed_info.txt"):
            empty_severity = "warning"
        elif name == "calendar.txt" and calendar_dates_usable:
            empty_severity = "warning"
        elif name == "calendar_dates.txt" and calendar_usable:
            empty_severity = "warning"
        report.add(
            CODE_MEMBER_EMPTY,
            empty_severity,
            int(member.normalized.is_empty()),
            f"{name}: member contains no usable normalized rows",
            member=name,
        )
        for reason, count in member.reason_counts.items():
            report.add(
                reason,
                "quarantine",
                count,
                f"{name}: rows quarantined by {reason}",
                member=name,
            )

    trips = feed.table("trips.txt")
    routes = feed.table("routes.txt")
    stops = feed.table("stops.txt")
    stop_times = feed.table("stop_times.txt")
    calendar = feed.table("calendar.txt") if "calendar.txt" in feed.members else None
    services = feed_daily_services(feed)

    # Referential integrity -----------------------------------------------------
    active_service_ids = services.select("service_id").unique()
    defined_frames = []
    if calendar is not None:
        defined_frames.append(calendar.select("service_id"))
    if "calendar_dates.txt" in feed.members:
        defined_frames.append(feed.table("calendar_dates.txt").select("service_id"))
    defined_service_ids = pl.concat(defined_frames).unique()
    report.add(
        CODE_REF_TRIP_ROUTE,
        "warning",
        _anti_count(trips, routes, "route_id"),
        "trips referencing a route_id absent from routes.txt",
        member="trips.txt",
    )
    report.add(
        CODE_REF_TRIP_SERVICE,
        "warning",
        _anti_count(trips, defined_service_ids, "service_id"),
        "trips referencing a service_id absent from calendar/calendar_dates",
        member="trips.txt",
    )
    report.add(
        CODE_REF_STOP_TIME_TRIP,
        "warning",
        _anti_count(stop_times, trips, "trip_id"),
        "stop_times referencing a trip_id absent from trips.txt",
        member="stop_times.txt",
    )
    report.add(
        CODE_REF_STOP_TIME_STOP,
        "warning",
        _anti_count(stop_times, stops, "stop_id"),
        "stop_times referencing a stop_id absent from stops.txt",
        member="stop_times.txt",
    )
    if "shapes.txt" in feed.members:
        report.add(
            CODE_REF_TRIP_SHAPE,
            "warning",
            _anti_count(trips, feed.table("shapes.txt"), "shape_id"),
            "trips referencing a shape_id absent from shapes.txt",
            member="trips.txt",
        )
    else:
        report.add(
            CODE_SHAPES_FILE_MISSING,
            "warning",
            trips.filter(pl.col("shape_id").is_not_null()).height,
            "trips reference shape_id but shapes.txt is absent",
            member="trips.txt",
        )
    report.add(
        CODE_TRIP_WITHOUT_STOP_TIMES,
        "warning",
        trips.select("trip_id")
        .join(stop_times.select("trip_id").unique(), on="trip_id", how="anti")
        .height,
        "trips with no rows in stop_times.txt",
        member="trips.txt",
    )

    # Stop-time plausibility ----------------------------------------------------
    both_times = (
        pl.col("arrival_time_secs").is_not_null()
        & pl.col("departure_time_secs").is_not_null()
    )
    report.add(
        CODE_DEPARTURE_BEFORE_ARRIVAL,
        "warning",
        stop_times.filter(
            both_times & (pl.col("departure_time_secs") < pl.col("arrival_time_secs"))
        ).height,
        "stop_times rows departing before they arrive",
        member="stop_times.txt",
    )
    non_monotonic = (
        stop_times.sort("trip_id", "stop_sequence")
        .filter(pl.col("departure_time_secs").is_not_null())
        .with_columns(
            pl.col("departure_time_secs")
            .cum_max()
            .over("trip_id")
            .alias("_max_departure_so_far")
        )
        .filter(pl.col("departure_time_secs") < pl.col("_max_departure_so_far"))
        .height
    )
    report.add(
        CODE_NON_MONOTONIC_TIMES,
        "warning",
        non_monotonic,
        "stop_times rows whose departure decreases along the trip",
        member="stop_times.txt",
    )
    time_travel = (
        stop_times.sort("trip_id", "stop_sequence")
        .with_columns(
            pl.col("departure_time_secs")
            .shift(1)
            .over("trip_id")
            .alias("_previous_departure")
        )
        .filter(
            pl.col("arrival_time_secs").is_not_null()
            & pl.col("_previous_departure").is_not_null()
            & (pl.col("arrival_time_secs") < pl.col("_previous_departure"))
        )
        .height
    )
    report.add(
        CODE_TIME_TRAVEL_BETWEEN_STOPS,
        "warning",
        time_travel,
        "stop arrivals earlier than the preceding stop departure",
        member="stop_times.txt",
    )
    report.add(
        CODE_STOP_SEQUENCE_NEGATIVE,
        "warning",
        stop_times.filter(pl.col("stop_sequence") < 0).height,
        "stop_times rows with a negative stop_sequence",
        member="stop_times.txt",
    )
    terminals = (
        stop_times.sort("trip_id", "stop_sequence")
        .group_by("trip_id", maintain_order=True)
        .agg(
            pl.col("arrival_time_secs").last().alias("_last_arrival"),
            pl.col("departure_time_secs").first().alias("_first_departure"),
        )
        .filter(
            pl.col("_last_arrival").is_null() | pl.col("_first_departure").is_null()
        )
        .height
    )
    report.add(
        CODE_TERMINAL_TIME_MISSING,
        "warning",
        terminals,
        "trips missing a first departure or last arrival time",
        member="stop_times.txt",
    )

    # Stops ---------------------------------------------------------------------
    duplicate_codes = (
        stops.filter(pl.col("stop_code").is_not_null())
        .group_by("stop_code")
        .len()
        .filter(pl.col("len") > 1)
        .height
    )
    report.add(
        CODE_DUPLICATE_STOP_CODE,
        "warning",
        duplicate_codes,
        "stop_code values shared by more than one stop "
        "(breaks a 1:1 APC stop-code crosswalk)",
        member="stops.txt",
    )
    report.add(
        CODE_COORD_OUT_OF_RANGE,
        "warning",
        stops.filter(
            (pl.col("stop_lat").is_not_null() & (pl.col("stop_lat").abs() > 90))
            | (pl.col("stop_lon").is_not_null() & (pl.col("stop_lon").abs() > 180))
        ).height,
        "stops with coordinates outside valid WGS-84 ranges",
        member="stops.txt",
    )

    # Calendar and service usage ------------------------------------------------
    report.add(
        CODE_CALENDAR_RANGE_INVERTED,
        "warning",
        (
            calendar.filter(pl.col("start_date") > pl.col("end_date")).height
            if calendar is not None
            else 0
        ),
        "calendar rows whose start_date is after end_date",
        member="calendar.txt" if calendar is not None else "calendar_dates.txt",
    )
    report.add(
        CODE_SERVICE_WITHOUT_TRIPS,
        "warning",
        active_service_ids.join(
            trips.select("service_id").unique(), on="service_id", how="anti"
        ).height,
        "service_ids with active days but no trips (zombie services)",
        member="calendar.txt" if calendar is not None else "calendar_dates.txt",
    )
    report.add(
        CODE_SERVICE_NEVER_ACTIVE,
        "warning",
        trips.select("service_id")
        .unique()
        .join(defined_service_ids, on="service_id", how="semi")
        .join(active_service_ids, on="service_id", how="anti")
        .height,
        "service_ids used by trips that are defined but never active on any date",
        member="trips.txt",
    )
    report.add(
        CODE_DIRECTION_OUT_OF_RANGE,
        "warning",
        trips.filter(
            pl.col("direction_id").is_not_null() & ~pl.col("direction_id").is_in([0, 1])
        ).height,
        "trips with direction_id outside {0, 1}",
        member="trips.txt",
    )

    return report
