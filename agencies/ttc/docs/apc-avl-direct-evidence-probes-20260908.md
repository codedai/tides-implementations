# APC–AVL Direct Evidence Layer v2: Frozen-Window Perturbation Checks

Date: 2026-09-08. This report is a bounded robustness check of the observational association method; it is not an independent accuracy evaluation, and it does not confirm `trip performed`, actual stop roles, or complete trip start and end points.

The final results are taken from `artifacts/apc-avl-direct-evidence-20260908/probes02/`. The initial `probes01/` is fully retained but has been superseded by this rerun: the final matcher keeps the owner context of all raw aliases of a duplicate measurement, and makes contemporaneous counter-evidence account for the vehicle's feasible travel distance, so that normal approach/departure positions do not refute an exact near point. All figures below were re-measured; none are carried over from the initial run.

## Conclusions

v2 separates "has local support", "only part of the interval has support", and "competition or contradiction exists"; exact duplicate AVL measurements and changes in input order do not affect decisions in the measured windows. When the measured GPS is held fixed and only the segmentation context is refined, pure measured-point support also remains unchanged.

However, v2 is very sensitive to clock offset: these windows originally have 1,413 `direct` rows, and after shifting all APC by −60 / +60 s only 58 / 172 respectively still output `direct`. This is a coverage loss caused by the more conservative spatiotemporal consistency check; it cannot be called an accuracy improvement, nor can it be taken to mean that the true clock offset is one minute.

## Frozen inputs and scope

The windows selected in the previous study based on raw-data features before any method was run are reused directly, with no re-selection based on v2 output:

- `artifacts/apc-avl-coverage-study-20260908/probes01/selection.json`: 12 general windows.
- `artifacts/apc-avl-coverage-study-20260908/probes-edge02/selection.json`: 8 edge windows, 2 per class: shared-stop opposite-direction overlap, DST-ambiguous hour, end-of-month APC tail, and internal AVL gap.
- 20 two-hour windows in total, with 12,909 AVL rows and 1,749 APC rows. The windows truncate the preceding and following history, so their edges are not actual trip start and end points; the windows are not a random representative sample, and the aggregate rates should not be generalized to all operating scenarios.
- After independently checking the source IDs in each window, the summed row counts and the deduplicated source row counts are exactly equal: 12,909 for AVL and 1,749 for APC in both cases, so no source row is double-counted across windows in this run. The 12 general windows contain 8,603 AVL / 1,011 APC rows and the 8 edge windows 4,306 AVL / 738 APC rows; "no cross-window duplication" does not mean the raw AVL contains no records that are identical in content but carry different source IDs.
- The original paths and SHA256 of the two selection files and the SHA256 of the executing code are recorded in the new `probes02/manifest.json`. The raw data, original selection files, and historical experiment outputs were not modified.
- The main check computes the `avl_context` segmentation once and then freezes the original owner assignment. When GPS is removed, only the remaining source records are projected and the observed coverage is updated; when GPS is duplicated, the copies inherit the original owner. This avoids misattributing the old segmentation algorithm's sensitivity to duplicates as a change in the merge method itself.
- `owner_refinement` separately treats every measured timestamp as a distinct owner; this is a deliberately constructed context stress, not a trip segmentation recommendation.

No GTFS, human labels, automatic clock correction, or tail sequence inference were used. The baseline is the original v1 default spatiotemporally constrained association, and v2 is the new default direct-evidence method; the two versions have different evidence requirements for `direct`, so they cannot be ranked by count alone.

## Original-window results

| Window type | APC rows | v1 direct | v2 direct | v2 partial |
|---|---:|---:|---:|---:|
| General windows (12) | 1,011 | 1,007 | 981 | 11 |
| Shared-stop opposite-direction overlap (2) | 145 | 92 | 91 | 2 |
| DST-ambiguous hour (2) | 154 | 34 | 33 | 1 |
| End-of-month APC tail (2) | 251 | 125 | 123 | 2 |
| Internal AVL gap (2) | 188 | 187 | 185 | 2 |
| Total | 1,749 | 1,445 | 1,413 | 18 |

The remaining v2 results are 17 `ambiguous`, 181 `unmatched`, and 120 `ineligible`. Of the `direct` rows, 349 have instant support and 1,064 have bounded-interval support; of the `partial` rows, 10 are arrival-only, 7 departure-only, and 1 interior-only.

Here "bounded interval" means that the same local candidate episode supports the corresponding interval endpoints; it does not prove that the vehicle remained stopped throughout the interval, nor that the vendor's APC aggregation grain has been established.

Tracing v1's original 1,445 `direct` rows one by one: 1,412 remain `direct` in v2; 17 are downgraded to `partial` (10 arrival-end, 6 departure-end, 1 interior); 6 become `ambiguous` due to contemporaneous counter-evidence; 9 become `ambiguous` due to insufficient score; and 1 no longer has an acceptable local candidate. The 9 insufficient-score rows each have only one candidate with a score below 650, and 7 of them also fall below the 60-point margin over the null candidate; the "COMPETING_EPISODES" in the combined reason code must not be taken as evidence that multi-candidate competition actually occurred for these real records. In addition, 1 v1 `ambiguous` row becomes v2 `direct` and 1 becomes `partial`, giving v2 totals of 1,413 / 18.

## Masking and clock perturbations

Each window is run with the original input, the 10 existing perturbations, input reordering, and owner refinement, for 13 input versions in total; across the two methods this yields 520 "method × input version × window" results.

| Perturbation | APC denominator | v1 direct | v2 direct | v2 direct + partial |
|---|---:|---:|---:|---:|
| Original input | 1,749 | 1,445 | 1,413 | 1,431 |
| Remove middle 5 minutes of AVL | 1,749 | 1,399 | 1,362 | 1,384 |
| Remove first 15 minutes of APC and last 13 minutes of AVL together | 1,539 | 1,037 | 1,015 | 1,026 |
| APC time −120 s | 1,749 | 48 | 10 | 17 |
| APC time −60 s | 1,749 | 993 | 58 | 93 |
| APC time +60 s | 1,749 | 1,176 | 172 | 226 |
| APC time +120 s | 1,749 | 164 | 39 | 45 |

Under the middle gap, 6 of v2's originally `direct` records are downgraded to `partial` and 45 become ambiguous/unmatched; a further 2 originally `partial` records lose support. The method does not turn the gap into fabricated GPS observations.

After masking both ends together, the 1,539 retained APC rows have 1,227 `direct` in the unmasked reference result; of these, 1,015 remain `direct`, 3 are downgraded to `partial`, and 209 become unmatched. The reference output is merely the same algorithm's result on more complete input, not a ground-truth label.

A clock shift is an in-memory transformation of source times that re-flags times shifted into the DST-ambiguous period; it is not written back to the source records and does not estimate or correct the true clock. Such a pronounced contraction means that, as long as vendor time synchronization is unconfirmed, v2's direct-support coverage cannot serve as evidence that production feasibility has been met. True clock consistency needs to be checked as an independent gate, rather than blindly loosening thresholds to recover the association rate.

To prevent the mixture of general and difficult windows from masking differences, the table below lists v2 `direct / partial` separately. The edge total is the sum of the four edge strata below it, not an additional sample:

| Window stratum | Original | −120 s | −60 s | +60 s | +120 s |
|---|---:|---:|---:|---:|---:|
| 12 general windows, 1,011 APC | 981 / 11 | 8 / 5 | 46 / 25 | 120 / 31 | 29 / 4 |
| 8 edge windows total, 738 APC | 432 / 7 | 2 / 2 | 12 / 10 | 52 / 23 | 10 / 2 |
| Shared-stop overlap, 145 APC | 91 / 2 | 1 / 1 | 5 / 4 | 12 / 10 | 0 / 1 |
| DST-ambiguous hour, 154 APC | 33 / 1 | 1 / 0 | 1 / 0 | 2 / 2 | 0 / 1 |
| End-of-month APC tail, 251 APC | 123 / 2 | 0 / 1 | 2 / 0 | 11 / 4 | 2 / 0 |
| Internal AVL gap, 188 APC | 185 / 2 | 0 / 0 | 4 / 6 | 27 / 7 | 8 / 0 |

Tracking only the 1,413 originally v2 `direct` rows, so that direct associations newly gained after perturbation are not mixed into the "retained" count:

| APC clock perturbation | Still direct | Downgraded to partial (endpoint/interior) | Ambiguous due to contemporaneous counter-evidence | Weak score/insufficient margin | No local support | Time became ineligible |
|---|---:|---:|---:|---:|---:|---:|
| −120 s | 9 | 5 | 1,365 | 0 | 34 | 0 |
| −60 s | 51 | 30 | 1,223 | 1 | 108 | 0 |
| +60 s | 162 | 50 | 1,059 | 6 | 134 | 2 |
| +120 s | 37 | 4 | 1,313 | 0 | 55 | 4 |

The sharp drop under this stress therefore comes mainly from observed contemporaneous position contradictions, not merely from raising the acceptance score threshold; the near point at the original time cannot cover the measured position that has already moved away at the shifted instant. The counter-evidence rule itself remains an uncalibrated engineering assumption, and its conservatism does not mean the original associations have been confirmed wrong.

The final version checks measured contemporaneous counter-evidence using `distance_m > max(200, 100 + (130 / 3.6) × time_difference_s)` while keeping the original contemporaneous search constraint; this leaves room for normal vehicle motion and no longer treats every point more than 200 m away within 10 s as a contradiction. After the correction, `direct` at +60 s rises from 165 in the initial run to 172 and `partial` from 32 to 54, but the severe clock sensitivity remains.

## Invariance and context decoupling

All 640 implemented per-window checks pass, including exactly one result per retained APC row, input reordering, full AVL duplication, and no local support when the two sources are 45 minutes apart. The normalized decision check does not treat duplicate source rows themselves as independent evidence.

In addition, 40 full v2 decision-object comparisons were made on the saved results of the 20 windows: after sorting by APC ID, the original output and the reordered/duplicated outputs are identical, including candidates, scores, positive and counter-evidence, and result fields; this supplementary read-only comparison is not part of the 640-check count above.

Under the artificial stress in which every AVL timestamp becomes a distinct owner:

- All 574 of v2's original pure measured-point `direct` rows remain `direct`; all 17 pure measured-point `partial` rows remain `partial`. The support level and measured-ping references of these 591 rows are also completely unchanged.
- Of the 839 original `direct` rows that involve interpolation, 712 remain `direct`, 76 become `partial`, 42 `ambiguous`, and 9 `unmatched`. Interpolation across segments is prohibited, so this kind of derived evidence cannot be required to remain unconditionally unchanged.
- v2's total `direct` goes from 1,413 to 1,286; v1's goes from 1,445 to 157. This shows only that the two layers differ in degree of coupling, not that v2 identifies more true trips.

## Independent evidence replay and constructed counterexamples

260 replays cover 10,921 accepted local association results and their 27,487 positive-support witnesses (20,014 measured points, 7,473 interpolations), with no failures found within the scope of this check. The counts include repeated appearances of the same window/record under different perturbations and are not 27,487 independent observations.

The replay does not call the matcher's geometry functions; it uses an independent spherical-distance implementation and checks: real source IDs, same vehicle, time availability, isolation of same-instant conflicts, point coordinates and instants, adjacent real interpolation brackets, gap and speed ceilings, stored position/distance/time residuals, and that the target lies within the APC interval. It does not verify passenger counts, actual stops, trip assignment, or true clock error. The newly added negative evidence in `counter_witnesses` is outside the independent validation scope of this positive-support replay.

The results of the six constructed checks are as follows; they are expected-behavior contracts, not field accuracy labels:

| Constructed scenario | v1 | v2 |
|---|---|---|
| 10-minute interval with a single incidental near point, all others clearly distant | direct | Contemporaneous GPS contradiction, ambiguous |
| 10-minute interval with continuous near points | direct | bounded_interval direct |
| Two passes of the same stop within one chunk, with the APC interval spanning both | direct | Competing candidates, ambiguous |
| Two overlapping APC intervals at the same stop in opposite directions | Two direct | Both local direct retained, with role overlap flagged |
| No measured point nearby, but a valid short-gap interpolation | direct | instant direct, explicitly marked interpolation |
| APC event inside a 10-minute GPS gap | unmatched | unmatched |

22 regression tests in this module pass, covering retention of pure measured points under owner refinement, withdrawal of cross-owner interpolation, and duplication/reordering invariance for the six constructed inputs, among others. A further 2 duplicate-alias owner-context regression tests pass: when the raw aliases of the same measured point belong to different owners, the local direct observation is retained but no single owner is selected; an interpolation bracket containing such a divergence cannot pass the single-owner check. The constructed shared-terminal check explicitly requires `trip_identity_verified=False`.

## Bounded verification of three real records

This section is not part of the frozen-window evaluation above. Instead, from the completed final `full02` results, the first 32 vehicle cases sorted by hashed file name were fixed, and illustrative records were then selected by `SHA256(scenario name | APC source ID)`. Because it depends on output-selected scenarios, it cannot serve as an independent accuracy sample. Each record was traced back to the canonical APC row, the actual AVL source records, the frozen owner, and the stored evidence; only relative times and distances are shown below, with no vehicle IDs, absolute times, or stop IDs.

- Case A, `avl_context`: the APC interval is 401 s long. v1 output `direct` using a point at second 295 inside the interval, about 0.34 m from the APC position. However, the measured position 6 s before the arrival end is about 470.05 m away, and the measured position 1 s before the departure end is about 322.05 m away; the corresponding short-bracket interpolations are about 433.58 m and 323.65 m away respectively. v2 retains the local interior candidate but outputs `ambiguous` because of endpoint counter-evidence. This is a real illustration that "one near point cannot prove the entire APC interval"; the record has only one local candidate and should not be described as multi-candidate competition.
- Case B, `avl_context`: the APC interval is 429 s long, with measured near points about 23.38 m away both 2 s after the arrival end and 1 s after the departure end, plus measured support in between. The frozen algorithm splits the owner at second 43 inside the interval because of a sustained source-context change. v1 is `ambiguous` because the two owners have equal evidence; v2 retains a pure measured-point `bounded_interval direct` while outputting both owners, `segment_id=null`, and `context_status=multiple_segments`, and keeps the mixed route-label flag. Local position evidence can hold without first selecting an actual trip.
- Case C, `gap_only`: the APC interval is 10 s long; the departure end is supported by a 10 s interpolation bracket formed by two real points at relative seconds 6 and 16, with an estimated residual of about 16.30 m. The two bracket endpoints do carry different raw route labels. v2 stores both labels and outputs `route_state=mixed` and `route_conflict=true`; v1's conflict flag for the same record is false. v2 still retains the local direct evidence under the default flag policy, but it neither silently turns the mixed source labels into "route concordant" nor interprets the operational meaning of the special source label.

Among the first 32 `avl_context` case files, there are 1,254 candidates for the first scenario and 10 for the second, and no accepted mixed-route interpolation of the third scenario was found; the third example therefore explicitly switched to the `gap_only` results of the same 32 files, which have 22 candidates. The source IDs, selection hashes, SHA256 of the full result/frozen input files, and relative-time verification records for the three examples are stored in the private artifact `probes02/real_case_audit.json`. The replay found no errors in source references, same-vehicle identity, time availability, stored position, or distance; these bounded checks do not prove APC aggregation grain, actual operational role, continuity of the full trajectory, or trip assignment.

## Reproduction and artifacts

Run from `agencies/ttc`; choose a new, non-existent output subdirectory, as the program refuses to overwrite an existing run:

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync \
  python -m experiments.apc_avl_direct_evidence.probes \
  --audit artifacts/apc-avl-coverage-study-20260908/raw_audit \
  --out artifacts/apc-avl-direct-evidence-20260908/probes-reproduction \
  --selection \
    artifacts/apc-avl-coverage-study-20260908/probes01/selection.json \
    artifacts/apc-avl-coverage-study-20260908/probes-edge02/selection.json
```

The final result directory for this run is `artifacts/apc-avl-direct-evidence-20260908/probes02/`, containing the frozen manifest, the complete decisions per window/perturbation/method, summaries, the 640 checks, the positive-witness replay, the constructed cases, and the completion record. Execution took 7.62 s; this excludes the earlier full raw-data audit and the separately run full-data method comparison. The initial `probes01/` was neither deleted nor overwritten and should not be mixed with the final results.
