"""APC typed normalization (plan S2, provisional per ADR 0002).

Canonical treatments from the plan's S2 table:

- the unnamed export index is preserved as ``source_export_id`` lineage;
- ``StopID`` becomes ``source_stop_code`` and is never renamed to a GTFS
  ``stop_id``;
- arrival/departure are parsed as **naive** local timestamps; an inverted
  interval is flagged, not repaired (repair needs the frozen S3 hypotheses);
- counts and loads are parsed without rounding; negative and fractional
  domains are measured and flagged, not rejected, because the publish rule
  awaits G0.4/D-06;
- exact payload duplicates (ignoring the export index) and visit-key
  collisions — differing rows sharing (vehicle, route, stop, arrival,
  departure) — are distinct annotations; neither is summed or dropped.
"""

from __future__ import annotations

import polars as pl

from ..ingest.apc import EXPORT_INDEX_COLUMN, REQUIRED_COLUMNS, scan_apc_landing
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

REASON_INVALID_TIMESTAMP = "APC.PARSE.INVALID_TIMESTAMP"
REASON_MISSING_VEHICLE = "APC.PARSE.MISSING_VEHICLE"
REASON_MISSING_STOP_CODE = "APC.PARSE.MISSING_STOP_CODE"
REASON_COORD_OUT_OF_RANGE = "APC.PARSE.COORD_OUT_OF_RANGE"
REASON_INVALID_NUMBER = "APC.PARSE.INVALID_NUMBER"

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

TTC_LAT_RANGE = (43.0, 45.0)
TTC_LON_RANGE = (-80.6, -78.5)

VISIT_KEY = ("VehicleID", "Route", "StopID", "StopArrivalTime", "StopDepartureTime")

FLAG_COLUMNS = [
    "_flag_interval_inverted",
    "_flag_outside_ttc_envelope",
    "_flag_negative_count",
    "_flag_load_equation_violated",
    "_flag_duplicate_payload",
    "_flag_visit_key_collision",
]

_INT_FIELDS = {
    "NoTrips": "no_trips",
    "AvgSample": "avg_sample",
    "Boarding": "boarding",
    "Alighting": "alighting",
}
_FLOAT_FIELDS = {
    "AvgDwell": "avg_dwell",
    "AvgOTP": "avg_otp",
    "Arrload": "arrival_load",
    "Depload": "departure_load",
    "Lat": "latitude",
    "Lon": "longitude",
}


def normalize_apc(
    source: SourceFile, *, ingest_run_id: str = "local"
) -> NormalizedSource:
    landing = scan_apc_landing(source, ingest_run_id=ingest_run_id).collect(
        engine="streaming"
    )
    landing_rows = landing.height
    landing_column_names = [
        EXPORT_INDEX_COLUMN,
        *REQUIRED_COLUMNS,
        *lineage_columns(),
    ]

    df = landing.with_columns([clean_str(c) for c in REQUIRED_COLUMNS])
    df = df.with_columns(
        [
            pl.col(src).cast(pl.Int64, strict=False).alias(dst)
            for src, dst in _INT_FIELDS.items()
        ]
        + [
            pl.col(src).cast(pl.Float64, strict=False).alias(dst)
            for src, dst in _FLOAT_FIELDS.items()
        ]
        + [
            pl.col("StopArrivalTime")
            .str.to_datetime(TIMESTAMP_FORMAT, strict=False)
            .cast(pl.Datetime("us"))
            .alias("arrival_local_time"),
            pl.col("StopDepartureTime")
            .str.to_datetime(TIMESTAMP_FORMAT, strict=False)
            .cast(pl.Datetime("us"))
            .alias("departure_local_time"),
            pl.col("VehicleID").alias("vehicle_id_raw"),
            pl.col("Route").alias("route_label"),
            pl.col("RouteName").alias("route_name"),
            pl.col("Branch").alias("branch_label"),
            pl.col("RouteDirection").alias("direction_label"),
            pl.col("StopID").alias("source_stop_code"),
            pl.col("ONSTREET").alias("on_street"),
            pl.col("ATSTREET").alias("at_street"),
            pl.col("PeriodID").alias("period_id"),
            pl.col("DAYTYPE").alias("daytype"),
            pl.col("StopArrivalTime").alias("arrival_raw"),
            pl.col("StopDepartureTime").alias("departure_raw"),
        ]
    )

    numeric_parse_failed = pl.any_horizontal(
        [
            pl.col(src).is_not_null() & pl.col(dst).is_null()
            for src, dst in {**_INT_FIELDS, **_FLOAT_FIELDS}.items()
        ]
    )
    coord_out_of_range = (
        (pl.col("latitude").abs() > 90) | (pl.col("longitude").abs() > 180)
    ).fill_null(False)
    reasons = [
        reason_when(
            pl.col("StopArrivalTime").is_null()
            | pl.col("arrival_local_time").is_null()
            | pl.col("StopDepartureTime").is_null()
            | pl.col("departure_local_time").is_null(),
            REASON_INVALID_TIMESTAMP,
        ),
        reason_when(pl.col("vehicle_id_raw").is_null(), REASON_MISSING_VEHICLE),
        reason_when(pl.col("source_stop_code").is_null(), REASON_MISSING_STOP_CODE),
        reason_when(coord_out_of_range, REASON_COORD_OUT_OF_RANGE),
        reason_when(numeric_parse_failed, REASON_INVALID_NUMBER),
    ]
    accepted, rejected = split_by_reasons(
        df, reasons, landing_columns=landing_column_names
    )

    accepted = accepted.with_columns(
        (pl.col("departure_local_time") < pl.col("arrival_local_time")).alias(
            "_flag_interval_inverted"
        ),
        (
            pl.col("latitude").is_not_null()
            & pl.col("longitude").is_not_null()
            & (
                ~pl.col("latitude").is_between(*TTC_LAT_RANGE)
                | ~pl.col("longitude").is_between(*TTC_LON_RANGE)
            )
        ).alias("_flag_outside_ttc_envelope"),
        pl.any_horizontal(
            [
                (pl.col(c) < 0).fill_null(False)
                for c in ("boarding", "alighting", "arrival_load", "departure_load")
            ]
        ).alias("_flag_negative_count"),
        (
            (
                pl.col("departure_load")
                - (pl.col("arrival_load") + pl.col("boarding") - pl.col("alighting"))
            ).abs()
            > 0
        )
        .fill_null(False)
        .alias("_flag_load_equation_violated"),
        pl.col("arrival_local_time").dt.date().alias("source_calendar_date"),
    )
    accepted = annotate_payload_duplicates(
        accepted, list(REQUIRED_COLUMNS), flag_name="_flag_duplicate_payload"
    )
    # Visit-key collision: >1 distinct payload sharing the 5-column visit key.
    collision_keys = (
        accepted.group_by(list(VISIT_KEY))
        .agg(pl.len().alias("__n"))
        .filter(pl.col("__n") > 1)
        .select(VISIT_KEY)
    )
    accepted = accepted.join(
        collision_keys.with_columns(pl.lit(True).alias("_flag_visit_key_collision")),
        on=list(VISIT_KEY),
        how="left",
    ).with_columns(
        (
            pl.col("_flag_visit_key_collision").fill_null(False)
            & ~pl.col("_flag_duplicate_payload")
        ).alias("_flag_visit_key_collision")
    )
    accepted = stable_vehicle_bucket(accepted, "vehicle_id_raw")
    accepted = accepted.select(
        "source_calendar_date",
        "vehicle_bucket",
        EXPORT_INDEX_COLUMN,
        "arrival_local_time",
        "departure_local_time",
        "arrival_raw",
        "departure_raw",
        "vehicle_id_raw",
        "route_label",
        "route_name",
        "branch_label",
        "direction_label",
        "source_stop_code",
        "on_street",
        "at_street",
        "latitude",
        "longitude",
        "period_id",
        "daytype",
        "no_trips",
        "avg_dwell",
        "avg_otp",
        "avg_sample",
        "arrival_load",
        "boarding",
        "alighting",
        "departure_load",
        *FLAG_COLUMNS,
        *lineage_columns(),
    ).sort("arrival_local_time", "_source_row_number")

    result = NormalizedSource(
        source=source,
        landing_rows=landing_rows,
        accepted=accepted,
        rejected=rejected,
        flag_counts=count_flags(accepted, FLAG_COLUMNS),
        metrics={
            "distinct_vehicles": accepted["vehicle_id_raw"].n_unique(),
            "distinct_stop_codes": accepted["source_stop_code"].n_unique(),
            "avg_sample_counts": dict(
                accepted["avg_sample"].value_counts().iter_rows()
            ),
            "arrival_local_time_min": str(accepted["arrival_local_time"].min()),
            "arrival_local_time_max": str(accepted["arrival_local_time"].max()),
        },
    )
    result.check_conservation()
    return result
