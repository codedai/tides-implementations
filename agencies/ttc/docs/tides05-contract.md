# Experiment 05 TIDES research export

This is an inferred research package, not calibrated operational certification.
The explicitly requested Experiment 05 one-to-one assignment populates
`trips_performed.trip_id_scheduled` without applying Experiment 04c's confidence
thresholds. An unmatched assignment is a true null, not proof of an operational
`Unscheduled`, canceled, added or deadhead trip. Other experiments are unchanged.

## API and input contract

`tools.experiment05.tides.publish_tides(output_dir, mode, segments, observations,
stop_visits, assignments, gtfs_bundle, config=None, *, run_binding=None)` returns
`{path, validation, summary}`. `output_dir` must not already exist. Modes are
`global`, `early`, `middle`, and `late`. `build_tides_rows` is the pure mapping API;
`validate_tides_rows` independently checks core rows and references.
`output_dir` is the exact package directory; the exporter never appends `mode`.
After the pipeline verifies same-run input/code/config/prerequisite pins,
`read_completed_tides(output_dir, mode, *, expected_binding=None)` validates every
completed package file hash and returns the same report shape without rewriting.
Production supplies the same lowercase SHA-256 run binding to both APIs, derived
from run identity, configuration, mode and upstream artifact pins. The binding is
stored in `completion.json`; a mismatched or missing binding rejects cross-run
package reuse even when the copied package is independently valid. `None` is only
for standalone export/inspection, not production resumption.

Inputs are lists of dictionaries (assignments may instead be keyed by mode):

- Segments: `trip_id_performed`, explicit `service_date` (`YYYY-MM-DD`), string
  `vehicle_id`, `route_id`, `direction_id`, and `gtfs_feed_version`. Raw/effective
  envelopes stay in diagnostics. Only separately provided `actual_trip_start`
  and `actual_trip_end` populate operational endpoints; envelopes are not copied.
- Observations: unique string `rid`, `source`, source row/file SHA-256 lineage,
  string `vehicle_id`, nullable `device_id`, `t`, genuine `lat`/`lon`, `heading`,
  `speed_kph`, optional performed-trip link, and projection diagnostics. Missing
  time is explicitly omitted, not repaired. OFF_REFERENCE AVL is retained.
- Visits: unique `stop_visit_id`, performed-trip/service-date key, vehicle,
  feed-scoped `stop_id`, actual `trip_stop_sequence` starting at 1 and consecutive,
  `arrival_t`, `departure_t`, and `source_rids` resolving to input observations.
  One source observation cannot be attributed to multiple visits. Optional
  `scheduled_stop_sequence` must resolve uniquely in the assigned occurrence;
  the exporter does not infer scheduled sequence from a nearest stop.
- Assignments: `trip_id_performed`, `match_mode`, nullable `trip_id_scheduled`;
  optional service date/feed override must agree with the active bundle. No
  scheduled occurrence may be assigned twice within a mode.
- Bundle: the existing GTFS feed bundle with `feed_version`, active `trips`,
  `stops`, and each trip's ordered `stop_times`. Arrival/departure timestamps must
  already be UTC seconds. Supplied route, direction, shape and block associations
  are checked against the selected occurrence.

All numeric timestamps are **UTC epoch seconds**, never naive wall-clock epoch
values. The caller resolves Toronto timezone/DST hypotheses and GTFS extended
hours before calling. The exporter never chooses a fold or guesses service date.
Identifiers stay strings; numeric identifiers are rejected rather than coerced.

## Resources and missing evidence

The package writes `vehicles`, `trips_performed`, `vehicle_locations`, and
`stop_visits`; `devices` is included when real device IDs exist. All core columns
come from the vendored schemas; unknown/missing core columns fail validation.
CSV nulls are empty cells, JSON nulls are `null`, and zero is preserved as zero.
Speed is converted from km/h to m/s. Heading and GPS are not recomputed.

`config.export.include_devices=false` omits the devices resource and its core
foreign-key values; original device IDs remain in lineage with an omission reason.
The internal direction label `UNKNOWN` is null in the optional TIDES direction
field, while diagnostics preserve the original label. Source timezone metadata
comes from `config.normalization.timezone`; event timestamps remain UTC.

APC stop coordinates are not vehicle pings. An APC record can enter
`vehicle_locations` only with explicit `genuine_vehicle_coordinates=true`.
Unverified load and door allocation remain null: whole-vehicle boarding counts
are not put in `boarding_1`. Non-integer dwell is left null with a reason rather
than rounded. Inverted intervals fail.

The production config selects the user-approved research assumption
`passenger_event_policy="apc_stop_interval_departure_proxy_v1"`: one valid,
attached APC source row represents one stop visit and its counts are visit totals.
Vehicle-arrival/departure events use the respective original timestamps (count 1).
Positive integer boarding/alighting totals each form one event at the original
departure time. Both passenger timestamps are **documented proxies**, not measured
individual passenger instants. Per-event diagnostics retain source IDs, the original
interval, policy and assumed/proxy semantics; no extra core columns, arbitrary door,
or nearest-ping linkage is invented. Zero passenger activity creates no count event;
missing counts are not zero. `AvgOTP` remains unused. This export-only convention
does not change projection, segmentation, interval evidence or candidate scoring.

The previous `omit_without_event_semantics` policy remains available and is the
standalone API fallback when no policy is supplied. Under that policy a visit must
explicitly provide `passenger_event_semantics_valid=true`, a valid count and
`boarding_event_t` / `alighting_event_t` within its stop interval. The new policy
does not claim that source grain has been independently verified; original evidence
flags are preserved. See [APC assumptions and stop-visit construction](experiment05-apc-events.md)
for the complete versioned mapping, limitations, example and audit requirements.

## Fixed standard, offline validation and provenance

The authoritative reference is
[TIDES v1.0](https://tides-transit.org/v1.0/tables/), pinned to upstream commit
`b2cb23178e00cb58dbdb1b9a5a8626818278ecbb`. Original schemas, package profile,
Table Schema reference, CC-BY-4.0 attribution and checksums are vendored in
`schemas/experiment05/tides-v1/`. Each package also includes this material under
`schemas/`; temporary upstream checkouts are not runtime dependencies.

Validation directly checks the local Draft-07 package profile, Frictionless
schema reports, exact written CSVs, primary/composite keys, vehicle/device/visit
and GTFS references, one-to-one assignments, consecutive actual visits and dwell
consistency. No schema download occurs: the descriptor's pinned upstream profile
URL is normative attribution, while validation loads its included local copy.
The upstream shell validator's exit code is not used as a conformance verdict;
its cleanup/error handling can mask a failed validation.

`diagnostics.json` contains inference status, source lineage, observation versus
operational envelopes, and omitted-event reasons; it is not a declared TIDES
resource. `validation_report.json`, `summary.json`, and `completion.json` record checks
and hashes. CSV and schema files are deterministic under fixed inputs. Output
is privately staged and renamed only after success; existing outputs are never
overwritten. Failed staging directories, if any, remain explicitly incomplete.
Passing structural checks does not independently verify inferred trip boundaries,
APC source grain, or schedule-assignment accuracy.
