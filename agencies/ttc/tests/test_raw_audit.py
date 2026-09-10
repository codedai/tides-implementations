"""Synthetic edge cases for the fresh raw audit; never reads private raw data."""

from datetime import datetime

import polars as pl

from experiments.apc_avl_coverage_study.raw_audit import (
    add_dst_flags,
    audit_apc,
    coverage_audit,
    normalize_raw,
    source_common,
    wall_time_status,
)


def apc_raw(**updates: list[str]) -> pl.DataFrame:
    count = len(next(iter(updates.values()))) if updates else 1
    defaults = {
        "": "0",
        "DAYTYPE": "Mo",
        "Route": "SYNTHETIC_R",
        "RouteName": "Synthetic",
        "Branch": "TEST",
        "VehicleID": "TEST_V",
        "RouteDirection": "NORTH",
        "StopID": "TEST_STOP",
        "ONSTREET": "A",
        "ATSTREET": "B",
        "Lat": "43.7",
        "Lon": "-79.4",
        "PeriodID": "Afternoon",
        "StopArrivalTime": "2023-11-06 13:15:00",
        "StopDepartureTime": "2023-11-06 13:15:00",
        "NoTrips": "1",
        "AvgDwell": "0",
        "AvgOTP": "0",
        "AvgSample": "1",
        "Arrload": "0",
        "Boarding": "1",
        "Alighting": "0",
        "Depload": "1",
    }
    values = {key: [value] * count for key, value in defaults.items()}
    values.update(updates)
    return pl.DataFrame(values)


def test_dst_is_classified_without_attaching_timezone_or_choosing_fold():
    times = [
        datetime(2023, 11, 5, 1, 30),
        datetime(2023, 3, 12, 2, 30),
        datetime(2023, 11, 6, 1, 30),
    ]
    assert [wall_time_status(value) for value in times] == [
        "ambiguous",
        "nonexistent",
        "unique",
    ]
    result = add_dst_flags(pl.DataFrame({"time": times}), ["time"])
    assert result["time_ambiguous"].to_list() == [True, False, False]
    assert result["time_nonexistent"].to_list() == [False, True, False]
    assert result["time"].dtype == pl.Datetime("us")
    assert result["time"].to_list() == times


def test_canonical_conserves_duplicates_invalid_rows_and_fractional_counts():
    raw = apc_raw(
        Boarding=["1.5", "1.5", "oops"],
        AvgSample=["2", "2", "1"],
        VehicleID=["TEST_V", "TEST_V", ""],
        **{"": ["0", "1", "2"]},
    )
    result = normalize_raw(raw, "apc", "synthetic_digest")
    assert result.height == 3
    assert result["rid"].n_unique() == 3
    assert result["boarding"].to_list() == [1.5, 1.5, None]
    assert result["invalid"].to_list() == [False, False, True]
    summary = source_common(raw, result, "apc")
    assert summary["exact_payload_duplicate_rows_including_all_members"] == 2
    assert summary["exact_payload_duplicate_excess_rows"] == 1
    assert summary["numeric_parse_failure_by_column"]["Boarding"] == 1


def test_nested_overlap_is_not_lost_when_immediate_predecessor_ends_early():
    raw = apc_raw(
        StopID=["A", "B", "C"],
        StopArrivalTime=[
            "2023-11-06 13:00:00",
            "2023-11-06 13:05:00",
            "2023-11-06 13:10:00",
        ],
        StopDepartureTime=[
            "2023-11-06 13:20:00",
            "2023-11-06 13:06:00",
            "2023-11-06 13:11:00",
        ],
    )
    audit, _ = audit_apc(normalize_raw(raw, "apc", "synthetic_digest"))
    assert audit["overlap_with_any_earlier_interval"]["count"] == 2
    assert audit["overlap_with_immediate_predecessor"]["count"] == 1


def test_payload_duplicates_and_visit_key_conflicts_have_separate_counts():
    raw = apc_raw(Boarding=["1", "1", "2"], **{"": ["0", "1", "2"]})
    canonical = normalize_raw(raw, "apc", "synthetic_digest")
    common = source_common(raw, canonical, "apc")
    audit, _ = audit_apc(canonical)
    assert common["exact_payload_duplicate_rows_including_all_members"] == 2
    assert audit["visit_key_conflicting_payload_groups"] == 1


def test_asymmetric_source_spans_remain_observation_envelopes():
    avl = pl.DataFrame(
        {
            "vehicle": ["TEST_V", "TEST_V"],
            "time": [datetime(2023, 11, 6, 13), datetime(2023, 11, 6, 14)],
            "invalid": [False, False],
            "time_ambiguous": [False, False],
        }
    )
    apc = normalize_raw(
        apc_raw(
            StopArrivalTime=["2023-11-06 13:15:00", "2023-11-06 14:13:00"],
            StopDepartureTime=["2023-11-06 13:15:00", "2023-11-06 14:13:00"],
        ),
        "apc",
        "synthetic_digest",
    )
    summary, days = coverage_audit(avl, apc, pl.DataFrame())
    assert days["apc_start_minus_avl_s"][0] == 900
    assert days["apc_end_minus_avl_s"][0] == 780
    assert summary["tail_thresholds"]["300"]["avl_starts_earlier_apc_ends_later"] == 1
    assert summary["apc_rows_after_same_day_last_avl"] == 1
    assert "trip" not in days.columns
