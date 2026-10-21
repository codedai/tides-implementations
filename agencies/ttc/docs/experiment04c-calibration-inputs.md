# Experiment 04c: Independent Calibration Input Contract

There are currently no real independent labels that meet the requirements below, and no trained operational prior. This document describes only the inputs;
the algorithm and experimental results are in the [04c main write-up](experiment04c-occurrence-plausibility.md).

## 1. Labels for candidate-ranking probability calibration

`continuous/train_calibration.py` accepts a JSON array, JSONL, or CSV.
Each record represents the representative case of one independent operating episode; multiple split fragments of the same episode
cannot serve as multiple independent calibration samples. Ground-truth labels must come from independent verification and must not be generated automatically from the current matcher's top result,
clicks in the old workbench, AvgOTP, or an existing upstream schedule assignment.

Strict mode requires:

- Identity: `feed_version`, `route_id`, `service_date`, `case_id`, `episode_id`;
  `(feed_version, route_id, service_date, case_id)` must not repeat.
- Evidence: `label_id` and `evidence_id` are non-empty strings; `evidence_sha256` is the
  64-character lowercase hexadecimal SHA-256 of the source evidence; `verified: true`, `independent: true`.
  `label_source` may only be `manual_verified`, `external_ground_truth`, or
  `independent_validated`. `schedule_derived` and `uses_upstream_schedule_assignment`
  must not be true; writing `false` explicitly is recommended. Declarations and hashes do not substitute for human verification or prove the authenticity of the source.
- Ground truth, one of two: a non-empty `true_trip_id`, or `no_scheduled_trip: true`.
- Splits: `split` is `train`, `validation`, or `test`, and the full dataset must contain all three.
  **The route_id, service_date, and episode_id dimensions must each be disjoint across splits**,
  not merely the combined key. Consequently, 2023-11-06 / route 29 alone cannot form a qualifying held-out set.
- Model inputs: a non-empty `candidates` list with a stable, unique `candidate_id` (`trip_id`
  may substitute), `trip_id`, and `score`; the complete set of scored candidates should be retained, not only those that passed the gates.
  The calibrator sorts by score and requires valid finite numeric candidate features.
  Each label's `scorer_fingerprint` must agree with the CLI argument and the scorer version being evaluated.
  The CLI **does not** automatically fill shadow candidates into the label's `candidates` field.

The following is **a single synthetic format example only; it is not a real label, is insufficient for training, and must not be activated**; the fictitious
`true` declarations, IDs, scores, and hashes illustrate the format only:

```json
[
  {
    "feed_version": "SYNTHETIC-feed", "route_id": "SYNTHETIC-train-route",
    "service_date": "2000-01-01", "case_id": "SYNTHETIC-case",
    "episode_id": "SYNTHETIC-train-episode", "split": "train",
    "label_id": "SYNTHETIC-label", "evidence_id": "SYNTHETIC-evidence",
    "evidence_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "verified": true, "independent": true, "label_source": "manual_verified",
    "schedule_derived": false, "uses_upstream_schedule_assignment": false,
    "true_trip_id": "SYNTHETIC-trip-1", "scorer_fingerprint": "SYNTHETIC-ONLY",
    "candidates": [
      {"candidate_id": "SYNTHETIC-candidate-1", "trip_id": "SYNTHETIC-trip-1", "score": 0.2},
      {"candidate_id": "SYNTHETIC-candidate-2", "trip_id": "SYNTHETIC-trip-2", "score": 1.3}
    ]
  }
]
```

Defaults require at least 60 train and 40 validation episodes; each enabled feature bin must have at least 15 samples in each of the two,
and test must be non-empty. Meeting the counts does not guarantee passing calibration; the held-out reliability
check must also pass, and test does not participate in fitting or model selection. The model's domain of applicability remains limited by the scorer fingerprint, the feature support domain,
and the independently validated feed / route / service_date.

Optional independent ground-truth fields: `true_pattern_id`, `true_delay_s`, `adjacent_trip_ids`,
`boundary_times_s`, and `operating_regime`, each supporting the corresponding evaluation. Boundary times must use the same
time axis as the output; without an independent regime label, do not treat `inferred_delay_regime` as ground truth.
In CSV, boolean fields use the literals `true` / `false` and list fields use JSON strings; JSON is recommended.

## 2. Actual offline command and output

Run from `agencies/ttc`. Both `--old-results` / `--new-results` accept an episode array
and also an existing `{ "old": [...], "new": [...], ... }` shadow envelope, reading the corresponding side from each.
The same file may be passed to both arguments. The output directory must exist, and the output file must be a new file.

```sh
CALIBRATION_SHADOW='artifacts/merge-debug-20231106/runs/run-20231106-20260910T063603-e4bf6bc60823/shadow/global-episodes.json'
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.gtfs_matching.continuous.train_calibration \
  --old-results "$CALIBRATION_SHADOW" --new-results "$CALIBRATION_SHADOW" \
  --output artifacts/merge-debug-20231106/experiment04c-validation/calibration-availability.json
```

This command provides no labels, so the expected result is `UNCALIBRATED`, not a successful training run. Once qualifying real labels are obtained,
add `--labels /path/to/verified-labels.json` and
`--scorer-fingerprint 'fingerprint consistent with the labels and the actual scoring results'` to the same command.
The four decision modes should each use their corresponding shadow file, aligned with the candidate/scoring fingerprint in the labels.

The tool records the content hashes of the input files; even when a result passes it is only `FITTED_NOT_ACTIVATED`,
`activated` is always `false`, and the workbench configuration is not modified nor is matching rerun.
Without independent labels, the required ground-truth fields, a support domain, or a valid denominator, the corresponding metric is `null` with an availability reason;
`null` does not mean 0. Ranking changes, candidate counts, elapsed time, and abstention descriptions can be computed, but they are not accuracy improvements.
Without a valid calibrator, candidate probabilities remain uncalibrated and cannot be used to claim a confirmed scheduled occurrence.

## 3. The operational delay prior is a separate artifact

The CLI above **does not train the operational prior**; its output is fixed to note
`OPERATIONAL_PRIOR_NOT_TRAINED_BY_RANK_CALIBRATION_UTILITY`.
`operational_prior.py` consumes only an externally trained, independently validated
`continuous_operational_prior_artifact` **JSON object**, not a file path.

The artifact must include `schema_version: operational-delay-prior-v1`,
`independently_validated: true`, a non-empty `validation_artifact` reference, complete
`training_provenance`, and non-empty `records`. Provenance requirements:

- `independent_verified: true`; `split_policy` must be exactly
  `route_id AND service_date AND episode_id disjoint`.
- `labels_sha256` is the canonical content digest of the label set; use the
  `training_provenance` returned by `evaluation.validate_independent_labels(..., require_complete_splits=True,
  require_provenance=True)`, and do not mix in the CLI's raw file hashes.
- `split_counts` is a positive integer for all three splits; `split_groups` contains, under each split, non-empty,
  duplicate-free string arrays of `route_id` / `service_date` / `episode_id`, each array no longer than
  the corresponding sample count, with the three dimensions each disjoint across splits.

The following is a **synthetic artifact format example, not a training product; it must not be copied as real history**:

```json
{
  "schema_version": "operational-delay-prior-v1",
  "independently_validated": true,
  "validation_artifact": "SYNTHETIC-ONLY-NOT-A-VALIDATION-REPORT",
  "training_provenance": {
    "independent_verified": true,
    "labels_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "split_policy": "route_id AND service_date AND episode_id disjoint",
    "split_counts": {"train": 1, "validation": 1, "test": 1},
    "split_groups": {
      "train": {"route_id": ["SYNTHETIC-A"], "service_date": ["2000-01-01"], "episode_id": ["SYNTHETIC-A"]},
      "validation": {"route_id": ["SYNTHETIC-B"], "service_date": ["2000-01-02"], "episode_id": ["SYNTHETIC-B"]},
      "test": {"route_id": ["SYNTHETIC-C"], "service_date": ["2000-01-03"], "episode_id": ["SYNTHETIC-C"]}
    }
  },
  "records": [{
    "record_id": "SYNTHETIC-global", "context": {}, "sample_count": 1,
    "centre_s": 0, "routine_early_scale_s": 120, "routine_late_scale_s": 300,
    "disruption_early_scale_s": 600, "disruption_late_scale_s": 1800,
    "disruption_probability": 0.05
  }]
}
```

Each record's `sample_count` must be a positive integer, `centre_s` a finite number of seconds, and the four scales positive seconds;
each disruption scale must be no smaller than the routine scale on the same side, and the mixture probability strictly between 0 and 1. Every record must provide all parameters.
`context` may only be a hierarchical prefix: `{}` → `feed_version` → plus `route_id` → plus `direction`
→ plus `observed_time_bucket`; the same context must not repeat, and the most specific matching record is selected.
Candidate trip IDs and candidate scheduled times must not enter the context.

The API accepts integer hour buckets 0–47; the current 04c workbench generates **0–23** from the local wall-clock hour of the joint observations,
so an externally trained artifact must use the same convention and must not write 1 a.m. as 25 on its own.
When no matching history exists, engineering parameters explicitly labelled `uncalibrated_operational_defaults` are used,
and the default prior tails are not described by the retriever as a validated empirical tail.
