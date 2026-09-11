# R1 design-validation spike findings

- Date: 2026-08-07 (overnight spike)
- Code: `experiments/r1_design_spike/` (package `r1spike`, algorithm
  version `x-r1-spike-1`, git revision `0826af04ffef…` plus uncommitted
  spike files)
- Runs cited: `artifacts/experimental-r1-spike-r1c-{normal_day,
  dst_fall_back,month_boundary}/` (local only; not committed)

## 1. Scope and authorization

This spike is **experimental, diagnostic, and non-publishable**. It is
not a production implementation of S3–S8, does not implement the final
S7 contract, and does not close R1 or authorize formal S3+ work. The
formal disposition of [ADR 0002](decisions/0002-merge-plan-review-disposition.md)
is unchanged: architecture direction approved; provisional S0–S2
diagnostic work authorized; S3+ frozen; **R1 remains open**; no TIDES
publication authorized. Every artifact carries
`profile = diagnostic-experimental-r1-spike`, `publishable = false`,
`tides_conformance_claimed = false`, and the writer refuses TIDES-shaped
file names and paths outside the experimental run directories by
construction (`artifacts_io.py`, verified by tests).

The "legacy-informed R1 re-review report" naming blockers R1-B01–R1-B11
is **not present in this workspace**; the blocker list used below is a
reconstruction from the review's R1/R2/R3 sections and the legacy
methodology document, recorded as an assumption (see the mapping table
in `experiments/r1_design_spike/README.md`).

## 2. Inputs and assumptions

Inputs (all read-only; raw CSVs were never rescanned or modified):

- Normalized S0–S2 artifacts: `artifacts/diagnostic-2026-08-06/normalized/`
  (AVL 6,202,996 rows; APC 573,596 rows; tree fingerprint
  `08224b483bf28eb5…` — file names+sizes, not a full content re-hash;
  the underlying source SHA-256 values are pinned in that run's own
  manifest).
- GTFS artifacts produced by the **existing** `ttc-tides gtfs process`
  CLI for service dates 2023-11-04/05 (feed `ttc-d092742e6a5b`) and
  2023-11-27/28/29/30, 2023-12-01 (feed `ttc-de8294439596`), selected
  through the **draft candidate manifest with `--allow-candidate`** —
  no approved feed exists (D-15 open); this is a recorded override, not
  an approval.
- Config `experiments/r1_design_spike/config/spike_config.yaml`
  (hash `1833fb11a95fe881…`), score contract
  `experimental_score_contract.yaml` (hash `8f66732c75d443a2…`,
  3 variants: conservative / permissive / forced_diagnostic — the last
  is intentionally unsafe and exists only to exercise Stages 2–3).

Deterministic vehicle selection (query recorded per manifest, sorted
ascending, first 3 — no hand-picking):

| Slice | Query | Vehicles | AVL rows | APC rows |
| --- | --- | --- | --- | --- |
| A normal day (2023-11-28) | `avl_route_day` | 1011, 7237, 7257 | 7,275 | 616 |
| B DST (2023-11-04 23:30 → 11-05 03:30) | `avl_dst_window` | 1267, 7261, 9019 | 3,253 | 363 |
| C month boundary (11-30 22:00 → 12-01 02:30) | `apc_after_avl_end` | 9030, 9033, 9034 | 2,158 | 449 |

Key labeled assumptions (full list in every run manifest): naive
America/Toronto wall-time interpretation (G0.3/D-16 open, experimental);
`source_date_key` = wall calendar date (verified on this fixture) used
as the date-agreement proxy; dominant-shape-per-(date,route) geometry
for APC spatial features (branch/pattern semantics blocked by D-11);
uncalibrated score parameters; experimental XT-FOLD-A fold rule;
experimental X4 segmentation thresholds; simplified Stage-3 segment
weakening.

## 3. What was implemented

- **Fixed-point scoring engine** (`fixedpoint.py`, `scoring.py`):
  integer qscores at scale 10⁶, exact-rational quantization with
  half-even rounding, registered-features-only, per-feature monotone
  piecewise-linear transforms, per-decision/per-regime family weights,
  declared null-family priors, hard bounds with stable reason codes, and
  threshold+margin decisions where `margin_q = best − max(second, null)`
  and equal evidence always abstains.
- **Complete candidate contracts** (`contracts.py`,
  `candidate_generation.py`): full segment tuples
  (date/feed/route/trip-or-none) with `UNRESOLVED_SERVICE_DATE`,
  `NO_ROUTE_MAPPING`, `NO_SCHEDULED_TRIP` null alternatives; full APC
  tuples (fold/date/feed/vehicle/route/stop/segment) with exactly one
  `NO_ATTACHMENT`; fold/stop/route/date variants stay distinct.
- **Time hypotheses** (`time_hypotheses.py`): both folds retained for
  ambiguous wall times, spring-forward structurally invalid, the
  conservative XT-FOLD-A anchor-continuity rule for AVL (uniform-fold
  hypotheses only; collision groups refuse), APC folds never
  independently resolved (D-05).
- **Experimental segmentation** X4-A (gap-only) and X4-B (gap +
  debounced 2-of-4 context votes), global per vehicle, cross-midnight
  preserving, uncertain gaps never split, `tatripId` never a partition
  key, experimental `xs1-<32 hex>` IDs only.
- **Geometry** (`geometry.py`): metre-space point-to-*segment*
  projection (equirectangular), explicit `shape_pt_sequence` sorting,
  retained residuals/along-distance, three evidence states
  (missing/weak/hard-contradiction), scoped candidate geometries with no
  anonymous nearest-shape fallback.
- **Temporal features** (`temporal_features.py`): declared
  gap/overlap/denominator definitions, bracketing-only interpolation,
  missing ≠ zero.
- **Stage-1 APC summary policies** A/B/C behind one interface
  (`stage1_policies.py`) — none selected as final.
- **Staged resolution** (`resolution.py`, `windows.py`): components via
  union-find, three caps failing closed, Stage 1 → barrier → Stage 2 →
  barrier → Stage 3 with immutable context, window ownership rules, and
  schedule-order execution.
- **Sequence validation** (`sequence_validation.py`): occurrence-based
  integer-cost DP, all-optima enumeration (bounded), conservative
  keep-only-intersection rule for ties, fold rollback signaling.
- **Baselines**: B1 bounded unique-survivor; B0 legacy join implemented
  and tested on synthetic data only (legacy artifacts absent — §9).
- **Diagnostics**: per-slice metrics, edge-case inventory, determinism,
  sensitivity, performance reports; guarded artifact writer.

Deliberately **not** built: production S3–S8, `trip_id_performed`,
TIDES resources/publication, calibrated thresholds, Stage-3
segment-score recomputation (simplified to a visible
all-attachments-demoted rule), branch/pattern-scoped geometry,
non-uniform Policy-C priors, and a full production sequence optimizer.

Tests: 219 spike tests pass; the production suite (109 tests) passes
unchanged; ruff check/format clean.

## 4. R1 blocker evidence matrix

Classes: **S** supported by experiment · **C** contradicted by
experiment · **A** still ambiguous · **N** not attempted · **B** blocked
by unavailable semantics.

| Blocker (reconstructed) | Class | Evidence |
| --- | --- | --- |
| R1-B01 objective-function structure | **S** (expressible) + **C** (adequate as-specified) | The unit-normalizing transform/family/regime structure is implementable and deterministic (`scoring.py`; `test_scoring_decisions.py`). But on the real fixture the S5-style evidence families cannot separate adjacent scheduled trips: all 21 mapped trip-scale segments land `low_margin` with margins 743–25,392 q (0.07–2.5 %) over 32–52 hard-valid tuples (`x_segment_decisions.parquet`, slice A). |
| R1-B02 evidence regimes | **S** | Regime switch on missing support implemented without renormalization; renormalization structurally impossible (raises); verified in `test_scoring_decisions.py::TestRegimeSeparation`. |
| R1-B03 Stage-1 APC summary | **A** | Policies A/B/C all implemented with the mass-≤1 invariants (12 tests). On every slice, **zero decisions change across policies** — date-consistency support discriminates *dates*, while the binding ambiguity is *adjacent trips on the same date*, which all share identical support by construction. The fixture never made d-vs-d-1 the deciding margin, so the policy question remains open and may be immaterial at trip grain. |
| R1-B04 graph class / caps | **S** (mechanics) + **C** (assumed sizes) | Caps fail closed with stable reasons (tests). Measured: X4-A (gap-only, 1800 s) yields *day-scale* segments (median 3.6 h, max 15 h) that trivially overflow a 24-tuple cap; even X4-B trip-scale segments carry 32–52 tuples on this 5–10-min-headway route, so 21/24 exceeded cap 24. The plan's "components are independent and small" holds only under a tight candidate prefilter: at a 7,200 s gap prefilter, slice A collapses to **3 components = 3 whole vehicle-days**. |
| R1-B05 fixed-point determinism | **S** | Identical logical checksums across forward/reverse/shuffled window orders, 24 h vs 12 h windows, and a full double-run, on all three slices (`determinism_report.json`, `identical: true`); ID-swap and exact-tie tests pass; all decision arithmetic is integer. |
| R1-B06 complete candidates + nulls + margins | **S** | Null alternatives compete and win measurably (unit tests; PO/PI segments accept a no-route/no-trip tuple under the permissive variant); one disposition per record holds on all slices (616/363/449 rows in `x_record_dispositions.parquet`). |
| R1-B07 fold hand-off / rollback | **S** (mechanics) + **A** (rule adequacy) | Both folds retained; attachment-selected folds recorded; Stage-3 demotion rolled a real fixture record's fold back to unresolved (`c1-…-197975`, slice B, forced variant). But XT-FOLD-A leaves 1,195/3,253 DST-slice pings unresolved (collision groups: through-running vehicles traverse the hour twice), making 8/12 segments time-uncertain and 277/363 records `time_unresolved`. A uniform-fold hypothesis family is too coarse; see §10. |
| R1-B08 window ownership / order invariance | **S** | Ownership rules implemented incl. unresolved-time and no-candidate fallbacks; execution-layout invariance verified synthetically and on all real slices. One doc/impl mismatch found (source-calendar fallback uses UTC midnight, not local — deterministic but mislabeled; noted for revision). |
| R1-B09 geometry contract | **S** + **B** (branch/pattern scoping) | Metre-space segment projection with retained residuals works (31 tests; ~0.01 % error at fixture scale); evidence states fire on real data — one slice-A segment lost *every* candidate to `GEOM.HARD_CONTRADICTION` (median residual > 500 m; deadhead/garage activity). Route 29 vs 929 remain separate scoped candidates. Branch/short-turn/pattern-scoped geometry is blocked: mapping `Branch`/direction labels to GTFS patterns needs unconfirmed semantics (D-11). |
| R1-B10 temporal contract | **S** | Declared formulas and shorter-interval overlap denominator; missing stays missing; the legacy 9,083 s stale match is reproduced by the B0 join and hard-rejected by the contract (`XR1.TEMPORAL.STALE`); 868–2,010 stale candidate hypotheses per slice were generated (7,200 s prefilter) and scored to hard-invalid rather than silently pre-pruned. |
| R1-B11 sequence validation / tied demotions | **S** (with a caveat) | Occurrence-based DP with full optimum enumeration; conservative intersection rule; tied-demotion, extra-stop, repeated-stop, [100, 200, 150], and rollback scenarios pass. Caveat: under enumeration truncation, `certain` vs `ambiguous` demotion labels become enumeration-order-dependent (documented; consumers must treat `certain_demotions` as unreliable when `OPTIMA_TRUNCATED` is present). |

## 5. Design hypotheses tested

**H1 — Threshold+margin acceptance over S5 baseline features can accept
trips on frequent routes.** *Contradicted.* Expected: clear winners on
a normal day. Observed: 21/21 mapped slice-A segments abstain
`low_margin` under the conservative variant; margins are 0.07–2.5 %
because adjacent headways differ by one ~300–600 s start-residual step
on an otherwise identical feature vector (same shape, same overlap
shape, same date). Under permissive (margin 2.5 %), still **zero**
mapped trip-scale segments accept — all 21 remain `low_margin` (the
best conservative margin, 25,392 q, is a near-miss of the 25,000 q
permissive bar but does not survive the permissive re-weighting); the
only permissive acceptances are the two unmapped PO/PI segments
resolving to null no-route tuples (H7). **The result depends materially
on evidence the current feature set does not contain** (per-stop timing alignment, direction,
or headway-aware acceptance semantics). A production margin threshold
should not be frozen until a discriminative feature family exists.

**H2 — Gap-only segmentation is a usable resolution substrate.**
*Contradicted for frequent routes.* X4-A (1800 s) produces day-scale
segments (a vehicle rarely gaps 30 min mid-day), which makes segment ↔
scheduled-trip matching structurally meaningless and overflowed every
cap. X4-B (2-of-4 debounced context votes) produced trip-scale segments
(24/12/9 per slice; median ≈ 57 min — still often 1–2 trips wide).
Variant disagreement is near-total (1 identical segment out of 24+4 on
slice A). Segmentation granularity and Stage-1 acceptance grain are a
*joint* design decision, not separable ones.

**H3 — The Stage-1 APC summary policy materially changes decisions.**
*Not on this fixture.* Zero decision deltas for A vs B vs C on all
slices (see R1-B03). The spike cannot distinguish the policies until
date-discrimination is the binding decision.

**H4 — Conservative uniform-fold resolution (XT-FOLD-A) suffices for
the DST hour.* *Contradicted for through-running vehicles.* All three
DST-slice vehicles have wall-time collision groups (two traversals of
01:00–01:59), the designed refusal fires, and 37 % of slice pings stay
unresolved, cascading to 76 % of APC records. The plan's promise that
"sequence evidence" resolves AVL folds needs a *monotone two-subsequence*
hypothesis family (each traversal a monotone pass), not uniform folds.

**H5 — Stage 3 catches wrong low-margin acceptances.** *Supported,
dramatically.* Under the forced variant (margin bar ~0.05 %), Stage 3
demoted **416/616** slice-A records and 14/24 segments; on slice B it
demoted 120 and rolled back one attachment-selected fold; on slice C it
demoted 78 and left 6 exact evidence ties. Sequence consistency is the
strongest discriminative signal observed all night — which is exactly
why it (or a feature derived from it) belongs *inside* Stage-1 scoring
rather than only as a post-hoc veto.

**H6 — Fixed-point scoring makes decisions layout-independent.**
*Supported.* See R1-B05.

**H7 — Null alternatives prevent forced choices.** *Supported.* PO/PI
(unmappable-label) segments abstain conservatively and resolve to an
explicit "date+feed but no route mapping" tuple only under permissive —
and 32 APC records then attach to those *route-less activity segments*,
a legal-but-questionable outcome the plan should decide on explicitly;
right-censored December-1 records are never forced onto a November
owner.

**H8 — B1 (unique hard-valid survivor) is a viable fallback.**
*Contradicted.* B1 accepts 25/616 (4 %) on slice A (574 records have
multiple surviving segments) yet accepts 226/363 (62 %) on slice B
where the bounded window leaves few segments — i.e., its behavior is an
artifact of slice geometry, not evidence strength. B1–B2 never
co-accept (B2 abstains where B1 accepts), so agreement analysis is
uninformative this round. None of this is a correctness claim.

## 6. Real edge cases found

- **Handled correctly** (conservative refusal or explicit state):
  day-scale X4-A segments (cap fail-closed); off-route/garage segment
  with all candidates hard-contradicted; PO/PI unmappable segments;
  right-censored month-boundary records — 297 APC records fall after
  the AVL fixture end, and censoring appears as growing gap-to-segment:
  the earliest still receive (stale-bounded) candidates while the tail
  gets `no_candidate` (20 records); none was forced onto a November 30
  owner; 3 cross-midnight segments;
  DST collision groups (unresolved, never defaulted); stop-code 16367-
  style unmapped stops (17 slice-A records carry `STOP.UNMAPPED`
  hypotheses); 868–2,010 stale candidate tuples hard-rejected.
- **Handled only under one assumption**: APC spatial residuals assume
  dominant-shape geometry per (date, route); date agreement assumes
  `source_date_key` = wall date (fixture-verified only).
- **Abstained on**: every mapped trip-scale segment (low margin, §5 H1);
  6 exact evidence ties (slice C forced run).
- **Could not represent**: multi-trip X4-B segments cannot select "two
  consecutive trips" — the candidate space has no trip-set alternative.
- **Could not compute within bounds**: none at slice scale (worst wall
  time 37 s, peak RSS 256 MiB), but component growth (3 vehicle-day
  components at a 7,200 s prefilter) signals month-scale risk.
- **Cannot resolve without TTC semantics**: direction/branch mapping
  (D-11) for direction-aware geometry and scoring; APC grain (D-06)
  untouched by design.

## 7. Determinism findings

For every slice: forward vs reverse vs seeded-shuffled window order,
24 h vs 12 h windows, and an identical-input double run all produce
byte-identical logical fingerprints (statuses, selected keys, qscores,
margins, dispositions, rollbacks) — `determinism_report.json:
identical=true` in all three run directories. The synthetic suite
additionally pins ID-swap invariance and exact-tie abstention.
Threading was not varied (the resolution engine is single-threaded
Python; polars' internal threading sits upstream of all decision
arithmetic, which is pure integer). Physical CSV reordering was not
tested; the determinism claim is scoped to identical normalized inputs,
config, and algorithm version.

## 8. Performance findings

| Slice | Wall (full sensitivity battery) | Peak RSS | Candidates (seg / APC) | Dominant stage |
| --- | --- | --- | --- | --- |
| A | 37.1 s | 183 MiB | 991 / 6,142 | segment-candidate geometry medians (25.1 s, computed once; each resolution pass ≈ 1.2 s) |
| B | 6.5 s | 230 MiB | 265 / 3,531 | candidate generation |
| C | 7.2 s | 256 MiB | 325 / 2,643 | candidate generation |

Each battery = 12 full resolutions (5 determinism layouts + double run
+ 2 variants + 2 policies + primary + forced). Largest structures:
3 whole-vehicle-day components (slice A); 52 hard-valid tuples on one
segment. No cap overflow at the final experimental caps (128/64/400);
21/24 segments would overflow at cap 24.

## 9. Legacy comparison

- **B0**: the legacy clean artifacts (`ttc_avl_seg_clean_data.csv`,
  `ttc_apc_clean_data.csv`) are **not present in this workspace**, so B0
  ran only as code against synthetic fixtures
  (`baseline_report.json: b0.available=false`, checked paths recorded).
  Its tested behavior reproduces the documented hazards: a 9,083 s stale
  backward match joins with no tolerance, missing load coerces to
  integer 0, fractional load truncates, and permuting `TripID` changes
  its output while leaving spike segmentation untouched. **B0 agreement
  is not ground truth and was not used as such.**
- **B1 vs B2**: reported in §5 H8; B1 accepts a superset of nothing B2
  accepts (B2 accepted zero records under the conservative primary), so
  the informative comparison awaits a discriminative Stage-1 feature
  set.
- **Human labels**: none exist; not evaluated.
- **Schema/TIDES conformance: not evaluated and not claimed.**

## 10. Recommended plan revisions (passage-level; not applied)

1. **S7 objective (§S7 "Objective structure")**: state that AVL-only
   interval/residual evidence is *known-insufficient* to separate
   adjacent trips on short-headway routes, and require at least one of:
   (a) per-stop scheduled-time alignment features inside the Stage-1
   score (a bounded version of S8's alignment), (b) reviewed
   direction evidence (blocked by D-11 today), or (c) an acceptance
   semantics that can resolve to a *headway-window trip set* with an
   explicit `resolved_headway_ambiguous` status instead of forcing
   single-trip margins.
2. **S4 (§S4 comparison protocol)**: record that the gap-only baseline
   yields day-scale segments on this fixture and cannot feed trip-grain
   matching; require the segmentation comparison to co-vary with the
   Stage-1 acceptance grain (H2), and specify the context-debounce rule
   (fields, votes, spans) that X4-B currently improvises. Note the
   found chatter edge: simultaneous one-ping chatter on two fields can
   still mint a boundary at the flip-back index.
3. **S3/S4 fold rule (§S3 "Time and DST")**: replace "continuity
   evidence produces a unique winner" with a *monotone two-subsequence*
   hypothesis family for the repeated hour (each traversal
   wall-monotone, spatially continuous), since uniform-fold selection
   provably cannot cover through-running vehicles and abstains on 37 %
   of DST-hour pings.
4. **Caps (§S7)**: express `segment_hypothesis_cap` relative to
   route-headway pressure (e.g. cap ≥ expected overlapping trips at the
   observed segment duration + expansion window), and document that the
   candidate-prefilter gap bound controls component size — the
   "components are small" claim must cite both numbers.
5. **§5 windowing**: fix the APC no-time-hypothesis ownership fallback
   wording (local vs UTC midnight) when it becomes normative.
6. **S7 stage 3**: specify the demotion→segment-weakening recompute
   precisely (this spike's all-attachments-demoted simplification is
   too coarse to freeze), and address enumeration truncation: tied
   optimum counts can be astronomically large, and the
   certain/ambiguous split is only trustworthy un-truncated.
7. **Stage-1 APC summary (§S7 stage 1)**: document that the summary can
   only discriminate date/feed hypotheses, never trips; if its purpose
   is date support, say so and scope its evaluation to date-ambiguous
   cases (cross-midnight, month edges).
8. **§6 artifacts**: add the APC rows of `time_resolutions`
   ("retained-unresolved / selected-by-attachment / rolled-back") to
   the artifact contract — the spike needed them to make Appendix B's
   fold-audit question answerable.

## 11. Recommended next implementation slice

The smallest spike that removes the most uncertainty: **Stage-1
discriminative evidence on slice A** — add (a) a bounded per-stop
scheduled-time alignment feature (schedule-side only, no APC) and (b) a
two-candidate direction-mapping hypothesis (destination first token ↔
`direction_id`, both polarities retained as hypotheses, no D-11 claim),
then re-measure the slice-A margin distribution between best and
second-best adjacent trips under both variants. Success criterion: the
margin distribution separates from the 0.07–2.5 % band by an order of
magnitude for unambiguous vehicle-trips, or it does not — either result
settles revision 1's direction. Secondary candidate: the monotone
two-subsequence fold experiment on slice B (measures how much of the
1,195-ping unresolved mass is actually recoverable).

## 12. Explicit non-claims

- No TIDES package, resource, or `datapackage.json` was produced; the
  writer structurally refuses them.
- No formal service date was resolved for publication; all resolutions
  are diagnostic and experimental.
- No approved threshold was chosen; every numeric parameter remains
  experimental and uncalibrated, and the `forced_diagnostic` variant is
  intentionally unsafe.
- No APC count semantics were inferred; passenger quantities never
  entered any identity, date, schedule, or attachment score (enforced
  and tested).
- No milestone (M0–M4) was accepted; no ADR was edited.
- **R1 remains open.**
