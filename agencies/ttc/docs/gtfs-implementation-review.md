# GTFS implementation review

## Review outcome

The GTFS preprocessing stage is fit for the current offline proof of concept.
It deterministically inventories and normalizes a feed, selects a reviewed feed
for a service date, derives scheduled trips, stop events, and stop patterns,
and emits quality evidence without requiring SQL, a database, or a network
service.

This conclusion is deliberately limited to the GTFS stage. The reviewed code
does not ingest AVL/APC data or construct and validate a TIDES package.

## Integrity changes made during review

| Area | Review finding | Implemented resolution |
| --- | --- | --- |
| GTFS structure | `calendar.txt` was treated as mandatory even though a feed may define all service through `calendar_dates.txt`. | Require at least one of the two calendar members and support exception-only service expansion. |
| Headers and values | Required columns and required values shared one flag, which could not express GTFS's conditionally nullable time fields. | Separate header presence from per-row value requirements; `arrival_time` and `departure_time` headers are mandatory while permitted row-level blanks remain nullable. |
| Numeric parsing | IEEE `NaN` and infinity survived float parsing. | Quarantine all non-finite parsed floats as `GTFS_INVALID_FLOAT`. |
| Primary keys | Keeping the first duplicate made validity depend on ZIP row order. | Quarantine every row with a duplicated complete source key. Exact keys are checked after a compact hash prefilter; hash collisions cannot create false duplicates. |
| Feed approval | Draft `candidate` rows could be selected like reviewed feeds. | Select only `approved` rows by default. `--allow-candidate` is an explicit investigation-only override. |
| Manifest trust | Statuses, hashes, ranges, and paths were weakly validated. | Validate required values, SHA-256/content hashes, statuses, duplicate identities, paired calendar bounds, and relative paths; prevent symlink/path traversal outside the GTFS root. |
| Feed pinning | A source archive could change after manifest creation. | Recompute and compare the exact archive SHA-256 before coverage or processing. |
| Feed-selection cost | Selecting one date expanded every manifest archive. | Filter by status and manifest service bounds first, then expand only feeds capable of covering the requested date. |
| Pattern identity | Delimiter-concatenated keys could collide and version-specific `route_id` made otherwise stable patterns change across feed revisions. | Introduce `p2` identifiers using length-prefixed components and a public route identity, direction, and ordered stop path. Hash only distinct pattern keys. |
| Quality coverage | Several schedule-integrity failures were invisible. | Add warnings for trips without stop times, missing referenced shapes files, negative stop sequences, arrival before the previous stop's departure, and independently invalid latitude/longitude. |
| Archive reads | Large ZIP members were first copied into a Python bytes object and record IDs were hashed row by row in Python. | Read directly from the ZIP decompression stream and create deterministic row IDs vectorially from one member namespace. |
| Memory use | Repeated lineage strings, unconditional string stripping, all-row reason strings, and wide duplicate aggregation inflated memory. | Dictionary-encode constant lineage, clean only columns that need it, build reason strings only for quarantined rows, and prefilter duplicate keys with a compact hash. |
| Output safety | Generated artifacts could be written under immutable GTFS inputs, and forced directory runs could retain stale files. | Reject outputs inside the GTFS source boundary and replace an explicitly forced output directory as one unit. Fatal quality results do not produce matched artifacts. |

## Verification evidence

The ordinary suite is synthetic and redistributable. It contains 92 tests
covering archive identity, malformed ZIPs and headers, typed normalization,
quarantine conservation, calendar exceptions and exception-only feeds,
overlapping feed precedence, draft approval behavior, DST/post-midnight
times, stable collision-safe patterns, relational quality checks, CLI safety,
and deterministic reruns.

The following checks pass:

```shell
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
```

An opt-in acceptance run was also performed against the local TTC archive
selected for 2023-11-05 by the current draft manifest:

| Measure | Observed value |
| --- | ---: |
| Selected feed | `ttc-d092742e6a5b` |
| Normalized `stop_times` rows | 3,230,182 |
| Scheduled trips | 28,291 |
| Scheduled stop events | 939,706 |
| Feed-wide stop patterns | 1,208 |
| Quarantined rows | 0 |
| Fatal findings | 0 |
| Warnings | 1 service used by trips but never active |
| Date-scoped selection | 0.19 s, about 81 MB peak RSS |
| Full process after optimization | 3.05 s, about 2.85 GB peak RSS |

The efficiency checkpoint before the normalization refactor took 11.0
seconds and about 3.41 GB peak RSS on the same machine and input. All eight
normalized and five matched tables were compared logically before and after
the optimization; every table was equal after excluding the deliberately
run-specific `_ingest_run_id` value and its dictionary encoding.

These timings are diagnostic measurements, not cross-machine performance
guarantees.

## Remaining prototype limits

- The checked-in artifact is a draft manifest. Its priorities are hypotheses
  derived from ZIP member timestamps; a data owner must confirm provenance and
  promote intended rows to `approved` before routine use.
- Normalization is eager and processes a complete selected feed in memory. The
  measured 2.85 GB process peak is workable for the current 3.2-million-row
  feed, but larger feeds may require batch/lazy normalization and partitioned
  writes.
- Landing data currently exists in memory rather than as a separately persisted
  `landing/` artifact. Normalized rows and quarantined rows retain lineage, but
  a durable raw landing layer remains future work.
- The quality policy reports referential and plausibility issues as warnings.
  Promotion of particular warning codes to fatal must be a reviewed policy
  decision rather than an implicit algorithm change.
- The implementation covers the TTC schedule members needed by this prototype,
  not every optional GTFS Schedule table or every conditional field rule.
- `p2` pattern stability depends on stable public agency/route names. A real
  public route rename intentionally creates a new route identity even if its
  stop path is unchanged.
- Run manifests do not yet capture code/dependency versions, stage timings, and
  output hashes. That remains necessary for a complete reproducibility record.
- No real TTC extract is committed to the test suite; real-data acceptance is
  opt-in because redistribution permission is not established.

## Recommended next gate

Before GTFS is used in AVL/APC reconciliation, review and commit an approved
`config/feed_manifest.csv`, rerun the date boundaries around each November
handover, and decide which GTFS warnings should block downstream matching.
Only then should route/stop crosswalk and scheduled-trip matching work treat
the selected feed as authoritative.
