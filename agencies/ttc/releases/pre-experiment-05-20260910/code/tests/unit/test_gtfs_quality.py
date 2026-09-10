"""Quality evaluation: referential integrity, plausibility, and reporting."""

import json

from tests.conftest import make_feed_files
from ttc_tides.ingest.gtfs import inventory_archive
from ttc_tides.normalize.gtfs import normalize_archive
from ttc_tides.quality.gtfs import (
    CODE_COORD_OUT_OF_RANGE,
    CODE_DEPARTURE_BEFORE_ARRIVAL,
    CODE_DUPLICATE_STOP_CODE,
    CODE_MEMBER_EMPTY,
    CODE_NON_MONOTONIC_TIMES,
    CODE_REF_STOP_TIME_STOP,
    CODE_REF_STOP_TIME_TRIP,
    CODE_REF_TRIP_ROUTE,
    CODE_REF_TRIP_SERVICE,
    CODE_SERVICE_WITHOUT_TRIPS,
    CODE_SHAPES_FILE_MISSING,
    CODE_STOP_SEQUENCE_NEGATIVE,
    CODE_TIME_TRAVEL_BETWEEN_STOPS,
    CODE_TRIP_WITHOUT_STOP_TIMES,
    evaluate_feed,
)


def report_for(gtfs_zip_factory, **overrides):
    files = make_feed_files(**overrides) if overrides else None
    archive = inventory_archive(gtfs_zip_factory(files=files))
    return evaluate_feed(normalize_archive(archive))


def finding_count(report, code):
    return sum(f.count for f in report.findings if f.code == code)


class TestCleanFeed:
    def test_default_feed_has_no_fatal_findings_or_quarantine(self, gtfs_zip_factory):
        report = report_for(gtfs_zip_factory)
        assert report.fatal_findings == []
        assert report.quarantined_rows == 0
        assert finding_count(report, CODE_REF_STOP_TIME_STOP) == 0

    def test_report_is_json_serializable_with_conservation_totals(
        self, gtfs_zip_factory
    ):
        report = report_for(gtfs_zip_factory)
        payload = json.loads(json.dumps(report.to_dict(), default=str))
        assert payload["feed_id"].startswith("ttc-")
        for counts in payload["members"].values():
            assert counts["landing_rows"] == (
                counts["normalized_rows"] + counts["quarantined_rows"]
            )

    def test_empty_calendar_is_allowed_when_dates_define_service(
        self, gtfs_zip_factory
    ):
        report = report_for(
            gtfs_zip_factory,
            calendar_txt=(
                "service_id,monday,tuesday,wednesday,thursday,friday,"
                "saturday,sunday,start_date,end_date\n"
            ),
            calendar_dates_txt=("service_id,date,exception_type\nWD,20231101,1\n"),
        )
        assert report.fatal_findings == []
        assert finding_count(report, CODE_MEMBER_EMPTY) == 1

    def test_only_service_calendar_with_no_usable_rows_is_fatal(self, gtfs_zip_factory):
        report = report_for(
            gtfs_zip_factory,
            calendar_txt=None,
            calendar_dates_txt="service_id,date,exception_type\n",
        )
        assert any(
            finding.code == CODE_MEMBER_EMPTY for finding in report.fatal_findings
        )


class TestReferentialIntegrity:
    def test_trip_referencing_unknown_route_and_service(self, gtfs_zip_factory):
        report = report_for(
            gtfs_zip_factory,
            trips_txt=(
                "route_id,service_id,trip_id\n"
                "70291,WD,T1\n"
                "MISSING_ROUTE,WD,T2\n"
                "70291,MISSING_SVC,T3\n"
            ),
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,9:00:00,9:00:00,S1,1\nT1,9:10:00,9:10:00,S2,2\n"
            ),
        )
        assert finding_count(report, CODE_REF_TRIP_ROUTE) == 1
        assert finding_count(report, CODE_REF_TRIP_SERVICE) == 1

    def test_stop_time_referencing_unknown_trip_and_stop(self, gtfs_zip_factory):
        report = report_for(
            gtfs_zip_factory,
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,9:00:00,9:00:00,S1,1\n"
                "T1,9:10:00,9:10:00,GHOST_STOP,2\n"
                "GHOST_TRIP,9:00:00,9:00:00,S1,1\n"
            ),
        )
        assert finding_count(report, CODE_REF_STOP_TIME_TRIP) == 1
        assert finding_count(report, CODE_REF_STOP_TIME_STOP) == 1


class TestPlausibility:
    def test_departure_before_arrival_and_non_monotonic_times(self, gtfs_zip_factory):
        report = report_for(
            gtfs_zip_factory,
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,9:00:00,8:59:00,S1,1\n"  # departs before it arrives
                "T1,9:10:00,9:10:00,S2,2\n"
                "T1,9:05:00,9:05:00,S3,3\n"  # goes back in time
            ),
        )
        assert finding_count(report, CODE_DEPARTURE_BEFORE_ARRIVAL) == 1
        assert finding_count(report, CODE_NON_MONOTONIC_TIMES) == 1

    def test_arrival_before_previous_departure_is_flagged(self, gtfs_zip_factory):
        report = report_for(
            gtfs_zip_factory,
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,9:00:00,9:10:00,S1,1\n"
                "T1,9:05:00,9:15:00,S2,2\n"
            ),
        )
        assert finding_count(report, CODE_TIME_TRAVEL_BETWEEN_STOPS) == 1

    def test_trip_without_stop_times_and_negative_sequence_are_flagged(
        self, gtfs_zip_factory
    ):
        report = report_for(
            gtfs_zip_factory,
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,9:00:00,9:00:00,S1,-1\n"
                "T1,9:10:00,9:10:00,S2,2\n"
            ),
        )
        assert finding_count(report, CODE_STOP_SEQUENCE_NEGATIVE) == 1
        # T2 and T3 are declared in trips.txt but have no stop_times.
        assert finding_count(report, CODE_TRIP_WITHOUT_STOP_TIMES) == 2

    def test_missing_shapes_file_is_flagged_when_trips_reference_shapes(
        self, gtfs_zip_factory
    ):
        report = report_for(gtfs_zip_factory, shapes_txt=None)
        assert finding_count(report, CODE_SHAPES_FILE_MISSING) == 3

    def test_out_of_range_coordinate_is_found_when_pair_is_incomplete(
        self, gtfs_zip_factory
    ):
        report = report_for(
            gtfs_zip_factory,
            stops_txt=(
                "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
                "S1,1001,First Stop,91,\n"
                "S2,1002,Second Stop,43.71,-79.41\n"
                "S3,1003,Third Stop,43.72,-79.42\n"
            ),
        )
        assert finding_count(report, CODE_COORD_OUT_OF_RANGE) == 1

    def test_duplicate_stop_codes_are_flagged_for_apc_crosswalk(self, gtfs_zip_factory):
        report = report_for(
            gtfs_zip_factory,
            stops_txt=(
                "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
                "S1,1001,First Stop,43.70,-79.40\n"
                "S2,1001,Second Stop,43.71,-79.41\n"  # same public code
                "S3,1003,Third Stop,43.72,-79.42\n"
            ),
        )
        assert finding_count(report, CODE_DUPLICATE_STOP_CODE) == 1

    def test_zombie_service_is_flagged(self, gtfs_zip_factory):
        report = report_for(
            gtfs_zip_factory,
            calendar_txt=(
                "service_id,monday,tuesday,wednesday,thursday,friday,"
                "saturday,sunday,start_date,end_date\n"
                "WD,1,1,1,1,1,0,0,20231101,20231130\n"
                "SUN,0,0,0,0,0,0,1,20231101,20231130\n"
                "ZOMBIE,1,1,1,1,1,1,1,20231101,20231130\n"  # no trips use it
            ),
        )
        assert finding_count(report, CODE_SERVICE_WITHOUT_TRIPS) == 1


class TestQuarantineAccounting:
    def test_quarantined_rows_appear_in_member_counts(self, gtfs_zip_factory):
        report = report_for(
            gtfs_zip_factory,
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,9:00:00,9:00:00,S1,1\n"
                "T1,not-a-time,9:10:00,S2,2\n"
            ),
        )
        member = report.member_counts["stop_times.txt"]
        assert member["landing_rows"] == 2
        assert member["normalized_rows"] == 1
        assert member["quarantined_rows"] == 1
        assert report.quarantined_rows == 1
        quarantine_findings = [f for f in report.findings if f.severity == "quarantine"]
        assert len(quarantine_findings) == 1
