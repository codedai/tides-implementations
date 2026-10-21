# APC–AVL Direct Evidence Layer Optimization (2026-09-08)

This round upgrades the spatiotemporal association implementation to a separately versioned release. The focus is on giving every association claim its corresponding evidence: instant records, records with support at both the arrival and departure endpoints, records with only partial support, competing candidates, and spatiotemporal counter-evidence are no longer collapsed into a single `direct`. No raw data, prior study results, or trip segmentation were modified; no GTFS, human labels, automatic clock correction, or single-source tail inference were used; and no production TIDES package was produced.

Code and reproduction steps are in the [experiment README](../experiments/apc_avl_direct_evidence/README.md); the core implementation is [matcher.py](../experiments/apc_avl_direct_evidence/matcher.py). The earlier [TIDES-based evaluation standard](apc-avl-coverage-evaluation-standard.md) continues to apply; its numeric thresholds are exploratory settings of this project, not normative TIDES requirements.

## 1. Scope of this round's changes

The comparison uses exactly the same 6,202,996 AVL rows and 573,596 APC rows, plus the three already-frozen segmentation maps: `gap_only`, `avl_context`, and `apc_assisted`. Every APC row keeps its own result. The shared old-version time/interval eligibility denominator is 573,121; the denominator was not shrunk because the new version abstains more often.

After the first full run `full01` completed, an independent review added multiple ownership for duplicate measurements and a feasible-motion envelope for contemporaneous position counter-evidence. The initial source code and function hash snapshot were retained. The final version re-executed every vehicle as `full02` rather than renaming the initial results. An incremental-reuse check that was developed along the way was not used for the final result.

### 1.1 Final full-data comparison

`full02` completed the paired comparison across all 157 vehicle identities and the three fixed maps. The APC total under each context is 573,596:

| Fixed segmentation context | Old direct | New direct | New partial | New ambiguous | New unmatched | New ineligible |
|---|---:|---:|---:|---:|---:|---:|
| `gap_only` | 562,362 | 547,505 | 4,937 | 12,151 | 8,528 | 475 |
| `avl_context` | 562,097 | 547,499 | 4,939 | 12,152 | 8,531 | 475 |
| `apc_assisted` | 559,287 | 547,455 | 4,946 | 12,188 | 8,532 | 475 |

Taking `avl_context` as an example, on the shared eligibility denominator of 573,121, old-version direct coverage is 98.08%, new-version direct is 95.53%, and direct + partial is 96.39%. New-version direct comprises 437,563 endpoint-pair (`bounded_interval`) supports and 109,936 instant supports; partial comprises 2,785 arrival-only, 2,090 departure-only, and 64 interior-only.

The paired transitions are more informative than coverage rates alone: of the old direct rows, 4,776 became partial, 9,574 became ambiguous, and 559 became unmatched; conversely, 259 old ambiguous and 52 old unmatched rows gained new-version direct. The new version still has 155 direct rows that span multiple fixed chunks; they retain local support but do not claim a single trip assignment.

8,165 APC rows have endpoint counter-evidence under the current spatiotemporal interpretation; another 3,987 fall under the combined reason "competing episodes or insufficient score/margin", and these 3,987 cannot all be called multiple visits. Actual abstentions due to multiple episodes within the same chunk number 1,558. The default route policy still retains conflicts: among locally supported rows, 945 are `conflict` and 147 are `mixed`.

These results show that the evidence scope and abstention reasons of the output are more explicit; they do not prove improved accuracy. See the [full-run summary](../artifacts/apc-avl-direct-evidence-20260908/full02/summary.json), the [paired comparison notes](../artifacts/apc-avl-direct-evidence-20260908/full02/report.md), and the per-vehicle `paired/` and `assignments/` directories.

For new-version `avl_context` direct rows, the spatial residual P50/P95/P99 is 10.45/51.51/80.96 m and the temporal residual is 3/23/37 s. These use the maximum residual among the required endpoint witnesses, whereas the old version used the residual of the best local witness; the definitions differ, so this should not be described directly as residual improvement or degradation. Relative to the development run `full01`, the final motion-envelope counter-evidence revision moved 375 ambiguous rows to direct, 573 to partial, and 98 to unmatched; all previously direct/partial statuses were preserved. Detailed statistics and version differences are in the [final statistics](../artifacts/apc-avl-direct-evidence-20260908/final-statistics01/assessment.json).

### 1.2 Integrity and runtime feasibility

Before and after the full run, the hashes of the canonical inputs, the frozen old-version results, and the executing code were unchanged. Each context keeps exactly one final disposition per APC row, and the implemented integrity checks found no violations. The independent positive-support check for `avl_context` covers 552,438 APC rows and 1,458,166 witnesses, of which 406,216 are interpolations; these are counts of evidence instances, not independent accuracy sample sizes.

A separately implemented negative-evidence validator, which imports neither the matcher nor the evaluator, replayed all 1,720,788 dispositions across 157 vehicle identities × three contexts. The 70,487 retained counter witnesses (43,122 measured points, 27,365 interpolations) passed all 3,821,376 bounded checks N01–N09 with no failures and 0 search-overflow skips. The checks cover real source identity, measurement quality, target endpoint, motion allowance, interpolation geometry together with the ownership of all aliases, route provenance, abstention status, and interval-excursion flags; the replay took 87.62 seconds. Evidence reused across contexts is counted repeatedly. It does not prove that no candidate or counter-evidence was missed, and it does not verify the true clock or actual trips. Full results are in the [independent counter-evidence replay](../artifacts/apc-avl-direct-evidence-20260908/negative-replay02/negative_evidence_replay.json).

Duplicate-alias ownership was separately covered by a bounded positive-evidence audit: the full-vehicle mapping screen identified 25 at-risk vehicle identities, and for those vehicles every one of the 1,626,926 accepted witnesses and 1,652,564 saved candidate witnesses across 213,520 APC rows × three maps was checked; no interpolation crossing alias owners and no omission of declared contexts were found, and the audit input hashes were unchanged. The two witness classes overlap and cannot be summed as independent observations. For the remaining 132 vehicle identities, the full mapping check confirmed that there are no valid cross-owner duplicate measurements; this bounded audit was not additionally repeated for them. Results are in the [positive-evidence alias-ownership audit](../artifacts/apc-avl-direct-evidence-20260908/positive-alias-replay02/assessment.json).

The unified test suite passes with 327 tests, and `ruff check` and the format check pass. Specification, semantic, and truthfulness checks that were not executed remain unexecuted: the completion record explicitly keeps `all_validation_rules_evaluated=false`, `publishable=false`, and `accuracy=null`, and passing engineering checks is not extended into passing all TIDES gates.

The local full run took 794.2 seconds (13.24 minutes) with a peak RSS of 4,173.5 MiB. Index construction took 45.2 s, candidate preparation 57.3 s, matching for the three contexts 26.2/24.4/24.5 s respectively, and positive-evidence checking 66.4 s; the remaining roughly 550.4 s covers unbroken-down costs such as reading input, converting to row objects, loading frozen contexts, persisting deeply nested evidence, and hashing, and cannot be summarized as pure I/O. Indexes and candidates are reused per vehicle across the three maps; this demonstrates that the run is feasible on the current machine, and does not constitute a fair speedup comparison against the old version, which ran a different number of combinations. Machine details are in [completion.json](../artifacts/apc-avl-direct-evidence-20260908/full02/completion.json).

## 2. Key optimizations and what the evidence means

| Risk in the old method | New method | Still cannot be asserted from this |
|---|---|---|
| Only the single best GPS point was sought within a long APC interval | Arrival, departure, and interior evidence are evaluated separately; an observed departure and return split into distinct local candidates | An actual stop for the full interval, an accurate dwell, a complete trip |
| Multiple visits within the same long chunk were merged into one candidate | Local spatiotemporal episodes compete; the null candidate and score margin are retained | That one episode is one confirmed stop |
| An earlier near point was selected while a more contemporaneous distant point was ignored | Negative evidence is retained and checked; point support cannot bridge an observed departure or a quality barrier | That an APC error or sensor failure has been confirmed |
| Distant points from normal approach/departure could also trigger counter-evidence | A point counter must exceed both the distance threshold and the allowed motion distance | That the true speed, error distribution, or clock offset is known |
| Duplicate GPS rows inflated the evidence count or hid context conflicts | Same time and same position form one geometric measurement; the owners of all raw aliases take part in the context decision | That raw aliases can be deleted or that all source labels are trustworthy |
| When the two bracket endpoints carried different routes, the interpolation context was blanked | The complete route sets of both endpoints are kept and explicitly flagged `mixed`/`conflict` | That source routes have been mapped to authoritative route identities |
| An APC row crossing a chunk boundary discarded all local support | Measured-point evidence is separated from chunk ownership; multiple contexts can retain local direct support | That true trip boundaries have been recovered |
| An old candidate cache could be carried over to new parameters or new APC rows | The cache is bound to the source index, the relevant APC fields, and the full configuration, and is rejected on mismatch | That an unvalidated configuration can be deployed directly |

Here `episode` refers only to a stretch of candidate observations of the same vehicle near the APC position. Observed excursions, invalid instants, and long sampling gaps separate episodes. A candidate ID is not a trip ID. Interpolation remains a derived position feature and must reference adjacent real, valid GPS endpoints; when duplicate aliases belong to different owners, one representative row cannot be used to bypass the cross-boundary restriction.

## 3. New output contract

| Output | Meaning |
|---|---|
| `direct / instant` | A zero-duration APC record's instant has qualifying local measured-point or short-gap interpolation support |
| `direct / bounded_interval` | Both arrival and departure are supported by the same local candidate, involving at least two distinct measurements, with no observed interval excursion |
| `partial / arrival_only` | Only arrival-end support is accepted |
| `partial / departure_only` | Only departure-end support is accepted |
| `partial / interior_only` | Only local support inside the interval is accepted |
| `ambiguous` | Counter-evidence, competition, insufficient score/margin, or search overflow; no forced selection |
| `unmatched` | No qualifying support within the implemented local search |
| `ineligible` | Time, position, vehicle identity, or interval fails the eligibility conditions; the original record is retained |

`bounded_interval` does not mean "GPS exists for every consecutive second", nor "the actual dwell has been verified". `partial` must not be used as if the entire APC interval had been confirmed. The two versions define `direct` differently, so the new version's `direct` and `partial` must be compared together, and a drop in count cannot be called an accuracy improvement.

The independent field `context_status` reports `single_segment`, `multiple_segments`, `unmapped`, or `no_assignment`. A record can therefore have direct measured support while `segment_id=null` and `segment_ids` contains two candidate contexts. This is precisely the distinction between local observation and actual trip assignment.

Positive support is stored in `witnesses` and counter-evidence in `counter_witnesses`; both carry real source IDs, measurement identity, target time, support time, spatial residual, interpolation gap, and the complete route labels. Raw APC rows are not merged for sharing the same physical location; passenger counts do not contribute to the positive matching score and are not duplicated and accumulated across multiple GPS rows.

## 4. Thresholds and applicability assumptions

The defaults retain a 60 s point time tolerance, a 100 m local radius, and a 90 s maximum interpolation/episode gap. The baseline distance for counter-evidence is 200 m, the contemporaneous window is 10 s, and the motion ceiling is 130 km/h. A point counter must additionally satisfy:

`distance > max(200 m, 100 m + 130 km/h × time difference)`, where the time difference must be converted to compatible units.

For example, if there is an exact near point at the APC instant and 10 s later the GPS is already 300 m away, that is still within the configured motion ceiling, and the later point alone cannot refute the earlier local support. The corresponding constructed regression tests cover normal approach, normal departure, and motion beyond the allowance. An interpolated counter sits at the target instant but still depends on the short-gap linear position model.

The score floor/null candidate/margin are 650/550/60, and the APC interval cap is 30 minutes. None of these thresholds and weights have been calibrated against independent field labels; scores are not probabilities. The default route policy retains conflict flags and does not promote unconfirmed source labels into authoritative hard constraints.

The new version also changed the ranking weights and endpoint qualification, for example the normalized distance penalty coefficient from 400 to 300. Consequently, although both versions nominally share the 100 m/60 s hard bounds, the effective acceptance regions are not the same; result changes cannot be attributed entirely to episode or duplicate-measurement handling, and they do not constitute a single-factor ablation conclusion.

A 60 s sample matching tolerance is not the same as being able to automatically handle a 60 s device clock offset. Both sources' times are still interpreted as the original local wall-clock time, DST-ambiguous rows are not given a guessed fold, and no automatic clock shift is applied. Counter-evidence is a conditional inconsistency under the current time/position interpretation and cannot prove that the APC is wrong.

## 5. Fixed windows and independent validation

The complete stratified data are in the [final fixed-window report](apc-avl-direct-evidence-probes-20260908.md). The final `probes02` reuses the previously fixed 12 general windows and 8 edge windows, containing 1,749 unique APC source records and 12,909 unique AVL source records; these windows were not re-selected according to the new version's matching results.

640 bounded checks pass, and the independent replay of 27,487 positive-support witnesses found no failures. The counts recur across perturbations and cannot be interpreted as independent sample sizes. Pure measured-point support keeps its logical result under reordering, duplication, and owner-refinement stress; interpolation that crosses owners is withdrawn or downgraded.

The duplicate-measurement context check across all 157 vehicle identities found that `gap_only` has no valid cross-owner duplicate measurements, `avl_context` has 33 involving 25 vehicle identities, and `apc_assisted` has 32 involving 24 vehicle identities. Events under the two maps can overlap and cannot be summed into 65 independent physical events; this statistic shows that context conflicts do exist, not that there is the same number of final mis-associations. The full diagnostics are in the [alias screen](../artifacts/apc-avl-direct-evidence-20260908/alias-screen-all01/assessment.json).

Three real output records were additionally traced back to their canonical source records, illustrating respectively an interior near point masking endpoint excursions, valid measured support spanning chunks, and loss of route labels at the two interpolation endpoints. The cases and their relative times/distances are in the [de-identified case notes](apc-avl-direct-evidence-probes-20260908.md#bounded-verification-of-three-real-records). These are diagnostic samples selected by output scenario, not an independent accuracy assessment.

Clock sensitivity is an explicit limitation: the original windows have 1,413 new-version `direct` rows, but after artificially shifting APC by −60/+60 s only 58/172 remain. Position counter-evidence is the main cause. This is neither an estimated TTC clock offset nor evidence of improved accuracy. The missing-data/masking experiments likewise compare only against the unmasked algorithm output and provide no true trip labels.

## 6. Usage recommendations

Use this implementation as a traceable direct-evidence layer and for diagnostics; do not publish it directly as actual trips or complete stop events. Consume the support level, the actual witnesses, the counter-evidence, and the context and grain flags in preference to reading only a single `status` or `score`.

For a case such as AVL 13:00–14:00 and APC 13:15–14:13, this layer claims only the portion with local observational support. The APC-only tail keeps its unmatched/partial-support status, and direct support is not added by extrapolating GPS; any subsequent sequence inference needs a separate, explicitly labeled layer. The union of observed coverage is still not the actual trip start and end.

Still to be confirmed: the clocks of both sources and the generation contract of the APC time fields, APC row grain and additivity, vehicle/route identity, true trip boundaries, and the complete stop sequence. The current full month of data has already been used for method development; future accuracy validation needs a new, pre-declared evaluation design. Without independent ground truth, this round's conclusions concern implementation behavior, evidence consistency, and runtime feasibility, not association accuracy or a trip-accuracy ranking.
