# Experiment 05 visual inspection

The local batch workbench now includes a read-only visualization layer. Run it
from `agencies/ttc`:

```bash
./experiment05.sh web --port 8767
```

Open <http://127.0.0.1:8767>. Select an available full date, route `29`, and one or
all four modes; start processing or open an existing successful run. Results
include visual inspection, the existing summary, validation reports and downloads.
Opening a saved run does not re-run ingestion, scoring, assignment or export.

## What the views mean

- **Trip detail:** select a vehicle, direction or search text, then use the trip
  selector or previous/next buttons. Raw and effective boundaries are separate.
  The geographic map uses real longitude/latitude and a local metre projection;
  it has north orientation, scale, zoom and pan, but no external street basemap.
- **AVL/APC/GTFS timeline:** AVL timestamps remain individually available; APC
  is an arrival–departure interval. GTFS stop passages and dwells use the saved,
  original scheduled clock. They are never shifted by estimated delay to make a
  candidate look closer. The time window and horizontal pan control synchronize
  the map, timeline and progress plots.
- **Time–distance:** separate canonical references have separate panels. Valid
  AVL fragments are drawn only on their own reference axis; APC is stop-position
  interval evidence, not an invented vehicle GPS ping. Invalid, censored and
  excluded records remain available on the map/timeline and in diagnostic
  downloads, but are not connected into a valid service trajectory.
- **Candidates:** up to three saved ranks can be toggled independently. The
  assigned candidate is identified from `assignments.parquet`, even when it is
  rank 2 or 3. Others are alternatives/diagnostics, including hard-constraint
  failures. Costs, components, gates and reasons are saved values, not a second
  score calculation. Lower cost is better; assignment is not calibrated identity
  confirmation.
- **Full-day overview:** one panel per direction and canonical reference;
  click a trip to open detail. One schedule per trip is displayed: the assigned
  occurrence, or an explicitly diagnostic rank-1 fallback when unassigned.
  A trip can appear on multiple reference panels; that is not an additional trip.
  There is a separate full-day window/pan control.
- **Four modes:** `global`, `early`, `middle`, `late` retain their stored 04c/05
  definitions. Partial modes select thirds of valid projected travel distance,
  not equal thirds of elapsed wall-clock time and not causal online forecasts.
  Highlighting reads saved `selection.time_ranges`; changing the display mode
  does not recompute or change any assignment. Modes not published in a run are
  disabled rather than synthesized.

Trip view windows offer full extent and 5, 15, 30, 60 or 120 minutes; the full-day
view offers full extent and 30, 60, 120 or 240 minutes. The complete available
extent includes candidate schedules, which may start earlier or end later than
the observed trip. The shown timestamps make this distinction visible.

## Display integrity and limitations

The reader uses only files listed with SHA-256 hashes in the successful run's
manifest. It verifies touched files before serving data, rechecks changes even
for cached views, rejects symlinks and reports missing/changed evidence. A saved
assignment missing its exact saved top-three candidate is an integrity error,
not an invitation to silently replace it by rank 1. Run IDs and trip IDs are
identifiers, not arbitrary file paths. Existing local-only HTTP origin/host
guards also apply to these GET endpoints.

Trip detail preserves all route-eligible source observations associated with the
segment, including exact-copy AVL, OFF_REFERENCE and censored rows. Duplicate
copies do not create extra valid service evidence or false breaks. Plotting also
breaks at changes of reference/fragment, persisted restart/separator flags,
excluded observations and gaps greater than the **run's pinned**
`matcher.continuous_coverage_max_gap_s` (90 seconds in the reviewed runs). This is
conservative display connectivity, not a change to scoring or segmentation.

Only the day view may simplify a valid fragment to at most 160 display points,
deterministically retaining endpoints. It reports original/display counts and
never uses these simplified coordinates to calculate a metric. Shared pre-mode
data is read once per cached run, not exported four times. The in-memory cache is
bounded to two runs; no new large visualization datasets are persisted.

APC map symbols use the mapped GTFS stop location, explicitly labeled
`STOP_LOCATION_NOT_VEHICLE_PING`. Candidate map geometry is the canonical
reference cropped to its scheduled progress range, not a claim to display the
original candidate's unmodified GTFS shape. Time–distance comparisons stay on
that same reference; references cannot be treated as one interchangeable metre
axis. The day view does not visualize unprojectable trips without any candidate
as a distance curve; they remain accessible in the trip selector and diagnostics.

Unattached APC has no performed-trip identity and therefore is not placed inside
an arbitrary trip detail. The catalog reports its count (263 rows in the latest
`2023-11-06` run); individual unattached records remain in the shared observation
and attachment-audit downloads. The current trip viewer is not a separate
row-by-row explorer for orphan APC.

The validated run is historical evidence: opening it with newer visualization
code does not retroactively upgrade its pipeline version, APC policy or
calibration. For example, the reviewed `2023-11-20` run used the conservative
passenger-event omission policy, while the latest `2023-11-06` run uses the
[explicit departure-proxy policy](experiment05-apc-events.md).

## Read-only API, version 1

All responses have `schema="experiment05-visualization-v1"`, `run_id`, full
`service_date`, `route_id`, `timezone`, `match_mode`, published `match_modes`,
`published_pipeline_version`, `read_only=true`, and `scoring_recomputed=false`.

| GET endpoint | Main payload |
| --- | --- |
| `/api/runs/{run_id}/visualization/catalog?mode={mode}` | Sorted performed segments, source/projection counts, raw/effective extents, assignment, unmatched reason, available directions. |
| `/api/runs/{run_id}/visualization/trip?mode={mode}&trip_id={encoded_id}` | AVL/APC source rows, visits, references, continuous fragments, saved mode selection, top-three schedules and scores, assignment and diagnostics. |
| `/api/runs/{run_id}/visualization/overview?mode={mode}` | Per-reference/direction facets, valid display fragments, one assigned/fallback schedule, original/display point counts and bounds. |

Numeric absolute times are UTC Unix seconds, distances are canonical metres and
coordinates are WGS84 degrees. The browser formats times in the pinned run
timezone and shows full dates when outside the selected date. Scheduled movement
vertices come directly from stop arrivals/departures, without fixed-grid
resampling. Nonzero dwell retains both timestamps at the same progress.

Unknown/missing/duplicated query parameters return 400; missing runs, trips or
uncomputed modes return 404. Failed/unpublished runs, changed hashes and
assignment inconsistencies cannot be served as validated visualization results.
Large per-candidate delay arrays and local-window matrices are not loaded for a
day overview; full audit data remains downloadable.

Implementation: [read model](../tools/experiment05/visualization.py),
[HTTP adapter](../tools/experiment05/web.py),
[browser views](../tools/experiment05/static05/viz.js).
Focused tests: [reader/HTTP](../tests/test_experiment05_visualization.py),
[browser math](../tests/test_experiment05_viz_math.mjs).
See the [TIDES evidence-boundary audit](experiment05-tides-compliance.md) before
interpreting validation or a visually close scheduled curve as established truth.
