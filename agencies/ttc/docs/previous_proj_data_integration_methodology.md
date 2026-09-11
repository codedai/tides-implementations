# GTFS–AVL–APC Data Integration Methodology

**Project:** Transit Analysis  
**Documented:** 2026-08-07  
**Scope:** The code, notebooks, existing documentation, diagrams, and data artifacts in this workspace

## 1. Purpose and main conclusion

This document explains how the project uses General Transit Feed Specification (GTFS), Automatic Vehicle Location (AVL), and Automatic Passenger Counter (APC) data together. It distinguishes between:

1. transformations that are executed by the current notebook workflow;
2. reusable or intended transformations present elsewhere in the source code; and
3. preprocessing already embedded in the supplied “clean” CSV files but not reproducible from code in this repository.

The executable notebook path is narrower than the workflow described in `intro.md` and `image.png`. It does **not** create a fully integrated dataset directly from the raw GTFS, AVL, and APC files. Instead, it:

- builds a linear route-distance reference from one hard-coded GTFS shape (`shape_id = 832096`, Route 29 northbound);
- reads a pre-cleaned Route 29 AVL artifact that already contains `TripID`, `RouteDirection`, `SteadyState`, and other derived fields;
- snaps every selected AVL ping to the closest densified GTFS shape vertex;
- reads a pre-cleaned APC artifact that already contains a compatible `TripID`; and
- attaches the latest non-future APC `Arrload` to each AVL ping using a backward `pandas.merge_asof` lookup within `TripID`.

The repository does not contain the code that originally constructs the shared AVL/APC `TripID`, segments raw AVL records into trips, creates the clean APC stop sequences, or derives the clean-file anomaly/bunching fields. Those clean files are therefore upstream inputs to the active integration, not outputs that can currently be regenerated here.

## 2. Evidence and interpretation rules

This document uses the following evidence order:

1. executable Python called by the notebooks;
2. other Python utilities in the repository;
3. schemas and values observed in the supplied data artifacts; and
4. narrative claims in `intro.md`, `data/README.md`, and `image.png`.

When narrative and executable behavior differ, the executable behavior is reported as authoritative and the difference is called out. The workspace is not recognized as a Git worktree, so this document cannot attach a commit identifier to the snapshot.

## 3. Pipeline at a glance

```text
GTFS shapes.txt
  └─ select shape_id 832096
       └─ densify to ≤ minimum original vertex spacing
            └─ calculate northbound cumulative distance `dis`
                         │
pre-cleaned AVL CSV      │
  └─ filter Route/date   │
       └─ nearest-vertex snap (cKDTree) ──► aligned AVL pings
                                                   │
pre-cleaned APC CSV                              backward as-of join
  └─ retain TripID, StopArrivalTime, Arrload ──────┘
                                                   │
                                                   ▼
                            one row per selected AVL ping, enriched with
                            preceding APC Arrload and integer `pc`
```

GTFS supplies the spatial reference. The clean-file `TripID` supplies the logical AVL-to-APC relationship. Time determines which APC stop event is assigned to each AVL ping.

## 4. Implementation-status matrix

| Stage | Location | Status in the active notebook path |
|---|---|---|
| Read five GTFS tables and normalize schedule times | `utils/read_gtfs.py:32–44` | Available, but not called by the notebooks |
| Build route/direction stop-code dictionaries | `utils/read_gtfs.py:47–104` | Available, but not consumed downstream |
| Select a per-direction “dominant” shape | `utils/read_gtfs.py:107–161` | Intended generic path; currently fails during shape processing |
| Read raw AVL/APC and correct service-day keys | `utils/read_avl_and_apc.py:3–38` | Available, but not called by the notebooks |
| Filter raw AVL/APC by route | `utils/read_avl_and_apc.py:41–65` | Intended; APC filtering fails after the preceding positional column drop |
| Construct the shared AVL/APC `TripID` | No executable source found | Already present in the clean CSV artifacts |
| Densify GTFS Route 29 shape 832096 | `data/data_preprocess.py:5–30` | Active |
| Snap AVL coordinates to the GTFS shape | `utils/interpolate.py:26–38` | Active |
| Interpolate AVL to one-second intervals | `utils/interpolate.py:81–139` | Optional utility; not called by the notebooks |
| Join APC load to AVL pings | `data/data_preprocess.py:32–59` | Active |
| Visualize the integrated result | `plot_motion.py`; `datainterpolate.ipynb` cells 0–3; `datainterpolate copy.ipynb` cells 0–4 | Active exploratory use |
| Alignment-specific module | `utils/align.py` | Empty |

## 5. Data sources and the fields actually used

### 5.1 GTFS static feed

The feed contains `routes.txt`, `trips.txt`, `stop_times.txt`, `stops.txt`, `shapes.txt`, `calendar.txt`, and `calendar_dates.txt`. The feed calendar covers 2020-10-11 through 2020-11-21, whereas AVL and APC observations cover November 2023. The active pipeline uses GTFS only as a static geometry source; it does not use `calendar.txt` or `calendar_dates.txt`, and it does not match 2023 observations to scheduled 2020 trips.

The active path reads `routes.txt` and `trips.txt` but does not use them. It uses only these GTFS shape fields:

| Field | Use |
|---|---|
| `shape_id` | Select the hard-coded reference shape `832096` |
| `shape_pt_lat`, `shape_pt_lon` | Densification and AVL spatial snapping |
| File row order | Treated as polyline order; `shape_pt_sequence` is not explicitly sorted |
| `shape_dist_traveled` | Ignored; distance is recomputed geodesically |

In this feed, shape 832096 is used by Route 29 northbound trips, contains 286 original vertices, begins near Exhibition Place, and ends near Wilson Station. The feed contains Route 29 but no Route 929 entry, even though the raw 2023 operational files contain both routes.

### 5.2 Raw AVL

`ttc_avl_data.csv` contains approximately 6.2 million November 2023 observations. Important native fields include `Date_Key`, `Message_DateTime`, `Vehicle`, `Latitude`, `Longitude`, `Heading`, `Route`, `Run`, `Destination`, `KPH`, and `tatripId`.

The active path does not read this raw file. It reads `ttc_avl_seg_clean_data.csv`, a 604,449-row Route 29 artifact with additional fields including `TripID`, `Distance`, `SteadyState`, `RouteDirection`, and bunching-related columns. The code that creates those additions is not present. Exact artifact relationships described in Section 8 reveal some of the likely calculations, but they do not replace the missing generator.

### 5.3 Raw APC

`ttc_apc_data.csv` contains 573,596 stop events for Routes 29 and 929. Important fields include `Branch`, `VehicleID`, `RouteDirection`, `StopID`, `StopArrivalTime`, `StopDepartureTime`, `Arrload`, `Boarding`, `Alighting`, and `Depload`.

The active path instead reads `ttc_apc_clean_data.csv`, a 425,591-row Route 29 artifact. It already contains `TripID`, `StopSeq`, abnormality flags, and bunching fields. Only `TripID`, `StopArrivalTime`, and `Arrload` are loaded for the final join. Section 8 records the raw-to-clean relationships that can be demonstrated from the supplied artifacts.

### 5.4 Cross-source field contract

| Concept | GTFS | AVL | APC | How it is used |
|---|---|---|---|---|
| Route | `route_short_name` / `route_id` | `Route` | `Route` | Active filtering uses only AVL `Route`; the GTFS shape is independently hard-coded |
| Direction | First token of `trip_headsign` in the generic reader | First token of `Destination`, or precomputed `RouteDirection` | `RouteDirection` | Not used as an explicit final join key; it is encoded in precomputed `TripID` |
| Trip | `trip_id` | `tatripId` and derived `TripID` | Derived `TripID` | Final join uses only the derived clean-file `TripID` |
| Position | Shape latitude/longitude | GPS latitude/longitude | Stop latitude/longitude | Only GTFS shape and AVL GPS are spatially aligned; APC coordinates are not used |
| Event time | Scheduled arrival/departure | `Message_DateTime` | `StopArrivalTime` | AVL time is matched to the latest non-future APC arrival within `TripID` |
| Passenger load | Not applicable | Not native | `Arrload` | Copied to `Arrload`, then converted to integer `pc` |

The generic stop-reference code maps GTFS `stop_id` to GTFS `stop_code` before constructing its dictionary. This matters because APC `StopID` values in this dataset mostly—and by convention appear intended to—correspond to GTFS stop codes, despite `data/README.md` describing them as GTFS `stop_id` values. In the clean APC artifact, 94 of 99 unique IDs match a 2020 GTFS `stop_code`; the other five are the newer codes listed in Section 7.2. The active integration does not use `StopID` or the stop-reference dictionary.

## 6. Detailed active method

### 6.1 Build a dense one-dimensional route reference

`data.data_preprocess.read_data()` performs the active GTFS work:

1. Read `shapes.txt`, `routes.txt`, and `trips.txt`.
2. Select rows where `shape_id == 832096`.
3. Compute the geodesic distance in metres between every pair of adjacent vertices using `geopy.distance.geodesic`. The helper is named `haversine_distance`, but it uses an ellipsoidal geodesic calculation rather than the haversine formula.
4. Let the smallest original adjacent distance be `d_min`.
5. For every segment of length `d > d_min`, create

   ```text
   n_interior = ceil(d / d_min) - 1
   ```

   interior points by linear interpolation in latitude and longitude. This divides the segment into `ceil(d / d_min)` pieces.
6. Initialize `dis = 0` and accumulate geodesic point-to-point distance along the densified polyline.

For the supplied shape, the current snapshot has:

| Quantity | Value |
|---|---:|
| Original vertices | 286 |
| Minimum original adjacent distance | 0.587829 m |
| Maximum original adjacent distance | 386.615 m |
| Densified vertices | 24,243 |
| Recomputed route length | 14,168.051 m |

The resulting `dis` is a northbound linear reference: approximately zero near Exhibition Place and approximately 14.17 km near Wilson Station. A southbound vehicle can still be represented on the same axis, but its `dis` normally decreases with time.

### 6.2 Select AVL observations

`get_merge_pc_by_date(route=29, dateKey=20231128)` reads the pre-cleaned AVL artifact, then:

1. derives `Color` from `SteadyState` (`red` only when the value equals 2; otherwise `blue`); artifact analysis indicates this upstream field is a low-motion run length, so red means a run of exactly two qualifying pings rather than a general bunching category;
2. floors `Message_DateTime` to a one-minute datetime field for animation (it is converted to a string after the join);
3. filters rows by AVL `Route` and AVL `Date_Key`;
4. sorts by `Message_DateTime`.

No GTFS route lookup verifies that the chosen AVL route corresponds to shape 832096. Changing the function’s `route` argument does not change the hard-coded shape.

### 6.3 Snap AVL pings to the GTFS reference

`find_closest_points()` builds a `scipy.spatial.cKDTree` over the densified `(shape_pt_lat, shape_pt_lon)` coordinates and performs a vectorized `k=1` nearest-neighbour query for every selected AVL `(Latitude, Longitude)` pair.

For matched vertex index `j`, it overwrites:

```text
AVL.Latitude  = shape[j].shape_pt_lat
AVL.Longitude = shape[j].shape_pt_lon
AVL.dis       = shape[j].dis
```

Important details:

- The distance used by the tree is Euclidean distance in unprojected latitude/longitude degrees, not metres.
- The returned nearest-neighbour distance is discarded. No maximum snap tolerance, off-route rejection, or quality flag is applied.
- Raw GPS coordinates are overwritten rather than retained alongside aligned coordinates.
- Snapping is not partitioned by route, branch, or direction.
- Every row, including southbound rows, is snapped to the same northbound shape.
- The match is to a dense vertex, not to the mathematically nearest point on a polyline segment. Densification approximates segment projection.

After snapping, the active function converts `Vehicle` to a string for plotting and adds `Text = "Steady"` whenever upstream `SteadyState > 0`.

### 6.4 Attach APC load with a trip-aware backward as-of join

The APC clean artifact is read with only `TripID`, `StopArrivalTime`, and `Arrload`. Timestamps are parsed, then the join is:

```python
pd.merge_asof(
    aligned_avl,
    apc[["TripID", "StopArrivalTime", "Arrload"]],
    by="TripID",
    left_on="Message_DateTime",
    right_on="StopArrivalTime",
    direction="backward",
)
```

For an AVL ping with trip key `k` and time `t`, the assigned APC row is therefore:

```text
arg max StopArrivalTime
subject to APC.TripID = k
           APC.StopArrivalTime <= t
```

This is a left-like as-of operation: the output retains one row per selected AVL ping. Exact timestamp matches are allowed by the pandas default. There is no tolerance, so any earlier APC observation with the same `TripID` can match regardless of age.

Finally:

```text
pc = int(fill_missing(Arrload, 0))
```

Thus `pc` is the load **on arrival at the most recent stop**, not necessarily the load after boarding and alighting. `Depload` would represent departure load but is not loaded. Missing APC matches are made indistinguishable from a genuine zero load, and the integer cast truncates any fractional average loads.

### 6.5 Active output

The result contains all selected AVL clean columns, the snapped `Latitude` and `Longitude`, GTFS-derived `dis`, `StopArrivalTime`, `Arrload`, and integer `pc`. Before returning, both `Message_DateTime` and `Message_DateTime1min` are converted from datetimes to strings. The result retains the upstream AVL `Distance` column as a separate field. Artifact comparison shows that `Distance` is the geodesic displacement from the preceding ping within the same `TripID`, with the first ping set to zero; it is not cumulative route distance. The visualizations use GTFS-derived `dis`.

## 7. Preprocessing utilities outside the active path

### 7.1 GTFS reader and schedule-time correction

`read_gtfs_data_from_storage()` reads five GTFS tables and rewrites any scheduled hour of 24 or greater as `hour % 24`. For example, `25:10:00` becomes `01:10:00`. This makes strings acceptable as clock times but removes the GTFS next-service-day offset, so chronological ordering across midnight is no longer preserved. It derives `RouteDirection` as the first whitespace-delimited token of `trip_headsign`.

### 7.2 Stop/direction reference algorithm

`get_stops_directions_ref_dict()`:

1. finds route IDs by exact, type-sensitive equality on `route_short_name`;
2. iterates every service ID used by that route, without consulting the calendar for an analysis date;
3. keeps trips whose first stop has corrected arrival time between 06:00:00 and 22:00:00 inclusive;
4. merges stop times to `stops.txt` and replaces `stop_id` with `stop_code` and `stop_name`;
5. for each headsign-derived direction, enumerates `stop_code.unique()` from zero in first-observed row order; and
6. unions differing dictionaries across services, allowing later dictionary values to overwrite earlier indices after printing a warning.

Route 29 receives a special synthetic stop code `1652`: index 0 southbound and `len(NORTH) + 1` northbound. The code assumes uppercase `SOUTH` and `NORTH` keys. This dictionary is computed in `main.py` but is not used by the active integration or plots.

The exception is incomplete for the supplied vintage mismatch. Raw Route 29 APC contains six stop codes absent from the 2020 GTFS stop-code set (`1652`, `16098`, `16367`, `16382`, `16383`, and `16384`); the clean APC artifact removes `1652` but retains the other five. A current stop crosswalk is needed before this dictionary can support APC-to-GTFS stop joins.

### 7.3 Generic per-direction shape selection

`get_shape_directions_ref_dict()` groups GTFS trips by the first headsign token and chooses the shape with the greatest number of vertices. “Dominant” therefore means longest vertex list, not most frequently scheduled shape. Whenever a shorter and longer shape are compared, the function asserts exact latitude/longitude set containment.

That assumption does not hold for every Route 29 variant in the supplied GTFS snapshot: several shorter shape variants contain vertices that are not exact members of the largest shape. In addition, `process_route_shapes()` is called inside the direction loop and is currently nonfunctional because:

- it does not initialize `dis` before cumulative-distance calculation;
- `calculate_cumulative_distance()` mutates its input but returns `None`, while `process_route_shapes()` assigns that return value back to the DataFrame; and
- already processed directions can be processed again on later loop iterations.

The notebooks avoid this generic path and implement the hard-coded shape path described in Section 6.

### 7.4 Raw AVL/APC reader and service-day rule

`read_avl_apc_data_from_storage()` parses AVL `Message_DateTime` and APC arrival/departure timestamps. It creates original and modified date keys. Its service-day rule is:

```text
if timestamp.hour > 3:
    keep Date_Key
else:
    Date_Key = calendar date(timestamp - 1 day)
```

Therefore all times from 00:00:00 through 03:59:59 are assigned to the preceding service date; 04:00:00 begins a new service date. Timestamps remain timezone-naive, and there is no daylight-saving-time treatment.

The reader derives AVL direction from the first token of `Destination` and adds row-index IDs. It then drops columns positionally. With the supplied APC schema, `ttc_apc_data.iloc[:, 3:]` removes the unnamed CSV index, `DAYTYPE`, **and `Route`**. The subsequent `process_avl_apc_data()` attempts to filter APC on the now-missing `Route` column and will raise `KeyError`.

This service-day utility is not called by `data/data_preprocess.py`. The active notebook path filters the date key already stored in the clean AVL artifact and does not create a service-day key for APC.

### 7.5 Optional one-second AVL interpolation

`interpolate_lon_lat()` groups AVL rows by `tatripId`. For gaps longer than one second, it advances from the earlier location once per second using the earlier row’s constant `KPH` and `Heading` with `geodesic.destination`.

This function is not called by the notebooks. If used, it assumes each group is already chronologically ordered, imposes no maximum gap, does not force the synthetic trajectory to meet the later observed endpoint, and carries only a subset of AVL columns. For gaps greater than one second, it emits interior synthetic rows but omits the earlier raw anchor; only the final row of each group is unconditionally retained. This differs from the anchor-preserving behavior described in `intro.md`.

## 8. Precomputed artifact provenance and inferred algorithms

The following results come from exact comparisons between the raw and clean CSVs in this snapshot. “Exact” means the relationship reproduced all applicable supplied rows. “Strong indication” identifies a likely upstream step whose source implementation is still unavailable. These observations document the artifacts but cannot guarantee that a missing generator would behave the same way on new data.

### 8.1 APC clean artifact

The 425,591-row APC clean file is exactly the raw Route 29 subset after removing two terminal stop codes:

| Removed APC `StopID` | Interpreted terminal | Rows removed |
|---:|---|---:|
| 1652 | Wilson Station | 8,204 |
| 14363 | Princes’ Gates Loop | 7,848 |

The subtraction reconciles exactly: `441,643 raw Route 29 rows - 16,052 terminal rows = 425,591 clean rows`. The artifact removes the saved index, `RouteName`, stop latitude/longitude, and `AvgSample`; sorts globally by `StopArrivalTime`; and adds `TripID`, `StopSeq`, two abnormal flags, `LoadRate`, `SteadyState`, and `BunchState`.

Exact artifact relationships include:

- every `(RouteDirection, StopID)` pair maps to one fixed `StopSeq`;
- northbound sequences span 1–49 and southbound sequences span 1–50;
- branch `DLWI` covers northbound sequence 41–49 and `DLPRcon` covers southbound sequence 9–50;
- incomplete trips and sequence gaps are retained; and
- `LoadRate = AvgDwell / (Boarding + Alighting)`, with a zero denominator mapped to zero.

The observed `LoadRate` therefore has units of seconds per counted passenger, not passengers per unit time as stated in `data/README.md`. The algorithms for `AbnormalAvgDwell`, `AbnormalLoadRate`, APC `SteadyState`, and `BunchState` cannot be recovered from simple global thresholds; source code is required before assigning them firm semantics.

### 8.2 AVL clean artifact

Of the 604,449 stored rows, 604,448 have complete derived fields. Every valid base AVL record is an exact member of the raw AVL data, with raw duplicates collapsed. Every valid timestamp lies within the overall first-arrival-to-last-departure span of its corresponding APC `TripID`. Because some keys are reused on different dates, that span check is not occurrence-qualified and is only consistent with—not proof of—upstream matching to APC trip intervals. The actual matching and trip-index rules are absent.

Several derived relationships reproduce exactly:

- `Distance` is point-to-point geodesic displacement from the preceding ping within `TripID`; the first ping is zero.
- A row qualifies as low-motion when `Distance < 80 m` **and** `KPH < 9`.
- `SteadyState` is the length of the contiguous qualifying run containing that row; non-qualifying rows receive zero. Values span 0–145, so this is not a 0/1/2 bunching state.
- `AROUND_TIME` is `Message_DateTime` rounded to the nearest 30 seconds.
- `Time_diff_abs` is the absolute number of seconds between `Message_DateTime` and `AROUND_TIME`, hence 0–15.
- `AroundLat` and `AroundLon` are produced by projecting from the raw point to `AROUND_TIME` at constant `KPH` and `Heading` with `geodesic.destination`; a negative time difference projects backward.

`BunchedTrips` contains serialized lists of other `TripID` values. Its values are consistent with same-direction neighbours on the 30-second grid and an approximately 250 m cutoff, but that threshold is only an inference because no implementation exists.

There is also an identity inconsistency: 3,581 AVL rows across 90 `TripID` values have a precomputed `RouteDirection`/`TripID` direction that disagrees with the first token of raw `Destination`; the malformed final row has null direction. The active merge trusts `TripID` and does not validate this disagreement.

The last stored row is malformed (`TripID = 9032_S_46_`) and has null derived fields. The file is grouped by vehicle and stops partway through vehicle 9032, even though raw AVL includes vehicles through 9152; only 87 of 156 raw vehicles appear. Together with the partial final record and references to absent later vehicles inside `BunchedTrips`, this strongly suggests the checked-in clean AVL artifact is truncated or was produced by an interrupted job. Analyses using it are not complete for the raw month.

### 8.3 `TripID` identity contract

The shared `TripID` is the essential logical key for the final join, but its construction is absent from executable source. The clean artifacts show the pattern:

```text
{vehicle}_{direction_initial}_{segment_index}_{branch}
```

Examples include `1000_N_0_29Dcon` and `9151_N_0_DLWI`. Artifact-level checks show that:

- the first component agrees with AVL `Vehicle` or APC `VehicleID`;
- the second component agrees with the first letter of `RouteDirection`;
- the final component agrees with APC `Branch`; and
- the numeric third component usually does **not** equal raw AVL `Run`, so it should be treated as an undocumented trip/segment index rather than the native run number.

The same `TripID` can occur on more than one date. In the supplied artifacts, 69 AVL keys span multiple AVL `Date_Key` values and 213 APC keys span multiple `StopArrivalTime` calendar dates. The final join does not include service date, `Date_Key`, `tatripId`, route, or direction as separate keys. Correctness therefore assumes that the undocumented segmentation process makes `TripID` sufficiently unique at the relevant time, or that the latest prior timestamp naturally selects the intended occurrence.

The narrative in `intro.md` says GTFS trips and stop times are used to create `TripID` on AVL and that the identifier is independently reconstructed on APC. The repository contains neither implementation, so that statement should be treated as intended or upstream methodology, not a reproducible step of this codebase.

## 9. Assumptions and their consequences

| Assumption | Consequence if violated |
|---|---|
| The 2020 Route 29 geometry is representative of November 2023 operations | Vehicles can be snapped to obsolete or incorrect paths |
| Shape 832096 is appropriate for the requested route, branch, and both directions | Other routes or variants receive incorrect `dis`; Route 929 is unsupported by the active geometry |
| Shape rows are already in `shape_pt_sequence` order | Densification and cumulative distance can jump between unrelated points |
| The minimum original spacing is positive and a sensible target | Duplicate/near-duplicate vertices can cause division by zero or excessive densification |
| Euclidean degree-space nearest neighbours are adequate | Longitude/latitude scale distortion can change the selected point, especially near parallel segments |
| Every AVL point belongs on the reference path | Off-route, garage, terminal, or detour pings are forced onto the route |
| Clean-file `TripID` is correct and temporally unambiguous | APC loads can be assigned to the wrong physical trip |
| APC file order remains globally sorted by `StopArrivalTime` | `merge_asof` can raise a “keys must be sorted” error; the code does not sort APC explicitly |
| Any prior APC row is acceptable | Stale loads can be carried forward because no maximum lag is enforced |
| `Arrload` is the desired occupancy state | Between-stop occupancy may differ from departure load after boarding/alighting |
| Missing load means zero and fractional load can be truncated | Missingness and legitimate zero are conflated; a small number of averages lose precision |
| Naive local timestamps share the same clock convention | Time matches can be wrong at timezone/DST boundaries or across inconsistent exports |
| Pre-cleaned CSVs are trusted, immutable inputs | The end-to-end result cannot be fully regenerated; undocumented fields cannot be conclusively audited from the raw files alone |

## 10. Snapshot diagnostics

These checks describe the supplied files; they are not validations enforced by the application.

| Check | Observed result |
|---|---:|
| Clean AVL rows / unique `TripID` | 604,449 / 2,721 |
| Clean APC rows / unique `TripID` | 425,591 / 8,995 |
| AVL `TripID` values also present in APC | 2,720 of 2,721 |
| Month-wide unlimited, date-unqualified backward as-of non-null matches | 604,448 of 604,449 AVL rows |
| Same diagnostic join: APC lag greater than one hour | 96 AVL rows |
| Same diagnostic join: maximum observed lag | 9,083 seconds |
| Default Route 29 / 2023-11-28 output rows | 11,856 |
| Default-date median / 90th percentile approximate snap residual | 5.35 m / 342.46 m |
| Default-date maximum approximate snap residual | 653.89 m |

The snap residuals were calculated after the fact using the same selected nearest vertices and a local metre scaling; the production function discards its own tree distance. Large residuals demonstrate why a retained residual and an acceptance threshold are important.

The clean AVL `SteadyState` column contains run lengths through 145, whereas `data/README.md` describes only states 0, 1, and 2. As shown in Section 8, its values exactly follow contiguous runs with `Distance < 80 m` and `KPH < 9`. The plotting code treats exactly 2 as red and all other values as blue, while its text label treats every value greater than zero as “Steady.” The README’s enumeration and bunching interpretation are inconsistent with the supplied artifact.

### 10.1 Visualization interpretation cautions

The plots can mislabel the integrated output even when the merge succeeds:

- animation frames are spaced every three minutes while the title says “1-Minute Steps”;
- chart axes and subtitles label the date as November 28, 2019, although the default data are from 2023;
- `dis` is cumulative metres, but the y-axis is titled “Stop Sequence”; and
- the Altair time slider is defined but never attached to the chart or used as a filter.

## 11. Known execution and reproducibility gaps

1. `main.py` does not run against the checked-in layout: readers default to the project root while files are under `data/raw_data`.
2. `main.py` passes five arguments to `get_shape_directions_ref_dict()`, whose signature accepts four.
3. `main.py` supplies route short name `'501'` as a string, while pandas infers GTFS `route_short_name` as numeric in the supplied feed; equality is type-sensitive.
4. `main.py` requests Route 501, while the supplied operational AVL/APC files cover Routes 29 and 929; correcting only its path and type issues would still yield no operational Route 501 data.
5. The generic shape processor has the initialization/return issues described in Section 7.3.
6. Raw APC route filtering loses its `Route` column before filtering.
7. The active workflow depends on pre-cleaned artifacts with undocumented generation.
8. The processed AVL artifact appears truncated and ends with a malformed partial record, so it does not provide complete raw-month coverage.
9. There is no dependency manifest, automated test suite, data-contract validation, or command-line entry point for the active workflow.
10. The notebooks are visualization drivers; saved outputs are large, and one notebook also contains an unrelated Spark/cars demonstration.
11. `web_gen.py` assigns the plotting function itself to `fig` rather than calling it with integrated data, so it is not a working integration entry point.

## 12. Recommended reproducible specification

To make the intended methodology fully reproducible while preserving its basic design:

1. **Create an explicit raw-to-clean stage.** Version the exact rules that segment AVL/APC records and construct `TripID`. Include `service_date` in the identity contract and record how the third `TripID` component is calculated.
2. **Parameterize GTFS selection.** Resolve `route_short_name` to `route_id`, choose shapes by direction and branch, sort by `shape_pt_sequence`, and record the GTFS feed version. Use a feed contemporaneous with the observations when schedule or stop matching matters.
3. **Align to route segments in metre space.** Preserve raw GPS columns, use a suitable projected coordinate reference system or geodesic segment projection, retain `snap_distance_m`, and reject or flag matches outside a documented tolerance.
4. **Make the temporal join contract explicit.** Sort both inputs, join by a date-qualified trip key, add a maximum tolerance, and preserve a match-status/lag field. Decide explicitly whether occupancy means `Arrload` or `Depload`.
5. **Preserve missingness and numeric meaning.** Do not silently convert unmatched loads to zero or truncate fractional values; add separate `apc_matched` and `apc_match_lag_s` fields.
6. **Validate invariants.** At minimum test key uniqueness, direction agreement, shape monotonicity, timestamp order, stop sequence, route/shape consistency, snapping residuals, APC lag, and output row preservation.
7. **Repair and consolidate entry points.** Make one configuration-driven function or command reproduce the dataset from defined inputs, then have notebooks and the dashboard call that function.

## 13. Source map

| File | Relevance |
|---|---|
| [`data/data_preprocess.py`](../data/data_preprocess.py) | Active shape densification, AVL filtering/snapping, and AVL–APC as-of join |
| [`utils/interpolate.py`](../utils/interpolate.py) | Geodesic distances, route densification, KD-tree snap, optional AVL temporal interpolation |
| [`utils/read_gtfs.py`](../utils/read_gtfs.py) | Generic GTFS ingestion, schedule-time correction, stop and shape reference builders |
| [`utils/read_avl_and_apc.py`](../utils/read_avl_and_apc.py) | Generic raw AVL/APC ingestion and service-day logic |
| [`main.py`](../main.py) | Incomplete generic driver; does not perform the final integration |
| [`plot_motion.py`](../plot_motion.py) | Visual consumers of `dis`, `pc`, vehicle, and time fields |
| [`datainterpolate.ipynb`](../datainterpolate.ipynb) | Primary exploratory driver of the active workflow |
| [`datainterpolate copy.ipynb`](<../datainterpolate copy.ipynb>) | Shorter duplicate visualization workflow |
| [`data/README.md`](../data/README.md) | Data catalog and a concise as-of join example; some schema semantics differ from artifacts |
| [`intro.md`](../intro.md) | Intended/narrative methodology; includes steps not backed by executable source |
| [`image.png`](../image.png) | Intended workflow diagram |
| [`utils/align.py`](../utils/align.py) | Empty placeholder |

## 14. Compact pseudocode for the active integration

```text
function Integrate(route = 29, date_key = 20231128):
    shape_raw = GTFS.shapes where shape_id == 832096

    distances = geodesic distance of each adjacent shape pair
    d_min = minimum(distances)
    shape_dense = []
    for each adjacent pair (p_i, p_next):
        append p_i
        insert ceil(distance(p_i, p_next) / d_min) - 1
            linearly spaced latitude/longitude points
    append final point
    shape_dense.dis = cumulative geodesic distance from first point

    avl = read pre-cleaned AVL
    avl = avl where Route == route and Date_Key == date_key
    avl = sort avl by Message_DateTime

    tree = cKDTree(shape_dense[latitude, longitude])
    nearest = tree.query(avl[Latitude, Longitude], k = 1)
    overwrite avl coordinates from shape_dense[nearest]
    avl.dis = shape_dense[nearest].dis

    apc = read pre-cleaned APC[TripID, StopArrivalTime, Arrload]
    merged = backward merge_asof(
        left = avl,
        right = apc,
        by = TripID,
        left_time = Message_DateTime,
        right_time = StopArrivalTime,
        tolerance = None  # no tolerance is supplied by the implementation
    )

    merged.pc = integer(merged.Arrload, with missing replaced by 0)
    return merged
```
