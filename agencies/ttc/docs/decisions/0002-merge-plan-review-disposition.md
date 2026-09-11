# ADR 0002: Merge-plan review disposition and provisional S0–S2 authorization

- Status: accepted
- Date: 2026-08-06

## Context

The merge implementation plan
([`merge-implementation-plan.md`](../merge-implementation-plan.md), revision
2026-08-06) was professionally reviewed
([`merge-plan-review.md`](../merge-plan-review.md)). The review verified all
of the plan's empirical fixture measurements and TIDES v1.0 specification
claims, endorsed the architecture, and identified six blocking revision
requirements (R1–R6): joint-resolution specification (R1), fold-resolution
hand-off (R2), windowing/ownership rules (R3), mid-trip exclusion policy
(R4), governance fallback and gate scoping (R5), and governing-document
alignment sequencing (R6). It found that no blocking item changes the
S0–S2 contracts (preflight, AVL landing/normalization, APC
landing/normalization).

## Decision

1. The architecture direction of the merge implementation plan is
   **approved**.
2. **Provisional, non-publishable S0–S2 diagnostic work is authorized**, in
   parallel with **one R1–R6 documentation revision** of the plan.
3. **S3 and all downstream publication-bound work are frozen.**
4. M1 is **not** to be declared complete, and downstream contracts are
   **not** to be frozen, until the changed plan sections pass a
   **section-scoped re-review**.
5. Once all six blockers are closed: exit M0, formally accept M1, and resume
   implementation from the revised contracts.

## Consequences

- S0–S2 artifacts produced before M1 acceptance are diagnostic and
  provisional: they carry a `diagnostic-provisional` profile marker, claim no
  TIDES conformance, and may be invalidated by the re-review.
- No S3+ module (temporal hypotheses, segmentation, mapping, attachment,
  resolution, TIDES construction, publication) may be implemented until the
  re-review passes.
- The plan revision must address R1–R6 and should fold in advisory findings
  where practical; unchanged sections do not require re-review.
- Governing-document alignment (architecture, README, source audit) is
  scheduled at M0 per the revised plan; it is not performed as part of the
  R1–R6 revision itself.
