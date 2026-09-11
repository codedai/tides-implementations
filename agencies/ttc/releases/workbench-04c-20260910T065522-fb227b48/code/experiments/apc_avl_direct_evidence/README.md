# APC–AVL direct evidence optimization

This separately versioned experiment implements the user's request to deepen
the **direct spatiotemporal evidence layer**, after the coverage study. It does
not modify the frozen first study, resegment vehicles, use GTFS, read old human
judgments, correct source clocks, infer APC-only tails, or publish TIDES tables.
The underlying month has already been examined and is not an untouched holdout.

See the [Chinese optimization report](../../docs/apc-avl-direct-evidence-optimization-20260908.md)
and [fixed-window robustness report](../../docs/apc-avl-direct-evidence-probes-20260908.md)
for measured results and tradeoffs. The earlier
[TIDES-based standard](../../docs/apc-avl-coverage-evaluation-standard.md) still
applies: source support, trip identity, and publication eligibility are different
claims. Thresholds below are project experiments, not TIDES requirements.

## What changed

The old method picked the best nearby point or interpolation within an APC
interval, then compared owners defined by experimental trip chunks. This can
collapse separate visits inside a long chunk, overlook an observed departure
and return, or reject good local point evidence solely because a chunk boundary
crosses the APC interval.

The optimized matcher instead:

1. Indexes distinct AVL measurement instants per source vehicle. Exact repeated
   positions share geometry but retain raw identities in the canonical source;
   equal-time position conflicts and timestamped invalid records are barriers.
   Source route labels at an instant remain a set, not an arbitrarily selected
   label. Two-sided implausible GPS spikes are flagged and excluded from direct
   witnesses, without deleting or repairing their raw records.
2. Constructs local near-stop **episodes** from observed chronology, independently
   of trip chunks. Observed excursions, quality barriers and long gaps separate
   episodes. These are candidate physical associations, not verified visits.
3. Evaluates arrival, departure and interior witnesses separately. A near point
   cannot bridge over an observed departure or a bad instant. Short interpolation
   uses adjacent, real, valid measurements and retains both endpoint references.
4. Retains explicit negative evidence: sufficiently contemporaneous distant GPS,
   or a valid bracketed position far from the APC location. It does not select
   an older near point while hiding this contradiction. An observed interval
   excursion prevents a full bounded-interval support claim.
5. Competes local episodes against one another and a null alternative. Two
   visits inside the same trip chunk remain two candidates. Candidate scores
   are deterministic experimental rankings, never confidence probabilities.
6. Separates observed support from chunk ownership. All raw aliases of a
   measurement contribute possible owners; selecting a representative GPS row
   must not hide a conflicting alias context. Genuine point evidence can
   span two frozen chunks while trip context remains unresolved. Derived
   interpolation cannot cross a frozen chunk boundary.
7. Preserves overlapping APC rows, including opposite-direction records sharing
   a terminal location. Such rows may share physical support; the method neither
   forces a one-to-one relation nor sums their passenger counts.
8. Binds candidate caches to their source index, relevant APC fields and exact
   configuration, preventing stale candidates from bypassing tighter bounds or
   a changed vehicle, time or position.

## Output contract

| Status | Support level | Claim |
|---|---|---|
| `direct` | `instant` | A zero-duration APC record has local point/bracket support. |
| `direct` | `bounded_interval` | Both endpoints have support within one bounded local episode, with at least two distinct underlying measurements and no observed interval excursion. |
| `partial` | `arrival_only`, `departure_only`, `interior_only` | Only the named local portion is accepted; not a full interval claim. |
| `ambiguous` | null | Competing episodes, contradictory evidence, insufficient score/margin, or bounded-search overflow. |
| `unmatched` | null | No admissible local support in the implemented search. |
| `ineligible` | null | Unresolved/invalid time, position, interval or vehicle identity. The original record remains present. |

`bounded_interval` does **not** assert continuous perfect GPS coverage, an actual
stop dwell, a passenger event, or a performed trip. Motion between samples is
not observed. APC export grain and timestamp meanings remain unconfirmed.

`context_status` independently reports `single_segment`, `multiple_segments`,
`unmapped` or `no_assignment`. `segment_id` can be null for a directly supported
record with multiple contexts; `segment_ids` and real witness IDs preserve the
evidence. No segment identifier is promoted to `trip_id_performed`.

Each accepted witness records its real source IDs, measurement keys, position,
target, support time, temporal/spatial residuals, bracket gap, and all supporting
route labels. A derived witness is explicitly `kind=interpolation`; no synthetic
row is added to AVL. Negative witnesses are retained separately in
`counter_witnesses`. Missing source coverage is never filled with zero passengers
or extrapolated GPS. Counts do not contribute positive matching evidence.

## Parameters and assumptions

`matcher.DEFAULT_CONFIG` is the versioned executable configuration. Defaults are
60 s point tolerance, 100 m local radius, 90 s maximum interpolation/episode gap,
200 m contradiction radius and 10 s contemporaneous point window. The motion
ceiling is 130 km/h; source APC intervals above 30 minutes are ineligible.
Candidate score floor/null/margin are 650/550/60. Search caps are explicit.
Ranking weights and endpoint qualification also differ from the old method;
shared nominal hard bounds do not imply the same effective acceptance region.
This is a paired version comparison, not a single-factor ablation.

A point counter must exceed both 200 m and the motion allowance
`100 m + 130 km/h × absolute time difference` (with compatible units). This
prevents physically permitted approach/departure from overruling an exact
near-stop observation. The speed ceiling and position tolerance are still
experimental assumptions, not a measured position-error distribution. A derived
counter at the target time is conditional on the bounded interpolation model.

The context policy is `flag`: conflicting or mixed source route labels remain
visible but are not treated as authoritative trip/route truth. An experimental
`abstain_conflict` policy exists; it is not the default full-run comparison.

Times remain on the original naive local wall-clock axis. Ambiguous DST rows
are not given a guessed fold. There is no estimated or applied clock shift.
The stronger contradiction logic is deliberately sensitive to unsynchronized
sources; fixed-window clock perturbations measure that limitation, not the
actual TTC clock offset. Resolving the timestamp contract is a prerequisite
for interpreting counterevidence as an operational inconsistency.

## Reproduce

Run from `agencies/ttc` using the existing locked environment. Choose new output
directories; commands refuse to overwrite prior runs. Outputs stay owner-only
under the gitignored experiment artifact root.

```sh
umask 077
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m experiments.apc_avl_direct_evidence.evaluate \
  --audit artifacts/apc-avl-coverage-study-20260908/raw_audit \
  --baseline artifacts/apc-avl-coverage-study-20260908/factorial02 \
  --out artifacts/apc-avl-direct-evidence-20260908/full-rerun

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m experiments.apc_avl_direct_evidence.probes \
  --audit artifacts/apc-avl-coverage-study-20260908/raw_audit \
  --selection artifacts/apc-avl-coverage-study-20260908/probes01/selection.json \
    artifacts/apc-avl-coverage-study-20260908/probes-edge02/selection.json \
  --out artifacts/apc-avl-direct-evidence-20260908/probes-rerun

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m experiments.apc_avl_direct_evidence.validation.replay_negative \
  --run artifacts/apc-avl-direct-evidence-20260908/full-rerun \
  --out artifacts/apc-avl-direct-evidence-20260908/negative-rerun

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m experiments.apc_avl_direct_evidence.validation.duplicate_contexts \
  --run artifacts/apc-avl-direct-evidence-20260908/full-rerun \
  --out artifacts/apc-avl-direct-evidence-20260908/alias-rerun

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m pytest tests experiments/apc_avl_coverage_study/tests \
  experiments/apc_avl_direct_evidence/tests -q

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync ruff check \
  experiments/apc_avl_direct_evidence
```

`evaluate --limit 1` is a smoke run, not full-fixture evidence. The evaluator
builds one index and one prepared candidate set per vehicle, then reuses both
across the three unchanged segmentation maps. Beginning/end hashes verify
canonical inputs, frozen baseline artifacts and the inference implementation.

## Evidence and limits

`full02/` contains the final full paired dispositions, nested candidate/positive/negative
evidence, per-vehicle and aggregate metrics, common eligibility denominators,
independent positive-witness checks, and timing/provenance. `probes02/` preserves
20 pre-existing raw-feature-selected windows and every original/perturbed
decision. Development smoke runs, initial `full01/` and `probes01/` are retained,
not used as final results. The initial source snapshot and separate retrospective
audits explain the alias-context and motion-envelope corrections. An optional
bounded-reuse replay harness was developed but not used for the final result:
the final full run executes every vehicle with the finalized matcher.

The separately implemented negative-evidence validator imports neither matcher
nor evaluator. It reconstructs source chronology and replays retained counters,
their raw-alias context, conflict dispositions, and interval-excursion flags.
It does not establish that the search found every possible counter or candidate.
The final `negative-replay02/` audited all 157 vehicles in all three contexts:
70,487 retained counter-witness instances, 3,821,376 bounded checks, zero failures
and zero overflow skips. These repeated checks are not independent truth labels.

`positive-alias-replay02/` separately checked accepted and saved candidate
witness ownership in all 25 vehicles identified by the full-vehicle alias
mapping screen, with zero implemented violations. The alias command above
omits this optional screen and audits every completed vehicle instead.

Full-input accounting and elementary reference/time/space checks do not prove
episode correctness. Perturbation references are the unperturbed algorithm's
outputs, not ground truth. Masking, clock-shift and owner-refinement tests are
reported separately. A lower accepted fraction may represent more honest
abstention or unnecessary rejection; a higher fraction may still contain wrong
associations. Independent attachment precision and actual-trip accuracy remain
unknown, and the prior TIDES publication gates remain unresolved.
