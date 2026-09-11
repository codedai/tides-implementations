"""Independent read-only audit of persisted four-mode decision experiments.

This module deliberately does not import the partitioner, matcher, decision
guard, overview builder, or their validation functions. It checks stored source
identity, clocks, boundaries, display gates and hashes; it does not assess trip
identity accuracy or recompute alignment scores.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import math
from pathlib import Path

MODES = ("global", "early", "middle", "late")
VERSION = "independent-four-mode-persisted-v1"


def _read(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError("Pinned path escapes its run")
    return path


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _key(row):
    return f"{row.get('source', 'avl')}|{row.get('source_rid', row.get('rid'))}"


def _candidate_key(row):
    return row.get("candidate_id") or "|".join(
        str(row.get(k, "")) for k in ("feed_version", "service_date", "trip_id")
    )


def _valid(row, config):
    return (
        row.get("source", "avl") == "avl"
        and row.get("projection_status") == "ON_REFERENCE"
        and _finite(row.get("t"))
        and _finite(row.get("s_m"))
        and _finite(row.get("quality", 1))
        and row.get("quality", 1) >= config.get("window_min_quality", 0.2)
    )


def _inside(t, spans):
    return _finite(t) and any(
        span["start"] <= t
        and (t <= span["end"] if span.get("end_inclusive") else t < span["end"])
        for span in spans
    )


class Report:
    def __init__(self):
        self.violations = collections.Counter()
        self.examined = collections.Counter()
        self.distributions = collections.defaultdict(collections.Counter)
        self.examples = []

    def check(self, condition, code, where):
        if not condition:
            self.violations[code] += 1
            if len(self.examples) < 40:
                self.examples.append({"code": code, "location": where})

    def output(self):
        return {
            "validation_ok": not self.violations,
            "violations": dict(self.violations),
            "violation_count": sum(self.violations.values()),
            "examples": self.examples,
            "examined": dict(self.examined),
            "distributions": {
                key: dict(value) for key, value in self.distributions.items()
            },
            "accuracy": None,
            "accuracy_assessed": False,
            "scores_recomputed": False,
            "production_partition_or_matcher_used": False,
            "audit_version": VERSION,
        }


def _independent_boundaries(points, apc, config):
    """Run-wise high-water lengths, independent of persisted selection metadata."""
    rows = sorted(
        (r for r in points if _finite(r.get("t"))), key=lambda r: (r["t"], _key(r))
    )
    times = [
        row[k]
        for row in [*rows, *apc]
        for k in ("t", "interval_start", "interval_end")
        if _finite(row.get(k))
    ]
    cumulative, previous, maximum, travelled = [], None, None, 0.0
    for row in rows:
        if not _valid(row, config):
            previous = None
        else:
            continuous = (
                previous is not None
                and row.get("reference_id") == previous.get("reference_id")
                and row.get("fragment_id") == previous.get("fragment_id")
                and not row.get("scope_separator_before")
                and not row.get("sequence_restart")
                and 0 < row["t"] - previous["t"] <= config.get("window_max_gap_s", 90)
            )
            if continuous:
                next_maximum = max(maximum, row["s_m"])
                travelled += next_maximum - maximum
                maximum = next_maximum
            else:
                maximum = row["s_m"]
            previous = row
        cumulative.append(travelled)
    cut_times = []
    if travelled > 0:
        for threshold in (travelled / 3, 2 * travelled / 3):
            cut_times.append(
                next(
                    r["t"]
                    for r, distance in zip(rows, cumulative, strict=True)
                    if distance >= threshold
                )
            )
    ranges = {}
    if times:
        ranges["global"] = [
            {"start": min(times), "end": max(times), "end_inclusive": True}
        ]
    else:
        ranges["global"] = []
    for index, mode in enumerate(MODES[1:]):
        if travelled <= 0:
            ranges[mode] = []
        else:
            edges = [min(times), *cut_times, max(times)]
            ranges[mode] = [
                {
                    "start": edges[index],
                    "end": edges[index + 1],
                    "end_inclusive": index == 2,
                }
            ]
    return rows, travelled, ranges


def _scan_scope(value, allowed_avl, allowed_all, report, where):
    if isinstance(value, list):
        for index, child in enumerate(value):
            _scan_scope(child, allowed_avl, allowed_all, report, f"{where}[{index}]")
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in {"probability", "calibrated_probability"}:
                report.check(
                    child is None, "uncalibrated_probability", f"{where}.{key}"
                )
            elif key == "source_id":
                report.check(
                    child in allowed_avl,
                    "outside_mode_alignment_source",
                    f"{where}.{key}",
                )
            elif key in {"source_ids", "sampled_source_ids"}:
                report.check(
                    set(child) <= allowed_avl,
                    "outside_mode_alignment_sources",
                    f"{where}.{key}",
                )
            elif key == "observation_ids":
                report.check(
                    set(child) <= allowed_all,
                    "outside_mode_window_sources",
                    f"{where}.{key}",
                )
            _scan_scope(child, allowed_avl, allowed_all, report, f"{where}.{key}")


def _clip_expected(row, spans):
    if not spans:
        return None
    span = spans[0]
    start, end = row.get("interval_start"), row.get("interval_end")
    if _finite(start) and _finite(end) and start <= end:
        left, right = max(start, span["start"]), min(end, span["end"])
        if left > right or (left == right and not _inside(left, spans)):
            return None
        return left, right
    return (row["t"], row["t"]) if _inside(row.get("t"), spans) else None


def audit_modes(artifact, config, report, where):
    points = artifact.get("projection", {}).get("points", [])
    apc = artifact.get("projection", {}).get("apc_evidence", [])
    by_id = {_key(row): row for row in points}
    by_apc = {_key(row): row for row in apc}
    timed, distance, ranges = _independent_boundaries(points, apc, config)
    modes = artifact.get("matching_modes", {})
    report.check(set(modes) == set(MODES), "missing_modes", where)
    report.check(len(by_id) == len(points), "duplicate_original_avl", where)
    owners = collections.Counter()
    if not distance:
        report.examined["zero_projected_travel_cases"] += 1
    for mode, entry in modes.items():
        if mode not in MODES:
            continue
        location = f"{where}:{mode}"
        result, selection = entry["result"], entry["selection"]
        report.distributions[f"{mode}_status"][result.get("status")] += 1
        report.distributions[f"{mode}_evidence"][result.get("evidence_status")] += 1
        report.distributions[f"{mode}_selection"][selection.get("status")] += 1
        selected = selection.get("selected_avl_ids", [])
        selected_apc = selection.get("selected_apc_ids", [])
        expected_avl = (
            set(by_id)
            if mode == "global"
            else {_key(row) for row in timed if _inside(row["t"], ranges[mode])}
        )
        expected_apc = (
            set(by_apc)
            if mode == "global"
            else {
                key
                for key, row in by_apc.items()
                if _clip_expected(row, ranges[mode]) is not None
            }
        )
        report.check(
            set(selected) == expected_avl and len(set(selected)) == len(selected),
            "incorrect_avl_selection",
            location,
        )
        report.check(
            set(selected_apc) == expected_apc
            and len(set(selected_apc)) == len(selected_apc),
            "incorrect_apc_selection",
            location,
        )
        report.check(
            selection.get("time_ranges") == ranges[mode],
            "incorrect_third_time_ranges",
            location,
        )
        report.check(
            math.isclose(
                selection.get("total_effective_progress_m", -1), distance, abs_tol=1e-6
            ),
            "incorrect_partition_distance",
            location,
        )
        expected_valid = {key for key in expected_avl if _valid(by_id[key], config)}
        report.check(
            set(selection.get("selected_valid_avl_ids", [])) == expected_valid,
            "incorrect_valid_avl_selection",
            location,
        )
        report.check(
            len(result.get("candidates", [])) <= 3, "candidate_cap_exceeded", location
        )
        if mode != "global":
            owners.update(selected)
            clips = {
                row["source_id"]: row for row in selection.get("apc_interval_clips", [])
            }
            report.check(
                set(clips) == expected_apc, "apc_clip_lineage_missing", location
            )
            for key, clip in clips.items():
                original = by_apc.get(key, {})
                report.check(
                    (clip.get("interval_start"), clip.get("interval_end"))
                    == _clip_expected(original, ranges[mode]),
                    "apc_clip_outside_scope",
                    f"{location}:{key}",
                )
                report.check(
                    clip.get("original_start") == original.get("interval_start")
                    and clip.get("original_end") == original.get("interval_end"),
                    "apc_original_interval_changed",
                    f"{location}:{key}",
                )
                report.check(
                    clip.get("passenger_counts_prorated") is False,
                    "apc_counts_fabricated",
                    f"{location}:{key}",
                )
            if distance <= 0:
                report.check(
                    result.get("evidence_status") == "NO_EVIDENCE"
                    and not result.get("all_candidates"),
                    "zero_travel_borrowed_evidence",
                    location,
                )
        _scan_scope(
            result, set(selected), set(selected) | set(selected_apc), report, location
        )
        for candidate in result.get("all_candidates", []):
            previous = {}
            for row in candidate.get("whole_alignment", {}).get("delay_process", []):
                original = by_id.get(row.get("source_id"))
                report.check(
                    original is not None and row.get("t") == original.get("t"),
                    "delay_source_clock_changed",
                    location,
                )
                corrected = row.get("corrected_t")
                if corrected is None:
                    continue
                good = all(
                    _finite(row.get(key)) for key in ("t", "delay_s", "corrected_t")
                )
                report.check(good, "invalid_delay_clock", location)
                if not good:
                    continue
                report.check(
                    math.isclose(row["t"] - row["delay_s"], corrected, abs_tol=1e-6),
                    "incorrect_corrected_clock",
                    location,
                )
                fragment = row.get("fragment_id")
                report.check(fragment is not None, "missing_delay_fragment", location)
                if fragment in previous:
                    report.check(
                        row["t"] >= previous[fragment][0]
                        and corrected + 1e-6 >= previous[fragment][1],
                        "nonmonotone_delay_clock",
                        location,
                    )
                previous[fragment] = row["t"], corrected
        report.examined["mode_results"] += 1
    report.check(
        all(count == 1 for count in owners.values()), "partial_avl_overlap", where
    )
    expected_owners = {_key(row) for row in timed} if distance > 0 else set()
    report.check(set(owners) == expected_owners, "partial_avl_not_conserved", where)


def _all_gates(candidate, config):
    minimum = config.get("aggregation_min_coverage", 0.35)
    ratios = (
        "coverage",
        "schedule_supported_window_weight_ratio",
        "schedule_aligned_point_ratio",
        "schedule_aligned_duration_ratio",
    )
    score, support = candidate.get("score"), candidate.get("support_score")
    return (
        _finite(score)
        and 0 <= score <= config.get("aggregation_match_cost_max", 3)
        and _finite(support)
        and 0 <= support <= 1
        and candidate.get("whole_alignment", {}).get("status") == "EVIDENCE"
        and all(
            _finite(candidate.get(key)) and candidate[key] >= minimum for key in ratios
        )
    )


def _knots(schedule):
    rows = []
    for original in schedule:
        s = original.get("s_m", original.get("s"))
        if "arrival_t" in original or "departure_t" in original:
            clocks = [
                (kind, original.get(key))
                for kind, key in (
                    ("arrival", "arrival_t"),
                    ("departure", "departure_t"),
                )
            ]
        else:
            clocks = [(None, original.get("t"))]
        for kind, t in clocks:
            if not _finite(t) or not _finite(s):
                continue
            row = {"t": t, "s": s}
            if kind:
                row["kind"] = kind
            if "stop_id" in original:
                row["stop_id"] = original["stop_id"]
            rows.append((t, original.get("stop_sequence", 0), row))
    return [row for _, _, row in sorted(rows, key=lambda value: value[:2])]


def _hypothesis(point, reference):
    if point.get("reference_id") == reference:
        return point
    alternative = next(
        (
            r
            for r in point.get("projection_alternatives", [])
            if r.get("reference_id") == reference
        ),
        None,
    )
    return (
        {**point, **alternative}
        if alternative
        else {**point, "projection_status": "UNPROJECTABLE"}
    )


def _continuity(points, reference, config):
    previous, group, result = None, 0, {}
    for source in sorted(
        points, key=lambda p: (p.get("t") is None, p.get("t") or 0, _key(p))
    ):
        row = _hypothesis(source, reference)
        if not _valid(row, config):
            previous = None
            continue
        if (
            previous is None
            or row.get("scope_separator_before")
            or row.get("fragment_id") != previous.get("fragment_id")
            or row.get("reference_id") != previous.get("reference_id")
            or row["t"] - previous["t"] > config.get("score_max_gap_s", 90)
            or row["s_m"]
            < previous["s_m"] - config.get("score_backtrack_tolerance_m", 25)
        ):
            group += 1
        result[str(row["rid"])] = group
        previous = row
    return result


def audit_overview_case(record, artifact, canonical, mode, config, report):
    location = f"overview:{mode}:{artifact['case_id']}"
    entry = artifact["matching_modes"][mode]
    result, selection = entry["result"], entry["selection"]
    report.check(
        record.get("decision_selection") == selection,
        "overview_selection_changed",
        location,
    )
    report.check(
        record.get("status") == result.get("status"),
        "overview_status_changed",
        location,
    )
    ranked = sorted(
        (
            r
            for r in result.get("all_candidates", [])
            if _finite(r.get("score")) and r["score"] >= 0
        ),
        key=lambda r: (r["score"], _candidate_key(r)),
    )
    accepted = [row for row in ranked if _all_gates(row, config)]
    expected = accepted[0] if accepted else ranked[0] if ranked else None
    candidate = record.get("candidate")
    source = "accepted" if accepted else "fallback" if ranked else "none"
    report.check(
        record.get("candidate_source") == source,
        "overview_candidate_source_dishonest",
        location,
    )
    report.distributions[f"{mode}_overview_candidate_source"][
        record.get("candidate_source")
    ] += 1
    if expected is None:
        report.check(candidate is None, "overview_invented_candidate", location)
    elif candidate is None:
        report.check(False, "overview_candidate_missing", location)
    else:
        report.check(
            candidate.get("candidate_id") == _candidate_key(expected),
            "overview_wrong_candidate_rank",
            location,
        )
        report.check(
            candidate.get("source") == source
            and candidate.get("diagnostic_only") == (source == "fallback"),
            "overview_fallback_not_marked",
            location,
        )
        report.check(
            candidate.get("probability") is None
            and candidate.get("trip_identity_confirmed") is False
            and candidate.get("calibrated") is False,
            "overview_false_confidence",
            location,
        )
        report.check(
            candidate.get("reference_id") == expected.get("reference_id"),
            "overview_candidate_reference_changed",
            location,
        )
        report.check(
            candidate.get("trajectory")
            == _knots(expected.get("scheduled_trajectory", [])),
            "overview_schedule_shifted_or_fabricated",
            location,
        )
        gtfs = next(
            (
                row
                for row in canonical.get("trips", [])
                if _candidate_key(row) == _candidate_key(expected)
            ),
            None,
        )
        report.check(
            gtfs is not None
            and expected.get("scheduled_trajectory") == gtfs.get("scheduled"),
            "candidate_not_original_gtfs_schedule",
            location,
        )
        failed = candidate.get("failed_gate_checks", [])
        report.check(
            bool(failed) == (source == "fallback"),
            "overview_gate_failure_hidden",
            location,
        )
    points = artifact.get("projection", {}).get("points", [])
    by_rid = {str(row["rid"]): row for row in points}
    reference_ids = {row["reference_id"] for row in canonical.get("references", [])}
    selected = set(selection.get("selected_valid_avl_ids", []))
    visible, membership = [], {}
    for group in record.get("fragments", []):
        reference = group.get("reference_id")
        report.check(
            reference in reference_ids, "overview_unknown_reference_axis", location
        )
        if reference not in membership:
            membership[reference] = _continuity(points, reference, config)
        groups = set()
        for t, s, rid, highlighted in group.get("points", []):
            original = by_rid.get(rid)
            report.check(original is not None, "overview_invented_avl", location)
            if original is None:
                continue
            projected = _hypothesis(original, reference)
            report.check(
                _valid(projected, config)
                and projected.get("t") == t
                and projected.get("s_m") == s,
                "overview_point_or_axis_changed",
                location,
            )
            report.check(
                highlighted == (mode == "global" or _key(original) in selected),
                "overview_wrong_selected_highlight",
                location,
            )
            groups.add(membership[reference].get(rid))
            visible.append(rid)
        report.check(
            len(groups) <= 1 and None not in groups,
            "overview_bridged_separator",
            location,
        )
    primary = {str(row["rid"]) for row in points if _valid(row, config)}
    report.check(primary <= set(visible), "overview_omitted_valid_projection", location)
    report.check(
        len(visible) == len(set(visible)), "overview_duplicate_projection", location
    )
    report.check(
        record.get("omitted_valid_primary_point_count") == 0,
        "overview_omission_counter",
        location,
    )
    for row in record.get("apc_intervals", []):
        report.check(
            row.get("trajectory_fabricated") is False,
            "overview_apc_fake_trajectory",
            location,
        )
        for span in row.get("selected_intervals", []):
            report.check(
                row["start"] <= span["start"] <= span["end"] <= row["end"],
                "overview_apc_clip_outside_original",
                location,
            )
            if mode != "global":
                report.check(
                    any(
                        scope["start"] <= span["start"] <= span["end"] <= scope["end"]
                        for scope in selection.get("time_ranges", [])
                    ),
                    "overview_apc_clip_outside_mode",
                    location,
                )
    report.examined["overview_cases"] += 1


def audit_run(newpath):
    root = Path(newpath).resolve()
    report = Report()
    manifest = _read(root / "manifest.json")
    completion = _read(root / "completion.json")
    report.check(completion.get("validation_ok") is True, "run_not_complete", root.name)
    report.check(
        manifest.get("partial_matching_replay") is True, "not_partial_replay", root.name
    )
    source = Path(manifest["partial_source_path"]).resolve()
    outputs = _read(root / "output_sha256.json")
    source_pins = manifest["partial_source_sha256"]
    start_hashes = {}
    for directory, pins, kind in (
        (root, outputs, "new_output_hash"),
        (source, source_pins, "source_pin_hash"),
    ):
        for relative, digest in pins.items():
            path = _path(directory, relative)
            actual = _sha(path) if path.is_file() else None
            report.check(actual == digest, kind, str(path))
            start_hashes[str(path)] = actual
            report.examined[kind] += 1
    for name, digest in manifest.get("partial_code_sha256", {}).items():
        path = Path(__file__).with_name(name + ".py")
        report.check(
            path.is_file() and _sha(path) == digest, "partial_code_hash_changed", name
        )
    source_outputs = _read(source / "output_sha256.json")
    for relative, digest in source_outputs.items():
        if relative.startswith(("cases/", "catalog/", "overview/")) or relative in {
            "manifest.json",
            "config.json",
            "summary.json",
            "completion.json",
            "run.json",
            "output_sha256.json",
        }:
            continue
        report.check(
            outputs.get(relative) == digest, "copied_raw_or_gtfs_changed", relative
        )
        report.examined["unchanged_copied_files"] += 1
    case_files = {name for name in outputs if name.startswith("cases/")}
    report.check(
        case_files == {name for name in source_outputs if name.startswith("cases/")},
        "case_set_changed",
        root.name,
    )
    config_bundle = _read(root / "config.json")
    config = config_bundle.get("gtfs", config_bundle)
    old_config_bundle = _read(source / "config.json")
    old_config = old_config_bundle.get("gtfs", old_config_bundle)
    report.check(
        {k: v for k, v in config.items() if k != "matching_partial_modes_enabled"}
        == {
            k: v for k, v in old_config.items() if k != "matching_partial_modes_enabled"
        },
        "matching_thresholds_changed",
        root.name,
    )
    canonical = _read(root / "gtfs/canonical.json")
    overviews, records = {}, {}
    for mode in MODES:
        overview = _read(root / f"overview/{mode}.json")
        overviews[mode] = overview
        rows = overview.get("cases", [])
        records[mode] = {row["id"]: row for row in rows}
        report.check(
            len(records[mode]) == len(rows) == len(case_files),
            "overview_case_count",
            mode,
        )
        report.check(
            overview.get("schedule_time_basis") == "original_scheduled_wall_clock"
            and overview.get("matching_recomputed") is False,
            "overview_time_or_matching_policy",
            mode,
        )
        expected_refs = {
            row["reference_id"]: row.get("direction_label")
            for row in canonical.get("references", [])
        }
        report.check(
            {
                row["reference_id"]: row.get("direction_label")
                for row in overview.get("reference_facets", [])
            }
            == expected_refs,
            "overview_reference_facets_changed",
            mode,
        )
    case_ids = set()
    for relative in sorted(case_files):
        old, artifact = _read(source / relative), _read(root / relative)
        case_id = artifact["case_id"]
        case_ids.add(case_id)

        def stable(row):
            return {
                key: value
                for key, value in row.items()
                if key not in {"config", "matching_modes"}
            }

        report.check(
            stable(old) == stable(artifact), "upstream_case_evidence_changed", case_id
        )
        report.check(
            artifact.get("matching_modes", {}).get("global", {}).get("result")
            == old.get("result"),
            "global_result_changed",
            case_id,
        )
        audit_modes(artifact, config, report, case_id)
        for mode in MODES:
            if case_id not in records[mode]:
                report.check(False, "overview_case_missing", f"{case_id}:{mode}")
            else:
                audit_overview_case(
                    records[mode][case_id], artifact, canonical, mode, config, report
                )
        report.examined["cases"] += 1
    for mode in MODES:
        report.check(
            set(records[mode]) == case_ids, "overview_case_identity_mismatch", mode
        )
        summary = overviews[mode].get("summary", {})
        report.check(
            summary.get("case_count") == len(case_ids),
            "overview_summary_case_count",
            mode,
        )
        report.check(
            summary.get("statuses") == dict(report.distributions[f"{mode}_status"]),
            "overview_summary_statuses",
            mode,
        )
    for path, digest in start_hashes.items():
        report.check(_sha(path) == digest, "file_changed_during_audit", path)
    return report.output() | {
        "run_id": root.name,
        "source_run_id": source.name,
        "audit_sha256": _sha(__file__),
        "raw_and_global_preservation_assessed": True,
        "scope": "Independent persisted invariants; no matching-accuracy claim",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit_run(args.run)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        destination = args.output.resolve()
        source = Path(
            _read(args.run / "manifest.json")["partial_source_path"]
        ).resolve()
        if destination.is_relative_to(args.run.resolve()) or destination.is_relative_to(
            source
        ):
            parser.error("Audit report must be outside completed runs")
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(payload + "\n")
    print(payload)
    raise SystemExit(0 if result["validation_ok"] else 1)


if __name__ == "__main__":
    main()
