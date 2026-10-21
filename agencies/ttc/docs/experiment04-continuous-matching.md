# Experiment 04: Continuous Time-Space Matching

The follow-up weight comparison experiment is documented in [Experiment 04b](experiment04b-delay-weights.md): outer absolute delay weight 10,
dynamics weight 0.1, with matching adjustments to the cost-scale thresholds; the original 04 parameters and results recorded on this page are unchanged.

This run covers only **2023-11-06, Route 29**. The new core does not branch on vehicle, route ID, date, time of day, or an expected winner; the workbench data selector remains an experimental tool for this one day and this one route, not a deployed agency-wide ingestion service.

Experiment 04 takes the complete 03d run `run-20231106-20260909T183705-d67ffa30723d` as input, reuses its canonical references, sequence projections, 30-minute joint-gap split, and source records unchanged, and recomputes global / early / middle / late. This is a controlled-input scorer comparison, not a re-cleaning of the data; it does not rewrite 03d, the source CSV/parquet files, or human reviews.

## Completed run

Run ID: `run-20231106-20260910T030622-57e42718b0c1`. 306 segments, 31 vehicles, 1,224 results across the four modes; recomputation and saving with 8 workers took **693.743 s (about 11.6 minutes)**. Workbench: <http://127.0.0.1:8767>, with Experiment 04 selected by default and 03d still selectable.

| Mode | PATTERN_ONLY | UNMATCHED | BOUNDARY_UNCERTAIN | Top-candidate ranking changes relative to 03d |
|---|---:|---:|---:|---:|
| Global | 299 | 7 | 0 | 72 |
| Early | 299 | 7 | 0 | 48 |
| Middle | 300 | 6 | 0 | 35 |
| Late | 293 | 7 | 6 | 70 |

These are descriptive statistics, not accuracy improvements. Each of the four mode overviews has 300 top-diagnostic schedule lines, and a further 6 segments have no usable diagnostic candidate; there are no calibrated formal scheduled-occurrence candidates. PATTERN_ONLY and "numeric candidates exist but did not pass calibration" can coexist.

Acceptance: 950 Python regression tests, 5 continuous-UI unit tests, and 7 bilingual unit tests; all 133 browser checks for the new 04 run and all 103 browser checks for the old 03d run passed. Browser reports and screenshots are in `artifacts/merge-debug-20231106/experiment04-validation/`. The human review database SHA-256 remains `00bbe13be62749be4909b043c5438e0afff567e8fb8212a295e0984e94535e00` after starting the new workbench.

The full on-disk audit also passed: 1,224 mode results and 36,170 candidate scores each satisfy the cost-sum, original-clock, and three-tier coverage identities; all 409 source pins and 421 new-result pins are consistent. The 92,143 AVL and 13,862 APC source observations and their upstream fields are unchanged. The report is `artifacts/merge-debug-20231106/experiment04-validation/full-run-audit.json`. The complete candidates and intermediate diagnostics occupy roughly 3.1 GiB of compressed case files; retaining high-recall intermediate results has a clear storage cost.

## Architecture and data contracts

| Stage | Implementation | Contract |
|---|---|---|
| Multi-route namespace | `continuous/references.py` | Grouped by feed_version + route_id, with direction + family + reference inside; s values on different references are never subtracted |
| family / canonical validation | `canonical.py` | Geometric coverage, branches, ordered stops, low-ambiguity monotone embedding; if the longest shape fails, multiple references are retained; the new approach supports east-west orientations |
| Sequence projection | `projection.py` | Evaluates physical distance, heading, time, progress, and feasible speed on all permitted references; direction is a prior, not a filter |
| off-reference runs | `off_reference.py` | 100 m default gate; retains original rows, alternative references, and rejection reasons; isolated outliers may be skipped, sustained runs / GPS gaps split fragments |
| APC normalization | `continuous/apc.py` | canonical stop ID, s, service interval, boardings/alightings, confidence; no GPS points are fabricated when coordinates are missing; derived schedule information such as AvgOTP, vendor assignment, and delayed is ignored |
| Continuous schedule lines | `continuous/schedule.py` | Native stop_times; departure → next-stop arrival is a continuous motion segment; dwell is a time interval at fixed s; passage times are found by binary search |
| Candidate retrieval | `continuous/retrieval.py` | Pattern/reference compatibility first, then occurrence retrieval by scheduled passage time at the observed positions; per window per pattern retains top-K or best + delta, and takes the union |
| Windows | `continuous/windows.py` | Non-overlapping 60 s atomic windows, 300/600 s contexts, APC/terminal/GPS/direction/fragment event windows; total observation weight across scales is 1 |
| Shared delay | `continuous/delay.py` | One IRLS robust state model over the whole candidate segment; temporal-support weighting, physical rate-of-change constraints; per-window time-shift resets are prohibited |
| Whole-segment integration | `continuous/integration.py` | Rescores all retrieved candidates; seven cost components, coverage, local robust statistics, shared process, and a single change-point diagnostic |
| Vehicle day | `continuous/sequence.py` | Optional soft-constraint DP with unknown / extra, split, occurrence, block, and time continuity; in this experiment it is a shadow annotation only and does not promote final conclusions |
| Calibration | `continuous/calibration.py` | Independent episode labels, route/date/episode splits, held-out checks, explicit unknown mass, at most three credible candidates |
| Serialization / comparison | `continuous/serialization.py`, `continuous/evaluation.py`, `continuous/shadow.py` | All components and intermediate evidence are written to disk; JSON round-trip, cost-sum, and three-tier coverage identity checks; evaluation can be redone without re-ingesting |

Input times to the mathematical core must lie on a single, explicitly declared time axis. `clock.py` provides the GTFS service-day noon-minus-12h time definition, times beyond 24:00, and DST ambiguous / nonexistent observation checks. This pinned replay **continues to use the 03d naive local wall-clock axis** and does not silently reinterpret existing numbers as UTC. New ingestion across a DST transition must explicitly convert both sides' times and handle the fold; the wall-clock numbers from this experiment cannot be reused directly. Relevant specification: [GTFS Schedule Reference](https://gtfs.org/documentation/schedule/reference/).

## Scoring

At the same canonical s as each valid real AVL point, the scheduled interval is looked up and the raw residual interval `[observed_t - departure_t, observed_t - arrival_t]` is stored. When the delay state falls inside that residual interval, the conditioned interval-distance loss is 0; otherwise the distance to the nearest endpoint is computed. The raw residual is not forced to a single 0. GTFS non-timepoint / motion-segment interpolation uses a larger temporal uncertainty.

One performed segment / candidate pair fits exactly one shared delay process. The state fit uses temporal support × quality; raw point counts cannot stand in for independent duration. Isolated rejected points do not start a separate delay model; across real sustained off-reference runs / long gaps, no artificial adjacent-state link is imposed.

| Persisted component | Default outer weight | Meaning |
|---|---:|---|
| `absolute_delay_level_cost` | 1.0 | Historical prior from a validated source; without history, a robust cost of delay normalized by local headway. Large delays are allowed but are not a free shift |
| `conditioned_fit_cost` | 1.0 | Robust residual after removing the shared delay process, normalized by temporal uncertainty |
| `delay_dynamics_cost` | 0.5 | Physical plausibility of delay changes over real time and progress |
| `APC_stop_time_cost` | 0.5 | Compatibility between APC service intervals and the actual served-stop intervals corrected by the same shared delay |
| `projection_and_coverage_cost` | 2.0 | cross-track residual + proportion of lost projected duration + proportion of lost schedule alignment |
| `pattern_progress_cost` | 1.0 | heading, explicit direction prior, backward progress, unserved stops; a single confident key-stop contradiction has a separate term that is not diluted by many mainline observations |
| `vehicle_sequence_cost` | 0.0 | Reserved for validated joint decisions; 0 in this run, and the actual sequence contributions are stored separately in the shadow without rewriting local probabilities |

The total cost is the sum of the seven weighted contributions; lower is better, and it is **not a probability**. Raw components, weights, contributions, pattern sub-terms, per-point passage intervals, delay/uncertainty, and local results are all stored. `support_score = exp(-cost)` is a display scale only and is likewise not a calibrated probability.

Windows do not vote, nor does each window level the schedule on its own. The whole-segment score uses the candidate's shared delay fit; in addition, the atomic weighted mean / median, trimmed mean, p90, number of consecutive supporting windows, informative coverage, APC support, and delay median/MAD are reported. Context windows are used for retrieval and diagnostics and are not added to the whole-segment cost a second time. A change-point is a statistical hypothesis; it is flagged BOUNDARY_UNCERTAIN only with corroborating nearby terminal/gap/direction/reference events, and is not a confirmed boundary.

## Coverage and thresholds

All three tiers report the number of observations, plus the number of intervals between raw adjacent observations and their real duration:

- projection: valid projected / all spatial;
- schedule alignment: schedule-supported valid / valid projected;
- end-to-end: schedule-supported valid / all spatial.

Each tier has an explicit numerator, denominator, and ratio. For each unit, `projection × schedule = end_to_end`. This is the **spatial evidence coverage of the AVL trajectory**; APC with real coordinates passes a separate spatial gate and still serves as stop-time evidence, and APC without coordinates is still retained; neither enters the AVL trajectory denominator. Long gaps remain in the time denominator of the all-spatial intervals but do not produce reliable links. A single-point failure affects only that point and its adjacent edges and does not zero out the whole segment. The legacy `schedule_aligned_*_ratio` aliases kept for backward compatibility are **end-to-end**; prefer reading the new `coverage_metrics.schedule`.

Main default thresholds:

| Setting | Default |
|---|---:|
| projection gate (upstream) | 100 m |
| Minimum valid points / valid duration / valid travel distance | 3 / 20 s / 50 m |
| Minimum valid projection quality | 0.2 |
| Longest ordinary gap for continuous coverage/state | 90 s |
| Minimum projection / schedule / end-to-end duration coverage | 0.35 / 0.35 / 0.25 |
| Absolute upper bound on total cost | 3.0 |
| Single-point schedule-support residual | at most 4 × temporal uncertainty |
| Core retrieval buffer | 2 × local headway |
| rescue buffer | max(core, 4 × headway, 3600 s) + clock uncertainty + gap rescue |
| gap rescue increment | 600 s |
| Retrieval per-pattern top-K / best + delta | 12 / 1.0 |
| headway default / minimum / maximum when there is no adjacent occurrence | 600 / 60 / 3600 s |
| APC time cost scale | 180 s |
| heading cost scale | 45° |
| Direction fallback cost | 1.0 |
| Backward-progress scale | 100 m |
| delay IRLS prior / smooth coefficients | 1.0 / 0.3 |
| Maximum delay growth / recovery rate | 0.95 / 3.0 seconds per second |
| Extra weight for key-stop contradictions | 1.0 |

All defaults are authoritative in `continuous/config.py::defaults()` and the module `DEFAULTS` it loads, and are written in full to each run's `config.json`; seconds, meters, degrees, and dimensionless costs are indicated in the parameter names. The web layer permits bounded numeric/boolean configuration; models, prior dictionaries, and paths cannot be injected from requests. The legacy DTW `score_*` weights take effect only when the continuous flag is off. Reference array indices and the temporary GTFS resampling step do not enter the new production cost.

Adjacent delay states are further constrained by a progress-dependent change bound of `0.95 × dt + 0.5 × abs(ds)`; 3 s/s is not an unconditionally permitted recovery rate. The current terminal event detector checks the vicinity of the reference start (s ≤ 200 m); the end terminal and complex terminal sets are not yet configured separately, so it must not be treated as a complete terminal detector.

NO_EVIDENCE means there is not enough reliable data; NO_CANDIDATE means there is enough reliable data but no candidate passed the absolute quality checks. The two are explicitly distinguished in `evidence_status`. Neither is a reason to force-select the nearest occurrence.

## Calibration boundary

There are currently no independent route/date held-out labels and no validated historical delay prior, so this experiment does not fabricate candidate probabilities, accuracy, or calibration error. When shape evidence is sufficient the result may be PATTERN_ONLY; schedule candidates are still shown among the workbench's top three diagnostics, the day overview adopts the top diagnostic under the existing rules, and this is explicitly not a confirmed match.

The calibration module includes unknown/outside-top3 probability, credible-quality gating, scorer feature/code/weight fingerprint, model digest, and feed/route/date validation-domain checks. Unknown new domains do not automatically inherit old probabilities. Allowing large real delays does not mean exact-trip accuracy under large delays has been validated.

The vehicle sequence shadow uses soft block/time/delay continuity; when there are no coordinates or no independent vehicle-assignment terminal/substitution fields, the corresponding constraints do not fabricate evidence. It is currently annotation-only and has not yet become a calibrated final decision layer. Canonical progress for repeated stops / loops must already be unrolled; ambiguous passages are explicitly rejected rather than picking one visit via an independent nearest point or the legacy discrete DTW. This is a conservative abstention, not a claim that all real-world loop data has been solved.

## Reproduction and shadow evaluation

From the `agencies/ttc` directory:

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python -m tools.merge_review.gtfs_matching.multimode --source run-20231106-20260909T183705-d67ffa30723d --continuous --workers 8 --label 'Experiment 04 · continuous time-space scoring'
```

Legacy mode omits `--continuous`; the existing 03d source is not rewritten. The new run stores `shadow/{global,early,middle,late}-episodes.json`, `-report.json`, and `-vehicle-sequence.json`. The report separates descriptive ranking/status changes from independent-label metrics, stratified by route, headway, shape structure, and data quality. With only one route/date here, no claim of validated cross-route generalization can be made.

Once independent labels are available, the stored scores can be re-evaluated without re-ingesting:

```sh
uv run --offline --no-sync python -m tools.merge_review.gtfs_matching.continuous.shadow --episodes PATH_TO_RUN/shadow/global-episodes.json --labels verified-labels.json --output new-independent-report.json
```

Labels require `case_id, feed_version, route_id, service_date, episode_id, split, verified=true, independent=true, label_source`, plus either `true_trip_id` or `no_scheduled_trip=true`. `label_source` may only be `manual_verified / external_ground_truth / independent_validated`; train, validation, and test must not share a route, date, or operating episode. Only test labels enter the final accuracy report. Optional `true_pattern_id / adjacent_trip_ids / true_delay_s / boundary_times_s` support the corresponding metrics.

The report includes retrieval recall, pattern accuracy, exact trip accuracy, adjacent-trip confusion, diagnostic/reported top3 coverage, non-abstained precision, abstention, ECE, delay MAE, boundary precision/recall/F1, and runtime; when labels or required attributes are missing the value is null with a stated reason. Where the old 03d run did not store per-segment independent runtimes, no old runtime is fabricated; 04 additionally records the full replay wall-clock time.

## Tests and acceptance

The new mathematics, integration, calibration, sequence, serialization, and UI tests run together with the existing canonical/projection/off-reference tests. They cover adjacent same-shape occurrences, high frequency, long delays, early running, dwells, sparse AVL, GPS gaps, isolated/sustained off-reference, branches/short-turns, nearby opposite-direction trips, repeated stops, conservative abstention on loops/retraces, feed/date isolation, >24:00, DST, and four-mode isolation.

General invariance tests change the temporary GTFS 5/10/15/20 s resampling setting, add collinear shape vertices, and vary AVL sampling density. They check candidate ranking, status, coverage, and costs within tolerance, rather than writing rules for a particular historical winner. 9042 serves only as a read-only historical regression, not as a label or tuning target.

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python -m pytest tests tools --ignore=tools/labeling_ui -q --tb=short
node tools/merge_review/tests/test_continuous_ui.mjs
```

HTTP/browser tests require local socket permissions; `tools/labeling_ui` is an older standalone app and, because of its dependency/module conflicts, is not part of this regression set.
