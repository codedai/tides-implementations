# Experiment 03: Route 29 layered GTFS candidate review

Latest version: `run-20231106-20260909T183705-d67ffa30723d`.
Workbench: <http://127.0.0.1:8767>; select "Experiment 03d · Route 29 · Stronger time-shift penalty (weight 1.0)". The label persisted in that run's `run.json` was recorded in Chinese at run time; the English label here is its translation.

## Experiment 03d: stronger absolute time-shift penalty

Experiment 03c `run-20231106-20260909T170835-6cb6e4feb48a` is the immutable source; only `score_delay_prior_weight` is raised from **0.05 to 1.0**. The absolute time-shift term of the final score is `weight × abs(estimated_delay_s) / 3600`: the penalty for a 15-minute offset rises from 0.0125 to 0.25, and early and late offsets are still treated symmetrically. The historical default and the configurations and results of completed experiments are not overwritten.

The same parameter also participates in local delay-state selection, whole-trip aggregation and the boundary model, so this is not simply adding a number to the old total score: all four modes re-run retrieval, refit the shared delay, recompute local / whole-segment alignment and make the final decision again. Raw APC / AVL, segmentation, canonical references and projection are reused unchanged; the scope is still 2023-11-06, Route 29, 31 vehicles, 306 segments, 92,143 AVL and 13,862 APC. 8 local processes took 653.945 seconds. The new run explicitly records that the global result was recomputed, so experiment 03c's "global result reused as-is" audit condition cannot be applied to it.

The cost threshold 3.0, projection-distance gate 100 m, coverage threshold 35% and ambiguity support margin 0.1 are all unchanged. A higher weight may reduce the number of candidates passing the cost gate; the support score is still `exp(-cost)`, which is neither a probability nor a basis for inferring an accuracy improvement from status changes.

| Decision mode | MATCHED | AMBIGUOUS | PATTERN_ONLY | UNMATCHED | BOUNDARY_UNCERTAIN |
| --- | ---: | ---: | ---: | ---: | ---: |
| Global | 0 | 36 | 270 | 0 | 0 |
| Early | 1 | 45 | 257 | 3 | 0 |
| Middle | 13 | 65 | 224 | 4 | 0 |
| Late | 0 | 34 | 263 | 3 | 6 |

For 9021 / `direction:9021:0000`, the global top candidate changes from `46413320` to `46413314`:

| New rank | GTFS trip | Cost | Model time-shift median |
| --- | --- | ---: | ---: |
| 1 | 46413314 | 2.3906628381 | +90 s |
| 2 | 46413320 | 2.5279463481 | −1530 s |
| 3 | 46413319 | 2.9124250834 | −570 s |

This case remains AMBIGUOUS, with a support-score difference of 0.0117461881. The early-mode top candidate is still `46413320`; the middle- and late-mode top candidates are still `46413314`; no further parameter was changed to make the rankings of the four modes agree.

The single-candidate overlays of the four modes' day overviews are: global 180 passing the gates / 120 fallback / 6 segments with no candidate; early 230 / 70 / 6; middle 280 / 20 / 6; late 154 / 145 / 7. All four overviews retain all 306 segments, and the schedule curves still use the raw GTFS times. Candidate cards now show the actual weight and its final time-shift penalty, and the parameter panel explains the unit and the recomputation scope.

Verification: 823 relevant Python regression tests passed (`tests tools --ignore=tools/labeling_ui`); the new run passed 99 browser checks and the original 03c compatibility check passed 95, all with no POSTs, external requests or browser errors. The browser report for the new run is `artifacts/merge-debug-20231106/time-weight-browser/experiment03d/browser-modes-results.json`. The formal notes database SHA256 remains `00bbe13be62749be4909b043c5438e0afff567e8fb8212a295e0984e94535e00`. The standalone legacy `tools/labeling_ui` app is outside the scope of this change; its tests have a same-name test-package collection conflict, and when run separately they still fail to collect because numpy / streamlit are missing, so they are not counted in the pass counts above, and no dependencies were installed or legacy app modified for this.

The independent full audit passed with zero violations: it verified 409 new output hashes, 412 source pins, 65 unchanged raw / GTFS files, and 306 cases, 1,224 mode results and 1,224 overview records. The locally weighted statistics, final scoring formula, support scores and gating of the 8,993 aggregate candidates were independently re-checked; all DTW distances were not recomputed, so this is not an accuracy validation by a second matcher. All projection / APC / segment boundaries / four-mode selections are consistent with 03c, and the only change in the run configuration is the time-shift weight. The report is `artifacts/merge-debug-20231106/time-weight-audit/persisted-audit.json`.

Compared with 03c, the number of segments whose first formal candidate changed in global / early / middle / late is 50 / 21 / 23 / 25 (including candidates appearing or disappearing); this is the actual full recomputation result, not a simple score-sensitivity estimate with the old fit held fixed. 8767 has been set to the new version by default; formal read-only GETs verified the four-mode 9021 rankings and the four 306-segment day overviews, and the formal notes database hash is still unchanged.

Experiment 03c and earlier results are retained below.

## Experiment 03c: partial decisions and day overview

Only 2023-11-06, Route 29 is processed, keeping experiment 03b's 306 segments. 8 local processes completed the three additional modes and the four day overviews in 317.336 seconds. Raw APC / AVL, segment boundaries, the full projection and the global matching result remain as they were; the new results are appended to an independent run, and historical experiments are not overwritten.

"Global" here means the whole-segment decision for each segment, not a joint full-day vehicle optimization. Early / middle / late split the cumulative valid forward distance of successful projections into approximate thirds: OFF_REFERENCE, low-quality points, GPS gaps and jumps between different reference axes are not accumulated, and backward jitter does not accumulate distance repeatedly. Cut points fall on real AVL times; no trajectory points are interpolated. Rejected points within the selected time range still enter the coverage-loss denominator; the other two parts do not participate in scoring but remain in the charts as light-colored context.

Each part independently retrieves from the full GTFS pool, constructs windows, scores, fits a shared delay and applies whole-segment (currently selected part) gating. It does not reuse the global candidate shortlist or the global delay. APC time intervals are clipped only in the derived evidence, with the original interval, clipping endpoints and source ID saved; raw fields and boarding / alighting counts are not modified, and passenger counts are not summed across modes.

| Decision mode | MATCHED | AMBIGUOUS | PATTERN_ONLY | UNMATCHED | BOUNDARY_UNCERTAIN |
| --- | ---: | ---: | ---: | ---: | ---: |
| Global | 1 | 18 | 287 | 0 | 0 |
| Early | 5 | 23 | 275 | 3 | 0 |
| Middle | 8 | 26 | 268 | 4 | 0 |
| Late | 1 | 15 | 281 | 3 | 6 |

The above is a distribution of algorithm statuses, not accuracy; partial evidence yielding MATCHED more readily does not mean it is more correct. Probability remains null. The three segments with no positive valid projected distance are not forced into thirds; APC-only records are retained, and their partial modes have NO_EVIDENCE selections.

The top of the workbench offers the four modes and a "Trip review / Day overview" toggle. The overview shows all 306 segments of the 31 vehicles, faceted by NORTH / SOUTH and by independent canonical reference, with gaps left unconnected and APC drawn as stop intervals. Each segment overlays only one GTFS candidate: preferentially the lowest-cost candidate that passes all candidate gates, otherwise the first scored diagnostic candidate that genuinely failed the gates, marked as fallback with a distinct style; when there is no scored candidate, none is fabricated. Scheduled times are not shifted by the estimated delay.

The overview provides full-day and selectable windows with horizontal panning; clicking a trajectory / segment card opens trip review in the same mode. Partial modes that a historical version did not compute cannot be selected. Switching modes triggers no recomputation and submits no notes; existing notes are still stored per run / segment and are not cleared by mode switches.

Note: this is an **offline partial-evidence decision**; the shared sequence projection used the complete segment, so it is not a real-time early prediction free of future information. Per-run / per-mode intermediate evidence, partition provenance, APC clipping and all candidates are saved in the case artifacts, and the four `overview/*.json` files are also included in the output hashes.

Example `direction:9028:0004`: total valid distance about 13,647.97 m; the internal valid distances of the three parts are about 4,537.53 / 4,444.39 / 4,473.08 m, the difference arising from not fabricating connections across cut points. The formal candidate counts in global / early / middle / late are 0 / 3 / 3 / 0, but all four modes are PATTERN_ONLY, so "three cards appear" must not be read as three confirmed trips.

The single-candidate day displays are: global 189 passing the gates / 111 fallback / 6 segments with no candidate; early 234 / 66 / 6; middle 281 / 19 / 6; late 159 / 140 / 7. These differ from the MATCHED counts: a candidate passing the evidence and cost gates does not mean that other similar trips have been ruled out. None of the four modes omitted a primary projected point that met the plotting quality requirements.

989 Python regression tests passed; the final new-feature browser check passed 91, the legacy GTFS check 99 and the gap check 78 (268 in total), with zero POSTs, external requests or browser errors. The browser report is `artifacts/merge-debug-20231106/partial-modes-browser/verified-final/browser-modes-results.json`, and the same directory holds the day-overview and middle-mode decision screenshots. The formal notes database's file hash and its reviews / review_events counts are unchanged. 8767 was restarted with the new version as the default run, and the four day overviews were verified with read-only GETs.

The existing independent audit verified the global 306 cases, 58,490 windows and 3,735 candidate evaluations with zero violations / unknown schemas. The new independent audit verified 1,224 mode results and 1,224 overview records, 409 new output hashes, 408 source pins and 65 unchanged raw / GTFS files; there were no data, scope, APC clipping, ordering, candidate gating or cross-gap connection violations.

Audit traceability note: the original report `artifacts/merge-debug-20231106/partial-modes-audit/persisted-audit.json` retains `validation_ok=false`; its only flag is that the current `overview.py` differs from the source hash recorded at run time. The cause is that two long strings were re-wrapped for formatting after the run completed; the execution-time hash and the completed results were not overwritten, and this current-workspace code difference is not disguised as "all hashes identical". Data integrity and byte-for-byte reproducibility from the current code are two separate conclusions.

The supplementary verification `artifacts/merge-debug-20231106/partial-modes-audit/verification.json` re-verified all 409 output / 408 source hashes, confirming that the original audit evidence is unchanged, `data_validation_ok=true`, while still retaining `current_checkout_matches_execution=false`. Reverting only the two line wraps above rebuilds source code whose SHA256 exactly matches the execution-time hash, and the ASTs of the two versions (ignoring line / column positions) are identical, so this difference is mechanically verified rather than manually waved through. The supplementary verifier adds 3 passing tests, for 992 passing Python tests together with the full regression suite. The original audit report was not rewritten.

Experiment 03b and earlier results are retained below as the global baseline and historical trace.

## Experiment 03b: joint-observation gap segmentation

Still only 2023-11-06, Route 29. Experiment 02's direction candidates are retained first, then subdivided wherever the union of real AVL times and ordered APC arrival–departure intervals has a strict `gap > 1800 s`; exactly 30 minutes is not split. AVL direction holds and invalid spatial projections are not treated as absence of records, and APC is not turned into trajectory points.

4 original direction segments become 8 child segments, **302 → 306** for the day; the unique sources are still **92,143 AVL + 13,862 APC = 106,005**, with no raw record deleted or rewritten. Each child segment re-runs full GTFS matching, 8 workers, 562.131 seconds. Apart from the added `segmentation_joint_gap_s=1800.0`, the configuration is identical to the previous version.

| Original direction segment | Joint gap (local time) | Gap seconds | Child AVL + APC before / after |
| --- | --- | ---: | --- |
| `direction:9027:0001` SOUTH | 01:31:10–15:35:06 | 50,636 | 244 + 50 / 398 + 51 |
| `direction:9027:0002` NORTH | 17:53:10–19:55:00 | 7,310 | 293 + 27 / 194 + 0 |
| `direction:9042:0003` NORTH | 02:17:53–09:35:32 | 26,259 | 134 + 51 / 156 + 0 |
| `direction:9147:0002` SOUTH | 12:55:17–13:34:26 | 2,349 | 539 + 10 / 0 + 6 |

Child IDs are the original ID plus `:gap:0001` / `:gap:0002`. The workbench separately shows the parent segment ID, child index, gap start / end times and source evidence; focus members are strictly determined by the child's `source_rids`, the original `candidate_segment_ids` remain parent annotations, and the newly displayed membership is `review_candidate_ids`. Default browsing follows the actual source coverage and does not treat the outer midnight edge as a real trip end.

The final distribution is **287 PATTERN_ONLY, 18 AMBIGUOUS, 1 MATCHED**, which is not accuracy. The 1,295 OFF_REFERENCE rows remain; the matching thresholds, the uncalibrated-probability constraint and the display of the first three below-threshold diagnostic candidates are unchanged. 9147's APC-only tail segment is fully retained without fabricating AVL.

805 Python regression tests, 78 new child-segment web acceptance checks and 99 legacy web compatibility checks passed. The independent v4 full audit verified 306 cases, 58,490 windows and 3,735 candidate evaluations with zero violations and zero unknown schemas; the report is `artifacts/merge-debug-20231106/joint-gap-20260909/persisted-audit.json`. The final web report is `artifacts/merge-debug-20231106/joint-gap-browser/final-noevidence/browser-gap-results.json`.

For the 298 unsplit cases, the projection, APC evidence, complete result and parent_boundary are field-for-field identical to the previous version; the raw observation Parquet fields, row order and schema of all 31 vehicles are identical. Formal reviews / events are unchanged, and browser acceptance made zero POSTs. All 40 windows of 9147's tail segment are NO_EVIDENCE, with zero retrieved / scored / final candidates; PATTERN_ONLY retains only APC pattern evidence and does not claim a GTFS trip identity.

The sections below record the previous version `054519` for old / new comparison; its immutable artifacts were not overwritten.

## Scope and results

- Only local date **2023-11-06, route 29**; 929 is excluded.
- Inherits experiment 02's deduplication, PI/PO filtering and direction candidate boundaries, without claiming that candidate segments are confirmed complete trips.
- 31 vehicles, 302 segments; 92,143 unique AVL and 13,862 unique APC, with raw fields and source IDs retained.
- The 4 GTFS shapes used were validated into 3 canonical references; different references do not share an unvalidated progress axis.
- 90,848 AVL rows are ON_REFERENCE; 1,295 are OFF_REFERENCE (about 1.41%), retained with the raw record and rejection diagnostics, and excluded from matching distance and delay estimation.
- Final **283 PATTERN_ONLY, 18 AMBIGUOUS, 1 MATCHED**. This is a distribution of heuristic decisions, **not accuracy**, and MATCHED is not a human-verified identity.

APC cross-boundary conflict intervals may be retained in multiple candidates, so APC counts within cases cannot be summed directly into unique full-day observations or passenger counts. This experiment does not output formal TIDES ridership summary tables.

## What can be inspected

Below the dual-source timeline, the GTFS scheduled arrival / departure times are shown; below the raw GPS cumulative distance chart, the schedule, the valid AVL projections and the APC stop-time intervals are shown on the common canonical s axis. Raw APC is not fabricated into a GPS trajectory.

Browsing windows of 1/2/5/10/15/30/60/120 minutes, the full range, or a custom number of seconds / minutes can be selected; the horizontal bar synchronizes the map, the raw timeline / progress chart and the two GTFS comparison charts. The window table shows EVIDENCE / NO_EVIDENCE, and raw records show projection attempts, rejection reasons and the reference.

Competing candidates that exceed the cost gate but have near-equivalent explanatory power carry a dedicated flag and still participate in the ambiguity decision; the top candidate cannot be declared the unique identity just because the runner-up happens to slightly exceed the cost threshold.

Suggested first three examples:

| Vehicle / segment | Suitable for checking |
| --- | --- |
| 9036 / `direction:9036:0000` | 88 of 347 AVL are OFF_REFERENCE; see raw-map retention, canonical line breaks and raw rid diagnostics |
| 9025 / `direction:9025:0014` | Top candidate cost 2.98275, runner-up 3.06560; the runner-up slightly exceeding 3.0 is still insufficient to prove the top candidate unique |
| 9027 / `direction:9027:0000` | All 205 AVL can be projected; the candidate belongs to the post-midnight tail trip of the November 5 service date, for checking cross-midnight schedule conventions |

The only MATCHED is `direction:9054:0012`: of the 7 aggregates retrieved / scored in this run, only one passes the structural-evidence gate; whole-segment aligned point coverage is about 66.28% and duration coverage about 68.86%. This is the only feasible explanation within this model, not an independently verified result; with no second qualifying structural competitor, the margin is null and no score difference is fabricated.

## Computation and reproduction

The heavy evidence comes from `run-20231106-20260909T053127-0df217126cb9`, 8 workers, 487.907 seconds. The final version only replays the final decision layer; saving and verifying the complete logs took 239.434 seconds, without recomputing projection, retrieval, DTW or whole-segment alignment.

All projection, APC evidence, all_candidates, local_scores, windows, retrieval and whole_trip heavy fields are exactly identical to the source run. The source hash, the historical execution code hash and the new decision-layer code hash are recorded separately, and the old run is retained rather than overwritten.

The earlier `run-20231106-20260909T050724-7d87d0cd56e6` had a backward-stepping fitted corrected clock and is kept only as a historical diagnostic, not a deliverable; `053127` fixed the heavy alignment, but its original final decision still had false uniqueness caused by the cost threshold, which was finally corrected by `054519`'s conservative decision layer. Another interrupted prototype has no completion and is not presented as a completed experiment.

## Verification and limitations

671 Python regression tests passed, covering canonical validation, sequence projection, windows, retrieval, local scoring, whole-segment integration, the calibration interface, the final decision, replay and the workbench interface. Static code checks passed. Acceptance used only an independent temporary notes database; formal reviews / events are unchanged.

The final web acceptance passed two groups of 68 checks each: the cost-gate case (including small-margin precision) and the real OFF_REFERENCE case; zero POSTs, external or browser errors. Legacy experiment 02 separately passed 54 compatibility checks.

The independent v3 audit passed completely, with zero violations and zero unknown schemas: it checked 404 source hashes, the heavy fields of 302 cases unchanged, 510,997 shared-delay time-order pairs and 233,630 fit knots. The report is at `artifacts/merge-debug-20231106/gtfs-browser/final-guarded/persisted-result-audit.json`; the final browser reports are in `cost-edge-margin/` and `off-reference/` in the same directory.

Because independently verified trip labels are lacking, **no verifiable final probability calibration was performed, and probabilities are null**. After the final candidate set is changed, the calibration result of the previous decision layer cannot be carried over either. The calibration module and grouped validation tests are provided, but the current results cannot be used to claim a known accuracy.

GTFS is explicitly pinned to the draft feed `ttc-d092742e6a5b` covering this date; its provenance has not been manually approved. The direction crosswalk is inferred from shape / stop-order geometry, without hard-coding GTFS direction_id 0/1 as south / north. The optional vehicle-day / block_id soft-constraint layer is not enabled.

Implementation modules, configuration, log contract and rerun commands: see [README.md](README.md).
