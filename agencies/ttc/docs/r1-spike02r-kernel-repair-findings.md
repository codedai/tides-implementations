# R1 Spike 02R — Kernel Repair Findings

Experimental, diagnostic, non-publishable (profile
`diagnostic-experimental-r1-spike-02r`). Canonical run `p4`
(2026-08-08, produced after a five-lens adversarial review of the
first run; all 26 confirmed findings were fixed and the battery
re-run), algorithm `x-r1-spike02r-1`. This repository-safe report is
redacted: cases use pseudonymous ids and relative quantities; raw
vehicle ids, source record ids, precise timestamps, coordinates, and
passenger values appear only in the restricted local report inside the
permission-restricted run directory.

## 1. Executive result

Spike 02R repaired the validity of the experimental kernel; it drew no
new architecture conclusion. What was repaired, and what the repaired
kernel measured:

- **The comparator is now genuinely APC-independent.** V1R is
  structurally unable to receive APC input (including count-derived
  flags), and its output fingerprints are byte-identical under APC
  removal/permutation on all three slices.
- **The previous apparent improvement was reduced after evidence
  decoupling and dual margins.** Of the 49 previously accepted DST
  attachments, **42 are unique survivors by exclusion, 1 loses its
  candidate set, and only 6 remain accepted** — and those 6 rest on a
  single unconfirmed evidence family (source-date agreement, D-03
  open), beating the alternate-service-date rival by exactly that
  family's weight, precisely at the acceptance bar. **This candidate
  class is a unique survivor, not an evidence-supported winner**,
  except where a proof-invalid alternative was legitimately excluded.
- **Evidence reuse is quantified**: on the normal slice, 41 of 62 APC
  records that built boundaries also appear among the anchor-scoring
  visit sources (6.5% of anchor evidence edges); 27 of 32 scored
  chunks overlap their own boundary evidence and only 5 are fully
  independent. **This result survives cross-fitting** can be said of
  **7 of the 10** in-sample normal-slice anchor acceptances (2 of 2 on
  each regression slice); the rest change or their chunks do not exist
  out-of-fold.
- **Hard-pinned anchors are not just risky — they can be
  infeasible.** With complete candidate keys, the pinned aligner has
  NO feasible path on one normal-slice component (23 assignments have
  no pinned baseline at all); on components where pinning is feasible,
  the revocable aligner changes 7 normal-slice and 1 month-slice
  assignments. Spike 02's "anchors and the DP never conflicted" was
  prevented by construction and is retracted as evidence.
- **A refined chunk is not a performed trip — and under an honest
  junction rule, nothing on this fixture stitches.** Once a junction
  that crosses ANY applied cut or turnaround is penalized (the
  adversarial review showed the earlier rule let chains span real
  terminal visits), **zero of the 50 candidate junctions survive**;
  every performed-trip hypothesis (24) is a single trip-core chunk,
  and the 26 terminal fragments stay unresolved. Fragment repair
  therefore remains an OPEN problem: it needs junction evidence that
  can distinguish a false mid-route cut from a real terminal visit,
  which the current objective does not have.

**No correctness conclusion is permitted because reviewed labels are
not available.** The architecture verdict is **INCONCLUSIVE — NO
REVIEWED HOLDOUT LABELS** (§16).

## 2. Authorization and containment

ADR 0003 (`docs/decisions/0003-isolated-r1-diagnostic-experiments.md`)
records the bounded authorization: permitted sources, permitted
experimental stages, local-only use, production firewall,
no-publication rule, retention, restrictive permissions, redaction
rules, expiration, and the explicit confirmation that M0/M1/R1 status
is unchanged. Its acceptance is attributed to the project owner's
recorded instruction commissioning this spike, with a revocation
clause if that attribution is incorrect. Directories holding real
vehicle traces (normalized inputs, all spike run outputs,
`database_offline/`) carry owner-only permissions; every artifact in
the 02R run directory — including the redacted labeling CSV — is
0700/0600. All artifacts carry the seven mandatory markers
(`publishable=false`, `architecture_selected=false`,
`human_correctness_claimed=false`, …).

## 3. Reproducibility

`run_manifest.json` records real content SHA-256 for every input file
(normalized parquet tree, per-date GTFS trees, configs) and every
output artifact; a source-tree hash over all experiment files
including untracked ones, plus git revision and dirty status; Python
and package versions; dependency-lock hash; tzdata provenance; full
argv; relevant environment variables; hostname; stage timings; and
content hashes of every frozen Spike 01 (`r1c`) and Spike 02 (`s2b`)
baseline artifact. Filename/size fingerprints are not used anywhere in
02R. Peak RSS (410.6 MiB) and wall time (135.5 s) are in
`performance_report.json`. All 324 spike tests (Spike 01's 220 and
Spike 02's 62 unchanged, 42 new) and 109 production tests pass; ruff
clean; no type checker is configured in this project (documented
absence).

## 4. Comparator integrity

V1R accepts only AVL-derived inputs (structural: no parameter can
carry APC data; GTFS-only clusters asserted; APC-tainted clusters and
count-derived flags rejected — the adversarial review caught a latent
leak through the negative-count flag, now closed and test-covered).
The metamorphic requirement holds on real data: V1R chunk+decision
fingerprints are identical before and after APC permutation/removal on
every slice, and all three profiles are identical across unchanged
rebuilds. The previous `v1_avl_only` comparator is retired as
contaminated. Profile identities are separate (`xsg-avl-*` vs
`xsg-apc-*`, 27 vs 62 normal-slice chunks) with a recorded crosswalk;
V2R-STRUCTURAL (29 chunks) and V2R-ACTIVITY (62) also diverge, so the
activity signals measurably change segmentation. No V2R result feeds
back into any AVL-core behavior.

## 5. Evidence-dependency and cross-fitting

Provenance edges are materialized for every boundary and anchor, with
anchor edges built from the visits actually consumed by per-stop
scoring (the review found and removed an over-approximation). Normal
slice: 62 boundary-source records, 556 anchor-source records, 41
shared; 6.5% of anchor evidence edges reuse a boundary-building
record; 27/32 scored chunks overlap, 5/32 fully independent (DST:
13/26 shared, 4/9 independent; month: 9/14, 0/5). Deterministic
cross-fitting (fold assignment by value-independent union-find over
suspected same-visit chains, so near-boundary duplicates can never
span folds) is reported PER DIRECTION (fold-A-built vs fold-B-built),
with chunk-existence changes visible as their own transition class:
normal slice 10 in-sample acceptances → **7 survive both directions**;
DST 2 → 2; month 2 → 2. Cross-fitted evidence removes direct
same-record reuse only; it is not independent ground truth.

## 6. Boundary decision repair

The boundary kernel has an explicit `NO_BOUNDARY` alternative, a
global non-overlap objective with a fragmentation penalty, and exact
per-candidate optimum analysis (a cut is applied only when forbidding
it strictly lowers the optimum; in-some-but-not-all-optima ties
abstain; there is no enumeration cap). Normal slice: V1R applies 12
cuts with 30 NO_BOUNDARY wins and 4 tied abstentions; V2R-STRUCTURAL
applies 13 with 27 NO_BOUNDARY wins and 8 contract-censored;
V2R-ACTIVITY applies 29 with 10 NO_BOUNDARY wins, 2 excluded by the
global objective, 5 contract-censored. An applied cut that owns no
ping still splits the run (a review-caught defect, fixed and
test-covered); nothing is selected for being the only surviving
proposal.

## 7. Candidate censorship and dual margins

Every blocked alternative is classified (`proof_invalid` /
`plausible_but_censored` / `contract_unresolved` / `coverage_missing`
/ `policy_blocked`; missing-feed coverage is classified as coverage,
not proof) and censored alternatives are scored with hard blocks
clamped toward the worst transform endpoint so they compete in the
EVIDENCE margin. Acceptance requires absolute score, survival margin,
evidence margin, and no contract-unresolved competitor within the
bar; survival-only wins emit `UNIQUE_SURVIVOR_BY_EXCLUSION`. On the
normal and month slices the prior conservative pipeline accepted
nothing, so dual margins convert `policy_blocked` into explicit
`no_candidate` dispositions without creating acceptances.

## 8. Evidence regime repair

Anchor candidates whose feature vectors carry APC-derived per-stop or
terminal evidence are scored under `APC_EVIDENCE` weights — every
Spike-02 anchor was previously misclassified `AVL_ONLY`, so **all
previous anchor acceptances are reclassified**. Missing APC evidence
deterministically selects the AVL-only model; renormalization remains
structurally impossible.

## 9. Complete-candidate preservation

Candidates carry the full key
`(service_date, feed_id, route_mapping_id, scheduled_trip_id,
polarity_hypothesis)` end-to-end. Same-trip-id-different-feed,
same-trip-two-dates, and two-polarity cases remain distinct and
tie/abstain rather than collapse (test-enforced). The alignment DP
orders states by complete keys; components are scoped by vehicle and
feed/date domain. The six DST survivors (§14) are the live
demonstration that date-hypothesis identity must not collapse: their
entire remaining margin IS the date dimension.

## 10. Revocable anchors and exact optimum stability

Anchors are soft hypotheses with a break penalty; every anchor can be
retained, revoked, or tie. Stability is exact (mandatory across ALL
optimal paths; the synthetic 128-equal-optima case is reported
unstable; components beyond the state bound fail closed; the path
must beat the null path absolutely and the second-best distinct path
by a margin — the review found the margin gate dead and it now
demotes). Against the frozen hard-pinned aligner: pinning is
INFEASIBLE on one normal-slice component (23 assignments with no
pinned baseline, resolved only by the revocable model); on feasible
components, 7 normal-slice and 1 month-slice assignments change and
the DST slice agrees fully.

## 11. Fragment roles and stitching

Chunk roles across slices: 24 trip-core candidates, 4 arrival
fragments, 5 departure fragments, 41 terminal turnarounds, 17
unresolved (fragments bounded by turnarounds on BOTH sides now
abstain to unresolved instead of being arbitrarily labeled arrival).
The stitch decision requires physically ordered coreless chains
(departure-fragment before arrival-fragment) and penalizes any
junction crossing an applied cut or turnaround. Under that honest
rule, **all 50 junctions refuse to stitch**; the 24 performed-trip
hypotheses (`xpt2r-*`) are single trip-core chunks. Two chunks sharing
a scheduled-trip candidate are never auto-stitched, turnarounds can
never enter a hypothesis, and no production identity is minted.
**These fragments cannot yet be treated as performed trips**, and the
open design question is junction evidence that separates false
mid-route cuts from real terminal visits.

## 12. Labeling readiness

The versioned label schema and the stratified deterministic review
packet exist (restricted parquet with lineage, 0600; redacted CSV with
pseudonymous case ids and relative times only, 0600, verified free of
raw ids and absolute times by test). **No reviewed labels exist**, so
precision, recall, and any architecture-superiority statement are
BLOCKED.

## 13. Factorial diagnostic results

The factorial pre-registration is frozen
(`experiment_preregistration.json`): S0–S3 × E0–E3 × T_mid ×
independence × alignment × confidence, direction weight 0 in the
primary analysis, group split by vehicle-day with the development day
excluded from holdout, declared endpoints and fail-closed rule,
`labels_reviewed: false`. Only mechanical/diagnostic metrics are
reported; nothing is named accuracy. Phase B's expanded sample (12
vehicle-days from a recorded hash-ordered query) is selected and
recorded but deliberately unprocessed.

## 14. DST and month-boundary regression

DST: prior 49 accepted → **6 accepted / 42 unique_survivor_by_
exclusion / 1 no_candidate**. The 42 fail the evidence margin once
their fold-blocked alternatives compete. The 6 survivors are
fully-contained fold-resolved attachments whose only remaining rival
is the alternate-service-date hypothesis, separated by exactly the
date-agreement family weight at exactly the bar — borderline by
construction and flagged as resting on unconfirmed D-03 evidence.
Month boundary: right-censoring preserved; prior policy-blocked
records become explicit no_candidate; zero acceptances. No fold is
invented anywhere: fold-unresolved boundary candidates are
`plausible_but_censored` and never applied.

## 15. Determinism and performance

V1R fingerprints are invariant under APC permutation; all three
profiles are identical across unchanged rebuilds on every slice; the
boundary, alignment, stitching, and margin kernels are integer-exact
with no enumeration caps in any acceptance path. Phase A: 135.5 s
wall, 410.6 MiB peak RSS; dominant costs are cross-fitted anchor
scoring (67 s) and the dual-margin attachment battery (20 s).

## 16. Architecture decision

**INCONCLUSIVE — NO REVIEWED HOLDOUT LABELS.** The repaired kernel
changes the diagnostic picture materially — fewer acceptances, larger
abstention, quantified reuse, no defensible stitches — but no
architecture choice is permitted from unlabeled data, and coverage is
not a verdict.

## 17. Recommended plan revisions (passage-level only)

1. S7 margins: adopt the dual-margin contract (survival + evidence;
   censored alternatives compete; contract-unresolved competitors
   block; `UNIQUE_SURVIVOR_BY_EXCLUSION` is never `accepted`).
2. S7 evidence regimes: any APC-derived feature forces the
   APC-evidence registry; AVL-only labeling of APC-informed candidates
   is a defect class to test for.
3. S4/S7: boundary decisions need an explicit no-boundary null and a
   global objective with exact tie analysis.
4. S7/S8: candidates must carry the complete
   date/feed/route/trip/polarity key end-to-end; the DST result shows
   the date dimension can be the entire remaining margin.
5. Alignment: anchors must be revocable; hard pinning can be
   infeasible on real data; stability claims require exact all-optima
   analysis with a fail-closed bound.
6. Fragment handling: separate chunk roles and an explicit stitch
   decision before any performed-trip identity; the junction-evidence
   gap (false cut vs real terminal) is the blocking design question.
7. Evaluation protocol: cross-fitting (or equivalent decoupling) with
   per-direction reporting; same-record boundary+confirmation reuse
   must be quantified.

## 18. Recommended next stage

**The human labeling pass over the frozen redacted packet**, followed
by the pre-registered calibration/holdout factorial. It is the
smallest step that unblocks every currently-forbidden conclusion.

## 19. Explicit non-claims

R1 remains open. No milestone is accepted. No production S4–S8 is
implemented or authorized. No TIDES resource or package is produced.
No APC count-field semantics are confirmed (D-06), no stop-code 1652
crosswalk is approved (D-10), no direction semantics are resolved
(D-11), no threshold is calibrated (D-14), no feed is approved (D-15).
No architecture is selected: the verdict is INCONCLUSIVE without
reviewed held-out labels; the six borderline DST acceptances are not
claimed correct; the 24 performed-trip hypotheses are hypotheses, not
trips; and TIDES many-to-one permission validates nothing about
fragments.
