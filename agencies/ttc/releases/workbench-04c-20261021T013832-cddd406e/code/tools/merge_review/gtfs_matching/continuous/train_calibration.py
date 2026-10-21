"""Offline independent-label calibration, never a raw-data matching run.

No label file means an explicit unavailable report. Legacy review clicks and
matcher-generated schedule fields are not converted into independent truth.
This utility does not activate a model, change configurations or train on test
outcomes. Its output is exclusive-create and includes input content digests.
Result files may be episode arrays or persisted shadow ``{old: [...], new: [...]}``
envelopes; --old-results and --new-results select only their explicit side.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .calibration import _class, _distribution_for, fit_calibration_model
from .evaluation import (
    evaluate_shadow,
    expected_calibration_error,
    load_verified_labels,
)


def build_calibration_report(labels=None, *, old_rows=None, new_rows=None, config=None):
    """Fit train frequencies, select using validation, report test independently."""
    labels = list(labels or [])
    config = (config or {}) | {"continuous_occurrence_model_enabled": True}
    model = fit_calibration_model(labels, config)
    pairs, class_pairs, unsupported = [], [], 0
    test = [row for row in labels if row["split"] == "test"]
    if model.get("calibrated"):
        for row in test:
            probabilities, _ = _distribution_for(model, row["candidates"])
            if probabilities is None:
                unsupported += 1
                continue
            truth = _class(row)
            winner = max(range(4), key=probabilities.__getitem__)
            pairs.append((probabilities[winner], winner == truth))
            class_pairs.extend(
                (p, index == truth) for index, p in enumerate(probabilities)
            )
    return {
        "schema_version": "continuous-offline-calibration-v1",
        "status": "FITTED_NOT_ACTIVATED" if model.get("calibrated") else "UNCALIBRATED",
        "model": model,
        "heldout_test_reliability": {
            "n": len(pairs),
            "provided_test_n": len(test),
            "unsupported_n": unsupported,
            "ece": expected_calibration_error(pairs),
            "classwise_ece": expected_calibration_error(class_pairs),
            "reason": None
            if pairs
            else "NO_VALIDATED_CALIBRATOR_AND_SUPPORTED_TEST_LABELS",
            "test_used_for_fit_or_model_selection": False,
            "definition": (
                "Winner-rank including unknown; not final decision precision."
            ),
        },
        "evaluation": evaluate_shadow(old_rows or [], new_rows or [], labels, config),
        "operational_prior": {
            "trained": False,
            "reason": "OPERATIONAL_PRIOR_NOT_TRAINED_BY_RANK_CALIBRATION_UTILITY",
        },
        "activated": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--old-results", type=Path)
    parser.add_argument("--new-results", type=Path)
    parser.add_argument("--scorer-fingerprint")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    inputs = {}

    def load(path, loader, *, side=None):
        if path is None:
            return []
        before = path.read_bytes()
        rows = loader(path)
        if path.read_bytes() != before:
            raise ValueError("Input changed during offline calibration read")
        inputs[str(path.resolve())] = hashlib.sha256(before).hexdigest()
        if isinstance(rows, dict) and side is not None:
            if side not in rows or not isinstance(rows[side], list):
                raise ValueError(f"Shadow envelope requires a list-valued {side} side")
            rows = rows[side]
        if not isinstance(rows, list):
            raise ValueError(
                "Persisted labels/results must be lists of episode records"
            )
        return rows

    report = build_calibration_report(
        load(args.labels, load_verified_labels),
        old_rows=load(
            args.old_results, lambda path: json.loads(path.read_text()), side="old"
        ),
        new_rows=load(
            args.new_results, lambda path: json.loads(path.read_text()), side="new"
        ),
        config={"continuous_scorer_fingerprint": args.scorer_fingerprint},
    )
    report["input_sha256"] = inputs
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(
            report,
            stream,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        stream.write("\n")
    print(
        json.dumps(
            {"status": report["status"], "activated": False, "output": str(args.output)}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
