"""Conservative listwise empirical calibration on independent operating episodes.

The model predicts mutually exclusive winner-rank outcomes plus an explicit
unknown/outside-reported-set outcome. It is NOT softmax over candidate costs.
No verified, domain-supported held-out model means null probabilities and no
claimed scheduled-trip credible set. Ranked diagnostics remain available to UI.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections import defaultdict

from .evaluation import (
    expected_calibration_error,
    validate_independent_labels,
    validate_training_provenance,
)

DEFAULTS = {
    "continuous_calibration_min_train": 60,
    "continuous_calibration_min_validation": 40,
    "continuous_calibration_min_bin": 15,
    "continuous_calibration_max_ece": 0.1,
    "continuous_calibration_credible_mass": 0.9,
    "continuous_calibration_min_heldout_set_coverage": 0.85,
    "continuous_calibration_max_unknown_probability": 0.1,
    "continuous_calibration_match_probability": 0.9,
    "continuous_calibration_max_score": 3.0,
    "continuous_calibration_min_projection_coverage": 0.35,
    "continuous_calibration_min_schedule_coverage": 0.35,
    "continuous_calibration_output_max": 3,
    "continuous_calibration_prior_count": 1.0,
    "continuous_calibration_score_bins": [0.5, 1.0, 2.0, 4.0, 8.0],
    "continuous_calibration_margin_bins": [0.1, 0.5, 1.0, 2.0],
    "continuous_scorer_fingerprint": None,
}
VERSION = "continuous-listwise-empirical-v1"
FEATURE_SCHEMA = ["top_score", "top1_top2_cost_margin", "candidate_count_capped_3"]


def _finite(value):
    return (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _config(config):
    c = DEFAULTS | (config or {})
    if not isinstance(c.get("continuous_occurrence_model_enabled", False), bool):
        raise ValueError("continuous_occurrence_model_enabled requires a boolean")
    for key, value in c.items():
        if (
            key not in DEFAULTS
            or key == "continuous_scorer_fingerprint"
            or key.endswith("_bins")
        ):
            continue
        if not _finite(value) or value < 0:
            raise ValueError(f"Invalid calibration threshold {key}")
        if (
            any(token in key for token in ("probability", "coverage", "_mass", "_ece"))
            and value > 1
        ):
            raise ValueError(
                f"Calibration probability/coverage threshold above one: {key}"
            )
    for key in (
        "continuous_calibration_min_train",
        "continuous_calibration_min_validation",
        "continuous_calibration_min_bin",
    ):
        if c[key] < 1 or int(c[key]) != c[key]:
            raise ValueError(
                f"Calibration independent sample count must be positive integer: {key}"
            )
    if c["continuous_calibration_prior_count"] <= 0:
        raise ValueError(
            "Calibration prior count must be positive to retain unknown mass"
        )
    for key in (
        "continuous_calibration_score_bins",
        "continuous_calibration_margin_bins",
    ):
        values = c[key]
        if (
            not isinstance(values, (list, tuple))
            or any(not _finite(value) for value in values)
            or list(values) != sorted(set(values))
        ):
            raise ValueError(
                f"Calibration bins must be finite and strictly increasing: {key}"
            )
    return c


def _identity(candidate):
    return str(candidate.get("candidate_id", candidate.get("trip_id", "")))


def _rank(candidates):
    candidates = list(candidates or [])
    if any(not _identity(item) for item in candidates):
        raise ValueError("Calibration candidates require stable candidate or trip IDs")
    if len({_identity(item) for item in candidates}) != len(candidates):
        raise ValueError("Calibration candidate IDs must be unique")
    return sorted(
        candidates,
        key=lambda item: (
            item["score"] if _finite(item.get("score")) else math.inf,
            _identity(item),
        ),
    )


def _features(candidates):
    ranked = _rank(candidates)
    if not ranked or not _finite(ranked[0].get("score")):
        return None
    score = ranked[0]["score"]
    margin = (
        ranked[1]["score"] - score
        if len(ranked) > 1 and _finite(ranked[1].get("score"))
        else 0.0
    )
    return {
        "top_score": score,
        "top1_top2_cost_margin": margin,
        "candidate_count_capped_3": min(3, len(ranked)),
    }


def _bin(features, config):
    return ":".join(
        str(value)
        for value in (
            bisect.bisect_right(
                config["continuous_calibration_score_bins"], features["top_score"]
            ),
            bisect.bisect_right(
                config["continuous_calibration_margin_bins"],
                features["top1_top2_cost_margin"],
            ),
            features["candidate_count_capped_3"],
        )
    )


def _class(row):
    if row.get("no_scheduled_trip") is True:
        return 3
    truth = str(row["true_trip_id"])
    for rank, candidate in enumerate(_rank(row["candidates"])[:3]):
        if str(candidate.get("trip_id", candidate.get("candidate_id"))) == truth:
            return rank
    return 3


def _distribution(counts, prior):
    total = sum(counts) + prior * 4
    return [(count + prior) / total for count in counts]


def _distribution_for(model, candidates):
    features = _features(candidates)
    if features is None:
        return None, "INVALID_OR_EMPTY_CANDIDATE_FEATURES"
    support = model["feature_support"]
    for name in FEATURE_SCHEMA:
        low, high = support[name]
        if not low <= features[name] <= high:
            return None, "CALIBRATION_FEATURES_OUT_OF_SUPPORT"
    key = _bin(features, model["config"])
    selected = model["bins"].get(key)
    if selected is None:
        return None, "CALIBRATION_BIN_HAS_NO_HELDOUT_SUPPORT"
    probabilities = list(selected["probabilities"])
    count = min(3, len(candidates))
    for rank in range(count, 3):
        probabilities[3] += probabilities[rank]
        probabilities[rank] = 0.0
    return probabilities, None


def _model_digest(model):
    payload = {key: value for key, value in model.items() if key != "model_digest"}
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def occurrence_decision_guard(candidate):
    """A free fitted delay never identifies a displaced scheduled occurrence.

    This is a decision gate, not a rank penalty or a new probability. Strong
    corroboration must independently identify this particular trip and domain;
    ordinary APC timing, geometry, fitted residuals and sequence priors do not.
    Source evidence digests bind declared evidence; they do not authenticate it.
    """
    displacement = candidate.get("occurrence_displacement")
    result = {
        "allowed": False,
        "reason": "MISSING_OR_INVALID_OCCURRENCE_DISPLACEMENT",
        "crossed_occurrences": None,
        "independent_evidence_ids": [],
    }
    if not isinstance(displacement, dict):
        return result
    if displacement.get("status", "EVIDENCE") != "EVIDENCE":
        return result
    crossed, outside = (
        displacement.get("crossed_occurrences"),
        displacement.get("outside_cell_s"),
    )
    if (
        not _finite(crossed)
        or crossed < 0
        or int(crossed) != crossed
        or not _finite(outside)
        or outside < 0
    ):
        return result
    result["crossed_occurrences"] = int(crossed)
    if crossed < 1:
        return result | {"allowed": True, "reason": "NO_CONFUSABLE_OCCURRENCE_CROSSED"}
    evidence = candidate.get("occurrence_corroboration", [])
    if isinstance(evidence, dict):
        evidence = [evidence]
    if not isinstance(evidence, list):
        evidence = []
    accepted = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        digest = item.get("evidence_sha256")
        if (
            item.get("kind")
            not in {
                "verified_dispatch_assignment",
                "independent_operating_episode_label",
            }
            or item.get("verified") is not True
            or item.get("independent") is not True
            or item.get("used_for_delay_fit") is not False
            or item.get("uses_upstream_schedule_assignment") is not False
            or not isinstance(item.get("evidence_id"), str)
            or not item["evidence_id"].strip()
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            continue
        if all(
            candidate.get(field) not in (None, "")
            and str(item.get(field, "")) == str(candidate[field])
            for field in (
                "trip_id",
                "feed_version",
                "route_id",
                "service_date",
                "performed_episode_id",
            )
        ):
            accepted.append(item["evidence_id"])
    return result | {
        "allowed": bool(accepted),
        "reason": "INDEPENDENT_OCCURRENCE_IDENTITY_CORROBORATED"
        if accepted
        else "DISPLACED_OCCURRENCE_REQUIRES_INDEPENDENT_CORROBORATION",
        "independent_evidence_ids": sorted(set(accepted)),
    }


def _validate_occurrence_artifact(model):
    """Fail closed on invalid reliability/support metadata, even if rehashed."""
    provenance = model.get("training_provenance")
    validate_training_provenance(provenance)
    if model.get("occurrence_decision_guard_version") != "independent-identity-v1":
        raise ValueError("Missing occurrence decision guard version")
    c = _config(model["config"])
    if (
        model.get("status") != "CALIBRATED_ON_INDEPENDENT_HELDOUT_EPISODES"
        or model["training_n"] != provenance["split_counts"]["train"]
        or model["training_n"] < c["continuous_calibration_min_train"]
        or model["training_groups"] != provenance["split_groups"]["train"]
        or model["validation_groups"] != provenance["split_groups"]["validation"]
    ):
        raise ValueError("Inconsistent calibration training support")
    validation = model["validation"]
    if (
        not _finite(validation.get("n"))
        or validation["n"] < c["continuous_calibration_min_validation"]
        or validation["n"] + validation.get("unsupported_n", 0)
        != provenance["split_counts"]["validation"]
        or any(
            not _finite(validation.get(key))
            or not 0 <= validation[key] <= c["continuous_calibration_max_ece"]
            for key in ("ece", "classwise_ece")
        )
        or not _finite(validation.get("credible_set_coverage"))
        or not c["continuous_calibration_min_heldout_set_coverage"]
        <= validation["credible_set_coverage"]
        <= 1
    ):
        raise ValueError("Invalid independent calibration reliability")


def fit_calibration_model(rows, config=None):
    """Fit independent rank outcome frequencies, then validate held-out reliability.

    Each row is one labeled episode with ``candidates``, explicit label/split
    provenance (see evaluation module), and a ``scorer_fingerprint``. Validation
    routes/dates/episodes cannot overlap training or final test groups. Only
    domains and bins actually represented in held-out validation are enabled.
    """
    c = _config(config)
    rows = list(rows or [])
    strict = c.get("continuous_occurrence_model_enabled", False)
    validation = validate_independent_labels(
        rows, require_complete_splits=strict, require_provenance=strict
    )
    unavailable = {
        "schema_version": VERSION,
        "calibrated": False,
        "status": "UNCALIBRATED",
        "reason": "NO_INDEPENDENT_LABELS",
        "feature_schema": FEATURE_SCHEMA,
        "label_validation": validation,
    }
    if not rows:
        return unavailable
    if strict and not validation["usable_for_occurrence_calibration"]:
        return unavailable | {"reason": "MISSING_HELDOUT_ROUTE_SERVICE_DAY_LABELS"}
    if len({str(row["episode_id"]) for row in rows}) != len(rows):
        raise ValueError(
            "Calibration requires one representative case per independent "
            "operating episode; fragments cannot count as independent samples"
        )
    fingerprint = c["continuous_scorer_fingerprint"]
    if not fingerprint or any(
        row.get("scorer_fingerprint") != fingerprint for row in rows
    ):
        raise ValueError(
            "All calibration rows require the same explicit scorer fingerprint"
        )
    if any(_features(row.get("candidates")) is None for row in rows):
        raise ValueError("Calibration episodes need finite ranked candidate features")
    train = [row for row in rows if row["split"] == "train"]
    heldout = [row for row in rows if row["split"] == "validation"]
    if (
        len(train) < c["continuous_calibration_min_train"]
        or len(heldout) < c["continuous_calibration_min_validation"]
    ):
        return unavailable | {
            "reason": "INSUFFICIENT_INDEPENDENT_TRAINING_OR_VALIDATION_EPISODES",
            "training_n": len(train),
            "validation_n": len(heldout),
        }
    counts = defaultdict(lambda: [0, 0, 0, 0])
    for row in train:
        counts[_bin(_features(row["candidates"]), c)][_class(row)] += 1
    heldout_bin_counts = defaultdict(int)
    for row in heldout:
        heldout_bin_counts[_bin(_features(row["candidates"]), c)] += 1
    bins = {
        key: {
            "n": sum(items),
            "validation_n": heldout_bin_counts[key],
            "probabilities": _distribution(
                items, c["continuous_calibration_prior_count"]
            ),
        }
        for key, items in counts.items()
        if sum(items) >= c["continuous_calibration_min_bin"]
        and heldout_bin_counts[key] >= c["continuous_calibration_min_bin"]
    }
    feature_rows = [_features(row["candidates"]) for row in train]
    model = {
        "schema_version": VERSION,
        "feature_schema": FEATURE_SCHEMA,
        "scorer_fingerprint": fingerprint,
        "config": {key: value for key, value in c.items() if key in DEFAULTS},
        "bins": bins,
        "feature_support": {
            name: [
                min(row[name] for row in feature_rows),
                max(row[name] for row in feature_rows),
            ]
            for name in FEATURE_SCHEMA
        },
        "validated_domains": sorted(
            {
                (
                    str(row.get("feed_version", "")),
                    str(row["route_id"]),
                    str(row["service_date"]),
                )
                for row in heldout
            }
        ),
        "training_groups": {
            field: sorted({str(row[field]) for row in train})
            for field in ("route_id", "service_date", "episode_id")
        },
        "validation_groups": {
            field: sorted({str(row[field]) for row in heldout})
            for field in ("route_id", "service_date", "episode_id")
        },
        "training_n": len(train),
        "label_validation": validation,
        "labels_verified_independent": True,
        "unknown_class_definition": (
            "No scheduled occurrence OR true occurrence outside the first "
            "three scored candidates."
        ),
    }
    if strict:
        model["training_provenance"] = validation["training_provenance"]
        model["occurrence_decision_guard_version"] = "independent-identity-v1"
    top_pairs, all_pairs, set_coverage, log_losses = [], [], [], []
    rejected_n = 0
    for row in heldout:
        probabilities, _ = _distribution_for(model, row["candidates"])
        if probabilities is None:
            rejected_n += 1
            continue
        truth = _class(row)
        winner = max(range(4), key=probabilities.__getitem__)
        top_pairs.append((probabilities[winner], truth == winner))
        all_pairs.extend(
            (probability, truth == index)
            for index, probability in enumerate(probabilities)
        )
        accumulated, credible = 0.0, []
        for index in sorted(range(4), key=lambda item: -probabilities[item]):
            credible.append(index)
            accumulated += probabilities[index]
            if accumulated >= c["continuous_calibration_credible_mass"]:
                break
        set_coverage.append(truth in credible)
        log_losses.append(-math.log(max(probabilities[truth], 1e-12)))
    ece = expected_calibration_error(top_pairs)
    class_ece = expected_calibration_error(all_pairs)
    coverage = sum(set_coverage) / len(set_coverage) if set_coverage else None
    calibrated = (
        len(top_pairs) >= c["continuous_calibration_min_validation"]
        and ece is not None
        and ece <= c["continuous_calibration_max_ece"]
        and class_ece <= c["continuous_calibration_max_ece"]
        and coverage >= c["continuous_calibration_min_heldout_set_coverage"]
    )
    model.update(
        {
            "calibrated": calibrated,
            "status": "CALIBRATED_ON_INDEPENDENT_HELDOUT_EPISODES"
            if calibrated
            else "VALIDATION_FAILED",
            "reason": None
            if calibrated
            else "INSUFFICIENT_HELDOUT_SUPPORT_OR_RELIABILITY",
            "validation": {
                "n": len(top_pairs),
                "unsupported_n": rejected_n,
                "ece": ece,
                "classwise_ece": class_ece,
                "credible_set_coverage": coverage,
                "log_loss": sum(log_losses) / len(log_losses) if log_losses else None,
            },
        }
    )
    model["model_digest"] = _model_digest(model)
    return model


def _coverage(candidate, name):
    value = candidate.get(name)
    if value is None:
        value = candidate.get("coverage_metrics", {}).get(name)
    if isinstance(value, dict):
        for key in ("observation_ratio", "point_ratio", "observations", "ratio"):
            if _finite(value.get(key)):
                return value[key]
        return None
    return value


def _quality(candidate, config):
    if candidate.get("quality_eligible") is False:
        return False
    if (
        not _finite(candidate.get("score"))
        or not 0 <= candidate["score"] <= config["continuous_calibration_max_score"]
    ):
        return False
    # A caller-provided true flag certifies its stricter point AND interval
    # physical-evidence gates; the absolute candidate cost remains checked here.
    if candidate.get("quality_eligible") is True:
        return True
    projection = _coverage(candidate, "projection_coverage")
    schedule = _coverage(candidate, "schedule_alignment_coverage")
    return (
        _finite(projection)
        and projection >= config["continuous_calibration_min_projection_coverage"]
        and _finite(schedule)
        and schedule >= config["continuous_calibration_min_schedule_coverage"]
    )


def calibrate_candidates(candidates, model=None, config=None):
    """Return a zero-to-three credible set only with validated probabilities.

    A calibrated rank distribution must include enough mass for the unknown
    class. We never renormalize after excluding a low-quality candidate: doing
    so would manufacture certainty. Unsupported domain/fingerprint/feature
    schema/model changes cause explicit abstention, not silent transfer.
    """
    c = _config(config)
    ranked = _rank(candidates)
    adequate_pattern = any(
        item.get("quality_eligible") is True
        or (
            _finite(_coverage(item, "projection_coverage"))
            and _coverage(item, "projection_coverage")
            >= c["continuous_calibration_min_projection_coverage"]
        )
        for item in ranked
    )
    result = {
        "calibration_status": "UNCALIBRATED",
        "calibrated": False,
        "candidate_probabilities": {_identity(item): None for item in ranked},
        "unknown_probability": None,
        "credible_set": [],
        "status": "PATTERN_ONLY" if adequate_pattern else "UNMATCHED",
        "top1_top2_margin": None,
        "pattern_confidence": None,
        "scheduled_occurrence_confidence": None,
        "reasons": [],
        "model_digest": None,
    }
    guards = None
    if c.get("continuous_occurrence_model_enabled", False):
        guards = {_identity(item): occurrence_decision_guard(item) for item in ranked}
        result["occurrence_decision_guards"] = guards
    if adequate_pattern and any(item.get("boundary_uncertain") for item in ranked):
        result["status"] = "BOUNDARY_UNCERTAIN"
    if (
        not model
        or not isinstance(model, dict)
        or model.get("calibrated") is not True
        or model.get("labels_verified_independent") is not True
    ):
        result["reasons"] = ["NO_VALIDATED_INDEPENDENT_CALIBRATOR"]
        if guards and not guards[_identity(ranked[0])]["allowed"]:
            result["reasons"].append(guards[_identity(ranked[0])]["reason"])
        return result
    invalid = None
    if (
        model.get("schema_version") != VERSION
        or model.get("feature_schema") != FEATURE_SCHEMA
    ):
        invalid = "CALIBRATION_FEATURE_SCHEMA_MISMATCH"
    else:
        try:
            if model.get("model_digest") != _model_digest(model):
                invalid = "CALIBRATION_MODEL_INTEGRITY_MISMATCH"
        except (TypeError, ValueError):
            invalid = "CALIBRATION_MODEL_INTEGRITY_MISMATCH"
    if invalid is None and (
        not c["continuous_scorer_fingerprint"]
        or model.get("scorer_fingerprint") != c["continuous_scorer_fingerprint"]
    ):
        invalid = "CALIBRATION_SCORER_FINGERPRINT_MISMATCH"
    if invalid is None and guards is not None:
        try:
            _validate_occurrence_artifact(model)
        except (KeyError, TypeError, ValueError, AttributeError):
            invalid = "CALIBRATION_INDEPENDENT_SPLIT_PROVENANCE_INVALID"
    domains = {
        (
            str(item.get("feed_version", "")),
            str(item.get("route_id", "")),
            str(item.get("service_date", "")),
        )
        for item in ranked
    }
    if invalid is None:
        try:
            if not domains or not domains.issubset(
                {tuple(item) for item in model["validated_domains"]}
            ):
                invalid = "CALIBRATION_ROUTE_DATE_DOMAIN_NOT_INDEPENDENTLY_VALIDATED"
        except (KeyError, TypeError, ValueError):
            invalid = "CALIBRATION_ARTIFACT_SCHEMA_INVALID"
    if invalid is not None:
        result["reasons"] = [invalid]
        return result
    try:
        probabilities, reason = _distribution_for(model, ranked)
        if probabilities is not None and (
            len(probabilities) != 4
            or any(not _finite(p) or not 0 <= p <= 1 for p in probabilities)
            or not math.isclose(sum(probabilities), 1.0, abs_tol=1e-9)
        ):
            raise ValueError("Invalid rank probability distribution")
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        probabilities, reason = None, "CALIBRATION_ARTIFACT_SCHEMA_INVALID"
    if probabilities is None:
        result["reasons"] = [reason]
        return result
    result.update(
        {
            "calibrated": True,
            "calibration_status": model["status"],
            "model_digest": model["model_digest"],
            "unknown_probability": probabilities[3],
            "candidate_probabilities": {
                _identity(item): probabilities[index] if index < 3 else None
                for index, item in enumerate(ranked)
            },
            "scheduled_occurrence_confidence": max(probabilities[:3]),
            "top1_top2_margin": probabilities[0] - probabilities[1]
            if len(ranked) > 1
            else None,
        }
    )
    # Credible set construction includes unknown; do not hide its probability.
    mass, indices = 0.0, []
    for index in sorted(range(4), key=lambda item: -probabilities[item]):
        indices.append(index)
        mass += probabilities[index]
        if mass >= c["continuous_calibration_credible_mass"]:
            break
    cap = min(3, max(0, int(c["continuous_calibration_output_max"])))
    if (
        3 in indices
        or probabilities[3] > c["continuous_calibration_max_unknown_probability"]
    ):
        result["reasons"] = ["UNKNOWN_OR_OUTSIDE_CANDIDATE_MASS_REQUIRES_ABSTENTION"]
    elif len(indices) > cap or any(index >= len(ranked) for index in indices):
        result["reasons"] = ["CALIBRATED_CREDIBLE_SET_EXCEEDS_OUTPUT_LIMIT"]
    elif any(not _quality(ranked[index], c) for index in indices):
        result["reasons"] = ["CREDIBLE_CANDIDATE_FAILS_ABSOLUTE_QUALITY_GATE"]
    elif guards is not None and any(
        not guards[_identity(ranked[index])]["allowed"] for index in indices
    ):
        result["reasons"] = sorted(
            {
                guards[_identity(ranked[index])]["reason"]
                for index in indices
                if not guards[_identity(ranked[index])]["allowed"]
            }
        )
    else:
        result["credible_set"] = [_identity(ranked[index]) for index in indices]
        result["status"] = (
            "MATCHED"
            if len(indices) == 1
            and probabilities[indices[0]]
            >= c["continuous_calibration_match_probability"]
            else "AMBIGUOUS"
        )
        result["reasons"] = ["INDEPENDENTLY_CALIBRATED_CREDIBLE_SET"]
    if any(item.get("boundary_uncertain") for item in ranked) and adequate_pattern:
        result["status"] = "BOUNDARY_UNCERTAIN"
        result["reasons"].append("BOUNDARY_UNCERTAINTY_REQUIRES_REVIEW")
    return result
