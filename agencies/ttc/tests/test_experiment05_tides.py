"""Synthetic export contracts; never read or publish historical experiment data."""

import copy
import csv
import json
import shutil
from datetime import UTC, datetime

import pytest

from tools.experiment05 import tides


@pytest.fixture
def evidence():
    instant = datetime(2023, 11, 6, 6, tzinfo=UTC).timestamp()
    segments = [
        {
            "trip_id_performed": "performed-a",
            "service_date": "2023-11-06",
            "route_id": "route-a",
            "direction_id": 0,
            "gtfs_feed_version": "feed-a",
            "vehicle_id": "0007",
            "raw_segment_start": instant,
            "raw_segment_end": instant + 120,
            "effective_segment_start": instant,
            "effective_segment_end": instant + 60,
        }
    ]
    observations = [
        {
            "rid": "avl-a",
            "source": "avl",
            "source_row": 1,
            "source_file_sha256": "a" * 64,
            "vehicle_id": "0007",
            "device_id": "gps-a",
            "t": instant,
            "lat": 43.7,
            "lon": -79.4,
            "heading": 0.0,
            "speed_kph": 36.0,
            "trip_id_performed": "performed-a",
            "trajectory_included": True,
            "projection_status": "PROJECTED",
        },
        {
            "rid": "avl-b",
            "source": "avl",
            "source_row": 2,
            "source_file_sha256": "a" * 64,
            "vehicle_id": "0007",
            "t": instant + 120,
            "lat": 44.1,
            "lon": -79.7,
            "heading": 45.0,
            "speed_kph": 0.0,
            "trip_id_performed": "performed-a",
            "trajectory_included": False,
            "projection_status": "OFF_REFERENCE",
        },
        {
            "rid": "apc-a",
            "source": "apc",
            "source_row": 17,
            "source_file_sha256": "b" * 64,
            "vehicle_id": "0007",
            "t": instant + 30,
            "lat": 43.7,
            "lon": -79.4,
            "trip_id_performed": "performed-a",
        },
    ]
    visits = [
        {
            "stop_visit_id": "visit-a",
            "trip_id_performed": "performed-a",
            "service_date": "2023-11-06",
            "vehicle_id": "0007",
            "stop_id": "stop-a",
            "trip_stop_sequence": 1,
            "arrival_t": instant + 10,
            "departure_t": instant + 40,
            "boarding": 3,
            "alighting": 0,
            "source_rids": ["apc-a"],
        }
    ]
    assignments = [
        {
            "trip_id_performed": "performed-a",
            "match_mode": "global",
            "trip_id_scheduled": "scheduled-a",
            "assignment_round": 1,
            "candidate_rank": 1,
            "score": 1e12,
            "calibrated": False,
        }
    ]
    bundle = {
        "feed_version": "feed-a",
        "route_id": "route-a",
        "stops": [{"stop_id": "stop-a"}, {"stop_id": "stop-b"}],
        "trips": [
            {
                "trip_id": "scheduled-a",
                "service_date": "2023-11-06",
                "feed_version": "feed-a",
                "route_id": "route-a",
                "direction_id": "0",
                "shape_id": "shape-a",
                "block_id": "block-a",
                "stop_times": [
                    {
                        "stop_id": "stop-a",
                        "stop_sequence": 7,
                        "arrival_t": instant,
                        "departure_t": instant + 10,
                    },
                    {
                        "stop_id": "stop-b",
                        "stop_sequence": 9,
                        "arrival_t": instant + 600,
                        "departure_t": instant + 600,
                    },
                ],
            }
        ],
    }
    return {
        "segments": segments,
        "observations": observations,
        "stop_visits": visits,
        "assignments": assignments,
        "gtfs_bundle": bundle,
        "config": {},
    }


def build(evidence, mode="global"):
    return tides.build_tides_rows(mode, **evidence)


def test_vendored_schemas_exact_pins_and_no_invented_core_columns():
    manifest = tides.verify_schemas()
    assert manifest["commit"] == tides.SPEC_COMMIT
    assert manifest["license"] == "CC-BY-4.0"
    assert "route_id" not in tides._row("vehicle_locations", {})
    assert "trip_id_scheduled" not in tides._row("stop_visits", {})
    with pytest.raises(ValueError, match="Unknown TIDES core"):
        tides._row("stop_visits", {"trip_id_scheduled": "invented"})


def test_assignment_no_calibration_threshold_and_off_reference_preserved(evidence):
    before = copy.deepcopy(evidence)
    tables, diagnostics = build(evidence)
    assert tables["trips_performed"][0]["trip_id_scheduled"] == "scheduled-a"
    assert tables["trips_performed"][0]["actual_trip_start"] is None
    assert tables["trips_performed"][0]["actual_trip_end"] is None
    assert len(tables["vehicle_locations"]) == 2
    assert tables["vehicle_locations"][0]["speed"] == 10.0
    assert tables["vehicle_locations"][1]["latitude"] == 44.1
    assert tables["passenger_events"] == []
    assert diagnostics["research_only"] and not diagnostics["calibrated_certification"]
    assert evidence == before
    assert tides.validate_tides_rows(tables, evidence["gtfs_bundle"])["valid"]


@pytest.mark.parametrize("mode", ["global", "early", "middle", "late"])
def test_modes_are_independent_publications(evidence, tmp_path, mode):
    evidence["assignments"][0]["match_mode"] = mode
    result = tides.publish_tides(tmp_path / mode, mode, **evidence)
    assert result["validation"]["valid"]
    assert result["validation"]["serialized_resources_valid"]
    assert result["summary"]["assigned_scheduled_trips"] == 1
    root = tmp_path / mode
    assert (root / "completion.json").exists()
    assert (root / "validation_report.json").exists()
    assert not (root / mode).exists()
    assert (root / "schemas/trips_performed.schema.json").exists()
    assert not (root / "passenger_events.csv").exists()
    descriptor = json.loads((root / "datapackage.json").read_text())
    assert all(r["schema"].startswith("schemas/") for r in descriptor["resources"])
    with (root / "trips_performed.csv").open() as stream:
        row = next(csv.DictReader(stream))
    assert row["actual_trip_start"] == "" and row["actual_trip_end"] == ""
    assert row["vehicle_id"] == "0007"
    assert row["schedule_trip_start"] == "2023-11-06T06:00:10Z"
    assert row["trip_id_scheduled"] == "scheduled-a"
    assert "null" not in row.values() and "None" not in row.values()


def test_publish_is_append_only_and_deterministic(evidence, tmp_path):
    first = tides.publish_tides(tmp_path / "first", "global", **evidence)
    second = tides.publish_tides(tmp_path / "second", "global", **evidence)
    assert first["validation"]["output_sha256"] == second["validation"]["output_sha256"]
    before = (tmp_path / "first/completion.json").read_bytes()
    with pytest.raises(ValueError, match="new directory"):
        tides.publish_tides(tmp_path / "first", "global", **evidence)
    assert (tmp_path / "first/completion.json").read_bytes() == before


def test_unmatched_is_true_null_not_unscheduled_fact(evidence):
    evidence["assignments"][0]["trip_id_scheduled"] = None
    tables, _ = build(evidence)
    trip = tables["trips_performed"][0]
    assert trip["trip_id_scheduled"] is None and trip["schedule_relationship"] is None
    assert tables["vehicle_locations"][0]["trip_id_scheduled"] is None


@pytest.mark.parametrize(
    "patch",
    [
        {"trip_id_scheduled": "absent"},
        {"gtfs_feed_version": "wrong-feed"},
        {"trip_id_performed": "absent"},
        {"service_date": "2023-11-07"},
    ],
)
def test_assignment_foreign_keys_fail_closed(evidence, patch):
    evidence["assignments"][0].update(patch)
    with pytest.raises(ValueError):
        build(evidence)


def test_one_to_one_occurrences_and_performed_keys(evidence):
    evidence["segments"].append(
        {**evidence["segments"][0], "trip_id_performed": "performed-b"}
    )
    evidence["assignments"].append(
        {**evidence["assignments"][0], "trip_id_performed": "performed-b"}
    )
    with pytest.raises(ValueError, match="more than once"):
        build(evidence)


def test_supported_scheduled_sequence_not_raw_actual_sequence(evidence):
    evidence["stop_visits"][0]["scheduled_stop_sequence"] = 7
    tables, _ = build(evidence)
    visit = tables["stop_visits"][0]
    assert visit["trip_stop_sequence"] == 1 and visit["scheduled_stop_sequence"] == 7
    assert visit["schedule_arrival_time"] == "2023-11-06T06:00:00Z"
    assert visit["boarding_1"] is None and visit["departure_load"] is None
    evidence["stop_visits"][0]["scheduled_stop_sequence"] = 9
    with pytest.raises(ValueError, match="Scheduled visit foreign key"):
        build(evidence)


def test_repeated_stops_remain_distinct_actual_visits(evidence):
    evidence["stop_visits"].append(
        {
            **evidence["stop_visits"][0],
            "stop_visit_id": "visit-b",
            "trip_stop_sequence": 2,
            "source_rids": [],
            "arrival_t": evidence["stop_visits"][0]["arrival_t"] + 60,
            "departure_t": evidence["stop_visits"][0]["departure_t"] + 60,
        }
    )
    tables, _ = build(evidence)
    assert [r["trip_stop_sequence"] for r in tables["stop_visits"]] == [1, 2]
    assert tides.validate_tides_rows(tables, evidence["gtfs_bundle"])["valid"]


def test_incomplete_sequence_unknown_columns_and_bad_fks_fail(evidence):
    tables, _ = build(evidence)
    tables["stop_visits"][0]["trip_stop_sequence"] = 2
    report = tides.validate_tides_rows(tables, evidence["gtfs_bundle"])
    assert not report["valid"]
    assert "ACTUAL_VISIT_SEQUENCE_NOT_CONSECUTIVE" in {
        e["reason"] for e in report["errors"]
    }
    tables, _ = build(evidence)
    tables["stop_visits"][0]["trip_id_scheduled"] = "not-a-core-column"
    assert not tides.validate_tides_rows(tables)["valid"]
    tables, _ = build(evidence)
    tables["vehicle_locations"][0]["trip_id_performed"] = "absent"
    assert not tides.validate_tides_rows(tables)["valid"]
    tables, _ = build(evidence)
    tables["trips_performed"][0]["trip_id_scheduled"] = "absent"
    assert not tides.validate_tides_rows(tables, evidence["gtfs_bundle"])["valid"]


def test_passenger_events_require_explicit_semantics_and_event_time(evidence):
    visit = evidence["stop_visits"][0]
    visit.update(
        passenger_event_semantics_valid=True, boarding_event_t=visit["arrival_t"] + 2
    )
    tables, _ = build(evidence)
    event = tables["passenger_events"][0]
    assert event["event_count"] == 3 and event["event_type"] == "Passenger boarded"
    assert event["location_ping_id"] is None
    assert tides.validate_tides_rows(tables, evidence["gtfs_bundle"])["valid"]
    visit["boarding_event_t"] = visit["departure_t"] + 1
    with pytest.raises(ValueError, match="outside"):
        build(evidence)


@pytest.mark.parametrize("count", [2.5, -1, True, float("nan"), float("inf")])
def test_bad_counts_are_not_rounded_to_events(evidence, count):
    evidence["stop_visits"][0].update(
        boarding=count,
        passenger_event_semantics_valid=True,
        boarding_event_t=evidence["stop_visits"][0]["arrival_t"],
    )
    tables, diagnostics = build(evidence)
    assert not tables["passenger_events"]
    assert diagnostics["omission_counts"]["COUNT_SEMANTICS_INVALID_NO_ROUNDING"] == 1


def test_inverted_intervals_fail_and_fractional_dwell_is_not_rounded(evidence):
    visit = evidence["stop_visits"][0]
    visit["departure_t"] = visit["arrival_t"] - 1
    with pytest.raises(ValueError, match="Inverted"):
        build(evidence)
    visit["departure_t"] = visit["arrival_t"] + 1.5
    tables, diagnostics = build(evidence)
    assert tables["stop_visits"][0]["dwell"] is None
    assert diagnostics["omission_counts"]["NONINTEGER_SECONDS_NO_ROUNDING"] == 1


def test_utc_timestamps_do_not_guess_dst_fold_or_service_date(evidence):
    # Two instants in Toronto's repeated 01:30 hour stay distinct in UTC.
    evidence["observations"][0]["t"] = datetime(
        2023, 11, 5, 5, 30, tzinfo=UTC
    ).timestamp()
    evidence["observations"][1]["t"] = datetime(
        2023, 11, 5, 6, 30, tzinfo=UTC
    ).timestamp()
    tables, _ = build(evidence)
    assert [r["event_timestamp"] for r in tables["vehicle_locations"]] == [
        "2023-11-05T05:30:00Z",
        "2023-11-05T06:30:00Z",
    ]
    assert tables["trips_performed"][0]["service_date"] == "2023-11-06"
    evidence["observations"][0]["t"] = "2023-11-05 01:30:00"
    with pytest.raises(ValueError, match="UTC epoch"):
        build(evidence)


def test_unresolved_time_preserves_explicit_omission_and_unlinked_gps(evidence):
    evidence["observations"][0]["t"] = None
    evidence["observations"][1]["trip_id_performed"] = None
    tables, diagnostics = build(evidence)
    assert len(tables["vehicle_locations"]) == 1
    assert tables["vehicle_locations"][0]["service_date"] is None
    assert diagnostics["omission_counts"]["TIME_UNRESOLVED"] == 1


def test_changed_schema_pin_is_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr(tides, "SCHEMA_ROOT", tmp_path)
    (tmp_path / "vendor.json").write_text(
        json.dumps({"commit": "wrong", "tag": "v1.0"})
    )
    with pytest.raises(ValueError, match="revision"):
        tides.verify_schemas()


def test_failed_validation_does_not_publish_destination(evidence, tmp_path):
    evidence["stop_visits"][0]["stop_id"] = "not-in-feed"
    with pytest.raises(ValueError, match="validation failed"):
        tides.publish_tides(tmp_path / "invalid", "global", **evidence)
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong_vehicle"])
def test_visit_source_identity_cannot_be_invented_or_double_attributed(
    evidence, change
):
    if change == "missing":
        evidence["stop_visits"][0]["source_rids"] = ["absent"]
    elif change == "duplicate":
        evidence["stop_visits"][0]["source_rids"] = ["apc-a", "apc-a"]
    else:
        evidence["observations"][-1]["vehicle_id"] = "wrong"
    with pytest.raises(ValueError):
        build(evidence)


def test_visit_order_is_numeric_not_lexicographic(evidence):
    template = evidence["stop_visits"][0]
    evidence["stop_visits"] = [
        {
            **template,
            "stop_visit_id": f"visit-{i}",
            "trip_stop_sequence": i,
            "source_rids": [],
        }
        for i in reversed(range(1, 13))
    ]
    tables, _ = build(evidence)
    assert [row["trip_stop_sequence"] for row in tables["stop_visits"]] == list(
        range(1, 13)
    )


def test_header_only_resources_report_zero_evidence_honestly(evidence, tmp_path):
    for key in ("segments", "observations", "stop_visits", "assignments"):
        evidence[key] = []
    result = tides.publish_tides(tmp_path / "empty", "global", **evidence)
    assert result["validation"]["valid"]
    assert not result["validation"]["semantic_certification"]
    assert all(count == 0 for count in result["summary"]["rows"].values())


def test_valid_passenger_event_csv_and_optional_real_apc_coordinates(
    evidence, tmp_path
):
    visit = evidence["stop_visits"][0]
    visit.update(
        passenger_event_semantics_valid=True,
        boarding_event_t=visit["arrival_t"],
        alighting=2,
        alighting_event_t=visit["departure_t"],
    )
    evidence["observations"][-1]["genuine_vehicle_coordinates"] = True
    result = tides.publish_tides(tmp_path / "events", "global", **evidence)
    assert result["validation"]["valid"]
    assert result["summary"]["rows"]["passenger_events"] == 2
    assert result["summary"]["rows"]["vehicle_locations"] == 3
    with (tmp_path / "events/passenger_events.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert {row["event_count"] for row in rows} == {"2", "3"}
    assert all(row["location_ping_id"] == "" for row in rows)


def test_after_midnight_utc_schedule_and_service_date_stay_distinct(evidence):
    trip = evidence["gtfs_bundle"]["trips"][0]
    base = datetime(2023, 11, 7, 6, tzinfo=UTC).timestamp()
    trip["stop_times"][0]["departure_t"] = base
    trip["stop_times"][-1]["arrival_t"] = base + 600
    tables, _ = build(evidence)
    assert tables["trips_performed"][0]["service_date"] == "2023-11-06"
    assert tables["trips_performed"][0]["schedule_trip_start"] == "2023-11-07T06:00:00Z"


def test_source_avg_otp_never_affects_export(evidence):
    original = build(evidence)
    evidence["observations"][-1]["AvgOTP"] = 1e9
    evidence["stop_visits"][0]["avg_otp"] = -1e9
    assert build(evidence) == original


def test_unknown_internal_direction_becomes_null_only_in_tides_core(evidence, tmp_path):
    evidence["segments"][0]["direction_id"] = "UNKNOWN"
    evidence["assignments"][0]["trip_id_scheduled"] = None
    tables, diagnostics = build(evidence)
    assert tables["trips_performed"][0]["direction_id"] is None
    assert diagnostics["segment_envelopes"][0]["raw_direction_id"] == "UNKNOWN"
    assert evidence["segments"][0]["direction_id"] == "UNKNOWN"
    result = tides.publish_tides(tmp_path / "unknown", "global", **evidence)
    assert result["validation"]["valid"]


def test_toml_device_export_policy_does_not_create_dangling_foreign_keys(
    evidence, tmp_path
):
    evidence["config"] = {
        "export": {"include_devices": False},
        "normalization": {"timezone": "America/Vancouver"},
    }
    before = copy.deepcopy(evidence)
    tables, diagnostics = build(evidence)
    assert tables["devices"] == []
    assert all(row["device_id"] is None for row in tables["vehicle_locations"])
    lineage = next(row for row in diagnostics["lineage"] if row.get("rid") == "avl-a")
    assert lineage["device_id"] == "gps-a"
    assert diagnostics["source_timezone"] == "America/Vancouver"
    assert (
        diagnostics["omission_counts"][
            "DEVICES_EXPORT_DISABLED_SOURCE_ID_RETAINED_IN_LINEAGE"
        ]
        == 1
    )
    result = tides.publish_tides(tmp_path / "no-devices", "global", **evidence)
    assert result["validation"]["valid"]
    assert not (tmp_path / "no-devices/devices.csv").exists()
    assert evidence == before


@pytest.mark.parametrize("value", [0, 1, "false", None])
def test_device_export_flag_requires_boolean(evidence, value):
    evidence["config"] = {"export": {"include_devices": value}}
    with pytest.raises(ValueError, match="must be boolean"):
        build(evidence)


def test_completed_mode_resume_is_read_only_and_checks_exact_hashes(evidence, tmp_path):
    folder = tmp_path / "global"
    result = tides.publish_tides(folder, "global", **evidence)
    before = {
        str(path.relative_to(folder)): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }
    assert tides.read_completed_tides(folder, "global") == result
    assert before == {
        str(path.relative_to(folder)): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }
    with pytest.raises(ValueError, match="contract differs"):
        tides.read_completed_tides(folder, "early")
    path = folder / "vehicle_locations.csv"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed"):
        tides.read_completed_tides(folder, "global")


@pytest.mark.parametrize("change", ["missing_marker", "extra_file", "symlink"])
def test_incomplete_or_changed_mode_cannot_resume(evidence, tmp_path, change):
    folder = tmp_path / "global"
    tides.publish_tides(folder, "global", **evidence)
    if change == "missing_marker":
        (folder / "completion.json").rename(folder / "unfinished.json")
    elif change == "extra_file":
        (folder / "extra.json").write_text("{}")
    else:
        (folder / "alias.csv").symlink_to(folder / "vehicles.csv")
    with pytest.raises(ValueError):
        tides.read_completed_tides(folder, "global")


def test_bound_mode_resume_requires_same_run_without_rewriting(evidence, tmp_path):
    folder = tmp_path / "global"
    binding = "abc01234" * 8
    result = tides.publish_tides(folder, "global", run_binding=binding, **evidence)
    before = {
        str(path.relative_to(folder)): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }
    assert json.loads(before["completion.json"])["run_binding"] == binding
    assert (
        tides.read_completed_tides(folder, "global", expected_binding=binding) == result
    )
    assert before == {
        str(path.relative_to(folder)): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("source_binding", [None, "a" * 64])
def test_valid_copied_package_cannot_resume_another_run(
    evidence, tmp_path, source_binding
):
    original = tmp_path / "old-run/global"
    target = tmp_path / "new-run/global"
    tides.publish_tides(original, "global", run_binding=source_binding, **evidence)
    shutil.copytree(original, target)
    before = (target / "completion.json").read_bytes()
    with pytest.raises(ValueError, match="binding differs"):
        tides.read_completed_tides(target, "global", expected_binding="b" * 64)
    assert (target / "completion.json").read_bytes() == before
    assert tides.read_completed_tides(target, "global")["validation"]["valid"]


@pytest.mark.parametrize("binding", [True, 1, {}, "", "a" * 63, "A" * 64])
def test_binding_rejects_noncanonical_digest_before_writing(
    evidence, tmp_path, binding
):
    folder = tmp_path / "global"
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        tides.publish_tides(folder, "global", run_binding=binding, **evidence)
    assert not folder.exists()
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        tides.read_completed_tides(folder, "global", expected_binding=binding)
