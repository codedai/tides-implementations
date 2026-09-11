"""Read-only, episode-grouped evaluation of persisted matcher shadow results.

Label provenance is deliberately explicit. Saved matcher decisions and upstream
schedule-derived fields are not independent truth. Missing labels or attributes
produce null metrics, never accuracy claims based on agreement between scorers.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

DEFAULTS = {
    "continuous_evaluation_ece_bins": 10,
    "continuous_evaluation_boundary_tolerance_s": 120.0,
    "continuous_evaluation_headway_bins_s": [300.0, 600.0, 1200.0],
    "continuous_evaluation_quality_bins": [0.35, 0.7, 0.9],
}
LABEL_SOURCES = {"manual_verified", "external_ground_truth", "independent_validated"}
SPLITS = {"train", "validation", "test"}
METRICS = (
    "candidate_retrieval_recall",
    "pattern_accuracy",
    "exact_trip_accuracy",
    "adjacent_trip_confusion",
    "top3_coverage",
    "reported_top3_coverage",
    "precision_non_abstained",
    "abstention_rate",
    "calibration_error",
    "delay_mae_s",
    "boundary_precision",
    "boundary_recall",
    "boundary_f1",
    "runtime_mean_s",
    "runtime_p90_s",
)


def _finite(value):
    return (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def episode_key(row):
    """A case ID alone is not globally unique across feeds/routes/service dates."""
    return tuple(
        str(row.get(key, ""))
        for key in ("feed_version", "route_id", "service_date", "case_id")
    )


def load_verified_labels(path):
    """Load JSON array, JSONL or CSV label records; validate before use.

    CSV booleans use literal ``true``/``false`` and list-valued columns use JSON.
    No external writes, schedule assignments or review edits occur here.
    """
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        if path.suffix.lower() == ".csv":
            rows = list(csv.DictReader(stream))
            for row in rows:
                for key in ("verified", "independent", "no_scheduled_trip"):
                    if key in row:
                        row[key] = {"true": True, "false": False}.get(
                            row[key], row[key]
                        )
                for key in ("adjacent_trip_ids", "boundary_times_s", "candidates"):
                    if row.get(key):
                        row[key] = json.loads(row[key])
                if row.get("true_delay_s"):
                    row["true_delay_s"] = float(row["true_delay_s"])
        elif path.suffix.lower() in {".jsonl", ".ndjson"}:
            rows = [json.loads(line) for line in stream if line.strip()]
        else:
            rows = json.load(stream)
    if not isinstance(rows, list):
        raise ValueError("Independent labels must be a list of episode records")
    validate_independent_labels(rows)
    return rows


def validate_independent_labels(labels):
    """Reject unverified labels and route/date/operating-episode split leakage.

    Route, service date and episode identifiers must each be disjoint across
    splits. This is intentionally stricter than checking a combined route/date
    key: otherwise different days of the same route leak route-specific tuning.
    Several performed fragments in one operating episode may share one split.
    """
    rows = list(labels or [])
    if not rows:
        return {"status": "NO_INDEPENDENT_LABELS", "count": 0, "splits": {}}
    seen = set()
    memberships = {
        key: defaultdict(set) for key in ("route_id", "service_date", "episode_id")
    }
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Independent label records must be objects")
        if (
            row.get("verified") is not True
            or row.get("independent") is not True
            or row.get("label_source") not in LABEL_SOURCES
        ):
            raise ValueError("Labels require explicit verified, independent provenance")
        if row.get("schedule_derived") or row.get("uses_upstream_schedule_assignment"):
            raise ValueError(
                "Pre-existing schedule assignments are not independent labels"
            )
        for key in ("case_id", "route_id", "service_date", "episode_id"):
            if row.get(key) in (None, ""):
                raise ValueError(f"Independent label missing {key}")
        split = row.get("split")
        if split not in SPLITS:
            raise ValueError("Independent labels require train/validation/test split")
        has_trip = row.get("true_trip_id") not in (None, "")
        no_trip = row.get("no_scheduled_trip") is True
        if has_trip == no_trip:
            raise ValueError(
                "Label requires exactly one true_trip_id or no_scheduled_trip"
            )
        key = episode_key(row)
        if key in seen:
            raise ValueError(
                "Duplicate or conflicting label for a performed episode case"
            )
        seen.add(key)
        for field, groups in memberships.items():
            groups[str(row[field])].add(split)
    for field, groups in memberships.items():
        leaking = [value for value, splits in groups.items() if len(splits) > 1]
        if leaking:
            raise ValueError(
                f"Independent validation {field} leakage across splits: {leaking[:3]}"
            )
    return {
        "status": "VERIFIED_GROUP_DISJOINT_LABELS",
        "count": len(rows),
        "splits": dict(Counter(row["split"] for row in rows)),
        "split_policy": "route_id AND service_date AND episode_id disjoint",
        "provenance": sorted({row["label_source"] for row in rows}),
    }


def _candidates(row):
    return (
        row.get(
            "all_candidates", row.get("ranked_candidates", row.get("candidates", []))
        )
        or []
    )


def _trip_id(candidate):
    return str(candidate.get("trip_id", candidate.get("candidate_id", "")))


def _prediction(row):
    identity = row.get("decision_trip_id", row.get("matched_trip_id"))
    if identity is None:
        selected = row.get("decision_candidate_id")
        candidates = _candidates(row)
        candidate = next(
            (item for item in candidates if item.get("candidate_id") == selected), None
        )
        if candidate is None and candidates:
            candidate = candidates[0]
        identity = _trip_id(candidate) if candidate else None
    return str(identity) if identity is not None else None


def _retrieved(row):
    explicit = row.get("retrieved_trip_ids")
    if explicit is not None:
        return {str(item) for item in explicit}
    retrieval = row.get("retrieval", {})
    ids = retrieval.get("candidate_ids") if isinstance(retrieval, dict) else None
    mapping = {
        str(candidate.get("candidate_id")): _trip_id(candidate)
        for candidate in _candidates(row)
    }
    if ids is not None:
        return {mapping.get(str(item), str(item)) for item in ids}
    return {_trip_id(candidate) for candidate in _candidates(row)}


def _ratio(items):
    return sum(items) / len(items) if items else None


def _quantile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def expected_calibration_error(pairs, bins=10):
    """ECE over actual calibrated probabilities, not normalized score weights."""
    pairs = list(pairs)
    if not pairs:
        return None
    if bins < 1:
        raise ValueError("ECE bins must be positive")
    if any(
        not _finite(p) or not 0 <= p <= 1 or y not in (0, 1, False, True)
        for p, y in pairs
    ):
        raise ValueError("ECE requires probabilities and binary outcomes")
    groups = defaultdict(list)
    for probability, correct in pairs:
        groups[min(bins - 1, int(probability * bins))].append((probability, correct))
    return sum(
        len(group)
        / len(pairs)
        * abs(
            sum(p for p, _ in group) / len(group)
            - sum(y for _, y in group) / len(group)
        )
        for group in groups.values()
    )


def _boundaries(predicted, truth, tolerance):
    """Maximum ordered one-to-one event matching, avoiding double-counted hits."""
    predicted, truth = sorted(predicted), sorted(truth)
    if any(not _finite(item) for item in predicted + truth):
        raise ValueError("Boundary times must be finite service-day seconds")
    left = right = matches = 0
    while left < len(predicted) and right < len(truth):
        delta = predicted[left] - truth[right]
        if abs(delta) <= tolerance:
            matches += 1
            left += 1
            right += 1
        elif delta < 0:
            left += 1
        else:
            right += 1
    return matches, len(predicted), len(truth)


def _score_rows(rows, labels, config):
    by_key = {episode_key(label): label for label in labels}
    samples = defaultdict(list)
    boundaries = [0, 0, 0]
    boundary_n = 0
    paired_count = 0
    for row in rows:
        samples["abstention_rate"].append(row.get("status") != "MATCHED")
        runtime = row.get("runtime_s", row.get("processing_seconds"))
        if _finite(runtime) and runtime >= 0:
            samples["runtime"].append(runtime)
        label = by_key.get(episode_key(row))
        if label is None:
            continue
        paired_count += 1
        candidates = _candidates(row)
        truth = (
            str(label["true_trip_id"])
            if label.get("true_trip_id") is not None
            else None
        )
        predicted = _prediction(row)
        committed = row.get("status") == "MATCHED"
        correct = truth is not None and predicted == truth
        if truth is not None:
            samples["candidate_retrieval_recall"].append(truth in _retrieved(row))
            samples["exact_trip_accuracy"].append(committed and correct)
            samples["top3_coverage"].append(
                truth in {_trip_id(item) for item in candidates[:3]}
            )
            samples["reported_top3_coverage"].append(
                truth in {_trip_id(item) for item in row.get("candidates", [])[:3]}
            )
            if "adjacent_trip_ids" in label:
                samples["adjacent_trip_confusion"].append(
                    committed
                    and not correct
                    and predicted in {str(item) for item in label["adjacent_trip_ids"]}
                )
        if committed:
            samples["precision_non_abstained"].append(correct)
        if label.get("true_pattern_id") is not None:
            pattern = row.get("pattern_id")
            if pattern is None and candidates:
                pattern = candidates[0].get("pattern_id")
            samples["pattern_accuracy"].append(
                str(pattern) == str(label["true_pattern_id"])
            )
        top = candidates[0] if candidates else {}
        probability = top.get("probability")
        if (
            top.get("calibrated") is True
            and _finite(probability)
            and 0 <= probability <= 1
        ):
            samples["calibration"].append(
                (probability, truth is not None and _trip_id(top) == truth)
            )
        delay = row.get("estimated_delay_s", top.get("estimated_delay_s"))
        if (
            committed
            and correct
            and _finite(delay)
            and _finite(label.get("true_delay_s"))
        ):
            samples["delay_mae_s"].append(abs(delay - label["true_delay_s"]))
        if "boundary_times_s" in label and "boundary_times_s" in row:
            counts = _boundaries(
                row["boundary_times_s"],
                label["boundary_times_s"],
                config["continuous_evaluation_boundary_tolerance_s"],
            )
            boundaries = [a + b for a, b in zip(boundaries, counts, strict=True)]
            boundary_n += 1
    metrics = {key: _ratio(samples[key]) for key in METRICS}
    metrics["calibration_error"] = expected_calibration_error(
        samples["calibration"], int(config["continuous_evaluation_ece_bins"])
    )
    metrics["runtime_mean_s"] = _ratio(samples["runtime"])
    metrics["runtime_p90_s"] = _quantile(samples["runtime"], 0.9)
    matched, predicted_n, truth_n = boundaries
    metrics["boundary_precision"] = matched / predicted_n if predicted_n else None
    metrics["boundary_recall"] = matched / truth_n if truth_n else None
    metrics["boundary_f1"] = (
        2 * matched / (predicted_n + truth_n) if predicted_n + truth_n else None
    )
    details = {}
    for name, value in metrics.items():
        source = (
            "calibration"
            if name == "calibration_error"
            else "runtime"
            if name.startswith("runtime_")
            else name
        )
        n = boundary_n if name.startswith("boundary_") else len(samples[source])
        reason = (
            None
            if value is not None
            else "NO_INDEPENDENT_LABELS"
            if not labels
            else "NO_ELIGIBLE_LABELED_SAMPLES_OR_REQUIRED_ATTRIBUTE"
        )
        if name == "calibration_error" and labels and value is None:
            reason = "NO_VALIDATED_CALIBRATED_PROBABILITIES"
        details[name] = {"n": n, "reason": reason}
    return {
        "metrics": metrics,
        "metric_details": details,
        "case_count": len(rows),
        "labeled_case_count": paired_count,
    }


def _band(value, thresholds):
    if not _finite(value):
        return "UNKNOWN"
    for threshold in thresholds:
        if value < threshold:
            return f"lt_{threshold:g}"
    return f"ge_{thresholds[-1]:g}" if thresholds else "ALL"


def evaluate_shadow(old_rows, new_rows, labels=None, config=None):
    """Compare saved outputs, without ingesting/reprojecting raw observations.

    Only test-split labels are used for reported quality metrics. Training and
    validation labels are accepted for provenance/leakage checks, not reused as
    independent final test accuracy. Unlabeled reports contain descriptive
    status/rank changes and runtime only.
    """
    c = DEFAULTS | (config or {})
    old_rows, new_rows, labels = list(old_rows), list(new_rows), list(labels or [])
    validation = validate_independent_labels(labels)
    test_labels = [label for label in labels if label["split"] == "test"]
    for name, rows in (("old", old_rows), ("new", new_rows)):
        keys = [episode_key(row) for row in rows]
        if len(set(keys)) != len(keys):
            raise ValueError(f"Duplicate {name} shadow case identity")
    old = _score_rows(old_rows, test_labels, c)
    new = _score_rows(new_rows, test_labels, c)
    old_by_key = {episode_key(row): row for row in old_rows}
    new_by_key = {episode_key(row): row for row in new_rows}
    common = old_by_key.keys() & new_by_key.keys()
    strata = {}
    selectors = {
        "route": lambda row: str(row.get("route_id", "UNKNOWN")),
        "headway": lambda row: _band(
            row.get("local_headway_s"), c["continuous_evaluation_headway_bins_s"]
        ),
        "shape_structure": lambda row: str(
            row.get("shape_structure", row.get("pattern_structure", "UNKNOWN"))
        ),
        "data_quality": lambda row: _band(
            row.get("valid_projection_point_ratio", row.get("coverage")),
            c["continuous_evaluation_quality_bins"],
        ),
    }
    for dimension, selector in selectors.items():
        buckets = defaultdict(lambda: {"old": [], "new": []})
        for side, rows in (("old", old_rows), ("new", new_rows)):
            for row in rows:
                buckets[selector(row)][side].append(row)
        strata[dimension] = {
            bucket: {
                side: _score_rows(items, test_labels, c)
                for side, items in sides.items()
            }
            for bucket, sides in sorted(buckets.items())
        }
    return {
        "schema_version": "continuous-shadow-evaluation-v1",
        "validation": validation
        | {"evaluation_split": "test", "test_label_count": len(test_labels)},
        "old": old,
        "new": new,
        "comparison": {
            key: new["metrics"][key] - old["metrics"][key]
            if new["metrics"][key] is not None and old["metrics"][key] is not None
            else None
            for key in METRICS
        },
        "descriptive": {
            "paired_cases": len(common),
            "old_only_cases": len(old_by_key.keys() - new_by_key.keys()),
            "new_only_cases": len(new_by_key.keys() - old_by_key.keys()),
            "status_changes": sum(
                old_by_key[key].get("status") != new_by_key[key].get("status")
                for key in common
            ),
            "top_rank_changes": sum(
                _prediction(old_by_key[key]) != _prediction(new_by_key[key])
                for key in common
            ),
            "old_status_counts": dict(
                Counter(row.get("status", "UNKNOWN") for row in old_rows)
            ),
            "new_status_counts": dict(
                Counter(row.get("status", "UNKNOWN") for row in new_rows)
            ),
            "agreement_is_not_accuracy": True,
        },
        "strata": strata,
        "metric_definitions": {
            "exact_trip_accuracy": (
                "Correct MATCHED decisions / labeled scheduled episodes; "
                "abstention counts as incorrect."
            ),
            "top3_coverage": (
                "True occurrence in first three ranked diagnostic candidates, "
                "regardless of abstention."
            ),
            "reported_top3_coverage": (
                "True occurrence in first three final reported scheduled candidates."
            ),
            "adjacent_trip_confusion": (
                "Wrong MATCHED adjacent occurrence / episodes with independently "
                "supplied adjacent-trip IDs."
            ),
            "delay_mae_s": (
                "Absolute delay error among correctly identified MATCHED episodes "
                "with independent delay labels."
            ),
            "abstention_rate": (
                "Fraction of all persisted cases whose status is not MATCHED; "
                "descriptive without labels."
            ),
            "calibration_error": (
                "Top-ranked calibrated candidate equal-width reliability ECE "
                "on independent test episodes."
            ),
            "boundary_f1": (
                "One-to-one boundary event F1 within configured seconds tolerance."
            ),
        },
    }
