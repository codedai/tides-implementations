# ADR 0003: Bounded authorization for isolated R1 diagnostic experiments

- Status: **Accepted** (see "Approval basis and revocation" — acceptance is
  attributed to the invoking project owner's recorded instruction, not to any
  automated agent)
- Approver: jiahao (project owner / repository operator)
- Date: 2026-08-08
- Relates to: ADR 0002 (merge-plan review disposition — unchanged by this ADR)

## Context

Spikes 01 and 02 (`experiments/r1_design_spike/`) produced diagnostic
evidence about the R1 blockers using real fixture data. An independent
review of Spike 02 identified validity defects in the experimental kernel
(contaminated AVL-only comparator, APC evidence reuse, unique-survivor
margins, candidate-identity collapse, hard-pinned anchors, truncated
optimum enumeration, fragment/performed-trip conflation, weak
reproducibility, and report privacy leakage). Spike 02R repairs the
kernel before any architecture conclusion is drawn. This ADR records the
bounded authorization under which these isolated diagnostic experiments
process real fixture data locally.

## Decision

Bounded experimental authorization is granted for the isolated R1
diagnostic experiments (Spikes 01, 02, and 02R), under all of the
following conditions.

1. **Permitted source files (read-only):**
   `artifacts/diagnostic-2026-08-06/normalized/{avl,apc}` (S0–S2
   normalized outputs), `artifacts/gtfs-*` (GTFS pipeline artifacts),
   `artifacts/feed_manifest.draft.csv`, and the frozen Spike 01/02
   artifacts (`artifacts/experimental-r1-spike-r1c-*`,
   `artifacts/experimental-r1-spike02-s2b-*`). Raw files under
   `database_offline/` remain immutable and are not read by the spikes.
2. **Permitted experimental stages:** diagnostic segmentation, boundary,
   attachment, anchor, alignment, stitching, labeling-preparation, and
   factorial-preparation experiments inside
   `experiments/r1_design_spike/` only.
3. **Local-only use:** all real-data outputs stay on this machine under
   `agencies/ttc/artifacts/experimental-r1-spike*`; nothing is uploaded,
   published, or transmitted.
4. **Production-module firewall:** no change to `ttc_tides` behavior; no
   production `segment_id` or `trip_id_performed` minting; no S3+
   production implementation is authorized by this ADR (ADR 0002's
   freeze stands).
5. **No-publication rule:** no TIDES resource, data package, or
   publication directory may be produced; experiment code must fail
   closed on any such write.
6. **Artifact retention:** real-data run artifacts are retained locally
   for reproduction, remain gitignored/untracked, and may be deleted at
   closure of R1 experimentation.
7. **Restrictive permissions:** directories containing real vehicle
   traces (normalized inputs and experiment run outputs) carry
   owner-only permissions (0700 directories / 0600 files).
8. **Report-redaction rules:** repository-committed Markdown must not
   contain raw vehicle IDs, source record IDs, precise timestamps,
   precise coordinates, or passenger values from the fixture; committed
   reports use pseudonymous case IDs and relative times. Full-detail
   reports live only inside the gitignored run directories.
   (Historical note: the Spike 01/02 findings documents predate this
   rule and contain vehicle IDs, record-ID fragments, and precise
   times; they are retained as-is for the audit trail, and a scrub is
   recommended as a follow-up governance decision.)
9. **Status confirmation:** M0 and M1 remain unaccepted, R1 remains
   open, and no architecture is selected by any experiment under this
   ADR.
10. **Expiration / closure:** this authorization expires when R1 is
    formally closed, when a superseding ADR replaces it, or on explicit
    revocation by the project owner, whichever comes first.

## Approval basis and revocation

Acceptance is recorded on the basis of the project owner's direct,
written instruction commissioning "R1 Design-Validation Spike 02R"
(2026-08-08), whose brief includes the real-data execution plan the
spike carries out. A chat instruction alone is not project governance;
this ADR is the governance record of that instruction. If this
attribution of approval is incorrect, this ADR reverts to **Proposed**,
real-fixture outputs of Spike 02R must be deleted, and only
synthetic/kernel work may proceed until acceptance is recorded by the
project owner.
