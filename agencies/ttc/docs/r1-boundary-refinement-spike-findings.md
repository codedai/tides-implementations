# R1 Boundary-Refinement Spike Findings

Spike 02: APC-assisted boundary refinement, terminal episodes, and GTFS
anchor-and-align. Experimental, diagnostic, non-publishable. Canonical
run id `s2b` (2026-08-08, produced after a five-lens adversarial review
whose confirmed fixes are folded in); code in
`experiments/r1_design_spike/src/r1spike/` (`spike02_*` and companion
modules), algorithm version `x-r1-spike02-1`.

## 1. Executive result

The experiment **supports terminal-specific two-pass segmentation and
GTFS anchor-and-align, with major caveats it also measured**:

- **Per-stop and terminal anchor evidence breaks the Spike-01 margin
  wall.** Under the same conservative thresholds where Spike 01
  accepted zero mapped segments (top-2 margins 743–25,392 q, 0.07–2.5%
  of scale), the anchor layer accepts **16 of 36 refined segments**.
  The decided-segment margin median is 46,068 q — about 1.8× Spike
  01's *maximum* and roughly 7× its median — and margins reach
  100,000 q (211,340 q with the deliberately honest dual-polarity
  direction handling disabled; see §10 for why direction caps the
  ceiling). Close-headway abstention is no longer universal.
- **One-pass APC-assisted refinement produces trip-scale cuts, but
  buys them with fragmentation.** Vehicle 7237's 15-hour coarse
  activity run splits into trip-scale segments, and Q3 APC evidence
  narrows terminal cuts in the physically expected direction; but 14
  of 36 V2 segments are sub-10-minute fragments, and candidate
  multiplicity at a fixed temporal bound *rises* (more segments per
  unit time), so H4's "refinement reduces ambiguity" claim is
  **contradicted at fixed bounds** — the real reduction comes from
  narrower `T_mid` bounds, which have a measured safe region (§8).
- **The improvement disappears when acceptance rests on exclusion
  rather than evidence.** The DST regression slice produced 49
  conservative-threshold acceptances that are unique-survivor
  artifacts: every one has exactly one hard-valid candidate — a single
  247-second fold-clean segment absorbing records spanning 37 minutes
  — because every alternative was hard-blocked. 44 of the 49 have zero
  interval overlap with that segment; 5 fold-resolved records are
  genuinely contained in it, so a blanket "all false" label would also
  be wrong — the point is that the *margins* (150,078–529,643 q,
  computed against the null) certify nothing when the alternative set
  was structurally emptied. This is the sharpest design risk the spike
  surfaced.
- Representation verdict: the evidence supports a **hybrid** of
  trip-scale decomposition and trip-sequence alignment (§12) — refined
  segments anchor to scheduled trips convincingly, but eleven of the
  sixteen anchored segments are fragments of their trips, so a pure
  one-segment-one-trip reading is not defensible.

**R1 remains open.** No milestone is accepted, nothing here is a
production S3–S8 implementation, and no threshold is approved.

## 2. Authorization and non-claims

This spike is experimental, diagnostic, and non-publishable
(`profile = diagnostic-experimental-r1-spike-02`, `publishable =
false`, `tides_conformance_claimed = false`,
`normative_contract_claimed = false`, `r1_closed = false`,
`production_modules_modified = false`, `authorization_basis =
user-requested isolated design-validation experiment`). It produced no
TIDES output, accepted no milestone, inferred no formal source
semantics (D-06, D-07, D-10, D-11 all remain open), and selected no
approved thresholds. The approved merge plan is **acyclic**; its
Appendix C explicitly flags "APC refines segment boundaries" as
conflicting with the approved order. Everything below therefore
evaluates a *candidate plan revision* under a hard one-pass
restriction, not the approved architecture.

## 3. Inputs and reproducibility

- Normalized S0–S2 inputs: `artifacts/diagnostic-2026-08-06/normalized`
  (tree fingerprint `08224b483bf28eb5…`, names+sizes fingerprint,
  identical to the Spike-01 `r1c` manifests — inputs unchanged).
- GTFS artifacts: the seven per-date artifacts of Spike 01 (feeds
  `ttc-d092742e6a5b`, `ttc-de8294439596`; candidate-manifest override,
  D-15 open).
- Spike-01 frozen baseline: `artifacts/experimental-r1-spike-r1c-*`
  (config `1833fb11…`, contract `8f66732c…`, code rev `0826af04…`).
  A full Spike-01 battery re-run after all Spike-02 code was in place
  reproduced the frozen normal-day logical checksum byte-for-byte
  (`9ccc2c0ce020ae14…`) — Spike-01 behavior is untouched.
- Spike-02 run `s2b`: merged score-contract hash `468a1fa313b5249d…`,
  code revision `0826af04ffef…` plus uncommitted spike files, Python
  3.12/uv, Linux.
- Slices (identical definitions and deterministic vehicle queries as
  Spike 01): normal_day 2023-11-28 vehicles 1011/7237/7257 (7,275 AVL,
  616 APC); dst_fall_back vehicles 1267/7261/9019; month_boundary
  vehicles 9030/9033/9034.
- Runtime / peak RSS: normal_day 193.8 s / 287.9 MiB (attachment
  battery 92.4 s and anchors+alignment 40.9 s dominate); dst 18.1 s;
  month 20.0 s (both peak 337.1 MiB, inherited from the same process
  in sequence).
- Baseline reproduction (§22 of the brief): recomputed from the frozen
  `r1c` normal-day parquet — dispositions {policy_blocked: 616},
  candidate rows 6,142, per-record distinct candidate-segment counts
  {1: 25, 3: 45, 4: 192, 5: 291, 6: 61, 7: 2} at the 7,200 s
  generation prefilter, X4-B 24 segments / nearest-rank median 3,420 s
  / max 4,695 s — **all comparisons matched** (`all_matched: true`),
  so every V0 comparison below stands on reproduced numbers.

## 4. What was implemented

**Completed:** APC source-signal derivation from count positivity with
Q0–Q3 tiers; provisional X4-A activity runs with censoring flags;
versioned terminal-cluster hypotheses from GTFS pattern endpoints with
the experimental 1652=Wilson association (D-10 open, provenance
recorded); trip-agnostic APC–AVL alignment edges; terminal/short-turn
episodes with four timestamp hypotheses and ordering checks; one-pass
boundary refinement in three variants (V1 AVL-only, V2 conditional Q3
precedence, VX intentionally unsafe unconditional) with full ping
conservation; spanning-record dual roles; adaptive attachment with
`T_mid` 20/30/45/60 min, a terminal path, and the experimental cluster
stop rescue; GTFS anchor candidates with terminal residuals, per-stop
occurrence alignment, and dual polarity hypotheses; bounded
order-preserving anchor-and-align with exact second-best path cost and
a many-to-one variant; full determinism matrix; 62 new tests (282
spike total, plus the unchanged 109 production tests) covering the 40
required scenarios.

**Adversarial review:** a five-lens review (code correctness/leakage,
boundary/ownership semantics, metric accuracy, safety compliance,
recall-vs-ambiguity; 34 verification agents) confirmed one genuine
code bug — applied *instantaneous* cuts (short-turn boundaries and
cuts falling between two pings) were silent no-ops — plus seven minor
code/artifact defects and a set of report inaccuracies. All were
fixed, two regression tests added, and the whole battery re-run as
`s2b`; every number in this document comes from the post-fix
artifacts.

**Partial:** the episode builder implements seven of the eight listed
evidence families (stop-sequence-restart only implicitly, via
direction/progress evidence); the `ownership_projection = departure`
turnaround experiment is represented in the spanning-role artifact but
no separate duration-shift report was produced.

**Deliberately deferred:** V5 (coarse-run-to-trip-sequence
comparator); automatic turnback-cluster minting from repeated dwell
locations; per-branch geometry (blocked by D-11 regardless).

**Blocked:** confirmed count semantics (D-06), the 1652 crosswalk
(D-10), and direction semantics (D-11) block any non-hypothetical
version of the signal states, the Wilson cluster, and the polarity
features respectively.

## 5. Provisional alignment findings

Trip-agnostic alignment was cheap (0.1 s) and informative before any
ownership decision: 595 edges covering 595 of 616 normal-day records
(21 records have no run within the 900 s bound — the coverage-hole
tail, which can never gain episode or terminal-path evidence), each
edge carrying bracketing pings, interpolated position residuals,
terminal membership, and the signal state. The edges were sufficient
to drive episode detection without ever asserting a segment owner.
The double-counting exposure this creates is quantified in §13.

## 6. Terminal/turnback findings

Normal day: 45 terminal episodes + 1 short-turn candidate; 32
selected, 9 insufficient-evidence (recorded, not applied), 5
ambiguous. **20 of 45 terminal episodes carry all four timestamps**;
25 are partial (`XR2.EPISODE.PARTIAL_TIMESTAMPS`); 5 ordering
violations were retained as ambiguous with alternatives — never
reordered. 15 episodes reached Q3 APC quality; 11 spanning records
were detected at episode level, 8 of which produced dual-role evidence
at applied boundaries. The four-timestamp model is expressible on real
data and the ordering constraint is violated rarely but genuinely.

The APC-vs-AVL boundary comparison on the five episodes where both a
physical buffer and a Q3 service cut exist is physically coherent:
service_end trails physical arrival by **21–44 s** (alighting
completes just after arrival) and service_start precedes physical
departure by **15–146 s** (doors close before pull-out). Small n, but
every shift points the physically expected way.

A false-boundary mode was found and fixed during testing: sustained
outbound motion trivially co-signs a drive-through transit, so the
outbound family now requires a real stop (dwell, or a visit span ≥
300 s). This gate is itself experimental.

DST slice: 23 terminal episodes, 9 ambiguous — fold-unresolved visits
correctly produced **no physical timestamps and no boundaries** (zero
unresolved-boundary pings under V2; the fold population is untouched).
Month slice: 9 episodes, 6 selected, censoring flags preserved.

## 7. Boundary-refinement findings (V0/V1/V2/VX)

Normal-day segmentation, all conservation invariants passing
(`conservation_ok: true` everywhere; no ping in two owners). Boundary
decision counts are from `x_boundary_decisions.parquet`:

| Variant | Segments | <600 s | 600–1799 s | 1800–3599 s | ≥3600 s | Turnarounds (median s) | Applied / ambiguous / below-bar decisions | Unresolved-boundary pings |
|---|---|---|---|---|---|---|---|---|
| V0 (x4b baseline) | 24 | – | – | (median 3,420) | max 4,695 | – | – | – |
| V1 AVL-only | 25 | 5 | 4 | 11 | 5 | 16 (687.5) | 22 / 9 / 15 | 78 |
| V2 conditional | 36 | 14 | 6 | 13 | 3 | 31 (117.0) | 32 / 14 / 0 | 0 |
| VX unconditional | 41 | 15 | 8 | 17 | 1 | 30 (74.5) | 38 / 8 / 0 | 229 |

- **H1 (refinement reaches trip scale): partially supported.** The
  provisional runs (up to 15 h) become trip-scale segments, and V1
  lands within one of the x4b count (25 vs 24) from independent
  evidence. But V2's additional splits come with 14 sub-600 s
  fragments, 12 of them sandwiched between two applied boundaries —
  consecutive terminal visits chopping layover areas — so duration
  medians alone overstate success.
- **H2 (APC adds information): supported, with a fragmentation tax.**
  V2 narrows turnaround intervals sharply (median 687.5 s → 117.0 s,
  consistent with service-time semantics) and applies boundaries at 15
  episodes V1 does not (V1 applies at 5 episodes V2 does not; 32 vs 22
  applied decisions overall). The added splits are also the main
  fragment source.
- **H3 (conditional safer than unconditional): supported.** VX applies
  boundaries on weak or insufficient evidence (test-verified
  divergence) and produces the largest unresolved-boundary ping
  population (229, vs 78 for V1 — whose physically-gated boundaries at
  APC-conflicted episodes leave those visit pings unresolved — and 0
  for V2); its extra segments also raise attachment multiplicity
  (5-way: 223 vs 77 under V2 at T_mid 3,600 s). Nothing suggests
  unconditional precedence is worth that.

## 8. Adaptive-bound findings (T_mid 20/30/45/60 min)

On V2 segments, conservative contract, candidate sets identical at
generation (prefilter 7,200 s), pruning applied afterward and logged.
"Lost nearest" counts records whose *nearest-gap* candidate was
pruned — a deliberate proxy; candidate scores are not consulted:

| T_mid | Multiplicity (distinct hard-valid segments/record) | Pruned candidate rows | Records losing their nearest candidate |
|---|---|---|---|
| 1,200 s | {0: 2, 1: 297, 2: 261, 3: 47, 4: 9} | 5,390 | **2** (0.3%, both to no_candidate) |
| 1,800 s | {1: 94, 2: 369, 3: 102, 4: 51} | 4,688 | 0 |
| 2,700 s | {1: 5, 2: 247, 3: 191, 4: 140, 5: 29, 5+: 4} | 3,768 | 0 |
| 3,600 s | {2: 104, 3: 232, 4: 158, 5: 77, 5+: 45} | 2,968 | 0 |

**H7 supported with a measured safe region.** T_mid = 1,800 s removes
4,688 gap-only candidate rows and eliminates the 5-way tail without
losing a single nearest-segment candidate; 1,200 s costs two records
their nearest owner. The recommendation is a **calibration range of
1,800–2,700 s**, not a final value. The terminal path retained
episode-adjacent candidates for 50 records; separately, the cluster
stop rescue (its own reason code, `XR2.ATTACH.CLUSTER_RESCUE`) gave
all 17 unmapped stop-1652 records hard-valid candidates. Rescue
candidates are not T_mid-protected: the Wilson records' owners
survived every tested bound because their gaps are small, not because
of a guarantee — a distinction any production rule should make
explicit.

## 9. Attachment findings

- Conservative contract: normal-day dispositions are unchanged from V0
  ({policy_blocked: 616}) for every variant at T_mid ≥ 1,800 s (at
  1,200 s, two records shift to no_candidate) — Stage-1 interval
  evidence still abstains on close headways, so Stage-2 stays gated.
  The discriminative gain lives in the anchor layer (§10), which is
  where acceptance now belongs.
- forced_diagnostic (intentionally unsafe, exists to exercise the
  stages): V0 accepted 158 / demoted 432; V1 accepted 239 / demoted
  337; V2 accepted 179–180 / demoted 396–435 across T_mid values; VX
  accepted 217 / demoted 394. **H5 (fewer demotions) is contradicted
  for V2 and only weakly supported for V1** — demotion pressure
  migrates rather than disappears, and V2's fragments re-create
  sequence inconsistencies.
- **H4 (refined boundaries reduce multiplicity): contradicted at fixed
  bounds.** At the same 7,200 s prefilter, V2's per-record
  candidate-segment distribution is heavier than V0's ({5+: 326} vs
  {5+: 63}) because refinement creates more, shorter segments per unit
  time. All real reduction comes from T_mid (§8) and must be
  attributed to the bound, not the boundaries.
- Terminal vs mid-route: the 62 terminal-cluster records have *higher*
  median multiplicity (4) than mid-route records (3) — terminal
  records remain the hard class, which is why they get their own path.
- **DST slice false certainty (the spike's most important negative
  result):** 49 records accepted under the conservative contract, all
  with `n_hard_valid = 1`, all onto one 247-second fold-clean segment,
  with arrivals spanning 37 minutes. 44 of the 49 have zero interval
  overlap with the segment (median gap 570 s); 5 fold-resolved records
  are genuinely contained (gap 0), so some acceptances may be correct
  — but the margins (150,078–529,643 q, against the null) certify
  nothing, because every alternative candidate was hard-blocked
  (time-uncertain segments and Stage-1-unresolved segments). Full
  dispositions: 49 accepted / 3 low_score / 4 no_candidate / 240
  policy_blocked / 67 time_unresolved — compare r1c's 277
  time_unresolved and zero acceptances. Refinement *converted*
  protective abstention into confident-looking unique-survivor
  acceptances. A production margin rule must count hard-blocked
  alternatives (sole-survivor-by-exclusion ⇒ abstain).
- Month slice: right-censoring preserved — nothing after the AVL end
  attaches. The r1c 20 no_candidate becomes 142 here (307
  policy_blocked) purely because the explicit T_mid filter removes
  3,600–7,200 s gap-only candidates at generation-visible level
  instead of hard-invalidating them at scoring; zero acceptances
  either way.

## 10. GTFS anchor-and-align findings

Anchor decisions over the 36 V2 segments (conservative thresholds,
absolute 550,000 q / margin 60,000 q):

- **16 accepted anchors, 18 low_margin, 2 no_candidate.** Margin
  distribution across the 34 decided segments: min 723, median
  46,068, max 100,000 q. Against Spike 01's 743–25,392 q band on the
  same slice, the median is ~1.8× the old *maximum* (and ~7× the old
  median of 6,409 q). **H6 supported**: close-headway (5–10 min)
  segments no longer universally abstain, and the effect comes from
  per-stop residual medians and terminal residuals, not from direction
  assumptions (see ablation). The new 723 q minimum belongs to a
  fragment segment created by the short-turn split — fragments remain
  hard even for anchors.
- Direction ablation: disabling direction changes **zero acceptance
  decisions** (16 anchors either way) but raises the margin ceiling
  from exactly 100,000 q to 211,340 q (median 42,153 without
  direction). The 100,000 cap is the direction family's full weight:
  under dual-polarity honesty the runner-up of a winning trip is the
  *same trip under the opposite polarity*. Direction evidence, handled
  honestly under D-11, currently subtracts margin headroom instead of
  adding discrimination. Notably, **all 16 anchors independently
  selected `pol_b`** (NORTH→1/SOUTH→0) — an observed consistency worth
  carrying into the D-11 review, not a resolved semantic.
- Anchored segment durations are bimodal: **5 trip-scale
  (2,165–3,301 s) and 11 fragments (324–953 s)** that anchor to a full
  scheduled trip on stop/terminal evidence. The anchor layer is
  therefore matching *fragments of performed trips* to trips — useful,
  but fatal to a naive one-segment-one-trip reading (§12).
- Ownership exposure of the anchor gate: taking each record's
  *nearest* hard-valid candidate segment, 226 records' nearest owner
  is an accepted anchor, 382 sit on low_margin segments, and 8 on the
  two no_candidate segments — i.e. an anchor-gated Stage 1 would today
  leave roughly 63% of records on segments that abstain. Any follow-up
  must measure orphaning under that gate, not only anchor survival.
- Anchor-and-align: single optimal path for every vehicle (no tied
  intervals on this slice), path margins 4,744–10,536 q, alignment
  double-run identical. Vehicle 7237: 13 anchors + 9 DP-matched + 4
  unmatched over 26 segments, 217 scheduled trips correctly skipped
  (run by other buses). The many-to-one variant (TIDES-permitted)
  turns 3 of 7237's 4 strict-variant unmatched segments into
  additional references to already-matched trips — consistent with the
  fragment story — at a much smaller path margin (723 q), i.e. the
  many-to-one reading is plausible but not yet well-separated. **H8
  partially supported**: the path is stable and decisive here, but
  with 13 of 26 assignments pinned by anchors on the key vehicle the
  DP had limited freedom; nothing indicates bad-anchor propagation
  (anchors and DP matches never conflicted).

## 11. Known difficult cases

**Vehicle 7237 / Wilson Station (stop-code 1652, D-10 open), record
`c1-6ffadf82dacf106a1388da7d30df0446-563860`** (arrival 10:32:57,
departure 10:46:18, 801 s, the mandatory regression case): the episode
is SELECTED with all four timestamps distinct — physical arrival
10:32:21, service end 10:32:57, service start 10:46:18, physical
departure 10:48:44 — i.e. the record's own interval endpoints became
the service-time hypotheses through Q3 promotion. The record carries
`spanning_terminal_visit` dual roles with distinct inbound
(`xrs2-6f24…`) and outbound (`xrs2-553d…`) segment candidates and no
count duplication. The **outbound** segment is an accepted anchor
(margin 100,000 q, trip 46536211, route 929); the inbound segment is
low_margin at 38,855 q — above the entire Spike-01 band but below the
conservative bar. The sibling long-dwell record `…564586` (896 s,
14:49:47→15:04:43) shows the **mirrored** pattern: its **inbound**
segment is the accepted anchor (trip 46536320, margin 100,000 q) while
its outbound segment is low_margin at 36,373 q. What Spike 01 could
only call "a policy question" now has a concrete mechanism: the
spanning record's arrival and departure anchor the two trips
separately, and the turnaround between them belongs to neither. Under
the conservative Stage-1 gate the records' final attachment
dispositions are still policy_blocked — attachment acceptance awaits
an anchor-aware Stage 1.

Other retained hard cases: the 5 ordering-violation episodes; the 4
APC/AVL conflict episodes (retained as ambiguous under V2; V1 still
applies its physical buffer, leaving those visit pings
boundary-unresolved); the single short-turn candidate (progress
reversal + dwell outside clusters — whose instantaneous cut exposed
the no-op bug the review caught); and the DST unique-survivor
population described in §9.

## 12. Architecture decision matrix

| Architecture | Verdict | Evidence |
|---|---|---|
| A. Activity run → trip-scale segments → one scheduled trip each | **PARTIALLY SUPPORTED** | Trip-scale cuts and 16 conservative anchors exist; but 11/16 anchors sit on sub-16-minute fragments and 14/36 V2 segments are fragments — one-to-one segment↔trip does not hold without repair. |
| B. Activity run → ordered scheduled-trip sequence | **PARTIALLY SUPPORTED** | The order-preserving DP is stable, decisive, and cheap; but it needed anchors (from A-style evidence) to be meaningful — pure sequence alignment without per-stop anchors was not tested, and Spike 01 showed interval evidence alone cannot seed it. |
| C. No reliable scheduled identity for this source combination | **CONTRADICTED** | 16 conservative-margin anchors on a 5–10-minute-headway route, on real data, with direction evidence disabled. |
| D. Hybrid: refined segments + anchor set + bounded sequence alignment, with many-to-one trip references for fragments | **SUPPORTED** | Every strong result used exactly this composition; the many-to-one variant absorbs the fragment problem the pure-A reading cannot (though its path margin of 723 q on vehicle 7237 shows the reading is not yet well-separated). |

## 13. Design risks and double counting

The same APC records act as (1) boundary evidence (66 records feed
episode timestamps), (2) attachment candidates whose terminal-path
retention *depends on the boundary they helped define* (50 records),
and (3) per-stop anchor alignment inputs for the segments those
boundaries created. The spike kept (3) structurally independent of
scored attachment (visits are containment-based, never
attachment-selected), but (1)→(2) is genuine self-reinforcement: an
APC record that defined service_end will, by construction, sit
adjacent to the resulting boundary and therefore survive the terminal
path. Nothing in this spike treats that as independent confirmation,
and no production design should either. The DST unique-survivor
result (§9) is the same risk in temporal form: structural exclusion of
alternatives masquerading as margin. Both belong in any S7 revision as
explicit invariants ("an attachment margin must count hard-blocked
alternatives"; "boundary-defining APC evidence cannot be cited as
attachment confirmation").

## 14. Determinism and performance

- Refinement: double-run and shuffled-run-order fingerprints identical
  on all slices.
- Resolution: forward/reverse/shuffled window orders and 12 h windows
  produce byte-identical logical checksums (`603efa61a56c5ce5…` on
  normal_day); double runs identical.
- Anchor-and-align: double-run identical.
- `determinism_identical: true` in all three `s2b` run manifests. No
  mismatches anywhere. (The `s2a` pre-fix run was also fully
  deterministic; its artifacts were superseded and deleted.)
- Performance: normal_day full battery 193.8 s wall / 287.9 MiB peak
  RSS; dominant costs are the attachment battery (92.4 s — four
  segment sets × T_mid × contract grid, each re-running Spike-01
  candidate generation) and anchors+alignment (40.9 s, per-stop DP).
  Regression slices ~18–20 s each. Comfortable for a spike; a
  production design would cache candidate generation across the grid.

## 15. Edge cases not handled

- Fold-unresolved terminal visits produce no boundaries by design; the
  1,195-ping DST unresolved mass is untouched (the monotone
  two-subsequence fold family recommended by Spike 01 remains the
  missing piece).
- The unique-survivor acceptance mode (§9) is *detected* but not
  *prevented* — no margin rule counting blocked alternatives exists
  yet.
- Fragment repair: nothing merges sub-600 s fragments back into their
  trips; the many-to-one alignment variant only papers over it at the
  matching layer.
- Stop-sequence-restart evidence, automatic turnback-cluster minting,
  and the departure-ownership projection report are unimplemented.
- Terminal clusters cover only GTFS pattern endpoints of the slice
  routes; cross-route terminals used by interlined vehicles and any
  terminal absent from the feeds are invisible; ambiguous
  (equidistant) cluster membership abstains.
- Records with no activity run within 900 s never align (21 on the
  normal day) — APC-only vehicles and coverage holes stay unresolved,
  correctly but unhelpfully.
- The plausible-owner-loss metric counts nearest-gap candidates only;
  a best-scoring-owner variant is unimplemented (on this data the two
  coincide, which is why the proxy was acceptable here).
- The 1652=Wilson association remains a coordinates-plus-text
  hypothesis (D-10); a feed/date-versioned crosswalk review is still
  required before any non-experimental use.

## 16. Recommended plan revisions (passage-level; the normative plan is not edited)

1. S4/S7: if boundary refinement is adopted, adopt it as specified
   here — one pass, episode-gated, conditional Q3 precedence, ping
   conservation — and amend the acyclic-order passage (Appendix C
   conflict) explicitly rather than implicitly.
2. S7 margins: require the runner-up computation to account for
   hard-blocked alternatives (a sole survivor whose competitors were
   time-uncertain or otherwise structurally excluded must abstain, not
   accept). This single rule would have prevented the 49 DST
   unique-survivor acceptances.
3. S5/S7: adopt per-stop scheduled-time residuals and terminal
   residuals as first-class Stage-1 features (they, not interval
   residuals, produce usable close-headway margins), with the S8-style
   occurrence alignment they require.
4. S6: adopt a mid-route temporal bound calibrated in the
   1,800–2,700 s range plus a terminal-specific path; require a
   plausible-owner-loss metric (state whether it is nearest-gap or
   best-scoring) alongside any multiplicity-reduction claim.
5. D-11 review: carry the observed all-16-anchors-agree-on-`pol_b`
   consistency as input evidence; until resolved, direction features
   should stay out of scoring (they cap margins without changing
   decisions).
6. TIDES construction: plan for many-to-one performed→scheduled
   references (fragments), which TIDES permits and the fixture
   requires.

## 17. Recommended next spike

**Anchor-aware Stage 1 with blocked-alternative margins, on all three
slices.** The smallest experiment that would reduce the most remaining
uncertainty: feed the anchor feature families into the actual Stage-1
decision (replacing the interval-only contract used by `resolve()`),
implement the blocked-alternative margin rule, and measure (a) how
many of the 16 anchor acceptances survive as end-to-end attachments,
(b) how many records are orphaned by the gate (today 382 of 616
records' nearest owners are low_margin segments and 8 sit on
no_candidate segments), (c) whether the DST unique-survivor
acceptances all flip to abstentions, and (d) whether fragment segments
can be detected at Stage 1 (duration-ratio + partial-coverage
features) rather than repaired downstream. Either outcome settles
whether the hybrid architecture (§12) can be specified normatively.

## 18. Explicit non-claims

R1 remains open. This spike does not close R1; does not accept M0–M4;
does not produce or validate any TIDES resource; does not claim
conformance; does not confirm APC count-field semantics (D-06), the
visit-key collisions (D-07), the 1652 crosswalk (D-10), or direction
semantics (D-11); does not approve any threshold, geofence radius,
quality tier, or bound used here; does not establish the
four-timestamp terminal model, the Q tiers, or one-pass refinement as
normative contracts; and does not modify the approved plan or ADR
0002. The `forced_diagnostic` score variant and the VX refinement
variant are intentionally unsafe measurement instruments and must
never be promoted.
