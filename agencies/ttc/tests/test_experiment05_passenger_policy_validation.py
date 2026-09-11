"""Independent event-policy acceptance, including corrupt but schema-valid CSVs."""

import csv
import json

import pytest

from tests.test_experiment05_tides import evidence as base_evidence
from tools.experiment05 import tides
from tools.experiment05.validation import validate_proxy_event_mode


@pytest.fixture
def published(tmp_path):
    data = base_evidence.__wrapped__()
    data["config"] = {
        "export": {"passenger_event_policy": "apc_stop_interval_departure_proxy_v1"}
    }
    data["stop_visits"][0]["alighting"] = 2
    data["assignments"][0]["service_date"] = data["stop_visits"][0]["service_date"]
    target = tmp_path / "global"
    tides.publish_tides(target, "global", **data)
    return target, data


def inspect(published):
    target, data = published
    return validate_proxy_event_mode(
        target,
        "global",
        data["stop_visits"],
        {row["rid"]: row for row in data["observations"]},
        data["assignments"],
    )


def test_independent_check_passes_without_invoking_exporter(published, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Validator must not regenerate expectations with exporter")

    monkeypatch.setattr(tides, "build_tides_rows", forbidden)
    checks = inspect(published)
    assert len(checks) == 6 and all(row["passed"] for row in checks)


@pytest.mark.parametrize(
    "change",
    [
        "drop",
        "duplicate",
        "timestamp",
        "count",
        "null_assignment",
        "wrong_assignment",
        "fake_ping",
        "changed_event_id",
        "wrong_stop",
        "wrong_sequence",
    ],
)
def test_changed_csv_detected_independently_of_saved_schema_report(published, change):
    target, _ = published
    path = target / "passenger_events.csv"
    with path.open() as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    row = next(row for row in rows if row["event_type"] == "Passenger boarded")
    if change == "drop":
        rows.remove(row)
    elif change == "duplicate":
        rows.append(dict(row))
    else:
        field, value = {
            "timestamp": ("event_timestamp", "2023-11-06T06:00:10Z"),
            "count": ("event_count", "9"),
            "null_assignment": ("trip_id_scheduled", ""),
            "wrong_assignment": ("trip_id_scheduled", "another-trip"),
            "fake_ping": ("location_ping_id", "avl-a"),
            "changed_event_id": ("passenger_event_id", "other-id"),
            "wrong_stop": ("stop_id", "stop-b"),
            "wrong_sequence": ("trip_stop_sequence", "2"),
        }[change]
        row[field] = value
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    assert json.loads((target / "validation_report.json").read_text())["valid"]
    assert not all(row["passed"] for row in inspect(published))


@pytest.mark.parametrize(
    "change",
    [
        "missing_lineage",
        "duplicate_lineage",
        "source",
        "original_interval",
        "basis",
        "proxy",
        "assumed",
        "measured",
        "policy",
        "wrong_mode",
    ],
)
def test_missing_or_false_proxy_provenance_is_rejected(published, change):
    target, _ = published
    path = target / "diagnostics.json"
    diagnostics = json.loads(path.read_text())
    record = next(
        row
        for row in diagnostics["passenger_event_lineage"]
        if row["event_type"] == "Passenger boarded"
    )
    if change == "missing_lineage":
        del diagnostics["passenger_event_lineage"]
    elif change == "duplicate_lineage":
        diagnostics["passenger_event_lineage"].append(dict(record))
    elif change == "source":
        record["source_rids"] = ["avl-a"]
    elif change == "original_interval":
        record["original_interval"]["departure_t"] += 1
    elif change == "policy":
        diagnostics["export_policy"]["passenger_event_policy"] = (
            "omit_without_event_semantics"
        )
    elif change == "wrong_mode":
        diagnostics["match_mode"] = "early"
    else:
        field, value = {
            "basis": ("timestamp_basis", "MEASURED"),
            "proxy": ("timestamp_is_proxy", False),
            "assumed": ("semantics_assumed", False),
            "measured": ("measured_individual_passenger_time", True),
        }[change]
        record[field] = value
    path.write_text(json.dumps(diagnostics))
    assert not all(row["passed"] for row in inspect(published))


def test_source_apc_relationship_must_still_resolve(published):
    _, data = published
    data["observations"] = [
        row for row in data["observations"] if row["source"] != "apc"
    ]
    assert not all(row["passed"] for row in inspect(published))


@pytest.mark.parametrize("count", [0, None, 1.5, -1])
def test_missing_zero_invalid_counts_have_no_expected_count_event(tmp_path, count):
    data = base_evidence.__wrapped__()
    data["config"] = {
        "export": {"passenger_event_policy": "apc_stop_interval_departure_proxy_v1"}
    }
    data["stop_visits"][0].update(boarding=count, alighting=0)
    data["assignments"][0].update(
        service_date=data["stop_visits"][0]["service_date"], trip_id_scheduled=None
    )
    target = tmp_path / "global"
    result = tides.publish_tides(target, "global", **data)
    assert result["summary"]["rows"]["passenger_events"] == 2
    assert all(row["passed"] for row in inspect((target, data)))
