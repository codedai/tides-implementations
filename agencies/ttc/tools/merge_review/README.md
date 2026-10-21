# APC–AVL Trip-by-Trip Review and One-Day Visual Debug

A local evidence workbench. Frozen review mode reads the final `full02` directly;
one-day debug mode reruns only **2023-11-06** and saves each segmentation and
merge as a separate experiment version. Neither mode introduces GTFS, and neither
modifies the raw data, the frozen `full02` segmentation or earlier human labels.

Convention for subsequent experiments in this project: **unless the user specifies
another date, use only 2023-11-06 and present the results in the same workbench on
port 8767**. New experiments are saved as new versions and never overwrite earlier
results or human notes.

> **Artifact paths in this document.** Paths under `artifacts/` name local
> evidence files, which are never committed. Many of them were removed by the
> approved retention cleanup described in
> [frozen workbench retention](../../docs/frozen-workbench.md); they are recorded
> here as provenance, not as files you can expect to open. Regenerate results
> before relying on any of them.

## Interface language

The interface is English-only. All interface text is served by the local workbench;
no external translation service is called. Source data, JSON diagnostics, machine
status codes, IDs, experiment names and user input are displayed verbatim and are
never rewritten for presentation. The official workbench continues to serve the
existing experiment 03d results.

## Experiment 01: merge by VehicleID only and sort by time

Adds a `vehicle_only` mode: the AVL and APC records of the same vehicle are combined
with **UNION ALL** into one observation stream. "Merge" here means a source-tagged
union of records followed by ordering; it is neither a many-to-many join of the two
tables on vehicle number alone nor nearest-time matching. No trip segmentation,
spatial gate, time-difference gate, interpolation or evidence-matching algorithm is
invoked.

- Records are grouped by exact match on the normalized raw vehicle-number string.
  The vehicle sets of both sources are kept; a vehicle is not dropped because only
  one source contains it.
- AVL uses the raw `time`; APC uses the raw `arrival`, and `departure` is used only
  when the arrival time is missing, with an explicit flag. No raw arrival or
  departure time is modified, and no two-source clock offset or service day is
  guessed.
- Within a vehicle, records are sorted by `event_time`, with missing times placed
  last. Ties are ordered by a fixed source order and then by raw row number / ID;
  this only guarantees reproducible display and does not mean AVL or APC actually
  occurred first.
- Duplicate raw rows are retained; no Cartesian product of every AVL × every APC is
  generated, and passenger counts are not summed.
- Each vehicle is one **vehicle-day observation stream**, not a trip. Time gaps only
  break the line in the trajectory plot; they do not split the stream.
- APC is flagged `not_evaluated` (matching not performed), not `unmatched`
  (matching failed), and carries no match score. APC is not projected onto the AVL
  distance curve; the map / timeline only show the real source positions and times
  side by side.

Selecting "VehicleID grouping + time ordering only" in the console disables the
segmentation and merge parameters. The combined observation table interleaves the
two sources and supports search, pagination and click-to-locate. Every raw record
keeps its source ID, raw time and the fields from the day's input; the raw CSV is
unchanged. When comparing against older versions that performed spatiotemporal
matching, no "match-rate improvement" or "trip boundary change" is computed, because
the two experiments did not perform the same kind of judgment.

The only configuration for this mode is:

```json
{"method":"vehicle_only","segmentation":{},"merge":{}}
```

The formal run `run-20231106-20260908T204034-97eba31b5e7f` is complete, and the
original version is kept in the workbench version list. It saved
**232,985 observations = 213,895 AVL + 19,090 APC** and 51 vehicle-day groups,
not 51 confirmed trips. No trip chunks or APC–AVL match records were generated;
3 APC records with departures past midnight kept their raw departure times.
Full-day collection plus the in-memory / on-disk two-pass check took
**33.805 seconds**, with zero violations in the source, field, day-range, ordering
and no-spurious-match checks.

The full regression suite **passed 483 tests**, including HTTP experiment creation
and checks that forbid calling the segmentation / matching algorithms. In the
browser, fields outside JavaScript's exact integer range (such as the UInt64
`payload_hash`) are rendered as decimal strings to avoid silent rounding; the values
and types in Parquet are unchanged. Times use the declared local wall-clock axis.
Run check report: `../../artifacts/merge-debug-20231106/runs/run-20231106-20260908T204034-97eba31b5e7f/summary.json`,
regression report: `../../artifacts/merge-debug-20231106/vehicle-only-regression-tests.xml`.

Final browser acceptance **passed 97 checks**: full pagination / filtering, raw IDs
from both sources, readable local arrival and departure times, map selection by raw
coordinates, exact display of large integers, no APC projection, cross-mode
incomparability, review-record isolation, refresh and mobile layout; no browser
errors, HTTP errors or external requests. The original frozen mode separately
**passed 82 compatibility checks**. Acceptance used an independent test workspace;
after one real experiment was created, that run was reused for the remaining
checks, and no test notes were written to the formal run. Report:
observation-stream browser acceptance (`../../artifacts/merge-debug-20231106/vehicle-stream-browser-acceptance/final-display/browser-vehicle-stream-results.json`),
screenshots: same-vehicle time-ordered table (`../../artifacts/merge-debug-20231106/vehicle-stream-browser-acceptance/final-display/vehicle-source-stream.png`),
dual-source timeline with AVL distance (`../../artifacts/merge-debug-20231106/vehicle-stream-browser-acceptance/final-display/vehicle-time-progress.png`).

To retest the observation-stream UI, start port 8768 with an independent
`acceptance-workspace` and test-notes directory, then run:

```sh
node tools/merge_review/tests/browser_vehicle_stream_smoke.mjs --allow-test-writes
```

Only the dedicated local test port 8768 is allowed. `--existing-run <test run ID>`
reuses existing test results, but review notes are still saved to that test run.
Do not enable this write test against the formal workspace.

## Experiment 01 AVL-deduplicated variant and full-day horizontal browsing

`vehicle_only_dedup` is an independent derivative of experiment 01: it still collects
records by VehicleID only and sorts them by source time, without trip segmentation,
spatiotemporal pairing or APC deduplication. The original `vehicle_only` experiment
and its human notes are unchanged.

Deduplication requires **exact field-by-field equality on all 13 business fields** of
the raw AVL CSV, not merely vehicle and time, and not `payload_hash` alone. Each
group keeps the row with the smallest `source_row`; records that differ in time, in
any field or in raw string form are all retained. APC records are never removed, even
with the same vehicle and arrival time, to avoid deleting records from different
directions.

The derived run's `observations/` contains only the retained AVL plus all APC;
`deduplication/` stores a snapshot of the original fields and the mapping from every
original AVL row to its representative. The web page's map, timeline, distance curve
and sorted table use the deduplicated records; the raw CSV and experiment 01 remain
available for recovery or lookup. Deduplication does not judge a record to be wrong,
and it is not a matching-accuracy improvement.

The dual-source timeline gains an independent "Pan across the day" range slider.
Choose a 10 / 30 / 60 minute window first, then drag the slider to pan it; the map
and distance curve follow. Keyboard Home / End jump to the earliest / latest window
of the day. The existing "Time cursor" below it still changes only the instant being
viewed within the window and does not replace range panning. In the full-day overview
the slider is explicitly disabled, with a one-click return to the 30-minute detail
window. The combined observation table shows all retained records by default and
filters with the range slider only when "Current viewing window only" is checked.
Panning never deletes data or re-splits trips.

The configuration still contains no segmentation or matching thresholds:

```json
{"method":"vehicle_only_dedup","segmentation":{},"merge":{}}
```

The formal deduplicated run **`run-20231106-20260908T211936-160cab4053e8`**
("experiment 01b") is now the latest result loaded when the workbench starts:
**177,638 AVL + 19,090 APC = 196,728 observations** across 51 vehicles, with
36,257 exact AVL duplicate copies removed. All 213,895 raw AVL rows have a
recoverable representative mapping. The full-day run plus the two-pass check took
**40.833 seconds**; an independent re-check confirmed that all APC raw fields are
unchanged, that every representative is the smallest source row within its group of
fully identical raw fields, and that the content hashes of experiment 01's manifest
and summary are unchanged.

The full backend regression suite **passed 526 tests**; the legacy frozen browser
compatibility regression **passed 82 checks**. The new interface created a real
deduplication experiment for the first time and **passed 55 browser checks**,
covering real mouse clicks / drags, Home / End / arrow keys, the first and last
windows of the day, three-chart linkage, source mapping and mobile layout; it used an
independent test workspace and wrote no formal notes. Reports:
dedup run summary (`../../artifacts/merge-debug-20231106/runs/run-20231106-20260908T211936-160cab4053e8/summary.json`),
full regression (`../../artifacts/merge-debug-20231106/dedup-timeline-regression-tests.xml`),
real run and interaction acceptance (`../../artifacts/merge-debug-20231106/dedup-timeline-browser/browser-dedup-timeline-results.json`).
After the mobile overview width fix, the same test run was reused and **passed 55
final checks** (zero new experiments / notes written); the full-day overview was also
verified to include all retained AVL and APC rather than only the current window.
Final report: responsive and full-day coverage check (`../../artifacts/merge-debug-20231106/dedup-timeline-browser/final-responsive/browser-dedup-timeline-results.json`),
screenshots: horizontal slider (`../../artifacts/merge-debug-20231106/dedup-timeline-browser/final-responsive/timeline-first-window.png`),
mobile layout (`../../artifacts/merge-debug-20231106/dedup-timeline-browser/final-responsive/timeline-mobile.png`).

Retesting deduplication and the slider still requires the independent 8768 test
workspace:

```sh
node tools/merge_review/tests/browser_dedup_timeline_smoke.mjs --allow-test-writes
```

This script creates a new test run; add `--existing-run <test run ID>` to only
re-verify existing results. The script writes no human notes, and external browser
requests are blocked.

## Experiment 02: filter PI/PO and split candidate trips by two-source direction

`direction_destination` builds on experiment 01b's **exact 13-field AVL
deduplication** and filters out representative records whose AVL `Route` is `PI` or
`PO`. The test is exact equality after trimming surrounding whitespace and
upper-casing; records are not filtered because an APC `Branch` contains letters or a
Destination is missing. The raw CSV, earlier experiments and human notes are
unchanged. The deduplication mapping still covers all raw AVL; the full fields and
reason for each filtered representative are stored separately in
`direction/{vehicle_case}.json`.

This version performs only time-evidence-based **candidate trip segmentation**: it
does not invoke the existing spatiotemporal matcher, does not project APC progress,
and does not use Heading, GTFS, source trip IDs or route changes as a substitute for
this direction experiment.

Explicit experiment rules:

1. For each vehicle, read the `NORTH / SOUTH` prefix of the AVL `Destination` and the
   APC `direction` separately, without rewriting the raw fields. Unknown directions
   are retained and are never automatically treated as the opposite direction.
2. An AVL direction is carried forward briefly from its fix time to the next fix with
   a different time, for at most **30 seconds**; the next PI/PO or unknown-direction
   record also truncates this carry-forward, so that it does not bridge a filtered
   interval without direction evidence. The last AVL record provides point evidence
   for its own instant only. This is an experimental assumption, not a
   vendor-confirmed field validity period.
3. APC arrival–departure intervals in forward order are treated as closed intervals.
   Records with a missing endpoint or a reversed interval provide point evidence only
   at the usable sort time, and the fallback reason is recorded explicitly; invalid /
   time-ambiguous records provide no definite direction but are retained. Direction
   evidence is confined to the local calendar day; raw departures past midnight are
   not clipped.
4. The set of active directions is checked in time batches. A definite direction is
   produced only when the set is NORTH-only or SOUTH-only. When both directions are
   present at once, both sides of the conflict and the time window are retained, and
   no spurious short segment is cut because of the stable same-instant display order.
   The previous candidate segment is kept during a conflict; a new candidate segment
   is started only at the next non-conflicting definite direction reversal.
5. When an APC interval spans a boundary, multiple `candidate_segment_ids` and a
   boundary-overlap flag are retained. Each observation is still persisted exactly
   once; the per-segment web page may display the record more than once for review,
   but passenger counts must not be summed repeatedly per candidate segment.
6. Unknown time spans and both ends of the day keep their undecided / range-truncation
   notes. A long span with no observations does not automatically prove that the
   vehicle was still on the same trip, and this version does not additionally split
   on time gaps. A direction reversal is not by itself an independently verified real
   trip boundary.

Conflict annotations distinguish cross-source from same-source direction
disagreement; `conflict_windows` are review windows generated from the active
evidence set, and adjacent windows may belong to the same operational event, so they
must not be counted as independent errors. The map, timeline, direction-segment queue
and combined observation table can look up the raw source rid; context and candidate
members are counted separately. Cumulative distance comes only from actual AVL
positions and is not a candidate-trip completion percentage.

The rules of this version are fixed and recorded in the manifest's
`direction_segmentation`, with no hidden legacy matcher thresholds:

```json
{"method":"direction_destination","segmentation":{},"merge":{}}
```

When comparing against 01b, check whether PI/PO filtering explains all source
changes, whether APC is fully retained, and the candidate / conflict counts. A
reduction in candidate count or conflicts is not interpreted as improved matching
accuracy.

The formal run **`run-20231106-20260908T220649-9be72540c029`** is complete; its
parent version explicitly points to experiment 01b
`run-20231106-20260908T211936-160cab4053e8`, and it reuses that run's validated
calendar-day input directly. It produced
**187,962 unique persisted observations = 168,872 AVL + 19,090 APC**, filtering
8,766 deduplicated PI/PO AVL records (PI 3,339, PO 5,427). The 36,257 duplicate
copies from 01b are still explained by the full deduplication mapping and are not
conflated with this PI/PO filtering.

All 51 vehicles have an audit entry; 50 produced a total of **541 direction
candidate segments**, and the remaining vehicle has only filtered records.
**795 APC and 13,811 AVL** records are involved in direction conflicts; **267 APC**
records span candidate boundaries and retain multiple memberships. Among the 14,127
conflict evidence windows, adjacent windows may belong to the same operational
conflict, so they do not represent 14,127 independent errors. This run did not
automatically delete conflicts, drop APC, pick a "better" GPS match point based on
direction, or sum candidate passenger counts.

The full-day run, including the in-memory / on-disk two-pass validation of
deduplication, source conservation and direction evidence, took **96.879 seconds**,
with zero violations in all implemented checks. The full merge-review /
coverage-study / direct-evidence / raw-audit regression suite **passed 502 tests**;
the legacy frozen page **passed 82 browser compatibility checks**, using only an
independent temporary notes database. Structural feasibility on the formal data does
not mean trip accuracy is known.

An independent column-by-column re-check confirmed that all retained AVL/APC raw
fields and the 213,895-row deduplication mapping are consistent with 01b, and that
all candidate-membership and conflict-source relationships are consistent in both
directions. The site's data layer actually loads 541 candidates + 1 audit window.
The fully filtered vehicle is **9037**: its 34 raw AVL rows deduplicate to 26, all
PO, with no APC; it provides only an exclusion audit and does not fabricate a trip.

Short segment to review first: vehicle **9047**, `direction:9047:0002`, a
**12-second NORTH candidate from 07:23:36 to 07:23:48**. After the raw interval
07:04:27–07:23:36 of one SOUTH APC record ends, only AVL NORTH evidence remains
briefly; at 07:23:48 the AVL turns SOUTH again. The current rules have no minimum
duration / direction smoothing, so this segment is retained. It is an experimental
boundary awaiting review, not a confirmed 12-second real trip; the web page retains
the preceding long APC record and the AVL boundary evidence on both sides for lookup.

Results: experiment 02 summary (`../../artifacts/merge-debug-20231106/runs/run-20231106-20260908T220649-9be72540c029/summary.json`),
backend regression (`../../artifacts/merge-debug-20231106/direction-regression-tests.xml`),
legacy frozen browser compatibility (`../../artifacts/merge-debug-20231106/direction-frozen-compat/browser-results.json`).

A real browser successfully created an independent test experiment 02 with the new
version and **passed 54 creation / interaction checks**. Finally, the same test run
was reused for **54 read-only re-checks**, covering vehicle 9047's second-scale short
candidate, conflict-window open / closed endpoints, cross-vehicle search, loading
guards when switching versions, raw-field lookup, multi-candidate membership,
three-chart linkage and mobile layout; zero browser / HTTP / external-request errors,
and no formal or test human notes written. An independent compatibility re-check of
01b separately **passed 55 checks**, reusing the original test results without
creating a new experiment.

Reports: real experiment creation (`../../artifacts/merge-debug-20231106/direction-browser/actual-02/browser-direction-results.json`),
final read-only check (`../../artifacts/merge-debug-20231106/direction-browser/final-short-candidate-fixed/browser-direction-results.json`).
Two asynchronous-request issues in the interface (a version switch overwriting the
method, and a stale search callback misapplied to the new vehicle) were fixed with
loading guards and queue isolation; this does not change any saved experiment method
or result. Old failure screenshots produced during testing, and one 01b test copy
created with a mis-selected method, remain in the independent acceptance directory
and do not enter the formal workbench.

For retests, point only at the independent 8768 test workspace:

```sh
node tools/merge_review/tests/browser_direction_smoke.mjs --allow-test-writes
```

Adding `--existing-run <test run ID>` re-verifies existing results read-only,
without resubmitting an experiment or writing review notes.

## One-Day Visual Debug (new in this iteration)

Start from `agencies/ttc`:

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.server --debug-date 2023-11-06
```

Open **http://127.0.0.1:8767**. By default this does not occupy port 8765 used by the
original frozen site. The first start extracts the single-day source data and runs
the initial baseline; later starts reuse the cached single-day input and the
completed versions. The day's raw input has 213,895 AVL and 19,090 APC records
across 51 vehicles.

Debug workflow:

1. Select a completed version and a baseline version for comparison.
2. Choose `vehicle_only` to only collect source records, `vehicle_only_dedup` to
   first deduplicate AVL exactly, or a segmentation method (`gap_only` /
   `avl_context` / `apc_assisted`) and adjust the segmentation and direct-evidence
   merge parameters. Enter an experiment name and rerun all vehicles for the day.
   Only one background experiment runs at a time; older versions can still be
   browsed meanwhile.
3. Compare candidate segment boundaries, APC statuses and membership changes, then
   inspect the specific source records on the map, timeline and cumulative distance
   curve.
4. Save an independent human judgment for the current version. A new version does
   not inherit an older version's "acceptable" conclusion.
5. Change parameters step by step and create new experiments; the code and
   configuration fingerprints are recorded in each version, and old results are not
   overwritten. If the local algorithm code was modified, restart the debug server
   before creating an experiment, so that the long-running process does not keep
   using already-imported stale code.

Comparison aligns on the raw APC `rid`, not on segment IDs that may change.
Segment-membership changes are determined by comparing the actual GPS member sets: a
segment ID rename is not by itself a membership change. Diagnostic changes are not
automatically interpreted as "improvement". Match ratio, segment count and
lossless-retention checks are not accuracy; without independent ground truth,
accuracy remains unknown.

The date range is explicitly the **source-local calendar day
[2023-11-06 00:00, 2023-11-07 00:00)**, not an inferred service day. AVL is selected
by raw fix time and APC preferentially by arrival time; departure time is used only
when arrival is missing. Raw APC departure times past midnight are retained, not
clipped to midnight. Neighboring-day GPS is not loaded by default, so the day edges
are **data-range truncation**, not real trip start / end points; a single day's
segments cannot prove a complete cross-day trip.

This iteration provides no "promote to full data" button, and browsed human notes
are not treated as blind ground truth. After single-day iteration, dates not used for
tuning plus boundary cases must still be validated before full-data application is
discussed.

The default experiment directory is `artifacts/merge-debug-20231106`; the review
database is in its `reviews/`. Use `--debug-dir` and `--state-dir` to specify
independent locations and `--port` to set the local port. The HTTP interface accepts
no arbitrary file paths, arbitrary dates, scripts or commands, only known parameters
with bounded ranges. Shutting down the server interrupts unfinished experiments, and
incomplete results are not loaded as successful versions; completed versions and
review records are preserved.

The current field display keeps the APC `Route`, `RouteName` and `Branch`; lettered
branches are not automatically rewritten as route numbers or non-revenue labels.
Unassociated APC windows no longer claim to be "APC-only real single-source trips";
the actual GPS inside a window and the GPS of already-assigned segments are counted
separately, and displayed context does not equal accepted association.

### Acceptance for this iteration (2026-09-08)

- The full regression suite **passed 437 tests**, including the one-day runner, data
  adapter, HTTP and existing algorithm tests.
- The real single-day default run took **14.543 seconds**, with 51 vehicles and 130
  candidate segments; all 19,090 APC records have a unique disposition record. The
  implemented source-conservation / supporting-evidence checks had zero violations;
  accuracy remains unknown.
- One-day browser acceptance **passed 75 checks**, actually changing parameters and
  creating a new version, and checking diffs, three-chart selection, note protection,
  cross-version isolation and mobile layout; no external requests or browser errors.
- The final UI separately **passed 15 read-only re-checks**, covering the
  boundary-reason-change swimlane, refresh recovery and mobile layout, with zero
  POSTs.
- The original frozen-mode browser regression **passed 82 checks**, using an
  independent temporary review database, without writing real legacy notes.

Local speed is a measurement for this data slice and configuration, not a performance
guarantee for all parameters. Browser acceptance uses an independent
`acceptance-workspace`; test experiments do not appear in the formal workbench's run
list. Results: one-day browser acceptance (`../../artifacts/merge-debug-20231106/browser-acceptance/browser-debug-results.json`),
boundary and record diff screenshot (`../../artifacts/merge-debug-20231106/browser-acceptance/source-rid-boundary-diff.png`),
three-chart screenshot (`../../artifacts/merge-debug-20231106/browser-acceptance/rerun-timeline-progress.png`).

To retest the one-day browser flow, first start **8768** with an independent
workspace and review directory, then run:

```sh
node tools/merge_review/tests/browser_debug_smoke.mjs --allow-test-writes
```

This script creates experiments and writes test notes, so only the dedicated
`127.0.0.1:8768` is allowed; do not point it at the formal workbench.

## Starting frozen review mode

Run from `agencies/ttc`, reusing the existing locked Python environment, with no new
dependencies, npm build or CDN:

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.server
```

Open **http://127.0.0.1:8765**. Press `Ctrl+C` in the terminal to stop. If the port
is in use, specify `--port 8767`. The server listens only on the local loopback
address and cannot be used directly for public deployment.

To specify the data and an independent review-record directory explicitly:

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.server \
  --run artifacts/apc-avl-direct-evidence-20260908/full02 \
  --state-dir artifacts/merge-review-website-20260908/reviews \
  --port 8765
```

## How to review

1. On the left, select the segmentation context and vehicle, then filter by date,
   route, association status or human review status. The default is `avl_context`
   and vehicle 9027 (when present). This is only a browsing starting point, not a
   random evaluation sample.
2. Use previous / next trip or `J` / `K` to step through items one by one. The queue
   includes the frozen AVL segments as well as APC review windows that are
   unassociated, non-uniquely assigned or whose association was not accepted; it
   does not show only successful matches.
3. Click an APC record on the map, timeline or detail table to see that record's
   positive evidence, counterevidence, competing candidates, spatiotemporal residuals
   and real source IDs. "Context" from adjacent records does not mean they are
   associated with the current segment.
4. For long segments, choose a 10 / 30 / 60 minute window, move backward or
   forward, or focus on the selected APC ±5 minutes; "Nearby context" can be
   0 / ±3 / ±15 / ±30 minutes, used to inspect single-source tails near the start and
   end points. This is a viewing range and does not change membership or trip
   segmentation. Trajectory gaps stay visible and are not bridged with connecting
   lines.
5. Choose "Acceptable / Problematic / Uncertain", write a note, then save or
   "Save and next trip". `Ctrl/Cmd+S` saves; the bottom left exports a JSON review
   record with history.

The review objects are **candidate trips / observation segments, not confirmed
`trip performed`**. Some segments can be as long as a day, and APC attachment
windows are organized in 15-minute buckets for browsing; these windows do not
constitute new trip segmentation or matching. Cross-segment APC records can appear
in multiple review objects, so queue counts cannot be used to count distinct trips
or to sum passenger counts.

## What the three visualizations mean

| View | What can be checked | What it does not represent |
|---|---|---|
| Map | Actual same-vehicle GPS trajectory, APC positions, supporting points, real interpolation endpoints and positional counterevidence | A confirmed route / trip identity |
| Dual-source timeline | Coverage of both sources, arrival–departure intervals, GPS gaps, evidence anchors | That the two source clocks are synchronized, or that the vehicle was continuously stopped for the whole interval |
| Time–distance curve | Cumulative distance within continuous valid GPS observation runs, supported APC positions | GTFS schedule completion rate, unknown travel across gaps, real total trip mileage |

Cumulative distance breaks at invalid / ambiguous / same-instant position conflicts,
at sampling intervals over 90 seconds, or at implied speeds over 130 km/h, and each
new continuous run starts from zero. This is a conservative geometric definition for
visualization and does not change the merge. Duplicate raw rows are retained, but
identical time-position pairs add no distance. All times are shown on the source
data's local wall-clock axis, without a second browser-timezone conversion and
without guessing the DST fold.

Source passenger-count fields are shown as-is, with unknown values as `—` rather than
zero. Counts are not duplicated because one APC record supports multiple GPS points,
and boarding / alighting counts of unconfirmed granularity are not summed into
whole-trip passenger totals.

## Map and privacy

The default is an offline coordinate map that sends no trajectories externally and
downloads no map tiles. Only after "Street basemap" is actively enabled does the
browser request OpenStreetMap tiles for the current viewport, and the external
service can then learn the IP and the requested geographic extent; do not enable it
when disclosing the geographic extent is not permitted. There is no interface that
uploads raw APC / AVL data or source IDs.

The basemap uses normal browser caching, requests only the current viewport and
shows attribution; it provides no bulk download or offline tile scraping. For
availability and usage limits, see the
[OSM tile service policy](https://operations.osmfoundation.org/policies/tiles/).
Without a network connection, the coordinate map, timeline, progress curve and local
review records remain usable.

## Where reviews are stored

By default, reviews are saved to
`artifacts/merge-review-website-20260908/reviews/reviews.sqlite3`, which is covered
by the existing gitignore, in a directory accessible only to the current user. A
record is identified jointly by the content hashes of the run manifest / config /
completion, the segmentation method, the vehicle and the review object. Different
result versions never share a review.

Each save updates the current judgment and appends to the history within the same
SQLite transaction. Multiple tabs are protected against silent overwrites by a
revision check; on conflict the save is rejected and must be reconciled after
reloading. Exports include the current judgment and the history. This is an
**unblinded review performed after seeing the model diagnostics**; exports are
explicitly marked `unblinded=true`, `ground_truth=false`, and never flow
automatically into any training, method scoring or frozen blind-labeling process.

The server includes a Host check, save tokens, an Origin check, a fixed static-file
allowlist, input length limits and a no-external-embedding policy; these are local
safeguards, not the authentication system a public network deployment would require.

## Verification

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m pytest tools/merge_review/tests -q
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync ruff check \
  tools/merge_review
node --check tools/merge_review/static/app.js
```

HTTP tests use a local ephemeral port and a temporary database; restricted execution
environments must allow loopback sockets. The standalone browser test script is
`tests/browser_smoke.mjs`, which uses the local Chrome and Node's built-in DevTools
WebSocket, with no downloaded dependencies. Write tests may be enabled only against a
separate test-records directory; test saves must never be run against the real
review database.

The data adapter layer loads lazily per vehicle and caches at most two vehicles'
data; the first selection of a vehicle usually takes a few seconds, and long segments
may return a large complete-evidence JSON. Raw rows or evidence are never silently
dropped to shorten loading time.

Local acceptance on 2026-09-08: 375 regression tests passed across the original
project and the new site, and the ruff / formatting and JS syntax checks passed; 82
interaction checks in a standalone Chrome browser passed, with no external requests,
console errors, runtime errors or HTTP errors on the page. They covered bidirectional
selection across the three charts, long-segment browsing, the four context levels,
unsaved-note protection, save / refresh recovery and mobile layout; the online tile
service was not requested in the automated tests. Verification records:
[test report](../../artifacts/merge-review-website-20260908/verification-tests.xml),
[browser check](../../artifacts/merge-review-website-20260908/browser-final01/browser-results.json),
[interface screenshot](../../artifacts/merge-review-website-20260908/browser-final01/desktop-1440.png).

Implementation entry points: [server.py](server.py), [data.py](data.py),
[store.py](store.py). The merge method and its limitations for this iteration are
described in the
[direct-evidence layer optimization report](../../docs/apc-avl-direct-evidence-optimization-20260908.md).
