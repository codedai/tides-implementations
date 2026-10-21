# APC–AVL raw-data edge-case audit (2026-09-08)

This audit re-read both raw CSV files, covering **6,202,996 AVL rows** and **573,596 APC rows**, without using GTFS and without modifying the raw data or existing human labels. The results show that coverage inconsistency does exist; duplicate exports, the repeated DST hour, terminal handovers, APC record grain and extraction-window truncation all affect how that inconsistency should be interpreted.

This is the complete inventory of implemented checks; it does not mean every possible anomaly has been exhausted, nor can it be used to declare any particular trip segmentation or merge architecture the winner. The thresholds here are used to discover and stratify inspection samples; they have not been calibrated as criteria for directly accepting associations.

## Evidence and counting conventions

- Reproducible entry point: [raw_audit.py](../experiments/apc_avl_coverage_study/raw_audit.py). The full run completed successfully; five synthetic tests cover DST classification, lossless retention, nested interval overlap, the distinction between duplicates and collisions, and first/last inconsistency between the two sources. Code checks passed.
- Local access-restricted evidence: [inventory.json](../artifacts/apc-avl-coverage-study-20260908/raw_audit/inventory.json), recording file SHA-256 digests, raw column names, parsed types, statistics and at most five examples per check. It is a gitignored local artifact and is not published with this report.
- [avl.parquet](../artifacts/apc-avl-coverage-study-20260908/raw_audit/avl.parquet) and [apc.parquet](../artifacts/apc-avl-coverage-study-20260908/raw_audit/apc.parquet) retain every row; the raw file hash and row number form a stable row identity. There is no deduplication, summation, rounding, fabricated records or DST fold selection. Artifact directory permissions are `0700` and files are `0600`.
- "Rows" in the tables use all AVL rows or all APC rows, respectively, as the default denominator. Conditional checks count rows meeting the listed condition; multiple conditions can cover the same row, so they cannot be summed into a total anomaly row count.
- AVL adjacent pairs after sorting by vehicle and local time total **6,202,840**, of which **5,131,261** have a positive time gap; repeated timestamps were not automatically merged. APC adjacent pairs are sorted by vehicle, arrival, departure and source row number, with a denominator of **573,448** pairs. Gaps include cross-day, cross-shift and missing-record cases and cannot be directly interpreted as travel time.
- "Vehicle × calendar day" groups by AVL message date and APC arrival date; the departure time of a cross-midnight APC row can still extend into the next day. This is an observational diagnostic partition, **not a service date, an actual trip or a continuous coverage interval**.
- All timestamps remain timezone-naive local wall-clock time; only the ambiguous and nonexistent hours are flagged according to `America/Toronto`. Clock synchronization, fixed offset and drift between the two sources have not been verified.

## Format, missing values, duplicates and identity

| Check | Result | Status and interpretation |
|---|---:|---|
| Raw column count | AVL 13; APC 23 (including the export index) | Full schema recorded; identifiers initially retained as strings |
| Raw missing or blank fields | One APC street-description field, 21,119 rows; all other columns 0 | Observed; missing optional text differs from missing spatiotemporal keys |
| Timestamp parse failures or missing | 0 in both sources | Not observed in this sample |
| Numeric parse failures, non-finite values | 0 in all checked numeric fields | Not observed in this sample; bad values were not silently zero-filled |
| `invalid` structural flag | 0 in both sources | Covers key identity/time, global coordinate domain and similar checks; does not imply direct matchability or publishability |
| Rows that are members of an identical-payload duplicate group | AVL 2,118,605; APC 0 | **Includes every copy in each duplicate group**; the export index is ignored when comparing APC |
| Extra identical-payload copy rows | AVL 1,071,525; APC 0 | Total rows minus distinct payloads; not the same metric as the previous row |
| AVL multi-row groups with the same vehicle and timestamp | 1,047,123 groups | Group count differs from duplicate row count |
| Same-vehicle, same-timestamp AVL payload conflicts / position conflicts | 54 groups each | All fall within the DST-ambiguous hour; 0 outside the ambiguous hour |
| APC stop-key multi-row groups / conflict groups | 450 groups each | Key is vehicle, route, stop code, arrival, departure; direction or other fields may still differ |
| APC same-vehicle, same-arrival-time multi-stop-code / multi-position groups | 1,377 groups each | Requires checking stop proximity, terminal role and the repeated hour; passenger counts cannot be directly added or summed |
| Vehicle sets | AVL 156; APC 148; shared 147 | AVL-only 9; APC-only 1; matching names do not yet prove the device mapping is correct over time |
| Route label sets | AVL 4 kinds; APC 2 kinds | The two additional non-numeric AVL labels total 295,801 rows; their operational role needs verification |
| Same-vehicle time regressions in raw AVL row order | 2,952,022 rows | The export is not sorted by vehicle time; this is not evidence of vehicles moving backwards |
| Distinct source `tatripId` values | 3,133 | Cannot be used directly as an actual trip primary key |
| Source `tatripId` reused across vehicles / calendar days / routes | 2,410 / 2,420 / 469 values | Each of the three uses the 3,133 distinct values as denominator; the sets can overlap |
| `Date_Key` parsing and relation to message date | 0 parse failures; all equal the message calendar day | No differences found in this sample; this does not prove it represents the operational service date |

Different positions within the same second could in principle trigger a "cannot be in two places at once" check, but here they all fall within the repeated hour: sorted wall-clock time does not map uniquely to a real instant. The ambiguity should be retained; one position must not simply be chosen, nor all conflicts flagged as bad GPS.

## Time, space, sampling and operational context

| Check | Result | Status and interpretation |
|---|---:|---|
| Records in the DST-ambiguous hour | AVL 3,831; APC 428 rows | An APC row counts if either endpoint falls in the ambiguous hour; no fold is selected |
| Records in the DST-nonexistent hour | 0 in both sources | Not observed in this window; not a conclusion about other months |
| AVL cross-midnight adjacent pairs within 15 minutes | 279 pairs | Truncating by calendar day may split continuous activity |
| APC arrival and departure on different calendar days | 76 rows | Crossing midnight cannot automatically be judged an error or have its date forcibly changed |
| Zero coordinates / coordinates outside the diagnostic region | AVL 431 rows each; APC 0 for both | The two counts coincide in this sample; globally valid coordinates are not the same as operationally usable coordinates |
| AVL positive sampling interval | Median 10 s; P95 20 s; P99 24 s | Uses all positive-gap adjacent pairs; different scenarios still need separate checking |
| AVL gaps over 60 / 90 / 120 s | 1,804 / 1,705 / 1,484 pairs | Cumulative thresholds; cannot be summed |
| AVL gaps over 300 / 900 / 1,800 / 3,600 s | 1,341 / 1,254 / 1,203 / 1,184 pairs | Includes cross-shift and cross-day cases; not all occur within a single trip |
| AVL short-gap implied speed over 130 km/h | 159 pairs on initial screen; 41 pairs after requiring both ends in-region and non-DST-ambiguous | Gap within 0–120 s and greater than zero; still jump-point candidates, not a real speed conclusion |
| AVL reported speed over 130 km/h, negative speed, heading out of range | 0 rows each | Reported speed and position-difference speed are different signals |
| AVL unrecognized boolean flags | 0 rows in both boolean fields | Not observed in this sample |
| AVL adjacent displacement under 3 m with increasing time | 1,991,947 pairs | Includes explanations such as stopped vehicles, GPS quantization and repeated reported positions |
| Runs of identical coordinates with adjacent gaps of at most 90 s | At least 5 min: 11,405 runs; at least 15 min: 1,064 runs | Counted from all 3,222,592 coordinate runs; not confirmed terminal layovers |
| AVL route / run number / destination / source trip label changes | 3,999 / 1,171 / 17,817 / 16,385 changes | All same-vehicle adjacent pairs; boundary evidence requires persistence and other information |
| The above changes with an adjacent gap of at most 120 s | 2,862 / 27 / 17,344 / 15,130 changes | Many context switches remain after excluding long gaps |
| Single-row A→B→A label flips with both adjacent gaps of at most 60 s | Route 595; run number 0; destination 883; source trip 721 | Diagnosed on the sorted raw retained rows; counts may change after deduplication |
| APC same-vehicle context changes | Route 567; direction 15,214; branch 914 | These are source label changes, not yet verified turnaround points |
| APC positive inter-stop observation gap | Median 46 s; P95 195 s; P99 538 s | Denominator is 562,405 positive-gap adjacent pairs |
| APC gaps over 300 / 900 / 1,800 / 3,600 s | 13,212 / 3,015 / 1,327 / 1,199 pairs | Includes cross-day cases, missing stops, layovers and the next trip |
| APC short-gap implied speed over 130 km/h | 64 pairs | Gap of 1–300 s from the previous departure to this arrival, with this row non-DST-ambiguous; candidates need verification |
| Same APC stop code with multiple coordinates / coordinate bounding extent over 100 m | 0 stop codes for both | Coordinates are consistent in this sample; this does not prove stop codes equal GTFS stop identity |

The momentary switching of source context shows that "split a trip whenever a label changes" needs to be tested as a falsifiable hypothesis. A long stationary period also cannot define a terminal on its own: the checks above do not distinguish terminal layover, traffic congestion, garage activity, device freeze and GPS position quantization.

## APC grain, load and interval relationships

| Check | Result | Status and interpretation |
|---|---:|---|
| `AvgSample` distribution | 1: 528,370 rows; 2: 45,020 rows; 4: 206 rows | An explicit multi-sample signal exists; the field's meaning has not been verified |
| `NoTrips` distribution | All 573,596 rows are 1 | Cannot independently prove that "one row equals one actual stop visit" |
| `grain_uncertain` explicit signal | 45,226 rows | `AvgSample` or `NoTrips` not equal to 1, or fractional boardings/alightings/load; rows not triggering it have not obtained grain confirmation either |
| Fractional boardings-alightings / arrival load / departure load | 0 / 23 / 24 rows | No rounding; the publication semantics of fractional loads versus integer passenger counts remain to be confirmed |
| Fractional or non-positive `AvgSample`, `NoTrips` | 0 rows each | Not observed in this sample |
| Negative boardings/alightings or negative load | 0 rows in every field | Not observed in this sample |
| Boardings and alightings both zero | 203,391 rows | Zero activity is not grounds for deleting a stop visit |
| Boardings, alightings and arrival/departure loads all zero | 24,808 rows | Intervals lacking APC cannot be filled with zero ridership on this basis |
| Absolute value of departure load - arrival load - boardings + alightings over 10⁻⁶ | 5,969 rows | Single-row balance check; not attributed to device fault or automatically corrected |
| Same route, same direction, 0–300 s gap, current arrival load differs from previous departure load by more than 1 | 23,032 rows | A count after conditional filtering; grain, missing records and boundaries can all affect continuity |
| Reversed arrival/departure interval | 0 rows | Not observed in this sample |
| Zero-length arrival/departure interval | 112,898 rows | Every APC interval cannot be required to contain a positive-duration AVL sequence |
| Arrival/departure interval over 5 / 30 min | 10,530 / 47 rows | Large intervals may span terminal layovers or other semantics; the whole interval should not be used to accept all GPS points |
| Arrival-to-departure duration differs from `AvgDwell` by more than 1 s | 0 rows | Only shows that no difference beyond tolerance was detected; cannot be double-weighted as independent evidence |
| Interval overlaps with any earlier-arriving record | 10,213 rows | Uses the running maximum of prior departure times, which detects nested long intervals |
| Interval overlaps with the immediately preceding record | 10,039 rows | Comparing only with the previous row finds 174 fewer rows |
| Adjacent overlap above: same stop, different direction | 8,043 rows | Consistent with terminal handover or different operational roles at the same location; not yet manually confirmed |
| Adjacent overlap above: different stops, at most 100 m apart | 1,984 rows | Possible neighboring stop/platform/terminal coding; not an automatic contradiction |
| Adjacent overlap above: different stops, more than 100 m apart | 10 rows | Conflict candidates requiring closer inspection; "cannot be served simultaneously" has not been proven |
| Adjacent overlap above: this row has a grain signal / lies in the DST-ambiguous hour | 672 / 5 rows | Overlaps with the distance and direction categories; cannot be summed |
| Same stop code reappearing consecutively within 15 minutes | 1,332 rows | May be a repeated same-stop event, handover, layover or turnaround; needs to be distinguished |

The most important distinction is that there is still an evidence gap between "interval overlap" and "mutually exclusive real stop visits". Most adjacent overlaps occur at the same stop in different directions or at neighboring stop codes, which supports prioritizing a review of how the two operational roles at a terminal are represented. Treating all overlapping rows as errors would exclude many observations with reasonable explanations; merging and summing them all is equally unsupported.

`grain_uncertain=false` only means that none of the explicit signals above was detected. **The event grain of all APC rows remains independently unconfirmed**, and this limitation should not be lifted because `NoTrips=1` or because boardings/alightings are integers.

## Coverage inconsistency between the two sources

There are **1,401 vehicle × calendar-day** partitions in total: 1,347 present in both sources, 35 AVL-only and 19 APC-only. Among the partitions present in both sources, 1 has first/last observation time ranges that do not intersect at all. The first/last range here is the outer envelope; long gaps can still exist inside it.

Each column below uses the 1,347 partitions present in both sources as the denominator; the condition is "exceeds" the corresponding threshold, and columns can overlap.

| First/last difference threshold | APC starts earlier than AVL | AVL starts earlier than APC | APC ends later than AVL | AVL ends later than APC | AVL starts earlier and APC ends later |
|---|---:|---:|---:|---:|---:|
| Over 0 s | 72 | 1,271 | 183 | 1,158 | 173 |
| Over 60 s | 45 | 1,086 | 85 | 993 | 68 |
| Over 300 s | 40 | 1,009 | 37 | 923 | 29 |
| Over 900 s | 36 | 949 | 19 | 796 | 13 |

Using all APC rows as the denominator, the following were also observed:

| Check | Result | Interpretation |
|---|---:|---|
| APC in a vehicle × calendar day with no AVL | 1,892 rows | Differs from the vehicle being absent for the whole month or having data on other days |
| APC arrival earlier than the first AVL of the same day | 4,150 rows | GPS should not be extrapolated to cover the head |
| APC arrival later than the last AVL of the same day | 337 rows | Single-source tails need separate attribution evidence |
| APC departure later than the last AVL of the same day | 460 rows | Includes cases that lose AVL support within the interval; overlaps with the previous row |
| Searching the vehicle's full window, no AVL at all before / after arrival | 43 / 1,406 rows | The before/after search spans calendar days; a different convention from the same-day coverage statistics |
| Bracketing AVL exists at arrival time, with span over 90 s | 6,430 rows | A long gap must not be disguised as dense-trajectory support |
| Bracketing AVL each within 60 s of arrival | 565,765 rows | **Time condition only**; space, route, clock and candidate uniqueness have not been verified |
| Strictly inside a bracketing AVL gap over 300 / 900 s | 6,130 / 6,022 rows | Bracketed over the full window, possibly across calendar days or shifts; not equivalent to a device dropout within the same trip |

APC arrivals with bracketing AVL total 572,181 rows, with a bracketing span median of 13 s, P95 of 23 s and P99 of 3,091 s. The long tail shows that overall sampling frequency cannot replace a local support check for each association. These two points were not used here to interpolate or to establish any association.

The extraction window is also truncated: AVL stops exactly at the month-end calendar-day boundary, while APC extends into the early hours of the next month. Some tail differences may therefore be caused by the extraction range. Even when first/last times are misaligned within the same vehicle × calendar day, it cannot be asserted that a sensor stopped early within the same actual trip; shifts, trip boundaries, the extraction window and the available stop sequence must be checked first.

## Unknown or unmeasured items

| Item | Current status | Impact on subsequent comparison |
|---|---|---|
| Whether a single APC row represents an independent stop visit; how the sampling, averaging and load fields are generated | Semantics unknown | Retain raw values and provenance; an untriggered grain signal cannot be equated with confirmation |
| Fixed offset, drift and resets between the two sources' clocks | Not estimated, not verified | Clocks cannot be corrected directly from first/last time differences; independent evidence from shared stop visits is needed |
| Mapping to the two real instants during DST | Flagged ambiguous, not resolved | Chronology and spatial conflict checks must retain the feasible interpretations |
| Mapping from vehicle number to actual vehicle, device and device replacement | Only string sets compared | The same-name matching assumption needs device or operational records to support it |
| Real trip start/end, short-turns, route changes, garage and non-revenue runs | No ground truth established | Context changes, stationary periods and long gaps can only provide candidate boundaries |
| Authority and update timing of source route, run number, destination and source trip labels | Semantics unconfirmed; changes and reuse quantified | Should not automatically serve as a reliable trip primary key or a per-change segmentation condition |
| Whether a long stop at the same stop in different directions is two roles of one physical event | Many consistent observations, not manually confirmed | Overlap cannot automatically be treated as conflict, nor directly summed |
| GPS quantization, device freeze, real stops and traffic congestion | Only identical-position runs measured | A stationary threshold cannot independently prove a terminal or an out-of-service period |
| Stop-code aliases, neighboring stops and directional platform relationships beyond within-stop-code consistency | No stop-relationship ground truth established | Spatial proximity does not equal the same stop identity |
| Missing stop visits, under-reported ridership, double counting and true device accuracy | No manual or independent-device ground truth established | Neither coverage nor balance-equation consistency can be directly interpreted as accuracy |
| GTFS stops, scheduled trips, service dates and schedule deviation | Deliberately not used in this audit | This report does not evaluate schedule-matching correctness or on-time performance |
| Other months, routes, devices, spring DST and different export formats | Not measured | Checks observed at zero cannot be extrapolated to general absence |

These limitations do not prevent comparing segmentation and merge methods, but they require structural validity, spatiotemporal support, source coverage, record grain, boundary evidence and unresolved proportions to be reported separately. Any "accuracy" conclusion still requires independent samples; what this audit provides is a reproducible risk inventory and a basis for validation stratification.
