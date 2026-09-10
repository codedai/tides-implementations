# Experiment 05: reproducible, date-selected batch pipeline

## Scope and provenance

Experiment 05 reuses the validated 04c canonical construction, sequence
projection, continuous passage scorer, operational-delay prior, occurrence
confusability and exact candidate ordering. It does not produce a performance
comparison against 04c. Research assignments are not calibrated ground truth.

The pre-development baseline is Git tag `pre-experiment-05-20260910`, commit
`3809a76`, with a hash-inventoried minimal set under
`releases/pre-experiment-05-20260910/`. It contains frozen source, final config,
tests, environment, run manifests, reports, canonical references and complete
representative 04c/upstream cases. Original raw sources and GTFS archives remain
in place, with hashes recorded. Historical full-run replays may require retired
intermediates; fresh raw rebuilds do not. No date/vehicle/trip-specific winners
are used by Experiment 05.

`config/experiment05-v1.toml` is the authoritative versioned configuration.
All new physical thresholds and the inherited matcher settings are explicit.
The pipeline version includes a content hash of executable code, local assets,
vendored schemas and dependency locks. A run also records the Git commit,
complete dependency environment, config hash and input hashes. Configuration,
code or dependency changes during a run fail closed.

## Stage diagram and interfaces

```text
raw AVL CSV ─┐
             ├─ 1 load_raw ─ 2 normalize ─ 3 event_timeline ─ 4 project_avl
raw APC CSV ─┘                                               │
date-active GTFS + canonical reference ───────────────────────┘
                                                            ↓
5 segment_trips + extent/censor diagnostics → 6 attach_apc / stop visits
                                                            ↓
7 generate_candidates: fixed rankings per mode; shared observations only once
                                                            ↓
8 assign_scheduled: three locked rounds, separately for each mode and group
                                                            ↓
9 map_tides: one package per mode → 10 validate_publish → successful cache
```

Feed availability is checked before initialization; canonical construction is
part of projection. Numerical projection chunks may reset at source direction
changes, gaps or excluded-route intervals, but do not create performed IDs.
Performed-trip segmentation happens only after projection. Plateau assessment
may consult raw APC activity and a high-recall schedule counterfactual before
candidate scoring; it never consults a final scheduled assignment.

Every stage has explicit owned outputs (`pipeline.STAGE_OUTPUTS`), row counts,
input prerequisites, timestamps and output hashes. Stage tables have
`experiment05-table-v1` descriptors (`*.schema.json`), including column types,
JSON-encoded diagnostic columns, row counts and the Parquet SHA-256. Native
scalar columns remain queryable. JSON diagnostic columns preserve arbitrary
nested alternatives without Polars schema-inference truncation.

Successful stages are independently verifiable and idempotent. Inputs and
completed prerequisites are rechecked before continuing. Failed stages retain
partial files; no failed run is a successful cache. Unique atomic temporary
files permit recovery without overwriting unidentified interrupted writes.
TIDES stage recovery accepts only a complete package bound to the same run,
mode, code/config and upstream stage hashes. Final validation independently
compares each exported scheduled assignment with the current assignment table.

## Time, identity and source-preservation contract

The on-demand selection is a **local calendar-day observation window**, with
the selected date also constraining GTFS service-date assignment. This is not a
claim that a civil day and an operating service day are identical. Early-hour
rows retain previous-day service-date hypotheses, but are not silently assigned
to a different service date. Consequently, the subsequent civil day's portion
of a scheduled trip with GTFS times above 24:00 is not loaded by this window.
Extended GTFS times are nevertheless preserved and correctly ordered; expanding
observation windows into complete operating days is an explicit future policy
change, not an implicit date conversion.

Source civil timestamps are independently normalized with an IANA timezone.
Unique source times become UTC epoch seconds. Ambiguous/nonexistent DST times
retain raw values and flags, with no invented fold or trajectory timestamp.
GTFS uses the local-noon-minus-12-hours UTC anchor, including extended hours.

Each retained daily source row has an immutable
`rid = source:full_input_sha256:original_csv_row_number`. The complete raw payload,
source file hash and original row number are preserved. Vehicle IDs are trimmed
without stripping leading zeros; devices remain null unless supplied. Exact
AVL duplicates are annotated with `duplicate_of`, remain in diagnostics, and
contribute no additional trajectory weight. APC intervals are not collapsed by
a nearest-time join. Out-of-route rows remain in shared source diagnostics but
do not enter route-29 matching or TIDES packages.

Schedule-derived source fields (`AvgOTP`, source trip assignments and analogous
flags) remain audit-only; matching inputs explicitly exclude them. APC without
genuine coordinates remains stop/time-interval evidence, never a made-up ping.
Stop codes map through the selected feed; uncertain attachment remains explicit.
APC intervals may be clipped for scoring at a confirmed effective boundary while
raw arrival/departure values remain untouched. Passenger counts are not prorated.

## Segmentation and plateau states

Boundaries use confirmed projected direction changes, joint AVL/APC gaps over
the configured threshold, and sequence-restart progress resets. IDs derive from
the input lineage and raw segment boundaries, not scheduled IDs or match modes.
Internal plateaus with recovery do not automatically split a trip.

The platform state machine and counterfactual vetoes are defined in
[experiment05-platform.md](experiment05-platform.md). States include `NORMAL`,
`STATIONARY_PLATEAU`, `TERMINAL_LAYOVER`, `LONG_MIDROUTE_STOP`, `FROZEN_TELEMETRY`,
`OFF_REFERENCE_MOVING`, `REFERENCE_MISMATCH`, `RIGHT_CENSORED_TAIL` and
`RECOVERED_AFTER_PLATEAU`.

Flat canonical progress alone is insufficient. Raw movement, continuity,
alternative references/directions, terminal/short-turn context, APC activity and
recovery are checked. Counterfactual progress holds the pre-plateau delay fixed
or allows only configured growth; it cannot explain indefinite stationarity
through freely increasing delay. Candidates already finished are not
extrapolated; insufficient eligible hypotheses cause abstention. A confirmed
censored tail keeps one AVL endpoint and relevant APC interval. All source rows
remain diagnostic, but censored observations do not enter trajectory/delay/
coverage calculations. Uncensored OFF_REFERENCE rows still affect coverage.

## Candidate generation and three-round assignment

Modes keep their 04c names and definitions:

- `global`: the complete eligible projected segment.
- `early`, `middle`, `late`: chronological thirds of valid projected travel
  distance, with boundaries snapped to real AVL observation times. Rejected
  projection gaps do not contribute length; APC intervals are scoped explicitly.

Each mode independently retrieves and scores its candidates. Rank 1 is best;
the unchanged 04c ordering tuple is `(score, candidate_id)` ascending. Scores,
unweighted components, weights, coverage, delay summaries, hard constraints and
all ranks are persisted before assignment. Rank 4+ is audit-only.

Groups are `(service_date, route_id, direction_id, match_mode, gtfs_feed_version)`.
The route in this internal key is the feed's route namespace; the UI route
selector is the public route number. The manifest records both identities.
Actual order is `(actual_start_t, stable trip_id_performed)`; scheduled order is
`(scheduled_start_service_seconds, stable trip_id_scheduled)`, without wrapping
extended hours. No occupancy is shared between modes.

```text
for group in stable_group_order:
    locked = empty
    for rank in [1, 2, 3]:
        proposals = this rank only, for each still-unmatched performed trip
        validate feed/date/route/direction/existence and authoritative schedule order
        reject already used occurrences
        enforce strict bounds from nearest locked predecessor/successor
        choose a strictly increasing proposal chain:
            maximize assignment count
            then minimize sum of unchanged candidate scores
            then compare ordered (score, candidate_id, performed_id) tuples
        lock selected assignments; never change earlier rounds
    emit one row per performed trip, including genuine null if unassigned
```

The monotone-chain dynamic program checks conflicts **within** the round as well
as against earlier locked assignments. Existing scores are accumulated exactly
for deterministic total-quality comparisons; no new score or score threshold is
introduced. Hard constraints are never waived. Invalid candidate references are
audited, not silently replaced by lower ranks in the same round.

Assignments are `ASSIGNED`/`UNMATCHED`, separate from the matcher's calibrated
status. Reasons include `NO_CANDIDATES`, `TOP3_EXHAUSTED`,
`LOST_SAME_SCHEDULE_CONFLICT`, `CANDIDATE_ALREADY_USED`,
`VIOLATED_LOWER_ORDER_BOUND`, `VIOLATED_UPPER_ORDER_BOUND`,
`CROSSED_CURRENT_ROUND_ASSIGNMENT` and `INVALID_GTFS_REFERENCE`.
Reason counts are nonexclusive; one unmatched trip may have multiple diagnostics.

## Output layout and schema

```text
outputs/experiment_05/
  cleanup/                       # dry-run, protected inventory, execution audit
  runs/<run_id>/
    config.toml
    run_manifest.json
    summary.json
    validation_report.json
    stages/                      # mode-independent, pinned stage checkpoints
    gtfs/{bundle,canonical}.json
    fused_projected_observations.parquet
    performed_trip_segments.parquet
    platform_diagnostics.parquet
    apc_attachment_audit.parquet
    stop_visits_evidence.parquet
    candidate_rankings.parquet
    local_window_evidence.parquet
    mode_results.parquet
    scheduled_lookup.parquet
    assignments.parquet
    assignment_audit.parquet
    tides/<match_mode>/
      datapackage.json
      trips_performed.csv
      stop_visits.csv
      vehicle_locations.csv
      vehicles.csv
      validation_report.json
      completion.json
      diagnostics.json
      summary.json
      schemas/
```

The four required diagnostic tables have grains of one retained source row, one
performed segment, one candidate per performed/mode/rank, and one assignment
proposal/audit event respectively. All segment extent fields, alternative
projections and censor decisions are available. `assignment_audit` also retains
rank-4+ nonproposal diagnostics. Shared raw/projection/segmentation data is not
copied into four mode folders.

Large repeated scheduled trajectories are referenced through shared canonical
GTFS data. Per-ping candidate delay traces are optional via
`execution.persist_alignment_traces`; default outputs retain all score
components and local evidence, not duplicated trace arrays. Nested diagnostics
are described by each table's sidecar, rather than inserted into TIDES core.

See [tides05-contract.md](tides05-contract.md) for fixed TIDES v1.0 schemas,
profile/Frictionless validation and cross-table rules. Empty CSV cells encode
null; JSON uses genuine null. No string `None`, `null` or `NA` is emitted as a
missing value. Current APC gives counts over intervals, not confirmed passenger
event timestamps; `passenger_events.csv` is omitted with explicit reasons.
Devices are omitted by default. No invented core fields are added.

## CLI and browser use

From `agencies/ttc` with the existing offline pinned environment:

```sh
./experiment05.sh availability
./experiment05.sh run --service-date 2023-11-06 --route-id 29 --match-mode all
./experiment05.sh run --service-date 2023-11-20 --match-mode early
./experiment05.sh run --service-date 2023-11-06 --match-mode all --force-rerun
./experiment05.sh web --port 8767
```

Open `http://127.0.0.1:8767`. The page selects a date and a single mode or all
four, shows input availability, stages/counts, assignments/reasons, validation
and hash-checked file/package downloads. It calls the same `run_pipeline`
library as the CLI. Requests are local-only and do not accept arbitrary paths.
No public Internet deployment or streaming processing is implied.

The successful cache key includes date, public route, selected modes, pipeline
version, config hash, raw AVL/APC hashes, GTFS archive/metadata hashes and runtime
environment. Default calls reuse verified success; force creates a new run.
Run metadata timestamps/IDs differ on force, but fixed-input business outputs
and IDs must agree. Missing inputs/feed or invalid stages return explicit errors.

For independent stages:

```sh
./experiment05.sh init --service-date 2023-11-06 --match-mode all
./experiment05.sh stage load_raw --run-dir outputs/experiment_05/runs/<run_id>
./experiment05.sh stage normalize --run-dir outputs/experiment_05/runs/<run_id>
# Continue using the ten stage names in order, or resume any validated boundary.
./experiment05.sh verify --run-dir outputs/experiment_05/runs/<run_id>
```

Use `uv sync --frozen` to restore dependencies on another installation; raw
inputs are supplied separately. Output directory names must be Web-compatible.
The final implementation and acceptance report record actual full-data runs,
cleanup outcomes and any remaining limitations, without a 04c comparison.
