# Experiment 05: versioned mapping convention for APC stop visits and passenger events

## Scope and assumptions

Under the user-confirmed research assumption, this version interprets one APC
record that passes attachment validation as one stop visit by the same vehicle
at one stop; that record's boarding/alighting are the aggregate boardings and
alightings for that visit. This assumption is not a vendor-certified definition
of the source data, nor a grain that TIDES requires all APC vendors to adopt.
Original diagnostics such as `grain_uncertain` are not changed to verified.

The production configuration `config/experiment05-v1.toml` explicitly selects:

```toml
[export]
passenger_event_policy = "apc_stop_interval_departure_proxy_v1"
```

The former `omit_without_event_semantics` can still be selected explicitly; the
standalone export API keeps the former conservative behavior when no policy is
supplied. The policy change alters the config hash, the code content version and
the cache key; it does not modify already published earlier runs. Raw data,
pre-matching evidence, trip segmentation, candidate scoring/ranking and the
three-round assignment are not affected by this **export policy**.

## Time, counts and event types

This policy is applied only to visits with a traceable APC source row, a valid
arrival/departure interval, and unique stop/performed-trip attribution. Genuine
source row identity is preserved; APC is not disguised as AVL coordinate points.

| TIDES event_type | event_timestamp | event_count | Time meaning |
|---|---|---|---|
| `Vehicle arrived at stop` | APC original arrival time | 1 | Vehicle arrival time from the source record |
| `Vehicle departed stop` | APC original departure time | 1 | Vehicle departure time from the source record |
| `Passenger boarded` | APC original departure time | Aggregate boardings for this stop visit | Representative time booked at the departure instant, not a measured per-passenger instant |
| `Passenger alighted` | APC original departure time | Aggregate alightings for this stop visit | As above; does not claim all alightings occurred at the departure instant |

For example, if an APC record indicates arrival at 10:00, departure at 10:01,
5 boardings and 2 alightings, then an arrival event, a departure event and two
aggregate passenger events are generated; both passenger events are timestamped
at 10:01. Seven per-passenger events are not fabricated, and no ordering between
boarding and alighting is assumed. Times are exported in UTC after the existing
timezone normalization.

A zero count generates no passenger event of that type; missing values are not
filled with zero, and problems such as negative values, non-integers, incomplete
or inverted times must still be rejected/recorded in diagnostics. This
assumption cannot be used to bypass data-integrity checks. Aggregate counts for
a stop visit are not prorated over the clipped business duration. GTFS matching
continues to use the APC time **interval**, not the exported proxy time.

The diagnostics for each exported event retain the source row IDs, stop visit,
original arrival/departure interval, policy ID, time source, and whether the
time is a proxy / the semantics are an assumption not independently confirmed.
This information is written to the package's `diagnostics.json` and is not added
to TIDES core CSV fields. Without a genuine event-level location association,
`location_ping_id` remains null; the nearest AVL ping or a door is not chosen
arbitrarily.

TIDES v1.0 allows `event_count` to represent multiple events of the same type
and requires an event timestamp; it does not, however, prescribe that aggregate
passenger counts for a stop visit must use the departure time. What this
document defines is this project's explicit research mapping convention.
Passing the structural validator does not mean that the source field semantics
or time precision have been independently verified.

## How stop_visits are actually determined at present

Generation happens in pipeline stage 6, before GTFS candidate generation and
final assignment:

```text
independently normalized raw APC rows
  → map the stop to its position/interval on the canonical reference
  → check time, reference, stop, direction and progress against same-vehicle performed trips
  → unique best trip + unique stop_id
  → one source APC row generates one stop_visit
  → order and number within the trip by original arrival time, departure time, source row ID
```

The specific rules and current defaults are governed by the `[apc]` and
matcher/projection configuration:

1. Arrival and departure times are both valid, and departure is not earlier
   than arrival; the interval length does not exceed
   `maximum_service_interval_s = 1800` seconds.
2. Attribution is sought only among performed segments with the same vehicle
   ID. The APC interval may overlap the segment's effective business interval;
   when it does not, the distance between the two intervals must not exceed
   `attach_time_buffer_s = 120` seconds. A confirmed censored tail must not be
   reintroduced into matching through attachment.
3. An unambiguous canonical stop mapping consistent with that trip's valid AVL
   reference must exist. The absence of genuine APC vehicle coordinates does
   not cause deletion; the row remains stop-plus-time-interval evidence, not a
   GPS ping.
4. If comparable valid AVL points exist near the interval, the minimum
   canonical progress difference between the stop and the AVL must not exceed
   `attach_progress_tolerance_m = 200` meters. This is not "every point must be
   within 200 meters", nor does it overwrite the APC arrival/departure times
   with the nearest AVL time.
5. Selection is ascending by
   `(direction conflict, time gap, -overlap duration, progress error)`.
   Direction conflict is a soft penalty; when best trips tie, the ambiguity is
   retained rather than forced by file row order. Without nearby AVL,
   attachment may still rely on other evidence; the lack of a local progress
   comparison is not reported as observed consistency.
6. After trip attribution is selected, a visit is generated only if the stop
   mapping converges to a unique stop_id. `stop_visit_id` is constructed from
   the stable performed trip ID and the source row ID; `trip_stop_sequence` is
   numbered consecutively from 1. Repeated visits to the same stop are not
   merged merely because the stop_id is the same.
7. Actual arrival/departure times come directly from that APC row, and dwell is
   their difference; actual stop visits are not back-derived from the GTFS
   schedule. When exporting dwell as integer seconds, values that cannot be
   represented are not rounded arbitrarily.

## Current boundaries and known limitations

- This is an **APC-driven observed stop-visit hypothesis**, not a complete list
  of stop visits independently identified from the AVL trajectory. Actual stop
  visits lacking APC are not automatically fabricated; the absence of a matched
  GTFS scheduled trip does not prevent an evidenced performed trip from
  retaining its stop_visits.
- Currently one APC source row corresponds to one visit; adjacent rows are not
  merged and APC is not irreversibly deduplicated. `visit_merge_gap_s` in the
  configuration is not currently used by any merge logic and must not be
  understood as a 30-second stop-visit merge being performed.
  Duplicate/overlapping APC intervals require independent review and cannot
  automatically be understood as distinct passenger activity.
- Actual stop-visit numbering is the **observation order**, not the GTFS
  `stop_sequence`. The current stage 6 does not automatically map a visit to a
  specific scheduled stop occurrence of the final candidate; without an
  explicit mapping, scheduled_stop_sequence and the scheduled arrival/departure
  times remain null. Events are linked through the actual trip and stop visit
  and use the trip_id_scheduled of their own mode; assignments are not borrowed
  across modes.
- `boarding_1/2` and `alighting_1/2` carry door semantics; without
  door-allocation evidence, whole-vehicle counts are not arbitrarily placed in a
  door field. Counts are retained in the passenger_events event_count and in
  the shared APC evidence.
- If a vendor dictionary or device time semantics are obtained later, the
  revision should be made under a new policy ID rather than silently changing
  this version.

Implementation entry points: `tools/experiment05/apc.py:attach_apc` and
`tools/experiment05/tides.py:build_tides_rows`. See the
[TIDES export contract](tides05-contract.md) for the pinned standard version and
validation method, and the [output schema](experiment05-output-schema.md) for
the data structures of each stage.
