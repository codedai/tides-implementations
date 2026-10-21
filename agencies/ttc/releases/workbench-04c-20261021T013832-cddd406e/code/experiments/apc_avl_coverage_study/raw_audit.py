"""Fresh, schedule-independent audit of the immutable TTC APC and AVL CSVs.

Run from agencies/ttc with ``uv run --offline --no-sync python
experiments/apc_avl_coverage_study/raw_audit.py``. Every input row is retained in
the canonical parquet files. Timestamps remain Toronto wall-clock observations:
no DST fold, service day, performed-trip boundary or scheduled identity is chosen.
Raw identifiers and examples are written only to restricted, ignored artifacts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "artifacts/apc-avl-coverage-study-20260908/raw_audit"
TZ = ZoneInfo("America/Toronto")
FLOAT = pl.Float64
TIME = pl.Datetime("us")


def wall_time_status(value: datetime) -> str:
    """Classify a naive timestamp without selecting one of its possible instants."""
    first = value.replace(tzinfo=TZ, fold=0)
    second = value.replace(tzinfo=TZ, fold=1)
    if first.utcoffset() == second.utcoffset():
        return "unique"
    first_valid = first.astimezone(UTC).astimezone(TZ).replace(tzinfo=None) == value
    second_valid = second.astimezone(UTC).astimezone(TZ).replace(tzinfo=None) == value
    return "ambiguous" if first_valid and second_valid else "nonexistent"


def secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_json(path: Path, data: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def write_parquet(path: Path, frame: pl.DataFrame) -> None:
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    frame.write_parquet(temporary, compression="zstd", statistics=True)
    temporary.chmod(0o600)
    temporary.replace(path)


def raw_inventory(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle))
    return {
        "path": str(path.relative_to(ROOT)),
        "bytes": path.stat().st_size,
        "sha256": digest,
        "raw_columns": header,
    }


def read_raw(path: Path) -> pl.DataFrame:
    # No inferred numeric identifiers; the unnamed APC export index stays lineage.
    return pl.read_csv(path, infer_schema=False, encoding="utf8", try_parse_dates=False)


def clean(name: str) -> pl.Expr:
    return pl.col(name).str.strip_chars().replace("", None)


def numeric(name: str, alias: str) -> pl.Expr:
    return clean(name).cast(FLOAT, strict=False).alias(alias)


def missing_or_bad_coordinates() -> pl.Expr:
    return (
        pl.col("lat").is_null()
        | pl.col("lon").is_null()
        | ~pl.col("lat").is_finite()
        | ~pl.col("lon").is_finite()
        | ~pl.col("lat").is_between(-90, 90)
        | ~pl.col("lon").is_between(-180, 180)
    ).fill_null(True)


def add_dst_flags(frame: pl.DataFrame, fields: list[str]) -> pl.DataFrame:
    hours: set[datetime] = set()
    for field in fields:
        hours.update(frame[field].drop_nulls().dt.truncate("1h").unique().to_list())
    ambiguous = [x for x in hours if wall_time_status(x) == "ambiguous"]
    nonexistent = [x for x in hours if wall_time_status(x) == "nonexistent"]
    expressions = []
    for label, selected in (
        ("time_ambiguous", ambiguous),
        ("time_nonexistent", nonexistent),
    ):
        expressions.append(
            pl.any_horizontal(
                [
                    pl.col(field)
                    .dt.truncate("1h")
                    .is_in(pl.Series(selected, dtype=TIME).implode())
                    for field in fields
                ]
            )
            .fill_null(False)
            .alias(label)
        )
    return frame.with_columns(expressions)


def normalize_raw(raw: pl.DataFrame, source: str, digest: str) -> pl.DataFrame:
    payload_columns = [name for name in raw.columns if name != ""]
    common = [
        pl.int_range(1, pl.len() + 1, dtype=pl.UInt32).alias("source_row"),
        pl.struct(payload_columns).hash(seed=0).alias("payload_hash"),
    ]
    if source == "avl":
        frame = raw.select(
            *common,
            clean("Vehicle").alias("vehicle"),
            clean("Message_DateTime")
            .str.to_datetime("%Y-%m-%d %H:%M:%S%.f", strict=False, time_unit="us")
            .alias("time"),
            numeric("Latitude", "lat"),
            numeric("Longitude", "lon"),
            numeric("Heading", "heading"),
            numeric("KPH", "speed"),
            clean("Route").alias("route"),
            clean("Run").alias("run"),
            clean("Destination").alias("destination"),
            clean("tatripId").alias("source_trip"),
            clean("Date_Key").alias("source_date"),
            clean("OffRoute").alias("off_route"),
            clean("Delayed").alias("delayed"),
        ).with_columns(
            (
                pl.col("vehicle").is_null()
                | pl.col("time").is_null()
                | missing_or_bad_coordinates()
                | (pl.col("speed") < 0).fill_null(False)
            ).alias("invalid")
        )
        frame = add_dst_flags(frame, ["time"])
    else:
        frame = raw.select(
            *common,
            clean("").alias("export_id"),
            clean("VehicleID").alias("vehicle"),
            clean("StopArrivalTime")
            .str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False, time_unit="us")
            .alias("arrival"),
            clean("StopDepartureTime")
            .str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False, time_unit="us")
            .alias("departure"),
            numeric("Lat", "lat"),
            numeric("Lon", "lon"),
            clean("Route").alias("route"),
            clean("RouteDirection").alias("direction"),
            clean("Branch").alias("branch"),
            clean("StopID").alias("stop"),
            clean("DAYTYPE").alias("daytype"),
            clean("PeriodID").alias("period"),
            numeric("Boarding", "boarding"),
            numeric("Alighting", "alighting"),
            numeric("Arrload", "arrival_load"),
            numeric("Depload", "departure_load"),
            numeric("AvgSample", "avg_sample"),
            numeric("NoTrips", "no_trips"),
            numeric("AvgDwell", "avg_dwell"),
            numeric("AvgOTP", "avg_otp"),
        ).with_columns(
            (
                pl.col("vehicle").is_null()
                | pl.col("stop").is_null()
                | pl.col("arrival").is_null()
                | pl.col("departure").is_null()
                | missing_or_bad_coordinates()
            ).alias("invalid"),
            (
                (pl.col("avg_sample") != 1)
                | (pl.col("no_trips") != 1)
                | pl.any_horizontal(
                    [
                        (pl.col(name) - pl.col(name).round()).abs() > 1e-9
                        for name in (
                            "boarding",
                            "alighting",
                            "arrival_load",
                            "departure_load",
                        )
                    ]
                )
            )
            .fill_null(True)
            .alias("grain_uncertain"),
        )
        frame = add_dst_flags(frame, ["arrival", "departure"])
    frame = frame.with_columns(
        pl.concat_str(pl.lit(f"{source}:{digest}:"), pl.col("source_row")).alias("rid")
    )
    assert frame.height == raw.height
    return frame


def series_summary(values: pl.Series) -> dict[str, Any]:
    values = values.drop_nulls()
    if values.dtype.is_float():
        values = values.filter(values.is_finite())
    if not len(values):
        return {"n": 0}
    return {
        "n": len(values),
        "min": values.min(),
        "p50": values.quantile(0.5),
        "p90": values.quantile(0.9),
        "p95": values.quantile(0.95),
        "p99": values.quantile(0.99),
        "max": values.max(),
    }


def frequency(
    frame: pl.DataFrame, column: str, limit: int = 30
) -> list[dict[str, Any]]:
    return (
        frame.group_by(column).len().sort("len", descending=True).head(limit).to_dicts()
    )


def flag(frame: pl.DataFrame, expr: pl.Expr, examples: list[str] | None = None) -> dict:
    flagged = frame.filter(expr.fill_null(False))
    result: dict[str, Any] = {"count": flagged.height}
    if examples is None:
        examples = [
            x
            for x in ("rid", "vehicle", "time", "arrival", "departure")
            if x in frame.columns
        ]
    result["examples"] = flagged.select(examples).head(5).to_dicts()
    return result


def distance_expression(
    lat_a: pl.Expr, lon_a: pl.Expr, lat_b: pl.Expr, lon_b: pl.Expr
) -> pl.Expr:
    # Haversine remains valid for reversed direction, dateline crossings and poles.
    delta_lat = (lat_b - lat_a).radians()
    delta_lon = (lon_b - lon_a).radians()
    hav = (
        (delta_lat / 2).sin().pow(2)
        + lat_a.radians().cos() * lat_b.radians().cos() * (delta_lon / 2).sin().pow(2)
    ).clip(0, 1)
    return 12_742_000 * hav.sqrt().arcsin()


def source_common(raw: pl.DataFrame, frame: pl.DataFrame, source: str) -> dict:
    missing = raw.select(
        [
            clean(name).is_null().sum().alias(name or "<export_index>")
            for name in raw.columns
        ]
    ).row(0, named=True)
    dup = raw.select([x for x in raw.columns if x != ""]).is_duplicated()
    result = {
        "row_count": raw.height,
        "raw_dtypes": {n: str(t) for n, t in raw.schema.items()},
        "canonical_dtypes": {n: str(t) for n, t in frame.schema.items()},
        "missing_or_empty_by_column": missing,
        "exact_payload_duplicate_rows_including_all_members": int(dup.sum()),
        "exact_payload_duplicate_excess_rows": raw.height
        - raw.select([x for x in raw.columns if x != ""]).n_unique(),
        "exact_payload_duplicate_examples": frame.filter(dup)
        .select("rid")
        .head(5)
        .to_dicts(),
        "invalid": flag(frame, pl.col("invalid")),
        "dst_ambiguous_wall_time": flag(frame, pl.col("time_ambiguous")),
        "dst_nonexistent_wall_time": flag(frame, pl.col("time_nonexistent")),
        "outside_ttc_diagnostic_envelope": flag(
            frame,
            ~pl.col("lat").is_between(43, 45) | ~pl.col("lon").is_between(-80.6, -78.5),
        ),
        "zero_coordinates": flag(frame, (pl.col("lat") == 0) | (pl.col("lon") == 0)),
        "vehicle_count": frame["vehicle"].drop_nulls().n_unique(),
        "routes": frequency(frame, "route"),
    }
    mappings = (
        {"Latitude": "lat", "Longitude": "lon", "Heading": "heading", "KPH": "speed"}
        if source == "avl"
        else {
            "Lat": "lat",
            "Lon": "lon",
            "Boarding": "boarding",
            "Alighting": "alighting",
            "Arrload": "arrival_load",
            "Depload": "departure_load",
            "NoTrips": "no_trips",
            "AvgSample": "avg_sample",
            "AvgDwell": "avg_dwell",
            "AvgOTP": "avg_otp",
        }
    )
    result["numeric_parse_failure_by_column"] = {
        src: int(
            (
                raw.select(clean(src).is_not_null()).to_series() & frame[dst].is_null()
            ).sum()
        )
        for src, dst in mappings.items()
    }
    result["nonfinite_numeric_by_column"] = {
        dst: int((frame[dst].is_not_null() & ~frame[dst].is_finite()).sum())
        for dst in mappings.values()
    }
    fields = ["time"] if source == "avl" else ["arrival", "departure"]
    result["time_parse_failure_or_missing"] = {n: frame[n].null_count() for n in fields}
    result["time_ranges"] = {
        n: {"min": frame[n].min(), "max": frame[n].max()} for n in fields
    }
    return result


def audit_avl(frame: pl.DataFrame) -> tuple[dict, pl.DataFrame]:
    valid = frame.filter(~pl.col("invalid")).sort(["vehicle", "time", "source_row"])
    valid = (
        valid.with_columns(
            pl.col("time").shift().over("vehicle").alias("previous_time"),
            pl.col("lat").shift().over("vehicle").alias("previous_lat"),
            pl.col("lon").shift().over("vehicle").alias("previous_lon"),
            pl.col("source_trip").shift().over("vehicle").alias("previous_source_trip"),
            pl.col("route").shift().over("vehicle").alias("previous_route"),
            pl.col("run").shift().over("vehicle").alias("previous_run"),
            pl.col("destination").shift().over("vehicle").alias("previous_destination"),
            pl.col("time_ambiguous")
            .shift()
            .over("vehicle")
            .alias("previous_ambiguous"),
        )
        .with_columns(
            (pl.col("time") - pl.col("previous_time"))
            .dt.total_seconds()
            .alias("gap_s"),
            distance_expression(
                pl.col("previous_lat"),
                pl.col("previous_lon"),
                pl.col("lat"),
                pl.col("lon"),
            ).alias("step_m"),
        )
        .with_columns(
            (pl.col("step_m") / pl.col("gap_s") * 3.6).alias("implied_kph"),
        )
    )
    keys = valid.group_by("vehicle", "time").agg(
        pl.len().alias("n"),
        pl.struct("lat", "lon").n_unique().alias("locations"),
        pl.col("payload_hash").n_unique().alias("payloads"),
        pl.col("rid").first().alias("example_rid"),
        pl.col("time_ambiguous").any().alias("time_ambiguous"),
    )
    reused = valid.group_by("source_trip").agg(
        pl.len().alias("rows"),
        pl.col("vehicle").n_unique().alias("vehicles"),
        pl.col("time").dt.date().n_unique().alias("calendar_days"),
        pl.col("route").n_unique().alias("routes"),
        pl.col("time").min().alias("first"),
        pl.col("time").max().alias("last"),
    )
    raw_order = frame.with_columns(
        pl.col("time").diff().over("vehicle").dt.total_seconds().alias("raw_delta_s")
    )
    transitions = {
        field: flag(
            valid,
            pl.col(field).ne_missing(pl.col(f"previous_{field}"))
            & pl.col("previous_time").is_not_null(),
        )
        for field in ("route", "run", "destination", "source_trip")
    }
    date_frame = frame.with_columns(
        pl.col("source_date")
        .str.to_date("%Y%m%d", strict=False)
        .alias("parsed_source_date")
    ).with_columns(
        (pl.col("time").dt.date() - pl.col("parsed_source_date"))
        .dt.total_days()
        .alias("date_key_calendar_delta_days")
    )
    short_context = valid.filter(pl.col("gap_s").is_between(0, 120))
    stationary = valid.filter((pl.col("step_m") < 3) & (pl.col("gap_s") > 0))
    plausible_steps = short_context.filter(
        pl.col("lat").is_between(43, 45)
        & pl.col("lon").is_between(-80.6, -78.5)
        & pl.col("previous_lat").is_between(43, 45)
        & pl.col("previous_lon").is_between(-80.6, -78.5)
        & ~pl.col("time_ambiguous")
        & ~pl.col("previous_ambiguous").fill_null(False)
    )
    plateaus = (
        valid.with_columns(
            (
                pl.col("lat").ne_missing(pl.col("previous_lat"))
                | pl.col("lon").ne_missing(pl.col("previous_lon"))
                | (pl.col("gap_s") > 90)
                | pl.col("previous_time").is_null()
            )
            .cast(pl.UInt32)
            .cum_sum()
            .over("vehicle")
            .alias("plateau")
        )
        .group_by("vehicle", "plateau")
        .agg(
            pl.col("time").min().alias("start"),
            pl.col("time").max().alias("end"),
            pl.len().alias("n"),
            pl.col("lat").first(),
            pl.col("lon").first(),
            pl.col("rid").first().alias("example_rid"),
        )
        .with_columns(
            (pl.col("end") - pl.col("start")).dt.total_seconds().alias("duration_s")
        )
    )
    flickers = valid.with_columns(
        *[
            pl.col(name).shift(-1).over("vehicle").alias(f"next_{name}")
            for name in ("route", "run", "destination", "source_trip")
        ],
        pl.col("gap_s").shift(-1).over("vehicle").alias("next_gap_s"),
    )
    result = {
        "valid_rows_for_adjacency": valid.height,
        "raw_order_time_reversals_within_vehicle": flag(
            raw_order, pl.col("raw_delta_s") < 0
        ),
        "adjacent_wall_clock_gap_seconds": series_summary(valid["gap_s"]),
        "adjacent_positive_gap_seconds": series_summary(
            valid.filter(pl.col("gap_s") > 0)["gap_s"]
        ),
        "gap_thresholds": {
            str(n): flag(valid, pl.col("gap_s") > n)
            for n in (60, 90, 120, 300, 900, 1800, 3600)
        },
        "vehicle_timestamp_duplicate_groups": keys.filter(pl.col("n") > 1).height,
        "vehicle_timestamp_conflicting_payload_groups": keys.filter(
            pl.col("payloads") > 1
        ).height,
        "vehicle_timestamp_conflicting_location_groups": keys.filter(
            pl.col("locations") > 1
        ).height,
        "conflicting_location_groups_outside_dst_ambiguous_hour": keys.filter(
            (pl.col("locations") > 1) & ~pl.col("time_ambiguous")
        ).height,
        "conflicting_location_examples": keys.filter(pl.col("locations") > 1)
        .head(5)
        .to_dicts(),
        "apparent_teleport_over_130_kph_gap_under_120s": flag(
            short_context,
            (pl.col("implied_kph") > 130)
            & (pl.col("gap_s") > 0)
            & ~pl.col("time_ambiguous"),
        ),
        "implied_speed_positive_gaps": series_summary(
            valid.filter(pl.col("gap_s") > 0)["implied_kph"]
        ),
        "apparent_teleport_over_130kph_both_coordinates_ttc_non_dst": flag(
            plausible_steps,
            (pl.col("implied_kph") > 130) & (pl.col("gap_s") > 0),
        ),
        "stationary_adjacent_pairs_under_3m": stationary.height,
        "stationary_pair_gap_seconds": series_summary(stationary["gap_s"]),
        "exact_coordinate_plateau_gap_at_most_90s": {
            "duration_seconds": series_summary(plateaus["duration_s"]),
            "episodes_at_least_5_minutes": plateaus.filter(
                pl.col("duration_s") >= 300
            ).height,
            "episodes_at_least_15_minutes": plateaus.filter(
                pl.col("duration_s") >= 900
            ).height,
            "examples": plateaus.filter(pl.col("duration_s") >= 900).head(5).to_dicts(),
        },
        "single_row_context_flicker_A_B_A_with_gaps_at_most_60s": {
            name: flickers.filter(
                (pl.col(f"previous_{name}") == pl.col(f"next_{name}"))
                & pl.col(name).ne_missing(pl.col(f"previous_{name}"))
                & pl.col("gap_s").is_between(0, 60)
                & pl.col("next_gap_s").is_between(0, 60)
            ).height
            for name in ("route", "run", "destination", "source_trip")
        },
        "heading_outside_0_360": flag(frame, ~pl.col("heading").is_between(0, 360)),
        "reported_speed_over_130_kph": flag(frame, pl.col("speed") > 130),
        "reported_speed_negative": flag(frame, pl.col("speed") < 0),
        "unknown_boolean_tokens": {
            name: flag(frame, ~pl.col(name).str.to_lowercase().is_in(["true", "false"]))
            for name in ("off_route", "delayed")
        },
        "source_context_transitions": transitions,
        "source_trip_value_count": reused.height,
        "source_trip_values_reused_across_vehicles": reused.filter(
            pl.col("vehicles") > 1
        ).height,
        "source_trip_values_reused_across_calendar_days": reused.filter(
            pl.col("calendar_days") > 1
        ).height,
        "source_trip_values_reused_across_routes": reused.filter(
            pl.col("routes") > 1
        ).height,
        "source_trip_reuse_examples": reused.filter(pl.col("vehicles") > 1)
        .head(5)
        .to_dicts(),
        "date_key_parse_failure_or_missing": date_frame[
            "parsed_source_date"
        ].null_count(),
        "date_key_calendar_delta_days": frequency(
            date_frame, "date_key_calendar_delta_days"
        ),
        "cross_midnight_adjacent_pairs_under_15_minutes": flag(
            valid,
            (pl.col("time").dt.date() != pl.col("previous_time").dt.date())
            & pl.col("gap_s").is_between(0, 900),
        ),
        "context_transition_short_gap_counts": {
            field: short_context.filter(
                pl.col(field).ne_missing(pl.col(f"previous_{field}"))
            ).height
            for field in ("route", "run", "destination", "source_trip")
        },
    }
    gaps = valid.filter(pl.col("gap_s") > 90).select(
        "rid", "vehicle", "previous_time", "time", "gap_s", "step_m", "time_ambiguous"
    )
    return result, gaps


def audit_apc(frame: pl.DataFrame) -> tuple[dict, pl.DataFrame]:
    valid = frame.filter(~pl.col("invalid")).sort(
        ["vehicle", "arrival", "departure", "source_row"]
    )
    valid = valid.with_columns(
        (pl.col("departure") - pl.col("arrival")).dt.total_seconds().alias("dwell_s"),
        pl.col("departure").shift().over("vehicle").alias("previous_departure"),
        pl.col("arrival").shift().over("vehicle").alias("previous_arrival"),
        pl.col("stop").shift().over("vehicle").alias("previous_stop"),
        pl.col("direction").shift().over("vehicle").alias("previous_direction"),
        pl.col("route").shift().over("vehicle").alias("previous_route"),
        pl.col("branch").shift().over("vehicle").alias("previous_branch"),
        pl.col("lat").shift().over("vehicle").alias("previous_lat"),
        pl.col("lon").shift().over("vehicle").alias("previous_lon"),
        pl.col("departure_load")
        .shift()
        .over("vehicle")
        .alias("previous_departure_load"),
    ).with_columns(
        (pl.col("arrival") - pl.col("previous_departure"))
        .dt.total_seconds()
        .alias("gap_s"),
        distance_expression(
            pl.col("previous_lat"), pl.col("previous_lon"), pl.col("lat"), pl.col("lon")
        ).alias("step_m"),
        (
            pl.col("departure_load")
            - pl.col("arrival_load")
            - pl.col("boarding")
            + pl.col("alighting")
        ).alias("load_residual"),
    )
    keys = frame.group_by("vehicle", "route", "stop", "arrival", "departure").agg(
        pl.len().alias("n"),
        pl.col("payload_hash").n_unique().alias("payloads"),
        pl.col("rid").first().alias("example_rid"),
    )
    timestamp = frame.group_by("vehicle", "arrival").agg(
        pl.len().alias("n"),
        pl.col("stop").n_unique().alias("stops"),
        pl.struct("lat", "lon").n_unique().alias("locations"),
        pl.col("rid").first().alias("example_rid"),
    )
    stop_coordinates = (
        frame.group_by("stop")
        .agg(
            pl.struct("lat", "lon").n_unique().alias("coordinates"),
            pl.col("lat").min().alias("min_lat"),
            pl.col("lat").max().alias("max_lat"),
            pl.col("lon").min().alias("min_lon"),
            pl.col("lon").max().alias("max_lon"),
        )
        .with_columns(
            distance_expression(
                pl.col("min_lat"),
                pl.col("min_lon"),
                pl.col("max_lat"),
                pl.col("max_lon"),
            ).alias("bbox_diagonal_m")
        )
    )
    # All prior departures, rather than only the immediately previous row, catch
    # a long interval containing several later short visits.
    overlap = valid.with_columns(
        pl.col("departure")
        .cum_max()
        .shift()
        .over("vehicle")
        .alias("prior_max_departure")
    )
    result = {
        "avg_sample_distribution": frequency(frame, "avg_sample"),
        "no_trips_distribution": frequency(frame, "no_trips"),
        "grain_uncertain_signal": flag(frame, pl.col("grain_uncertain")),
        "grain_semantics_unverified_for_all_rows": True,
        "fractional_fields": {
            name: flag(frame, (pl.col(name) - pl.col(name).round()).abs() > 1e-9)
            for name in (
                "boarding",
                "alighting",
                "arrival_load",
                "departure_load",
                "avg_sample",
                "no_trips",
            )
        },
        "negative_count_or_load_fields": {
            name: flag(frame, pl.col(name) < 0)
            for name in ("boarding", "alighting", "arrival_load", "departure_load")
        },
        "nonpositive_sampling_fields": {
            name: flag(frame, pl.col(name) <= 0) for name in ("avg_sample", "no_trips")
        },
        "zero_boarding_and_alighting": flag(
            frame, (pl.col("boarding") == 0) & (pl.col("alighting") == 0)
        ),
        "all_zero_activity_and_load": flag(
            frame,
            pl.all_horizontal(
                [
                    pl.col(name) == 0
                    for name in (
                        "boarding",
                        "alighting",
                        "arrival_load",
                        "departure_load",
                    )
                ]
            ),
        ),
        "load_equation_residual": series_summary(valid["load_residual"]),
        "load_equation_residual_over_1e_6": flag(
            valid, pl.col("load_residual").abs() > 1e-6
        ),
        "load_continuity_residual_over_1_short_gap_same_context": flag(
            valid,
            ((pl.col("arrival_load") - pl.col("previous_departure_load")).abs() > 1)
            & pl.col("gap_s").is_between(0, 300)
            & (pl.col("route") == pl.col("previous_route"))
            & (pl.col("direction") == pl.col("previous_direction")),
        ),
        "dwell_seconds": series_summary(valid["dwell_s"]),
        "inverted_intervals": flag(valid, pl.col("dwell_s") < 0),
        "zero_duration_intervals": flag(valid, pl.col("dwell_s") == 0),
        "dwell_over_5_minutes": flag(valid, pl.col("dwell_s") > 300),
        "dwell_over_30_minutes": flag(valid, pl.col("dwell_s") > 1800),
        "reported_dwell_disagrees_over_1_second": flag(
            valid, (pl.col("dwell_s") - pl.col("avg_dwell")).abs() > 1
        ),
        "arrival_departure_cross_calendar_day": flag(
            valid, pl.col("arrival").dt.date() != pl.col("departure").dt.date()
        ),
        "visit_key_duplicate_groups": keys.filter(pl.col("n") > 1).height,
        "visit_key_conflicting_payload_groups": keys.filter(
            pl.col("payloads") > 1
        ).height,
        "visit_key_collision_examples": keys.filter(pl.col("payloads") > 1)
        .head(5)
        .to_dicts(),
        "same_vehicle_arrival_multiple_stop_groups": timestamp.filter(
            pl.col("stops") > 1
        ).height,
        "same_vehicle_arrival_multiple_location_groups": timestamp.filter(
            pl.col("locations") > 1
        ).height,
        "concurrent_different_stop_examples": timestamp.filter(pl.col("stops") > 1)
        .head(5)
        .to_dicts(),
        "overlap_with_any_earlier_interval": flag(
            overlap, pl.col("arrival") < pl.col("prior_max_departure")
        ),
        "overlap_with_immediate_predecessor": flag(valid, pl.col("gap_s") < 0),
        "immediate_overlap_context_counts": {
            "same_stop_opposite_direction": valid.filter(
                (pl.col("gap_s") < 0)
                & (pl.col("stop") == pl.col("previous_stop"))
                & pl.col("direction").ne_missing(pl.col("previous_direction"))
            ).height,
            "different_stop_within_100m": valid.filter(
                (pl.col("gap_s") < 0)
                & pl.col("stop").ne_missing(pl.col("previous_stop"))
                & (pl.col("step_m") <= 100)
            ).height,
            "different_stop_over_100m": valid.filter(
                (pl.col("gap_s") < 0)
                & pl.col("stop").ne_missing(pl.col("previous_stop"))
                & (pl.col("step_m") > 100)
            ).height,
            "current_record_has_grain_uncertain_signal": valid.filter(
                (pl.col("gap_s") < 0) & pl.col("grain_uncertain")
            ).height,
            "current_record_in_dst_ambiguous_hour": valid.filter(
                (pl.col("gap_s") < 0) & pl.col("time_ambiguous")
            ).height,
        },
        "consecutive_repeated_stop_under_15_minutes": flag(
            valid,
            (pl.col("stop") == pl.col("previous_stop"))
            & pl.col("gap_s").is_between(0, 900),
        ),
        "interstop_positive_gap_seconds": series_summary(
            valid.filter(pl.col("gap_s") > 0)["gap_s"]
        ),
        "gap_thresholds": {
            str(n): flag(valid, pl.col("gap_s") > n) for n in (300, 900, 1800, 3600)
        },
        "context_transitions": {
            name: flag(
                valid,
                pl.col(name).ne_missing(pl.col(f"previous_{name}"))
                & pl.col("previous_arrival").is_not_null(),
            )
            for name in ("route", "direction", "branch")
        },
        "direction_values": frequency(frame, "direction"),
        "stop_codes_with_multiple_coordinate_pairs": stop_coordinates.filter(
            pl.col("coordinates") > 1
        ).height,
        "stop_codes_coordinate_bbox_over_100m": stop_coordinates.filter(
            pl.col("bbox_diagonal_m") > 100
        ).height,
        "stop_coordinate_examples": stop_coordinates.filter(
            pl.col("bbox_diagonal_m") > 100
        )
        .head(5)
        .to_dicts(),
        "apparent_interstop_speed_over_130kph_under_5minutes": flag(
            valid,
            pl.col("gap_s").is_between(1, 300)
            & (pl.col("step_m") / pl.col("gap_s") * 3.6 > 130)
            & ~pl.col("time_ambiguous"),
        ),
    }
    gaps = valid.filter(pl.col("gap_s") > 300).select(
        "rid",
        "vehicle",
        "previous_departure",
        "arrival",
        "gap_s",
        "step_m",
        "time_ambiguous",
    )
    return result, gaps


def coverage_audit(
    avl: pl.DataFrame, apc: pl.DataFrame, avl_gaps: pl.DataFrame
) -> tuple[dict, pl.DataFrame]:
    av = avl.filter(~pl.col("invalid")).with_columns(
        pl.col("time").dt.date().alias("calendar_date")
    )
    ap = apc.filter(~pl.col("invalid")).with_columns(
        pl.col("arrival").dt.date().alias("calendar_date")
    )
    av_days = av.group_by("vehicle", "calendar_date").agg(
        pl.len().alias("avl_rows"),
        pl.col("time").min().alias("avl_start"),
        pl.col("time").max().alias("avl_end"),
        pl.col("time_ambiguous").sum().alias("avl_ambiguous_rows"),
    )
    ap_days = ap.group_by("vehicle", "calendar_date").agg(
        pl.len().alias("apc_rows"),
        pl.col("arrival").min().alias("apc_start"),
        pl.max_horizontal("arrival", "departure").max().alias("apc_end"),
        pl.col("time_ambiguous").sum().alias("apc_ambiguous_rows"),
    )
    days = av_days.join(
        ap_days, on=["vehicle", "calendar_date"], how="full", coalesce=True
    )
    days = days.with_columns(
        (pl.col("apc_start") - pl.col("avl_start"))
        .dt.total_seconds()
        .alias("apc_start_minus_avl_s"),
        (pl.col("apc_end") - pl.col("avl_end"))
        .dt.total_seconds()
        .alias("apc_end_minus_avl_s"),
        (
            pl.min_horizontal("apc_end", "avl_end")
            - pl.max_horizontal("apc_start", "avl_start")
        )
        .dt.total_seconds()
        .alias("span_overlap_s"),
    ).sort("vehicle", "calendar_date")
    paired = days.filter(
        pl.col("avl_rows").is_not_null() & pl.col("apc_rows").is_not_null()
    )
    ap_rows = ap.join(av_days, on=["vehicle", "calendar_date"], how="left")
    # A ping bracket is temporal coverage evidence only: no route or distance
    # compatibility, interpolation, trip membership or clock-offset correction.
    left = ap.select("rid", "vehicle", "arrival", "departure").sort("arrival")
    right = av.select("vehicle", "time").sort("time")
    bracket = left.join_asof(
        right.rename({"time": "before"}),
        left_on="arrival",
        right_on="before",
        by="vehicle",
        strategy="backward",
        check_sortedness=False,
    )
    bracket = bracket.join_asof(
        right.rename({"time": "after"}),
        left_on="arrival",
        right_on="after",
        by="vehicle",
        strategy="forward",
        check_sortedness=False,
    )
    bracket = bracket.with_columns(
        (pl.col("arrival") - pl.col("before")).dt.total_seconds().alias("before_age_s"),
        (pl.col("after") - pl.col("arrival")).dt.total_seconds().alias("after_age_s"),
        (pl.col("after") - pl.col("before")).dt.total_seconds().alias("bracket_s"),
    )
    vehicles_av = set(avl["vehicle"].drop_nulls().to_list())
    vehicles_ap = set(apc["vehicle"].drop_nulls().to_list())
    result = {
        "unit": (
            "vehicle plus arrival calendar date; diagnostic partitions, "
            "not service days or trips"
        ),
        "avl_vehicle_count": len(vehicles_av),
        "apc_vehicle_count": len(vehicles_ap),
        "vehicles_in_both": len(vehicles_av & vehicles_ap),
        "avl_only_vehicles": len(vehicles_av - vehicles_ap),
        "apc_only_vehicles": len(vehicles_ap - vehicles_av),
        "vehicle_calendar_days_union": days.height,
        "vehicle_calendar_days_both": paired.height,
        "vehicle_calendar_days_avl_only": days.filter(
            pl.col("apc_rows").is_null()
        ).height,
        "vehicle_calendar_days_apc_only": days.filter(
            pl.col("avl_rows").is_null()
        ).height,
        "paired_days_disjoint_observed_spans": paired.filter(
            pl.col("span_overlap_s") < 0
        ).height,
        "apc_start_minus_avl_seconds": series_summary(paired["apc_start_minus_avl_s"]),
        "apc_end_minus_avl_seconds": series_summary(paired["apc_end_minus_avl_s"]),
        "tail_thresholds": {
            str(n): {
                "apc_begins_before_avl": paired.filter(
                    pl.col("apc_start_minus_avl_s") < -n
                ).height,
                "avl_begins_before_apc": paired.filter(
                    pl.col("apc_start_minus_avl_s") > n
                ).height,
                "apc_ends_after_avl": paired.filter(
                    pl.col("apc_end_minus_avl_s") > n
                ).height,
                "avl_ends_after_apc": paired.filter(
                    pl.col("apc_end_minus_avl_s") < -n
                ).height,
                "avl_starts_earlier_apc_ends_later": paired.filter(
                    (pl.col("apc_start_minus_avl_s") > n)
                    & (pl.col("apc_end_minus_avl_s") > n)
                ).height,
            }
            for n in (0, 60, 300, 900)
        },
        "apc_rows_on_vehicle_days_without_avl": ap_rows.filter(
            pl.col("avl_rows").is_null()
        ).height,
        "apc_rows_before_same_day_first_avl": ap_rows.filter(
            pl.col("arrival") < pl.col("avl_start")
        ).height,
        "apc_rows_after_same_day_last_avl": ap_rows.filter(
            pl.col("arrival") > pl.col("avl_end")
        ).height,
        "apc_intervals_extend_past_last_avl": ap_rows.filter(
            pl.col("departure") > pl.col("avl_end")
        ).height,
        "apc_arrivals_temporal_bracket_no_compatible_clock_assumed": {
            "no_before": bracket.filter(pl.col("before").is_null()).height,
            "no_after": bracket.filter(pl.col("after").is_null()).height,
            "bracket_seconds": series_summary(bracket["bracket_s"]),
            "bracket_exceeds_90s_with_both_sides": bracket.filter(
                pl.col("bracket_s") > 90
            ).height,
            "both_sides_within_60s": bracket.filter(
                (pl.col("before_age_s") <= 60) & (pl.col("after_age_s") <= 60)
            ).height,
            "inside_internal_gap_gt_300s": bracket.filter(
                (pl.col("bracket_s") > 300)
                & (pl.col("before_age_s") > 0)
                & (pl.col("after_age_s") > 0)
            ).height,
            "inside_internal_gap_gt_900s": bracket.filter(
                (pl.col("bracket_s") > 900)
                & (pl.col("before_age_s") > 0)
                & (pl.col("after_age_s") > 0)
            ).height,
        },
        "avl_gap_records_over_90s": avl_gaps.height,
    }
    return result, days


def run(output: Path) -> dict:
    os.umask(0o077)
    secure_dir(output.parent)
    secure_dir(output)
    paths = {
        "avl": ROOT
        / "database_offline/avl/20240401_Vehicle GPS Rt29 RT 929 Nov 2023.csv",
        "apc": ROOT / "database_offline/apc/ttc_apc_data.csv",
    }
    inventory: dict[str, Any] = {
        "audit_version": "1.0",
        "created_utc": datetime.now(UTC).isoformat(),
        "timezone_context": (
            "America/Toronto; naive wall time retained; no fold selected"
        ),
        "no_gtfs_used": True,
        "limits": [
            "Finite inventory of observed and known plausible edge classes; "
            "not proof all possible cases are exhausted.",
            "Calendar-day spans are observation envelopes, not continuous coverage "
            "or performed-trip boundaries.",
            "All APC grain semantics remain unverified; grain_uncertain flags "
            "visible aggregation signals only.",
            "Source identifiers, zero activity, source context changes and Date_Key "
            "are evidence, not trip ground truth.",
            "Thresholds (90 s bracket, 130 kph, 100 m) are diagnostic probes, "
            "not calibrated acceptance gates.",
            "DST ambiguous rows remain duplicated wall-clock possibilities; "
            "sorting does not resolve chronology.",
            "No raw rows deduplicated, repaired, summed, rounded or discarded "
            "from canonical outputs.",
        ],
        "sources": {},
    }
    frames = {}
    avl_gaps = None
    for source, path in paths.items():
        print(f"Reading and hashing {source} raw CSV", flush=True)
        metadata = raw_inventory(path)
        raw = read_raw(path)
        canonical = normalize_raw(raw, source, metadata["sha256"])
        write_parquet(output / f"{source}.parquet", canonical)
        print(f"Canonical {source}: {canonical.height:,} rows written", flush=True)
        common = source_common(raw, canonical, source)
        del raw
        details, gaps = (
            audit_avl(canonical) if source == "avl" else audit_apc(canonical)
        )
        write_parquet(output / f"{source}_gaps.parquet", gaps)
        inventory["sources"][source] = {**metadata, **common, "edges": details}
        write_json(output / "inventory_partial.json", inventory)
        frames[source] = canonical
        if source == "avl":
            avl_gaps = gaps
        print(f"Completed {source} edge inventory", flush=True)
    assert avl_gaps is not None
    coverage, days = coverage_audit(frames["avl"], frames["apc"], avl_gaps)
    inventory["coverage"] = coverage
    write_parquet(output / "vehicle_calendar_day_coverage.parquet", days)
    inventory["output_files"] = [
        "avl.parquet",
        "apc.parquet",
        "avl_gaps.parquet",
        "apc_gaps.parquet",
        "vehicle_calendar_day_coverage.parquet",
        "inventory.json",
    ]
    write_json(output / "inventory.json", inventory)
    print(
        json.dumps(
            {
                "avl_rows": frames["avl"].height,
                "apc_rows": frames["apc"].height,
                "vehicle_days_both": coverage["vehicle_calendar_days_both"],
                "output": str(output),
            },
            indent=2,
        ),
        flush=True,
    )
    return inventory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    run(args.output.resolve())


if __name__ == "__main__":
    main()
