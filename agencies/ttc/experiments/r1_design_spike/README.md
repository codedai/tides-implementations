# R1 design-validation spike (experimental, non-publishable)

- Profile: `diagnostic-experimental-r1-spike`
- `publishable = false`; `tides_conformance_claimed = false`
- Authorization basis: experimental design-validation spike; **not**
  ADR-approved S3+ production work. Per
  [ADR 0002](../../docs/decisions/0002-merge-plan-review-disposition.md),
  the architecture direction is approved, S0–S2 provisional diagnostic
  work is authorized, formal S3+ is frozen, **R1 remains open**, and no
  TIDES publication is authorized. Nothing in this directory changes
  that. This spike does not close R1 and does not mark M0–M4 complete.

The spike's purpose is evidence, not product: test whether the proposed
R1 design can be expressed in executable code, expose ambiguities and
real-fixture edge cases, measure candidate explosion / abstention /
runtime, and identify which R1 design decisions need revision. Findings:
[`docs/r1-design-spike-findings.md`](../../docs/r1-design-spike-findings.md).

## Layout

```
experiments/r1_design_spike/
├── README.md                  this file (incl. R1-B implementation map)
├── config/
│   ├── experimental_score_contract.yaml   versioned experimental score
│   │                                      contract (2 variants)
│   └── spike_config.yaml                  slices, caps, thresholds
├── src/r1spike/               the spike package (never imported by
│                              production ttc_tides code)
└── tests/                     spike-only pytest suite
```

## Running

From `agencies/ttc` (the production `.venv` covers all dependencies):

```sh
uv run pytest experiments/r1_design_spike/tests -q           # spike tests
PYTHONPATH=experiments/r1_design_spike/src \
    uv run python -m r1spike.run_spike --slice all --run-id <id>
```

Outputs land under `artifacts/experimental-r1-spike-<id>-<slice>/`
(gitignored; never committed). The writer fails closed on any path or
file name that resembles a TIDES resource, a data package, or the
immutable `database_offline/` boundary (`src/r1spike/artifacts_io.py`).

## R1 blocker reconstruction and implementation map

The "legacy-informed R1 re-review report" that enumerated R1-B01–R1-B11
is **not present in this workspace**; the blocker list below is
reconstructed from the review's R1 section (plus the R2/R3 material the
spike brief folds into it) and the legacy-methodology lessons in
[`previous_proj_data_integration_methodology.md`](../../docs/previous_proj_data_integration_methodology.md).
The reconstruction is itself an assumption and is recorded as such in
every run manifest.

| Blocker (reconstructed) | Overnight scope | Where |
| --- | --- | --- |
| R1-B01 objective-function structure (incompatible units → per-feature monotone transforms, family combination) | implement and test | `scoring.py`, `config/experimental_score_contract.yaml`, `tests/test_scoring_decisions.py` |
| R1-B02 avl-only vs apc-evidence regimes; missing family ⇒ regime switch, never renormalization | implement and test | `scoring.py` (`regime_switch`), `resolution.py`, `tests/test_scoring_decisions.py` |
| R1-B03 Stage-1 APC date-consistency summary semantics | implement and test (policies A/B/C behind one interface; none selected) | `stage1_policies.py`, `tests/test_stage1_policies.py` |
| R1-B04 graph class, component decomposition, candidate caps failing closed | implement and test | `resolution.py` (`_components`, cap rules), `tests/test_resolution_synthetic.py` |
| R1-B05 fixed-point determinism, half-even rounding, ties without ID evidence | implement and test | `fixedpoint.py`, `scoring.py::decide`, `tests/test_fixedpoint.py`, `tests/test_scoring_decisions.py` |
| R1-B06 complete candidate tuples with explicit null alternatives and null-aware margins | implement and test | `contracts.py`, `candidate_generation.py`, `tests/test_resolution_synthetic.py` |
| R1-B07 fold-resolution hand-off (AVL sequence rule; APC folds selected only by attachment; Stage-3 rollback) | implement and test (conservative XT-FOLD-A rule; known-coarse cases documented) | `time_hypotheses.py`, `resolution.py`, `tests/test_time_hypotheses.py`, `tests/test_resolution_synthetic.py` |
| R1-B08 window ownership and execution-order invariance | implement and test + measure on real slices | `windows.py`, `run_spike.py` determinism report, `tests/test_guards_windows.py`, `tests/test_resolution_synthetic.py` |
| R1-B09 geometry evidence contract (metre-space segment projection, retained residuals, three evidence states, scoped candidates, no anonymous fallback) | implement and test; branch/pattern-scoped geometry **blocked by unavailable semantics** (D-11) — dominant-shape simplification documented | `geometry.py`, `tests/test_geometry.py` |
| R1-B10 temporal evidence contract (gap/overlap definitions, declared denominator, stale hard bounds incl. the legacy 9,083 s case, missing ≠ zero) | implement and test | `temporal_features.py`, contract hard bounds, `tests/test_temporal_features.py` |
| R1-B11 sequence validation on stop occurrences with enumerated tied optima and conservative demotion | implement and test (bounded enumeration; full production optimizer deliberately deferred) | `sequence_validation.py`, `tests/test_sequence_validation.py` |

Additional deliberate deferrals (documented, not silent): Stage-3
segment-score recomputation after demotion (simplified to a visible
all-attachments-demoted rule), per-branch geometry, Policy-C prior
sensitivity beyond the uniform prior, and B0 execution on real legacy
artifacts (absent from this workspace; interface + synthetic tests only).

## Hard limits honored

No production S3–S8 modules, no changes to the production pipeline
dependency graph or CLI, no `trip_id_performed`, no TIDES resources, no
publication, no conformance claims, no edits to ADR 0002, no raw-file
modification, no writes into `database_offline/`, no real-data artifacts
in version control, and no silent selection of uncertain timestamps,
service dates, mappings, or trips.

---

# Spike 02: APC-assisted boundary refinement, terminal episodes, anchor-and-align

**Status: experimental, diagnostic, non-publishable.** Spike 02 extends
this package (new `spike02_*`, `boundary_*`, `provisional_*`,
`terminal_*`, `adaptive_attachment`, `gtfs_anchors`, `anchor_alignment`,
`refined_segments` modules) without modifying any Spike-01 module
semantics — the frozen `r1c` baseline remains exactly re-runnable. It
does not close R1, accept any milestone, produce TIDES resources, or
select approved thresholds.

**Architecture caveat (load-bearing):** the approved merge plan is
acyclic; its Appendix C explicitly flags "APC refines segment
boundaries" as conflicting with the approved order. Spike 02 tests that
idea strictly as a *candidate plan revision* under a hard one-pass
restriction (`boundary_refinement.py`; no API can feed refined segments
back into refinement).

Pipeline: provisional X4-A activity runs → APC source-signal derivation
(positivity only; D-06 open; Q0–Q3 experimental tiers) → versioned
terminal-cluster hypotheses from GTFS pattern endpoints (the 1652 =
Wilson Station association is a D-10-open hypothesis with recorded
provenance) → trip-agnostic APC–AVL alignment edges → terminal/turnback
episodes with four timestamp hypotheses → one-pass boundary refinement
(V1 AVL-only / V2 conditional Q3 precedence / VX intentionally unsafe
unconditional) → frozen refined segments with ping conservation → final
attachment with adaptive `T_mid` bounds and a terminal-specific path →
GTFS terminal/per-stop anchor candidates with dual direction-polarity
hypotheses (D-11 open) → bounded order-preserving anchor-and-align.

Run (from `agencies/ttc`):

```sh
PYTHONPATH=experiments/r1_design_spike/src \
    uv run python -m r1spike.run_spike02 --slice all --run-id <id>
# outputs: artifacts/experimental-r1-spike02-<id>-<slice>/ (gitignored)
uv run pytest experiments/r1_design_spike/tests   # Spike 01 + 02 suites
```

Findings: `agencies/ttc/docs/r1-boundary-refinement-spike-findings.md`.
