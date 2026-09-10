"""Schedule derivation: calendar expansion, wall-clock times, and patterns."""

import datetime as dt
from zoneinfo import ZoneInfo

import polars as pl

from tests.conftest import make_feed_files
from ttc_tides.ingest.gtfs import inventory_archive
from ttc_tides.normalize.gtfs import normalize_archive
from ttc_tides.reconcile.schedule import (
    derive_patterns,
    expand_daily_services,
    feed_daily_services,
    scheduled_stop_events_for_date,
    scheduled_trips_for_date,
    service_day_anchor,
)

TORONTO = ZoneInfo("America/Toronto")


def normalized_default(gtfs_zip_factory, **overrides):
    files = make_feed_files(**overrides) if overrides else None
    return normalize_archive(inventory_archive(gtfs_zip_factory(files=files)))


class TestCalendarExpansion:
    def test_weekday_flags_are_respected(self, gtfs_zip_factory):
        feed = normalized_default(gtfs_zip_factory)
        services = feed_daily_services(feed)
        wd = services.filter(pl.col("service_id") == "WD")["service_date"].to_list()
        # weekdays only, minus the removed Nov 6, plus the added Saturday Nov 11
        assert dt.date(2023, 11, 1) in wd  # Wednesday
        assert dt.date(2023, 11, 4) not in wd  # Saturday, not added
        assert dt.date(2023, 11, 5) not in wd  # Sunday
        sun = services.filter(pl.col("service_id") == "SUN")["service_date"].to_list()
        assert dt.date(2023, 11, 5) in sun
        assert dt.date(2023, 11, 6) not in sun

    def test_calendar_dates_exceptions_take_precedence(self, gtfs_zip_factory):
        feed = normalized_default(gtfs_zip_factory)
        wd = (
            feed_daily_services(feed)
            .filter(pl.col("service_id") == "WD")["service_date"]
            .to_list()
        )
        assert dt.date(2023, 11, 11) in wd  # Saturday added by exception_type=1
        assert dt.date(2023, 11, 6) not in wd  # Monday removed by exception_type=2

    def test_calendar_dates_can_be_absent(self, gtfs_zip_factory):
        feed = normalized_default(gtfs_zip_factory, calendar_dates_txt=None)
        wd = (
            feed_daily_services(feed)
            .filter(pl.col("service_id") == "WD")["service_date"]
            .to_list()
        )
        assert dt.date(2023, 11, 6) in wd  # nothing removes it now

    def test_calendar_dates_only_feed_is_supported(self, gtfs_zip_factory):
        feed = normalized_default(
            gtfs_zip_factory,
            calendar_txt=None,
            calendar_dates_txt=(
                "service_id,date,exception_type\nWD,20231101,1\nWD,20231102,1\n"
            ),
        )
        services = feed_daily_services(feed)
        assert services.select("service_id", "service_date").rows() == [
            ("WD", dt.date(2023, 11, 1)),
            ("WD", dt.date(2023, 11, 2)),
        ]

    def test_inverted_calendar_range_produces_no_days(self):
        calendar = pl.DataFrame(
            {
                "service_id": ["X"],
                "monday": [1],
                "tuesday": [1],
                "wednesday": [1],
                "thursday": [1],
                "friday": [1],
                "saturday": [1],
                "sunday": [1],
                "start_date": [dt.date(2023, 11, 30)],
                "end_date": [dt.date(2023, 11, 1)],
            }
        )
        assert expand_daily_services(calendar, None).is_empty()


class TestScheduledTrips:
    def test_only_active_services_produce_trips(self, gtfs_zip_factory):
        feed = normalized_default(gtfs_zip_factory)
        # Wednesday Nov 1: WD trips T1, T2 but not Sunday trip T3
        trips = scheduled_trips_for_date(feed, dt.date(2023, 11, 1))
        assert sorted(trips["trip_id"].to_list()) == ["T1", "T2"]
        # Sunday Nov 5: only T3
        sunday = scheduled_trips_for_date(feed, dt.date(2023, 11, 5))
        assert sunday["trip_id"].to_list() == ["T3"]

    def test_route_context_is_version_specific(self, gtfs_zip_factory):
        feed = normalized_default(gtfs_zip_factory)
        trips = scheduled_trips_for_date(feed, dt.date(2023, 11, 1))
        assert set(trips["route_id"].to_list()) == {"70291"}
        assert set(trips["route_short_name"].to_list()) == {"29"}


class TestStopEventWallClock:
    def test_post_midnight_time_lands_on_next_calendar_day(self, gtfs_zip_factory):
        feed = normalized_default(gtfs_zip_factory)
        # Sunday Nov 12 (a normal day): T3 runs at 25:30 -> Monday 01:30 local
        events = scheduled_stop_events_for_date(feed, dt.date(2023, 11, 12))
        first = events.filter(pl.col("stop_sequence") == 1).row(0, named=True)
        arrival = first["arrival_datetime"]
        assert arrival.astimezone(TORONTO) == dt.datetime(
            2023, 11, 13, 1, 30, tzinfo=TORONTO
        )

    def test_dst_fall_back_day_uses_elapsed_time_from_noon_anchor(
        self, gtfs_zip_factory
    ):
        """2023-11-05 is the America/Toronto fall-back date (25-hour day).

        Noon on Nov 5 is already EST, so the GTFS anchor (noon minus 12 h) is
        05:00 UTC — one hour *after* local midnight (which was still EDT).
        25:30 elapsed from that anchor is 06:30 UTC = 01:30 EST on Nov 6.
        A naive local-midnight anchor would yield 00:30 instead.
        """
        feed = normalized_default(gtfs_zip_factory)
        events = scheduled_stop_events_for_date(feed, dt.date(2023, 11, 5))
        first = events.filter(pl.col("stop_sequence") == 1).row(0, named=True)
        anchor = service_day_anchor(dt.date(2023, 11, 5), "America/Toronto")
        assert anchor == dt.datetime(2023, 11, 5, 5, 0, tzinfo=dt.UTC)
        naive_midnight = dt.datetime(2023, 11, 5, 0, 0, tzinfo=TORONTO)
        assert anchor == naive_midnight.astimezone(dt.UTC) + dt.timedelta(hours=1)
        arrival_utc = first["arrival_datetime"].astimezone(dt.UTC)
        assert arrival_utc == anchor + dt.timedelta(hours=25, minutes=30)
        local = first["arrival_datetime"].astimezone(TORONTO)
        assert (local.hour, local.minute) == (1, 30)
        assert local.date() == dt.date(2023, 11, 6)

    def test_anchor_is_local_midnight_on_normal_days(self):
        anchor = service_day_anchor(dt.date(2023, 11, 12), "America/Toronto")
        assert anchor == dt.datetime(2023, 11, 12, 0, 0, tzinfo=TORONTO).astimezone(
            dt.UTC
        )

    def test_events_carry_stop_code_for_apc_crosswalk(self, gtfs_zip_factory):
        feed = normalized_default(gtfs_zip_factory)
        events = scheduled_stop_events_for_date(feed, dt.date(2023, 11, 1))
        assert set(events["stop_code"].to_list()) == {"1001", "1002", "1003"}


class TestPatterns:
    def test_pattern_ids_are_deterministic_across_runs(self, gtfs_zip_factory):
        feed = normalized_default(gtfs_zip_factory)
        patterns_a, trip_patterns_a = derive_patterns(feed)
        patterns_b, trip_patterns_b = derive_patterns(feed)
        assert patterns_a.sort("pattern_id").equals(patterns_b.sort("pattern_id"))
        assert trip_patterns_a.equals(trip_patterns_b)

    def test_same_path_same_id_across_feeds(self, tmp_path, gtfs_zip_factory):
        from tests.conftest import write_gtfs_zip

        feed_a = normalized_default(gtfs_zip_factory)
        # a different archive (different agency name => different feed_id)
        files = make_feed_files(
            agency_txt=(
                "agency_id,agency_name,agency_url,agency_timezone\n"
                "1,TTC v2,http://www.ttc.ca,America/Toronto\n"
            )
        )
        feed_b = normalize_archive(
            inventory_archive(write_gtfs_zip(tmp_path / "b.zip", files))
        )
        assert feed_a.archive.feed_id != feed_b.archive.feed_id
        ids_a = dict(derive_patterns(feed_a)[1].select("trip_id", "pattern_id").rows())
        ids_b = dict(derive_patterns(feed_b)[1].select("trip_id", "pattern_id").rows())
        assert ids_a == ids_b

    def test_route_id_revision_does_not_change_public_pattern_id(
        self, tmp_path, gtfs_zip_factory
    ):
        from tests.conftest import write_gtfs_zip

        feed_a = normalized_default(gtfs_zip_factory)
        files = make_feed_files(
            routes_txt=(
                "route_id,agency_id,route_short_name,route_long_name,route_type\n"
                "NEW29,1,29,DUFFERIN,3\n"
                "NEW929,1,929,DUFFERIN EXPRESS,3\n"
            ),
            trips_txt=(
                "route_id,service_id,trip_id,trip_headsign,direction_id,"
                "block_id,shape_id\n"
                "NEW29,WD,T1,NORTH,0,B1,SH1\n"
                "NEW29,WD,T2,SOUTH,1,B1,SH1\n"
                "NEW29,SUN,T3,NORTH - LATE,0,B2,SH1\n"
            ),
        )
        feed_b = normalize_archive(
            inventory_archive(write_gtfs_zip(tmp_path / "route-revision.zip", files))
        )
        ids_a = dict(derive_patterns(feed_a)[1].select("trip_id", "pattern_id").rows())
        ids_b = dict(derive_patterns(feed_b)[1].select("trip_id", "pattern_id").rows())
        assert ids_a == ids_b

    def test_pattern_encoding_cannot_collide_on_delimiters(self, gtfs_zip_factory):
        feed = normalized_default(
            gtfs_zip_factory,
            stops_txt=(
                "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
                '"A,B",1001,First Stop,43.70,-79.40\n'
                "C,1002,Second Stop,43.71,-79.41\n"
                "A,1003,Third Stop,43.72,-79.42\n"
                '"B,C",1004,Fourth Stop,43.73,-79.43\n'
            ),
            trips_txt=(
                "route_id,service_id,trip_id,direction_id\n"
                "70291,WD,T1,0\n"
                "70291,WD,T2,0\n"
            ),
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                'T1,9:00:00,9:00:00,"A,B",1\n'
                "T1,9:10:00,9:10:00,C,2\n"
                "T2,10:00:00,10:00:00,A,1\n"
                'T2,10:10:00,10:10:00,"B,C",2\n'
            ),
        )
        _, trip_patterns = derive_patterns(feed)
        ids = dict(trip_patterns.select("trip_id", "pattern_id").rows())
        assert ids["T1"] != ids["T2"]

    def test_direction_and_order_change_the_pattern(self, gtfs_zip_factory):
        feed = normalized_default(gtfs_zip_factory)
        _, trip_patterns = derive_patterns(feed)
        ids = dict(trip_patterns.select("trip_id", "pattern_id").rows())
        # T1 (S1->S2->S3, dir 0) vs T2 (S3->S2->S1, dir 1) vs T3 (dir 0, same
        # path as T1): T1 and T3 share a pattern, T2 does not.
        assert ids["T1"] == ids["T3"]
        assert ids["T1"] != ids["T2"]

    def test_loop_route_repeated_stop_is_preserved(self, gtfs_zip_factory):
        feed = normalized_default(
            gtfs_zip_factory,
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,9:00:00,9:00:00,S1,1\n"
                "T1,9:10:00,9:10:00,S2,2\n"
                "T1,9:20:00,9:20:00,S1,3\n"  # returns to S1: a loop
            ),
        )
        patterns, trip_patterns = derive_patterns(feed)
        t1_pattern = trip_patterns.filter(pl.col("trip_id") == "T1")["pattern_id"][0]
        row = patterns.filter(pl.col("pattern_id") == t1_pattern).row(0, named=True)
        assert row["stop_path"] == ["S1", "S2", "S1"]
        assert row["stop_count"] == 3

    def test_orphan_stop_time_does_not_create_a_pattern(self, gtfs_zip_factory):
        feed = normalized_default(
            gtfs_zip_factory,
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,9:00:00,9:00:00,S1,1\n"
                "GHOST,10:00:00,10:00:00,S2,1\n"
            ),
        )
        _, trip_patterns = derive_patterns(feed)
        assert trip_patterns["trip_id"].to_list() == ["T1"]

    def test_pattern_id_is_versioned(self, gtfs_zip_factory):
        feed = normalized_default(gtfs_zip_factory)
        patterns, _ = derive_patterns(feed)
        ids = patterns["pattern_id"].to_list()
        assert all(p.startswith("p2-") for p in ids)
        assert all(len(p.removeprefix("p2-")) == 32 for p in ids)
