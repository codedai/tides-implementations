"""Synthetic tests for the explicitly assumed APC departure-proxy export policy."""

import copy
import csv
import json
from datetime import UTC, datetime

import pytest

from tests.test_experiment05_tides import evidence as base_evidence
from tools.experiment05 import tides

POLICY = "apc_stop_interval_departure_proxy_v1"
CONSERVATIVE = "omit_without_event_semantics"


@pytest.fixture
def evidence():
    value = base_evidence.__wrapped__()
    value["config"] = {"export": {"passenger_event_policy": POLICY}}
    value["stop_visits"][0].update(
        alighting=2, grain_uncertain=True, passenger_event_semantics_valid=False
    )
    return value


def _build(evidence, mode="global"):
    return tides.build_tides_rows(mode, **evidence)


def _stamp(timestamp):
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def test_new_policy_generates_four_events_with_explicit_proxy_lineage(evidence):
    before = copy.deepcopy(evidence)
    tables, audit = _build(evidence)
    rows = {row["event_type"]: row for row in tables["passenger_events"]}
    assert set(rows) == {
        "Vehicle arrived at stop",
        "Vehicle departed stop",
        "Passenger boarded",
        "Passenger alighted",
    }
    visit = evidence["stop_visits"][0]
    assert rows["Vehicle arrived at stop"]["event_timestamp"] == _stamp(
        visit["arrival_t"]
    )
    for kind in ("Vehicle departed stop", "Passenger boarded", "Passenger alighted"):
        assert rows[kind]["event_timestamp"] == _stamp(visit["departure_t"])
    assert {kind: row["event_count"] for kind, row in rows.items()} == {
        "Vehicle arrived at stop": 1,
        "Vehicle departed stop": 1,
        "Passenger boarded": 3,
        "Passenger alighted": 2,
    }
    assert len(audit["passenger_event_lineage"]) == 4
    for row in audit["passenger_event_lineage"]:
        event = rows[row["event_type"]]
        assert row["passenger_event_id"] == event["passenger_event_id"]
        assert row["source_rids"] == row["apc_source_rids"] == ["apc-a"]
        assert row["original_interval"]["arrival_t"] == visit["arrival_t"]
        assert row["original_interval"]["departure_t"] == visit["departure_t"]
        assert row["semantics_assumed"] is True
        assert row["measured_individual_passenger_time"] is False
        assert row["assumption_policy"] == POLICY
        assert row["source_grain_uncertain"] is True
        assert row["timestamp_is_proxy"] == row["event_type"].startswith("Passenger")
        if row["timestamp_is_proxy"]:
            assert row["timestamp_basis"] == "APC_DEPARTURE_PROXY_FOR_VISIT_TOTAL"
            assert row["count_basis"] == "APC_VISIT_TOTAL"
    assert tides.validate_tides_rows(tables, evidence["gtfs_bundle"])["valid"]
    assert evidence == before


def test_other_core_tables_and_raw_interval_are_unchanged(evidence):
    visit = evidence["stop_visits"][0]
    visit["departure_t"] = evidence["segments"][0]["effective_segment_end"] + 10
    visit["decision_interval_clip"] = {"reason": "matching-only"}
    visit["interval_end"] = evidence["segments"][0]["effective_segment_end"]
    before = copy.deepcopy(evidence)
    proxy, _ = _build(evidence)
    conservative = copy.deepcopy(evidence)
    conservative["config"]["export"]["passenger_event_policy"] = CONSERVATIVE
    old, _ = _build(conservative)
    for name in tides.TABLES:
        if name != "passenger_events":
            assert proxy[name] == old[name]
    for row in proxy["passenger_events"]:
        if row["event_type"] != "Vehicle arrived at stop":
            assert row["event_timestamp"] == _stamp(visit["departure_t"])
    assert evidence == before


@pytest.mark.parametrize("source_rids", [[], ["avl-a"]])
def test_proxy_requires_resolving_apc_lineage(evidence, source_rids):
    evidence["stop_visits"][0]["source_rids"] = source_rids
    tables, audit = _build(evidence)
    assert not tables["passenger_events"]
    assert audit["omission_counts"]["APC_SOURCE_LINEAGE_REQUIRED_FOR_PROXY_POLICY"] == 4
    assert not audit["passenger_event_lineage"]


@pytest.mark.parametrize("missing", ["arrival_t", "departure_t"])
def test_proxy_requires_complete_raw_interval(evidence, missing):
    evidence["stop_visits"][0][missing] = None
    tables, audit = _build(evidence)
    assert not tables["passenger_events"]
    assert (
        audit["omission_counts"]["COMPLETE_APC_STOP_INTERVAL_REQUIRED_FOR_PROXY_POLICY"]
        == 4
    )


def test_inverted_interval_is_not_repaired(evidence):
    visit = evidence["stop_visits"][0]
    visit["departure_t"] = visit["arrival_t"] - 1
    with pytest.raises(ValueError, match="Inverted"):
        _build(evidence)


def test_zero_duration_still_has_distinct_event_identities(evidence):
    visit = evidence["stop_visits"][0]
    visit["departure_t"] = visit["arrival_t"]
    tables, _ = _build(evidence)
    rows = tables["passenger_events"]
    assert len(rows) == len({row["passenger_event_id"] for row in rows}) == 4
    assert len({row["event_timestamp"] for row in rows}) == 1
    assert tables["stop_visits"][0]["dwell"] == 0


@pytest.mark.parametrize("count", [2.5, -1, True, float("nan"), float("inf"), "3"])
def test_invalid_count_does_not_remove_valid_arrival_departure(evidence, count):
    evidence["stop_visits"][0]["boarding"] = count
    tables, audit = _build(evidence)
    assert {row["event_type"] for row in tables["passenger_events"]} == {
        "Vehicle arrived at stop",
        "Vehicle departed stop",
        "Passenger alighted",
    }
    assert audit["omission_counts"]["COUNT_SEMANTICS_INVALID_NO_ROUNDING"] == 1


def test_zero_and_unknown_counts_are_distinct(evidence):
    evidence["stop_visits"][0].update(boarding=0, alighting=None)
    tables, audit = _build(evidence)
    assert len(tables["passenger_events"]) == 2
    assert audit["omission_counts"] == {"COUNT_UNAVAILABLE": 1}


def test_conservative_policy_keeps_explicit_event_time_contract(evidence):
    visit = evidence["stop_visits"][0]
    evidence["config"]["export"]["passenger_event_policy"] = CONSERVATIVE
    tables, _ = _build(evidence)
    assert not tables["passenger_events"]
    visit.update(
        passenger_event_semantics_valid=True, boarding_event_t=visit["arrival_t"] + 1
    )
    tables, audit = _build(evidence)
    assert len(tables["passenger_events"]) == 1
    assert tables["passenger_events"][0]["event_timestamp"] == _stamp(
        visit["arrival_t"] + 1
    )
    lineage = audit["passenger_event_lineage"][0]
    assert lineage["timestamp_basis"] == "EXPLICIT_CONFIRMED_EVENT_TIMESTAMP"
    assert lineage["assumption_policy"] is None and not lineage["semantics_assumed"]
    visit["boarding_event_t"] = visit["departure_t"] + 1
    with pytest.raises(ValueError, match="outside"):
        _build(evidence)


@pytest.mark.parametrize("mode", ["global", "early", "middle", "late"])
def test_four_modes_publish_valid_core_only_rows_and_true_nulls(
    evidence, tmp_path, mode
):
    evidence["assignments"][0].update(match_mode=mode, trip_id_scheduled=None)
    target = tmp_path / mode
    result = tides.publish_tides(target, mode, **evidence)
    assert result["validation"]["serialized_resources_valid"]
    assert result["summary"]["rows"]["passenger_events"] == 4
    with (target / "passenger_events.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    core = {field["name"] for field in tides._schema("passenger_events")["fields"]}
    for row in rows:
        assert set(row) == core
        assert (
            row["trip_id_scheduled"]
            == row["location_ping_id"]
            == row["device_id"]
            == ""
        )
        assert row["trip_id_performed"] == "performed-a"
        assert row["trip_stop_sequence"] == "1"
    audit = json.loads((target / "diagnostics.json").read_text())
    assert audit["export_policy"]["passenger_event_policy"] == POLICY
    assert len(audit["passenger_event_lineage"]) == 4


def test_repeated_stop_visits_are_not_collapsed(evidence):
    source = copy.deepcopy(evidence["observations"][-1])
    source.update(rid="apc-b", source_row=18)
    evidence["observations"].append(source)
    visit = copy.deepcopy(evidence["stop_visits"][0])
    visit.update(stop_visit_id="visit-b", trip_stop_sequence=2, source_rids=["apc-b"])
    evidence["stop_visits"].append(visit)
    tables, audit = _build(evidence)
    assert len(tables["passenger_events"]) == 8
    assert len({row["passenger_event_id"] for row in tables["passenger_events"]}) == 8
    assert {tuple(row["source_rids"]) for row in audit["passenger_event_lineage"]} == {
        ("apc-a",),
        ("apc-b",),
    }


@pytest.mark.parametrize("policy", [None, True, "typo", {}, []])
def test_unknown_policy_fails_closed(evidence, policy):
    evidence["config"]["export"]["passenger_event_policy"] = policy
    with pytest.raises(ValueError, match="passenger_event_policy"):
        _build(evidence)
