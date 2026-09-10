"""Normalization: typed casts, quarantine reasons, and row conservation."""

import polars as pl
import pytest

from tests.conftest import make_feed_files
from ttc_tides.ingest.gtfs import FatalGtfsError, inventory_archive
from ttc_tides.normalize.gtfs import (
    REASON_DUPLICATE_PRIMARY_KEY,
    REASON_INVALID_DATE,
    REASON_INVALID_FLOAT,
    REASON_INVALID_TIME,
    REASON_MISSING_REQUIRED_VALUE,
    REASON_VALUE_NOT_ALLOWED,
    normalize_archive,
)


def normalize_single(gtfs_zip_factory, member: str, **overrides):
    archive = inventory_archive(gtfs_zip_factory(files=make_feed_files(**overrides)))
    return normalize_archive(archive).members[member]


class TestTypedCasts:
    def test_ids_stay_strings_and_numbers_become_typed(self, default_archive):
        feed = normalize_archive(default_archive)
        stops = feed.table("stops.txt")
        assert stops.schema["stop_id"] == pl.String
        assert stops.schema["stop_code"] == pl.String
        assert stops.schema["stop_lat"] == pl.Float64
        trips = feed.table("trips.txt")
        assert trips.schema["direction_id"] == pl.Int64
        calendar = feed.table("calendar.txt")
        assert calendar.schema["start_date"] == pl.Date

    def test_gtfs_times_parse_including_unpadded_and_past_24h(self, default_archive):
        feed = normalize_archive(default_archive)
        stop_times = feed.table("stop_times.txt")
        t1_first = stop_times.filter(
            (pl.col("trip_id") == "T1") & (pl.col("stop_sequence") == 1)
        )
        # "9:00:00" (unpadded) and the original string is preserved
        assert t1_first["arrival_time_secs"].item() == 9 * 3600
        assert t1_first["arrival_time"].item() == "9:00:00"
        t3_first = stop_times.filter(
            (pl.col("trip_id") == "T3") & (pl.col("stop_sequence") == 1)
        )
        assert t3_first["arrival_time_secs"].item() == 25 * 3600 + 30 * 60

    def test_invalid_time_is_quarantined_with_reason(self, gtfs_zip_factory):
        member = normalize_single(
            gtfs_zip_factory,
            "stop_times.txt",
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,9:00:00,9:00:00,S1,1\n"
                "T1,9:5:00,9:05:00,S2,2\n"  # unpadded minutes are invalid
                "T1,banana,9:10:00,S3,3\n"
            ),
        )
        assert member.normalized.height == 1
        assert member.quarantined.height == 2
        assert member.reason_counts == {f"{REASON_INVALID_TIME}:arrival_time": 2}

    def test_invalid_date_is_quarantined(self, gtfs_zip_factory):
        member = normalize_single(
            gtfs_zip_factory,
            "calendar.txt",
            calendar_txt=(
                "service_id,monday,tuesday,wednesday,thursday,friday,"
                "saturday,sunday,start_date,end_date\n"
                "WD,1,1,1,1,1,0,0,2023-11-01,20231130\n"  # ISO date is invalid
                "OK,1,1,1,1,1,0,0,20231101,20231130\n"
            ),
        )
        assert member.normalized["service_id"].to_list() == ["OK"]
        assert f"{REASON_INVALID_DATE}:start_date" in member.reason_counts

    def test_calendar_flags_outside_01_are_quarantined(self, gtfs_zip_factory):
        member = normalize_single(
            gtfs_zip_factory,
            "calendar.txt",
            calendar_txt=(
                "service_id,monday,tuesday,wednesday,thursday,friday,"
                "saturday,sunday,start_date,end_date\n"
                "BAD,2,1,1,1,1,0,0,20231101,20231130\n"
            ),
        )
        assert member.normalized.is_empty()
        assert f"{REASON_VALUE_NOT_ALLOWED}:monday" in member.reason_counts

    def test_non_finite_float_is_quarantined(self, gtfs_zip_factory):
        member = normalize_single(
            gtfs_zip_factory,
            "stops.txt",
            stops_txt=(
                "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
                "S1,1001,Infinite Stop,inf,-79.40\n"
            ),
        )
        assert member.normalized.is_empty()
        assert f"{REASON_INVALID_FLOAT}:stop_lat" in member.reason_counts


class TestStructureAndConservation:
    def test_missing_required_column_is_fatal(self, gtfs_zip_factory):
        with pytest.raises(FatalGtfsError, match="trip_id"):
            normalize_single(
                gtfs_zip_factory,
                "trips.txt",
                trips_txt="route_id,service_id\n70291,WD\n",
            )

    def test_missing_both_service_calendar_members_is_fatal(self, gtfs_zip_factory):
        archive = inventory_archive(
            gtfs_zip_factory(
                files=make_feed_files(
                    calendar_txt=None,
                    calendar_dates_txt=None,
                )
            )
        )
        with pytest.raises(FatalGtfsError, match="calendar.txt or calendar_dates.txt"):
            normalize_archive(archive)

    def test_conditionally_required_time_columns_must_exist(self, gtfs_zip_factory):
        with pytest.raises(FatalGtfsError, match="arrival_time"):
            normalize_single(
                gtfs_zip_factory,
                "stop_times.txt",
                stop_times_txt=(
                    "trip_id,departure_time,stop_id,stop_sequence\nT1,9:00:00,S1,1\n"
                ),
            )

    def test_missing_required_value_is_quarantined(self, gtfs_zip_factory):
        member = normalize_single(
            gtfs_zip_factory,
            "trips.txt",
            trips_txt=(
                "route_id,service_id,trip_id\n"
                "70291,WD,T1\n"
                "70291,WD,\n"
                "70291,WD,   \n"  # whitespace-only is also missing
            ),
        )
        assert member.normalized["trip_id"].to_list() == ["T1"]
        assert member.reason_counts == {f"{REASON_MISSING_REQUIRED_VALUE}:trip_id": 2}

    def test_absent_optional_column_becomes_null(self, gtfs_zip_factory):
        member = normalize_single(
            gtfs_zip_factory,
            "trips.txt",
            trips_txt="route_id,service_id,trip_id\n70291,WD,T1\n",
        )
        assert member.normalized["direction_id"].to_list() == [None]
        assert member.normalized["block_id"].to_list() == [None]

    def test_duplicate_primary_key_quarantines_every_ambiguous_row(
        self, gtfs_zip_factory
    ):
        member = normalize_single(
            gtfs_zip_factory,
            "trips.txt",
            trips_txt=(
                "route_id,service_id,trip_id,trip_headsign\n"
                "70291,WD,T1,FIRST\n"
                "70291,WD,T1,SECOND\n"
            ),
        )
        assert member.normalized.is_empty()
        assert member.reason_counts == {REASON_DUPLICATE_PRIMARY_KEY: 2}

    def test_duplicate_key_uses_parsed_integer_value(self, gtfs_zip_factory):
        member = normalize_single(
            gtfs_zip_factory,
            "stop_times.txt",
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,9:00:00,9:00:00,S1,1\n"
                "T1,9:01:00,9:01:00,S2,01\n"
            ),
        )
        assert member.normalized.is_empty()
        assert member.reason_counts == {REASON_DUPLICATE_PRIMARY_KEY: 2}

    def test_conservation_holds_for_every_member(self, default_archive):
        feed = normalize_archive(default_archive)
        for member in feed.members.values():
            member.check_conservation()  # raises on violation
            assert member.landing_rows == (
                member.normalized.height + member.quarantined.height
            )

    def test_quarantine_preserves_lineage(self, gtfs_zip_factory):
        member = normalize_single(
            gtfs_zip_factory,
            "trips.txt",
            trips_txt=("route_id,service_id,trip_id\n70291,WD,T1\n70291,WD,\n"),
        )
        row = member.quarantined.row(0, named=True)
        assert row["_source_member"] == "trips.txt"
        assert row["_source_row_number"] == 2
        assert row["_record_id"]
