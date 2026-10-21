# APC–AVL segmentation and merge method comparison (2026-09-08)

The current data supports building traceable APC–AVL spatiotemporal associations first, but it is not yet sufficient to certify observed chunks as verified `trip performed` records. Taking AVL-context segmentation with nominal parameters as an example, spatiotemporal association directly links 562,097 APC rows (98.00% of all APC), and sequence extension adds a further 276 inferred associations; these proportions represent evidence coverage, not accuracy.

The full fixture of 6,202,996 AVL rows and 573,596 APC rows was processed, covering the union of 157 source vehicle identifiers, across 21 method/threshold combinations. The objects of analysis are observed chunks and association hypotheses. There are 0 independently reviewed truth labels; association accuracy and trip-boundary accuracy are both undefined.

This study was conducted independently under the user's new request: it did not read the old blind labels, did not run the factorial frozen under the old protocol, did not use GTFS, and did not generate production TIDES data. Raw files remain local; this document contains only aggregate statistics and no vehicle trajectories, raw identifiers or row-level timestamps.

Raw-data issues are covered in the [raw audit](apc-avl-raw-edge-cases-20260908.md), the specification and denominator definitions in the [TIDES evaluation standard](apc-avl-coverage-evaluation-standard.md), and parameters in [study_config.json](../experiments/apc_avl_coverage_study/study_config.json). The access-restricted complete results are in [factorial_summary.csv](../artifacts/apc-avl-coverage-study-20260908/factorial02/factorial_summary.csv) and [analysis02/assessment.json](../artifacts/apc-avl-coverage-study-20260908/analysis02/assessment.json).

## 1. Nine combinations at nominal thresholds

Direct: spatiotemporal support from real AVL points or short-interval interpolation features. Inferred: supported by frozen direct anchors, with no GPS points generated. Weak: time-only baseline results, not counted as trusted direct associations. Ambiguous, unmatched and ineligible records are all retained. All row counts use the total raw APC count as the common denominator; the denominator of rows meeting the time/structure eligibility conditions is recorded separately in the complete results.

| Segmentation | Merge | Direct | Inferred | Weak | Ambiguous | Ineligible | Unmatched | Direct / all APC |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| APC-assisted segmentation | Spatiotemporal + sequence extension | 559,287 | 355 | 0 | 3,143 | 475 | 10,336 | 97.51% |
| APC-assisted segmentation | Spatiotemporal association | 559,287 | 0 | 0 | 3,143 | 475 | 10,691 | 97.51% |
| APC-assisted segmentation | Time-only baseline | 0 | 0 | 565,874 | 242 | 475 | 7,005 | 0.00% |
| AVL-context segmentation | Spatiotemporal + sequence extension | 562,097 | 276 | 0 | 827 | 475 | 9,921 | 98.00% |
| AVL-context segmentation | Spatiotemporal association | 562,097 | 0 | 0 | 827 | 475 | 10,197 | 98.00% |
| AVL-context segmentation | Time-only baseline | 0 | 0 | 565,931 | 185 | 475 | 7,005 | 0.00% |
| Gap-only segmentation | Spatiotemporal + sequence extension | 562,362 | 260 | 0 | 609 | 475 | 9,890 | 98.04% |
| Gap-only segmentation | Spatiotemporal association | 562,362 | 0 | 0 | 609 | 475 | 10,150 | 98.04% |
| Gap-only segmentation | Time-only baseline | 0 | 0 | 566,107 | 9 | 475 | 7,005 | 0.00% |

## 2. Interpretability of segmentation results

| Segmentation | Chunks | Median duration (min) | Non-DST median (min) | P95 (min) | < 2 min | > 2 h | Multi-direction chunks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Gap-only segmentation | 5,675 | 0.0 | 674.4 | 1012.2 | 4,282 | 1,300 | 1,287 |
| AVL-context segmentation | 8,006 | 0.0 | 27.1 | 911.1 | 4,303 | 1,376 | 1,383 |
| APC-assisted segmentation | 11,100 | 21.6 | 69.4 | 406.9 | 4,306 | 2,925 | 3,161 |

A multi-direction chunk is one observed chunk in which at least two APC source directions each have at least 3 directly associated records. The denominator is all chunks of that segmentation method; the metric flags boundaries that may need further inspection. Source directions have not been authoritatively verified, and APC-assisted segmentation itself uses direction evidence, so this metric carries a self-validation bias and cannot be treated as a true over-merge rate. Shorter chunks or purer directions do not automatically indicate higher quality.

All three segmentation methods retain the 3,831 DST-ambiguous GPS rows as separate uncertain chunks, so the overall median is strongly affected by zero-duration chunks; the non-DST median is listed separately. Retaining position-uncertain points also increases the number of short chunks, so not every short chunk can be interpreted as real operational fragmentation. APC-assisted segmentation still has 2,925 chunks longer than 2 hours and 3,161 multi-direction chunks. Combined with the measured ambiguity at shared terminals, this round has not achieved reliable recovery of actual trip boundaries.

## 3. Trade-offs between strict, nominal and wide parameters

All numeric thresholds are exploratory configurations. The strict/nominal/wide time tolerances are 30/60/120 s, the spatial radii 50/100/200 m, the maximum interpolation intervals 60/90/180 s, and the sequence-extension horizons 10/15/30 min. Actual acceptance is further constrained by the score and candidate-margin rules; the configured radius is not an automatic acceptance range.

| Segmentation | Regime | Direct | Added by sequence extension | Ambiguous (sequence extension) | > 100 m / measurable direct | > 60 s / measurable direct |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Gap-only segmentation | strict | 550,465 | 222 | 7,301 | 0/550,465 | 0/550,465 |
| Gap-only segmentation | nominal | 562,362 | 260 | 609 | 0/562,362 | 0/562,362 |
| Gap-only segmentation | wide | 564,090 | 348 | 402 | 320/564,090 | 1,155/564,090 |
| AVL-context segmentation | strict | 550,164 | 235 | 7,512 | 0/550,164 | 0/550,164 |
| AVL-context segmentation | nominal | 562,097 | 276 | 827 | 0/562,097 | 0/562,097 |
| AVL-context segmentation | wide | 563,820 | 365 | 623 | 318/563,820 | 1,155/563,820 |
| APC-assisted segmentation | strict | 547,322 | 297 | 9,709 | 0/547,322 | 0/547,322 |
| APC-assisted segmentation | nominal | 559,287 | 355 | 3,143 | 0/559,287 | 0/559,287 |
| APC-assisted segmentation | wide | 560,937 | 449 | 2,960 | 323/560,937 | 1,068/560,937 |

100 m / 60 s are common reference lines across configurations. A wide configuration exceeding these two reference lines does not violate its own 200 m / 120 s rules. Conversely, the time-only baseline cannot be regarded as meeting the common spatial evidence standard even though it produces more associations. Residuals are evidence the algorithm already used, so a smaller residual cannot independently prove an association correct.

| Nominal combination | Spatial P50/P95 (m) | Temporal gap P95 (s) | Share > 100 m | Share > 60 s |
| --- | ---: | ---: | ---: | ---: |
| APC-assisted segmentation + Spatiotemporal + sequence extension | 2.7/26.7 | 12.0 | 0.00% | 0.00% |
| APC-assisted segmentation + Spatiotemporal association | 2.7/26.7 | 12.0 | 0.00% | 0.00% |
| APC-assisted segmentation + Time-only baseline | 16.1/78.4 | 6.0 | 2.73% | 0.06% |
| AVL-context segmentation + Spatiotemporal + sequence extension | 2.8/26.6 | 12.0 | 0.00% | 0.00% |
| AVL-context segmentation + Spatiotemporal association | 2.8/26.6 | 12.0 | 0.00% | 0.00% |
| AVL-context segmentation + Time-only baseline | 16.1/78.4 | 6.0 | 2.73% | 0.06% |
| Gap-only segmentation + Spatiotemporal + sequence extension | 2.8/26.5 | 12.0 | 0.00% | 0.00% |
| Gap-only segmentation + Spatiotemporal association | 2.8/26.5 | 12.0 | 0.00% | 0.00% |
| Gap-only segmentation + Time-only baseline | 16.1/78.5 | 6.0 | 2.73% | 0.06% |

## 4. Coverage inconsistency and missing evidence

| Nominal sequence extension | Chunks with APC lead | Chunks with APC tail | Denominator: chunks with any association | APC tail duration P95 (s) |
| --- | ---: | ---: | ---: | ---: |
| Gap-only segmentation | 4 | 95 | 1,312 | 694.0 |
| AVL-context segmentation | 4 | 184 | 1,748 | 556.0 |
| APC-assisted segmentation | 4 | 735 | 4,808 | 119.0 |

Sequence inference also has a physical-evidence issue that needs separate checking: three distinct stop codes may point to nearby locations. The current kernel checks the distance from the target to its anchors, but does not require physical progression between the three anchors. The following is a 50 m exploratory screen added post hoc in this analysis; it is not a calibrated acceptance threshold.

| Nominal sequence extension | All inferred rows | Rows with complete anchor coordinates | All three anchors pairwise under 50 m |
| --- | ---: | ---: | ---: |
| Gap-only segmentation | 260 | 260 | 0 |
| AVL-context segmentation | 276 | 276 | 0 |
| APC-assisted segmentation | 355 | 355 | 0 |

The following table uses AVL-context segmentation + nominal sequence extension as an example; the categories may overlap and cannot be summed as mutually exclusive totals.

| Evidence condition | APC denominator | Direct | Inferred | Weak |
| --- | ---: | ---: | ---: | ---: |
| dst_ambiguous | 428 | 0 | 0 | 0 |
| time_nonexistent | 0 | 0 | 0 | 0 |
| grain_uncertain_signal | 45,226 | 43,842 | 0 | 0 |
| avg_sample_1 | 528,370 | 518,255 | 276 | 0 |
| avg_sample_gt_1 | 45,226 | 43,842 | 0 | 0 |
| cross_midnight_interval | 76 | 76 | 0 | 0 |
| boundary_spanning_interval | 275 | 0 | 0 | 0 |
| no_candidate | 10,396 | 0 | 0 | 0 |
| multiple_candidates | 166 | 0 | 0 | 0 |

The distribution of final candidate chunk counts for the nominal combinations is as follows. Sequence inference records the final owner as a single candidate, so this is not the size of the original candidate universe and cannot be used to prove that no trip interpretation was missed.

| Nominal combination | 0 candidates | 1 candidate | 2 or more candidates | APC denominator |
| --- | ---: | ---: | ---: | ---: |
| APC-assisted segmentation + Spatiotemporal + sequence extension | 10,811 | 562,594 | 191 | 573,596 |
| APC-assisted segmentation + Spatiotemporal association | 11,166 | 562,239 | 191 | 573,596 |
| APC-assisted segmentation + Time-only baseline | 7,480 | 553,576 | 12,540 | 573,596 |
| AVL-context segmentation + Spatiotemporal + sequence extension | 10,396 | 563,034 | 166 | 573,596 |
| AVL-context segmentation + Spatiotemporal association | 10,672 | 562,758 | 166 | 573,596 |
| AVL-context segmentation + Time-only baseline | 7,480 | 561,380 | 4,736 | 573,596 |
| Gap-only segmentation + Spatiotemporal + sequence extension | 10,365 | 563,229 | 2 | 573,596 |
| Gap-only segmentation + Spatiotemporal association | 10,625 | 562,969 | 2 | 573,596 |
| Gap-only segmentation + Time-only baseline | 7,480 | 566,038 | 78 | 573,596 |

APC rows carrying a grain-uncertainty signal can still form diagnostic spatiotemporal candidates, but this cannot close the APC publication gate. `AvgSample=1` is likewise not contractual proof of a single stop visit. Unmatched rows may result from missing coverage, physical inconsistency, insufficient candidates or cross-boundary ambiguity; they must not be uniformly interpreted as bad data.

The first and last timestamps of a real point sequence constitute only an observation envelope. All output `actual_trip_start/end` fields remain null. For the AVL 13:00–14:00, APC 13:15–14:13 case, an inferred APC tail may be retained when supported by independent direct anchors and continuity evidence; 13:00–14:13 is not designated as a verified actual trip start and end.

## 5. Robustness checks and credibility boundaries

The project and study implementation tests total 203, with 0 failures and 0 errors; see the [JUnit artifact](../artifacts/apc-avl-coverage-study-20260908/verification-tests-final.xml). The general and targeted probes additionally executed 580 scoped checks. Passing these checks means the corresponding implementation behavior was verified; the real boundary limitations and constructed counterexamples described below still hold.

The general probes selected 12 fixed 2-hour vehicle windows, sampled by raw-data presence rather than by matching outcome. They cover 90 segmentation/merge/perturbation configurations and 1080 window × configuration comparisons; see the access-restricted [probes01/probe_summary.json](../artifacts/apc-avl-coverage-study-20260908/probes01/probe_summary.json). The masking experiments use the unmasked algorithm output as the reference and count retained ownership, abstentions, direct-to-inferred transitions and disjoint chunk switches. These are stability metrics under missing data and cannot be called true matching accuracy.

| Perturbation | Configurations | Retained APC comparisons | Reference-assigned then abstained | Direct to inferred, same membership | Disjoint chunk switches |
| --- | ---: | ---: | ---: | ---: | ---: |
| apc_clock_+120s | 9 | 9,099 | 5,341 | 0 | 0 |
| apc_clock_+60s | 9 | 9,099 | 1,104 | 18 | 0 |
| apc_clock_-120s | 9 | 9,099 | 5,829 | 0 | 0 |
| apc_clock_-60s | 9 | 9,099 | 2,073 | 0 | 0 |
| apc_prefix_15m | 9 | 8,163 | 0 | 0 | 0 |
| avl_internal_gap_5m | 9 | 9,099 | 192 | 36 | 0 |
| avl_suffix_13m | 9 | 9,099 | 894 | 240 | 0 |
| exact_avl_duplication | 9 | 9,099 | 0 | 0 | 0 |
| no_overlap_45m_separation | 9 | 2,718 | 2,708 | 0 | 0 |
| offset_coverage_15m_13m | 9 | 8,163 | 894 | 240 | 0 |

Comparison counts in the table are repeated across algorithms and cannot be treated as independent sample sizes. Chunks with no surviving GPS support to compare against are listed separately as non-comparable; they must not be assigned a fabricated Jaccard=0 or counted as switches. The complete probe check status is in `checks.json`; any partial perturbation failures must be retained and explained.

Clock offset is a clear sensitivity point: in the general windows, the reference direct associations for APC-assisted segmentation + spatiotemporal association number 1,005; after artificially shifting the APC clock by -60/+60 s, associations drop to 660/821, and after -120/+120 s to 34/117. This experiment does not estimate the real TTC clock offset; it shows that a clock contract and offset diagnosis must precede any increase in association tolerance.

Targeted probes sampled by raw boundary risk were also executed, drawn from shared-terminal overlaps, DST ambiguity, extraction-window ends and internal AVL gaps. They form a different sample population from the general windows above; the final results are stored in [probes-edge02](../artifacts/apc-avl-coverage-study-20260908/probes-edge02/probe_summary.json) and cannot be pooled into a single overall accuracy. The earlier edge01 is retained for diagnostics and is not used as the final statistics.

These targeted probes executed 720 window × configuration comparisons; the number of failed checks is 0. Complete denominators, stratified results and any failures are retained in their manifest, summary and checks.

The following table uses APC-assisted segmentation + nominal sequence extension throughout, with the joint masking "APC prefix 15 min, AVL suffix 13 min" applied. 2 windows were selected for each of the four raw evidence conditions; both the reference and perturbed associations are algorithm references only.

| Targeted raw evidence | Retained APC denominator | Reference assigned | Assigned after masking | Direct to inferred, same membership | Reference-assigned then abstained |
| --- | ---: | ---: | ---: | ---: | ---: |
| dst_ambiguous_hour | 137 | 17 | 17 | 0 | 0 |
| internal_avl_gap_90_to_1800s | 160 | 159 | 159 | 25 | 0 |
| month_end_apc_tail | 215 | 89 | 60 | 0 | 29 |
| shared_stop_opposite_direction_overlap | 120 | 84 | 68 | 3 | 16 |

In the targeted real samples, two APC rows at the same stop in opposite directions with intervals overlapping by 27 seconds can both be directly associated with the same observed chunk. This shows that direct GPS support can hold while the actual trip boundary remains unresolved. Another shared-terminal sample remains ambiguous/unmatched. Single-source continuation at the end of the month did not recover the reference associations in these masked samples, so not every coverage tail can be treated as a completable same-trip continuation.

In that APC-assisted + sequence-extension DST targeted window, duplicating the GPS rows increased the raw chunk total from 743 to 1,484; the accepted owner relations projected onto the surviving source-record set can remain unchanged. These two metrics describe different objects. The 232 targeted checks cover only explicitly declared rules and do not mean that duplicate data has no effect on the chunk count.

The core code hashes, statuses and results of the independently constructed cases were verified, and three negative results were actually reproduced; see [segmentation_counterexamples.json](../artifacts/apc-avl-coverage-study-20260908/segmentation_counterexamples.json): duplicating a single GPS measurement can change context segmentation from 1 chunk to 2; duplicating all GPS rows can change a geometric reversal from 2 chunks to 1; and a shared terminal between consecutive roles with overlapping arrival/departure intervals can change the APC direction split from 2 chunks to 1. Even when the duplicate-data probes on the general windows pass, they do not eliminate these counterexamples. They limit the generalization robustness of the segmentation methods and cannot be offset by "no runtime anomalies".

The kernel computes candidate margin per observed chunk: competition between multiple stops or adjacent trips within the same long chunk is collapsed into one owner. A high direct association rate on long chunks therefore does not mean trip identity is clear. The APC source grain flag includes non-integer count/load signals, and APC-assisted segmentation excludes those records; it can be said that passenger-count values were not used as a positive matching score, but not that the whole method is entirely independent of the count fields.

Among the direct associations of nominal AVL-context segmentation + spatiotemporal association, 1,025 APC rows still differ from the source route label of the supporting GPS. Direct association here confirms only local spatiotemporal support, not route or trip semantics.

The machine-readable [rubric_check_results.json](../artifacts/apc-avl-coverage-study-20260908/analysis02/rubric_check_results.json) records the actual scope, denominator and unchecked portion of every rule. The number of hard-rule failures observed in this analysis is 0 (counted per configuration × rule); partially completed or unexecuted rules are explicitly marked `not_evaluated`, and there is no claim that "all TIDES gates pass".

The following were verified: a single disposition per APC row, chunk membership for all AVL rows, raw GPS references, no fabricated GPS in inferences, no acceptance of time-uncertain records, and no promotion of outputs to actual trips. Exhaustive enumeration of boundary candidates, independent recomputation of every interpolation residual, and invariance under all input permutations/batchings remain incomplete. The P02–P07 items (time contract, APC grain, event semantics, complete stop order and actual trip identity) remain unresolved; P01 has no production package available for publication validation.

## 6. Local run feasibility

The full 21-combination run took 20.3 minutes, with a peak process RSS of 6.93 GiB and a run directory totaling 0.55 GiB. This is the whole-run peak under a shared process, shared support candidates and segmentation caches; it cannot be apportioned into independent per-algorithm peaks.

| Nominal combination | Shared candidate build (s) | Segmentation (s) | Association (s) | Assignment files (MiB) |
| --- | ---: | ---: | ---: | ---: |
| APC-assisted segmentation + Spatiotemporal + sequence extension | 97.0 | 62.4 | 21.0 | 10.8 |
| APC-assisted segmentation + Spatiotemporal association | 97.0 | 62.4 | 17.7 | 10.7 |
| APC-assisted segmentation + Time-only baseline | 97.0 | 62.4 | 15.4 | 9.1 |
| AVL-context segmentation + Spatiotemporal + sequence extension | 97.0 | 62.2 | 22.8 | 10.6 |
| AVL-context segmentation + Spatiotemporal association | 97.0 | 62.2 | 19.1 | 10.6 |
| AVL-context segmentation + Time-only baseline | 97.0 | 62.2 | 17.9 | 8.9 |
| Gap-only segmentation + Spatiotemporal + sequence extension | 97.0 | 31.0 | 20.1 | 10.6 |
| Gap-only segmentation + Spatiotemporal association | 97.0 | 31.0 | 17.0 | 10.5 |
| Gap-only segmentation + Time-only baseline | 97.0 | 31.0 | 15.5 | 8.8 |

The shared candidate build is repeated on every row but actually executes once per vehicle; each segmentation is likewise reused across merge configurations, so the table columns cannot be summed as total runtime. The association column includes the spatiotemporal matching pre-step; disk write/read and aggregation overheads appear in the wall-clock time. Software and machine information is stored in the manifest; CPU time and per-method independent memory peaks were not measured. The single-month result supports local batch feasibility; it does not yet demonstrate multi-month incremental, parallel or production deployment capability.

## 7. Conditional recommendations

The APC–AVL-first route can continue: use spatiotemporal association as the direct evidence layer, preserve independent raw records, and then treat sequence extension as a revisable inference layer. The merge must not repeat-count one APC record across multiple GPS points, nor fill AVL gaps with fabricated observation points.

The time-only method is suitable as a wide candidate baseline; its higher association count cannot be taken as trusted coverage. Gap-only segmentation is suitable for producing continuous observation chunks; long and multi-direction chunks indicate that it is insufficient to declare a single trip directly. The AVL-context and APC-assisted methods can serve as two complementary sources of boundary candidates: use their measured coverage/abstention/fragmentation trade-offs to select the next review scope, rather than crowning a best method by direction purity or association count.

Retain the strict/nominal settings as human-review candidates for now, while sampling separately the associations added by the wide setting and all single-source prefixes/suffixes. Same-vehicle different-trip, reversal, internal-gap, clock-offset and aggregated-APC cases need to be labeled separately, followed by independent vehicle-day holdout validation after the rules are frozen. Without these labels, this study can only offer conditional conclusions on evidence consistency and engineering feasibility, not a ranking of the methods by true accuracy.

This study did not open the old label/holdout artifacts, but it did inspect the entire current raw month. Vehicle-days overlapping this study can no longer be called a raw-data holdout set completely unseen by this round's rule selection. Future independent validation needs a new, pre-declared evaluation design, preferably using fresh source-data windows; the old protocol and old label status are not modified in this round.
