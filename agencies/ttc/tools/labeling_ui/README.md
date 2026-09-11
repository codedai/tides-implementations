# scheduled_trip labeling UI

A local, offline, inspection-first Streamlit app for the human labeling pass
over r1-spike `scheduled_trip` review cases (see
`docs/r1-spike02r-kernel-repair-findings.md` and the run's
`labeling_packet_redacted.csv`).

It is a research labeling tool, not a product: the goal is a defensible
human judgment per case, with bias controls, raw-evidence panels, and an
auditable label trail.

## Quick start

```sh
cd agencies/ttc/tools/labeling_ui
uv sync                      # or: python -m venv .venv && .venv/bin/pip install -r requirements.txt
uv run streamlit run app.py
```

With this repo's layout, no arguments are needed — the app finds the latest
labeling packet under `agencies/ttc/artifacts/`. Explicit paths:

```sh
uv run streamlit run app.py -- \
  --cases ../../artifacts/experimental-r1-spike02r-p4/labeling_packet_redacted.csv \
  --artifacts ../../artifacts \
  --labels-dir labels \
  --reviewer jw
```

Environment variables work too (useful for headless testing):
`LABELING_UI_CASES`, `LABELING_UI_ARTIFACTS`, `LABELING_UI_LABELS_DIR`,
`LABELING_UI_REVIEWER`.

Run the tests (36, including a full render harness over the 18 real cases
when the artifacts are present):

```sh
uv run pytest
```

## What the reviewer sees

1. **Sidebar** — case list with label status, filters (labeled/unlabeled,
   stratum, kind, duration bucket, text search), sorts (original / shortest /
   longest / stratum), prev/next, progress bar.
2. **Header** — case facts (stratum, kind, duration, n_events) plus a context
   strip: vehicle, local start/end, AVL ping count, APC events nearby,
   candidate count. The case summary is shown with the model verdict masked
   (see bias controls).
3. **Map** — chunk AVL trace colored by time order with start/end markers,
   previous/next chunk traces, candidate route shapes, scheduled stops,
   terminal stars, APC events; all toggleable. Offline plain lat/lon plot
   with locked aspect ratio (no tiles fetched).
4. **Timeline** — prev/current/next chunk bars, gap annotations, candidate
   scheduled windows, APC tick marks.
5. **Time–distance** — AVL progress projected onto a selectable candidate's
   shape vs time, with each candidate's scheduled stop trajectory and APC
   events; falls back to cumulative distance when no shape exists.
6. **Stop ladder** — per candidate: scheduled stops in order vs scheduled
   time, observed AVL pass times (nearest ping ≤ 150 m), APC hits by stop
   code, missed stops marked, chunk window shaded.
7. **Candidate cards** — up to 5 anonymous cards (A, B, C…) plus a Null
   card, each with raw evidence only: scheduled window, duration vs chunk,
   start/end residuals, overlap, stop coverage, distance-to-shape, direction
   consistency, APC hits, and derived tags (`fragment-like`,
   `boundary-near`, `terminal-near`, `low stop coverage`, …).
8. **Decision form** — primary label, candidate picker for "other",
   confidence 1–5, optional fragment impression, rationale checkboxes,
   notes, revisit flag; drafts autosave on every change.
9. **Diagnostics** (collapsed, explicit reveal) — anchor status/margins, the
   model's chosen key and which card it is, qscores/regimes/censor classes,
   the full candidate list, run-level reports, raw case JSON.

Keyboard shortcuts (ignored while typing): `j`/`n` next, `k`/`p` prev,
`s` save, `x` save + next, `a` ambiguous + next, `i` insufficient + next.
They click the corresponding buttons via a small JS bridge; if the browser
blocks it, the buttons still work by mouse.

## Bias controls

- Candidates are shown as **anonymous cards lettered by scheduled start
  time** (then key) — never by model score.
- The model's verdict is hidden by default everywhere: the summary's
  "anchor status …" is masked in the header, and qscores/margins/status live
  only behind the diagnostics reveal.
- The reviewer never chooses "top candidate" explicitly. They pick a card
  (or Null/Ambiguous/Insufficient); the app maps the choice to
  `MATCH_TOP_CANDIDATE` vs `MATCH_OTHER_CANDIDATE` at save time by comparing
  with the model's (hidden) anchor pick.
- Revealing diagnostics is per-case, requires a click, and is persisted: a
  `reveal` event is appended to the JSONL audit log, and every label saved
  afterwards (any session) carries `diagnostics_revealed_before_save=true`,
  so post-hoc analysis can separate blinded from unblinded judgments.
- After saving, the UI shows the reviewer's own choice ("matched card B"),
  never the mapped MATCH_TOP/MATCH_OTHER label — that mapping (which would
  reveal agreement with the model) exists only in the persisted files.
- The sidebar text search runs over the redacted summary, so masked verdict
  words cannot be recovered through the filter box.
- The model's chunk-role classifications (`unresolved_fragment`,
  `terminal_turnaround`, …) never appear in the neutral panels; a test
  scans all serialized figures for the masked vocabulary.
- Residual disclosure: *which 5 candidates get cards* is chosen by model
  qscore (top-K ∪ the anchor's pick) because showing all ~37 keys per case
  is impractical. The full list is reachable via "Match other candidate…"
  in neutral date/route/trip order, so the shortlist never hard-limits the
  reviewer. Treat card membership itself as weak model signal.

## Outputs

Written to `--labels-dir` (default `labels/`, gitignored):

- `scheduled_trip_labels.csv` — one row per (case_id, reviewer), current
  judgment; atomic rewrite per save. Columns include the label, selected
  candidate letter + real candidate key, confidence, fragment impression,
  rationale flags, notes, created/modified timestamps (UTC), session id,
  app version, chunk id, diagnostics-reveal flag, revisit flag.
- `scheduled_trip_labels.jsonl` — append-only audit log, one object per
  save event (including re-saves) plus `reveal` events.
- `drafts.json` — unsaved form state per case/reviewer; a save deletes the
  case's draft so stale drafts never shadow the saved record.

Saves take an exclusive file lock (POSIX `flock`) around the
read-modify-write cycle, so concurrent tabs or reviewers sharing a labels
dir cannot silently lose each other's rows. Artifact caches key on file
mtimes, so regenerated artifacts are picked up without restarting.

## Data the app looks for (and how it degrades)

| Artifact | Used for | If missing |
| --- | --- | --- |
| `labeling_packet_redacted.csv` | case list | required |
| `labeling_packet_restricted.parquet` | case → chunk identity | metadata-only labeling, no panels |
| `x_refined_chunks.parquet` | chunk window, prev/next chunks | window from summary duration; no context chunks |
| `x_anchor_evidence_edges.parquet` | vehicle recovery (primary) | falls back to AVL window matching |
| `diagnostic-*/normalized/avl/` | chunk traces | map/time panels unavailable |
| `diagnostic-*/normalized/apc/` | APC evidence | APC overlays unavailable |
| `x_complete_schedule_candidates.parquet` | candidate set | no cards; labeling still possible |
| `x_anchor_candidates_v2.parquet` | model pick (diagnostics; MATCH_TOP mapping) | all matches recorded as MATCH_OTHER_CANDIDATE |
| `gtfs-<date>-<feed>/` snapshots | schedule windows, stop ladders, shapes | cards show temporal fields as n/a |
| `x_fragment_roles.parquet`, run reports | diagnostics extras | omitted |

Every degradation is surfaced in the header's "degraded evidence" expander
rather than crashing a panel.

## Assumptions made about the artifact schemas

Documented here because the spike's schemas are experimental (`x_*`):

1. **Instant convention.** All `*_instant_s` / `case_epoch_s` values are
   epoch seconds obtained by interpreting naive wall-clock timestamps as
   **America/Toronto** local time (the spike's D-16 "naive wall-time"
   convention). DST fall-back ambiguity resolves to the earlier instant;
   spring-forward gaps become null. Verified against the p4 run: recovered
   per-chunk ping counts match the recorded `n_pings` for all 18 cases.
2. **Chunk identity is not reversible** (`xsg-*` ids are hashes), so the
   vehicle is recovered by: anchor evidence edges → APC `_record_id` →
   `vehicle_id_raw`; falling back to "which vehicle's AVL pings fill the
   chunk window edge-to-edge" (rejected if no vehicle comes within 300 s of
   both edges). The header shows which method was used.
3. **Candidate keys** parse as `service_date|feed_id|route_id|trip_id|polarity`
   with empty fields tolerated (`route only` = date-level candidate).
   GTFS lookups target `gtfs-<service_date>-<feed_id>/{matched,normalized}/gtfs/`.
4. **Scheduled windows** come from `matched/gtfs/scheduled_stop_events.parquet`.
   Datetime columns are converted by dtype: already-tz-aware columns (as in
   the real snapshots) are trusted as-is, naive columns fall back to (1).
   Re-localizing aware columns would silently shift the fall-back night's
   "26:xx" trips an hour early — a regression test guards this. Stop
   coordinates join via `normalized/gtfs/stops.parquet`; geometry via
   `normalized/gtfs/shapes.parquet` and the trip's `shape_id`.
5. **APC events** attach by vehicle + arrival epoch within chunk window
   ± 30 min; "APC hits" on a candidate are events whose `source_stop_code`
   appears among the candidate's scheduled `stop_code`s **and** whose arrival
   falls inside that candidate's scheduled window ± 5 min (otherwise
   same-route candidates would all report identical hits).
6. **DST-crossing cases**: when a case's plotted range spans a UTC-offset
   change (the 2023-11-05 fall-back repeats the 1 AM wall hour), all time
   axes switch to true elapsed minutes since chunk start, with local times
   (incl. EDT/EST tag) in hover text — a wall-clock axis would shrink bars
   by an hour and render one-hour gaps as seconds.
7. **Derived metrics thresholds** (150 m stop coverage, 250 m terminal-near,
   120 s boundary-near, 300 s APC window pad, 10 m direction jitter floor)
   are UI heuristics for evidence display only — they are not the spike's
   calibrated thresholds (D-14 remains open) and are constants in
   `labeling/evidence.py`.

## Code layout

```
app.py                  # streamlit UI only
labeling/parsing.py     # pure parsing/time helpers
labeling/adapters.py    # artifact discovery + tolerant loaders (all cached)
labeling/resolver.py    # case_id -> CaseBundle (chunk, vehicle, pings, candidates)
labeling/evidence.py    # neutral per-candidate metrics + tags
labeling/geometry.py    # planar projection / polyline math
labeling/plots.py       # the four evidence figures
labeling/store.py       # CSV + JSONL label persistence, drafts, audit
labeling/keyboard.py    # JS shortcut bridge
tests/                  # unit tests + real-artifact render harness + AppTest flow
```

Known environment quirk: `st.dataframe`'s arrow serialization segfaults
under this machine's polars/pyarrow/protobuf combination, so tables render
as markdown (`show_table` in `app.py`) and pandas is pinned `<3`.
