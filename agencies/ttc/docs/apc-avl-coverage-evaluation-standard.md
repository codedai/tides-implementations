# APC–AVL coverage study: TIDES-based evaluation standard

Date: 2026-09-08. Status: diagnostic research specification, version
`apc-avl-coverage-v1`. The machine-readable companion is
[`tides_rubric.json`](../experiments/apc_avl_coverage_study/tides_rubric.json).

This standard evaluates whether a segmentation × attachment combination
preserves source evidence, makes supportable claims, and is practical to run.
It does not turn agreement between two noisy sources into independent truth.
The new user request authorizes a separate raw-data study and method comparison;
it does not alter the old blind-label artifacts or execute their frozen
factorial, and it does not publish a production TIDES package.

## 1. What TIDES requires, and what the study adds

The normative reference is TIDES tag `v1.0`, commit
[`b2cb23178e00cb58dbdb1b9a5a8626818278ecbb`](https://github.com/TIDES-transit/TIDES/tree/b2cb23178e00cb58dbdb1b9a5a8626818278ecbb/spec).
The official [versioned tables](https://tides-transit.org/v1.0/tables/) and
[architecture](https://tides-transit.org/v1.0/architecture/) were checked for
this study. The existing clean upstream checkout at
`/tmp/tides-v1-review.d11hA2/repo` independently returned that commit for both
`HEAD` and `v1.0^{}` with upstream origin
`https://github.com/TIDES-transit/TIDES.git`. The JSON records the seven inspected
schema/profile SHA-256 hashes so this verification does not depend on that
temporary checkout surviving. Upstream TIDES is attributed under its
[CC BY 4.0 license](https://github.com/TIDES-transit/TIDES/blob/b2cb23178e00cb58dbdb1b9a5a8626818278ecbb/LICENSE-CC-BY-4.0).

| Resource | Pinned required fields | Implication for this study |
| --- | --- | --- |
| `vehicles` | `vehicle_id` | Preserve source namespace and equivalence evidence. |
| `vehicle_locations` | `location_ping_id`, `event_timestamp`, `vehicle_id` | Trip ID and service date can remain absent. Real valid GPS records remain useful without a completed trip reconstruction. |
| `trips_performed` | `service_date`, `trip_id_performed`, `vehicle_id` | A physical segment is not sufficient: resolve actual trip identity and service date before publishing this resource. |
| `stop_visits` | `service_date`, `trip_id_performed`, `trip_stop_sequence` | A fragment-local ordinal cannot claim complete actual trip order. |
| `passenger_events` | `passenger_event_id`, `service_date`, `event_timestamp`, `trip_stop_sequence`, `event_type`, `vehicle_id` | A GPS ping reference is optional; event grain, time and visit order still need support. |

The trip key is `(service_date, trip_id_performed)`; the visit key adds
`trip_stop_sequence`. Actual visit sequence starts at 1 and is consecutive;
scheduled sequence is a separate field. `actual_trip_start` and
`actual_trip_end` mean origin departure and destination arrival. They are
nullable, so an observation envelope must not be substituted for those events.
One scheduled trip may relate to multiple performed trips, and a performed trip
need not yet have a scheduled association. Missing association alone does not
establish an operational `Added`, `Canceled`, `Missing` or `Unscheduled` status.

Resources are individually optional; the package profile requires at least one
resource. This permits capability-specific future outputs. However, the pinned
schemas do not encode the described relationships as `foreignKeys`; a generic
schema validator cannot replace the project's reference checks. Populated
performed-trip references must resolve on the composite key under the selected
package policy. GTFS references, when introduced later, must resolve in the
selected feed and applicable service date.

TIDES supplies field constraints and meanings. It supplies no APC–AVL distance,
clock tolerance, trip-gap threshold, tail duration, confidence score or matching
algorithm. Every such number used in this study is an exploratory configuration.
Zero source loss, no double attribution and no invented GPS records are study
integrity requirements. Unconfirmed APC semantics are publication gates, not a
reason to prohibit diagnostic experiments.

## 2. Separate the objects and evidence states

Keep source records immutable. A physical segment is a versioned grouping of
AVL observations; an APC record is a source observation of unconfirmed grain.
A candidate trip can connect one or several physical segments and APC records,
or remain a fragment. It becomes a performed-trip claim only after its identity
and boundaries have enough evidence. A candidate group with a long gap can also
represent several trips; stable raw `tatripId`, route or vehicle labels do not
prove otherwise.

Every attachment decision should preserve:

- APC source ID, candidate segment/trip ID and competing hypotheses, including
  null; accepted assignment is unique within one method configuration.
- `source_support`: observed GPS, APC only, AVL only, or insufficient evidence.
- `assignment_method`: direct point/interval support, bounded interpolation,
  inferred sequence continuation, or none.
- `assignment_status`: accepted, ambiguous, no candidate, evaluated
  incompatible, unresolved time, source invalid, or search overflow.
- Supporting AVL source IDs; time/space residuals where computable; missing
  evidence flags; boundary candidates; algorithm/configuration versions.
- Time basis and fold/clock hypotheses, source vehicle namespaces, and separate
  observed interval endpoints and claimed operational endpoints.

These dimensions must not be collapsed into a single confidence number. An
APC-only tail can be a reasonable inferred attachment while having no direct
GPS evidence. Conversely, an excellent GPS alignment in one fragment need not
identify the full trip. Missing coordinates do not mean a spatial mismatch;
absence of a candidate inside a bounded search is not proof that no true match
exists. An overflowed candidate search must abstain explicitly.

## 3. Hard checks before accepting a method as trustworthy

The JSON assigns stable IDs `I01`–`I10`. Each check reports `status`, violations,
evaluated count, not-evaluated count and reason. Zero evaluated cases produces
an unavailable rate, not a pass. Unimplemented assertions are `not_evaluated`.

| Check | Programmatically testable condition | Failure consequence |
| --- | --- | --- |
| I01: source conservation | Per source file, raw rows equal usable plus structurally rejected rows. Each usable record has exactly one terminal disposition per configuration, including out of scope. | Candidate method is no-go until corrected. |
| I02: lineage | Every output/link resolves to a file digest and original physical row ordinal; input hashes unchanged; decisions retain config/version. | No-go. |
| I03: attribution | Each APC row has at most one accepted grouping; candidate edges and supporting pings never multiply passenger counts. | No-go. |
| I04: segmentation accounting | Every eligible AVL row belongs to exactly one segment in each selected hypothesis; alternatives are explicitly separated. Batching does not change membership. | No-go. |
| I05: observed GPS | Every ping identity points to an actual AVL row; interpolation remains a derived feature. | No-go. |
| I06: direct support | Accepted direct links satisfy their declared vehicle/time/space rules. Interpolation uses ordered brackets in one segment within the declared maximum gap and crosses no unresolved boundary. No extrapolation. | No-go for accepted direct links; retain baseline failures for diagnosis. |
| I07: honest evidence | Inferred tails have no fabricated direct GPS support; missing residuals remain unknown; no uncalibrated score is labeled probability. | No-go. |
| I08: abstention | Null and relevant boundary alternatives remain possible; ties and search overflow do not become confident winners through tie-breaking. | No-go. |
| I09: semantics | Source trip labels, calendar dates, segment IDs and fragment ordinals are not promoted into verified trip identities, service dates or full-trip sequences. | No-go for such claims. |
| I10: determinism | Canonical decisions/IDs agree on a rerun with fixed immutable inputs/configuration; declared irrelevant ordering/batching permutations preserve outcomes. | No-go if violated; unverified if not run. |

I06 is an integrity condition about a method's declared accepted support. A
time-only baseline can still be evaluated as an intentionally weaker candidate
generator. If it accepts links that fail the common time-space acceptance rule,
the report must show those violations; it cannot inherit the trustworthy label
by defining away its spatial check.

For physical plausibility, compute implied travel speed, coordinate residual,
interval inversion and candidate sequence contradictions with explicit input
availability. Thresholds for speed, dwell, distance and debouncing are
exploratory flags. A GPS jump can reflect sensor error rather than an actual trip
boundary. Flagged observations remain preserved; changing values or deleting
rows merely to satisfy a plausibility rule fails the integrity contract.

## 4. Semantic eligibility for later publication

The following are separate from diagnostic method ranking. A successful study
can leave these gates open and still identify a useful next candidate.

| Gate | Evidence required | When unresolved |
| --- | --- | --- |
| P01: schema and references | Pinned required fields, keys, types, domains, package profile and explicit reference checks. | No conformance claim for affected resource. |
| P02: time and service date | Source clock basis, timezone interpretation, DST fold resolution, and a justified service-date decision. | Keep hypotheses. No guessed fold/cutoff; eligible GPS may omit trip linkage. |
| P03: APC source grain | Meaning of one row, `AvgSample`, `NoTrips`, counts, averaging, rounding, collisions and additivity. | APC links remain research hypotheses; no APC-derived published event/visit claim. |
| P04: event meaning | Count domain, timestamp proxy, load meaning and door allocation. | Do not turn estimated counts into integer events or total counts into door-1 counts. |
| P05: actual visit order | Actual trip context and visit completeness; repeated stops kept distinct; missing or excluded observations accounted for. | Do not renumber a truncated prefix or internal gaps into a purported full actual sequence. |
| P06: trip identity/endpoints | Boundary and continuity evidence for one actual trip; endpoint events supported independently of logging start/end. | Keep internal segment/candidate IDs and nullable operational endpoints. |
| P07: source/GTFS identity | Vehicle namespace evidence and, later, feed-scoped stop/route/trip mappings. | Preserve raw labels as evidence; no unsupported standardized IDs or schedule statuses. |

`AvgSample=1` alone does not close P03. The existing audit's larger sample counts
and visit-key collisions make this especially consequential. An inverted APC
interval must not create negative dwell; possible clock/fold interpretations
remain candidates until resolved. A load equation discrepancy is a diagnostic,
and an exact equation does not prove that the record is one observed stop visit.

The existing
[merge plan's visit-set completeness rule](merge-implementation-plan.md#s8--observed-visits-and-scheduled-sequence-alignment)
excludes a trip's stop-level outputs when an internal candidate observation is
unresolved or non-publishable. This study also calls out incomplete prefixes:
without evidence for the first actual visited stop, a sequence beginning at
fragment ordinal 1 is not sufficient evidence for TIDES `trip_stop_sequence=1`.
GPS or other defensible observations could eventually establish the missing
visits, but the algorithm cannot invent them just to satisfy consecutive values.

## 5. The unequal-coverage case

For AVL 13:00–14:00 and APC 13:15–14:13, first test the common vehicle hypothesis
and possible systematic clock offset using several independently corresponding
observations. The endpoints alone cannot distinguish a clock offset from actual
asymmetric logging. Do not optimize clock shift solely to maximize attachment.

| Region | Required treatment |
| --- | --- |
| 13:00–13:15, AVL only | Retain GPS observations. Passenger evidence is missing; it is not zero ridership. |
| 13:15–14:00, nominal overlap | Require actual point/bracket support, time-space agreement and sequence compatibility. An interval envelope can hide an internal GPS outage. |
| 14:00–14:13, APC only | Preserve APC records with null direct GPS reference. Provisional continuation requires temporal/spatial continuity from supported anchors, no accepted intervening boundary and consideration of adjacent-trip explanations. |

A 14:02 terminal followed by a 14:06 reversal is a competing split explanation;
proximity to the last GPS point cannot settle it. If sequence context cannot
distinguish continuation from another trip, retain both hypotheses or abstain.
If a continuation rule accepts the tail, its assignment is explicitly inferred
and revisable. Record anchor evidence and distance/duration from the last direct
support, not a fake residual to an extrapolated vehicle position.

The candidate observation envelope may be 13:00–14:13. Its actual start/end
remain unknown unless origin departure/destination arrival are observed and
identified. A later data window or boundary decision may split/reassign the tail;
raw source IDs persist while candidate identity and assignment version change.
The same rules apply to an APC-only prefix or an internal AVL outage. Completely
disjoint coverage needs independent bridging evidence and cannot be merged just
because vehicle or route strings match.

## 6. Measure tradeoffs with explicit denominators

For each configuration report the raw scan extent, experiment extent, structural
exclusions and semantic strata before match statistics. The machine-readable
metric definitions state numerator and denominator. At minimum report:

- Direct accepted APC rows / all raw APC rows, and / eligible evaluated APC rows.
  Both denominators are necessary to expose selection effects.
- Inferred continuation assignments separately; ambiguous, no-candidate,
  incompatible, time-unresolved and overflow outcomes as disjoint counts.
- Spatial/time residual quantiles with the number actually evaluated, plus
  missing counts. Use the same event-anchor/interval conventions across methods.
- Number and duration of segments; one-ping/short fragment share; boundary
  sensitivity; source-ID/route mixing; accepted crossing-boundary links.
  Route purity and fewer segments are diagnostics, not quality targets.
- APC-only/AVL-only prefix/tail length, internal gap length and supporting-row
  counts. Distinguish continuous sampled support from min/max envelope overlap.
- Runtime, peak process memory where measured, candidate count/overflow and
  output size. State machine, concurrency and shared preprocessing cost.

Report at least the regimes enumerated in the JSON: overlap, unequal tails,
internal gaps, no overlap/common vehicle, clock-offset candidates, DST,
midnight/file boundaries, source-grain strata, collisions, repeated stops,
turnarounds, label changes, bad coordinates and short fragments. Show absolute
counts alongside percentages. Edge categories can overlap; their counts must
not be summed into a unique total unless disjoint precedence is defined.

Compare configurations on common eligible cases in addition to their total
yield. A method that rejects difficult records can have lower residuals merely
because its evaluated population is easier. Likewise, a method optimizing a
distance residual has not independently validated itself by reporting that
residual. Compare segment memberships via source IDs, not method-specific
segment names. Boundary perturbations should report direct-to-inferred,
accepted-to-unresolved and neighboring-trip switches separately.

Do not average these dimensions into a quality score. First exclude observed
hard-invariant failures from trustworthy candidates. For the rest, show the
non-dominated tradeoffs: direct supported yield, abstention, residuals, stability,
inferred-extension exposure and cost. A recommendation is conditional on its use
case and evaluated regimes. A strict overlap method may be a suitable direct
association baseline while leaving the tail problem unsolved; a continuation
method may improve retained trip context while adding assumptions to review.

## 7. Recovery tests, accuracy and practical feasibility

Mask tests start with a declared set of supported real records and artificially
hide APC prefixes, AVL tails, internal spans or selected pings. Keep original
source identities for comparisons. Report whether still-observed APC cases retain
their original candidate grouping, become inferred, abstain or switch to a
neighbor. Report the number of independent vehicles/days and mask parameters.
These tests measure recovery relative to the original assignment and robustness
to artificial missingness. They do not establish that the original trip was
correct, and real outages may depend on route, device or operating state.

Synthetic tests can establish invariants for deliberately constructed ties,
boundaries, loops, midnight transitions, disjoint coverage and collisions.
Passing them is evidence that the implementation follows the specified rules,
not that those rules recover real TTC trips accurately.

Independent attachment precision and boundary precision/recall require reviewed
labels with an explicitly sampled population. Existing
[human-label status](human-labeling-v2-run01-status.md) reports no reviewed
holdout; the 18 pilot cases are not an independent holdout. Leave those accuracy
metrics null in this study. A future evaluation must freeze thresholds before
opening an independent reviewed holdout, keep vehicle-days together, report
labelability and selection coverage, and use uncertainty estimates that account
for repeated observations within a vehicle-day. Segmentation and tail assignment
need their own labels; scheduled-trip labels alone cannot establish either.

Operational feasibility is measured, not assigned a TIDES threshold. Record
wall/CPU time where available, process peak RSS, source row count, output bytes,
and candidate-graph size. Report shared preprocessing separately from method
cost. Declare resource limits before running; cap overflow must yield an explicit
unresolved result. Runtime on one month cannot by itself establish multi-month
scalability. A deterministic second run should compare canonical domain
decisions, excluding elapsed time and run timestamps. If batching/shuffle
invariance or full memory profiling is not tested, mark it unverified.

## 8. Edge-case inventory and limits of observation

The raw-data audit should distinguish `observed`, `searched_not_observed`,
`not_testable_with_current_fields`, and `not_yet_checked`. Each observed category
needs its detection rule, all-row denominator, count and a stable restricted
example reference. Zero matches for a detector does not prove operational
impossibility, and the following is a reusable taxonomy rather than a claim that
every case occurred in the fixture.

| Family | Cases to test and distinguish |
| --- | --- |
| File/identity | Different coverage windows; month-end censoring; absent vehicles in one source; formatting/namespace collisions; exact payload duplicates versus visit-key collisions; reused source trip/run IDs. |
| Time | Parse failures; missing timestamps; out-of-order records; equal-time positions; clock drift/offset; cross-midnight intervals; DST repeated/nonexistent hours; calendar date versus service date; long APC intervals. |
| GPS | Missing/out-of-range coordinates; valid-domain but implausible positions; jumps; stale/frozen locations; irregular sampling; one-ping fragments; outages inside an interval envelope. |
| APC grain/counts | `AvgSample`/`NoTrips` regimes; averaged or rounded counts; inverted arrival/departure; repeated stop records; load residuals; missing, negative or nonintegral values; unknown door allocation. |
| Boundary ambiguity | Chattering route/trip/direction labels; legitimate route changes; long dwell; terminal/turnaround; loop routes; short turns; interlining; layover/deadhead; multiple trips in one segment; one trip split across segments. |
| Attachment | Unequal prefixes/tails; no overlap; several nearby candidate segments; recurring same stop; opposing direction at close stops; repeated passes; APC spanning a boundary; missing coordinates; weak anchor sequence; conflicting clocks. |
| Evaluation | Selection bias; route-purity self-validation; extra candidates mistaken for extra passengers; threshold leakage; mask retention mistaken for accuracy; discarded ambiguities; no denominator for excluded rows; untested memory or determinism claims. |

Some categories require external evidence to establish their operational meaning:
vehicle substitution, device reassignment, intentional deadhead, a true terminal,
and averaged-versus-single-visit APC exports cannot always be resolved from raw
columns. A study should report them as hypotheses or observability limits.
Schedule integration later may resolve service date, standardized stops and
planned trip context, but should preserve these raw identities, alternative
boundaries and source-support distinctions.
