"""Landing layer: archive identity, lineage, and structural failures."""

import warnings
import zipfile

import polars as pl
import pytest

from tests.conftest import make_feed_files, write_gtfs_zip
from ttc_tides.ingest.gtfs import (
    DICTIONARY_LINEAGE_COLUMNS,
    LINEAGE_COLUMNS,
    FatalGtfsError,
    inventory_archive,
    read_member_landing,
)


class TestArchiveIdentity:
    def test_archive_sha256_matches_file_bytes(self, gtfs_zip_factory):
        import hashlib

        path = gtfs_zip_factory()
        archive = inventory_archive(path)
        assert archive.archive_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_content_hash_ignores_member_order_and_compression(self, tmp_path):
        files = make_feed_files()
        a = write_gtfs_zip(tmp_path / "a.zip", files)
        b = write_gtfs_zip(
            tmp_path / "b.zip",
            files,
            reverse_member_order=True,
            compression=0,  # stored, not deflated
        )
        arch_a, arch_b = inventory_archive(a), inventory_archive(b)
        assert arch_a.archive_sha256 != arch_b.archive_sha256
        assert arch_a.content_hash == arch_b.content_hash
        assert arch_a.feed_id == arch_b.feed_id

    def test_content_hash_changes_when_any_member_changes(self, tmp_path):
        base = inventory_archive(write_gtfs_zip(tmp_path / "a.zip", make_feed_files()))
        changed_files = make_feed_files(
            stops_txt=make_feed_files()["stops.txt"].replace("First", "Renamed")
        )
        changed = inventory_archive(write_gtfs_zip(tmp_path / "b.zip", changed_files))
        assert base.content_hash != changed.content_hash
        assert base.feed_id != changed.feed_id

    def test_missing_required_member_is_reported(self, gtfs_zip_factory):
        archive = inventory_archive(
            gtfs_zip_factory(files=make_feed_files(stop_times_txt=None))
        )
        assert archive.missing_required_members() == ("stop_times.txt",)

    def test_calendar_or_calendar_dates_is_required(self, gtfs_zip_factory):
        archive = inventory_archive(
            gtfs_zip_factory(
                files=make_feed_files(
                    calendar_txt=None,
                    calendar_dates_txt=None,
                )
            )
        )
        assert archive.missing_required_members() == (
            "calendar.txt or calendar_dates.txt",
        )

    def test_empty_archive_is_fatal(self, tmp_path):
        path = tmp_path / "empty.zip"
        with zipfile.ZipFile(path, "w"):
            pass
        with pytest.raises(FatalGtfsError, match="no files"):
            inventory_archive(path)

    def test_duplicate_member_name_is_fatal(self, tmp_path):
        path = tmp_path / "duplicate.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("agency.txt", "agency_id,agency_name\n1,TTC\n")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                zf.writestr("agency.txt", "agency_id,agency_name\n1,TTC 2\n")
        with pytest.raises(FatalGtfsError, match="duplicate member"):
            inventory_archive(path)

    def test_invalid_zip_is_reported_as_fatal(self, tmp_path):
        path = tmp_path / "not-a.zip"
        path.write_text("not a zip archive")
        with pytest.raises(FatalGtfsError, match="invalid GTFS ZIP"):
            inventory_archive(path)


class TestLanding:
    def test_all_columns_are_strings_with_lineage(self, default_archive):
        df = read_member_landing(default_archive, "stops.txt")
        for column in LINEAGE_COLUMNS:
            assert column in df.columns
        for column in DICTIONARY_LINEAGE_COLUMNS:
            assert df.schema[column] == pl.Categorical
        value_columns = [c for c in df.columns if c not in LINEAGE_COLUMNS]
        assert all(df.schema[c] == pl.String for c in value_columns)
        # leading zeros and numeric-looking IDs stay verbatim
        assert df["stop_code"].to_list() == ["1001", "1002", "1003"]

    def test_row_numbers_are_one_based(self, default_archive):
        df = read_member_landing(default_archive, "trips.txt")
        assert df["_source_row_number"].to_list() == [1, 2, 3]

    def test_record_ids_are_deterministic_and_unique(self, default_archive):
        first = read_member_landing(default_archive, "stop_times.txt")
        second = read_member_landing(default_archive, "stop_times.txt")
        assert first["_record_id"].to_list() == second["_record_id"].to_list()
        assert first["_record_id"].n_unique() == first.height

    def test_bom_is_stripped_from_header(self, gtfs_zip_factory):
        files = make_feed_files()
        files["agency.txt"] = "﻿" + files["agency.txt"]
        archive = inventory_archive(gtfs_zip_factory(files=files))
        df = read_member_landing(archive, "agency.txt")
        assert "agency_id" in df.columns
        assert df["agency_name"].to_list() == ["TTC"]

    def test_empty_fields_become_null(self, gtfs_zip_factory):
        files = make_feed_files(
            stops_txt=(
                "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
                "S1,,First Stop,43.70,-79.40\n"
            )
        )
        archive = inventory_archive(gtfs_zip_factory(files=files))
        df = read_member_landing(archive, "stops.txt")
        assert df["stop_code"].to_list() == [None]

    def test_duplicate_header_column_is_fatal(self, gtfs_zip_factory):
        files = make_feed_files(routes_txt="route_id,route_id,route_type\nR1,R1,3\n")
        archive = inventory_archive(gtfs_zip_factory(files=files))
        with pytest.raises(FatalGtfsError, match="duplicated column"):
            read_member_landing(archive, "routes.txt")

    def test_reading_absent_member_is_fatal(self, gtfs_zip_factory):
        archive = inventory_archive(
            gtfs_zip_factory(files=make_feed_files(shapes_txt=None))
        )
        with pytest.raises(FatalGtfsError, match="shapes.txt"):
            read_member_landing(archive, "shapes.txt")
