"""CLI: offline commands over synthetic archives."""

import datetime as dt
import json

import polars as pl
import pytest

from tests.conftest import make_feed_files, write_gtfs_zip
from ttc_tides.cli import main
from ttc_tides.reconcile.feeds import build_manifest_draft, write_manifest


@pytest.fixture
def gtfs_dir(tmp_path):
    d = tmp_path / "gtfs"
    d.mkdir()
    write_gtfs_zip(d / "nov.zip", make_feed_files())
    return d


@pytest.fixture
def manifest_path(gtfs_dir, tmp_path):
    path = tmp_path / "feed_manifest.csv"
    manifest = build_manifest_draft(gtfs_dir).with_columns(
        pl.lit("approved").alias("status")
    )
    write_manifest(manifest, path)
    return path


def test_inventory_lists_archives(gtfs_dir, capsys):
    assert main(["gtfs", "inventory", "--gtfs-dir", str(gtfs_dir)]) == 0
    out = capsys.readouterr().out
    assert "nov.zip" in out
    assert "ttc-" in out


def test_manifest_writes_draft_and_refuses_overwrite(gtfs_dir, tmp_path, capsys):
    out_path = tmp_path / "draft.csv"
    args = [
        "gtfs",
        "manifest",
        "--gtfs-dir",
        str(gtfs_dir),
        "--out",
        str(out_path),
    ]
    assert main(args) == 0
    assert out_path.is_file()
    assert pl.read_csv(out_path).height == 1
    # a second run must not clobber the reviewed file silently
    assert main(args) == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert main([*args, "--force"]) == 0


def test_select_resolves_feed_for_date(gtfs_dir, manifest_path, capsys):
    code = main(
        [
            "gtfs",
            "select",
            "--gtfs-dir",
            str(gtfs_dir),
            "--manifest",
            str(manifest_path),
            "--date",
            "2023-11-01",
            "--date",
            "2024-06-01",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "highest_priority" in out
    assert "no_feed_with_active_service" in out


def test_process_writes_artifacts_and_quality_report(
    gtfs_dir, manifest_path, tmp_path, capsys
):
    out_dir = tmp_path / "run"
    code = main(
        [
            "gtfs",
            "process",
            "--gtfs-dir",
            str(gtfs_dir),
            "--manifest",
            str(manifest_path),
            "--date",
            "2023-11-01",
            "--out",
            str(out_dir),
        ]
    )
    assert code == 0
    trips = pl.read_parquet(out_dir / "matched" / "gtfs" / "scheduled_trips.parquet")
    assert sorted(trips["trip_id"].to_list()) == ["T1", "T2"]
    events = pl.read_parquet(
        out_dir / "matched" / "gtfs" / "scheduled_stop_events.parquet"
    )
    assert events.height == 6
    report = json.loads((out_dir / "quality" / "gtfs_quality_report.json").read_text())
    assert report["summary"]["fatal_findings"] == 0
    assert report["selection"]["service_date"] == "2023-11-01"
    normalized = out_dir / "normalized" / "gtfs"
    assert (normalized / "stop_times.parquet").is_file()


def test_process_fatal_quality_writes_diagnostics_but_no_matches(tmp_path):
    gtfs_dir = tmp_path / "gtfs"
    gtfs_dir.mkdir()
    write_gtfs_zip(
        gtfs_dir / "empty-routes.zip",
        make_feed_files(
            routes_txt=(
                "route_id,agency_id,route_short_name,route_long_name,route_type\n"
            )
        ),
    )
    manifest_path = tmp_path / "manifest.csv"
    write_manifest(
        build_manifest_draft(gtfs_dir).with_columns(pl.lit("approved").alias("status")),
        manifest_path,
    )
    out_dir = tmp_path / "run"
    code = main(
        [
            "gtfs",
            "process",
            "--gtfs-dir",
            str(gtfs_dir),
            "--manifest",
            str(manifest_path),
            "--date",
            "2023-11-01",
            "--out",
            str(out_dir),
        ]
    )
    assert code == 1
    report = json.loads((out_dir / "quality" / "gtfs_quality_report.json").read_text())
    assert report["summary"]["fatal_findings"] == 1
    assert not (out_dir / "matched").exists()


def test_process_rejects_unknown_timezone_before_creating_output(
    gtfs_dir, manifest_path, tmp_path, capsys
):
    out_dir = tmp_path / "run"
    code = main(
        [
            "gtfs",
            "process",
            "--gtfs-dir",
            str(gtfs_dir),
            "--manifest",
            str(manifest_path),
            "--date",
            "2023-11-01",
            "--timezone",
            "Not/A_Timezone",
            "--out",
            str(out_dir),
        ]
    )
    assert code == 1
    assert "unknown IANA timezone" in capsys.readouterr().err
    assert not out_dir.exists()


def test_process_is_deterministic_across_runs(gtfs_dir, manifest_path, tmp_path):
    outputs = []
    for name in ("run_a", "run_b"):
        out_dir = tmp_path / name
        assert (
            main(
                [
                    "gtfs",
                    "process",
                    "--gtfs-dir",
                    str(gtfs_dir),
                    "--manifest",
                    str(manifest_path),
                    "--date",
                    "2023-11-01",
                    "--out",
                    str(out_dir),
                ]
            )
            == 0
        )
        outputs.append(out_dir)
    for artifact in (
        "matched/gtfs/scheduled_trips.parquet",
        "matched/gtfs/scheduled_stop_events.parquet",
        "matched/gtfs/patterns.parquet",
        "normalized/gtfs/stop_times.parquet",
    ):
        a = pl.read_parquet(outputs[0] / artifact)
        b = pl.read_parquet(outputs[1] / artifact)
        # _ingest_run_id is the only column allowed to differ between runs
        drop = [c for c in a.columns if c == "_ingest_run_id"]
        assert a.drop(drop).equals(b.drop(drop)), artifact


def test_evaluate_clean_feed_exits_zero(gtfs_dir, tmp_path, capsys):
    out_dir = tmp_path / "quality"
    code = main(
        [
            "gtfs",
            "evaluate",
            "--gtfs-dir",
            str(gtfs_dir),
            "--all",
            "--out",
            str(out_dir),
        ]
    )
    assert code == 0
    reports = list(out_dir.glob("*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text())
    assert payload["summary"]["fatal_findings"] == 0


def test_evaluate_refuses_stale_output_unless_forced(gtfs_dir, tmp_path, capsys):
    out_dir = tmp_path / "quality"
    args = [
        "gtfs",
        "evaluate",
        "--gtfs-dir",
        str(gtfs_dir),
        "--all",
        "--out",
        str(out_dir),
    ]
    assert main(args) == 0
    stale = out_dir / "stale.json"
    stale.write_text("stale")
    assert main(args) == 1
    assert stale.is_file()
    assert main([*args, "--force"]) == 0
    assert not stale.exists()


def test_evaluate_invalid_zip_reports_fatal_instead_of_crashing(tmp_path, capsys):
    gtfs_dir = tmp_path / "gtfs"
    gtfs_dir.mkdir()
    (gtfs_dir / "broken.zip").write_text("not a ZIP")
    code = main(
        [
            "gtfs",
            "evaluate",
            "--gtfs-dir",
            str(gtfs_dir),
            "--all",
            "--out",
            str(tmp_path / "quality"),
        ]
    )
    assert code == 1
    assert "FATAL" in capsys.readouterr().out


def test_evaluate_broken_feed_exits_nonzero(tmp_path, capsys):
    gtfs_dir = tmp_path / "gtfs"
    gtfs_dir.mkdir()
    write_gtfs_zip(gtfs_dir / "broken.zip", make_feed_files(stop_times_txt=None))
    code = main(
        [
            "gtfs",
            "evaluate",
            "--gtfs-dir",
            str(gtfs_dir),
            "--all",
            "--out",
            str(tmp_path / "quality"),
        ]
    )
    assert code == 1
    assert "FATAL" in capsys.readouterr().out


def test_select_requires_valid_date(gtfs_dir, manifest_path):
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "gtfs",
                "select",
                "--gtfs-dir",
                str(gtfs_dir),
                "--manifest",
                str(manifest_path),
                "--date",
                "notadate",
            ]
        )
    assert excinfo.value.code == 2


def test_stale_manifest_pin_is_fatal(gtfs_dir, manifest_path, capsys):
    # modify the archive after the manifest pinned its hash
    target = gtfs_dir / "nov.zip"
    target.write_bytes(target.read_bytes() + b"tampered")
    code = main(
        [
            "gtfs",
            "select",
            "--gtfs-dir",
            str(gtfs_dir),
            "--manifest",
            str(manifest_path),
            "--date",
            "2023-11-01",
        ]
    )
    assert code == 1
    assert "SHA-256 does not match" in capsys.readouterr().err


def test_process_date_derives_correct_dst_wall_clock(gtfs_dir, manifest_path, tmp_path):
    """End-to-end: on the DST fall-back date, 25:30 resolves to 01:30 EST
    (noon-minus-12h anchor), not the naive midnight-anchored 00:30."""
    out_dir = tmp_path / "dst_run"
    assert (
        main(
            [
                "gtfs",
                "process",
                "--gtfs-dir",
                str(gtfs_dir),
                "--manifest",
                str(manifest_path),
                "--date",
                "2023-11-05",
                "--out",
                str(out_dir),
            ]
        )
        == 0
    )
    events = pl.read_parquet(
        out_dir / "matched" / "gtfs" / "scheduled_stop_events.parquet"
    )
    first = events.filter(pl.col("stop_sequence") == 1).row(0, named=True)
    local = first["arrival_datetime"]
    assert local.date() == dt.date(2023, 11, 6)
    assert (local.hour, local.minute) == (1, 30)


def test_process_refuses_to_write_inside_source_boundary(
    gtfs_dir, manifest_path, capsys
):
    code = main(
        [
            "gtfs",
            "process",
            "--gtfs-dir",
            str(gtfs_dir),
            "--manifest",
            str(manifest_path),
            "--date",
            "2023-11-01",
            "--out",
            str(gtfs_dir / "generated"),
        ]
    )
    assert code == 1
    assert "source-data boundary" in capsys.readouterr().err
