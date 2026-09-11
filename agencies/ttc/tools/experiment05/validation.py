"""Pipeline invariants are independent of score thresholds and case winners."""

from __future__ import annotations

import collections
import csv
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path

from .io import read_json, read_rows, sha256


def validate_proxy_event_mode(package_dir, mode, visits, source_by_rid, assignments):
    """Independently reconstruct the declared proxy policy from upstream evidence.

    This deliberately does not call the exporter or trust its validation report.
    Results are aggregated per mode, never one JSON check per passenger event.
    """
    policy = "apc_stop_interval_departure_proxy_v1"
    target = Path(package_dir)
    expected, provenance = {}, {}
    source_errors = []
    assigned = {
        (row["service_date"], row["trip_id_performed"]): row["trip_id_scheduled"]
        for row in assignments
        if row["match_mode"] == mode
    }

    def finite(value):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )

    def stamp(value):
        return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")

    for visit in visits:
        rids = list(visit.get("source_rids", []))
        sources = [source_by_rid.get(rid) for rid in rids]
        if any(source is None for source in sources):
            source_errors.append(visit["stop_visit_id"])
        apc_ids = [
            rid for rid in rids if source_by_rid.get(rid, {}).get("source") == "apc"
        ]
        arrival, departure = visit.get("arrival_t"), visit.get("departure_t")
        if (
            not apc_ids
            or not finite(arrival)
            or not finite(departure)
            or departure < arrival
        ):
            continue
        key = visit["service_date"], visit["trip_id_performed"]
        if key not in assigned or any(
            source is not None
            and (
                source.get("vehicle_id") != visit["vehicle_id"]
                or source.get("trip_id_performed") not in (None, key[1])
                or source.get("service_date") not in (None, key[0])
            )
            for source in sources
        ):
            source_errors.append(visit["stop_visit_id"])
        specifications = [
            ("Vehicle arrived at stop", 1, arrival, "APC_RECORDED_ARRIVAL", False),
            ("Vehicle departed stop", 1, departure, "APC_RECORDED_DEPARTURE", False),
        ]
        for kind, field in (
            ("Passenger boarded", "boarding"),
            ("Passenger alighted", "alighting"),
        ):
            count = visit.get(field)
            if finite(count) and count > 0 and int(count) == count:
                specifications.append(
                    (
                        kind,
                        int(count),
                        departure,
                        "APC_DEPARTURE_PROXY_FOR_VISIT_TOTAL",
                        True,
                    )
                )
        for kind, count, timestamp, basis, proxy in specifications:
            event_id = (
                "e05-"
                + hashlib.sha256(
                    json.dumps(
                        [key, visit["trip_stop_sequence"], kind], separators=(",", ":")
                    ).encode()
                ).hexdigest()[:32]
            )
            expected[event_id] = {
                "event_type": kind,
                "event_count": str(count),
                "event_timestamp": stamp(timestamp),
                "service_date": key[0],
                "trip_id_performed": key[1],
                "trip_stop_sequence": str(visit["trip_stop_sequence"]),
                "vehicle_id": visit["vehicle_id"],
                "stop_id": visit.get("stop_id") or "",
                "trip_id_scheduled": assigned.get(key) or "",
                "scheduled_stop_sequence": str(visit["scheduled_stop_sequence"])
                if visit.get("scheduled_stop_sequence") is not None
                else "",
                "location_ping_id": "",
                "device_id": "",
                "train_car_id": "",
                "pattern_id": "",
            }
            provenance[event_id] = {
                "passenger_event_id": event_id,
                "stop_visit_id": visit["stop_visit_id"],
                "service_date": key[0],
                "trip_id_performed": key[1],
                "trip_stop_sequence": visit["trip_stop_sequence"],
                "event_type": kind,
                "event_count": count,
                "event_timestamp": stamp(timestamp),
                "source_rids": rids,
                "apc_source_rids": apc_ids,
                "original_interval": {
                    "arrival_t": arrival,
                    "departure_t": departure,
                    "arrival_time": stamp(arrival),
                    "departure_time": stamp(departure),
                },
                "timestamp_basis": basis,
                "timestamp_is_proxy": proxy,
                "semantics_assumed": True,
                "measured_individual_passenger_time": False,
                "assumption_policy": policy,
                "source_grain_uncertain": visit.get("grain_uncertain"),
                "count_basis": "APC_VISIT_TOTAL"
                if proxy
                else "ONE_SOURCE_RECORDED_VEHICLE_EVENT",
            }
    rows = []
    try:
        if (target / "passenger_events.csv").exists():
            with (target / "passenger_events.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                rows = list(csv.DictReader(stream))
        diagnostics = read_json(target / "diagnostics.json")
    except (OSError, ValueError) as exc:
        return [
            {
                "name": f"tides_{mode}_proxy_event_files_readable",
                "passed": False,
                "details": str(exc),
            }
        ]
    actual = {row.get("passenger_event_id"): row for row in rows}
    entries = diagnostics.get("passenger_event_lineage")
    entries_valid = isinstance(entries, list) and all(
        isinstance(row, dict) for row in entries
    )
    audit = (
        {row.get("passenger_event_id"): row for row in entries} if entries_valid else {}
    )

    def mismatches(fields):
        return [
            identity
            for identity in expected
            if identity in actual
            and any(
                actual[identity].get(field) != expected[identity][field]
                for field in fields
            )
        ]

    time_count_errors = mismatches(("event_type", "event_count", "event_timestamp"))
    link_errors = mismatches(
        set(next(iter(expected.values()), {}))
        - {
            "event_type",
            "event_count",
            "event_timestamp",
        }
    )
    provenance_errors = [
        identity
        for identity, wanted in provenance.items()
        if identity not in audit
        or json.dumps({key: audit[identity].get(key) for key in wanted}, sort_keys=True)
        != json.dumps(wanted, sort_keys=True)
    ]
    tests = [
        (
            "identity_completeness",
            len(rows) == len(actual) == len(expected) and set(actual) == set(expected),
            {"expected": len(expected), "actual": len(rows), "unique_ids": len(actual)},
        ),
        ("times_and_counts", not time_count_errors, time_count_errors[:10]),
        ("trip_assignment_and_nulls", not link_errors, link_errors[:10]),
        ("source_lineage", not source_errors, source_errors[:10]),
        (
            "provenance",
            entries_valid
            and len(entries) == len(audit) == len(expected)
            and set(audit) == set(expected)
            and not provenance_errors,
            provenance_errors[:10],
        ),
        (
            "policy_declaration",
            diagnostics.get("export_policy", {}).get("passenger_event_policy") == policy
            and diagnostics.get("match_mode") == mode,
            None,
        ),
    ]
    return [
        {
            "name": f"tides_{mode}_proxy_event_{name}",
            "passed": bool(passed),
            "details": details,
        }
        for name, passed, details in tests
    ]


def validate_pipeline(run_dir, manifest, config):
    folder = Path(run_dir)
    errors, checks = [], []

    def check(name, valid, details=None):
        record = {"name": name, "passed": bool(valid)}
        if details is not None:
            record["details"] = details
        checks.append(record)
        if not valid:
            errors.append(record)

    raw = read_rows(folder / "stages/raw_observations.parquet")
    observations = read_rows(folder / "fused_projected_observations.parquet")
    original = {row["rid"]: row for row in raw}
    final = {row["rid"]: row for row in observations}
    check(
        "source_row_identity_conservation",
        len(raw) == len(original) == len(observations) == len(final)
        and set(original) == set(final),
    )
    check(
        "original_payloads_not_mutated",
        all(row["raw"] == final[rid]["raw_payload"] for rid, row in original.items()),
    )
    check(
        "source_file_hash_and_row_identity",
        all(
            row["rid"]
            == f"{row['source']}:{row['source_file_sha256']}:{row['source_row']}"
            and row["source_file_sha256"] == manifest["input_hashes"][row["source"]]
            for row in observations
        ),
    )
    for source, path in manifest["input_paths"].items():
        check(
            f"raw_{source}_unchanged", sha256(path) == manifest["input_hashes"][source]
        )
    check(
        "gtfs_archive_unchanged",
        sha256(manifest["gtfs_feed"]["path"]) == manifest["gtfs_feed_hash"],
    )
    bundle = read_json(folder / "gtfs/bundle.json")
    check(
        "feed_manifest_unchanged",
        sha256(bundle["manifest_path"]) == manifest["feed_manifest_hash"],
    )
    segments = read_rows(folder / "performed_trip_segments.parquet")
    by_trip = {row["trip_id_performed"]: row for row in segments}
    check("performed_id_unique", len(by_trip) == len(segments))
    check(
        "membership_foreign_keys",
        all(
            row.get("trip_id_performed") is None or row["trip_id_performed"] in by_trip
            for row in observations
        ),
    )
    check(
        "effective_extent_inside_raw_extent",
        all(
            row["raw_segment_start"]
            <= row["effective_segment_start"]
            <= row["effective_segment_end"]
            <= row["raw_segment_end"]
            for row in segments
        ),
    )
    check(
        "right_censor_audit_fields_present",
        all(
            all(
                key in row
                for key in (
                    "raw_segment_start",
                    "raw_segment_end",
                    "effective_segment_start",
                    "effective_segment_end",
                    "plateau_onset_time",
                    "censor_decision_time",
                    "censored_tail_duration_s",
                    "censor_reason",
                )
            )
            for row in segments
        ),
    )
    check(
        "censored_tail_not_matching_evidence",
        all(
            not row.get("trajectory_included") and row.get("exclude_from_matching")
            for row in observations
            if row.get("censored_tail")
        ),
    )
    check(
        "projection_gate",
        all(
            row.get("projection_distance_m") is not None
            and row["projection_distance_m"]
            <= config["matcher"]["max_projection_distance_m"]
            for row in observations
            if row["source"] == "avl" and row.get("trajectory_included")
        ),
    )
    check(
        "apc_not_fabricated_trajectory",
        all(
            not row.get("trajectory_included")
            for row in observations
            if row["source"] == "apc"
        ),
    )
    ranks = read_rows(folder / "candidate_rankings.parquet")
    cohorts = collections.defaultdict(list)
    for row in ranks:
        cohorts[row["trip_id_performed"], row["match_mode"]].append(row)
    check(
        "rank_unique_consecutive",
        all(
            sorted(row["candidate_rank"] for row in rows)
            == list(range(1, len(rows) + 1))
            for rows in cohorts.values()
        ),
    )
    check(
        "rank_respects_04c_ordering_tuple",
        all(
            [
                (row["score"], row["candidate_id"])
                for row in sorted(rows, key=lambda item: item["candidate_rank"])
            ]
            == sorted((row["score"], row["candidate_id"]) for row in rows)
            for rows in cohorts.values()
        ),
    )
    check(
        "finite_candidate_scores",
        all(
            isinstance(row["score"], (int, float)) and math.isfinite(row["score"])
            for row in ranks
        ),
    )
    assignments = read_rows(folder / "assignments.parquet")
    keys = {(row["trip_id_performed"], row["match_mode"]) for row in assignments}
    expected = {
        (identity, mode) for identity in by_trip for mode in manifest["match_modes"]
    }
    check(
        "one_assignment_row_per_trip_and_mode",
        len(keys) == len(assignments) and keys == expected,
    )
    candidate_by_key = {
        (row["trip_id_performed"], row["match_mode"], row["candidate_rank"]): row
        for row in ranks
    }
    assignment_groups = collections.defaultdict(list)
    for row in assignments:
        if row["trip_id_scheduled"] is None:
            check(
                "unmatched_has_true_null_and_reason",
                bool(row.get("unmatched_reason"))
                and bool(row.get("unmatched_reasons")),
                [row["trip_id_performed"], row["match_mode"]],
            )
            continue
        candidate = candidate_by_key.get(
            (row["trip_id_performed"], row["match_mode"], row["candidate_rank"])
        )
        check(
            "assignment_from_fixed_legal_top3",
            row["candidate_rank"] in (1, 2, 3)
            and candidate is not None
            and candidate["hard_constraints_valid"]
            and candidate["candidate_id"] == row["candidate_id"]
            and candidate["trip_id_scheduled"] == row["trip_id_scheduled"]
            and candidate["score"] == row["score"],
            [row["trip_id_performed"], row["match_mode"]],
        )
        group = tuple(
            row[field]
            for field in (
                "service_date",
                "route_id",
                "direction_id",
                "match_mode",
                "gtfs_feed_version",
            )
        )
        assignment_groups[group].append(row)
    for group, rows in assignment_groups.items():
        ordered = sorted(
            rows,
            key=lambda row: (
                by_trip[row["trip_id_performed"]]["actual_start_t"],
                row["trip_id_performed"],
            ),
        )
        check(
            "scheduled_unique_in_group",
            len({row["trip_id_scheduled"] for row in rows}) == len(rows),
            list(group),
        )
        check(
            "globally_monotone_including_same_round",
            all(
                a["scheduled_order"] < b["scheduled_order"]
                for a, b in zip(ordered, ordered[1:], strict=False)
            ),
            list(group),
        )
    proxy_visits = (
        read_rows(folder / "stop_visits_evidence.parquet")
        if config.get("export", {}).get("passenger_event_policy")
        == "apc_stop_interval_departure_proxy_v1"
        else None
    )
    for mode in manifest["match_modes"]:
        report = read_json(folder / "tides" / mode / "validation_report.json")
        check(
            f"tides_{mode}_valid",
            report.get("valid")
            and report.get("profile_valid")
            and report.get("serialized_resources_valid"),
        )
        with (folder / "tides" / mode / "trips_performed.csv").open(
            newline=""
        ) as stream:
            exported = list(csv.DictReader(stream))
        expected_assignments = {
            (row["service_date"], row["trip_id_performed"]): row["trip_id_scheduled"]
            for row in assignments
            if row["match_mode"] == mode
        }
        actual_assignments = {
            (row["service_date"], row["trip_id_performed"]): row["trip_id_scheduled"]
            or None
            for row in exported
        }
        check(
            f"tides_{mode}_assignments_equal_current_run",
            len(exported) == len(expected_assignments)
            and actual_assignments == expected_assignments,
        )
        if proxy_visits is not None:
            for result in validate_proxy_event_mode(
                folder / "tides" / mode, mode, proxy_visits, final, assignments
            ):
                check(result["name"], result["passed"], result.get("details"))
    # Every completed upstream stage is still pinned before publication.
    for stage, result in manifest["stages"].items():
        if result.get("status") == "complete":
            check(
                f"stage_{stage}_outputs_unchanged",
                all(
                    sha256(folder / name) == expected
                    for name, expected in result["output_sha256"].items()
                ),
            )
    return {
        "schema": "experiment05-invariants-v1",
        "valid": not errors,
        "checks": checks,
        "errors": errors,
        "independent_accuracy_evaluation": False,
        "research_only": True,
    }
