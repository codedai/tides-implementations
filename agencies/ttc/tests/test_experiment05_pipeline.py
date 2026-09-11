"""Real ten-stage Experiment 05 on tiny, independently generated transit inputs.

Only fake-workspace Git/code metadata is stubbed. Source normalization, GTFS ZIP
loading, canonical construction, projection, plateau/segmentation, APC attachment,
continuous scoring, assignment, Parquet persistence and TIDES validation are real.
"""

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import make_feed_files, write_gtfs_zip
from tests.unit.test_source_normalize import APC_HEADER, AVL_HEADER, apc_row, avl_row
from tools.experiment05 import STAGES, pipeline
from tools.experiment05.config import DEFAULT_CONFIG, load_config
from tools.experiment05.io import read_json, read_rows, sha256


@pytest.fixture
def tiny_project(tmp_path, monkeypatch):
    root = tmp_path / "synthetic-project"
    root.mkdir()
    avl, apc = [], []
    for day in ("2024-04-01", "2024-04-02"):
        start = datetime.fromisoformat(day + "T09:00:30")
        for index in range(21):
            row = avl_row(
                date_key=day.replace("-", ""),
                ts=(start + timedelta(seconds=index * 15)).strftime(
                    "%Y-%m-%d %H:%M:%S.000"
                ),
                vehicle="synthetic-bus",
                lat=f"{43.65 + index * 0.0005:.7f}",
                lon="-79.4",
                heading="0",
                route="29",
                run="synthetic-run",
                dest="NORTH - synthetic route",
                kph="14",
                tatrip="not-a-GTFS-id",
            )
            avl.append(row)
            if index == 4:
                avl.append(row)  # Same physical payload, distinct original CSV row.
        for index, (clock, stop, lat) in enumerate(
            [
                ("09:03:00", "1002", "43.655"),
                ("09:05:30", "1003", "43.66"),
            ]
        ):
            arrival = datetime.fromisoformat(day + "T" + clock)
            apc.append(
                apc_row(
                    idx=str(index),
                    vehicle="synthetic-bus",
                    stop=stop,
                    route="29",
                    branch="synthetic-branch",
                    direction="NORTH",
                    lat=lat,
                    lon="-79.4",
                    arrive=arrival.strftime("%Y-%m-%d %H:%M:%S"),
                    depart=(arrival + timedelta(seconds=10)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    boarding="2",
                    alighting="1",
                    avg_otp="99999",
                )
            )
    (root / "avl.csv").write_text(AVL_HEADER + "\n" + "\n".join(avl) + "\n")
    (root / "apc.csv").write_text(APC_HEADER + "\n" + "\n".join(apc) + "\n")
    stop_times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence,timepoint"]
    trips = ["route_id,service_id,trip_id,direction_id,shape_id,block_id"]
    for name, clock in [
        ("earlier", "08:50:00"),
        ("near", "09:00:00"),
        ("later", "09:10:00"),
    ]:
        trips.append(f"synthetic-route,S,{name},0,shape-a,block-a")
        start = datetime.fromisoformat("2024-04-01T" + clock)
        for i, offset in enumerate([0, 150, 300], 1):
            instant = (start + timedelta(seconds=offset)).strftime("%H:%M:%S")
            stop_times.append(f"{name},{instant},{instant},S{i},{i},1")
    files = make_feed_files(
        routes_txt=(
            "route_id,agency_id,route_short_name,route_long_name,route_type\n"
            "synthetic-route,1,29,Synthetic route,3\n"
        ),
        calendar_txt="service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nS,1,1,1,1,1,1,1,20240401,20240402\n",
        calendar_dates_txt=None,
        trips_txt="\n".join(trips) + "\n",
        stop_times_txt="\n".join(stop_times) + "\n",
        stops_txt="stop_id,stop_code,stop_name,stop_lat,stop_lon\nS1,1001,Start,43.65,-79.4\nS2,1002,Middle,43.655,-79.4\nS3,1003,End,43.66,-79.4\n",
        shapes_txt="shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nshape-a,43.65,-79.4,1\nshape-a,43.655,-79.4,2\nshape-a,43.66,-79.4,3\n",
    )
    archive = write_gtfs_zip(root / "feed.zip", files)
    metadata = {
        "feed_id": "synthetic-feed",
        "relative_path": archive.name,
        "archive_sha256": sha256(archive),
        "calendar_min": "2024-04-01",
        "calendar_max": "2024-04-02",
        "priority": 1,
        "status": "approved",
    }
    with (root / "manifest.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(metadata))
        writer.writeheader()
        writer.writerow(metadata)
    config = DEFAULT_CONFIG.read_text()
    defaults = load_config()[0]
    for key, value in {
        "avl_path": "avl.csv",
        "apc_path": "apc.csv",
        "feed_manifest_path": "manifest.csv",
        "feed_gtfs_root": ".",
    }.items():
        config = config.replace(
            f'{key} = "{defaults["inputs"][key]}"', f'{key} = "{value}"'
        )
    config = config.replace("workers = 8", "workers = 1")
    config_path = root / "config.toml"
    config_path.write_text(config)
    assert load_config(config_path)[0]["execution"]["workers"] == 1
    monkeypatch.setattr(pipeline, "code_identity", lambda root: ("test-code-v1", {}))
    monkeypatch.setattr(
        pipeline.subprocess, "check_output", lambda *a, **k: "synthetic-commit\n"
    )
    return {"root": root, "config_path": config_path}


@pytest.mark.parametrize("day", ["2024-04-01", "2024-04-02"])
def test_real_ten_stage_pipeline_and_four_tides_packages(tiny_project, day):
    source_pins = {
        name: sha256(tiny_project["root"] / name)
        for name in ["avl.csv", "apc.csv", "feed.zip"]
    }
    progress = []
    result = pipeline.run_pipeline(day, progress=progress.append, **tiny_project)
    assert not result["cached"]
    manifest = result["run_manifest"]
    assert manifest["validation_ok"] and manifest["status"] == "complete"
    assert set(manifest["stages"]) == set(STAGES)
    assert all(row["status"] == "complete" for row in manifest["stages"].values())
    folder = Path(result["run_dir"])
    assert read_json(folder / "validation_report.json")["valid"]
    raw = read_rows(folder / "stages/raw_observations.parquet")
    observations = read_rows(folder / "fused_projected_observations.parquet")
    assert len(raw) == len(observations) == 24
    assert {r["rid"] for r in raw} == {r["rid"] for r in observations}
    assert sum(bool(r.get("duplicate_of")) for r in observations) == 1
    assert (
        sum(
            r["source"] == "avl" and r["projection_status"] == "ON_REFERENCE"
            for r in observations
        )
        == 21
    )
    assert all(
        not r["trajectory_included"] for r in observations if r["source"] == "apc"
    )
    ranking = read_rows(folder / "candidate_rankings.parquet")
    assignments = read_rows(folder / "assignments.parquet")
    assert len(ranking) == 12 and len(assignments) == 4
    assert {r["match_mode"] for r in ranking} == {"global", "early", "middle", "late"}
    assert all(r["trip_id_scheduled"] == "near" for r in assignments)
    for mode in ("global", "early", "middle", "late"):
        report = read_json(folder / "tides" / mode / "validation_report.json")
        assert (
            report["valid"]
            and report["profile_valid"]
            and report["serialized_resources_valid"]
        )
        with (folder / "tides" / mode / "trips_performed.csv").open() as stream:
            trip = next(csv.DictReader(stream))
        assert trip["service_date"] == day and trip["trip_id_scheduled"] == "near"
        assert not (folder / "tides" / mode / mode).exists()
        with (folder / "tides" / mode / "passenger_events.csv").open() as stream:
            events = list(csv.DictReader(stream))
        assert len(events) == 8  # Two visits, each with four distinct event types.
        visits = read_rows(folder / "stop_visits_evidence.parquet")
        assert len(visits) == 2
        assert {row["trip_id_scheduled"] for row in events} == {"near"}
        assert {row["trip_stop_sequence"] for row in events} == {"1", "2"}
        for sequence in ("1", "2"):
            selected = {
                row["event_type"]: row
                for row in events
                if row["trip_stop_sequence"] == sequence
            }
            assert set(selected) == {
                "Vehicle arrived at stop",
                "Vehicle departed stop",
                "Passenger boarded",
                "Passenger alighted",
            }
            assert selected["Passenger boarded"]["event_count"] == "2"
            assert selected["Passenger alighted"]["event_count"] == "1"
            departure = selected["Vehicle departed stop"]["event_timestamp"]
            assert selected["Passenger boarded"]["event_timestamp"] == departure
            assert selected["Passenger alighted"]["event_timestamp"] == departure
    assert source_pins == {
        name: sha256(tiny_project["root"] / name) for name in source_pins
    }
    assert [r["stage"] for r in progress if r["state"] == "complete"] == list(STAGES)


def test_success_cache_and_force_preserve_business_rows(tiny_project):
    first = pipeline.run_pipeline("2024-04-01", **tiny_project)
    cached = pipeline.run_pipeline("2024-04-01", **tiny_project)
    assert cached["cached"] and cached["run_dir"] == first["run_dir"]
    forced = pipeline.run_pipeline("2024-04-01", force=True, **tiny_project)
    assert not forced["cached"] and forced["run_dir"] != first["run_dir"]
    for name in (
        "candidate_rankings.parquet",
        "assignments.parquet",
        "performed_trip_segments.parquet",
    ):
        assert read_rows(Path(first["run_dir"]) / name) == read_rows(
            Path(forced["run_dir"]) / name
        )
    assert pipeline.verify_completed(first["run_dir"])["validation_ok"]


def test_export_policy_does_not_change_matching_or_source_evidence(tiny_project):
    revised = pipeline.run_pipeline("2024-04-01", **tiny_project)
    policy_path = tiny_project["root"] / "conservative.toml"
    policy_path.write_text(
        tiny_project["config_path"]
        .read_text()
        .replace(
            'passenger_event_policy = "apc_stop_interval_departure_proxy_v1"',
            'passenger_event_policy = "omit_without_event_semantics"',
        )
    )
    conservative = pipeline.run_pipeline(
        "2024-04-01", root=tiny_project["root"], config_path=policy_path
    )
    new, old = Path(revised["run_dir"]), Path(conservative["run_dir"])
    assert new != old  # A changed export policy must not reuse the old cache.
    for relative in (
        "stages/raw_observations.parquet",
        "stages/normalized_observations.parquet",
        "stages/event_timeline.parquet",
        "stages/projected_observations.parquet",
        "performed_trip_segments.parquet",
        "fused_projected_observations.parquet",
        "stop_visits_evidence.parquet",
        "candidate_rankings.parquet",
        "local_window_evidence.parquet",
        "assignments.parquet",
        "assignment_audit.parquet",
    ):
        assert read_rows(new / relative) == read_rows(old / relative)
    for mode in ("global", "early", "middle", "late"):
        for name in ("trips_performed", "stop_visits", "vehicle_locations", "vehicles"):
            assert (new / "tides" / mode / f"{name}.csv").read_bytes() == (
                old / "tides" / mode / f"{name}.csv"
            ).read_bytes()
        assert (new / "tides" / mode / "passenger_events.csv").is_file()
        assert not (old / "tides" / mode / "passenger_events.csv").exists()


def test_stage_prerequisite_and_completed_stage_idempotency(tiny_project):
    run = pipeline.initialize_run("2024-04-01", **tiny_project)
    with pytest.raises(ValueError, match="prerequisite"):
        pipeline.run_stage("normalize", run["run_dir"], root=tiny_project["root"])
    first = pipeline.run_stage("load_raw", run["run_dir"], root=tiny_project["root"])
    second = pipeline.run_stage("load_raw", run["run_dir"], root=tiny_project["root"])
    assert first == second
    assert first["row_counts"]["observations"] == 24


def test_changed_raw_input_after_initialization_is_rejected(tiny_project):
    run = pipeline.initialize_run("2024-04-01", **tiny_project)
    raw = tiny_project["root"] / "avl.csv"
    raw.write_text(raw.read_text() + "\n")
    with pytest.raises(ValueError, match="changed before"):
        pipeline.run_stage("load_raw", run["run_dir"], root=tiny_project["root"])
    manifest = read_json(Path(run["run_dir"]) / "run_manifest.json")
    assert manifest["status"] == "failed" and not manifest["validation_ok"]


def test_missing_date_never_becomes_empty_success(tiny_project):
    with pytest.raises(ValueError, match="Missing AVL or APC"):
        pipeline.initialize_run("2024-04-03", **tiny_project)
    assert not (tiny_project["root"] / "outputs").exists()


def test_changed_completed_prerequisite_rejected_before_next_stage(tiny_project):
    run = pipeline.initialize_run("2024-04-01", **tiny_project)
    pipeline.run_stage("load_raw", run["run_dir"], root=tiny_project["root"])
    raw = Path(run["run_dir"]) / "stages/raw_observations.parquet"
    raw.write_bytes(raw.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="changed"):
        pipeline.run_stage("normalize", run["run_dir"], root=tiny_project["root"])


def test_later_mode_failure_preserves_completed_package_and_resumes(
    tiny_project, monkeypatch
):
    """Inject an I/O boundary failure, never replace the real business stages."""
    from tools.experiment05 import tides

    run = pipeline.initialize_run("2024-04-01", **tiny_project)
    folder = Path(run["run_dir"])
    for stage in STAGES[: STAGES.index("map_tides")]:
        pipeline.run_stage(stage, folder, root=tiny_project["root"])
    ranking_digest = sha256(folder / "candidate_rankings.parquet")
    original_publish = tides.publish_tides

    def fail_after_global(output_dir, mode, *args, **kwargs):
        if mode == "early":
            raise OSError("Injected later-mode publication failure")
        return original_publish(output_dir, mode, *args, **kwargs)

    monkeypatch.setattr(tides, "publish_tides", fail_after_global)
    with pytest.raises(OSError, match="Injected later-mode"):
        pipeline.run_stage("map_tides", folder, root=tiny_project["root"])
    completed_global = folder / "tides/global"
    pins = {
        str(path.relative_to(completed_global)): sha256(path)
        for path in completed_global.rglob("*")
        if path.is_file()
    }
    assert tides.read_completed_tides(completed_global, "global")["validation"]["valid"]
    assert not (folder / "tides/early").exists()
    assert not read_json(folder / "run_manifest.json")["validation_ok"]
    monkeypatch.setattr(tides, "publish_tides", original_publish)
    pipeline.run_stage("map_tides", folder, root=tiny_project["root"])
    pipeline.run_stage("validate_publish", folder, root=tiny_project["root"])
    assert pipeline.verify_completed(folder)["validation_ok"]
    assert sha256(folder / "candidate_rankings.parquet") == ranking_digest
    assert pins == {
        str(path.relative_to(completed_global)): sha256(path)
        for path in completed_global.rglob("*")
        if path.is_file()
    }
    assert set(read_json(folder / "tides_reports.json")) == {
        "global",
        "early",
        "middle",
        "late",
    }
