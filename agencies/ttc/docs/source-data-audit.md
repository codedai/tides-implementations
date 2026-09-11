# Source data audit

## Purpose and limitations

This is a structural audit of the files currently under `database_offline/`. It supports pipeline design; it is not an authoritative TTC data dictionary. Meanings inferred from names or values are labeled as hypotheses and must be confirmed before they become transformations.

Audit date: 2026-08-05.

## Inventory

### AVL

File: `avl/20240401_Vehicle GPS Rt29 RT 929 Nov 2023.csv`

- 6,202,996 data rows
- approximately 840 MB
- timestamps/service keys cover 2023-11-01 through 2023-11-30
- 156 distinct vehicle identifiers
- route-value counts:
  - `29`: 3,501,537
  - `929`: 2,405,658
  - `PI`: 100,875
  - `PO`: 194,926
- UTF-8 byte-order mark is present before the first header.

Columns:

| Source field | Initial interpretation | Design treatment |
| --- | --- | --- |
| `Date_Key` | Source service date candidate | Parse as date; validate against timestamp and GTFS. |
| `Message_DateTime` | AVL event timestamp | Parse in `America/Toronto`. |
| `Vehicle` | Vehicle identifier | Normalize as string; candidate TIDES `vehicle_id`. |
| `Latitude`, `Longitude` | WGS-84 GPS position | Validate range and TTC-area plausibility. |
| `Heading` | Degrees clockwise from north | Candidate TIDES `heading` after range validation. |
| `Route` | Public route or operational-state code | Join to GTFS `route_short_name` only for known public routes. |
| `Run` | Operational run identifier | Matching/segmentation evidence; semantics unconfirmed. |
| `Destination` | Operator-facing destination/headsign | Matching evidence after conservative normalization. |
| `OffRoute` | Source off-route flag | Quality/matching evidence. |
| `Delayed` | Source delay flag | Diagnostic evidence; insufficient for seconds of schedule deviation. |
| `KPH` | Speed in kilometres per hour | Preserve; convert to m/s for TIDES. |
| `tatripId` | TTC/vendor trip-like identifier | Preserve as source evidence only. Not a GTFS trip ID. |

#### `tatripId` finding

There are 3,133 distinct `tatripId` values. Zero AVL rows have a `tatripId` that exists in the GTFS `trips.txt` files covering November 2023.

At least two observed values exist as `trip_id` in a July/August GTFS feed, but those GTFS records describe unrelated routes. This demonstrates that numeric equality across feed versions can be a false collision. The field must not populate TIDES `trip_id_scheduled` without an independently validated crosswalk.

#### `PI`/`PO` finding

The values occur where a public route number would normally appear. They may mean pull-in and pull-out, which would align conceptually with TIDES `trip_type`, but this is not proven by the file. Keep them in normalized AVL and exclude them from public-route GTFS matching until confirmed.

### APC

File: `apc/ttc_apc_data.csv`

- 573,596 data rows
- approximately 97 MB
- event timestamps cover 2023-11-01 through 2023-12-01
- 1,150 rows have a 2023-12-01 arrival date
- 148 distinct vehicle identifiers
- routes `29` and `929`
- directions `NORTH` and `SOUTH`
- 103 distinct source `StopID` values
- first CSV column has no name and appears to be an exported row index.

Columns:

| Source field | Initial interpretation | Design treatment |
| --- | --- | --- |
| unnamed first field | Export row index | Preserve as source export ID; do not treat as domain data. |
| `DAYTYPE` | Source day category | Diagnostic/check against inferred service date. |
| `Route`, `RouteName` | Public route and label | Match to selected GTFS route by `route_short_name`. |
| `Branch` | Route branch/pattern-like label | Scheduled-trip matching evidence; semantics unconfirmed. |
| `VehicleID` | Vehicle identifier | Normalize as string and reconcile with AVL `Vehicle`. |
| `RouteDirection` | Cardinal direction | Map to GTFS direction only through observed evidence/configuration. |
| `StopID` | Public stop code | Join to selected GTFS `stops.stop_code`, not `stops.stop_id`. |
| `ONSTREET`, `ATSTREET` | Stop intersection description | Stop-mapping evidence. |
| `Lat`, `Lon` | Stop/event coordinates | Validate mapped stop; controlled spatial fallback. |
| `PeriodID` | Source time-period label | Diagnostic; not a TIDES time field. |
| `StopArrivalTime`, `StopDepartureTime` | Observed stop interval | Candidate TIDES actual arrival/departure. |
| `NoTrips` | Aggregation/sample field | Semantics need confirmation before mapping. |
| `AvgDwell` | Dwell-like duration | Prefer departure minus arrival unless aggregation semantics are confirmed. |
| `AvgOTP` | On-time-performance-like field | Preserve only; semantics and units need confirmation. |
| `AvgSample` | Sample-size-like field | Preserve only; semantics need confirmation. |
| `Arrload`, `Depload` | Passenger load | Candidate stop-visit arrival/departure load evidence. |
| `Boarding`, `Alighting` | Passenger counts | Candidate aggregate passenger events; door semantics unknown. |

#### Stop identity finding

Across the GTFS feeds relevant to November:

- direct comparison with GTFS `stops.stop_id` maps 403,336 rows and 72 of 103 distinct source values;
- comparison with GTFS `stops.stop_code` maps 559,715 rows and 102 of 103 distinct source values;
- source stop code `1652` is the only remaining distinct value and occurs on 13,881 rows.

Therefore the source field named `StopID` is a public stop code in this fixture. The pipeline must use a version-aware `stop_code` crosswalk. Code `1652` needs feed/history investigation; a spatial fallback may be considered only if coordinates and stop descriptions identify one unambiguous GTFS stop.

#### Post-midnight finding

The 1,150 rows dated 2023-12-01 may belong to November 30 service. APC lacks an explicit service date, so the pipeline must infer it from selected GTFS service, times, stop order, and attached AVL context. A fixed cutoff should not be introduced without validation.

### GTFS

Directory: `gtfs/`

- 14 ZIP archives
- approximately 441 MB compressed
- each inspected archive contains: `agency.txt`, `calendar.txt`, `calendar_dates.txt`, `routes.txt`, `shapes.txt`, `stops.txt`, `stop_times.txt`, and `trips.txt`
- no inspected archive contains `feed_info.txt`
- service calendar envelope across the archives: 2023-01-08 through 2024-01-06
- multiple archives have overlapping or identical calendar ranges.

November candidates:

| Filename suffix | Calendar range | Earliest observed archive member date | Initial interpretation |
| --- | --- | --- | --- |
| `ad8cfc0b...` | 2023-10-08 to 2023-11-18 | 2023-09-27 | Older candidate covering early November. |
| `cdab5829...` | 2023-10-30 to 2023-11-18 | 2023-10-27 | Likely later revision for November 1–18. |
| `7ecc0186...` | 2023-11-19 to 2023-12-23 | 2023-11-06 | Candidate for November 19 onward. |

The archive member date is not guaranteed to be an acquisition or publication timestamp. A curated feed manifest must confirm precedence.

#### Versioned route IDs

Public routes retain their short names but change internal IDs:

| Feed suffix | Route 29 `route_id` | Route 929 `route_id` |
| --- | ---: | ---: |
| `ad8cfc0b...` | `70071` | `70196` |
| `cdab5829...` | `70291` | `70416` |
| `7ecc0186...` | `70507` | `70632` |

AVL/APC route values must first match `route_short_name` in the selected feed. Persisting a single cross-feed `route_id` would be incorrect.

## Risks to test first

1. Feed precedence for November 1–18.
2. Service-date inference for the December 1 APC timestamps.
3. Meaning and stability of AVL `Run` and `tatripId`.
4. Meaning of APC `Branch` and whether it corresponds to a stop pattern.
5. Stop code `1652` resolution.
6. APC-to-AVL temporal and spatial attachment rates.
7. Whether APC records are raw stop visits or aggregates implied by the `Avg*` and `NoTrips` fields.
8. Whether boarding/alighting counts have door-level meaning compatible with TIDES stop-visit fields.

## Data-handling note

The source fixture contains operational vehicle traces and is not committed by this scaffold. Before publishing samples or outputs, confirm redistribution, privacy, and licensing constraints and replace real identifiers when required.
