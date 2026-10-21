"""Read-only persisted experiment audit; never recompute or infer accuracy.

Run with ``python -m tools.merge_review.gtfs_matching.audit COMPLETED_RUN``.
The audit consumes raw persisted window bundles, not browser-adapted aliases.
It is independent of the matching implementation and records its own hash.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import gzip
import hashlib
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from tools.merge_review.retention import resolve_direction_parent

from .case_storage import decode_case_artifact

EPOCH = datetime(1970, 1, 1)
AUDIT_VERSION = "4-joint-gap-parent-partitions"
PROJECTION_STATUSES = {
    "ON_REFERENCE",
    "OFF_REFERENCE",
    "UNPROJECTABLE",
    "STOP_INTERVAL_ONLY",
}
RESULT_STATUSES = {
    "MATCHED",
    "AMBIGUOUS",
    "PATTERN_ONLY",
    "UNMATCHED",
    "BOUNDARY_UNCERTAIN",
}


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


AUDIT_SCRIPT_SHA256 = _sha(__file__)


def _read(path):
    if str(path).endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return decode_case_artifact(json.load(stream))
    with Path(path).open(encoding="utf-8") as stream:
        return decode_case_artifact(json.load(stream))


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _normal(value):
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise ValueError("Audit does not reinterpret timezone-aware source clocks")
        return (value - EPOCH).total_seconds()
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _normal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normal(v) for v in value]
    return value


def _identity(row):
    return f"{row.get('source', 'avl')}|{row.get('source_rid', row.get('rid'))}"


def _trip_key(row):
    return "|".join(
        str(row.get(k, "")) for k in ("feed_version", "service_date", "trip_id")
    )


class _Report:
    def __init__(self, max_examples):
        self.violations = collections.Counter()
        self.warnings = collections.Counter()
        self.examples = []
        self.max_examples = max_examples
        self.examined = collections.Counter()
        self.distributions = collections.defaultdict(collections.Counter)

    def issue(self, code, where, *, warning=False):
        (self.warnings if warning else self.violations)[code] += 1
        if len(self.examples) < self.max_examples:
            self.examples.append(dict(code=code, location=where, warning=warning))

    def output(self):
        return dict(
            validation_ok=not self.violations and not self.warnings,
            checks_complete=not self.warnings,
            violations=dict(self.violations),
            schema_warnings=dict(self.warnings),
            examples=self.examples,
            examined=dict(self.examined),
            distributions={k: dict(v) for k, v in self.distributions.items()},
            accuracy=None,
            accuracy_assessed=False,
            scores_recomputed=False,
            audit_version=AUDIT_VERSION,
            scope=(
                "Persisted source, reference, scoring-evidence and hash invariants only"
            ),
        )


def _required(container, key, kind, report, where):
    value = container.get(key) if isinstance(container, dict) else None
    if not isinstance(value, kind):
        report.issue("unknown_or_missing_schema", f"{where}.{key}", warning=True)
        return kind()
    return value


def _ratios(value, report, where):
    if isinstance(value, list):
        for index, item in enumerate(value):
            _ratios(item, report, f"{where}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            if (
                key.endswith("_ratio") or key in {"coverage", "support_score"}
            ) and item is not None:
                if not _finite(item) or not -1e-9 <= item <= 1 + 1e-9:
                    report.issue("ratio_out_of_bounds", f"{where}.{key}")
            if key in {"probability", "calibrated_probability"} and item is not None:
                report.issue("uncalibrated_probability_not_null", f"{where}.{key}")
            _ratios(item, report, f"{where}.{key}")


def _reference_projection(point, reference_id):
    if point.get("reference_id") == reference_id:
        return point
    matches = [
        a
        for a in point.get("projection_alternatives", [])
        if a.get("reference_id") == reference_id
    ]
    return matches[0] if len(matches) == 1 else None


def _reference_continuity(points, reference, config, report, where):
    """Independently label uninterrupted source runs, without scoring them."""
    thresholds = {}
    for key in (
        "window_min_quality",
        "score_max_gap_s",
        "score_backtrack_tolerance_m",
    ):
        value = config.get(key)
        if not _finite(value) or value < 0:
            report.issue("unknown_continuity_threshold", f"{where}.{key}", warning=True)
            return {}
        thresholds[key] = value
    ordered = sorted(
        (p for p in points if _finite(p.get("t"))),
        key=lambda p: (p["t"], _identity(p)),
    )
    previous, group, position = None, -1, 0
    membership = {}
    for point in ordered:
        hypothesis = _reference_projection(point, reference)
        effective = dict(point, **hypothesis) if hypothesis is not None else {}
        quality = effective.get("quality", 1.0)
        usable = (
            effective.get("projection_status") == "ON_REFERENCE"
            and _finite(effective.get("s_m"))
            and _finite(quality)
            and quality >= thresholds["window_min_quality"]
        )
        if not usable:
            previous = None
            continue
        if (
            previous is None
            or effective.get("scope_separator_before")
            or effective.get("fragment_id") != previous.get("fragment_id")
            or effective["t"] - previous["t"] > thresholds["score_max_gap_s"]
            or effective["s_m"]
            < previous["s_m"] - thresholds["score_backtrack_tolerance_m"]
        ):
            group += 1
            position = 0
        membership[_identity(point)] = (group, position)
        position += 1
        previous = effective
    return membership


def _continuous_ids(ids, membership):
    positions = [membership.get(key) for key in ids]
    return (
        bool(positions)
        and all(p is not None for p in positions)
        and all(
            b[0] == a[0] and b[1] == a[1] + 1
            for a, b in zip(positions, positions[1:], strict=False)
        )
    )


def _audit_delay_fit(whole, delay_by_id, continuity, report, where):
    fits = _required(whole, "delay_fit_fragments", list, report, where)
    represented = collections.Counter()
    fit_ids = set()
    for fit in fits:
        location = f"{where}/fit:{fit.get('id')}"
        if fit.get("id") in fit_ids:
            report.issue("duplicate_delay_fit_fragment", location)
        fit_ids.add(fit.get("id"))
        ids = _required(fit, "source_ids", list, report, location)
        represented.update(ids)
        if not _continuous_ids(ids, continuity):
            report.issue("delay_fit_crosses_source_break", location)
        rows = [delay_by_id.get(key, {}) for key in ids]
        if not rows or any(
            r.get("fragment_id") != fit.get("id")
            or r.get("status") != "SUPPORTED"
            or r.get("delay_fit_status") != fit.get("status")
            for r in rows
        ):
            report.issue("delay_fit_process_membership_changed", location)
        if rows and (
            fit.get("start") != rows[0].get("t") or fit.get("end") != rows[-1].get("t")
        ):
            report.issue("delay_fit_source_timestamp_pair_changed", location)
        knots = _required(fit, "knots", list, report, location)
        numeric = ("t", "raw_delay_s", "raw_corrected_t", "corrected_t", "delay_s")
        if not knots or any(not all(_finite(k.get(f)) for f in numeric) for k in knots):
            report.issue("unknown_delay_fit_knot_schema", location, warning=True)
            continue
        if any(
            b["t"] <= a["t"] or b["corrected_t"] < a["corrected_t"] - 1e-6
            for a, b in zip(knots, knots[1:], strict=False)
        ):
            report.issue("delay_fit_knot_clock_not_monotone", location)
            continue
        for knot in knots:
            if (
                abs(knot["raw_corrected_t"] - (knot["t"] - knot["raw_delay_s"])) > 1e-6
                or abs(knot["corrected_t"] - (knot["t"] - knot["delay_s"])) > 1e-6
                or not _finite(knot.get("constraint_adjustment_s"))
                or abs(
                    knot["constraint_adjustment_s"]
                    - (knot["raw_corrected_t"] - knot["corrected_t"])
                )
                > 1e-6
            ):
                report.issue("delay_fit_knot_timestamp_pair_changed", location)
            if not _finite(knot.get("weight")) or knot["weight"] <= 0:
                report.issue("invalid_delay_fit_knot_weight", location)
        knot_times = [k["t"] for k in knots]
        for row in rows:
            if not _finite(row.get("t")):
                continue
            index = bisect.bisect_right(knot_times, row["t"])
            expected = {}
            for field in ("raw_corrected_t", "corrected_t"):
                if index in {0, len(knots)}:
                    endpoint = knots[0] if index == 0 else knots[-1]
                    expected[field] = endpoint[field] + row["t"] - endpoint["t"]
                else:
                    left, right = knots[index - 1], knots[index]
                    fraction = (row["t"] - left["t"]) / (right["t"] - left["t"])
                    expected[field] = (
                        left[field] * (1 - fraction) + right[field] * fraction
                    )
            if (
                not _finite(row.get("corrected_t"))
                or abs(row["corrected_t"] - expected["corrected_t"]) > 1e-6
                or not _finite(row.get("raw_delay_s"))
                or abs(row["raw_delay_s"] - (row["t"] - expected["raw_corrected_t"]))
                > 1e-6
                or not _finite(row.get("constraint_adjustment_s"))
                or abs(
                    row["constraint_adjustment_s"]
                    - (expected["raw_corrected_t"] - expected["corrected_t"])
                )
                > 1e-6
            ):
                report.issue("delay_process_not_paired_to_fitted_clock", location)
        report.examined["delay_fit_knots"] += len(knots)
    supported = {
        key for key, row in delay_by_id.items() if row.get("status") == "SUPPORTED"
    }
    if represented.keys() != supported or any(n != 1 for n in represented.values()):
        report.issue("delay_fit_source_conservation_failed", where)


def _audit_shared_delay(candidate, whole, points, local, continuity, report, where):
    """Check persisted timestamp pairs and source continuity, not fit quality."""
    process = _required(whole, "delay_process", list, report, where)
    source = {_identity(row): row for row in points}
    delay_by_id = {row.get("source_id"): row for row in process}
    if len(delay_by_id) != len(process) or delay_by_id.keys() != source.keys():
        report.issue("delay_process_source_conservation_failed", where)
    windows = {w.get("window_id"): w for w in local}
    window_sources = {
        key: {
            identity
            for fragment in window.get("fragments", [])
            for identity in fragment.get("source_ids", [])
        }
        for key, window in windows.items()
    }
    aligned = {
        key
        for fragment in whole.get("fragments", [])
        for key in fragment.get("source_ids", [])
    }
    groups = collections.defaultdict(list)
    for row in process:
        key = row.get("source_id")
        location = f"{where}/{key}"
        original = source.get(key)
        if original is None:
            continue
        if row.get("t") != original.get("t") or row.get("source_rid") != original.get(
            "source_rid", original.get("rid")
        ):
            report.issue("delay_process_source_time_pair_changed", location)
        if row.get("schedule_aligned") is not (key in aligned):
            report.issue("delay_process_alignment_membership_changed", location)
        delay, corrected = row.get("delay_s"), row.get("corrected_t")
        if delay is not None:
            if not (
                _finite(delay)
                and _finite(corrected)
                and _finite(row.get("t"))
                and abs(corrected - (row["t"] - delay)) <= 1e-6
            ):
                report.issue("delay_corrected_timestamp_pair_changed", location)
            if row.get("status") != "SUPPORTED" or row.get("fragment_id") is None:
                report.issue("invalid_supported_delay_status", location)
            if key not in continuity:
                report.issue("delay_process_uses_rejected_projection", location)
            support = _required(row, "supporting_window_ids", list, report, location)
            if not support:
                report.issue("delay_without_supporting_window", location)
            for window_id in support:
                window = windows.get(window_id, {})
                if window.get("status") != "EVIDENCE" or key not in window_sources.get(
                    window_id, set()
                ):
                    report.issue("delay_support_window_pair_changed", location)
        elif corrected is not None or row.get("status") == "SUPPORTED":
            report.issue("invalid_unsupported_delay_timestamp", location)
        if row.get("fragment_id") is not None:
            groups[row["fragment_id"]].append(row)
    for group, rows in groups.items():
        rows.sort(key=lambda row: (row.get("t") or 0, row.get("source_id") or ""))
        ids = [row["source_id"] for row in rows]
        if not _continuous_ids(ids, continuity):
            report.issue("delay_fragment_crosses_source_break", f"{where}/{group}")
        supported = [r for r in rows if _finite(r.get("corrected_t"))]
        for left, right in zip(supported, supported[1:], strict=False):
            if right["corrected_t"] < left["corrected_t"] - 1e-6:
                report.issue(
                    "shared_delay_corrected_time_regression", f"{where}/{group}"
                )
        report.examined["shared_delay_order_pairs"] += max(0, len(supported) - 1)
    for fragment in whole.get("fragments", []):
        ids = fragment.get("source_ids", [])
        location = f"{where}/alignment:{fragment.get('fragment_id')}"
        if not _continuous_ids(ids, continuity):
            report.issue("whole_alignment_crosses_source_break", location)
        delayed = [delay_by_id.get(key, {}) for key in ids]
        if any(not _finite(row.get("corrected_t")) for row in delayed):
            report.issue("whole_alignment_without_shared_delay_pair", location)
        if len({row.get("fragment_id") for row in delayed}) != 1:
            report.issue("whole_alignment_crosses_delay_fragments", location)
        if ids and all(key in source for key in ids):
            start, end = source[ids[0]].get("t"), source[ids[-1]].get("t")
            if (
                fragment.get("source_start") != start
                or fragment.get("source_end") != end
            ):
                report.issue("whole_alignment_source_timestamp_pair_changed", location)
            if (
                _finite(start)
                and _finite(end)
                and (
                    not _finite(fragment.get("aligned_duration_s"))
                    or abs(fragment["aligned_duration_s"] - (end - start)) > 1e-6
                )
            ):
                report.issue("whole_alignment_duration_changed", location)
        if (
            fragment.get("all_original_points_dtw_validated") is not True
            or fragment.get("original_points") != len(ids)
            or fragment.get("dtw_scored_points") != len(ids)
        ):
            report.issue("whole_alignment_not_full_source_validated", location)
    if candidate.get("scheduled_trajectory") is None:
        report.issue("unknown_scheduled_trajectory_schema", where, warning=True)
    _audit_delay_fit(whole, delay_by_id, continuity, report, where)
    report.examined["shared_delay_source_pairs"] += len(process)
    report.distributions["whole_alignment_status"][str(whole.get("status"))] += 1


def _audit_artifact(artifact, original_rows, references, trips, report, where):
    config = _required(artifact, "config", dict, report, where)
    gate = config.get("max_projection_distance_m")
    if not _finite(gate) or gate <= 0:
        report.issue("unknown_gate_config", where, warning=True)
        gate = 100.0
    projection = _required(artifact, "projection", dict, report, where)
    points = _required(projection, "points", list, report, where)
    apc = _required(artifact, "apc_evidence", list, report, where)
    if "apc_evidence" in projection and projection["apc_evidence"] != apc:
        report.issue("apc_artifact_copies_differ", where)
    actual = points + apc
    by_id = {_identity(row): row for row in actual}
    expected = {_identity(row): row for row in original_rows}
    if len(by_id) != len(actual):
        report.issue("case_duplicate_source_identity", where)
    if by_id.keys() != expected.keys():
        report.issue("case_source_identity_changed", where)
    ref_ids = {r["reference_id"] for r in references}
    for key, row in by_id.items():
        location = f"{where}/{key}"
        original = expected.get(key)
        if original is not None:
            for field, value in original.items():
                if _normal(value) != row.get(field):
                    report.issue("case_original_field_changed", f"{location}.{field}")
            instant = (
                original.get("time")
                if original["source"] == "avl"
                else original.get("arrival")
                if original.get("arrival") is not None
                else original.get("departure")
            )
            if row.get("t") != _normal(instant):
                report.issue("case_wall_clock_changed", location)
            if original["source"] == "apc":
                if row.get("interval_start") != _normal(
                    original.get("arrival")
                ) or row.get("interval_end") != _normal(original.get("departure")):
                    report.issue("apc_interval_changed", location)
                if row.get("trajectory_fabricated") is not False:
                    report.issue(
                        "unknown_apc_trajectory_contract", location, warning=True
                    )
        if str(row.get("route") or "").strip() != "29":
            report.issue("non_route_29_in_case", location)
        alternatives = _required(row, "projection_alternatives", list, report, location)
        if len({a.get("reference_id") for a in alternatives}) != len(alternatives):
            report.issue("duplicate_reference_alternative", location)
        for index, hypothesis in enumerate([row] + alternatives):
            status = hypothesis.get("projection_status")
            if status not in PROJECTION_STATUSES:
                report.issue("unknown_projection_status", location, warning=True)
            if (
                hypothesis.get("reference_id") is not None
                and hypothesis["reference_id"] not in ref_ids
            ):
                report.issue("unknown_projection_reference", location)
            if status == "ON_REFERENCE":
                lateral = hypothesis.get("lateral_m")
                if not _finite(lateral) or not 0 <= lateral <= gate + 1e-6:
                    report.issue(
                        "on_reference_gate_failed", f"{location}/hypothesis:{index}"
                    )
        report.distributions[f"{row.get('source')}_projection_status"][
            row.get("projection_status", "unknown")
        ] += 1
    result = _required(artifact, "result", dict, report, where)
    status = result.get("status")
    if status not in RESULT_STATUSES:
        report.issue("unknown_result_status", where, warning=True)
    report.distributions["result_status"][str(status)] += 1
    final = _required(result, "candidates", list, report, where)
    candidates = _required(result, "all_candidates", list, report, where)
    report.distributions["final_candidate_count"][str(len(final))] += 1
    canonical = {_trip_key(t): t for t in trips}
    if len(final) > 3:
        report.issue("more_than_three_candidates", where)
    for group in (final, candidates):
        if len({_trip_key(c) for c in group}) != len(group):
            report.issue("duplicate_candidate_identity", where)
    for candidate in candidates + final:
        key = _trip_key(candidate)
        if key not in canonical or candidate.get("candidate_id") != key:
            report.issue("candidate_identity_changed", f"{where}/{key}")
        elif candidate.get("reference_id") != canonical[key].get("reference_id"):
            report.issue("candidate_reference_changed", f"{where}/{key}")
        if candidate.get("probability") is not None or candidate.get(
            "calibrated"
        ) not in {False, None}:
            report.issue("uncalibrated_candidate_probability", f"{where}/{key}")
    if result.get("calibration", {}).get("calibrated") is not False:
        report.issue("unknown_calibration_contract", where, warning=True)
    _ratios(result, report, where)
    if status == "MATCHED":
        if not final:
            report.issue("matched_without_candidate", where)
        else:
            best = final[0]
            whole = _required(best, "whole_alignment", dict, report, where)
            minimum = config.get("aggregation_min_coverage")
            if not _finite(minimum):
                report.issue("unknown_match_coverage_threshold", where, warning=True)
            else:
                if whole.get("status") != "EVIDENCE":
                    report.issue("matched_without_whole_evidence", where)
                for field in (
                    "coverage",
                    "schedule_aligned_point_ratio",
                    "schedule_aligned_duration_ratio",
                    "schedule_supported_window_weight_ratio",
                ):
                    if field not in best:
                        report.issue(
                            "unknown_matched_coverage_schema",
                            f"{where}.{field}",
                            warning=True,
                        )
                    elif not _finite(best[field]) or best[field] < minimum - 1e-9:
                        report.issue(
                            "matched_below_required_coverage", f"{where}.{field}"
                        )
    windows = _required(result, "windows", dict, report, where)
    raw_windows = _required(windows, "windows", list, report, where)
    if len({w.get("id") for w in raw_windows}) != len(raw_windows):
        report.issue("duplicate_window_id", where)
    weight_sums, atom_counts = collections.Counter(), collections.Counter()
    window_ids = {}
    for window in raw_windows:
        location = f"{where}/{window.get('id')}"
        ids = _required(window, "observation_ids", list, report, location)
        weights = _required(window, "weights", dict, report, location)
        window_ids[window.get("id")] = set(ids)
        if (
            len(set(ids)) != len(ids)
            or set(ids) != weights.keys()
            or set(ids) - by_id.keys()
        ):
            report.issue("window_source_membership_changed", location)
        for key, weight in weights.items():
            if not _finite(weight) or not 0 <= weight <= 1 + 1e-9:
                report.issue("invalid_window_source_weight", location)
                continue
            weight_sums[key] += weight
            if window.get("kind") == "atomic":
                atom_counts[key] += 1
        if (
            _finite(window.get("weight"))
            and abs(window["weight"] - sum(w for w in weights.values() if _finite(w)))
            > 1e-8
        ):
            report.issue("window_total_weight_changed", location)
    normalized = _required(windows, "observation_weights", dict, report, where)
    for key in by_id:
        value = weight_sums[key]
        if value > 1 + 1e-8:
            report.issue("source_window_weight_exceeds_one", f"{where}/{key}")
        if (
            key not in normalized
            or not _finite(normalized[key])
            or abs(normalized[key] - value) > 1e-8
        ):
            report.issue("normalized_window_weight_changed", f"{where}/{key}")
        if atom_counts[key] > 1:
            report.issue("overlapping_atomic_ownership", f"{where}/{key}")
    local = _required(result, "local_scores", dict, report, where)
    continuity_by_reference = {}

    def verify_scored(metric, reference, location, allowed=None):
        fragments = metric.get("fragments", [])
        if metric.get("status") == "EVIDENCE":
            fragments = _required(metric, "fragments", list, report, location)
            if not fragments:
                report.issue("evidence_without_score_fragments", location)
        for fragment in fragments:
            sampled = _required(fragment, "sampled_source_ids", list, report, location)
            full = _required(fragment, "source_ids", list, report, location)
            if not set(sampled).issubset(full):
                report.issue("sampled_source_not_in_full_score_trace", location)
            for key in set(sampled + full):
                source = by_id.get(key)
                hypothesis = (
                    _reference_projection(source, reference) if source else None
                )
                valid = (
                    source is not None
                    and source.get("source") == "avl"
                    and hypothesis is not None
                    and hypothesis.get("projection_status") == "ON_REFERENCE"
                )
                if not valid:
                    report.issue(
                        "rejected_or_foreign_reference_point_scored",
                        f"{location}/{key}",
                    )
                if allowed is not None and key not in allowed:
                    report.issue("scored_source_outside_window", f"{location}/{key}")
            report.examined["sampled_score_source_checks"] += len(sampled)
            report.examined["full_score_source_checks"] += len(full)

    for candidate_id, scores in local.items():
        trip = canonical.get(candidate_id)
        if trip is None or not isinstance(scores, list):
            report.issue(
                "unknown_local_candidate_schema",
                f"{where}/{candidate_id}",
                warning=True,
            )
            continue
        for score in scores:
            allowed = window_ids.get(score.get("window_id"))
            if allowed is None:
                report.issue("unknown_scoring_window", where)
            verify_scored(
                score,
                trip["reference_id"],
                f"{where}/{candidate_id}/{score.get('window_id')}",
                allowed,
            )
    for candidate in candidates:
        whole = candidate.get("whole_alignment")
        if isinstance(whole, dict):
            reference = candidate.get("reference_id")
            if reference not in continuity_by_reference:
                continuity_by_reference[reference] = _reference_continuity(
                    points, reference, config, report, where
                )
            verify_scored(
                whole,
                reference,
                f"{where}/{candidate.get('candidate_id')}/whole",
            )
            _audit_shared_delay(
                candidate,
                whole,
                points,
                local.get(candidate.get("candidate_id"), []),
                continuity_by_reference[reference],
                report,
                f"{where}/{candidate.get('candidate_id')}/whole",
            )
            trip = canonical.get(_trip_key(candidate), {})
            if candidate.get("scheduled_trajectory") != trip.get("scheduled"):
                report.issue("candidate_scheduled_timestamp_pairs_changed", where)
    report.examined.update(
        cases=1,
        avl_case_members=len(points),
        apc_case_members=len(apc),
        windows=len(raw_windows),
        candidate_evaluations=len(candidates),
    )


def audit_artifact(artifact, original_rows, references, trips, *, max_examples=20):
    """Pure bounded diagnostic entrypoint, also used for mutation unit tests."""
    report = _Report(max_examples)
    _audit_artifact(artifact, original_rows, references, trips, report, "case")
    return report.output()


def _pinned(root, relative, digest, report, category):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        report.issue(f"{category}_missing_or_unsafe_path", relative)
    elif not isinstance(digest, str) or _sha(path) != digest:
        report.issue(f"{category}_hash_changed", relative)


def _audit_replay_lineage(root, manifest, output_hashes, report):
    """Verify stage provenance without running a historical or new decision."""
    if not manifest.get("decision_replay"):
        return None
    source_path = manifest.get("decision_source_path")
    if not isinstance(source_path, str):
        report.issue("unknown_decision_source_path", "manifest", warning=True)
        return None
    source = Path(source_path).resolve()
    if source == root or not source.is_dir():
        report.issue("invalid_decision_source_path", source_path)
        return None
    old = _read(source / "manifest.json")
    completion = _read(source / "completion.json")
    record = _read(source / "run.json")
    if (
        completion.get("validation_ok") is not True
        or record.get("status") != "complete"
        or (source / "failure.json").exists()
        or old.get("decision_replay")
    ):
        report.issue("decision_source_not_completed_heavy_run", source.name)
    if (
        manifest.get("decision_source_run_id") != source.name
        or manifest.get("parent_run_id") != source.name
    ):
        report.issue("decision_source_identity_changed", source.name)
    source_outputs = _read(source / "output_sha256.json")
    if not isinstance(source_outputs, dict):
        report.issue("unknown_decision_source_inventory", source.name, warning=True)
        return None
    required = dict(source_outputs)
    required.update(
        {
            name: _sha(source / name)
            for name in ("output_sha256.json", "completion.json", "run.json")
        }
    )
    pinned = _required(manifest, "decision_source_sha256", dict, report, "manifest")
    if pinned != required:
        report.issue("decision_source_hash_inventory_changed", source.name)
    for relative, digest in pinned.items():
        _pinned(source, relative, digest, report, "decision_source")
    provenance = _required(manifest, "stage_provenance", dict, report, "manifest")
    if (
        provenance.get("upstream_code_sha256") != old.get("code_sha256")
        or provenance.get("upstream_evidence_run_id") != source.name
        or provenance.get("recomputed_stages") != ["final_decision"]
        or provenance.get("heavy_scores_recomputed") is not False
        or manifest.get("code_sha256") != old.get("code_sha256")
    ):
        report.issue("decision_stage_provenance_changed", source.name)
    code = _required(manifest, "decision_code_sha256", dict, report, "manifest")
    legacy_code = {"decision.py", "redecide.py"}
    storage_code = legacy_code | {"runner.py", "case_storage.py"}
    if set(code) not in (legacy_code, storage_code) or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
        for value in code.values()
    ):
        report.issue("unknown_decision_code_provenance", source.name, warning=True)
    mutable_manifest = {
        "run_id",
        "baseline",
        "created_at",
        "parent_run_id",
        "decision_replay",
        "decision_source_run_id",
        "decision_source_path",
        "decision_source_sha256",
        "decision_code_sha256",
        "stage_provenance",
    }
    for key, value in old.items():
        if key not in mutable_manifest and manifest.get(key) != value:
            report.issue("replay_upstream_manifest_changed", key)
    mutable_files = {"manifest.json", "summary.json"}
    for relative, digest in source_outputs.items():
        if relative.startswith(("cases/", "catalog/")) or relative in mutable_files:
            continue
        if output_hashes.get(relative) != digest:
            report.issue("replay_copied_evidence_hash_changed", relative)
    if {p for p in source_outputs if p.startswith("cases/")} != {
        p for p in output_hashes if p.startswith("cases/")
    }:
        report.issue("replay_case_artifact_set_changed", source.name)
    report.examined["decision_source_hashes"] += len(pinned)
    return source


def _audit_replay_case(source, relative, artifact, report):
    old = _read(source / relative)
    if {k: v for k, v in old.items() if k != "result"} != {
        k: v for k, v in artifact.items() if k != "result"
    }:
        report.issue("replay_case_source_or_projection_changed", relative)
    result = _required(artifact, "result", dict, report, relative)
    previous = _required(old, "result", dict, report, relative)
    for key, kind in (
        ("all_candidates", list),
        ("local_scores", dict),
        ("windows", dict),
        ("retrieval", dict),
        ("whole_trip", dict),
    ):
        value = _required(result, key, kind, report, relative)
        original = _required(previous, key, kind, report, relative)
        if value != original:
            report.issue("replay_heavy_scoring_evidence_changed", f"{relative}.{key}")
    report.distributions["replay_decision_transitions"][
        f"{previous.get('status')} -> {result.get('status')}"
    ] += 1
    report.examined["replay_cases_heavy_equality"] += 1


def _audit_replay_catalog(source, case, catalog, report):
    old = _read(source / "catalog" / f"{case}.json")
    mutable = {
        "gtfs_status",
        "gtfs_candidate_count",
        "pattern_confidence",
        "trip_confidence",
    }

    def stable(rows):
        return [{k: v for k, v in r.items() if k not in mutable} for r in rows]

    if stable(old) != stable(catalog):
        report.issue("replay_catalog_membership_or_boundary_changed", case)


_GAP_POLICY = {
    "operator": ">",
    "scope": "each existing same-vehicle direction parent, after route scoping",
    "presence": (
        "union of actual AVL instants and closed APC arrival/departure intervals"
    ),
    "avl_hold_s": 0.0,
    "apc_invalid_interval": "valid arrival point, else valid departure point",
    "timestamp_alias": "AVL event_time only when the time field is absent",
    "direction_labels": "all labels, including unknown and conflicting labels",
    "spatial_quality": "irrelevant to timestamped record presence",
    "invalid_time": "missing, nonfinite, ambiguous or nonexistent cannot bridge gaps",
    "parent_clipping": True,
    "edge_gaps_split": False,
    "unlocated": "separate diagnostic child only when its parent is split",
    "source_conservation": "exactly once within each original parent membership",
}


def _audit_gap_policy(manifest, config, report):
    """Old runs have no refinement contract; new runs must pin every semantic."""
    active = (
        "segmentation_joint_gap_s" in config
        or "joint_gap_segmentation" in manifest
        or "parent_boundaries_refined" in manifest
    )
    if not active:
        return None
    if manifest.get("parent_boundaries_refined") is not True:
        report.issue("joint_gap_refinement_flag_missing", "manifest")
    threshold = config.get("segmentation_joint_gap_s")
    if not _finite(threshold) or threshold < 0:
        report.issue("joint_gap_threshold_invalid", "config.gtfs")
        threshold = 1800.0
    expected = dict(_GAP_POLICY, segmentation_joint_gap_s=threshold)
    policy = manifest.get("joint_gap_segmentation")
    if not isinstance(policy, dict) or policy != expected:
        report.issue("joint_gap_policy_changed", "manifest.joint_gap_segmentation")
    return float(threshold)


def _gap_clock(value):
    """Parse a genuine wall-clock value, never reinterpret a timezone or alias."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return None
        value = (value - EPOCH).total_seconds()
    return float(value) if _finite(value) else None


def _gap_presence(row, parent):
    # Generic invalid includes bad spatial coordinates. It must not hide a
    # genuine timestamp; direction conflicts also do not erase record presence.
    if row.get("time_ambiguous") or row.get("time_nonexistent"):
        return None
    if row.get("source") == "avl":
        value = row.get("time") if "time" in row else row.get("event_time")
        first = last = _gap_clock(value)
    elif row.get("source") == "apc":
        arrival = _gap_clock(row.get("arrival"))
        departure = _gap_clock(row.get("departure"))
        first = arrival if arrival is not None else departure
        last = departure if arrival is None else arrival
        if arrival is not None and departure is not None and departure >= arrival:
            last = departure
    else:
        return None
    if first is None or last is None:
        return None
    lower, upper = _gap_clock(parent.get("start")), _gap_clock(parent.get("end"))
    if lower is not None:
        if last < lower or (last == lower and not parent.get("start_inclusive", True)):
            return None
        first = max(first, lower)
    if upper is not None:
        if first > upper or (first == upper and not parent.get("end_inclusive", True)):
            return None
        last = min(last, upper)
    return (first, last) if first <= last else None


def _source_counter(rows):
    return collections.Counter((r.get("source"), r.get("rid")) for r in rows)


def _independent_gap_partitions(rows, parent, threshold):
    """Frontier sweep over pinned inputs, independent of runner/gap implementation.

    The catalog never supplies an observation's interval or assigned island.
    Endpoint ownership is indexed independently to audit all boundary witnesses.
    """
    intervals, unlocated = [], []
    starts, ends = collections.defaultdict(set), collections.defaultdict(set)
    for index, row in enumerate(rows):
        span = _gap_presence(row, parent)
        if span is None:
            unlocated.append(row)
            continue
        first, last = span
        key = (row["source"], row["rid"])
        starts[first].add(key)
        ends[last].add(key)
        intervals.append((first, last, index))
    gaps, frontier = [], None

    def refs(keys):
        return [dict(source=s, rid=r) for s, r in sorted(keys)]

    for first, last, _ in sorted(intervals):
        if frontier is not None and first - frontier > threshold:
            gaps.append(
                dict(
                    start=frontier,
                    end=first,
                    duration_s=first - frontier,
                    threshold_s=threshold,
                    operator=">",
                    left_source_rids=refs(ends[frontier]),
                    right_source_rids=refs(starts[first]),
                )
            )
        frontier = last if frontier is None else max(frontier, last)
    partitions = [[] for _ in range(len(gaps) + 1)]
    cut_times = [gap["end"] for gap in gaps]
    # Restore original source ordering rather than letting interval sorting
    # manufacture a new display order or mutable source alias.
    for first, _, index in sorted(intervals, key=lambda item: item[2]):
        partitions[bisect.bisect_right(cut_times, first)].append(rows[index])
    return gaps, partitions, unlocated


def _audit_gap_catalog(
    original_rows, parent_segments, catalog, threshold, report, where
):
    """Verify source partitions before any artifact is paired with child inputs."""
    parents = collections.defaultdict(list)
    for row in original_rows:
        vehicle = row.get("vehicle")
        for parent_id in row.get("candidate_segment_ids") or [
            f"direction:{vehicle}:unresolved"
        ]:
            parents[parent_id].append(row)
    actual = {child["id"]: child for child in catalog}
    if len(actual) != len(catalog):
        report.issue("joint_gap_duplicate_child_identity", where)
    expected_members, expected_metadata, parent_metadata = {}, {}, {}
    for parent_id, rows in parents.items():
        segment = parent_segments.get(parent_id, {})
        parent = dict(segment)
        for key in ("start", "end"):
            parent[key] = _gap_clock(segment.get(key))
        if not segment:
            times = [
                value
                for row in rows
                if (value := _gap_clock(row.get("event_time"))) is not None
            ]
            parent.update(start=min(times, default=None), end=max(times, default=None))
        parent_metadata[parent_id] = parent
        gaps, partitions, unlocated = _independent_gap_partitions(
            rows, parent, threshold
        )
        report.examined["joint_gap_parent_cases"] += 1
        if not gaps:
            expected_members[parent_id] = rows
            expected_metadata[parent_id] = dict(
                start=parent["start"], end=parent["end"]
            )
            child = actual.get(parent_id)
            if child and (child.get("gap_split") or child.get("parent_case_id")):
                report.issue("joint_gap_fake_split", parent_id)
            continue
        report.examined["joint_gap_split_parent_cases"] += 1
        report.examined["joint_gap_count"] += len(gaps)
        report.examined["joint_gap_unlocated_observations"] += len(unlocated)
        common = dict(
            parent_case_id=parent_id,
            parent_segment_start=parent["start"],
            parent_segment_end=parent["end"],
            parent_boundary_reason=parent.get("boundary_reason"),
            parent_boundary_preserved=False,
            gap_split=True,
            gap_fragment_count=len(partitions),
        )
        for index, child_rows in enumerate(partitions):
            child_id = f"{parent_id}:gap:{index + 1:04d}"
            expected_members[child_id] = child_rows
            expected_metadata[child_id] = dict(
                common,
                gap_unlocated=False,
                gap_fragment_index=index + 1,
                gap_boundary_before=gaps[index - 1] if index else None,
                gap_boundary_after=gaps[index] if index < len(gaps) else None,
                start=gaps[index - 1]["end"] if index else parent["start"],
                end=gaps[index]["start"] if index < len(gaps) else parent["end"],
                start_inclusive=True if index else parent.get("start_inclusive", True),
                end_inclusive=True
                if index < len(gaps)
                else parent.get("end_inclusive", True),
                boundary_reason="joint_observation_gap"
                if index
                else parent.get("boundary_reason"),
            )
        if unlocated:
            child_id = f"{parent_id}:gap:unlocated"
            expected_members[child_id] = unlocated
            expected_metadata[child_id] = dict(
                common,
                kind="direction_unresolved",
                gap_unlocated=True,
                gap_fragment_index=None,
                gap_boundary_before=None,
                gap_boundary_after=None,
                start=None,
                end=None,
                boundary_reason="unlocated_timestamp",
            )
    if actual.keys() != expected_members.keys():
        report.issue("joint_gap_child_partition_changed", where)
    declared_by_parent = collections.defaultdict(collections.Counter)
    for child in catalog:
        child_id = child["id"]
        parent_id = child.get("parent_case_id") if child.get("gap_split") else child_id
        declared_by_parent[parent_id].update(
            _source_counter(child.get("source_rids", []))
        )
        rows = expected_members.get(child_id)
        if rows is None:
            continue
        if _source_counter(child.get("source_rids", [])) != _source_counter(rows):
            report.issue("joint_gap_child_source_membership_changed", child_id)
        for field, value in expected_metadata[child_id].items():
            if child.get(field) != value:
                report.issue("joint_gap_child_boundary_changed", f"{child_id}.{field}")
        source_parent = parent_metadata.get(parent_id, {})
        if (
            "direction" in source_parent
            and child.get("direction") != source_parent["direction"]
        ):
            report.issue("joint_gap_child_direction_changed", child_id)
        if child.get("gap_split"):
            spans = [
                span
                for row in rows
                if (span := _gap_presence(row, source_parent)) is not None
            ]
            checks = dict(
                avl_count=sum(row["source"] == "avl" for row in rows),
                avl_member_count=sum(row["source"] == "avl" for row in rows),
                apc_count=sum(row["source"] == "apc" for row in rows),
                source_start=min((span[0] for span in spans), default=None),
                source_end=max((span[1] for span in spans), default=None),
            )
            for field, value in checks.items():
                if child.get(field) != value:
                    report.issue(
                        "joint_gap_child_evidence_extent_changed", f"{child_id}.{field}"
                    )
    for parent_id in parents.keys() | declared_by_parent.keys():
        if declared_by_parent[parent_id] != _source_counter(parents.get(parent_id, [])):
            report.issue("joint_gap_parent_source_partition_changed", parent_id)
    report.examined["joint_gap_output_cases"] += len(expected_members)
    return expected_members


def audit_run(run, *, max_examples=20):
    """Audit an already completed run using only persisted inputs and outputs."""
    root = Path(run).resolve()
    completion = _read(root / "completion.json")
    record = _read(root / "run.json")
    if (
        completion.get("validation_ok") is not True
        or record.get("status") != "complete"
        or (root / "failure.json").exists()
    ):
        raise ValueError("Audit requires a completed, non-failed experiment run")
    manifest = _read(root / "manifest.json")
    if (
        manifest.get("representation") != "gtfs_trip_candidates"
        or manifest.get("debug_date") != "2023-11-06"
    ):
        raise ValueError("Audit supports only completed Nov6 GTFS candidate runs")
    report = _Report(max_examples)
    config = _read(root / "config.json")
    gap_threshold = _audit_gap_policy(manifest, config.get("gtfs", {}), report)
    output_hashes = _read(root / "output_sha256.json")
    if not isinstance(output_hashes, dict):
        raise ValueError("Output hash inventory must be an object")
    expected_hashes = {
        "manifest.json",
        "config.json",
        "selection.json",
        "summary.json",
        "gtfs/feed.json",
        "gtfs/canonical.json",
    }
    for relative, digest in output_hashes.items():
        _pinned(root, relative, digest, report, "output")
    replay_source = _audit_replay_lineage(root, manifest, output_hashes, report)
    parent = Path(manifest["direction_parent_path"]).resolve()
    if not parent.exists():
        parent = resolve_direction_parent(root, manifest)
    parent_hashes = _required(manifest, "parent_sha256", dict, report, "manifest")
    for required in (
        "manifest.json",
        "config.json",
        "summary.json",
        "completion.json",
        "selection.json",
    ):
        if required not in parent_hashes:
            report.issue("required_parent_hash_missing", required)
    for relative, digest in parent_hashes.items():
        _pinned(parent, relative, digest, report, "parent")
    for case, digest in _required(
        manifest, "parent_observations_sha256", dict, report, "manifest"
    ).items():
        _pinned(parent, f"observations/{case}.parquet", digest, report, "parent")
    canonical = _read(root / "gtfs/canonical.json")
    references = _required(canonical, "references", list, report, "canonical")
    trips = _required(canonical, "trips", list, report, "canonical")
    identities = [_trip_key(t) for t in trips]
    if len(identities) != len(set(identities)):
        report.issue("duplicate_canonical_trip_identity", "canonical.trips")
    allowed_dates = {
        manifest["debug_date"],
        (date.fromisoformat(manifest["debug_date"]) - timedelta(days=1)).isoformat(),
    }
    for row in references + trips:
        if row.get("feed_version") != manifest.get("gtfs_feed_version") or row.get(
            "route_id"
        ) != manifest.get("route_id"):
            report.issue("canonical_namespace_changed", "canonical")
        if "trip_id" in row and row.get("service_date") not in allowed_dates:
            report.issue("canonical_service_date_outside_scope", _trip_key(row))
    selection = _read(root / "selection.json")
    parent_selection = _read(parent / "selection.json")
    selected_vehicles = {s["vehicle_case"] for s in selection}
    expected_vehicles, all_catalog_ids = set(), set()
    suffix = manifest.get("case_artifact_suffix")
    if suffix not in {".json", ".json.gz"}:
        report.issue("unknown_case_artifact_suffix", "manifest", warning=True)
        return report.output()
    for item in parent_selection:
        case, vehicle = item["vehicle_case"], item["raw_vehicle"]
        parent_frame = pl.read_parquet(parent / "observations" / f"{case}.parquet")
        if "route" not in parent_frame.columns:
            continue
        expected = parent_frame.filter(
            pl.col("route").cast(pl.String).str.strip_chars() == "29"
        )
        if expected.is_empty():
            continue
        expected_vehicles.add(case)
        if case not in selected_vehicles:
            report.issue("route_29_vehicle_omitted", case)
            continue
        actual_frame = pl.read_parquet(root / "observations" / f"{case}.parquet")
        if not actual_frame.equals(expected):
            report.issue("route_29_original_rows_changed", case)
        # Pinned parent observations, not the output under examination, define
        # source truth even after an attacker refreshes the output hash inventory.
        original_rows = expected.to_dicts()
        report.examined.update(
            {
                f"unique_{source}": sum(r["source"] == source for r in original_rows)
                for source in ("avl", "apc")
            }
        )
        expected_members = collections.defaultdict(list)
        for row in original_rows:
            for segment in row.get("candidate_segment_ids") or [
                f"direction:{vehicle}:unresolved"
            ]:
                expected_members[segment].append(row)
        catalog = _read(root / "catalog" / f"{case}.json")
        if replay_source is not None:
            _audit_replay_catalog(replay_source, case, catalog, report)
        parent_segments = {
            s["id"]: s
            for s in _read(parent / "direction" / f"{case}.json").get("segments", [])
        }
        if gap_threshold is not None:
            expected_members = _audit_gap_catalog(
                original_rows, parent_segments, catalog, gap_threshold, report, case
            )
        if {s["id"] for s in catalog} != expected_members.keys():
            report.issue("catalog_case_membership_changed", case)
        for summary in catalog:
            segment = summary["id"]
            if segment in all_catalog_ids:
                report.issue("duplicate_global_case_identity", segment)
            all_catalog_ids.add(segment)
            rows = expected_members.get(segment, [])
            if {
                (r.get("source"), r.get("rid")) for r in summary.get("source_rids", [])
            } != {(r["source"], r["rid"]) for r in rows}:
                report.issue("catalog_source_membership_changed", segment)
            if segment in parent_segments:
                for field in ("start", "end"):
                    value = parent_segments[segment][field]
                    original = (
                        datetime.fromisoformat(value)
                        if isinstance(value, str)
                        else value
                    )
                    if summary.get(field) != _normal(original):
                        report.issue("parent_boundary_changed", f"{segment}.{field}")
            relative = (
                "cases/" + hashlib.sha256(segment.encode()).hexdigest()[:24] + suffix
            )
            expected_hashes.add(relative)
            artifact = _read(root / relative)
            if gap_threshold is not None:
                recorded = artifact.get("config", {}).get("segmentation_joint_gap_s")
                if not _finite(recorded) or recorded != gap_threshold:
                    report.issue("joint_gap_artifact_threshold_changed", segment)
            if replay_source is not None:
                _audit_replay_case(replay_source, relative, artifact, report)
            if artifact.get("case_id") != segment:
                report.issue("artifact_case_identity_changed", segment)
            _audit_artifact(artifact, rows, references, trips, report, segment)
        expected_hashes.update(
            {
                f"observations/{case}.parquet",
                f"catalog/{case}.json",
                f"scope/{case}.json",
            }
        )
        if f"direction/{case}.json" not in parent_hashes:
            report.issue("parent_boundary_hash_missing", case)
    if expected_vehicles != selected_vehicles:
        report.issue("route_29_vehicle_selection_changed", "selection")
    for missing in sorted(expected_hashes - output_hashes.keys()):
        report.issue("required_output_hash_missing", missing)
    result = report.output()
    result.update(
        run_id=root.name,
        run_path=str(root),
        audit_script_sha256=AUDIT_SCRIPT_SHA256,
        run_manifest_sha256=_sha(root / "manifest.json"),
        decision_replay=manifest.get("decision_replay", False),
        decision_source_run_id=manifest.get("decision_source_run_id"),
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--max-examples", type=int, default=20)
    args = parser.parse_args()
    result = audit_run(args.run, max_examples=max(0, args.max_examples))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["validation_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
