"""Pipeline invariants are independent of score thresholds and case winners."""

from __future__ import annotations

import collections
import csv
import math
from pathlib import Path

from .io import read_json, read_rows, sha256


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
