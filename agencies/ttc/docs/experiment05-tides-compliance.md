# Experiment 05: TIDES compliance and evidence-boundary audit

Audit date: 2026-09-10. Scope: route 29, 2023-11-06, completed run
`run-2023-11-06-a39c5491eae9f48b`, four decision modes; business version
`experiment-05-v1+663770f8d53984d6`. This was a read-only inspection of raw
records, code and published results; it did not modify the algorithm, the APC
interpretation policy or historical outputs, and it did not re-adjudicate real
trip labels.

## Conclusion

The current results pass this project's structural, reference, assignment and
source-consistency checks against the pinned TIDES v1.0, and can be described as
"research output organized according to TIDES v1.0, with explicitly stated
inference assumptions". **They cannot be described as official TIDES
certification, independently confirmed operational fact, or proof of legal
compliance.** No schema, primary-key or checked foreign-key failures were found
in the actual outputs of this run; the following important semantic limitations
do apply.

Priority indicates the order of follow-up work; it does not mean the source data
has been proven wrong:

| Priority | Finding | Impact and boundary |
|---|---|---|
| P1 | A civil day is not a complete operating day | The GTFS past-midnight tail of the previous operating day is not currently retrieved; early-morning attribution remains unresolved. |
| P1 | One APC row per stop visit, departure-time proxy for passenger events | A user-approved experimental assumption, not a vendor-confirmed definition; an identical interval at a terminal may correspond to two rows from a direction handover. |
| P1 | One-to-one scheduled assignment with no score threshold | An Experiment 05 constraint, not a TIDES one-to-one requirement, and not a calibrated identity confirmation. |
| P2 | Stop-visit coverage, scheduled stop sequence and real endpoints are incomplete | Leaving optional fields empty is valid, but complete stop visits, on-time performance or actual end-to-end trip duration cannot be computed from them. |
| Pre-release | Authorization to use and redistribute the raw data is not established by the evidence in this audit | Source-code/spec licenses do not substitute for a data license; this must not be inverted into a claim that the data is unlawful. |

## Verified structure and references

The official v1.0 pinned commit `b2cb23178e00cb58dbdb1b9a5a8626818278ecbb` is
used. The SHA256 of all nine local files (six table schemas, the package
profile, the table-schema reference and the license) match a clean upstream
checkout of the same commit; every package carries these files together with the
[vendor record](../schemas/experiment05/tides-v1/vendor.json). Online
cross-checks use the
[official v1.0 table definitions](https://tides-transit.org/v1.0/tables/) and the
[official architecture](https://tides-transit.org/v1.0/architecture/), without
mixing in later versions from main.

Traceable re-verification completed:

- Original run, stage 10: 1,483 checks passed, including six passenger-proxy
  policy integrity checks per mode across the four modes.
- This independent re-verification: **50/50 PASS**, re-validating the actual
  CSVs, the hashes of 2,232 published files, the raw inputs and row-level
  identity, the GTFS version/date/route, and the assignments for all four modes;
  under three random shufflings of input order, the assignments, audits and
  summaries all remained identical. See the
  [independent report](../outputs/experiment_05/acceptance/visualization-review/tides-compliance-independent.json).
- Additionally, `validate_proxy_event_mode` was replayed directly:
  **24/24 PASS** across the four modes; expectations were constructed
  independently from APC visits, source lineage and assignments and checked
  against the actual event CSVs and provenance, without calling the exporter to
  generate the "expected results". See
  [validation.py:16](../tools/experiment05/validation.py#L16).

Frictionless checks required fields, types, enums, non-negative counts and
unique keys; this project additionally checks composite trip/visit references,
vehicle/device/ping references, GTFS active occurrences and
route/direction/shape/block, and verifies that actual stop sequences are
numbered consecutively from 1. The `foreignReferences` in the schema must not be
taken to mean that generic Frictionless has automatically completed all
business-level relationship checks. See
[tides.py:616](../tools/experiment05/tides.py#L616).

Unknown values are written as empty CSV cells (the schema's missing value), not
as the strings `None`/`null`, and 0 is never used to stand in for missing. UTC
datetimes are kept separate from `service_date`; pings, doors and real endpoints
without source evidence are not fabricated. `devices` is not emitted, and all
core device IDs are left empty to avoid dangling references.

GTFS for this run: feed `ttc-d092742e6a5b`, valid 2023-10-30 through
2023-11-18; public route `29` corresponds to GTFS `route_id=70291`. Each of the
four packages contains 31 vehicles, 352 trips_performed, 112,002
vehicle_locations, 13,599 stop_visits and 38,211 passenger_events. The
global/early/middle/late modes assign 244/246/244/227 scheduled occurrences
respectively; these are four alternative experimental results and must not be
concatenated and treated as additional trips that occurred on the same day. See
the [actual run summary](../outputs/experiment_05/runs/run-2023-11-06-a39c5491eae9f48b/summary.json).

## Semantic review: passing validation does not mean actual events are confirmed

### 1. APC events and stop_visits

TIDES passenger_events covers events such as vehicle arrival/departure at stops
and boardings/alightings; `event_timestamp` is required and described as the
recorded time of the event, and `event_count` explicitly may represent multiple
boardings in a single record. It does not require one row per passenger, but
neither does it prescribe setting the aggregate boarding/alighting time to the
departure instant. See the
[pinned passenger_events schema](../schemas/experiment05/tides-v1/passenger_events.schema.json).

The current policy `apc_stop_interval_departure_proxy_v1` treats one valid,
uniquely attached APC row as one stop visit: arrival/departure events use the
two original APC endpoint times, and aggregate boarding/alighting are both
booked at the departure instant; they are **not per-passenger events measured at
that second**. Source IDs, the original interval, the proxy time and
`semantics_assumed` are preserved in each package's diagnostics; taking only the
CSV loses these precision limitations. For exchange, therefore, the
[mapping convention](experiment05-apc-events.md) and the diagnostics must be
delivered together, and strict measured-event semantics must not be claimed from
the surface CSV fields alone.

Per-mode event breakdown for this run: 13,599 arrivals, 13,599 departures,
5,462 boarding records and 5,551 alighting records; the `event_count` of the
latter two sums to 21,122 and 19,347 respectively. **38,211 is the number of
event rows, not a passenger count.** A zero count generates no passenger event;
missing or invalid counts are not filled with zero. No APC vehicle-location
pings were fabricated.

stop_visits are generated before candidates/assignment: same vehicle, time
interval, canonical stop/reference, a soft direction constraint and available
AVL progress jointly determine unique attribution, after which visits are
ordered and numbered by original arrival, departure and source ID. This neither
fills in every GTFS stop nor infers all actual stop visits solely from AVL
stops. See
[apc.py:32](../tools/experiment05/apc.py#L32) and
[apc.py:172](../tools/experiment05/apc.py#L172).

Independent inspection of `stop_visits_evidence.parquet` against source rows
found:

- Of 13,862 route 29 APC rows, 13,599 are attached; 67 of the 352 segments have
  no APC visit.
- No attached rows share an identical raw-payload SHA, but 1,026 rows still
  carry `grain_uncertain=True`. All attached rows have `NoTrips=1`, which is
  still insufficient to prove that every row is an independent physical stop.
- Grouping by `(vehicle_id, stop_id, arrival_t, departure_t)` yields **9 groups
  of two rows with an identical interval**, 18 rows in total; the source APC
  directions differ (south/north), all at terminal GTFS stop 10415. Five of
  these groups fall within the same performed segment and four span segments.
  These may be split bookings from a direction handover at the terminal, not
  exact duplicate source rows that can simply be deleted; the current policy
  retains both sets of arrival/departure events. The number of physical stops
  should not be doubled on this basis, nor should the rows be merged or
  passengers declared double-counted before the grain is confirmed.

### 2. Scheduled identity and dates

TIDES explicitly allows **one scheduled trip to correspond to multiple operated
trips**. Experiment 05's three-round, monotone, one-to-one occupancy assignment
with no score threshold is an experimental model; it may leave the other
fragment of a split scheduled occurrence unmatched, and that limitation cannot
be explained away as "TIDES conformance". See
[trips_performed schema:41](../schemas/experiment05/tides-v1/trips_performed.schema.json#L41) and
[assignment.py:1](../tools/experiment05/assignment.py#L1).
An assigned ID in core denotes a model-inferred Scheduled trip; an unassigned
one remains null and is not automatically equivalent to a real Unscheduled
trip. Without independent labels, matching accuracy, probability calibration
and actual delay error remain unknown. `AvgOTP` is retained only as an audit
field and is not a production matching feature in this run.

A GTFS service day may span civil days, and times use the
local-noon-minus-12-hours anchor, allowing values beyond 24:00. See the
[official GTFS time definition](https://gtfs.org/documentation/schedule/reference/#field-types).
This implementation converts to UTC using that anchor; in this run, 28 of the
300 active trips contain times beyond 24:00. The raw extraction, however, is
still a local civil day, and all segments are first assigned the requested date;
the previous-day hypothesis is diagnostic only, with
`feed_include_previous_day_tail=False`. In this run, 7,951 AVL and 1,132 APC
rows for route 29 carry a previous-day hypothesis; this does not mean those rows
have been confirmed as belonging to the previous day, nor that previous-day
last-trip candidates have been covered. Early-morning AVL/APC after the
requested day has also not been fully brought in. Complete operating-day
coverage therefore cannot be claimed. See
[sources.py:87](../tools/experiment05/sources.py#L87),
[sources.py:233](../tools/experiment05/sources.py#L233) and
[sources.py:287](../tools/experiment05/sources.py#L287).

Ambiguous/nonexistent DST times without an offset are flagged and no fold is
guessed; UTC conversion has synthetic tests, but 11-06 in this run is not a
transition day, so the real-data results of this run cannot be used to claim
that real DST scenarios have been fully accepted. See
[sources.py:123](../tools/experiment05/sources.py#L123).

### 3. Optional fields and diagnostic records

In this run, scheduled_stop_sequence and the scheduled arrival/departure times
are empty for all stop_visits: each actual visit has not yet been mapped to a
specific stop occurrence of the assigned GTFS trip. Leaving optional fields
empty does not cause schema failure, but per-stop OTP currently cannot be
computed directly from that CSV. Per-door passenger counts and departure_load
are likewise all empty; total boarding/alighting is not forced into an unknown
door. Totals can be aggregated from passenger_events by type and visit key. See
[tides.py:424](../tools/experiment05/tides.py#L424).

actual_trip_start/end are empty for all 352 segments: the raw/effective
envelope is not a confirmed origin departure and destination arrival, and real
trip endpoints are not impersonated by projection-window boundaries. The
112,002 AVL location rows retain 1,295 OFF_REFERENCE rows and 19,859 in-route
duplicate diagnostic rows, all of which may still carry a trip link; such a link
is source segment attribution and does not indicate that the point passed
projection, was in revenue service, or belongs to a continuous valid trajectory.
trip_type is left empty; OFF_REFERENCE is not automatically called deadhead.
This run has no confirmed censored tail, so its results cannot be used to verify
the handling accuracy of real right-censored tails. See
[tides.py:310](../tools/experiment05/tides.py#L310).

device_id is not required by the schema, but its description attaches
conditions such as a single device to an empty device ID on a passenger event;
the source data has no device inventory, so this run cannot independently
confirm that described condition. Currently the value is kept unknown and no
devices are fabricated; follow-up should confirm with the agency whether records
have already been aggregated across multiple devices.

## Licensing, data rights and formal-release boundary

The source repository has an [Apache 2.0 LICENSE](../../../LICENSE); the
official TIDES specification pages declare CC BY 4.0, and the local packages
retain the original schemas, pinned commit, SHAs and license. CC BY requires
appropriate attribution, a link to the license and, where applicable, an
indication of modifications, and must not imply official endorsement; it does
not automatically confer any other data- or privacy-related rights. See the
[official CC BY 4.0 description](https://creativecommons.org/licenses/by/4.0/).

These licenses are not a redistribution license for the raw TTC AVL/APC data or
the GTFS archive used. This audit did not obtain the corresponding authorization
contract, data-sharing terms, or license evidence sufficient to cover the
proposed release use; the datapackage also does not fill in a data `licenses`
entry speculatively. This means **the rights conclusion is not established**; it
is not a finding that possession or use is unlawful. The existing
[source data audit](source-data-audit.md#data-handling-note) likewise requires
rights to be confirmed before release. Before data is formally provided
externally, the data owner should confirm source and use authorization,
redistribution scope, attribution requirements, retention period, and the
privacy handling of vehicle trajectories and personnel associations; where
necessary, qualified legal personnel should review the specific terms.

Recommended final statement: **Structural and project-consistency validation
passed; business facts, operating-day attribution and some event semantics
remain explicitly labeled experimental inferences; this is neither official
certification nor a legal opinion on data use.**
