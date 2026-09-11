# Human labeling protocol v2 (blind second pass)

Status: **frozen** (protocol version 2.0). Scope: the 18 pseudonymous pilot
cases from the Spike 02R review packet, plus their task-specific child
cases. This document is repository-safe: it contains no raw identifiers, no
absolute timestamps, no coordinates, and no candidate keys. The full,
restricted protocol artifacts live in the gitignored run directory
`artifacts/human-labeling-v2-run01/` with owner-only permissions.

## Why v2 exists

The first-pass label audit (`artifacts/human-label-audit-v1/`, summarized
in `docs/` history) found that the v1 packet conflated case grain with
scheduled judgment, displayed a five-card model-informed shortlist from a
much larger candidate universe (26–57 complete keys per case), accepted
high-confidence labels without rationale, and treated synthetic packet
fields as operational counts. Zero of eighteen first-pass cases pass the
strict calibration gate. The 18 cases are therefore pilot,
ontology-discovery, UI-regression, and adjudication cases — they are
permanently excluded from independent holdout use.

## The v2 ontology

Separate dimensions, never conflated:

- **case grain** — single_trip_candidate, multi_trip_chunk, local_fragment,
  boundary_spanning, turnaround_or_idle, data_quality_invalid,
  coverage_censored, unknown;
- **reviewability** at that grain;
- **scheduled judgment** — matched, no_scheduled_match,
  ambiguous_candidates, insufficient_evidence, not_applicable — only where
  the grain permits it, with a complete five-position candidate key
  (service date, feed, route mapping, scheduled trip, polarity hypothesis;
  a trailing empty polarity is an explicit null, never silently filled);
- **fragment role** with tri-state stitch-previous/next;
- **activity state** (unconfirmed states stay unconfirmed — open decision
  D-11 is not resolved by a label);
- **quality state** with multiple flags;
- three confidences — grain, match (nullable), evidence quality — because
  high confidence that a case is invalid is not a high-confidence match.

Display aliases (A, B, …) are assigned only after schedule-neutral ordering
and are display-only; no judgment is ever serialized as a bare letter.

## Blind-review rules

Before the blind save the application never loads into visible state: any
first-pass field, the audit routing suggestion, any model score, rank,
margin, acceptance status, anchor status, unique-survivor or cross-fit
result, or the packet summary string (which embeds an anchor status).
The grain question comes first with no preselected answer. Case order is a
deterministic seeded shuffle recorded in the run directory. Confidence 4–5
requires structured plus free-text rationale; a scheduled match normally
requires two evidence families, or one explicitly declared decisive family.
Panels viewed, filters applied (with hidden-candidate counts), adjacent
context use, and every reveal are logged to an append-only history.

After the blind save the judgment is immutable. The application then
compares it with the preserved first pass, records agreement or
disagreement, opens an adjudication item on disagreement, and only then
allows logged reveals of the prior judgment and the model diagnostics.
Post-reveal changes are separate records; adjudication is a third record;
nothing overwrites either pass.

## Candidate presentation

Every case shows its complete candidate universe (26–57 keys per pilot
case) neutrally ordered by service date, feed, route, and scheduled start —
never by score, rank, margin, or acceptance status. Explicit
no-scheduled-trip alternatives and contract-unresolved sentinel keys stay
visible with warnings. The null option (no scheduled match) is always
offered. Reviewer filters and pagination are conveniences that always
display their hidden count and are logged; no model top-k ever substitutes
for the universe.

## Child cases

The audit's 11 child-case requests across 9 parents expand
deterministically into 21 child review tasks (per-subtrip, boundary
interval, inbound/outbound context, fragment role, plus one protocol-added
sequence review per multi-trip parent). Children are tasks, never labels:
no parent judgment or note is copied in; note-derived seeds are marked
`human_note_seeded_unverified` and stay hidden until the parent's own
blind save exists. Reviewers can request further children; requests are
logged, not auto-labeled.

## Promotion and the factorial

There is no global gold flag. Labels promote only into task-specific
reviewed sets (schedule, schedule-abstention, segmentation, boundary,
fragment, quality, activity) through mechanical gates: correct final grain,
complete keys, blind pass finished, agreement or adjudication, structured
rationale, no pre-save diagnostics reveal, and matching frozen protocol and
UI versions. Confidence alone never promotes. The pilot cases carry
`development_pilot = true` and `independent_holdout_eligible = false`
permanently.

The preregistered factorial remains **NO-GO**. The go/no-go report is
mechanical and can never trigger anything: even a fully green report
requires a separate explicit human authorization. Thresholds are untouched,
the holdout is unopened, and the preregistration is hash-verified
unchanged.

## Versioning

All protocol artifacts are hashed into a freeze manifest; every saved label
references the protocol hash. The store refuses to operate on a run
directory whose frozen zone fails verification — a protocol change after
labeling starts requires a version bump, and labels are never mixed across
versions without an explicit compatibility review.

## Implementation

- Protocol core (deterministic, no UI): `experiments/r1_design_spike/src/`
  `r1spike/labeling_v2_*.py`, CLI `r1spike.run_labeling_v2_setup`.
- Blind review UI: `experiments/r1_design_spike/labeling_ui_v2/`
  (standalone uv project; launch with `uv sync && uv run streamlit run
  app.py` from that directory).
- Tests: protocol battery in `experiments/r1_design_spike/tests/`
  `test_labeling_v2.py`; UI render harness and headless blind-flow tests in
  `experiments/r1_design_spike/labeling_ui_v2/tests/`.

The architecture verdict remains: **INCONCLUSIVE — NO REVIEWED HOLDOUT
LABELS**. R1 remains open. This protocol authorizes labeling only — no
calibration, no threshold change, no Phase B processing, no factorial, no
TIDES output.
