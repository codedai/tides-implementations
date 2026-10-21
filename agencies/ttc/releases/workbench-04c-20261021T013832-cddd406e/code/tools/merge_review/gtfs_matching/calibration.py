"""Optional probability calibration from explicitly verified, grouped labels only."""

from __future__ import annotations

import hashlib
import math

DEFAULTS = {
    "calibration_min_train": 30,
    "calibration_min_validation": 20,
    "calibration_max_ece": 0.15,
    "calibration_iterations": 600,
    "calibration_learning_rate": 0.05,
    "calibration_validation_fraction": 0.25,
}
FEATURES = ("support_score", "coverage", "margin")


def get_config(config=None):
    return DEFAULTS | (config or {})


def _sigmoid(value):
    return 1 / (1 + math.exp(-max(-40, min(40, value))))


def _predict(model, features):
    x = [
        (float(features.get(k, 0)) - model["means"][i]) / model["scales"][i]
        for i, k in enumerate(model["feature_names"])
    ]
    return _sigmoid(
        model["intercept"]
        + sum(a * b for a, b in zip(x, model["coefficients"], strict=True))
    )


def validate_calibrator(model, labeled_examples):
    rows = list(labeled_examples)
    if not rows:
        return {"status": "NO_VALIDATION_LABELS"}
    if any(str(row["group_id"]) in model.get("training_groups", []) for row in rows):
        raise ValueError("Calibration validation groups overlap training groups")
    pairs = [(_predict(model, row["features"]), int(row["label"])) for row in rows]
    brier = sum((p - y) ** 2 for p, y in pairs) / len(pairs)
    log_loss = -sum(
        y * math.log(max(p, 1e-12)) + (1 - y) * math.log(max(1 - p, 1e-12))
        for p, y in pairs
    ) / len(pairs)
    bins = []
    ece = 0.0
    for index in range(10):
        group = [(p, y) for p, y in pairs if min(9, int(p * 10)) == index]
        if group:
            confidence = sum(p for p, _ in group) / len(group)
            empirical = sum(y for _, y in group) / len(group)
            ece += len(group) / len(pairs) * abs(confidence - empirical)
            bins.append(
                {"count": len(group), "predicted": confidence, "observed": empirical}
            )
    return {
        "status": "HELD_OUT_VALIDATION",
        "n": len(rows),
        "groups": sorted({str(r["group_id"]) for r in rows}),
        "brier": brier,
        "log_loss": log_loss,
        "ece": ece,
        "reliability_bins": bins,
    }


def fit_calibrator(labeled_examples, config=None, *, labels_are_verified=False):
    c = get_config(config)
    rows = list(labeled_examples)
    uncalibrated = {
        "calibrated": False,
        "probability": None,
        "status": "UNCALIBRATED",
        "reason": "NO_VERIFIED_LABELS",
    }
    if not labels_are_verified or not rows:
        return uncalibrated
    for row in rows:
        if (
            row.get("label_source") not in {"manual_verified", "external_ground_truth"}
            or row.get("label") not in (0, 1, False, True)
            or not row.get("group_id")
        ):
            raise ValueError(
                "Calibration needs binary verified labels and independent group IDs"
            )
        if any(
            not math.isfinite(float(row.get("features", {}).get(name, 0)))
            for name in FEATURES
        ):
            raise ValueError("Calibration features must be finite")
    groups = sorted(
        {str(r["group_id"]) for r in rows},
        key=lambda s: hashlib.sha256(s.encode()).hexdigest(),
    )
    cut = max(1, round(len(groups) * c["calibration_validation_fraction"]))
    held_out = set(groups[:cut])
    train = [r for r in rows if str(r["group_id"]) not in held_out]
    validation = [r for r in rows if str(r["group_id"]) in held_out]
    if (
        len(train) < c["calibration_min_train"]
        or len(validation) < c["calibration_min_validation"]
        or len({int(r["label"]) for r in train}) < 2
        or len({int(r["label"]) for r in validation}) < 2
    ):
        return uncalibrated | {
            "reason": "INSUFFICIENT_GROUPED_TRAINING_OR_HELDOUT_LABELS",
            "training_n": len(train),
            "validation_n": len(validation),
        }
    means = [
        sum(float(r["features"].get(name, 0)) for r in train) / len(train)
        for name in FEATURES
    ]
    scales = [
        max(
            1e-6,
            math.sqrt(
                sum((float(r["features"].get(name, 0)) - mean) ** 2 for r in train)
                / len(train)
            ),
        )
        for name, mean in zip(FEATURES, means, strict=True)
    ]
    xs = [
        [
            (float(r["features"].get(name, 0)) - means[i]) / scales[i]
            for i, name in enumerate(FEATURES)
        ]
        for r in train
    ]
    beta, intercept = [0.0] * len(FEATURES), 0.0
    for _ in range(int(c["calibration_iterations"])):
        errors = [
            _sigmoid(intercept + sum(a * b for a, b in zip(x, beta, strict=True)))
            - int(row["label"])
            for x, row in zip(xs, train, strict=True)
        ]
        intercept -= c["calibration_learning_rate"] * sum(errors) / len(train)
        beta = [
            b
            - c["calibration_learning_rate"]
            * (
                sum(error * x[j] for error, x in zip(errors, xs, strict=True))
                / len(train)
                + 0.01 * b
            )
            for j, b in enumerate(beta)
        ]
    model = {
        "feature_names": list(FEATURES),
        "means": means,
        "scales": scales,
        "coefficients": beta,
        "intercept": intercept,
        "training_groups": sorted(set(groups) - held_out),
        "training_n": len(train),
        "label_provenance": sorted({r["label_source"] for r in rows}),
        "labels_verified_by_caller": True,
    }
    validation_result = validate_calibrator(model, validation)
    calibrated = validation_result["ece"] <= c["calibration_max_ece"]
    return model | {
        "calibrated": calibrated,
        "status": "CALIBRATED_ON_PROVIDED_LABELS"
        if calibrated
        else "VALIDATION_FAILED",
        "reason": None if calibrated else "HELDOUT_ECE_ABOVE_THRESHOLD",
        "validation": validation_result,
        "probability": None,
    }


def predict_probability(features, model=None):
    if (
        not model
        or not model.get("calibrated")
        or not model.get("labels_verified_by_caller")
    ):
        return None
    return _predict(model, features)
