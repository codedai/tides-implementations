# Experiment 05: progress plateaus and credible observation endpoints

`tools/experiment05/plateau.py` sits after sequence projection and before APC
trip membership and final GTFS matching. It does not read the final
candidate/winner, and it does not create new performed trip IDs. Resuming travel
after an internal stop remains part of the same segment; the segmentation logic
for direction, joint data gaps and progress resets is handled separately by
`segmentation.py`.

The interface is `assess_trip_extent(points, apc, canonical, config)`, returning
`{extent, points, diagnostics}`. All raw AVL rows, raw coordinates, UTC epoch
times, projection alternatives and fragment IDs are preserved. APC serves only
as genuine stop/time-interval evidence; no GPS points are fabricated.

## Diagnose first; a "flat s curve" is not equated with a stop

Non-overlapping atomic windows are used. Each window records the radius of the
raw coordinates, cumulative movement distance, speed between adjacent points,
the progress range/order for each reference, projection status and check
completeness. Too few points or a time gap cannot form an independent valid
window; genuinely co-located points in a trailing incomplete window may only
supplement an existing continuous plateau and do not receive an independent
evidence weight.

When a valid direction/reference alternative shows advance, or the raw
coordinates genuinely move, stationarity cannot be concluded:

| State | Meaning and handling |
|---|---|
| `NORMAL` | Normal progress, or the sustained plateau duration has not yet been reached |
| `STATIONARY_PLATEAU` | Window simultaneously shows raw spatial stationarity and stalled canonical progress |
| `TERMINAL_LAYOVER` | Sustained stationarity close to a valid terminal / short-turn endpoint |
| `LONG_MIDROUTE_STOP` | Sustained mid-route stop; not automatically censored |
| `FROZEN_TELEMETRY` | Enough repeated coordinates contradicting reported speed; only a telemetry-fault judgment, does not confirm end of service |
| `OFF_REFERENCE_MOVING` | Raw coordinates move, but no valid reference supports it |
| `REFERENCE_MISMATCH` | Current progress is static while the raw position or another valid reference clearly advances |
| `RECOVERED_AFTER_PLATEAU` | Subsequent valid continuous progress recovery in the same or opposite direction; original segment retained |
| `RIGHT_CENSORED_TAIL` | Credible observation-endpoint censoring after all of the conditions below hold |

Exactly repeated GPS coordinates by themselves do not prove a frozen device;
they may also be a genuine stop or coordinate quantization, and this
indistinguishability must be reported. Advance on an alternative reference
blocks censoring and is recorded in diagnostics; this module does not replace
the projection reference or direction on its own.

## Necessary conditions for censoring

The effective endpoint changes only when all of the following conditions hold
simultaneously:

1. Continuous plateau evidence supports genuine stationarity, or a
   frozen-telemetry fault, rather than raw-moving/flat-progress.
2. Every observation within the plateau declares that all references were
   checked, and the complete alternative list covers the current canonical
   bundle; checking only the current direction is insufficient.
3. The raw position and canonical progress are both close to a valid terminal.
   The terminal set includes not only the geometric endpoints of the reference
   but also the first and last stops of all actual scheduled trips, and
   therefore covers short turns.
4. There is no observation time gap, ambiguous time, missing right-boundary
   marker or subsequent valid recovery; an internal plateau is not a new
   boundary.
5. There is no unexplained APC service: service at a different location,
   unknown boarding/alighting, or ambiguous time blocks censoring. Valid
   passenger exchange at the same location moves the idle start later, to the
   last APC departure; if the raw coordinates conflict with the stop code, the
   stop code cannot override that spatial counter-evidence. The absence of APC
   activity only means "not observed", not proof that no actual passenger
   service occurred.
6. Sustained idle reaches the adaptive duration, and there is enough continuous
   observation to cover the recovery lookahead.
7. All high-recall GTFS hypotheses that remain eligible for testing pass the
   counterfactual advance check.

The local passage headway uses scheduled passages near the same location, not
the pattern-private headway of the finally matched trip. When samples are
insufficient, the fallback from the TOML is used explicitly. The sustained
threshold is:

`min(censor_max_duration_s, max(censor_min_duration_s,`
`headway × censor_headway_multiplier + censor_terminal_dwell_allowance_s))`.

The actual decision additionally requires `recovery_lookahead_s` to be reached,
and considers the idle duration after the last co-located APC service.

## A free delay process is not allowed to explain the whole plateau

Counterfactual retrieval uses only the valid reference, observed positions and
the high-recall passage-time rescue buffer; it does not read existing candidate
rankings. Each hypothesis fits delay separately using genuine, continuous,
monotone observations from **before the first plateau**. Enough points and
progress are required, and the pre-plateau delay MAD must qualify.

Each prior observation obtains its residual through the unsampled continuous
GTFS passage; dwell uses the arrival/departure residual interval rather than
arbitrarily choosing arrival or departure. Delay is not refitted within the
plateau. The most conservative predicted clock is:

`t_schedule = t_observed − (delay_before_plateau + uncertainty`
`                        + bounded_growth_rate × time_since_plateau)`.

The s that should have been reached under that clock is computed from the GTFS
continuous movement/dwell segments. The check passes only if the most
conservative advance of every eligible hypothesis reaches the configured
threshold. A single long scheduled dwell that can still explain the stop is
enough to veto; a plausible hypothesis without a reliable prior also causes the
result to abstain.

A hypothesis that has already reached the end of its own pattern, or a schedule
that has already finished under the conservative clock, cannot be used to test
subsequent advance and must be counted in excluded counts with an explicit
reason. It is not extrapolated beyond the end of the shape; if all hypotheses
are thereby ineligible for checking, censoring is refused rather than claiming
"all passed".

## Effective endpoint and outputs

`extent` records, respectively:

- `raw_segment_start/end`: the raw observation boundaries.
- `effective_segment_start/end`: the credible evidence range used for matching.
- `plateau_onset_time`, `censor_decision_time`: the first stable instant and
  the instant at which all thresholds were reached.
- `censored_tail_duration_s`, `censor_reason`, `platform_states`,
  `diagnostic_flags`.
- `retained_endpoint_rid`, `retained_apc_rids`: the single retained AVL endpoint
  and the retained APC intervals.

The effective end is traced back to the first stable plateau; if co-located APC
service occurred in the meantime, it is retained through the last departure.
The plateau keeps only one genuine valid AVL endpoint; the remaining plateau
observations are marked `exclude_from_matching=true`, `censored_tail=true` and
`trajectory_included=false`. The APC interval retains its full time span and
replaces the trajectory representation of this stretch of repeated stationary
AVL; the original APC is not modified.

OFF_REFERENCE points that did not pass censoring are still retained with
`exclude_from_matching=false`, continue to enter the coverage denominator of the
spatial evidence, and cannot be dropped by this module for free merely because
projection failed.

`physical_trip_end_confirmed` is always false: what is confirmed here is the
**applicable scope of the evidence**, not operational fact. With no observations
after the tail, it cannot be claimed that the vehicle actually ended service.

## Configuration and verification

All decision thresholds come directly from `[plateau]` in
`config/experiment05-v1.toml`; there are no implicit module defaults. Schedule
uncertainty, projection quality, and headway fallback/minimum gap count use the
existing `[matcher]` values in that configuration. Missing or invalid
configuration fails immediately.

`tests/test_experiment05_plateau.py` uses synthetic physical trajectories to
cover red lights, terminal/short turn, long mid-route stops with recovery, tails
without recovery, frozen coordinates contradicting speed, raw-moving/flat-s,
advance on other references, reverse-direction recovery at a terminal, data
gaps/unclear boundaries, finished hypotheses, long scheduled-dwell
counter-evidence, APC time/space/exchange conflicts, and non-double-counting of
atomic windows. It contains no production rules for any specific vehicle,
route, date, historical trip ID or expected winner.
