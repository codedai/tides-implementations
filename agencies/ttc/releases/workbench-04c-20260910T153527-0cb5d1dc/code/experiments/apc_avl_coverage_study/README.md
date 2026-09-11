# APC–AVL coverage study

This is the isolated diagnostic study commissioned on 2026-09-08: inspect the
raw TTC AVL/APC fixture, inventory edge cases, compare segmentation and merge
combinations, and evaluate feasibility and evidence using pinned TIDES v1.0
semantics. The preceding discussion specifically asks for APC–AVL integration
before GTFS schedule matching and for unequal source coverage within a candidate
performed trip.

The user's new instruction authorizes raw-data inspection and this new
diagnostic factorial. It does not change the existing production pipeline or
promote the older frozen labeling/factorial experiment. This study does not read
GTFS, old human judgments, or the old holdout artifacts. It does inspect the
entire available raw month: any overlapping source windows are therefore not
untouched raw-data holdouts for rules selected from this study. Future accuracy
validation needs a separately declared evaluation design, preferably fresh
source windows. Results are experimental
chunks and source-record associations, not certified performed trips or TIDES
resources. No new approval attribution or milestone acceptance is inferred.

## Reproduce

Run from `agencies/ttc`, using the existing locked Python environment. The
temporary cache location accommodates a read-only user-level cache. Raw inputs
stay under `database_offline/`; all row-level artifacts are owner-only and
gitignored under `artifacts/apc-avl-coverage-study-20260908/`.
Choose a previously unused rerun directory; do not overwrite the evaluated
artifacts. The full inference run took about 20 minutes and 6.93 GiB peak process
RSS on this workstation, excluding the raw scan and independent analysis.

```sh
umask 077
STUDY_OUT=artifacts/apc-avl-coverage-study-20260908/rerun01

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m experiments.apc_avl_coverage_study.raw_audit \
  --output "$STUDY_OUT/raw_audit"

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m experiments.apc_avl_coverage_study.run_study \
  --audit "$STUDY_OUT/raw_audit" --out "$STUDY_OUT/factorial02"

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m experiments.apc_avl_coverage_study.probes \
  --audit "$STUDY_OUT/raw_audit" --out "$STUDY_OUT/probes01"

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m experiments.apc_avl_coverage_study.probes --selection-mode edge \
  --audit "$STUDY_OUT/raw_audit" --out "$STUDY_OUT/probes-edge02"

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m experiments.apc_avl_coverage_study.segmentation_counterexamples \
  --output "$STUDY_OUT/segmentation_counterexamples.json"

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m pytest tests experiments/apc_avl_coverage_study/tests -q \
  --junitxml="$STUDY_OUT/verification-tests.xml"

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m experiments.apc_avl_coverage_study.analyze_results \
  --run "$STUDY_OUT/factorial02" --audit "$STUDY_OUT/raw_audit" \
  --probes "$STUDY_OUT/probes01" --out "$STUDY_OUT/analysis02"

UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync ruff check \
  experiments/apc_avl_coverage_study tests/test_raw_audit.py
```

The factorial refuses an existing output directory. `--limit N` is a
hash-ordered smoke test and is explicitly recorded as incomplete fixture scope.
The full run uses all vehicles across the complete available month; it does not
partition trip inference at midnight. Its 21 configurations comprise three
segmentation methods, a nominal temporal baseline, and two evidence-based merge
methods at each of three exploratory threshold settings.

## Methods

| Component | Method | Interpretation |
|---|---|---|
| Segmentation | `gap_only` | Split at long AVL gaps and source-quality barriers. A baseline that may combine many actual trips. |
| Segmentation | `avl_context` | Add sustained source-context changes and geometric reversals; source-trip IDs alone are not boundaries. |
| Segmentation | `apc_assisted` | Add APC source-direction changes and observed stop retracing within AVL coverage. Requires review of APC evidence reuse and grain. |
| Merge | `time_only` | Temporal baseline after common source-quality filtering, with residuals retained for evaluation. This is not the legacy notebook join. |
| Merge | `spatiotemporal` | Local vehicle/time/space support with bounded interpolation, null competition and owner margin. Abstain on intervals spanning candidate boundaries. |
| Merge | `sequence_extension` | Preserve direct assignments; add separately labeled, bounded APC continuation hypotheses supported by three distinct direct stop anchors. |

`study_config.json` defines all merge thresholds. `segmentation.DEFAULT_CONFIG`
defines segmentation thresholds; its exact source hash is in the run manifest.
Both are exploratory. Similarity scores are not probabilities, residuals are
optimization features rather than independent truth, and the study does not
establish a universal time/distance threshold.

Actual GPS observations remain in the source table. Short interpolation is a
candidate feature referencing real bracketing rows; it never creates a GPS ping.
The observation envelope has separate per-source start/end fields. Actual trip
start/end remain unknown, and a source calendar date is never renamed service
date. APC counts are never repeated across GPS rows or filled with zero when
APC is missing.

## Unequal coverage and uncertainty

Direct matches require overlap with observed AVL coverage. An APC-only prefix
or suffix may receive an inferred chunk association with no GPS reference.
Inferred records cannot become new anchors. Both endpoints of an inferred APC
interval must fall within the declared horizon from its supporting anchors.
Unknown grain, unresolved local evidence, implausible travel, source-direction
changes, observed stop revisits, and intervening chunk boundaries interrupt
continuation. A stop revisit can also be a legitimate loop: the current rule
abstains conservatively and does not decide which interpretation is correct.

The first source observation is not itself an operational boundary. DST
ambiguity, nonexistent local times and source-quality barriers are explicit;
they are not resolved by chronological sorting or a default fold. Clock shifts
in robustness probes are deliberate perturbations, not estimated corrections
to the fixture. The equivalence of source vehicle namespaces and the underlying
APC export semantics remain assumptions requiring external confirmation.

## Evaluation artifacts

- `raw_audit/`: SHA-256 source manifests, canonical row-level inputs, complete
  implemented edge-check inventory, bounded examples, source-calendar coverage.
- `factorial02/`: canonical evaluated run; configuration, input/code hashes,
  candidate GPS support, chunk boundaries, one disposition per APC row per
  combination, per-source coverage envelopes, per-vehicle metrics and totals.
- `analysis02/`: final independent accounting, evidence-stratified metrics and
  scoped rubric results. `analysis01/` is retained as the initial checker run;
  final verification tightens malformed-reference and conservation checks
  without changing the inference results.
- `probes01/`: twelve preselected windows for artificial missingness and
  perturbation probes; `probes-edge02/` separately tests eight raw-feature-selected
  windows around terminal overlap, DST, month-end and internal GPS gaps.
- `segmentation_counterexamples.json`: constructed negative controls expose
  duplicate-sensitive persistence/reversal detection and shared terminal-dwell
  representation gaps. Passing checks on sampled windows does not cancel these
  counterexamples.
- `smoke01/` and interrupted `factorial01/`: retained development diagnostics;
  they are not the final comparison. A first-coverage-boundary defect was fixed
  before restarting the full evaluated run.

`probes-edge01/` is retained as an earlier probe diagnostic. Its DST window
centering and comparison of duplicate uncertain singleton chunks were corrected
in `probes-edge02/`; core inference code remained unchanged.

See the [raw edge-case report](../../docs/apc-avl-raw-edge-cases-20260908.md),
[evaluation standard](../../docs/apc-avl-coverage-evaluation-standard.md), and
[method comparison](../../docs/apc-avl-method-comparison-20260908.md).
The JSON rubric distinguishes mandatory TIDES semantics, project integrity
invariants, publication gates and exploratory thresholds. Applicable checks
that are not implemented or measured remain `not_evaluated`.

Independent attachment and actual-trip boundary accuracy are unknown. Synthetic
tests establish behavior on constructed examples. Masking tests measure
recoverability relative to unmasked algorithm outputs; they are not ground
truth, and a stable assignment may still be wrong. Passing integrity checks or
achieving high association coverage does not establish publishable TIDES trips.
