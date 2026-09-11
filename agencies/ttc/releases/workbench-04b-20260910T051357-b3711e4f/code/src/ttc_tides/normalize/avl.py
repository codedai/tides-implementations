"""AVL typed normalization (plan S1, provisional per ADR 0002).

Canonical treatments from the plan's S1 table:

- ``Date_Key`` becomes ``source_date_key`` — evidence, never a service date;
- ``Message_DateTime`` is parsed as a **naive** local timestamp; no timezone
  or DST fold is attached here (fold hypotheses belong to the frozen S3);
- identifiers and labels stay trimmed strings; ``tatripId`` becomes
  ``source_tatrip_id`` and is never a GTFS key;
- global coordinate-range and negative-speed violations are structural
  rejections; the TTC-area envelope, heading range, speed ceiling, and
  unknown boolean tokens are non-destructive flags;
- exact payload duplicates are annotated on every member, never dropped.
"""

from __future__ import annotations

import polars as pl

from ..ingest.avl import REQUIRED_COLUMNS, scan_avl_landing
from ..ingest.sources import SourceFile
from .common import (
    NormalizedSource,
    annotate_payload_duplicates,
    clean_str,
    count_flags,
    lineage_columns,
    reason_when,
    split_by_reasons,
    stable_vehicle_bucket,
)

# Stage-namespaced rejection reason codes (plan §9).
REASON_INVALID_DATE_KEY = "AVL.PARSE.INVALID_DATE_KEY"
REASON_INVALID_TIMESTAMP = "AVL.PARSE.INVALID_TIMESTAMP"
REASON_MISSING_VEHICLE = "AVL.PARSE.MISSING_VEHICLE"
REASON_COORD_OUT_OF_RANGE = "AVL.PARSE.COORD_OUT_OF_RANGE"
REASON_INVALID_NUMBER = "AVL.PARSE.INVALID_NUMBER"
REASON_NEGATIVE_SPEED = "AVL.PARSE.NEGATIVE_SPEED"

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S%.3f"

#: Non-destructive plausibility envelope for the TTC service area.
TTC_LAT_RANGE = (43.0, 45.0)
TTC_LON_RANGE = (-80.6, -78.5)
SPEED_PLAUSIBLE_MAX_KPH = 130.0

FLAG_COLUMNS = [
    "_flag_outside_ttc_envelope",
    "_flag_heading_out_of_range",
    "_flag_speed_implausible",
    "_flag_unknown_bool_token",
    "_flag_duplicate_payload",
]


def _bool_expr(name: str, alias: str) -> pl.Expr:
    lowered = pl.col(name).str.to_lowercase()
    return (
        pl.when(lowered == "true")
        .then(True)
        .when(lowered == "false")
        .then(False)
        .otherwise(None)
        .alias(alias)
    )


def normalize_avl(
    source: SourceFile, *, ingest_run_id: str = "local"
) -> NormalizedSource:
    landing = scan_avl_landing(source, ingest_run_id=ingest_run_id).collect(
        engine="streaming"
    )
    landing_rows = landing.height
    landing_column_names = [*REQUIRED_COLUMNS, *lineage_columns()]

    df = landing.with_columns([clean_str(c) for c in REQUIRED_COLUMNS])
    df = df.with_columns(
        pl.col("Date_Key").str.to_date("%Y%m%d", strict=False).alias("source_date_key"),
        pl.col("Message_DateTime")
        .str.to_datetime(TIMESTAMP_FORMAT, strict=False)
        .cast(pl.Datetime("us"))
        .alias("event_local_time"),
        pl.col("Latitude").cast(pl.Float64, strict=False).alias("latitude"),
        pl.col("Longitude").cast(pl.Float64, strict=False).alias("longitude"),
        pl.col("Heading").cast(pl.Float64, strict=False).alias("heading"),
        pl.col("KPH").cast(pl.Float64, strict=False).alias("speed_kph"),
        pl.col("Vehicle").alias("vehicle_id_raw"),
        pl.col("Route").alias("route_label"),
        pl.col("Run").alias("run_label"),
        pl.col("Destination").alias("destination_label"),
        pl.col("tatripId").alias("source_tatrip_id"),
        _bool_expr("OffRoute", "off_route"),
        _bool_expr("Delayed", "delayed"),
        pl.col("Message_DateTime").alias("message_datetime_raw"),
    )

    invalid_number = (
        (pl.col("Latitude").is_not_null() & pl.col("latitude").is_null())
        | (pl.col("Longitude").is_not_null() & pl.col("longitude").is_null())
        | (pl.col("KPH").is_not_null() & pl.col("speed_kph").is_null())
    )
    coord_out_of_range = (pl.col("latitude").abs() > 90) | (
        pl.col("longitude").abs() > 180
    )
    reasons = [
        reason_when(
            pl.col("Date_Key").is_not_null() & pl.col("source_date_key").is_null(),
            REASON_INVALID_DATE_KEY,
        ),
        reason_when(
            pl.col("Message_DateTime").is_null() | pl.col("event_local_time").is_null(),
            REASON_INVALID_TIMESTAMP,
        ),
        reason_when(pl.col("vehicle_id_raw").is_null(), REASON_MISSING_VEHICLE),
        reason_when(coord_out_of_range.fill_null(False), REASON_COORD_OUT_OF_RANGE),
        reason_when(invalid_number, REASON_INVALID_NUMBER),
        reason_when(pl.col("speed_kph") < 0, REASON_NEGATIVE_SPEED),
    ]
    accepted, rejected = split_by_reasons(
        df, reasons, landing_columns=landing_column_names
    )

    accepted = accepted.with_columns(
        (
            pl.col("latitude").is_not_null()
            & pl.col("longitude").is_not_null()
            & (
                ~pl.col("latitude").is_between(*TTC_LAT_RANGE)
                | ~pl.col("longitude").is_between(*TTC_LON_RANGE)
            )
        ).alias("_flag_outside_ttc_envelope"),
        (
            pl.col("heading").is_not_null() & ~pl.col("heading").is_between(0.0, 360.0)
        ).alias("_flag_heading_out_of_range"),
        (pl.col("speed_kph") > SPEED_PLAUSIBLE_MAX_KPH)
        .fill_null(False)
        .alias("_flag_speed_implausible"),
        (
            (pl.col("OffRoute").is_not_null() & pl.col("off_route").is_null())
            | (pl.col("Delayed").is_not_null() & pl.col("delayed").is_null())
        ).alias("_flag_unknown_bool_token"),
        pl.col("event_local_time").dt.date().alias("source_calendar_date"),
    )
    accepted = annotate_payload_duplicates(
        accepted, list(REQUIRED_COLUMNS), flag_name="_flag_duplicate_payload"
    )
    accepted = stable_vehicle_bucket(accepted, "vehicle_id_raw")
    accepted = accepted.select(
        "source_calendar_date",
        "vehicle_bucket",
        "source_date_key",
        "event_local_time",
        "message_datetime_raw",
        "vehicle_id_raw",
        "latitude",
        "longitude",
        "heading",
        "speed_kph",
        "route_label",
        "run_label",
        "destination_label",
        "off_route",
        "delayed",
        "source_tatrip_id",
        *FLAG_COLUMNS,
        *lineage_columns(),
    ).sort("event_local_time", "_source_row_number")

    result = NormalizedSource(
        source=source,
        landing_rows=landing_rows,
        accepted=accepted,
        rejected=rejected,
        flag_counts=count_flags(accepted, FLAG_COLUMNS),
        metrics={
            "distinct_vehicles": accepted["vehicle_id_raw"].n_unique(),
            "route_label_counts": dict(
                accepted["route_label"].value_counts().iter_rows()
            ),
            "date_key_equals_calendar_date_violations": int(
                accepted.filter(
                    pl.col("source_date_key") != pl.col("source_calendar_date")
                ).height
            ),
            "event_local_time_min": str(accepted["event_local_time"].min()),
            "event_local_time_max": str(accepted["event_local_time"].max()),
        },
    )
    result.check_conservation()
    return result
