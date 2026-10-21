# Experiment 04c: Separating Operational Delay, Occurrence Confusion, and Retrieval

This experiment builds directly on **04**, not 04b. The scope remains 2023-11-06, route 29;
the raw observations, direction / 30-minute gap segmentation, canonical references, 100-meter projection gate, and the four decision
slices reuse the inputs already fixed by 04. The existing 03d, 04, and 04b results are not overwritten.

## Three distinct questions

1. **Operational delay prior**: how operationally plausible is this amount of early/late running? Uses an asymmetric
   routine/disruption mixture distribution on a physical seconds scale. All competing candidates share the time-of-day, route, and direction
   context derived from the observations; the prior is no longer scaled by each candidate's own headway. Without independent history, the parameters are explicitly marked as
   engineering assumptions, not calibrated operational probabilities.
2. **Local occurrence displacement**: how many confusable occurrences must be crossed to explain the observations? Within the currently observed s range,
   a common candidate class is built from geometric and ordered-stop compatibility; the preceding and following scheduled passage gaps, their source, and their confidence are recorded.
   The number of competing passage times crossed, and the distance outside the time cell bounded by the midpoints between adjacent passage times, are computed separately.
   A different exact pattern ID should not by itself produce a favorable delay denominator.
3. **High-recall retrieval**: which candidates are worth computing? The core set retains the nearest passing occurrence and its immediate preceding and following neighbors; the
   rescue set retains additional adjacent occurrences and hypotheses supported by delay-tail / vehicle-sequence evidence. Entering the rescue set
   does not exempt a candidate from the first two costs and does not imply the same prior probability as core candidates.

When local neighbors are missing, a hierarchical statistical fallback is used, and `headway_source`,
`headway_confidence`, `previous_headway_s`, and `next_headway_s` are exposed.
A value must not be replaced with 3,600 seconds merely because a narrow exact-pattern group has no adjacent occurrence.
Raw s coordinates across references cannot be subtracted directly; the geometric/progress mapping must be validated first.

The local set is conditioned on the **observed** ordered-stop constraints shared by all candidates, rather than building a private neighborhood from each candidate's own complete stop
pattern. Reliable APC can act as a hard subsequence constraint only when it also falls within the current AVL's space-time coverage;
APC outside the coverage or with low confidence is still retained as APC evidence and is not discarded for that reason.
Scheduled stop-order conflicts that cannot be distinguished by actual stop evidence should be reported as unresolved pattern ambiguity.
Post-midnight trailing trips of the previous service day in the active feed pool compete at their original absolute times;
if the caller explicitly specifies service_date, retrieval and whole-trip evaluation use the same restricted set.

## Scoring and identity guard

`continuous_occurrence_model_enabled` is the switch for the new features and is off by default to preserve the semantics of earlier experiments.
04c turns it on explicitly; it keeps 04's outer absolute delay weight of **1.0** and dynamics weight of **0.5**,
and additionally reports an independent `occurrence_displacement_cost` (default weight 1.0).
The old seven costs are still stored item by item; the new model has eight in total, with raw cost × weight = weighted contribution.
Quality gates are not relaxed to produce MATCHED while uncalibrated.

The new operational prior's default routine early/late scales are 120 / 300 seconds, disruption scales
600 / 1,800 seconds, with a disruption mixture weight of 0.05; the components are asymmetric two-sided Student-t3.
The absolute delay cost is the negative log-density difference relative to the distribution mode; these are configurable uncalibrated assumptions,
not historical regularities inferred from this day's matching results. The local displacement cost is
`crossing_weight × crossed_occurrences + cell_weight × outside_cell_s / common_local_headway_s`;
both internal weights default to 1. Missing credible occurrence-position evidence is not treated as zero cost.

The delay state's default atomic duration is 60 seconds and the minimum event interval is 15 seconds; observation gaps longer than 90 seconds
break the state link. Projection distance, GTFS endpoint timepoint attributes and interpolation, dwells, terminals,
and observation intervals enter the temporal uncertainty, and the dynamics cost is not accumulated per GPS ping.

The shared delay is still fitted once per performed-fragment/candidate pair, and local windows cannot shift on their own.
The new dynamics term is computed over non-overlapping physical-time states and incorporates the uncertainty of projection, schedule interpolation, dwells/terminals, and
observation gaps; per-observation write-back results and fragment separation are retained.
APC without real coordinates remains stop-time interval evidence only, and upstream schedule-derived fields such as AvgOTP do not enter the scoring.

A candidate that crosses at least one locally confusable occurrence cannot obtain MATCHED merely because it fits well after a shift.
The identity guard requires explicit, verifiable, independent occurrence evidence that did not enter the delay fit; ordinary APC time fit,
terminal proximity, or unverified vehicle-sequence soft scores cannot grant an automatic exemption. Diagnostic candidates can still be displayed but cannot be presented as
final confirmed occurrences. The guard diagnostics and displacement statistics are stored together with the candidate components.

## Running and verification

From `agencies/ttc`, rematch the specified **04** results, reusing the validated projections, and recompute the four modes:

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.gtfs_matching.multimode \
  --source run-20231106-20260910T030622-57e42718b0c1 \
  --occurrence-model --workers 8 \
  --label 'Experiment 04c · operational delay prior and local occurrence displacement'
```

The output is a new append-only run containing the configuration, input/code hashes, per-point delay, candidate components,
retrieval provenance, four-mode day overviews, and shadow reports. All thresholds and weights come from
`continuous/config.py::defaults()` and the modules it loads, and are written in full to the run configuration.
The frozen release can store an independent 04c profile; existing older frozen releases are not modified.

### Completed recomputation

- Input 04: `run-20231106-20260910T030622-57e42718b0c1`.
- Output 04c: `run-20231106-20260910T063603-e4bf6bc60823`.
- 306 segments × global / early / middle / late, 8 workers, batch processing took
  605.634 seconds. This time is the matching recomputation after reusing the 04 projections, not the time from the raw CSVs.
- The completion record and the 421 output SHA-256 pins passed the workbench load check.
- The scoring code for this run is stored in
  `releases/workbench-04c-20260910T065019-6915fbec`.
  Since then, the only changes were a compatibility fix in the offline evaluation for older versions' `occurrence_displacement: null`
  and adding this document to the frozen release; these changes did not rewrite or recompute this run's candidate scores.
  The final startup version is specified by `config/workbench-release.json`, and the old snapshots are retained.

Per-mode shadow reports are under `shadow/` in the output run; further independent checks and the browser acceptance
report are under `artifacts/merge-debug-20231106/experiment04c-validation/`.
Formal scheduled-occurrence results were not force-promoted to MATCHED: without calibration, statuses such as
PATTERN_ONLY are retained, and the workbench shows explicitly labelled diagnostic candidates for human review.

## Calibration limits

The current workspace has no independent trip labels that meet this experiment's requirements. The small number of selections in the old labeling UI lack
independence, verification, and episode/split binding, and cannot be converted automatically into ground truth.
A single route/day also cannot support held-out validation across routes **and** across service days.

The evaluation procedure requires route, service_date, and operating episode to be disjoint across train / validation /
test; training the operational prior, selecting hyperparameters, and the final test must not share run fragments.
Exact-trip accuracy, adjacent-occurrence confusion, retrieval recall, top-3 coverage, non-abstained precision, abstention rate, and ECE are reported
separately by headway regime.
Metrics without independent labels are null; unlabeled ranking changes, candidate counts, and runtime are descriptive results only
and cannot be claimed as improved algorithm accuracy. Probabilities remain uncalibrated, and a top-1 output is not forced.
