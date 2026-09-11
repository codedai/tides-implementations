# Human labeling v2 — run01 status

Repository-safe status of **Blind Adjudication Run 01** under the frozen
v2 protocol (`docs/human-labeling-protocol-v2.md`). Restricted artifacts
live in the gitignored, owner-only `artifacts/human-labeling-v2-run01/`.

## Current state

- **Protocol: frozen.** Schema v2.0, UI contract, candidate-presentation
  contract, child-case generation contract, promotion contract, and blind
  review protocol are hashed into `protocol_freeze_manifest.json`; every
  saved label must reference that hash.
- **Human review: not started.** The live label zone is initialized empty
  and schema-valid; no second-pass label exists, no label was generated
  automatically, and no first-pass label was modified. First/second-pass
  agreement is therefore undefined, honestly reported as zero comparisons.
- **Factorial: NO-GO.** The mechanical readiness report fails on every
  human-dependent condition and can never trigger execution.

## Frozen inputs and preservation

All 18 first-pass judgments are preserved byte-identically; the run setup
re-hashes its inputs after writing and records `inputs_unchanged`. The
audit's routing is carried as an **editable baseline**, not a label: the
reviewer's grain-first answer always wins and any change is stored as a
`routing_change` judgment.

## Routing baseline (18 pilot cases, pseudonymous)

| task baseline | cases |
|---|---|
| scheduled-trip blind second pass | C-134f2a21, C-179eba6d, C-1b38b137, C-47d3a45a, C-5555da7a, C-88bc09d3, C-b7163d0c |
| multi-trip segmentation | C-2630793f, C-ba6b0e92 |
| boundary-spanning dual role | C-913f7d4c, C-c953dac7 |
| fragment role and stitching | C-0ffcd227, C-3eb39b05, C-7d4b42b8, C-af359271 |
| insufficient-context fragment | C-ba617064 |
| source/time quality | C-7d5fcf98 |
| no-match activity | C-88e48154 |

## Candidate universe

The complete universe holds 660 candidate rows across the 18 cases (26–57
per case, matching the audit's counts exactly), plus one always-present
null option per case. Score-bearing columns are excluded from the frozen
universe manifest; ordering is schedule-neutral; aliases are immutable for
the run and deterministic across rebuilds.

## Child cases

21 child review tasks were generated deterministically from the audit's 11
requests across 9 parents (per-subtrip, boundary-interval,
inbound/outbound-context, fragment-role, plus one protocol-added sequence
review per multi-trip parent). The child count exceeding the request count
is by design and is reported, not forced. No child carries a label.

## What the human reviewer still has to do

1. Complete the blind second pass over all 18 parents in the recorded
   seeded order (grain first, task form second, save immutably).
2. Complete the unlocked child reviews (21 tasks).
3. Adjudicate every first/second disagreement and every grain change
   (third records; neither pass is overwritten).
4. Only then: regenerate the descriptive post-review analyses and re-read
   the go/no-go report — which will still require a separate explicit
   authorization before any factorial work.

## Explicit non-claims

- No human label was created, modified, or replaced by this system.
- No accuracy metric was calculated; no architecture was selected.
- The 18 pilot cases are not an independent holdout and never will be.
- Phase B remains unprocessed; the holdout remains unopened; thresholds
  remain untouched; the factorial remains blocked.
- The architecture verdict remains **INCONCLUSIVE — NO REVIEWED HOLDOUT
  LABELS**; R1 remains open.
