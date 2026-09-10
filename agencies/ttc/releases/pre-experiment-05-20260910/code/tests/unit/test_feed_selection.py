"""Feed manifest and per-date selection: precedence, ties, and coverage."""

import datetime as dt

import polars as pl
import pytest

from tests.conftest import make_feed_files, write_gtfs_zip
from ttc_tides.ingest.gtfs import FatalGtfsError
from ttc_tides.reconcile.feeds import (
    build_manifest_draft,
    load_manifest,
    manifest_service_coverage,
    select_feed,
    write_manifest,
)

NOV = dt.date(2023, 11, 1)


def calendar_txt(service_id: str, start: str, end: str, days: str = "1,1,1,1,1,1,1"):
    return (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        f"start_date,end_date\n{service_id},{days},{start},{end}\n"
    )


@pytest.fixture
def overlapping_feeds_dir(tmp_path):
    """Two feeds overlapping Nov 1-18, one feed for Nov 19 onward.

    Mirrors the real fixture: `older`/`newer` both cover early November and a
    `next` revision takes over later; route 29 keeps its short name but
    changes route_id between revisions.
    """
    gtfs_dir = tmp_path / "gtfs"
    gtfs_dir.mkdir()
    write_gtfs_zip(
        gtfs_dir / "older.zip",
        make_feed_files(
            calendar_txt=calendar_txt("A", "20231001", "20231118"),
            calendar_dates_txt=None,
            routes_txt=(
                "route_id,agency_id,route_short_name,route_long_name,route_type\n"
                "70071,1,29,DUFFERIN,3\n"
            ),
            trips_txt=("route_id,service_id,trip_id\n70071,A,OLD1\n"),
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "OLD1,9:00:00,9:00:00,S1,1\nOLD1,9:10:00,9:10:00,S2,2\n"
            ),
        ),
    )
    write_gtfs_zip(
        gtfs_dir / "newer.zip",
        make_feed_files(
            calendar_txt=calendar_txt("B", "20231030", "20231118"),
            calendar_dates_txt=None,
            routes_txt=(
                "route_id,agency_id,route_short_name,route_long_name,route_type\n"
                "70291,1,29,DUFFERIN,3\n"
            ),
            trips_txt=("route_id,service_id,trip_id\n70291,B,NEW1\n"),
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "NEW1,9:00:00,9:00:00,S1,1\nNEW1,9:10:00,9:10:00,S2,2\n"
            ),
        ),
    )
    write_gtfs_zip(
        gtfs_dir / "next.zip",
        make_feed_files(
            calendar_txt=calendar_txt("C", "20231119", "20231223"),
            calendar_dates_txt=None,
            routes_txt=(
                "route_id,agency_id,route_short_name,route_long_name,route_type\n"
                "70507,1,29,DUFFERIN,3\n"
            ),
            trips_txt=("route_id,service_id,trip_id\n70507,C,NXT1\n"),
            stop_times_txt=(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "NXT1,9:00:00,9:00:00,S1,1\nNXT1,9:10:00,9:10:00,S2,2\n"
            ),
        ),
    )
    return gtfs_dir


def manifest_with_priorities(gtfs_dir, priorities: dict[str, int], **status):
    manifest = build_manifest_draft(gtfs_dir)
    return manifest.with_columns(
        pl.col("relative_path").replace_strict(priorities, default=0).alias("priority"),
        pl.col("relative_path")
        .replace_strict(status, default="approved")
        .alias("status"),
    )


class TestManifestDraft:
    def test_draft_records_identity_and_calendar_range(self, overlapping_feeds_dir):
        draft = build_manifest_draft(overlapping_feeds_dir)
        assert draft.height == 3
        older = draft.filter(pl.col("relative_path") == "older.zip").row(0, named=True)
        assert older["calendar_min"] == dt.date(2023, 10, 1)
        assert older["calendar_max"] == dt.date(2023, 11, 18)
        assert older["status"] == "candidate"
        assert len(older["archive_sha256"]) == 64
        assert draft["priority"].n_unique() == 3

    def test_roundtrip_and_duplicate_feed_id_rejected(
        self, overlapping_feeds_dir, tmp_path
    ):
        draft = build_manifest_draft(overlapping_feeds_dir)
        path = tmp_path / "manifest.csv"
        write_manifest(draft, path)
        loaded = load_manifest(path)
        assert loaded["feed_id"].to_list() == draft["feed_id"].to_list()

        duplicated = pl.concat([draft, draft.head(1)])
        write_manifest(duplicated, path)
        with pytest.raises(FatalGtfsError, match="duplicate feed_id"):
            load_manifest(path)

    def test_invalid_status_and_path_traversal_are_rejected(
        self, overlapping_feeds_dir, tmp_path
    ):
        draft = build_manifest_draft(overlapping_feeds_dir)
        path = tmp_path / "manifest.csv"

        write_manifest(draft.with_columns(pl.lit("mystery").alias("status")), path)
        with pytest.raises(FatalGtfsError, match="invalid status"):
            load_manifest(path)

        write_manifest(
            draft.with_columns(pl.lit("../outside.zip").alias("relative_path")),
            path,
        )
        with pytest.raises(FatalGtfsError, match="relative_path"):
            load_manifest(path)

    def test_missing_content_hash_and_incomplete_range_are_rejected(
        self, overlapping_feeds_dir, tmp_path
    ):
        draft = build_manifest_draft(overlapping_feeds_dir)
        path = tmp_path / "manifest.csv"

        write_manifest(
            draft.with_columns(pl.lit(None).cast(pl.String).alias("content_hash")),
            path,
        )
        with pytest.raises(FatalGtfsError, match="content_hash"):
            load_manifest(path)

        write_manifest(
            draft.with_columns(pl.lit(None).cast(pl.Date).alias("calendar_min")),
            path,
        )
        with pytest.raises(FatalGtfsError, match="both calendar_min"):
            load_manifest(path)

    def test_coverage_rejects_modified_archive(self, overlapping_feeds_dir, tmp_path):
        manifest = build_manifest_draft(overlapping_feeds_dir).with_columns(
            pl.lit("approved").alias("status")
        )
        # simulate the input changing after the manifest was pinned
        target = overlapping_feeds_dir / "older.zip"
        target.write_bytes(target.read_bytes() + b"tampered")
        with pytest.raises(FatalGtfsError, match="SHA-256 does not match"):
            manifest_service_coverage(manifest, overlapping_feeds_dir)

    def test_manifest_symlink_cannot_escape_gtfs_root(
        self, overlapping_feeds_dir, tmp_path
    ):
        outside = tmp_path / "outside.zip"
        write_gtfs_zip(outside, make_feed_files())
        (overlapping_feeds_dir / "escape.zip").symlink_to(outside)
        manifest = (
            build_manifest_draft(overlapping_feeds_dir)
            .filter(pl.col("relative_path") == "escape.zip")
            .with_columns(pl.lit("approved").alias("status"))
        )
        with pytest.raises(FatalGtfsError, match="escapes GTFS directory"):
            manifest_service_coverage(manifest, overlapping_feeds_dir)


class TestSelection:
    def test_draft_candidate_is_not_selected_without_explicit_opt_in(
        self, overlapping_feeds_dir
    ):
        manifest = build_manifest_draft(overlapping_feeds_dir)
        coverage = manifest_service_coverage(manifest, overlapping_feeds_dir)
        selection = select_feed(manifest, coverage, dt.date(2023, 11, 10))
        assert not selection.selected

        coverage = manifest_service_coverage(
            manifest,
            overlapping_feeds_dir,
            allow_candidate=True,
        )
        selection = select_feed(
            manifest,
            coverage,
            dt.date(2023, 11, 10),
            allow_candidate=True,
        )
        assert selection.selected

    def test_higher_priority_wins_and_candidates_are_recorded(
        self, overlapping_feeds_dir
    ):
        manifest = manifest_with_priorities(
            overlapping_feeds_dir,
            {"older.zip": 10, "newer.zip": 20, "next.zip": 30},
        )
        coverage = manifest_service_coverage(manifest, overlapping_feeds_dir)
        selection = select_feed(manifest, coverage, dt.date(2023, 11, 10))
        newer_id = manifest.filter(pl.col("relative_path") == "newer.zip")["feed_id"][0]
        assert selection.feed_id == newer_id
        assert len(selection.candidate_feed_ids) == 2
        assert "highest_priority=20" in selection.reason

    def test_handover_to_next_revision(self, overlapping_feeds_dir):
        manifest = manifest_with_priorities(
            overlapping_feeds_dir,
            {"older.zip": 10, "newer.zip": 20, "next.zip": 30},
        )
        coverage = manifest_service_coverage(manifest, overlapping_feeds_dir)
        next_id = manifest.filter(pl.col("relative_path") == "next.zip")["feed_id"][0]
        assert select_feed(manifest, coverage, dt.date(2023, 11, 19)).feed_id == (
            next_id
        )

    def test_priority_tie_is_fatal(self, overlapping_feeds_dir):
        manifest = manifest_with_priorities(
            overlapping_feeds_dir,
            {"older.zip": 10, "newer.zip": 10, "next.zip": 30},
        )
        coverage = manifest_service_coverage(manifest, overlapping_feeds_dir)
        with pytest.raises(FatalGtfsError, match="tie"):
            select_feed(manifest, coverage, dt.date(2023, 11, 10))

    def test_uncovered_date_returns_no_feed(self, overlapping_feeds_dir):
        manifest = manifest_with_priorities(
            overlapping_feeds_dir,
            {"older.zip": 10, "newer.zip": 20, "next.zip": 30},
        )
        coverage = manifest_service_coverage(manifest, overlapping_feeds_dir)
        selection = select_feed(manifest, coverage, dt.date(2024, 6, 1))
        assert not selection.selected
        assert selection.reason == "no_feed_with_active_service"

    def test_date_scoping_skips_unrelated_archives(self, overlapping_feeds_dir):
        manifest = manifest_with_priorities(
            overlapping_feeds_dir,
            {"older.zip": 10, "newer.zip": 20, "next.zip": 30},
        )
        # This revision cannot cover Nov 10 according to its pinned bounds, so
        # a date-scoped lookup should not read or hash it.
        target = overlapping_feeds_dir / "next.zip"
        target.write_bytes(target.read_bytes() + b"tampered")
        coverage = manifest_service_coverage(
            manifest,
            overlapping_feeds_dir,
            service_dates=(dt.date(2023, 11, 10),),
        )
        assert coverage["service_date"].unique().to_list() == [dt.date(2023, 11, 10)]

    def test_superseded_feed_is_excluded(self, overlapping_feeds_dir):
        manifest = manifest_with_priorities(
            overlapping_feeds_dir,
            {"older.zip": 10, "newer.zip": 20, "next.zip": 30},
            **{"newer.zip": "superseded"},
        )
        coverage = manifest_service_coverage(manifest, overlapping_feeds_dir)
        selection = select_feed(manifest, coverage, dt.date(2023, 11, 10))
        older_id = manifest.filter(pl.col("relative_path") == "older.zip")["feed_id"][0]
        assert selection.feed_id == older_id

    def test_selection_uses_expanded_services_not_calendar_range(self, tmp_path):
        """A feed whose calendar range covers a date but with no active weekday
        service must not be a candidate for it."""
        gtfs_dir = tmp_path / "gtfs"
        gtfs_dir.mkdir()
        write_gtfs_zip(
            gtfs_dir / "weekend_only.zip",
            make_feed_files(
                calendar_txt=calendar_txt(
                    "WKND", "20231101", "20231130", days="0,0,0,0,0,1,1"
                ),
                calendar_dates_txt=None,
            ),
        )
        manifest = build_manifest_draft(gtfs_dir).with_columns(
            pl.lit("approved").alias("status")
        )
        coverage = manifest_service_coverage(manifest, gtfs_dir)
        # 2023-11-08 is a Wednesday: in range, but no service runs
        selection = select_feed(manifest, coverage, dt.date(2023, 11, 8))
        assert not selection.selected
        # 2023-11-11 is a Saturday: service runs
        assert select_feed(manifest, coverage, dt.date(2023, 11, 11)).selected
