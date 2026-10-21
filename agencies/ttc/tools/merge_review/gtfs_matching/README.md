# Experiment 03: route 29 schedule evidence

Scope is the original local calendar day **2023-11-06**, route **29** only.
The input is a completed experiment-02 run: exact AVL deduplication and PI/PO
filtering are inherited, not rerun with a different definition. Existing direction
reversals are retained; the current version additionally splits each scoped
direction candidate at joint APC/AVL blank gaps strictly over 30 minutes. Excluded
929 observations remain traceable through the parent and the scope audit;
omitted AVL service sections force a projection separator.

These candidates are not verified `trip_performed` identities. This experiment
does not publish TIDES tables, infer vehicle/block identity, correct clocks,
aggregate passenger totals or introduce GTFS as ground truth.

## Stages and persisted evidence

| Stage | Module | Evidence |
| --- | --- | --- |
| Date/version selection | `feed.py` | Archive and manifest hashes, competing feeds, active services, previous-service-day tail trips |
| Joint-observation gap refinement | `gaps.py` | Parent lineage, strict threshold, interval-union gap witnesses, lossless child membership |
| Canonical validation | `canonical.py` | Used shapes, geometry and stop-order validation, direction crosswalk, independent reference axes |
| Sequence projection | `projection.py` | Per-reference beam-Viterbi attempts, accepted/rejected points, post-sequence distance gate, APC stop intervals |
| Rejection diagnosis | `off_reference.py` | Raw rejected IDs, duration/movement/terminal/passenger context, conservative run hypotheses, fragments |
| Multiscale ownership | `windows.py` | Disjoint one-minute atomic windows, 5/10-minute contexts, optional event windows, normalized source weights |
| Candidate retrieval | `retrieval.py` | Service-date-aware local-passage/headway buffers, rescue candidates and high-recall union |
| Local alignment | `scoring.py` | Robust constrained DTW, partial Fréchet, directed p95 Hausdorff, quality/coverage penalties, shared delay DP |
| Whole candidate | `integration.py` | Weighted local statistics, whole-fragment alignment, delay median/MAD, single versus one-change-point models |
| Calibration | `calibration.py` | Verified-label opt-in, disjoint group validation, calibration diagnostics; no invented probabilities |
| Conservative final decision | `decision.py` | Competing evidence remains in ambiguity checks even just beyond the absolute cost cutoff |
| Decision-scope comparison | `partial.py`, `multimode.py` | Global and independent early/middle/late decisions over the same immutable projection |
| Day overview | `overview.py` | All source segments, reference-separated projected fragments, one accepted or explicitly failed diagnostic schedule overlay |

`gtfs/canonical.json` is the shared reference/schedule bundle. Each `cases/*.json.gz`
retains projection alternatives, APC evidence, windows, retrieval, local scores,
all integrated candidates, diagnostics and final candidates. Raw retained
observations remain separate Parquet files. `scope/*.json` records exclusions.
`output_sha256.json` and parent/source hashes make input/output changes detectable.
The workbench never reruns a projection while rendering it. Case logs use
lossless compressed JSON. HTTP responses retain all raw rows, all window
summaries and all final comparisons, but omit large local-score matrices and
repeated window-membership maps; `intermediate_artifact` identifies the complete
hash-verified log.

## Four decision modes

“Global” means the whole **individual observed segment**, not a vehicle-day or
fleet-wide joint optimization. Early / middle / late use approximately one third
each of successfully projected forward travel distance, in chronological order.
The distance counter advances only between adjacent valid observations on the
same canonical reference and continuous fragment, with the existing gap limit.
It uses forward high-water progress so backward jitter does not inflate length.
OFF_REFERENCE, low-quality projections, GPS gaps, reference changes and service
separators do not contribute a connecting distance. No incomparable canonical
axes are subtracted from one another.

One-third and two-third boundaries snap forward to real AVL timestamps; boundary
observations belong to the later part. No synthetic boundary points are inserted,
and the actual retained distance may differ slightly from exactly one third.
Rejected observations inside a selected time slice remain in its discarded-
coverage denominator. The other two thirds are excluded from matching, but stay
visible as original context in the workbench. Untimed records remain in the raw
artifact and are explicitly unassigned to partial modes. A segment with no valid
positive projected travel has NO_EVIDENCE partial selections, not a fallback to
global matching.

APC remains stop-time interval evidence. Its intervals are intersected with the
selected third in a derived copy, with original endpoints and clipping metadata
recorded in `selection.apc_interval_clips`. Passenger counts are not prorated or
summed across the three modes; an interval spanning a cut can support both sides.
Original APC fields are unchanged. Alternative projection references remain
available to each scheduled candidate; the partition does not lock its reference.

Each partial mode independently constructs windows, retrieves from the full
GTFS pool, scores, fits its own shared delay, checks whole-selected-part coverage,
and applies the same final gates. It does **not** reuse a global shortlist, global
delay or another part's scores. Coverage thresholds are measured against the
selected evidence, not against the length of the full original segment. Final
statuses and null/un-calibrated probabilities retain their original meaning.

These are **offline decision-scope experiments**, not causal real-time forecasts:
the common sequence projection was computed using the entire observed segment.
Evaluating operational online decisions would also require prefix-only projection
and a no-future-data experiment.

Artifacts retain `result` as the original global result and add
`matching_modes.{global,early,middle,late}.{result,selection}`. The append-only
replay below reuses all historical projections and global results exactly:

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.gtfs_matching.multimode \
  --source run-20231106-20260909T155703-8358896eb468 --workers 8
```

Normal new runs also compute the four modes by default
(`matching_partial_modes_enabled=true`). Setting the boolean false runs the
global baseline only. Historical runs expose only modes actually computed;
requests for unavailable modes fail explicitly.

### Experiment 03d: stronger absolute-delay prior

Experiment 03d starts from the completed 03c run and raises
`score_delay_prior_weight` from **0.05 to 1.0**. This is an explicit run setting;
the historical default and all completed 03c results remain unchanged. The
absolute-delay penalty is `weight * abs(estimated_delay_s) / 3600`, symmetric for
early and late operation: a 15-minute offset contributes 0.25 instead of 0.0125
to the aggregate cost. This remains a heuristic prior, not calibrated evidence
of operational truth.

The same parameter also influences local delay-state selection and the boundary
model. Therefore this replay recomputes matching for **all four modes**, including
the global result, rather than adjusting only displayed aggregate scores. Raw
observations, canonical references, segmentation, projection and decision-third
membership are reused without alteration. Cost/evidence/ambiguity thresholds
are unchanged; the stronger prior can lead to abstention as well as reordering.
The new run records changed scoring configuration and replay provenance, and
preserves the original run as its pinned source. Each candidate card displays
the run's actual weight and the aggregate absolute-delay penalty.

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.gtfs_matching.multimode \
  --source run-20231106-20260909T170835-6cb6e4feb48a --workers 8 \
  --score-delay-prior-weight 1.0 \
  --label 'Experiment 03d · Route 29 · Stronger time-shift penalty (weight 1.0)'
```

The persisted 03d run (`run-20231106-20260909T183705-d67ffa30723d`) recorded
its label in Chinese at run time; the English label above is its translation.

The workbench switches persisted modes with no matching recomputation or review
write. `/api/case`, `/api/catalog` and `/api/day-overview` accept the validated
`matching_mode` query parameter. Published `overview/{mode}.json` files are hashed
with the other outputs, avoiding hundreds of heavyweight case reads per page.

The day overview separates NORTH/SOUTH and incompatible canonical references.
It shows every candidate segment, including those without usable AVL. Valid AVL
fragments are not connected across rejected runs/gaps; APC is drawn as intervals.
Each case receives at most one GTFS overlay: the lowest-cost aggregate passing
all candidate evidence/cost gates, otherwise the first genuinely failed scored
candidate, explicitly marked as diagnostic fallback. No scored candidate means
no invented overlay. Acceptance here does not assert unique trip identity.
Scheduled timestamps are always the original timetable times, never shifted to
the fitted delay. Selecting an overview case opens the same run and decision
mode in the detailed workbench.

## Current feed assumptions

The repository has no approved manifest for this day. The experiment explicitly
pins the higher-priority covering draft `ttc-d092742e6a5b`, without approving it.
Route short name 29 maps to route ID 70291 in that version. It supplies 300
active trips for November 6 plus 28 November 5 schedule tails. Four used shapes
produce three canonical references: two southbound references remain separate
because their terminal geometry cannot be safely unified.

Direction IDs are feed-local: north/south is inferred from shape and ordered-stop
geometry, not assumed from numeric 0/1. This inferred operational crosswalk is
explicitly not independently verified. Schedule times beyond 24:00 remain on
their original service date, as specified by the
[GTFS Schedule reference](https://gtfs.org/documentation/schedule/reference/).
All source and schedule timestamps use the same **naive local wall-clock axis**;
they are not UTC instants. No neighboring-calendar-day actual observations are
loaded.

## Quality and abstention

`segmentation_joint_gap_s` defaults to **1800 seconds**, with a strict `>` test:
exactly 30 minutes is not a new boundary. After route-29 selection, each original
vehicle/direction candidate is independently split using the union of genuine
AVL timestamps and ordered APC arrival/departure intervals. Unknown/conflicting
direction labels and poor spatial quality still indicate a record is present;
neither the direction-evidence AVL 30-second hold nor projection filtering is
used to fill a gap. Invalid APC intervals contribute a real endpoint only.
Missing or unreliable timestamps cannot bridge a gap and are retained separately
if their parent is split. Empty time before/after observed coverage is not itself
split into an empty trip.

Each child receives independent sequence projection, windows, retrieval, scoring,
shared delay, whole alignment and final decision. No alignment crosses the new
boundary. APC-only children are retained without inventing an AVL trajectory.
Child IDs and exact source membership are recorded in the catalog alongside
`parent_case_id` and both gap endpoints; raw parent annotations remain unchanged.
The per-vehicle scope audit records gap witnesses and conservation checks.

`max_projection_distance_m` defaults to 100.0. Multiple candidates are evaluated
as whole per-reference sequences before the distance gate. In-gate sequence
rescue is evaluated as another complete path, not assembled from independently
chosen points. The finite top-K/beam search is approximate: “all references
checked” is not a proof that every mathematical path was exhaustively searched.

`OFF_REFERENCE` rows remain in raw and diagnostic outputs but are masked out of
schedule geometry, delay and distance scores. Sustained rejection separates
service fragments. Off-reference classifications are hypotheses, not confirmed
deadheading or detours. APC with coordinates uses geometry only as a quality
gate; its progress comes from a GTFS stop-code crosswalk. Coordinate-less APC
can retain stop-time evidence. Ambiguous stop positions remain ambiguous.

Windows lacking configured minimum points, duration or progress report
`NO_EVIDENCE`. Coverage is reported separately for points, duration, progress
and contiguous runs; discarded-coverage penalties prevent a small surviving
subset from creating a deceptively good score. Alignment sampling, when
necessary, records original/sample counts, source IDs and represented weights.

Whole alignment verifies original AVL observations against a fragment-local
shared smooth delay process, with separate schedule-aligned point, duration
and supported-window coverage. `MATCHED` requires successful whole alignment;
good local scores alone cannot override a failed whole-candidate check.
Window delay transitions must preserve corrected-clock order. Fragment-local
weighted isotonic fitting then enforces nondecreasing `t - delay` at the actual
observation-supported anchors, preserving raw estimates and adjustment amounts.
Unsupported observations do not borrow a fitted delay across a gap. A run cannot
complete if fitted timestamps no longer pair with their original source rows or
if any supported fragment's corrected clock goes backwards. The independent
audit additionally checks persisted fit knots and scoring-fragment continuity.

Final statuses are `MATCHED`, `AMBIGUOUS`, `PATTERN_ONLY`, `UNMATCHED` and
`BOUNDARY_UNCERTAIN`, with zero to three displayed candidates. A `MATCHED`
heuristic decision is not calibrated accuracy. Without independent verified
labels, candidate probability is `null`, calibration status explains why, and
support/confidence scores are explicitly not probabilities. The optional
vehicle-service-day/block sequence model is not enabled in this implementation.

## Run and inspect

From `agencies/ttc`, using the existing environment:

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python -m tools.merge_review.gtfs_matching --parent run-20231106-20260908T220649-9be72540c029
```

Use `--debug-dir` to run in an independent acceptance workspace. `--config`
accepts a JSON object of the same bounded GTFS parameters exposed by the web
form; filesystem/feed identity changes are not web parameters. Four independent
worker processes are used by default, configurable from one to eight. Outputs
are append-only; failed runs have a failure record and no completion marker.

The GTFS schedule strip appears immediately below the dual-source timeline;
the canonical schedule/progress comparison appears below observed GPS progress.
Select a candidate/reference and inspect the same source records on all charts.
Browse widths include 1/2/5/10/15/30/60/120 minutes, the full range, and a custom
seconds/minutes duration. Window selection does not silently reload or discard
the source evidence.

A separate **gate-failed diagnostic candidates** panel shows up to three already
scored hypotheses, ordered by saved aggregate cost. Each displays failed metrics
and the run's recorded thresholds, with independently selectable schedule and
canonical-progress overlays. Hypotheses already in the final list are not
duplicated; passing hypotheses outside its display cap are not called rejected.
Diagnostics default visible only when the final list is empty, and never change
the decision, formal candidate count, probability, thresholds or persisted run.
Retrieved-only trips with no aggregate score are not assigned invented scores.
This read-only HTTP presentation is implemented in `diagnostic_candidates.py`.

For a change confined to final decision rules, `redecide.py` can append a new
run from completed, hash-verified heavy evidence. It does not repeat projection,
retrieval, local scoring or whole alignment. Upstream code hashes remain attached
to the original computation; final-decision code hashes are recorded separately.
All heavy result fields, source rows and schedules must remain exactly unchanged.
The original heavy run is retained; replaying a previous replay is not allowed.

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python -m tools.merge_review.gtfs_matching.redecide --source <completed-heavy-run-id>
```

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python -m pytest tools/merge_review/tests tools/merge_review/gtfs_matching/tests -q
```

HTTP/browser tests require permission to bind localhost. Browser acceptance is
in `tools/merge_review/tests/browser_gtfs_smoke.mjs`; use an independent test
workspace, or its existing-run read-only mode. Never write acceptance notes to
the official review database.

`audit.py` is an independent, read-only check of completed intermediate logs;
it does not rerun scoring and cannot establish matching accuracy:

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python -m tools.merge_review.gtfs_matching.audit artifacts/merge-debug-20231106/runs/<run-id>
```
