# Review: merge implementation plan (GTFS + AVL + APC → TIDES v1.0)

> **Disposition (2026-08-06):** accepted — see
> [ADR 0002](decisions/0002-merge-plan-review-disposition.md). Architecture
> approved; provisional non-publishable S0–S2 diagnostic work authorized in
> parallel with one R1–R6 revision; S3+ frozen pending section-scoped
> re-review of the changed sections.

- Document reviewed: [`merge-implementation-plan.md`](merge-implementation-plan.md),
  revision dated 2026-08-06 ("revised design for review")
- Review date: 2026-08-06
- Review method: every empirical claim in the plan's §2 evidence table was
  independently recomputed from the files under `database_offline/`; every
  TIDES v1.0 specification claim was checked against the pinned commit
  `b2cb2317…`; the plan was then reviewed through four lenses (consistency
  with the implemented codebase, consistency with governing documents,
  algorithmic soundness, delivery realism), and blocking findings were
  adversarially re-verified before inclusion here.
- This review changes no code and does not edit the plan.

## 1. Verdict

**Approve the architecture; require revisions before M1 starts.**

The plan's foundations are sound and unusually well evidenced: all ten §2
fixture measurements reproduce exactly, all eleven TIDES v1.0 claims are
correct against the pinned spec commit, the acyclic redesign genuinely removes
the earlier service-date/stop-mapping/attachment cycle, and the
capability-based publication profiles are the right structure for a project
whose external dependencies may never resolve. No finding invalidates the
design direction.

What blocks implementation is specification completeness, not architecture:
S7 (joint resolution) and the windowing rules are currently under-specified
to the point that two competent implementers would build observably different
pipelines, and the governance model requires named human owners who do not
exist, with no fallback — which, as written, makes every publish profile
unreachable by construction. Both are fixable in the document.

## 2. Evidence verification results

### 2.1 Fixture measurements (§2 table)

All ten claims were recomputed from the raw CSVs. Nine are **confirmed
exactly** (row counts, `Date_Key` equality on all 6,202,996 rows, 14,591 /
1,930 `(vehicle, tatripId)` group counts, 156/148/147 vehicle overlap,
`AvgSample` populations, zero payload duplicates / 450 visit-key collision
pairs, load-equation 567,627 / 5,969 / max 47, DST-hour 3,831 / 425 / 653,
and the 2023-12-01 02:14:41 / 2023-11-30 23:59:59 boundary timestamps).

One claim is **confirmed in number but overstated in wording**: exactly 1,105
groups span two `Date_Key` values, but only **266** of them contain a
consecutive-ping midnight crossing with a gap ≤ 60 minutes (identical count
at 5/15/30/60-minute thresholds). The other 839 are the same
`(vehicle, tatripId)` recurring on different days. The design consequence
(D-03: `Date_Key` is not a resolved service date) survives on the 266 genuine
crossings, but §2's causal phrasing "continue across midnight" should be
corrected to distinguish continuity from reuse.

### 2.2 TIDES v1.0 specification claims

All eleven checked claims are **correct** at commit `b2cb2317…`, which is
verified to be the peeled commit of tag `v1.0`:

- required-field lists for all five resources match the schemas exactly
  (including the easy-to-miss facts that `service_date` is *not* required in
  `vehicle_locations` and `trip_stop_sequence` *is* required in
  `passenger_events`);
- the `schedule_relationship` enum split (`vehicle_locations`: Scheduled /
  Skipped / Added / Missing; `trips_performed`: Scheduled / Added /
  Unscheduled / Canceled / Duplicated) is exact — the plan's refusal to write
  `Unscheduled` into `vehicle_locations` and its rejection of WMATA's
  `Scheduled else Added` default are both spec-correct;
- one scheduled trip may serve multiple performed trips (stated verbatim in
  the spec), the `trips_performed` primary key is
  `(service_date, trip_id_performed)`, resources are individually optional
  (profile requires ≥ 1 resource), `heading` permits 0–360 inclusive,
  `event_count` explicitly supports aggregate boarding/alighting events, and
  the official validator has the local `-l` mode the plan requires (the
  script is `bin/validate-datapackage`; its usage string has a `-v`/`-r`
  typo worth knowing about).

## 3. Required revisions (blocking before M1)

### R1 — Specify S7 to an implementable level (§S7, §5)

1. **Objective function.** S7 rests on one sentence: "The score combines the
   independent AVL baseline, APC attachment evidence, and ordered-stop/
   scheduled-time compatibility." The three evidence families are in
   incompatible units (seconds, meters, ratios, confidences). Specify the
   structure: per-family normalization (calibrated score functions or
   likelihood ratios), the combination operator, and the treatment of missing
   families — §11 already mandates separate AVL-only/APC-enriched regimes;
   S7 must reference them.
2. **Graph class and algorithm.** "Enumerate the pruned graph exactly or use
   an algorithm proven equivalent on the configured graph class" defines
   neither the graph class nor the decomposition. On fixture-scale
   vehicle-days (~10–18 segments, ~129 APC records, close 29/929 headways),
   naive exact enumeration is infeasible while a component-wise decomposition
   is easy — but the plan must say which. The "proven equivalent" bar and the
   per-decision counterfactual margins ("best alternative that changes each
   reported decision") are research-grade obligations for a 1–2 person
   prototype; recommend: exact enumeration under a hard per-component
   candidate bound with window-unresolved on overflow, and row-local margins
   plus hard-contradiction checks as the M4 baseline, with joint
   counterfactual margins as a stretch goal.
3. **Floating-point determinism.** §12.3 requires partition-invariant
   results, but near-tie float aggregation order can flip winners between
   partitionings. Specify score quantization (compare after rounding to a
   declared precision; below it, resolve by the deterministic key chain) and
   canonical aggregation order.

### R2 — Close the fold-resolution hand-off (S3 / S4 / S7)

S3 "does not choose winners", S4 "chooses one only when continuity evidence
produces a unique, calibrated winner" and may emit "alternative segment
hypotheses", and S7 emits `unresolved_time` — but S7's hard-constraint list
has no dimension for selecting among S4's alternative segment hypotheses, and
no stage owns cross-source fold resolution for APC. State explicitly: either
folds for AVL pings are finalized in S4 and never revisited (and document the
bias), or ambiguous-region alternatives enter the S7 graph as a selectable
dimension with a constraint. Additionally (§6, Appendix B): the fold/ordering
selection is identity-bearing (`sg2` hashes the "selected ordering
hypothesis") yet no artifact in the §6 table records which fold was selected,
with what score/margin — Appendix B's auditability requirement is currently
unsatisfiable. Add a time-resolution decision artifact parallel to
`segment_resolutions`.

### R3 — Fix the windowing and ownership rules (§5, S7)

1. "The configured maximum continuity gap is also the minimum partition
   overlap" does not bound a *segment's* extent (a chain of pings each within
   the gap is unbounded), so a long-duration segment can still be split by a
   window boundary. Either form segments per vehicle over the full run before
   windowing (windows then only batch downstream work — recommended), or
   define lookahead as extend-until-first-qualifying-gap.
2. "Owned by the window containing its first resolved instant" is undefined
   for segments whose time is unresolved. Define their ownership.
3. APC records have no cross-window ownership rule: the hard constraint
   "each APC source record attaches to at most one physical segment" is only
   enforceable within one window, and an APC record near midnight can have
   candidate segments in two adjacent windows. Add an APC ownership rule
   symmetric to segment ownership.

### R4 — Decide the mid-trip-exclusion policy (S8 / S9 / S10)

`trip_stop_sequence` must start at 1 and be consecutive, but the plan never
chooses what happens when a mid-trip APC record is individually excluded —
and exclusion is guaranteed to be common (45,226 `AvgSample>1` rows are
blocked by D-06; `ATTACH.LOW_MARGIN` rows likewise). Renumbering
misrepresents actual visit order; blocking discards whole trips. Specify the
rule (e.g., publish a trip's `stop_visits`/`passenger_events` only when the
resolved-visit set is complete between its first and last resolved visit;
otherwise the trip's stop-level resources are policy-excluded while
`trips_performed` may still publish). Similarly, assign a terminal owner for
APC intervals that remain inverted after time resolution (S2 defers repair;
no later stage disposes of them; as written S9 could derive negative dwell) —
e.g. `APC.INTERVAL.INVERTED` at the post-S7 boundary.

### R5 — Close the governance vacuum (§4, §16, §15, §17)

1. §16 requires "a named human owner" per open-blocking decision; the project
   has none ("Primary contact: to be assigned"). As written, M0 cannot exit
   and every publish profile is unreachable forever. Add a fallback: the
   project owner is the default owner for gates closable from internal
   evidence (with documented residual risk); reserve external ownership for
   decisions genuinely requiring TTC authority (D-06…D-09, D-13). Add this
   single-point-of-failure to the §17 risk register.
2. Classify each G0 gate as internally-closable vs external-authority-only.
   Today G0.2 (manifest approval) and G0.3 (time contract) have no defined
   approver, which silently couples even `avl-core-v1` to external input the
   profiles were designed to decouple.
3. Resolve the vehicle-identity gate contradiction: §3.2 gates `avl-core-v1`
   on "vehicle identity", G0.5 blocks "APC attachment and supporting
   `vehicles`", D-12 blocks "APC attachment" only. An AVL-only `vehicles`
   table from a single namespace needs no cross-namespace decision. Rescope
   (recommended: D-12/G0.5 block only the APC profile) and rewrite §3.2's
   gate column as explicit G0/D-xx ID lists so scope cannot drift again.
4. Make M2/M4 exit criteria decidable: "approved segmentation version" and
   "accepted precision and abstention approved" name no approver and no
   pre-registered bar. Pre-register the decision *procedure* at the prior
   milestone (named approver records a precision floor and abstention
   ceiling before holdout evaluation), even if the numbers come from
   calibration. Relatedly, make the single-labeler fallback in §11.1 the
   planned default with a mechanical conservatism rule, since the two-labeler
   protocol requires people who do not exist.

### R6 — Move governing-document alignment to M0 and enumerate it (§2, §15)

§2 requires alignment "before implementation starts"; the deliverable appears
only in M6 (and the two lists disagree: §2 names README/architecture/audit,
M6 names README/architecture/ADRs). Move alignment into M0 and attach the
concrete enumeration, which this review compiled:

| Passage to change | Conflicts with |
| --- | --- |
| `architecture.md` "Performed-trip segmentation" steps 1, 3, 6 (order by service-date candidate first; APC refines boundaries; segmentation mints `trip_id_performed`) | §1 acyclic order, S4, S7, §8 |
| `architecture.md` time contract: "AVL first uses `Date_Key`…"; "attach the America/Toronto timezone" at normalization; APC arrival/departure "midnight repair" at normalization | D-03, D-05, S1, S2 |
| `architecture.md`: "the current November fixture does not span the fall transition date" — **factually false** (fall-back was 2023-11-05; 3,831 AVL rows sit in the repeated hour) | §2 evidence, D-05 |
| `architecture.md` quarantine checks ("duplicate source record", "performed-trip assignment is ambiguous") and README "low-confidence data is quarantined" | §10's four-outcome taxonomy, S1 duplicate rule |
| `architecture.md` `match_status` = matched/ambiguous/unmatched; README quality metrics list | S7's six statuses (publish a crosswalk; also define scenario 18's "unclassified") |
| `architecture.md` "partitioned by source and `service_date` when available" for intermediates | S1/§5 source-calendar + vehicle-bucket partitioning (service date is an S7 *output*) |
| `architecture.md` `trip_id_performed` = "hash of service date, vehicle, and stable segment evidence" | §8's canonical-input table (`segment_id` indirection, versions) |
| README merge-strategy steps 3–5 (service date → match → then merge APC) and the open-decision cutoff question | §1 joint resolution, D-04 |
| README scope: "first useful package should contain" all five tables unconditionally | §3.2 profiles (APC resources are gated) |
| `source-data-audit.md` `Date_Key` row ("validate against timestamp and GTFS") | D-03 |

## 4. Advisory findings (fix during revision; non-blocking)

1. **§8 ID policy vs inherited GTFS IDs.** The blanket "at least 128 bits"
   rule contradicts the existing `r1-<24 hex>` (96-bit namespace) GTFS
   record IDs the plan builds on; the table grandfathers `p2` but is silent
   on `r1`. Add an explicit exemption row or scope the policy to new
   identifiers (an `r2` migration would invalidate all existing artifacts —
   not worth it now).
2. **§5 graph edges.** Add `gtfs→S8` (scheduled sequences/times for
   alignment), `S1→S4` (segmentation's soft evidence lives in normalized AVL
   payload, not in the S3 hypothesis artifact), and `gtfs→S10` (referential
   checks) — or enrich the intermediate artifact contracts.
3. **§2 wording.** Correct the 1,105 sentence per §2.1 above (266 genuine
   crossings; remainder is cross-day reuse — which the next table row itself
   anticipates).
4. **§10 module naming.** The boundary table reassigns the existing
   `reconcile.schedule` module name to a different responsibility and has no
   row for the existing GTFS feed-selection/schedule-derivation code. Rename
   the new stage (e.g. `reconcile.schedule_candidates`).
5. **S5/S6 geometry gap.** Spatial features assume stop/shape geometry that
   no existing matched artifact exposes (scheduled stop events carry no
   lat/lon). State that a geometry artifact must be derived from the
   normalized layer and define the "reliable geometry" predicate.
6. **§16/§4 register completeness.** G0.2 is the only gate with no D-xx row;
   G0.3 is only partially covered by D-05 (fold only — basis/precision/clock
   source have no register home). Add rows.
7. **§12.2 scenario gaps.** No golden scenario exercises D-12 (an
   APC-only/AVL-only vehicle that must not attach) or D-09's door-field
   suppression. Add both.
8. **§6 artifact table gaps.** Quality/conformance/run-summary reports are
   promised (§3.1, S10, S11) but absent from the table; no stage is named as
   minting `trip_id_performed` (S4 explicitly does not; presumably S7/S8 —
   say so). Also map §6's `source_record_id` naming onto the implemented
   `_record_id`/`_source_*` lineage convention to avoid a gratuitous rename.
9. **Proportionality trims** (keep integrity, cut cost): defer
   content-addressed stage resume entirely (delete the §12.3 resume test;
   full reruns are cheap at this scale); collapse the nine per-stage
   algorithm versions into one pipeline version plus `p2` until stage-level
   cache stability is actually needed; default per-threshold "owner" to the
   project owner except where a threshold encodes claimed TTC semantics;
   restate the M6 performance criterion as what it is (a recorded, reviewed
   baseline — the measure-then-approve loop is not a pass/fail gate).
10. **§14 additions.** Record the current fixture's acquisition
    source/date/terms in the register (D-13 covers future redistribution but
    not present possession); extend the trace-exposure risk to operator
    re-identification, not only passengers.
11. **§8 ID stability scope.** Document that configuration changes may
    legitimately change derived IDs for affected segments (the current text
    lists only re-exports and algorithm versions), and that cross-run ID
    comparison requires matching config hashes.

## 5. Strengths worth preserving

- **Evidence-driven revision.** Every §2 number reproduces exactly against
  the raw fixture; the plan's redesign decisions trace to falsifiable
  measurements rather than assumptions.
- **Spec fidelity.** The §S9 conformance table is exact against the pinned
  schemas, including the subtle enum split that invalidates WMATA's
  `Scheduled else Added` default — and the pin itself is the real peeled
  v1.0 commit.
- **Correct WMATA reuse posture.** Appendix A reuses invariants
  (deterministic tie-breakers, consecutive visit numbering, quality-flag
  marts) while rejecting non-transferable semantics (4 a.m. cutoff, CAD
  trust, duplicate summing) with accurate citations to the actual SQL.
- **The profile ladder.** `diagnostic` / `avl-core-v1` / `apc-enriched-v1`
  with fail-closed gates is the right decoupling for possibly-unresolvable
  external dependencies; the APC grain gate (D-06) correctly blocks the most
  dangerous silent error (publishing averaged data as observations).
- **DST rigor.** Both-fold retention with sequence-based resolution and a
  hard rule against `fold=0` defaults is unusually careful, and the fixture
  genuinely contains the fall-back hour to test it (contrary to the current
  architecture doc).
- **Honest-claims posture.** Unmeasured performance promises are refused,
  observed rates are baselines rather than targets, and month-edge censoring
  is reported rather than forced.
- **Consistency with the implemented stage.** Claims about existing code are
  accurate (`p2-<32 hex>` patterns, approved-only selection with
  `--allow-candidate`, source-boundary write protection; 92 tests green).

## 6. Implementation cautions surfaced during verification

- Polars `dt.month()`/`dt.day()` return `Int8`; arithmetic like
  `month*100` silently overflows. The verification scripts hit this
  (11 × 100 wraps mod 256) before casting to `Int64`. Normalization code
  computing date keys must cast before arithmetic — a good unit-test case.
- The TIDES validator script is `bin/validate-datapackage`; its usage string
  advertises `-v` for the remote ref but the implemented flag is `-r`.
- APC `StopDepartureTime` yields 427 rows in the DST hour vs 425 by
  `StopArrivalTime`; any DST accounting must state which column it counts.

## 7. Disposition

The plan should be revised once, addressing R1–R6 (blocking) and as many
advisory items as practical, then re-reviewed at the level of the changed
sections only. The architecture, evidence base, profile structure, decision
register, and test strategy do not need to be revisited. M1 (landing and
normalization) could reasonably begin in `diagnostic` mode in parallel with
the plan revision, since no R-item changes the S0–S2 contracts; everything
from S4 onward should wait for the revision.
