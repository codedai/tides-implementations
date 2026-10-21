# Experiment 04b: Strengthen Absolute Delay Level, Reduce Delay Dynamics Cost

The scope remains **2023-11-06, Route 29**. The source is the complete Experiment 04 run
`run-20231106-20260910T030622-57e42718b0c1`; the new experiment is written to disk separately and does not overwrite 04.
The raw AVL/APC, canonical references, sequence projections, 30-minute joint-gap split, and the
evidence-selection scopes of global / early / middle / late are all retained.

## Results

New run: `run-20231106-20260910T041452-de4609d0825b`.
31 vehicles, 306 segments, 1,224 results across the four modes; recomputation and saving took **770.294 seconds
(about 12.8 minutes)**. The main workbench selects 04b by default; the old 04 is retained.

| Mode | Top candidate changed relative to 04 | PATTERN_ONLY | UNMATCHED | BOUNDARY_UNCERTAIN |
|---|---:|---:|---:|---:|
| Global | 16 | 299 | 7 | 0 |
| Early | 9 | 299 | 7 | 0 |
| Middle | 11 | 300 | 6 | 0 |
| Late | 4 | 293 | 7 | 6 |

These are descriptive statistics and cannot be read as an accuracy improvement. Statuses are unchanged in every mode, but the top-candidate ordering and the set passing the numeric quality
gates can change; without independent calibration, no formal scheduled candidates or fabricated probabilities are produced.
Each mode overview retains 300 top-diagnostic schedule lines, and a further 6 segments have no usable candidate.

## Parameters and threshold policy

| Configuration | 04 | 04b |
|---|---:|---:|
| `continuous_absolute_delay_weight` | 1.0 | 10.0 |
| `continuous_delay_dynamics_weight` | 0.5 | 0.1 |
| `continuous_match_cost_max` | 3.0 | 7.3 |
| `continuous_calibration_max_score` | 3.0 | 7.3 |
| `continuous_local_support_cost_max` | 3.0 | 16.5 |
| `continuous_boundary_penalty` | 0.5 | 2.75 |
| `continuous_boundary_gain_min` | 0.5 | 2.75 |
| `continuous_local_missing_cost` | implicit 4.0 | explicit 22.0 |
| `continuous_apc_support_cost_max` | borrowed the local gate 3.0 | independent, kept at 3.0 |
| `sequence_local_score_weight` | 1.0 | 30/73 ≈ 0.4109589041 |

The remaining final scoring weights are unchanged. In particular, **the prior / smooth weights inside the delay fitter remain
1.0 / 0.3**; the new 10 / 0.1 act on the final candidate cost and do not change the fitting objective of the shared delay curve.
The same candidate should therefore retain exactly the same fitted delay, raw components, and three-tier coverage.

The selection thresholds use an interpretable "weight-budget normalization", not tuning against a particular vehicle, winner, or target pass rate:

- The whole-score weight sum changes from 6.0 to 14.6, a ratio of 73/30; keeping the baseline
  `score / sum(weights) <= 0.5` cost budget gives `3 × 73/30 = 7.3`.
- The local AVL cost has only two terms, conditioned fit and absolute delay, whose weight sum changes from 2 to
  11, a ratio of 5.5; its support gate, boundary penalty/gain, and missing cost use this ratio.
- The APC support gate measures the **unweighted APC residual** and must be separated from the weighted AVL local gate; it stays at 3.
- Missing windows use an independent local missing cost of 22; whole gate + 1 = 8.3 cannot be carried over,
  otherwise missing evidence could be cheaper than a normal window. The configuration requires the missing cost to be strictly greater than the local support gate.
- The vehicle-day decoder remains an annotation-only shadow. Its input total score is multiplied by 30/73
  to keep the numeric scale relative to the unchanged transition / unknown costs; it does not promote final matches.

This is only an experimental cost-scale policy, **not statistical calibration, and it does not guarantee the same passing set as 04**.
Different cost terms have different empirical distributions, and weight sums alone cannot yield accuracy or pass probabilities.
For example, in the ideal case a fixed one-headway delay contributes 5 from the level term alone; two headways contribute 18.75,
which still exceeds the new gate of 7.3. The constraint that absolute delay places on the selection gate is indeed stronger than in 04.

The following settings are unchanged: the 100 m projection gate, minimum points/duration/travel distance, the projection / schedule /
end-to-end coverage gates, 4σ schedule support, the APC time scale, headway, retrieval top-K /
best+delta / core/rescue buffers, the 90 s continuity bound, and the probability and credible-set gates.
The coarse retrieval cost is not the new seven-term weighted total and does not scale with it.

There are currently no independent calibration labels or historical priors; the old model's fingerprint cannot be carried over. The calibration score/margin bins
remain in their untrained configuration and make no claim of completed calibration. Formal candidates and probabilities continue to follow abstention; the top three diagnostics
and the day overview's top diagnostic are retained, and passing the quality gates must not be equated with a confirmed scheduled occurrence.

## Running and reproduction

Run from the `agencies/ttc` directory:

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.gtfs_matching.multimode \
  --source run-20231106-20260910T030622-57e42718b0c1 --workers 8 \
  --continuous-absolute-delay-weight 10 \
  --continuous-delay-dynamics-weight 0.1 \
  --continuous-match-cost-max 7.3 \
  --continuous-calibration-max-score 7.3 \
  --continuous-local-support-cost-max 16.5 \
  --continuous-local-missing-cost 22 \
  --continuous-apc-support-cost-max 3 \
  --continuous-boundary-penalty 2.75 \
  --continuous-boundary-gain-min 2.75 \
  --sequence-local-score-weight 0.410958904109589 \
  --label 'Experiment 04b · Route 29 · absolute delay x10 / dynamics x0.1'
```

Running from a 04 source that already has continuous scoring enabled does not require passing `--continuous` again.
Explicit overrides recompute the local evidence, whole-segment ranking, gates, boundaries, and
shadow for all four modes, rather than only changing the total score shown in the UI. The source configuration values, overrides, and code and input-file digests are all stored.

Workbench address: <http://127.0.0.1:8767>; the newly completed experiment can be chosen in the version selector, and the old 04 is not deleted.

## Validation method

Passed **1,064 Python regression tests, 5 continuous-UI unit tests, and 7 bilingual unit tests**.
04b passed the 133 browser checks on the main workbench, and the old 04 also passed the 133 browser checks under the new code;
neither produced POST requests, external network requests, or browser errors. Reports are in
`artifacts/merge-debug-20231106/experiment04b-validation/`.
The main page's default run and configuration were verified over HTTP; candidate cards show raw costs, the actual 10 / 0.1 weights, and contributions.
The main notes database SHA-256 before and after remains
`00bbe13be62749be4909b043c5438e0afff567e8fb8212a295e0984e94535e00`.

New general synthetic tests cover: independent APC/local gates, missing-cost scale, the boundary model, reweighting of final weights
without changing the delay fit, raw costs, and coverage, continued abstention without calibration, early rejection of invalid configuration,
single-/multi-worker four-mode append-only replay, and source configuration and code provenance.

The full audit uses `tools/merge_review/tests/audit_continuous_run.py --reweight-only`,
verifying candidate by candidate that raw components, fitted processes, APC support, coverage, schedule lines, projection rejection records, candidate recall sets,
and four-mode evidence selection are unchanged, and reporting top-candidate changes and quality-gate descriptive statistics.
For candidates with no valid delay fit at all, the conditioned-fit missing sentinel is allowed to change with the total-score gate;
this is not a change in the residual of valid observations and cannot count as a successful match.

**Full audit PASS**: 306 segments, 1,224 mode results, 36,170 candidate scores/fits,
421 source-file pins, and 421 new-run pins all passed. The 92,143 AVL and 13,862 APC records
and their upstream fields are unchanged; candidate sets, decision segments, fitted processes, and coverage are all unchanged.
Report: `artifacts/merge-debug-20231106/experiment04b-validation/full-run-audit.json`.

| Mode | 04 top candidates passing the numeric total-score gate | 04b top candidates passing the numeric total-score gate |
|---|---:|---:|
| Global | 294 / 300 | 299 / 300 |
| Early | 299 / 300 | 300 / 300 |
| Middle | 300 / 300 | 300 / 300 |
| Late | 284 / 300 | 299 / 300 |

The denominator is the 300 segments per mode that have numeric candidates, not all 306 segments.
This table checks total cost only; it does not indicate passing the other coverage/credible-set gates, let alone real matching accuracy.
