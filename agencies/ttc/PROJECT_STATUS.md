# TTC TIDES Prototype: Project Status

| | |
| --- | --- |
| Status date | 2026-10-20 |
| Phase | Offline research prototype. Experiment 05 delivers a reproducible, versioned raw-to-TIDES v1.0 batch pipeline for TTC route 29 on selected service dates; the governed merge plan (S3 onward) remains frozen pending re-review; no output is calibrated against independent labels or authorized for publication. |
| Primary contact | Jiahao (GitHub `codedai`) |
| Repository | `agencies/ttc`, one contributor subfolder of the MobilityData-stewarded TIDES implementations showcase. Remote `origin`: `https://github.com/codedai/tides-implementations.git`, branch `main`. Apache 2.0; the contributor retains copyright. |
| Repository state | The `agencies/ttc` history on `main` runs from `3809a76` to `878089a` (both 2026-09-10), with tags `pre-experiment-05-20260910` (commit `3809a76`) and `experiment-05-v1` (commit `44c1aed`). The maintenance cleanup described in [section 11](#11-maintenance-log) — English-only sources, this status document, a re-cut frozen workbench release, the removal of the six superseded 2026-09-10 releases, de-linked dead Markdown links and a corrected Run 01 status document — was applied to `agencies/ttc` on 2026-10-20. `878089a` stays the reference point for everything that cleanup removed: the six release directories and the pre-translation source strings are recoverable from that commit. |

## How to read this document

This document is the single status and planning reference for the TTC
subfolder of `tides-implementations`. It is written for MobilityData
reviewers, TTC and vendor stakeholders, and future contributors who have not
followed the work. Sections 2 to 5 record what exists, sections 6 to 9 what
does not and why, and sections 10 and 11 where to look and what changed.

The companion [README](README.md) is the technical entry point: what the
directory is, how to run Experiment 05 and the frozen workbench, the Python
environment, the GTFS preprocessing commands, the source snapshot and audit
findings, the architecture and merge-strategy summaries, and the repository
layout. Status, roadmap and open decisions live only here. The detailed
documents under [`docs/`](docs/) are the evidence.

Every count, run identifier, date and hash in this document is taken from a
committed document, a configuration file, or a locally retained run manifest
named next to it. Dates before 2026-09-10 come from document headers and
accepted decision records, because the git history of `agencies/ttc` begins at
commit `3809a76` on 2026-09-10.

The project applies strict epistemic labels, and this document keeps them.
"Assigned", "matched", "direct" and "accepted" are status names produced by
specific algorithms under stated assumptions. None of them is a measured
accuracy. No reviewed independent labels exist, so precision, recall,
calibration and architecture-superiority statements are undefined throughout.

## 1. Purpose and scope

The prototype investigates whether local Toronto Transit Commission (TTC)
exports of GTFS, Automatic Vehicle Location (AVL) and Automatic Passenger
Counter (APC) data can be reconciled and converted into a
[TIDES](https://tides-transit.org/) v1.0 data package, and documents what
evidence that conversion can and cannot support.

Three constraints were fixed at the start ([ADR 0001](docs/decisions/0001-offline-python-prototype.md), 2026-08-05) and still hold:

1. Offline first. All inputs come from `database_offline/`; a run must not
   require a cloud service, external database or live API.
2. Python transformations. Polars, PyArrow and Frictionless; no dbt, SQL or
   warehouse layer.
3. Proof before production. The goal is a reproducible, inspectable
   end-to-end conversion of the available sample, not scheduling, ingestion,
   monitoring or deployment.

TIDES v1.0 tables in scope (pinned to upstream commit
`b2cb23178e00cb58dbdb1b9a5a8626818278ecbb`, tag `v1.0`): `vehicles`,
`vehicle_locations`, `stop_visits`, `passenger_events`, `trips_performed`,
plus `datapackage.json`. `devices` is vendored but not populated, and all
device identifiers are left empty rather than pointing at absent rows. The
`passenger_events` convention is specified in the
[APC stop-visit and passenger-event policy](docs/experiment05-apc-events.md)
and measured in the [compliance audit](docs/experiment05-tides-compliance.md);
the resource list in the [TIDES export contract](docs/tides05-contract.md)
predates that policy and does not yet name the table.

What the prototype is not: a production TTC data platform, an official TTC
publication, a TIDES certification, or a legal opinion on data use. Rail,
fares, operators and device enrichment are out of scope.

No TIDES publication is authorized. [ADR 0002](docs/decisions/0002-merge-plan-review-disposition.md)
freezes stage S3 and all downstream publication-bound work of the governed merge
plan; [ADR 0003](docs/decisions/0003-isolated-r1-diagnostic-experiments.md)
forbids any publication from the R1 experiments; every Experiment 05 package is
marked `research_only: true`; and authorization to redistribute the TTC source
data has never been established (decision D-13).

## 2. Status at a glance

Status vocabulary: Complete (delivered and reviewed, further work not required
for the stated scope), Frozen (deliberately held at a hash-locked state),
In progress, Blocked (cannot proceed without a named external input), Not
started.

| Workstream | Status | Key evidence | Detail |
| --- | --- | --- | --- |
| Scope, constraints, source-data audit | Complete (2026-08-05) | AVL 6,202,996 rows, APC 573,596 rows, 14 GTFS archives audited; `tatripId` not a GTFS key; APC `StopID` is a `stop_code` | [Source data audit](docs/source-data-audit.md), [ADR 0001](docs/decisions/0001-offline-python-prototype.md) |
| GTFS preprocessing CLI (`ttc-tides gtfs ...`) | Complete (reviewed 2026-08-06) | 2023-11-05 acceptance: 28,291 scheduled trips, 0 quarantined, 0 fatal; 92 GTFS unit tests at review (`tests/unit/` holds 109 today, after the S0–S2 additions; see [section 4.9](#49-test-suites)) | [GTFS implementation review](docs/gtfs-implementation-review.md) |
| GTFS feed manifest approval (G0.2 / D-15) | Blocked | All 14 draft rows `candidate`; `config/feed_manifest.csv` absent; experiments use a recorded `allow_candidate` override | [Merge plan §4, §16](docs/merge-implementation-plan.md) |
| Architecture and governed merge plan (S0–S11) | Frozen | ADR 0002: architecture approved, S3+ frozen pending section-scoped re-review of the R1–R6 revision; M0–M6 unaccepted | [Merge plan](docs/merge-implementation-plan.md), [Review](docs/merge-plan-review.md), [ADR 0002](docs/decisions/0002-merge-plan-review-disposition.md) |
| S0–S2 diagnostic runner (`ttc-tides diagnostic run`) | Complete (diagnostic-provisional) | Full-fixture run 2026-08-06: AVL 6,202,996 and APC 573,596 landed, 0 rejected | [Architecture](docs/architecture.md), `src/ttc_tides/pipeline.py` |
| R1 design spikes 01 / 02 / 02R | Complete (frozen runs `r1c`, `s2b`, `p4`) | Verdict: INCONCLUSIVE, NO REVIEWED HOLDOUT LABELS; R1 remains open | [Spike 01](docs/r1-design-spike-findings.md), [Spike 02](docs/r1-boundary-refinement-spike-findings.md), [Spike 02R](docs/r1-spike02r-kernel-repair-findings.md), [ADR 0003](docs/decisions/0003-isolated-r1-diagnostic-experiments.md) |
| Human labeling v1 and audit | Complete (2026-08-14) | 18 pilot cases first-pass labeled; 0/18 pass the strict calibration gate; factorial BLOCKED | [Protocol v2 (why v2 exists)](docs/human-labeling-protocol-v2.md) |
| Labeling protocol v2, blind second pass (Run 01) | In progress | Protocol frozen 2026-08-14; 2 of 18 parents saved (2026-09-08); 0 of 7 required scheduled-trip passes; 0 of 21 child tasks; 0 adjudicated | [Run 01 status](docs/human-labeling-v2-run01-status.md) |
| Pre-registered calibration/holdout factorial | Blocked (NO_GO) | 6 of the 14 mechanical conditions are false; a separate explicit human authorization is required even when all are true | [Protocol v2](docs/human-labeling-protocol-v2.md) |
| APC–AVL coverage study (21 combinations) | Complete (2026-09-08, diagnostic) | Best direct association 98.04% of APC rows; accuracy undefined | [Method comparison](docs/apc-avl-method-comparison-20260908.md), [Evaluation standard](docs/apc-avl-coverage-evaluation-standard.md) |
| APC–AVL direct-evidence optimization v2 | Complete (2026-09-08, diagnostic) | `full02`: direct 547,499 + partial 4,939 of 573,121 eligible; strong ±60 s clock sensitivity | [Optimization](docs/apc-avl-direct-evidence-optimization-20260908.md), [Probes](docs/apc-avl-direct-evidence-probes-20260908.md) |
| Merge-review workbench, Experiments 01 to 04c | Complete; 04c frozen | 2023-11-06, route 29, 31 vehicles, 306 segments, four decision modes; SHA-256-locked release selected by `config/workbench-release.json` | [Frozen workbench](docs/frozen-workbench.md), [Exp 03 results](tools/merge_review/gtfs_matching/RESULTS.md), [Exp 04](docs/experiment04-continuous-matching.md), [04b](docs/experiment04b-delay-weights.md), [04c](docs/experiment04c-occurrence-plausibility.md) |
| Experiment 05 raw-to-TIDES pipeline | Complete for route 29 on 2023-11-06 and 2023-11-20 (research package) | 352 and 313 performed trips; per-mode assignment 64.5–72.5%; 1,483 and 1,303 invariant checks, 0 errors; pinned TIDES v1.0 validation passes | [Design](docs/experiment05-design.md), [Pipeline overview](docs/ttc-tides-pipeline-and-comparison.md), [Compliance audit](docs/experiment05-tides-compliance.md) |
| Independent accuracy evaluation and calibration | Not started (blocked on labels) | Every run records `independent_accuracy_evaluation: false`; calibration reports `UNCALIBRATED` | [04c calibration inputs](docs/experiment04c-calibration-inputs.md) |
| Cross-midnight operating-day reconstruction | Not started | Observation window is a local calendar day; `feed_include_previous_day_tail = false` | [Design: time contract](docs/experiment05-design.md#time-identity-and-source-preservation-contract) |
| Vendor field semantics, data-use authorization | Blocked (external) | D-06 to D-13 require a TTC data owner or vendor; none assigned | [Section 7](#7-open-decisions) |
| English-only sources and documentation consolidation | Complete (2026-10-20) | Zero tracked files outside `releases/` contain CJK characters; the 42 text files that do are all inside the hash-pinned `releases/pre-experiment-05-20260910/` snapshot and are deliberately unedited (a byte-level scan also flags two compressed `cases/*.json.gz` artifacts there, whose decompressed JSON carries none); both web interfaces are English-only | [Section 11](#11-maintenance-log) |
| Workbench release retention | Complete (2026-10-20) | `releases/` holds exactly two directories in the working tree: the re-cut `workbench-04c-20261021T013832-cddd406e`, which `config/workbench-release.json` now selects, and the pre-05 snapshot; the six superseded 2026-09-10 releases were removed with `git rm` and remain in history at `878089a` | [Section 5.3](#53-frozen-release-pointer), [frozen workbench](docs/frozen-workbench.md) |

## 3. Timeline

| Date | Milestone | Evidence |
| --- | --- | --- |
| 2026-08-05 | Project scaffold: scope and constraints, source-data audit, ADR 0001 (offline Python prototype), `pyproject.toml`, immutable-input and artifact boundaries | [Audit](docs/source-data-audit.md), [ADR 0001](docs/decisions/0001-offline-python-prototype.md) |
| 2026-08-06 | Architecture and data contracts; GTFS preprocessing stage implemented, reviewed and accepted on 2023-11-05 real data; merge implementation plan revised for review blockers R1–R6; professional review issued; ADR 0002 accepted; S0–S2 diagnostic run on the full fixture | [Architecture](docs/architecture.md), [GTFS review](docs/gtfs-implementation-review.md), [Merge plan](docs/merge-implementation-plan.md), [Review](docs/merge-plan-review.md), [ADR 0002](docs/decisions/0002-merge-plan-review-disposition.md) |
| 2026-08-07 | R1 Spike 01 frozen as run `r1c` on three deterministic slices; methodology write-up of the predecessor notebook project | [Spike 01](docs/r1-design-spike-findings.md), [Predecessor methodology](docs/previous_proj_data_integration_methodology.md) |
| 2026-08-08 | Spike 02 frozen as run `s2b`; ADR 0003 accepted (bounded authorization for isolated R1 experiments); Spike 02R frozen as run `p4`; factorial pre-registration frozen; 18-case labeling packet produced | [Spike 02](docs/r1-boundary-refinement-spike-findings.md), [ADR 0003](docs/decisions/0003-isolated-r1-diagnostic-experiments.md), [Spike 02R](docs/r1-spike02r-kernel-repair-findings.md) |
| 2026-08-09 | v1 labeling UI (Streamlit, offline, bias-controlled) | [v1 UI](tools/labeling_ui/README.md) |
| 2026-08-13 | First-pass human labels on all 18 pilot cases | [`tools/labeling_results/`](tools/labeling_results/) |
| 2026-08-14 | Human label audit v1 (0/18 calibration-ready); Labeling Protocol v2 frozen; Blind Adjudication Run 01 initialized; v2 blind UI built | [Protocol v2](docs/human-labeling-protocol-v2.md), [Run 01 status](docs/human-labeling-v2-run01-status.md), [v2 UI](experiments/r1_design_spike/labeling_ui_v2/README.md) |
| 2026-09-06 to 2026-09-08 | Blind second-pass review sessions; two parent cases saved under Protocol v2 | local run directory `artifacts/human-labeling-v2-run01/` |
| 2026-09-08 | Raw APC/AVL edge-case audit; TIDES-based evaluation standard; 21-combination coverage study; direct-evidence v2 (`full02`, `probes02`); merge-review website; Experiments 01, 01b, 02 | [Edge cases](docs/apc-avl-raw-edge-cases-20260908.md), [Standard](docs/apc-avl-coverage-evaluation-standard.md), [Comparison](docs/apc-avl-method-comparison-20260908.md), [Merge review](tools/merge_review/README.md) |
| 2026-09-09 | Experiments 03, 03b, 03c, 03d (discrete GTFS matcher, four decision modes) | [Exp 03 results](tools/merge_review/gtfs_matching/RESULTS.md) |
| 2026-09-10 | Experiments 04, 04b, 04c (continuous scorer); frozen workbench releases; retention registry and purge of Experiments 01–03c; pre-Experiment-05 snapshot and tag; first git commit `3809a76` | [Exp 04](docs/experiment04-continuous-matching.md), [04b](docs/experiment04b-delay-weights.md), [04c](docs/experiment04c-occurrence-plausibility.md), [Frozen workbench](docs/frozen-workbench.md) |
| 2026-09-10 | Experiment 05 implemented (`b26ed05`), hardened (`d5b7b8a`), accepted (`2059ee9`, `44c1aed`); canonical runs for 2023-11-06 and 2023-11-20; departure-proxy passenger-event policy adopted; visualization layer; TIDES compliance audit; checkpoint commit `878089a` | [Design](docs/experiment05-design.md), [Overview](docs/ttc-tides-pipeline-and-comparison.md), [Compliance audit](docs/experiment05-tides-compliance.md) |
| 2026-10-20 | Maintenance cleanup: English-only sources, this status document, `outputs/` added to `.gitignore`, the frozen workbench release re-cut from the English sources and the pointer repointed to it, the six superseded 2026-09-10 releases removed, 30 dead Markdown links de-linked, the Run 01 status document corrected, and `ruff` configured to skip `releases/` | [Section 11](#11-maintenance-log) |

## 4. Completed work

### 4.1 Source audit and GTFS preprocessing

The structural audit of the offline fixture ([source-data-audit.md](docs/source-data-audit.md), 2026-08-05) established the facts that shape every later design:

- AVL: 6,202,996 GPS rows, 156 vehicles, 2023-11-01 to 2023-11-30; route
  labels `29` (3,501,537 rows), `929` (2,405,658), `PO` (194,926), `PI`
  (100,875).
- APC: 573,596 stop observations, 148 vehicles, 103 distinct `StopID`
  values; 1,150 rows dated 2023-12-01.
- GTFS: 14 ZIP archives (about 441 MB compressed), eight core files each, no
  `feed_info.txt`, calendar envelope 2023-01-08 to 2024-01-06; `route_id`
  changes between feed versions (route 29: 70071, 70291, 70507).
- AVL `tatripId` is not a GTFS trip key: 3,133 distinct values, none matching
  the November feeds.
- APC `StopID` is functionally a GTFS `stop_code`: 559,715 of 573,596 rows
  map by `stop_code`; code `1652` (13,881 rows) does not map.
- Vehicle namespaces overlap on 147 identifiers (9 AVL-only, 1 APC-only).

The GTFS preprocessing stage (`src/ttc_tides`, CLI `ttc-tides gtfs inventory | manifest | select | process | evaluate`) was implemented and reviewed on 2026-08-06 ([gtfs-implementation-review.md](docs/gtfs-implementation-review.md)). Thirteen integrity behaviors were added during review (approved-only manifest rows with an explicit `--allow-candidate` override, SHA-256 re-pin check, refusal to write inside the source directory, collision-safe `p2` pattern identifiers, quarantine of non-finite floats and duplicate keys). The opt-in real-data acceptance for 2023-11-05 on feed `ttc-d092742e6a5b` produced 3,230,182 normalized stop_times, 28,291 scheduled trips, 939,706 scheduled stop events, 1,208 patterns, 0 quarantined rows, 0 fatal findings and 1 warning, in 3.05 s at about 2.85 GB peak RSS. The draft feed manifest (`artifacts/feed_manifest.draft.csv`, 14 rows) is generated, not approved: every row remains `candidate`.

### 4.2 Architecture, merge plan, review and decision records

- [Architecture and data contracts](docs/architecture.md) (2026-08-06): artifact
  layers, lineage columns, feed-selection algorithm, identifier contract,
  quality classes, run-manifest and test-strategy contracts.
- [Merge implementation plan](docs/merge-implementation-plan.md) (revised
  2026-08-06): documentation-only specification of an acyclic evidence
  pipeline S0–S11, publication profiles (`diagnostic`, `avl-core-v1`,
  `apc-enriched-v1`), Gate-0 register G0.1–G0.8, decision register D-01 to
  D-16, milestones M0–M6, Appendix C alignment register.
- [Merge plan review](docs/merge-plan-review.md) (2026-08-06): verdict
  "approve the architecture; require revisions before M1 starts"; all 10
  fixture measurements recomputed (9 exact, 1 wording correction); all 11
  TIDES v1.0 specification claims confirmed; six blocking revisions R1–R6 and
  11 advisory findings.
- [ADR 0001](docs/decisions/0001-offline-python-prototype.md) (accepted for the
  prototype, 2026-08-05); [ADR 0002](docs/decisions/0002-merge-plan-review-disposition.md)
  (accepted, 2026-08-06: architecture approved, provisional S0–S2 authorized,
  S3 and downstream publication-bound work frozen until a section-scoped
  re-review passes, M1 not to be declared complete);
  [ADR 0003](docs/decisions/0003-isolated-r1-diagnostic-experiments.md)
  (Accepted on the project owner's recorded instruction, with a revocation
  clause, 2026-08-08: bounded, local-only, read-only authorization for the R1
  spikes with a production-module firewall and a no-publication rule).

### 4.3 S0–S2 diagnostic runner

`ttc-tides diagnostic run` (profile `diagnostic-provisional`, authorized by ADR 0002) executed once on the full fixture on 2026-08-06: AVL 6,202,996 rows landed and accepted, APC 573,596 landed and accepted, 0 rejected, about 5 s wall time. Recorded flags include 2,118,605 AVL exact-payload duplicates, 431 AVL rows outside the TTC envelope, 5,969 APC load-equation violations and 900 APC visit-key collision rows (450 pairs). The `date_key_equals_calendar_date_violations` metric is 0. No S3 or later stage exists in `src/ttc_tides`.

### 4.4 R1 design spikes 01, 02 and 02R

Three isolated, experimental, diagnostic, non-publishable spikes under ADR 0003 tested whether the plan's joint-resolution design (review blocker R1, reconstructed as R1-B01 to R1-B11 because the original re-review report is absent) is expressible and behaves as hypothesized. All ran on three deterministic slices (a normal day, the 2023-11-05 DST fall-back window, the month boundary) derived from the S0–S2 outputs. Spike code lives in `experiments/r1_design_spike/`; run directories are gitignored and permission-restricted.

- Spike 01 (run `r1c`, 2026-08-07): determinism byte-identical across window
  orders and repeated runs; hypothesis H1 contradicted (all 21 mapped
  trip-scale segments on the normal slice abstain with low margin, 0.07–2.5%
  of scale); gap-only segmentation yields day-scale segments; the fold policy
  leaves 1,195 of 3,253 DST pings unresolved.
- Spike 02 (run `s2b`, 2026-08-08, after a five-lens adversarial review that
  found one genuine code bug and seven minor defects, all fixed): APC-assisted
  boundary refinement raised 24 to 36 segments; anchors accepted on 16 of 36
  refined segments under conservative thresholds; the 49 DST acceptances were
  shown to be unique-survivor artifacts, not evidence-supported winners.
- Spike 02R (run `p4`, 2026-08-08, after a review that fixed all 26 confirmed
  findings): comparator made structurally APC-independent; dual margins with
  censorship classification reduced the DST acceptances from 49 to 6 (42
  `unique_survivor_by_exclusion`, 1 `no_candidate`), the 6 resting solely on
  source-date agreement (D-03 open); normal and month slices produced zero
  acceptances; 0 of 50 fragment junctions stitch; cross-fitting kept 7 of 10
  normal-slice in-sample acceptances. Wall time 135.5 s, peak RSS 410.6 MiB.

Verdict carried by every 02R artifact: `architecture_selected: false`,
`human_correctness_claimed: false`, `r1_closed: false`; "INCONCLUSIVE, NO
REVIEWED HOLDOUT LABELS". Where Spike 02 and 02R disagree, 02R supersedes.

### 4.5 Human labeling v1, audit, and Protocol v2 setup

- Pre-registration (frozen 2026-08-08): factors S0–S3 × E0–E3 × four `T_mid`
  values × in-sample/cross-fitted × alignment × confidence; split by
  vehicle-day into 8 calibration and 4 holdout vehicle-days; a Phase B sample
  of 12 pseudonymous vehicle-days drawn from a population of 1,362 and recorded
  unprocessed.
- Labeling packet: 18 pseudonymous cases (6 `fragment` stratum, 12
  `route_929`), redacted CSV plus restricted parquet, label schema `xlabel-1`.
- v1 labeling UI ([tools/labeling_ui](tools/labeling_ui/README.md), 2026-08-09):
  anonymous time-ordered candidate cards, masked model verdict, audited
  diagnostics reveal, flock-protected CSV and append-only JSONL.
- First pass (2026-08-13, all 18 cases): 13 matched (11 top candidate, 2
  other), 2 ambiguous, 2 insufficient evidence, 1 no scheduled match; 0
  diagnostic reveals before first save. Per-case summaries are committed under
  [`tools/labeling_results/`](tools/labeling_results/).
- Audit v1 (2026-08-14): 18 of 18 judgments preserved; 0 of 18 pass the strict
  calibration gate; 9 of 13 high-confidence matches lack a rationale; the first
  pass was taken on a model-informed shortlist of 5 of 26–57 candidates per
  case (presentation leakage); 11 child-case requests across 9 parents;
  factorial status BLOCKED.
- [Protocol v2](docs/human-labeling-protocol-v2.md) (frozen 2026-08-14,
  version 2.0): grain-first ontology, complete five-position candidate keys,
  blind review before any reveal, complete candidate universe (660 rows across
  18 cases, schedule-neutral order, no score columns), 21 deterministic child
  tasks, task-specific promotion gates, no global gold flag. The v2 blind UI
  ([labeling_ui_v2](experiments/r1_design_spike/labeling_ui_v2/README.md))
  never reads the packet file that embeds anchor status.

### 4.6 APC–AVL coverage study and direct-evidence optimization (2026-09-08)

These studies inspect the full raw November fixture without GTFS and without
reading any human label. Their authorization basis is a chat instruction
recorded only in their own READMEs and in the evaluation standard: they read
`database_offline/` from directories outside the perimeter ADR 0003 defines,
and no decision record covers them (see [section 7](#7-open-decisions)).

- [Raw edge-case audit](docs/apc-avl-raw-edge-cases-20260908.md): 2,118,605
  AVL exact-payload duplicate member rows (1,071,525 extra copies); 3,831 AVL
  and 428 APC DST-ambiguous rows; `AvgSample` populations 528,370 / 45,020 /
  206; 1,401 vehicle×calendar-days (1,347 in both sources).
- [Evaluation standard](docs/apc-avl-coverage-evaluation-standard.md)
  `apc-avl-coverage-v1`: integrity checks I01–I10 and publication gates
  P01–P07 derived from TIDES v1.0 (companion `tides_rubric.json`).
- [Coverage study](docs/apc-avl-method-comparison-20260908.md): 21
  combinations (3 segmentations × 3 merge rules × 3 threshold sets) over
  157 source vehicle identifiers. `avl_context` + spatiotemporal links 562,097
  APC rows directly (98.00%); `gap_only` + spatiotemporal 562,362 (98.04%);
  `apc_assisted` 559,287 (97.51%); time-only produces 0 direct links. Runtime
  20.3 min, peak RSS 6.93 GiB; 203 tests, 580 scoped probe checks and 720
  edge-probe comparisons with 0 failures. The recommendation is conditional:
  spatiotemporal as the direct-evidence layer, sequence extension as a
  revisable inference layer; the documents refuse to rank true accuracy.
- [Direct-evidence v2](docs/apc-avl-direct-evidence-optimization-20260908.md)
  (`full02`): on the common eligibility denominator of 573,121 rows, the
  earlier definition gave 562,097 direct (98.08%); v2 gives 547,499 direct
  (95.53%) plus 4,939 partial (96.39% combined), 12,152 ambiguous, 8,531
  unmatched. Spatial residual P50/P95/P99 10.45/51.51/80.96 m. Negative-evidence
  replay: 3,821,376 bounded checks, 0 failures. 327 tests pass. The two
  "direct" definitions differ, so the drop is neither an improvement nor a
  regression.
- [Fixed-window probes](docs/apc-avl-direct-evidence-probes-20260908.md)
  (`probes02`, 20 frozen two-hour windows, 1,749 APC rows): a ±60 s APC clock
  shift leaves 58 / 172 of 1,413 v2 direct links (±120 s: 10 / 39). Clock
  synchronization between AVL and APC is therefore a separate, unconfirmed
  gate.

### 4.7 Merge-review workbench, Experiments 01 to 04c, frozen release and retention

Scope of every GTFS-matching experiment in this lineage: 2023-11-06, route 29 only. Code lives in `tools/merge_review/` (review website on port 8765, [README](tools/merge_review/README.md)) and `tools/merge_review/gtfs_matching/` ([README](tools/merge_review/gtfs_matching/README.md), [RESULTS](tools/merge_review/gtfs_matching/RESULTS.md)). This lineage also reads the raw fixture from outside the perimeter ADR 0003 defines and carries no decision record of its own (see [section 7](#7-open-decisions)).

| Experiment | What changed | Headline figures |
| --- | --- | --- |
| 01 / 01b (2026-09-08) | Vehicle-only slicing; exact AVL deduplication | 232,985 observations (213,895 AVL + 19,090 APC), 51 vehicle-days; 01b removed 36,257 exact AVL duplicates |
| 02 (2026-09-08) | PI/PO filtering; direction-evidence segmentation | 8,766 PI/PO AVL rows filtered; 541 direction candidate segments from 50 of 51 vehicles |
| 03 / 03b / 03c / 03d (2026-09-09) | Discrete GTFS candidate matcher; joint-gap split (302 to 306 segments); four decision modes global/early/middle/late; absolute-delay prior weight 0.05 to 1.0 | 31 vehicles, 306 segments, 92,143 AVL and 13,862 APC; 03d global: 0 MATCHED / 36 AMBIGUOUS / 270 PATTERN_ONLY; replay 653.9 s |
| 04 (2026-09-10) | Continuous spatiotemporal scorer, seven weighted cost components, shared IRLS delay process | 1,224 mode results; global PATTERN_ONLY/UNMATCHED/BOUNDARY_UNCERTAIN 299/7/0; 693.7 s |
| 04b (2026-09-10) | Absolute-delay weight ×10, dynamics ×0.1, budget-normalized gate rescaling | Top-1 changes vs 04: 16 / 9 / 11 / 4; 770.3 s |
| 04c (2026-09-10) | Occurrence model: operational delay prior, local occurrence displacement, high-recall retrieval (based on 04, not 04b) | Top-1 changes vs 04: 75 / 52 / 61 / 62 of 300; 605.6 s; full independent audit PASS (36,070 candidate scores, 421 + 421 pins); all offline calibration reports `UNCALIBRATED`, `activated: false` |

Status counts are descriptive distributions, not accuracy; `support_score = exp(-cost)` is a display scale, not a probability. Review notes in the website database are unblinded and must not enter training or scoring.

Frozen execution ([frozen-workbench.md](docs/frozen-workbench.md)): `config/workbench-release.json` selects one SHA-256-locked release under `releases/` containing the business code, web assets, profiles 03d/04/04b/04c with run identifiers and config hashes, `uv.lock`, interpreter and package versions, and hashes of the five raw inputs (AVL CSV, APC CSV, feed manifest draft, two GTFS archives). `./workbench.sh verify | run | serve` fails on any mismatch. An earlier 04b release was verified by a full independent raw rebuild (`semantic_equal: true`, `byte_identity_claimed: false`, 0 differences across 1,224 mode results and 36,170 candidate scores).

Retention and cleanup on 2026-09-10 (three separate operations; figures are not additive without their labels):

1. Conservative cleanup `878d7230` (03:40Z): 1,381 files, 4,686,872,576
   allocated bytes (about 4.37 GiB), including 234 old screenshots; 3,518
   protected files unchanged.
2. Retention registry and purge of Experiments 01–03c (05:01–05:28Z): 2,451
   files, 3,771,356,985 logical bytes (about 3.52 GiB); the Experiment 02
   inputs still needed by 03d/04/04b were kept as a 119-file checksummed
   checkpoint. Deleting 03c removed the live input of 03d's historical score
   replay; a pre-retention attestation records that it passed.
3. Approved Experiment-05-era cleanup (17:56–17:58Z, `tools/experiment05/cleanup.py`,
   executed only after two held plans): 1,252 allow-listed case, overview and
   shadow-episode files from the 03d/04/04b/04c run roots, 14,127,707,246
   logical bytes (about 13.16 GiB); 3,068 preserved files re-verified. Full
   historical browsing of those four runs now requires regeneration, which has
   new provenance and is not claimed to be byte-identical.

The pre-Experiment-05 snapshot `releases/pre-experiment-05-20260910/` (247 manifest files, about 50.91 MB, created 2026-09-10T16:23Z, matched by git tag `pre-experiment-05-20260910` at commit `3809a76`) preserves the 04c code, configuration, environment and one representative heavy case. Its `frozen_release/` directory records the release it was derived from (`workbench-04c-20260910T153527-0cb5d1dc`, SHA-256 `462a6a92…`). That record is preserved; the release directory it documents was removed on 2026-10-20 and remains in git history at `878089a`, after a check that all 207 code files of that release are present hash-identical inside `releases/pre-experiment-05-20260910/code/` (0 missing, 0 hash mismatches). `config/workbench-release.json` now selects the re-cut `workbench-04c-20261021T013832-cddd406e` ([section 5.3](#53-frozen-release-pointer)). Re-verification on 2026-10-20 recomputed all 247 manifest entries of the snapshot with 0 missing files and 0 hash mismatches, and the `tools/experiment05/cleanup.py` snapshot check still passes after the pruning (247 pinned files, 5 raw inputs, git tag `pre-experiment-05-20260910` at `3809a76`).

### 4.8 Experiment 05: reproducible raw-to-TIDES pipeline

Experiment 05 (`tools/experiment05/`, configuration `config/experiment05-v1.toml`, schema `experiment05-config-v1`) is the current batch pipeline. CLI (`./experiment05.sh availability | run | init | stage | verify | web`) and browser (port 8767) call the same library. The ten stages are `load_raw`, `normalize`, `event_timeline`, `project_avl`, `segment_trips` (with plateau and right-censoring assessment), `attach_apc`, `generate_candidates`, `assign_scheduled`, `map_tides`, `validate_publish`. Successful runs are reused by input/code/config/feed fingerprint; historical runs are never overwritten.

Design decisions frozen in configuration: `allowed_routes = ["29"]`; four match modes; three locked assignment rounds over ranks 1–3 using a strictly monotone maximum-cardinality chain per (service date, route, direction, mode, feed) group, exact `Fraction` score sums, no score threshold, hard constraints never waived; candidate rankings hash-checked so ranks are never recomputed; export policy `apc_stop_interval_departure_proxy_v1` (one attached APC row is one stop visit; vehicle arrival and departure events at source times; aggregate boarding and alighting events at a departure-time proxy; zero counts emit no event); `feed_allow_candidate = true` as a recorded research override; `feed_include_previous_day_tail = false`.

Canonical runs (all `status: complete`, `validation_ok: true`, `research_only: true`; manifests retained locally under `outputs/experiment_05/runs/`):

| Run | Date | Policy | Performed trips | Assigned global / early / middle / late | TIDES rows per mode | Checks |
| --- | --- | --- | --- | --- | --- | --- |
| `run-2023-11-06-a39c5491eae9f48b` (current) | 2023-11-06 | departure proxy (config `98daa8e9…`, pipeline `experiment-05-v1+663770f8d53984d6`, commit `44c1aed`) | 352 | 244 / 246 / 244 / 227 (69.3% / 69.9% / 69.3% / 64.5%) | 31 vehicles, 112,002 vehicle_locations, 13,599 stop_visits, 38,211 passenger_events | 1,483 invariant checks, 0 errors; 50/50 independent acceptance; 2,232 output pins |
| `run-2023-11-06-2cf05504dfcdf899` (preserved acceptance run) | 2023-11-06 | omission policy (config `4936a83e…`, commit `d5b7b8a`) | 352 | identical to above | 0 passenger_events (11,013 omitted) | 1,459 checks; 46/46 acceptance; shared stages byte-identical to `a39c` |
| `run-2023-11-20-debe65bd31e3044b` (current for the date) | 2023-11-20 | omission policy | 313 | 225 / 227 / 223 / 215 (71.9% / 72.5% / 71.2% / 68.7%) | 29 vehicles, 100,771 vehicle_locations, 12,764 stop_visits, 0 passenger_events (10,378 omitted) | 1,303 checks; 46/46 acceptance; 1,994 pins; 1 right-censored trip (91 observations, 1,000 s tail) |
| `run-2023-11-20-18a7c85b6b6ab58b-41adcc0e` | 2023-11-20 | omission policy, global mode only, browser-authorized force rerun | 313 | 225 (global) | identical global package | 346 checks; 23/23 comparison against the all-mode run, byte-identical outputs |

Further measured facts for 2023-11-06 (`a39c`): source rows AVL 213,895 and APC 19,090; route-29 AVL 112,002 of which 19,859 are exact duplicates; APC attachment 13,599 of 13,862 (98.1%), 263 with no compatible performed trip, 0 ambiguous; 35,157 candidate rows across four modes; the 108 unmatched global trips carry non-exclusive reasons led by `TOP3_EXHAUSTED` 88, `CANDIDATE_ALREADY_USED` 74, `LOST_SAME_SCHEDULE_CONFLICT` 73 and `NO_CANDIDATES` 20; only 101 of 352 trips (28.7%) receive the same scheduled trip in all four modes (81 of 313, 25.9%, on 2023-11-20). The 38,211 passenger_events per mode are event rows (13,599 arrivals, 13,599 departures, 5,462 boarding records summing to 21,122, 5,551 alighting records summing to 19,347), not passengers. Wall time 797 s with 8 workers, of which `generate_candidates` 491 s.

Validation: TIDES v1.0 schemas vendored under `schemas/experiment05/tides-v1/` with per-file SHA-256 in `vendor.json`; Draft-07 profile, Frictionless in-memory and on-disk validation, composite business foreign keys, one-to-one assignment and consecutive stop-sequence checks; each package bound to its run through `run_binding`. Independent acceptance replays with three shuffled-input seeds reproduce assignments, audits and summaries exactly; 4,226 saved-run file hashes verified. Browser acceptance: 124, 27, 32, 15, 83 and 2,644 checks PASS across the read-only, preflight, authorized-force, APC-policy and visualization suites. The [TIDES compliance and evidence-boundary audit](docs/experiment05-tides-compliance.md) (2026-09-10) concludes that structural and project checks pass while operating-day attribution, APC grain and some event semantics remain explicitly experimental inference; it is not a certification or a legal data-use opinion.

Two initial full-day attempts (`run-2023-11-06-2cbc6641904fb2c3`, `run-2023-11-20-0069136e1473bb97`) are retained with `acceptance_disposition: ACCEPTANCE_SUPERSEDED` (stopped before publication for the APC interval-overlap and bounded-memory fixes; the second exited with code 137 during candidate generation). The independent plateau review of their stage-5 outputs found one upstream bug (APC selected by point time instead of closed-interval intersection, 180 omitted overlaps) that was fixed in `d5b7b8a`.

### 4.9 Test suites

Measured on 2026-10-20 after the English-only conversion, offline from
`agencies/ttc` with `UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync …`:

| Suite | Command | Result | Before the cleanup |
| --- | --- | --- | --- |
| GTFS preprocessing + Experiment 05 | `python -m pytest tests` | 451 passed | 451 passed |
| Merge-review workbench | `python -m pytest tools/merge_review` | 1,397 passed | 1,399 passed |
| R1 spike + APC–AVL studies | `PYTHONPATH=experiments/r1_design_spike/src python -m pytest experiments/r1_design_spike/tests experiments/apc_avl_coverage_study/tests experiments/apc_avl_direct_evidence/tests` | 640 passed | 640 passed |
| Labeling UI v1 | `pytest` (from `tools/labeling_ui`) | 43 passed | 43 passed |
| Labeling UI v2 (blind protocol) | `pytest` (from `experiments/r1_design_spike/labeling_ui_v2`) | 22 passed | 22 passed |
| Node unit: continuous evidence card | `node tools/merge_review/tests/test_continuous_ui.mjs` | 6 passed | 6 passed |
| Node unit: Experiment 05 visualization math | `node tests/test_experiment05_viz_math.mjs` | 7 passed | 7 passed |

Totals: 2,553 Python tests passed and 0 failed (451 + 1,397 + 640 + 43 + 22);
13 Node unit tests passed and 0 failed. The root suite's 451 tests comprise 337
Experiment 05 tests across 14 files, 109 GTFS and S0–S2 unit tests in
`tests/unit/`, and 5 raw-audit tests.

Three statements qualify these counts:

- The merge-review suite moved from 1,399 to 1,397 for one reason: the
  parametrized `test_language_assets_are_local_read_only_allowlisted_files`
  covered four runtime-translation assets and was renamed to
  `test_static_assets_are_local_read_only_allowlisted_files` covering the two
  that remain (`app.js`, `styles.css`), because the four i18n assets were
  deleted (1,399 − 4 + 2 = 1,397). No test was weakened or skipped. Two Node
  browser-translation tests (`test_i18n_runtime.mjs`, `browser_i18n_smoke.mjs`)
  were deleted with the i18n layer.
- The Chrome-driven browser acceptance scripts (the remaining `*.mjs` files
  under `tools/merge_review/tests/` and `tests/browser_experiment05_*.mjs`)
  were **not** executed in this cleanup; they require a running server and
  retained run data, and were only syntax-checked with `node --check`. They are
  not claimed to pass on 2026-10-20. Their historical results are recorded in
  the acceptance reports cited in section 4.8.
- `ruff check` over `src tools tests experiments` reports 216 pre-existing
  errors, the same count before and after the cleanup. The tree is not
  ruff-clean. The passing lint results quoted in the 2026-08 GTFS review and
  the 2026-09 experiment reports were scoped to the paths those documents name,
  at their dates.

Historical counts quoted in the spike documents (219, 282 and 324 spike tests;
109 production tests) remain accurate for their dates.

## 5. Current state of code, data and artifacts

### 5.1 Committed versus local-only

| Content | Location | In git | Notes |
| --- | --- | --- | --- |
| Source code, tests, configuration, schemas, documentation, decision records | `src/`, `tools/`, `experiments/`, `tests/`, `config/`, `schemas/`, `docs/` | Yes | Zero tracked files outside `releases/` contain CJK characters; the pre-translation strings survive in history at `878089a` and inside the pinned pre-05 snapshot |
| Frozen workbench release and pre-05 snapshot | `releases/` | Partly | The working tree holds exactly two directories. `pre-experiment-05-20260910/` (about 50 MB, 247 pinned files) is tracked and hash-pinned. `workbench-04c-20261021T013832-cddd406e` (about 3.5 MB), which `config/workbench-release.json` selects, is the product of the 2026-10-20 re-cut; that directory and the repointed pointer file have to enter version control together, or a checkout carries a pointer naming an absent release directory and `./workbench.sh verify` fails. The six superseded 2026-09-10 releases were removed on 2026-10-20 |
| First-pass label summaries | `tools/labeling_results/` (18 files) | Yes | Contain vehicle identifiers and absolute timestamps that predate the ADR 0003 redaction rule |
| Raw AVL, APC and GTFS inputs | `database_offline/` (about 1.4 GB) | No (gitignored CSV and ZIP) | Redistribution status not established |
| GTFS stage outputs, S0–S2 diagnostic run, R1 spike runs, labeling audit and Run 01 directories, coverage and direct-evidence studies, merge-review runs, workbench builds | `artifacts/` (about 3.1 GB) | No (gitignored except README) | R1 and labeling directories are owner-only (0700/0600) under ADR 0003 |
| Experiment 05 runs, acceptance reports, cleanup audits, plateau review | `outputs/` (about 5.0 GB) | No (never committed; ignored by a `.gitignore` rule added on 2026-10-20) | All Experiment 05 numbers above are verifiable only on the development machine |

Consequence: a reader of the repository can verify code, configuration, hashes and documentation, but not regenerate any real-data result without the raw inputs.

### 5.2 Canonical versus superseded runs

- Experiment 05, 2023-11-06: `a39c5491eae9f48b` is current; `2cf05504dfcdf899`
  is the preserved two-day acceptance run under the older policy; a third
  complete run, `3ce3f2995a619fa3` (visualization-era code fingerprint
  `540884d309d80c98`, same configuration, business outputs hash-identical to
  `a39c`), exists but is referenced by no document or acceptance report; its
  status is an open author decision.
- Experiment 05, 2023-11-20: `debe65bd31e3044b` (all modes) and the
  global-only `18a7c85b6b6ab58b-41adcc0e`; both under the older omission
  policy.
- Workbench: 03d `run-20231106-20260909T183705-d67ffa30723d`, 04
  `run-20231106-20260910T030622-57e42718b0c1`, 04b
  `run-20231106-20260910T041452-de4609d0825b`, 04c
  `run-20231106-20260910T063603-e4bf6bc60823`; their case and overview
  directories are empty after the approved cleanup, while manifests,
  configurations, output hash inventories, summaries and shadow reports remain.
- R1 spikes: canonical runs `r1c`, `s2b`, `p4`; Spike 02R runs p1–p3 were
  deleted as superseded.

### 5.3 Frozen release pointer

`config/workbench-release.json` selects one workbench release directory under `releases/` and records its SHA-256. On 2026-10-20 the release was re-cut from the English-only sources and the pointer was repointed to it: it now selects `releases/workbench-04c-20261021T013832-cddd406e` (created 2026-10-21T01:38:32Z, `release.json` SHA-256 `1e922fa9…`) in place of `releases/workbench-04c-20260910T153527-0cb5d1dc` (SHA-256 `462a6a92…`), the release the pre-05 snapshot records as its origin. `./workbench.sh verify` reports `PASS` for the new release with 201 code files and 5 raw inputs. It carries no CJK text, its `static/` directory holds only `app.js`, `index.html` and `styles.css` because the four runtime-translation assets no longer exist to be frozen, and its default profile 04c and its 03d/04/04b/04c source profiles pin the same four completed runs as before. A release cut from the current sources has new provenance; it is not claimed to be byte-identical to any 2026-09-10 release. The re-cut directory and the repointed pointer file are one unit: whatever carries one into version control must carry the other, because `./workbench.sh verify` resolves the pointer before anything runs and fails when the directory it names is absent.

The six superseded releases cut on 2026-09-10 (three `releases/workbench-04b-20260910T*`, three `releases/workbench-04c-20260910T*`, 1,196 tracked files) were removed with `git rm` in the same pass, so `releases/` now holds exactly two directories: the selected release (about 3.5 MB) and `pre-experiment-05-20260910/` (about 50 MB). Losslessness was verified before the deletion: all 207 code files of `workbench-04c-20260910T153527-0cb5d1dc` are present hash-identical inside `releases/pre-experiment-05-20260910/code/` (0 missing, 0 hash mismatches), that release's own `release.json` is preserved at `releases/pre-experiment-05-20260910/frozen_release/release.json`, and the removed directories remain in git history at commit `878089a`.

`releases/pre-experiment-05-20260910/` is kept byte-for-byte: `tools/experiment05/cleanup.py` requires that directory, its `pre-experiment-05-snapshot-v1` manifest and the git tag of the same name before it will plan anything, and the snapshot deliberately preserves the pre-cleanup sources, including the former non-English UI strings. Because that check demands an exact file-set match, the stray `ruff` caches that linting had written inside the snapshot broke it; the caches were deleted and `pyproject.toml` now sets `extend-exclude = ["releases"]`, so linting no longer writes into a pinned release directory. The check passes after the pruning: 247 pinned files, 5 raw inputs, tag at `3809a76`.

A frozen release is a snapshot taken at one instant, so its copies of
`README.md` and `docs/frozen-workbench.md` are the versions that existed when
the release was cut and still describe the pre-pruning `releases/` layout.
That divergence is inherent to the freeze model, which exists precisely so
that later edits cannot silently change a pinned release: re-cutting the
release to refresh those two copies would produce a new release id, which
would then have to be written back into the documentation and diverge again.
The working-tree documents are authoritative for readers; the frozen copies
are historical, and `./workbench.sh verify` checks a release against its own
manifest, never against the working tree.

### 5.4 How to reproduce

All commands run from `agencies/ttc` with the locked environment (`uv sync --frozen`); raw inputs must be present under `database_offline/`.

| Target | Command | Notes |
| --- | --- | --- |
| Experiment 05 availability, run, browser | `./experiment05.sh availability`; `./experiment05.sh run --service-date 2023-11-06 --route-id 29 --match-mode all`; `./experiment05.sh web --port 8767` | About 11–13 min and 4 GB RSS per date with 8 workers; `--force-rerun` for a fresh run |
| Experiment 05 saved-run verification | `./experiment05.sh verify` | Checks run binding and output hashes |
| Workbench verify, rebuild, serve | `./workbench.sh verify`; `./workbench.sh run --serve --port 8780`; `./workbench.sh serve --port 8781` | A full raw rebuild measured about 27 min for 04b; new provenance. `./workbench.sh serve` and `./experiment05.sh web` both default to port 8767, so pass `--port` to run them at the same time; serving the retired 03d–04c workspace presents no cases, because the approved cleanup deleted their case and overview files |
| Merge-review website | `uv run --offline --no-sync python -m tools.merge_review.server` | Port 8765; inspection only |
| GTFS preprocessing | `uv run ttc-tides gtfs inventory --gtfs-dir database_offline/gtfs` and the `manifest`, `select`, `process`, `evaluate` subcommands | See [README](README.md#gtfs-preprocessing) |
| S0–S2 diagnostic run | `uv run ttc-tides diagnostic run` | Diagnostic-provisional profile |
| Coverage study and direct evidence | See [coverage study README](experiments/apc_avl_coverage_study/README.md) and [direct-evidence README](experiments/apc_avl_direct_evidence/README.md) | Whole raw month; 20.3 min and 6.93 GiB for the factorial |
| R1 spikes | See [spike README](experiments/r1_design_spike/README.md) | Requires the S0–S2 normalized inputs and ADR 0003 conditions |
| Labeling UIs | See [v1](tools/labeling_ui/README.md) and [v2](experiments/r1_design_spike/labeling_ui_v2/README.md) | v2 requires the restricted Run 01 directory |
| Tests | `uv run pytest`; per-package suites as listed in [section 4.9](#49-test-suites) | Root suite 451 tests; browser suites need a running server and retained run data |

## 6. Work in progress and unfinished items

| Item | Current state | Gate or blocker |
| --- | --- | --- |
| Blind second pass, Run 01 | 2 of 18 parents saved (`C-913f7d4c` and `C-ba617064`, both re-graded to `local_fragment`, not evaluable at the current grain, queued for adjudication); 0 of 7 required scheduled-trip second passes; 0 of 21 child tasks; the adjudicated-label set is empty. [The Run 01 status document](docs/human-labeling-v2-run01-status.md) was corrected on 2026-10-20 and now records these saves instead of "not started" | Human reviewer time under the frozen protocol; children unlock only after the parent's blind save |
| Adjudication and task-specific gold sets | 2 items pending; all seven gold sets empty | Reviewer must produce explicit third records; promotion is mechanical and requires matching frozen protocol and UI versions |
| Pre-registered calibration factorial | `factorial_status: NO_GO`; 6 of the 14 mechanical conditions false (`all_seven_scheduled_second_passes_completed`, `required_child_reviews_completed`, `disagreements_adjudicated`, `full_candidate_keys_verified`, `calibration_holdout_membership_validated`, `phase_b_cases_labeled_under_frozen_protocol`); holdout unopened, pre-registration and thresholds untouched | Reviewed labels meeting the strict gate; verified calibration/holdout membership; then a separate explicit human authorization, which no green report can replace |
| Phase B sample (12 vehicle-days) and holdout (4 vehicle-days) | Selected, unprocessed, unopened; the 18 pilot cases are permanently ineligible as holdout | Pilot adjudication first; holdout opens once, after thresholds are frozen on calibration |
| Assignment calibration (Experiment 05, 04c) | Every assignment is an uncalibrated model output; probabilities null; no score threshold applied; `physical_trip_end_confirmed` always false | Independent, verified labels disjoint by route, service date and episode (at least 60 train and 40 validation episodes per the 04c contract), which 2023-11-06 / route 29 alone cannot provide |
| Four-mode agreement | Only 101 of 352 trips (28.7%) on 2023-11-06 and 81 of 313 (25.9%) on 2023-11-20 receive the same scheduled trip in all four decision modes; among trips assigned in `global`, `early` agrees on 161 of 244 (66.0%) and 137 of 225 (60.9%). The four packages are alternative experimental results and must not be concatenated as additional trips | Independent labels; until then the spread is the honest uncertainty of the evidence scope, not an error rate |
| Cross-midnight operating day | Window is a local calendar day; on 2023-11-06, 7,951 AVL and 1,132 APC rows carry previous-day hypotheses and 28 of 300 active scheduled trips have times past 24:00; none retrieved as candidates | Service-day cutoff policy (D-04); an explicit configuration and interface change |
| Single route and two dates | Route 29 only; 2023-11-06 and 2023-11-20; route 929 and PI/PO rows diagnostic-only; no DST-transition day run end-to-end | Explicit scope expansion; PI/PO semantics (D-11) before any `trip_type` mapping |
| 2023-11-20 packages under the current passenger-event policy | Still under the omission policy (0 passenger_events) | Decision to rerun with config `98daa8e9…` (creates a new run) |
| Merge plan re-review and M0 exit | Section-scoped re-review of the R1–R6 revision not performed; Appendix C alignment of 10 passages not done; `docs/architecture.md` still states the fixture does not span the fall DST transition, which the review marked factually false (2023-11-05 is in the fixture) | ADR 0002 freeze on S3+ stands; no ADR 0004 exists |
| Governed stages S3–S11 in `src/ttc_tides` | Not implemented; Experiment 05 implements comparable functionality under `tools/experiment05/` outside the governed plan | Governance question: the relationship between Experiment 05 and the ADR 0002 freeze is not recorded in any ADR |
| Feed manifest approval (D-15) | `config/feed_manifest.csv` absent; 14 draft rows `candidate`; priorities inferred from ZIP member timestamps | Project owner may approve with recorded residual risk (class I gate) |
| Vendor and agency semantics (D-06 to D-12) | APC grain, count semantics, door mapping, PI/PO, Run, Branch, `tatripId`, vehicle-namespace equivalence all unconfirmed; 1,026 attached rows `grain_uncertain` on 2023-11-06; 9 two-row terminal groups must not be auto-merged | TTC data owner or vendor (class E gates); none assigned |
| Stop code `1652` (D-10) | 13,881 APC rows unmapped; coordinates-plus-text hypothesis only | Reviewed feed- and date-specific crosswalk |
| Exact-duplicate AVL rows in `vehicle_locations` | 19,859 of 112,002 rows (about 18%) exported with their own identifiers; duplicate flag only in diagnostics | Explicit export-policy decision |
| Monotone one-to-one assignment | Operating assumption; TIDES permits many performed trips per scheduled trip (D-02); about 30% of trips per mode unassigned, mostly through conflicts rather than missing candidates | Relaxation requires labels to evaluate |
| Scheduled-stop fields in `stop_visits` | `scheduled_stop_sequence`, scheduled times, `timepoint`, `actual_trip_start/end`, `trip_type`, door counts, `departure_load` all null; 67 of 352 segments on 2023-11-06 have no APC visit | Visit-to-scheduled-occurrence mapping and AVL-based stop detection not implemented |
| AVL–APC clock-offset contract | Direct-evidence links collapse under ±60 s shifts; no estimated TTC offset | Independent clock-consistency check with vendor input |
| Plateau and right-censoring validation | 0 censored trips on 2023-11-06, 1 on 2023-11-20; 33 of 40 counterfactual checks had zero eligible hypotheses | Independent labels or telemetry before changing censoring discrimination |
| Documentation debt | `config/README.md` ("no configuration files have been created yet"), `artifacts/README.md` (planned layout), `src/README.md`, `tests/README.md` (describes only the GTFS preprocessing unit suite and proposes a `fixtures/integration/acceptance` layout that was never created) and the `docs/architecture.md` status paragraph describe the pre-Experiment-05 state; the `docs/tides05-contract.md` resource list omits `passenger_events`; and Appendix C of the merge plan lists 10 passages to align — 7 in `docs/architecture.md`, 4 involving `README.md` (the quarantine phrasing, the quality-metrics status list, merge-strategy steps 3–5 ordering against §1 joint resolution and D-04, and the unconditional five-table scope claim) and the `docs/source-data-audit.md` `Date_Key` row | Documentation pass; Appendix C alignment is formally an M0 deliverable |
| Redaction scrub | Spike 01 and 02 findings and `tools/labeling_results/` predate the ADR 0003 redaction rule and contain vehicle identifiers and precise timestamps | Owner governance decision (ADR 0003 condition 8) |
| Run `3ce3f2995a619fa3` | Complete, outputs identical to `a39c`, undocumented | Author decision: document as a cache-miss duplicate or retire |
| Browser acceptance suites | The Chrome-driven `*.mjs` suites were syntax-checked but not executed after the 2026-10-20 English-only conversion; their passing results date from 2026-09-10 | A running local server plus retained run data on the development machine |
| Output retention | Failed-attempt diagnostics and compact stage shards of Experiment 05 deliberately kept; `outputs/` gitignored, so no run evidence is in version control | Decision whether to commit small summary and acceptance JSON/Markdown separately |

## 7. Open decisions

Identifiers D-01 to D-16 and G0.x come from the [merge plan decision and gate registers](docs/merge-implementation-plan.md); R1–R6 from the [review](docs/merge-plan-review.md). Status wording follows the source documents.

| ID | Question | Why it matters | Who must decide | Status |
| --- | --- | --- | --- | --- |
| D-01 / G0.1 | Exact TIDES revision | Schema and validation target | Project owner | Resolved: tag v1.0, commit `b2cb2317…`; vendored |
| D-02 | May one scheduled trip map to several performed trips | Assignment constraint design | Project owner | Resolved: yes, per TIDES v1.0; Experiment 05 nonetheless applies one-to-one as an operating assumption |
| D-03 | Is AVL `Date_Key` the service date | Operating-day attribution | Project owner with data-owner confirmation | Resolved for the fixture: no; equals the local calendar date on all rows; the only evidence family behind Spike 02R's 6 DST acceptances |
| D-04 | Fixed service-day cutoff fallback | Cross-midnight publication | TTC scheduling authority | Rejected as publish default; diagnostic comparison allowed |
| D-05 | DST fold for an ambiguous timestamp | 3,831 AVL and 425–428 APC rows in the 2023-11-05 repeated hour | Project owner | Resolved: preserve both candidates; unresolved timestamps are not publishable; APC folds never independently resolved |
| D-06 / G0.4 | APC row grain and `NoTrips` / `AvgSample` semantics | Whether rows are additive visits | Vendor or TTC data owner (class E) | Open-blocking for the APC profile; Experiment 05 uses a recorded research assumption |
| D-07 | APC visit-key collisions (450 pairs) | Count additivity | Vendor or TTC data owner | Open-blocking for affected rows |
| D-08 / G0.7 | APC count semantics and event-time proxy | `passenger_events` meaning | Vendor or TTC data owner (class E) | Open-blocking; departure proxy is a documented research policy |
| D-09 | Load and door-field mapping | `boarding_1/2`, `alighting_1/2`, `departure_load` | Vendor | Open-blocking for those fields; defaults null |
| D-10 | Stop code `1652` | 13,881 unmapped APC rows | Project owner with TTC stop-data input | Open, row-scoped |
| D-11 / G0.6 | PI, PO, Run, Branch, RouteDirection, `tatripId`, `AvgOTP` meanings | `trip_type` and identifier mapping | TTC operations (class E) | Open, feature-scoped; preserved as evidence only |
| D-12 / G0.5 | AVL–APC vehicle namespace equivalence (147 overlaps, 9 AVL-only, 1 APC-only) | APC attachment and `apc-enriched-v1` | Project owner (AVL namespace) / TTC (cross-namespace) | Open-blocking for APC attachment under the governed plan only |
| D-13 / G0.8 | Redistribution, retention, data-use authorization | Any publication beyond local diagnostics | TTC data owner, legal, project owner | Open-blocking outside local use; `datapackage.json` carries no data license |
| D-14 | Segmentation, attachment and matching thresholds | Every numeric default is an uncalibrated hypothesis | Project owner via the labeled calibration protocol | Open-blocking for resolved-trip publication |
| D-15 / G0.2 | GTFS feed manifest approval | Which feed governs each date | Project owner (class I) with residual risk recorded | Open; all 14 rows `candidate`; `allow_candidate` override in use |
| D-16 / G0.3 | Time basis, precision and clock source | Naive America/Toronto wall time assumed everywhere | Project owner with vendor confirmation | Open, class I |
| R1–R6 | Review blockers (joint-resolution specification, fold hand-off, windowing, mid-trip exclusion, governance, document alignment) | Exit from M0 | Independent re-reviewer | Addressed in the 2026-08-06 revision; re-review not performed; R1 remains open |
| Governance | Does Experiment 05 operate inside or outside the ADR 0002 freeze | Whether its packages are "publication-bound work" | Project owner (ADR 0004 candidate) | Open; not recorded |
| Governance | Authorization basis for the APC–AVL studies (`experiments/apc_avl_coverage_study/`, `experiments/apc_avl_direct_evidence/`) and the merge-review workbench lineage (`tools/merge_review/`), which read the raw fixture from outside ADR 0003's perimeter on a chat instruction alone | Whether their artifacts have any governance record; ADR 0003 itself states that a chat instruction is not project governance | Project owner (ADR 0004 candidate) | Open; not recorded |
| Governance | Factorial authorization | Execution of the pre-registered experiment | Named human approver | NO_GO; separate explicit authorization required |
| Governance | Scrub of pre-ADR-0003 findings documents and `tools/labeling_results/` | Redaction policy consistency | Project owner | Open |
| Policy | Rerun 2023-11-20 under the departure-proxy policy | Consistent packages across dates | Project owner | Open |
| Policy | Omit or document exact-duplicate AVL rows at export | Consumer deduplication burden | Project owner | Open |
| Policy | Relax the monotone one-to-one constraint | Bunching, overtaking, mid-route fragments | Project owner, after labels | Open |
| Policy | Confidence threshold for publishing `trip_id_scheduled` | Any formal release | Project owner, after labels | Open; none applied |
| Workbench | Status of run `3ce3f2995a619fa3` | Canonical run inventory | Author | Open |

## 8. Plan

No dates or owners are assigned below unless stated; items are "unscheduled, owner to be assigned" by default. The project owner is the default approver for class I decisions; class E decisions require a TTC data owner or vendor who does not yet exist.

### 8.1 Near term

1. Finish the blind second pass under Protocol v2: the 7 required
   scheduled-trip parents, the remaining parents, then the 21 child tasks.
   Exit: `label_readiness_report_v2` shows the required count met and no
   parent in `SECOND_PASS_NOT_STARTED`.
2. Adjudicate first/second-pass disagreements and grain changes; promote
   task-specific gold sets. Exit: all 14 `factorial_go_no_go` conditions true.
3. Obtain the separate explicit human authorization for the pre-registered
   factorial; run it on the 8 calibration vehicle-days; open the 4 holdout
   vehicle-days once, after thresholds are frozen. Exit: held-out
   accepted-match precision reported with the fail-closed rule applied.
   Dependency: items 1 and 2.
4. Record the governance relationship between Experiment 05 and ADR 0002 in
   a decision record; decide the status of run `3ce3f2995a619fa3`; decide
   whether to rerun 2023-11-20 under the departure-proxy policy.
5. Clear the documentation debt listed in [section 6](#6-work-in-progress-and-unfinished-items)
   (`config/README.md`, `artifacts/README.md`, `src/README.md`,
   `tests/README.md`, the `docs/tides05-contract.md` resource list, and the
   `docs/architecture.md` status paragraph and DST statement), and align the 10
   Appendix C passages — 7 in `docs/architecture.md`, 4 involving `README.md`
   and the `docs/source-data-audit.md` `Date_Key` row — which is formally an M0
   deliverable. The `docs/frozen-workbench.md` release-retention and
   results-retention paragraphs and the Run 01 status document were corrected
   on 2026-10-20.
6. Re-run the Chrome-driven browser acceptance suites against a retained run,
   since the 2026-10-20 cleanup verified them only by syntax check. Exit: the
   suites named in [section 4.9](#49-test-suites) pass on the current sources.

### 8.2 Medium term

1. Section-scoped re-review of the R1–R6 revision of the merge plan; on a pass,
   exit M0 and record the disposition of the S3+ freeze. Exit: a new ADR.
2. Cross-midnight operating-day policy (D-04): previous-service-day GTFS tails
   and next-civil-day portions of post-24:00 trips; requires a configuration
   and interface change and a rerun of both dates.
3. Scope expansion on real data: the 2023-11-05 DST transition day, additional
   November dates, route 929; PI/PO rows remain excluded until D-11 is
   confirmed.
4. External confirmations: feed manifest approval (D-15); APC grain, counts,
   door and time semantics (D-06 to D-09); operational field meanings (D-11);
   AVL–APC clock synchronization (D-16 and the probes finding); stop code
   `1652` crosswalk (D-10).
5. Export-policy decisions: duplicate AVL rows; visit-to-scheduled-occurrence
   mapping so that scheduled fields in `stop_visits` can be populated; AVL-based
   stop detection and terminal profiles for `actual_trip_start/end`.
6. Independent accuracy evaluation design with pre-declared rules and fresh
   source windows, since the whole raw month has already been inspected for
   method development and is not an untouched holdout.
7. Redaction scrub of the pre-ADR-0003 documents and label summaries, if the
   owner so decides.

### 8.3 Longer term and explicitly not planned

- Not planned in this prototype: production orchestration (Dagster or
  equivalent), a SQL warehouse or dbt project, live or real-time ingestion,
  dashboards or ridership metrics, a public serving API, rail or fare data.
- Not planned: any formal TIDES publication or redistribution of TTC data
  until D-13 is resolved by the data owner.
- Not planned: converting unblinded merge-review notes into labels, summing
  APC counts across candidates or GPS points, or promoting the intentionally
  unsafe `forced_diagnostic` and `VX` variants from the spikes.
- Real-time (prefix-only) decision evaluation is a recognized design gap, not
  a scheduled item.

## 9. Risks and constraints

| Risk or constraint | Effect | Mitigation in place |
| --- | --- | --- |
| No TTC data owner or vendor contact | Class E gates (D-06, D-08, D-11, D-13, cross-namespace D-12) cannot close; the merge plan's risk register names this a single point of failure | Every dependent field is null or labeled experimental; nothing is published |
| Raw data redistribution not established | Results cannot be reproduced outside the development machine; fixtures cannot be shared | Hash-pinned manifests; synthetic unit fixtures only; `database_offline/` gitignored |
| Uncalibrated outputs | Assignment, anchor and coverage figures can be misread as accuracy | Mandatory epistemic labels in every manifest and document; `independent_accuracy_evaluation: false` recorded per run |
| Single development machine, local-only evidence | `artifacts/` (3.1 GB) and `outputs/` (5.0 GB) exist nowhere else; owner-only permissions on ADR 0003 directories | Content SHA-256 manifests; the hash-pinned pre-05 snapshot is tracked in git and `878089a` still holds the superseded release directories, both only in the local clone, since no `agencies/ttc` commit has been pushed; the re-cut release and the pointer that selects it have to reach version control together; cleanup tools require allow-lists and hash verification |
| Reproducibility depends on retained hashes, not retained outputs | Historical 03d–04c full results were deleted; regeneration has new provenance | Release pointer, snapshot, pins and completion records retained; byte identity never claimed |
| Clock and time-basis assumptions | Naive America/Toronto wall time; unconfirmed AVL–APC synchronization; ±60 s shift collapses direct links | DST-ambiguous rows flagged, never guessed; clock contract listed as a separate gate |
| Scope narrowness | Route 29 and two dates; no cross-route or cross-date generalization claim | Documented in every results section |
| Documentation drift | Several early documents describe a superseded state | Listed in section 6; this document is the current reference |
| Memory and runtime | Experiment 05 about 4 GB RSS per date; coverage factorial 6.93 GiB; GTFS process 2.85 GB | Bounded candidate storage and streaming checks added in `d5b7b8a` and `44c1aed` |

## 10. Repository map and documentation index

### 10.1 Directory layout (working tree, 2026-10-20)

```text
agencies/ttc/
├── README.md                      # Technical entry point
├── PROJECT_STATUS.md              # This document: status, open decisions, plan
├── pyproject.toml, uv.lock, .python-version
├── experiment05.sh                # Launcher for tools.experiment05 (uv, offline)
├── workbench.sh                   # Launcher for the frozen merge workbench
├── config/
│   ├── experiment05-v1.toml       # Authoritative Experiment 05 configuration
│   ├── workbench-release.json     # Pointer to the selected frozen release + SHA-256
│   └── README.md                  # Planned config boundary (predates Experiment 05)
├── schemas/experiment05/tides-v1/ # Vendored TIDES v1.0 schemas pinned by vendor.json
├── src/ttc_tides/                 # GTFS preprocessing CLI and S0–S2 diagnostic runner
├── tools/
│   ├── experiment05/              # Ten-stage raw-to-TIDES pipeline, web inspector, cleanup tool
│   ├── merge_review/              # Review website, Experiments 01–04c, workbench, retention
│   ├── labeling_ui/               # v1 Streamlit labeling UI
│   ├── labeling_results/          # 18 committed first-pass case summaries
│   └── experiment05_snapshot.py   # Pre-05 snapshot builder
├── experiments/
│   ├── r1_design_spike/           # Spikes 01/02/02R, Protocol v2 core, labeling_ui_v2
│   ├── apc_avl_coverage_study/    # 21-combination coverage factorial and raw audit
│   └── apc_avl_direct_evidence/   # Direct-evidence matcher v2 and probes
├── tests/                         # 451 pytest tests, browser harnesses, acceptance replays
├── docs/                          # All project documentation (index below)
│   └── decisions/                 # ADR 0001–0003
├── releases/
│   ├── workbench-04c-20261021T013832-cddd406e/  # The selected SHA-256-locked release, re-cut 2026-10-20
│   └── pre-experiment-05-20260910/# Hash-locked minimal reproducibility snapshot (kept)
├── database_offline/              # Raw GTFS/AVL/APC inputs (gitignored, ~1.4 GB)
├── artifacts/                     # Generated artifacts (gitignored, ~3.1 GB)
└── outputs/experiment_05/         # Experiment 05 runs and audits (gitignored, ~5.0 GB)
```

### 10.2 Documentation index

Foundations and governance

- [Source data audit](docs/source-data-audit.md)
- [Architecture and data contracts](docs/architecture.md)
- [GTFS implementation review](docs/gtfs-implementation-review.md)
- [Merge implementation plan](docs/merge-implementation-plan.md) and
  [its review](docs/merge-plan-review.md)
- [ADR 0001](docs/decisions/0001-offline-python-prototype.md),
  [ADR 0002](docs/decisions/0002-merge-plan-review-disposition.md),
  [ADR 0003](docs/decisions/0003-isolated-r1-diagnostic-experiments.md)
- [Predecessor notebook methodology](docs/previous_proj_data_integration_methodology.md)
  (background on an earlier, separate project)

R1 spikes and human labeling

- [Spike package README](experiments/r1_design_spike/README.md)
- [Spike 01 findings](docs/r1-design-spike-findings.md),
  [Spike 02 findings](docs/r1-boundary-refinement-spike-findings.md),
  [Spike 02R findings](docs/r1-spike02r-kernel-repair-findings.md)
- [Human labeling protocol v2](docs/human-labeling-protocol-v2.md),
  [Run 01 status](docs/human-labeling-v2-run01-status.md)
- [v1 labeling UI](tools/labeling_ui/README.md),
  [v2 blind UI](experiments/r1_design_spike/labeling_ui_v2/README.md)

APC–AVL diagnostic studies

- [Raw edge cases](docs/apc-avl-raw-edge-cases-20260908.md)
- [Evaluation standard](docs/apc-avl-coverage-evaluation-standard.md)
- [Method comparison](docs/apc-avl-method-comparison-20260908.md) and
  [study README](experiments/apc_avl_coverage_study/README.md)
- [Direct-evidence optimization](docs/apc-avl-direct-evidence-optimization-20260908.md),
  [probes](docs/apc-avl-direct-evidence-probes-20260908.md) and
  [matcher README](experiments/apc_avl_direct_evidence/README.md)

Merge-review workbench

- [Merge-review website and Experiments 01–02](tools/merge_review/README.md)
- [Experiment 03 pipeline](tools/merge_review/gtfs_matching/README.md) and
  [results](tools/merge_review/gtfs_matching/RESULTS.md)
- [Experiment 04](docs/experiment04-continuous-matching.md),
  [04b](docs/experiment04b-delay-weights.md),
  [04c](docs/experiment04c-occurrence-plausibility.md),
  [04c calibration inputs](docs/experiment04c-calibration-inputs.md)
- [Frozen workbench, retention and cleanup](docs/frozen-workbench.md)

Experiment 05

- [Pipeline overview, measured runs and cross-agency comparison](docs/ttc-tides-pipeline-and-comparison.md)
- [Design and contracts](docs/experiment05-design.md)
- [Plateau and right-censoring](docs/experiment05-platform.md)
- [APC stop-visit and passenger-event policy](docs/experiment05-apc-events.md)
- [Diagnostic output schema](docs/experiment05-output-schema.md)
- [TIDES export contract](docs/tides05-contract.md)
- [Visualization guide](docs/experiment05-visualization.md)
- [TIDES compliance and evidence-boundary audit](docs/experiment05-tides-compliance.md)

## 11. Maintenance log

### 2026-10-20: repository cleanup

This entry describes work applied to the `agencies/ttc` subfolder on
2026-10-20. The last `agencies/ttc` commit on `main` is `878089a`
(2026-09-10), with tags `pre-experiment-05-20260910` (commit `3809a76`) and
`experiment-05-v1` (commit `44c1aed`). That history pointer is deliberately
kept in view: the six release directories this cleanup removed and the
pre-translation source strings it replaced are recoverable from `878089a`.

Language and interfaces:

- Every Chinese-language document and user-interface string under
  `agencies/ttc`, outside the pinned `releases/pre-experiment-05-20260910/`
  snapshot, was translated to English: `docs/experiment05-{tides-compliance,platform,apc-events}.md`,
  `docs/experiment04{,b,c}-*.md`, the four `docs/apc-avl-*-20260908.md` reports,
  `tools/merge_review/README.md` and `tools/merge_review/gtfs_matching/{README,RESULTS}.md`,
  the merge-review Python layer and its tests, the merge-review web front end
  and its `.mjs` and contract tests, the Experiment 05 web UI
  (`tools/experiment05/static05/`), labeling UI v2
  (`experiments/r1_design_spike/labeling_ui_v2/`), and
  `experiments/apc_avl_coverage_study/analyze_results.py`. Zero tracked files
  outside `releases/` contain CJK characters. The 42 tracked text files that do
  are all inside `releases/pre-experiment-05-20260910/` — 41 under its `code/`
  snapshot of the pre-translation sources, plus `reports/summary.md`. A
  byte-level scan of compressed bytes also flags two case artifacts in that
  snapshot, `run/cases/7d66b83a36ab6a77245a3d89.json.gz` and its
  `upstream_sample/cases/` twin; their decompressed JSON contains no CJK.
  That snapshot is hash-pinned, matched by a git tag and verified by
  `tools/experiment05/cleanup.py` against an exact file set, so it is
  deliberately not edited; it is the one place the former Chinese strings are
  preserved and it doubles as the zh-to-en glossary of record. The pre-cleanup
  versions of the translated files also remain in history at `878089a`.
- Both web interfaces are English-only. The merge-review runtime translation
  layer was deleted (`static/i18n.js`, `i18n-static.js`, `i18n-dynamic.js`,
  `i18n.css`, `tests/test_i18n_runtime.mjs`, `tests/browser_i18n_smoke.mjs`)
  and the four `/i18n.*` routes were removed from `server.py`. The Experiment 05
  UI language switch and its `localStorage` persistence were removed.

Documents:

- `docs/ttc-tides-pipeline-and-comparison.md` is now the English pipeline
  overview and cross-agency comparison. The former Chinese original was deleted
  and the English twin (previously `ttc-tides-pipeline-and-comparison.en.md`)
  was renamed onto that path; there is no longer a separate `.en.md`. The
  gitignored `artifacts/docs-site/index.html` copy was deleted: the project has
  no documentation site.
- `PROJECT_STATUS.md` (this document) was created, and `README.md` was
  restructured as a shorter technical entry point, with the roadmap, the
  open-decision list and the annotated directory layout moved here so that each
  fact has one home.
- `docs/frozen-workbench.md` gained a release-retention paragraph that now
  matches the state of the directory: `releases/` holds exactly one workbench
  release, the one the pointer selects, plus the pre-05 snapshot.
  [Section 5.3](#53-frozen-release-pointer) records the same facts. Its
  results-retention section was corrected in the same pass: it had claimed
  that full 03d/04/04b/04c results were retained and that their disk usage was
  unchanged, which the approved 2026-09-10 cleanup had already made false, and
  it now records what survives and that `./workbench.sh serve` has no saved
  cases for those runs. That section also now names a non-default port for
  `./workbench.sh serve`, whose 8767 default collides with the Experiment 05
  inspector.
- 30 dead relative Markdown links were de-linked: 11 in
  `docs/previous_proj_data_integration_methodology.md`, which pointed at a
  prior external project's files, and 19 in `tools/merge_review/README.md`,
  which pointed at local evidence removed by the approved retention cleanup.
  The paths are retained as code spans for provenance, and
  `tools/merge_review/README.md` gained a note explaining why they are no
  longer links. No tracked Markdown file outside `releases/` now contains a
  broken relative link. The 52 that remain sit in 6 files inside the
  deliberately unedited `releases/pre-experiment-05-20260910/` snapshot, whose
  README and report paths resolve against the original working tree.
- `docs/human-labeling-v2-run01-status.md` was corrected. It had read "Human
  review: not started", while the live run directory holds blind second-pass
  saves for 2 of the 18 parent cases (`C-913f7d4c` and `C-ba617064`, both at
  grain `local_fragment`, round 2, referencing frozen protocol hash
  `f267ef5401b2`), the adjudicated-label set is empty, and
  `factorial_go_no_go.json` reports `NO_GO` with 6 of its 14 conditions false.
  The document now states that, and its 56 protocol and redaction tests in
  `experiments/r1_design_spike/tests/test_labeling_v2.py` still pass.
  [Section 6](#6-work-in-progress-and-unfinished-items) and
  [section 8.1](#81-near-term) carry the same reading.

Code, data and retention:

- `outputs/` is now ignored by a `.gitignore` rule added on 2026-10-20. The
  Experiment 05 run directories it holds (about 5.0 GB locally) were never
  committed.
- The frozen workbench release was re-cut from the English-only sources and
  `config/workbench-release.json` was repointed to it:
  `releases/workbench-04c-20261021T013832-cddd406e` (created
  2026-10-21T01:38:32Z, `release.json` SHA-256 `1e922fa9…`) in place of
  `releases/workbench-04c-20260910T153527-0cb5d1dc` (created
  2026-09-10T15:35:27Z, SHA-256 `462a6a92…`). `./workbench.sh verify` reports
  `PASS` for the new release with 201 code files and 5 raw inputs; it carries
  no CJK text, its `static/` directory holds only `app.js`, `index.html` and
  `styles.css`, and its default profile 04c and 03d/04/04b/04c source profiles
  pin the same four completed runs as before. The re-cut release has new
  provenance and is not claimed to be byte-identical to any 2026-09-10 release.
- The six superseded 2026-09-10 releases (three
  `releases/workbench-04b-20260910T*`, three
  `releases/workbench-04c-20260910T*`, 1,196 tracked files) were removed with
  `git rm`, leaving `releases/` with exactly two directories. Losslessness was
  checked first: all 207 code files of
  `workbench-04c-20260910T153527-0cb5d1dc` are present hash-identical inside
  `releases/pre-experiment-05-20260910/code/` (0 missing, 0 hash mismatches),
  and that release's own `release.json` is preserved at
  `releases/pre-experiment-05-20260910/frozen_release/release.json`. The
  removed directories remain in git history at `878089a`.
  [Section 5.3](#53-frozen-release-pointer) carries the same figures.
- `releases/pre-experiment-05-20260910/` was kept intact and unedited.
  `tools/experiment05/cleanup.py` requires that directory, its
  `pre-experiment-05-snapshot-v1` manifest and the git tag of the same name,
  and the snapshot deliberately preserves the pre-cleanup sources
  byte-for-byte, including the former Chinese UI strings. Its check demands an
  exact file-set match, which stray `ruff` caches written inside the snapshot
  had broken; those caches were deleted and `pyproject.toml` now sets
  `extend-exclude = ["releases"]` so linting never writes into a pinned release
  directory again. The check passes after the pruning: 247 pinned files, 5 raw
  inputs, git tag `pre-experiment-05-20260910` at `3809a76`.
- Test suites were re-measured; see [section 4.9](#49-test-suites). The only
  count that changed is the merge-review suite, and only because the four
  deleted i18n assets left a smaller parametrized allowlist test. The
  pre-existing `ruff` error count is unchanged; it is measured over
  `src tools tests experiments`, which the new `releases` exclusion does not
  touch.

The deletions in this entry — the i18n assets, the superseded Chinese
document and the six superseded release directories — remove copies that are
regenerable, superseded, or preserved in git history at `878089a`. They are not
evidence that any historical output inventory currently passes; see
[section 9](#9-risks-and-constraints).

### 2026-09-10: checkpoint commits

| Commit | Content |
| --- | --- |
| `3809a76` | Snapshot of 04c code, environment and minimal reproducibility set before Experiment 05; tag `pre-experiment-05-20260910` |
| `b26ed05` | Experiment 05 reproducible batch pipeline and deterministic TIDES assignment |
| `d5b7b8a` | Hardened APC boundary evidence (closed-interval intersection) and bounded candidate storage |
| `2059ee9` | Browser acceptance and diagnostic schema contract |
| `44c1aed` | Streaming full-data mode-independence acceptance checks; tag `experiment-05-v1` |
| `878089a` | Checkpoint of Experiment 05 and research-workbench progress (current `main`) |
