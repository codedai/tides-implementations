# Experiment 05 diagnostic schema contract, version 1

This describes the public diagnostic interface; TIDES core schemas are pinned
separately in `schemas/experiment05/tides-v1/`. The source-preserving tables are
shared by all modes. Diagnostic fields must not be copied into TIDES core.

## Encoding and units

Each Parquet file has a matching `*.schema.json` with schema identifier
`experiment05-table-v1`, row count, physical column types, JSON column names and
the exact Parquet SHA-256. Read nested values with `tools.experiment05.io.read_rows`;
their physical Parquet encoding is UTF-8 JSON, not Python representations.

All absolute numeric times (`t`, `arrival`, `departure`, `*_time`, segment
boundaries) are UTC Unix seconds, with fractional seconds allowed. Duration and
delay fields ending in `_s` are seconds; canonical progress and projection
distances ending in `_m` are metres. GTFS scheduled start/end service-seconds
are elapsed service-time values and may exceed 86,400; they are not Unix times.
Latitude/longitude are genuine source WGS84 degrees unless explicitly prefixed
`projected_`. `heading`/`heading_error_deg` are degrees; `speed_kph` is km/h.

Identifiers are strings, including IDs that look numeric. `service_date` is
`YYYY-MM-DD`. Missing values use actual nulls. A field whose entire column is
missing can have physical type `Null` rather than a non-null primitive type;
consumers must follow the logical nullable type described here, not treat that
as a new semantic schema. Added diagnostic columns are permitted in v1;
changing the meaning, grain, key or units of existing fields requires a new
contract version.

## Required public tables

| Table and grain | Key and principal logical fields |
| --- | --- |
| `fused_projected_observations`: one source row | `rid` unique string; `source` AVL/APC discriminator; `source_row` original 1-based CSV row; `source_file_sha256`, `raw_payload_sha256`; `raw_payload` object; `vehicle_id`, nullable `device_id`; nullable UTC `t`; `service_date_hypotheses` array; nullable `trip_id_performed` |
| `performed_trip_segments`: one inferred segment | `trip_id_performed` unique string; `service_date`, `route_id`, `direction_id`, `gtfs_feed_version`, `vehicle_id`; raw/effective start/end times; nullable plateau onset and censor decision times; `censored_tail_duration_s`; nullable `censor_reason`; `platform_states` and `diagnostic_flags` arrays |
| `candidate_rankings`: one candidate per segment/mode | `(trip_id_performed, match_mode, candidate_rank)` unique; ranks contiguous from 1; `candidate_id`, `trip_id_scheduled`; `score` finite number, lower is better; `ordering_tuple=[score,candidate_id]`; `hard_constraints_valid` boolean; `hard_constraint_reasons` array; score components and model diagnostics |
| `assignment_audit`: one proposal or diagnostic event | Group fields plus `trip_id_performed`, `match_mode`, `assignment_round`, candidate identity/rank where applicable; ordering bounds, conflicts and explicit reasons. This is an event table, not one row per performed trip. Rank 4+ audit events are not assignment proposals. |
| `assignments`: one outcome per segment/mode | `(trip_id_performed, match_mode)` unique; nullable `trip_id_scheduled`, rank, round and match quality; `assignment_status`; nullable primary `unmatched_reason`; complete nonexclusive `unmatched_reasons` array |

For candidates and assignments, `route_id` is the active feed's GTFS route ID.
The public selector value `29` and its GTFS route ID are both stored in the run
manifest. `direction_id="UNKNOWN"` is diagnostic only and cannot satisfy a
scheduled candidate direction constraint; the TIDES equivalent is null.

Projection fields on AVL include nullable `reference_id`, `family_id`, `s_m`,
`fragment_id`, `projection_distance_m`, `heading` and `quality`;
`projection_status`, `trajectory_included`, `exclude_from_matching`,
`censored_tail`, `duplicate_of` and `rejection_reason` distinguish the reasons
an observation does or does not contribute. An OFF_REFERENCE row is retained
but is not a valid trajectory point. Unless independently censored, it remains
in the projection-coverage denominator. Duplicate rows retain their own key.

APC has nullable raw-normalized `arrival`/`departure`, `boarding`/`alighting`,
`canonical_stop_intervals` and interval confidence. `interval_start`/
`interval_end` can be clipped to an effective boundary for evidence only;
`decision_interval_clip` records that operation. The raw payload is unchanged.
`apc_evidence_included`, `apc_attachment_reason` and candidate attachment
diagnostics retain ambiguity instead of silently selecting a nearest-time row.

## Supporting evidence and manifests

`platform_diagnostics.parquet` stores non-overlapping window/run movement,
alternative-reference checks, terminal/APC/recovery evidence, counterfactual
candidate support and censor vetoes. `stop_visits_evidence.parquet` links inferred
visits to source APC row IDs. `apc_attachment_audit.parquet` preserves attachment
choices/rejections. `local_window_evidence.parquet` contains per-mode local
candidate evidence; overlapping windows do not become independent assignment
votes. `mode_results.parquet` retains the matcher's status separately from the
project-specific three-round assignment outcome.

`scheduled_lookup.parquet` is the authoritative active-feed scheduled occurrence
index used by assignment. `gtfs/canonical.json` owns the common route reference
and scheduled geometry so candidate rows need not duplicate it.

`run_manifest.json` binds the pipeline/code/config/environment, input/feed hashes,
selected date/route/modes, stage order, stage row counts and outputs, execution
timestamps and validation. `summary.json` is the lightweight browser-facing
result. `validation_report.json` reports independent pipeline invariants; each
mode's TIDES package has its own validator report. Only successful, hash-verified
runs are reusable caches.
