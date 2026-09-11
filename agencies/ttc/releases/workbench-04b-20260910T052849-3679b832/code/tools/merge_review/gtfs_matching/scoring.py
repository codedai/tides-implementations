"""Robust partial schedule alignment; fragments are never concatenated."""

from __future__ import annotations

import bisect
import math
import statistics

from .retrieval import for_reference, passage_times, trip_key
from .windows import get_config as window_config
from .windows import observation_key, quality, usable

DEFAULTS = {
    "score_huber_delta": 1.5,
    "score_progress_scale_m": 75,
    "score_lateral_scale_m": 50,
    "score_lateral_weight": 0.3,
    "score_heading_scale_deg": 45,
    "score_heading_weight": 0.1,
    "score_time_band_s": 120,
    "score_time_metric_mps": 2.0,
    "score_reference_step_s": 10,
    "score_max_points": 80,
    "score_delay_step_s": 30,
    "score_delay_search_s": 90,
    "score_delay_jump_penalty": 0.1,
    "score_delay_jump_scale_s": 60,
    "score_max_gap_s": 90,
    "score_backtrack_tolerance_m": 25,
    "score_coverage_penalty": 2.0,
    "score_delay_prior_weight": 0.05,
    "score_warp_min_slope": 0.5,
    "score_warp_max_slope": 2.0,
}


def get_config(config=None):
    result = DEFAULTS | (config or {})
    if result["score_max_points"] < 3:
        raise ValueError("score_max_points must be at least three")
    for name in (
        "score_progress_scale_m",
        "score_lateral_scale_m",
        "score_heading_scale_deg",
        "score_reference_step_s",
        "score_delay_step_s",
        "score_delay_jump_scale_s",
    ):
        if result[name] <= 0:
            raise ValueError(f"{name} must be positive")
    return result


def huber(value, delta=1.5):
    value = abs(value)
    return 0.5 * value * value if value <= delta else delta * (value - delta / 2)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def schedule_knots(trip):
    points = []
    for row in trip.get("scheduled", []):
        s = row.get("s_m", row.get("s"))
        if s is None:
            continue
        for t in (
            row.get("arrival_t", row.get("t")),
            row.get("departure_t", row.get("t")),
        ):
            if t is not None:
                points.append((float(t), float(s)))
    points.sort()
    if any(b[1] < a[1] for a, b in zip(points, points[1:], strict=False)):
        return []
    return sorted(set(points))


def scheduled_progress(knots, t):
    if not knots or t < knots[0][0] or t > knots[-1][0]:
        return None
    index = bisect.bisect_right(knots, (t, math.inf)) - 1
    if index == len(knots) - 1 or knots[index][0] == t:
        return knots[index][1]
    a, b = knots[index], knots[index + 1]
    return a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0])


def fragments(rows, config=None):
    c = get_config(config)
    good = sorted(
        (r for r in rows if r.get("source", "avl") == "avl" and r.get("t") is not None),
        key=lambda r: (r["t"], observation_key(r)),
    )
    output = []
    previous = None
    for row in good:
        if not usable(row, c):
            previous = None
            continue
        if (
            previous is None
            or row.get("scope_separator_before")
            or row.get("reference_id") != previous.get("reference_id")
            or row.get("fragment_id") != previous.get("fragment_id")
            or row["t"] - previous["t"] > c["score_max_gap_s"]
            or row["s_m"] < previous["s_m"] - c["score_backtrack_tolerance_m"]
        ):
            output.append([])
        output[-1].append(row)
        previous = row
    return output


def constrained_dtw(observed, reference, config=None):
    """Subsequence DTW, Huber local cost, time band and bounded warp steps.

    Observations are (delay-corrected t,s). Every observed row participates;
    reference prefix/suffix may be unused. Allowed local index slopes are
    1/2,1,2; steps cannot jump across caller-provided fragments.
    """
    c = get_config(config)
    if not observed or not reference:
        return None
    if any(b[1] < a[1] for a, b in zip(reference, reference[1:], strict=False)):
        return None
    m = len(reference)
    reference_times = [p[0] for p in reference]
    previous = [math.inf] * m
    previous2 = None
    for i, point in enumerate(observed):
        current = [math.inf] * m
        left = bisect.bisect_left(reference_times, point[0] - c["score_time_band_s"])
        right = bisect.bisect_right(reference_times, point[0] + c["score_time_band_s"])
        for j in range(left, right):
            target = reference[j]
            dt = abs(point[0] - target[0])
            if dt > c["score_time_band_s"]:
                continue
            local = huber(
                (point[1] - target[1]) / c["score_progress_scale_m"],
                c["score_huber_delta"],
            )
            local += 0.02 * huber(
                dt / max(c["score_time_band_s"], 1), c["score_huber_delta"]
            )
            local += _projection_cost(point, c)
            local *= point[2] if len(point) > 2 else 1.0
            options = [0.0] if i == 0 else []
            if (
                i > 0
                and j > 0
                and c["score_warp_min_slope"] <= 1 <= c["score_warp_max_slope"]
            ):
                options.append(previous[j - 1])
            if (
                i > 0
                and j > 1
                and c["score_warp_min_slope"] <= 2 <= c["score_warp_max_slope"]
            ):
                options.append(previous[j - 2])
            if (
                i > 1
                and j > 0
                and previous2 is not None
                and c["score_warp_min_slope"] <= 0.5 <= c["score_warp_max_slope"]
            ):
                # Both observed points pay cost; no skipped observation is free.
                omitted = observed[i - 1]
                if abs(omitted[0] - target[0]) <= c["score_time_band_s"]:
                    options.append(
                        previous2[j - 1]
                        + huber(
                            (omitted[1] - target[1]) / c["score_progress_scale_m"],
                            c["score_huber_delta"],
                        )
                        * (omitted[2] if len(omitted) > 2 else 1.0)
                        + _projection_cost(omitted, c)
                        * (omitted[2] if len(omitted) > 2 else 1.0)
                    )
            if options:
                current[j] = local + min(options)
        previous2, previous = previous, current
    best = min(previous)
    weight = sum(p[2] if len(p) > 2 else 1.0 for p in observed)
    return best / weight if math.isfinite(best) and weight > 0 else None


def _projection_cost(point, config):
    lateral = point[3] if len(point) > 3 else None
    heading = point[4] if len(point) > 4 else None
    cost = 0.0
    if isinstance(lateral, (int, float)) and math.isfinite(lateral):
        cost += config["score_lateral_weight"] * huber(
            lateral / config["score_lateral_scale_m"]
        )
    if isinstance(heading, (int, float)) and math.isfinite(heading):
        cost += config["score_heading_weight"] * huber(
            heading / config["score_heading_scale_deg"]
        )
    return cost


def partial_frechet(observed, reference):
    if not observed or not reference:
        return None
    previous = [math.inf] * len(reference)
    for i, point in enumerate(observed):
        current = []
        for j, target in enumerate(reference):
            distance = math.dist(point, target)
            before = [0.0] if i == 0 else [previous[j]]
            if j:
                before.extend([current[j - 1], previous[j - 1]])
            current.append(max(distance, min(before)))
        previous = current
    return min(previous)


def directed_p95_hausdorff(observed, reference):
    if not observed or not reference:
        return None
    return percentile([min(math.dist(p, q) for q in reference) for p in observed], 0.95)


def _sample(rows, cap):
    if len(rows) <= cap:
        return [
            dict(row, _alignment_weight=row.get("_evidence_weight", 1.0))
            for row in rows
        ]
    indexes = sorted({round(i * (len(rows) - 1) / (cap - 1)) for i in range(cap)})
    weights = [0.0] * len(indexes)
    for i, row in enumerate(rows):
        nearest = round(i * (len(indexes) - 1) / (len(rows) - 1))
        weights[nearest] += row.get("_evidence_weight", 1.0)
    return [
        dict(rows[index], _alignment_weight=weight)
        for index, weight in zip(indexes, weights, strict=True)
    ]


def delay_states(rows, trip, config=None):
    c = get_config(config)
    good = [r for r in rows if usable(r, c)]
    estimates = []
    for row in good:
        values = passage_times(trip, row["s_m"])
        if values:
            estimates.append(min((row["t"] - t for t in values), key=abs))
    if not estimates:
        return []
    central = (
        round(statistics.median(estimates) / c["score_delay_step_s"])
        * c["score_delay_step_s"]
    )
    knots = schedule_knots(trip)
    result = []
    for offset in range(
        -int(c["score_delay_search_s"]),
        int(c["score_delay_search_s"]) + 1,
        int(c["score_delay_step_s"]),
    ):
        delay = central + offset
        residuals = [
            huber(
                (r["s_m"] - prediction) / c["score_progress_scale_m"],
                c["score_huber_delta"],
            )
            for r in good
            if (prediction := scheduled_progress(knots, r["t"] - delay)) is not None
        ]
        if residuals:
            coverage = len(residuals) / len(good)
            cost = sum(residuals) / len(residuals) + c["score_coverage_penalty"] * (
                1 - coverage
            )
            cost += c["score_delay_prior_weight"] * abs(delay) / 3600
            result.append({"delay_s": delay, "cost": cost, "coverage": coverage})
    return result


def smooth_delay_viterbi(windows, config=None, *, diagnostics=None):
    c = get_config(config)
    diagnostics = {} if diagnostics is None else diagnostics
    grouped = {}
    for w in windows:
        if not w.get("delay_states"):
            continue
        ids = tuple(w.get("delay_fragment_ids", ["unscoped"]))
        # A context spanning multiple source fragments must not couple them.
        group = ids if len(ids) == 1 else ("independent_context", w["window_id"])
        grouped.setdefault(group, []).append(w)
    output = {}
    for group_index, (group, group_windows) in enumerate(grouped.items()):
        ordered = sorted(
            group_windows,
            key=lambda w: (w.get("delay_anchor_t", w["anchor_t"]), w["window_id"]),
        )
        average_weight = sum(w.get("weight", 1) for w in ordered) / len(ordered)
        block, costs, paths = [], [], []
        block_index = 0

        def commit(block, costs, paths, group, group_index, block_index):
            if not block:
                return
            chosen = min(range(len(costs)), key=costs.__getitem__)
            for index in range(len(block) - 1, -1, -1):
                row = block[index]
                output[row["window_id"]] = row["delay_states"][chosen]["delay_s"]
                diagnostics[row["window_id"]] = {
                    "status": "MONOTONE_PATH"
                    if group[0] != "independent_context"
                    else "INDEPENDENT_MULTI_FRAGMENT_CONTEXT",
                    "block_id": f"delay_path:{group_index}:{block_index}",
                    "anchor_t": row.get("delay_anchor_t", row["anchor_t"]),
                }
                if index:
                    chosen = paths[index - 1][chosen]

        for current in ordered:
            emission_weight = current.get("weight", 1) / max(average_weight, 1e-12)
            if not block:
                block = [current]
                costs = [s["cost"] * emission_weight for s in current["delay_states"]]
                continue
            previous = block[-1]
            previous_t = previous.get("delay_anchor_t", previous["anchor_t"])
            current_t = current.get("delay_anchor_t", current["anchor_t"])
            next_cost, back = [], []
            for state in current["delay_states"]:
                choices = [
                    value
                    + c["score_delay_jump_penalty"]
                    * abs(state["delay_s"] - old["delay_s"])
                    / c["score_delay_jump_scale_s"]
                    if current_t - state["delay_s"] >= previous_t - old["delay_s"]
                    else math.inf
                    for value, old in zip(costs, previous["delay_states"], strict=True)
                ]
                chosen = min(range(len(choices)), key=choices.__getitem__)
                next_cost.append(choices[chosen] + state["cost"] * emission_weight)
                back.append(chosen)
            if not any(math.isfinite(value) for value in next_cost):
                commit(block, costs, paths, group, group_index, block_index)
                diagnostics[current["window_id"]] = {
                    "status": "UNREACHABLE_MONOTONE_TRANSITION",
                    "block_id": None,
                    "anchor_t": current_t,
                }
                # Reject this window, rather than silently choose an impossible
                # state. The next window starts a separately reported path.
                block, costs, paths = [], [], []
                block_index += 1
                continue
            block.append(current)
            costs = next_cost
            paths.append(back)
        commit(block, costs, paths, group, group_index, block_index)
    return output


def score_window(
    rows,
    trip,
    delay_s,
    config=None,
    *,
    window=None,
    point_delays=None,
    full_observation_dtw=False,
):
    c = get_config(config)
    chunks = fragments(rows, c)
    if point_delays is not None:
        supported_chunks = []
        for chunk in chunks:
            current = []
            for row in chunk:
                delay = point_delays.get(observation_key(row))
                if delay is None:
                    if current:
                        supported_chunks.append(current)
                    current = []
                    continue
                current.append(row)
            if current:
                supported_chunks.append(current)
        chunks = supported_chunks
    q = qualified_quality(rows, c, window=window)
    if q["status"] == "NO_EVIDENCE":
        return {**q, "score": None, "fragments": [], "estimated_delay_s": delay_s}
    knots = schedule_knots(trip)
    metrics = []
    for fragment in chunks:
        if len(fragment) < 2:
            continue
        sampled = _sample(fragment, int(c["score_max_points"]))

        def observed_points(source):
            return [
                (
                    r["t"]
                    - (
                        point_delays[observation_key(r)]
                        if point_delays is not None
                        else delay_s
                    ),
                    r["s_m"],
                    r["_alignment_weight"],
                    r.get("lateral_m"),
                    r.get("heading_error_deg"),
                )
                for r in source
            ]

        observed = observed_points(sampled)
        alignment_observed = (
            observed_points(_sample(fragment, len(fragment)))
            if full_observation_dtw
            else observed
        )
        # A delay process cannot manufacture time reversal and then hide it
        # through sorting. The entire affected source fragment fails validation.
        if any(
            b[0] < a[0]
            for a, b in zip(alignment_observed, alignment_observed[1:], strict=False)
        ):
            continue
        lo = max(knots[0][0], observed[0][0] - c["score_time_band_s"]) if knots else 0
        hi = (
            min(knots[-1][0], observed[-1][0] + c["score_time_band_s"]) if knots else -1
        )
        if hi < lo:
            continue
        step = (
            c["score_reference_step_s"]
            if full_observation_dtw
            else max(
                c["score_reference_step_s"],
                (hi - lo) / max(2, c["score_max_points"] * 2),
            )
        )
        spacings = [
            b[0] - a[0]
            for a, b in zip(alignment_observed, alignment_observed[1:], strict=False)
            if b[0] > a[0]
        ]
        if spacings:
            # Index-slope constraints are meaningful only at comparable sample
            # density. Never validate dense original rows against an 80/160-row
            # reference cap and call the resulting structural failure data loss.
            step = min(step, statistics.median(spacings))
        reference = [
            (lo + i * step, scheduled_progress(knots, lo + i * step))
            for i in range(int((hi - lo) / step) + 1)
        ]
        reference = [(t, s) for t, s in reference if s is not None]
        dtw = constrained_dtw(alignment_observed, reference, c)
        if dtw is None:
            continue
        # Time-windowed partial metrics, not comparison to an unobserved full route.
        scale = c["score_time_metric_mps"]
        p = [(point[1], (point[0] - lo) * scale) for point in observed]
        qpoints = [(s, (t - lo) * scale) for t, s in reference]
        frechet = partial_frechet(p, qpoints)
        hausdorff = directed_p95_hausdorff(p, qpoints)
        metric_cost = (
            dtw
            + 0.25 * huber(frechet / c["score_progress_scale_m"])
            + 0.25 * huber(hausdorff / c["score_progress_scale_m"])
        )
        metrics.append(
            {
                "fragment_id": fragment[0].get("fragment_id"),
                "dtw": dtw,
                "partial_frechet_m": frechet,
                "directed_p95_hausdorff_m": hausdorff,
                "score": metric_cost,
                "original_points": len(fragment),
                "scored_points": len(sampled),
                "dtw_scored_points": len(alignment_observed),
                "all_original_points_dtw_validated": full_observation_dtw,
                "source_ids": [observation_key(r) for r in fragment],
                "source_start": fragment[0]["t"],
                "source_end": fragment[-1]["t"],
                "aligned_duration_s": fragment[-1]["t"] - fragment[0]["t"],
                "sampled_source_ids": [observation_key(r) for r in sampled],
                "sampled_weights": [r["_alignment_weight"] for r in sampled],
                "weight": sum(r.get("_evidence_weight", 1.0) for r in fragment),
            }
        )
    if not metrics:
        return {
            "status": "NO_EVIDENCE",
            "reasons": ["NO_CONSTRAINED_ALIGNMENT"],
            "score": None,
            "fragments": [],
            "estimated_delay_s": delay_s,
            "schedule_aligned_point_count": 0,
            "schedule_aligned_point_ratio": 0,
            "schedule_aligned_duration_s": 0,
            "schedule_aligned_duration_ratio": 0,
        }
    n = sum(m["weight"] for m in metrics)
    raw_count = sum(r.get("source", "avl") == "avl" for r in rows)
    coverage = (
        sum(m["original_points"] for m in metrics) / raw_count if raw_count else 0
    )
    discarded = 1 - coverage
    timed = [
        r["t"]
        for r in rows
        if r.get("source", "avl") == "avl" and r.get("t") is not None
    ]
    duration = (
        window["end"] - window["start"]
        if window
        else max(timed, default=0) - min(timed, default=0)
    )
    aligned_duration = sum(m["aligned_duration_s"] for m in metrics)
    cost = (
        sum(m["score"] * m["weight"] for m in metrics) / n
        + c["score_coverage_penalty"] * discarded
    )
    return {
        **coverage_metrics(rows, c, window=window),
        "status": "EVIDENCE",
        "score": cost,
        "estimated_delay_s": delay_s,
        "coverage": coverage,
        "schedule_aligned_point_count": sum(m["original_points"] for m in metrics),
        "schedule_aligned_point_ratio": coverage,
        "schedule_aligned_duration_s": aligned_duration,
        "schedule_aligned_duration_ratio": aligned_duration / duration
        if duration > 0
        else 0,
        "alignment_coverage_definition": (
            "Original source rows in successful constrained DTW fragments; "
            "duration is original adjacent-source time, never across gaps. "
            "Full-row validation is explicit per fragment."
        ),
        "discarded_coverage_penalty": c["score_coverage_penalty"] * discarded,
        "fragments": metrics,
        "quality": q,
        "scoring_sampled": any(
            m["scored_points"] < m["original_points"] for m in metrics
        ),
        "apc_used_as_gps": False,
    }


def weighted_isotonic(values, weights):
    """Weighted least-squares projection onto a nondecreasing clock (PAVA)."""
    if not values:
        return []
    origin = values[0]
    blocks = []
    for index, (value, weight) in enumerate(zip(values, weights, strict=True)):
        if not math.isfinite(value) or not math.isfinite(weight) or weight <= 0:
            raise ValueError(
                "Isotonic clock requires finite values and positive weights"
            )
        blocks.append([index, index + 1, weight, (value - origin) * weight])
        while (
            len(blocks) > 1
            and blocks[-2][3] / blocks[-2][2] > blocks[-1][3] / blocks[-1][2]
        ):
            right = blocks.pop()
            left = blocks[-1]
            left[1] = right[1]
            left[2] += right[2]
            left[3] += right[3]
    fitted = [0.0] * len(values)
    for first, end, weight, total in blocks:
        fitted[first:end] = [origin + total / weight] * (end - first)
    return fitted


def _interpolate_clock(knots, t, field):
    i = bisect.bisect_right([k["t"] for k in knots], t)
    if i == 0:
        return knots[0][field] + t - knots[0]["t"]
    if i == len(knots):
        return knots[-1][field] + t - knots[-1]["t"]
    a, b = knots[i - 1], knots[i]
    return a[field] + (b[field] - a[field]) * (t - a["t"]) / (b["t"] - a["t"])


def score_whole_with_shared_delay(rows, trip, local, config=None):
    """Validate every source point against the same fragment-local delay process.

    Knots come only from already selected Viterbi window delays and successful
    window fragments. Interpolation/endpoint holding estimates delay, not GPS.
    A row without a successful supporting window remains unsupported.
    """
    process, delays, fit_fragments = [], {}, []
    successful = [w for w in local if w.get("score") is not None]
    for index, chunk in enumerate(fragments(rows, config)):
        ids = {observation_key(r) for r in chunk}
        supported_by = {}
        contributing = []
        for window in successful:
            aligned = {
                key
                for fragment in window.get("fragments", [])
                for key in fragment.get("source_ids", [])
            }
            shared = ids & aligned
            if not shared:
                continue
            contributing.append((window, shared, len(aligned)))
            for key in shared:
                supported_by.setdefault(key, []).append(window["window_id"])
        runs, active = [], []
        for row in chunk:
            key = observation_key(row)
            if key in supported_by:
                active.append(row)
            else:
                if active:
                    runs.append(active)
                    active = []
                process.append(
                    {
                        "source_id": key,
                        "source_rid": row.get("source_rid", row.get("rid")),
                        "t": row["t"],
                        "fragment_id": None,
                        "delay_s": None,
                        "raw_delay_s": None,
                        "constraint_adjustment_s": None,
                        "corrected_t": None,
                        "supporting_window_ids": [],
                        "status": "NO_SUPPORTED_WINDOW",
                        "delay_fit_status": "NO_FIT",
                    }
                )
        if active:
            runs.append(active)
        for run_index, run in enumerate(runs):
            source_times = {observation_key(r): r["t"] for r in run}
            grouped_knots = {}
            for window, shared, total_ids in contributing:
                times = [source_times[key] for key in shared if key in source_times]
                if not times:
                    continue
                # This anchor belongs to actual contributing observations, not
                # a context centre outside the observed fragment/run.
                anchor = statistics.median(times)
                weight = max(
                    window.get("weight", 1) * len(times) / max(total_ids, 1), 1e-12
                )
                grouped_knots.setdefault(anchor, []).append(
                    (window["estimated_delay_s"], weight)
                )
            knots = []
            for t, estimates in sorted(grouped_knots.items()):
                weight = sum(w for _, w in estimates)
                delay = sum(d * w for d, w in estimates) / weight
                knots.append(
                    {
                        "t": t,
                        "raw_delay_s": delay,
                        "raw_corrected_t": t - delay,
                        "weight": weight,
                    }
                )
            fitted = weighted_isotonic(
                [k["raw_corrected_t"] for k in knots], [k["weight"] for k in knots]
            )
            adjusted = False
            for knot, corrected_t in zip(knots, fitted, strict=True):
                knot.update(
                    corrected_t=corrected_t,
                    delay_s=knot["t"] - corrected_t,
                    constraint_adjustment_s=knot["raw_corrected_t"] - corrected_t,
                )
                adjusted |= abs(knot["constraint_adjustment_s"]) > 1e-7
            fragment_id = f"whole_fragment:{index:04d}:{run_index:04d}"
            fit_status = (
                "MONOTONE_CONSTRAINT_ADJUSTED"
                if adjusted
                else "MONOTONE_ALREADY_FEASIBLE"
            )
            fit_fragments.append(
                {
                    "id": fragment_id,
                    "source_ids": list(source_times),
                    "start": run[0]["t"],
                    "end": run[-1]["t"],
                    "knots": knots,
                    "status": fit_status,
                    "fallback": False,
                    "method": "weighted_isotonic_projection_of_soft_viterbi_clock",
                }
            )
            for row in run:
                key, t = observation_key(row), row["t"]
                raw_clock = _interpolate_clock(knots, t, "raw_corrected_t")
                corrected_t = _interpolate_clock(knots, t, "corrected_t")
                delay = t - corrected_t
                delays[key] = delay
                process.append(
                    {
                        "source_id": key,
                        "source_rid": row.get("source_rid", row.get("rid")),
                        "t": t,
                        "fragment_id": fragment_id,
                        "delay_s": delay,
                        "raw_delay_s": t - raw_clock,
                        "constraint_adjustment_s": raw_clock - corrected_t,
                        "corrected_t": corrected_t,
                        "supporting_window_ids": supported_by[key],
                        "status": "SUPPORTED",
                        "delay_fit_status": fit_status,
                    }
                )
    recorded = {r["source_id"] for r in process}
    process.extend(
        {
            "source_id": observation_key(row),
            "source_rid": row.get("source_rid", row.get("rid")),
            "t": row.get("t"),
            "fragment_id": None,
            "delay_s": None,
            "raw_delay_s": None,
            "constraint_adjustment_s": None,
            "corrected_t": None,
            "supporting_window_ids": [],
            "status": "EXCLUDED_PROJECTION",
            "delay_fit_status": "NO_FIT",
        }
        for row in rows
        if row.get("source", "avl") == "avl" and observation_key(row) not in recorded
    )
    result = score_window(
        rows, trip, None, config, point_delays=delays, full_observation_dtw=True
    )
    aligned_ids = {
        key
        for fragment in result.get("fragments", [])
        for key in fragment["source_ids"]
    }
    for row in process:
        row["schedule_aligned"] = row["source_id"] in aligned_ids
    process.sort(key=lambda row: (row["t"] is None, row["t"] or 0, row["source_id"]))
    result.update(
        delay_process=process,
        delay_fit_fragments=fit_fragments,
        delay_constraint_adjusted_point_count=sum(
            abs(r.get("constraint_adjustment_s") or 0) > 1e-7 for r in process
        ),
        delay_constraint_max_adjustment_s=max(
            (abs(r.get("constraint_adjustment_s") or 0) for r in process), default=0
        ),
        delay_model=(
            "Monotone-anchor Viterbi delays followed by weighted isotonic fitting "
            "of the corrected clock at actual source anchors, separately in "
            "each supported fragment. Raw estimates and all adjustments retained; "
            "interpolation estimates delay, not GPS; no source rows are removed."
        ),
        full_original_point_validation_required=True,
        all_original_points_dtw_validated=bool(process)
        and len(aligned_ids) == len(process),
        apc_used_as_gps=False,
    )
    return result


def evaluate_candidate(observations, window_bundle, trip, config=None):
    c = get_config(config)
    trace = for_reference(
        observations, trip.get("reference_id"), include_unprojectable=True
    )
    by_id = {observation_key(r): r for r in trace}
    fragment_by_id = {
        observation_key(row): f"reference_fragment:{index:04d}"
        for index, chunk in enumerate(fragments(trace, c))
        for row in chunk
    }
    local = []
    for window in window_bundle["windows"]:
        rows = [
            dict(by_id[key], _evidence_weight=window["weights"][key])
            for key in window["observation_ids"]
            if key in by_id
        ]
        states = (
            delay_states(rows, trip, c)
            if qualified_quality(rows, c, window=window)["status"] == "EVIDENCE"
            else []
        )
        source_times = [r["t"] for r in rows if observation_key(r) in fragment_by_id]
        local.append(
            {
                "candidate_id": trip_key(trip),
                "window_id": window["id"],
                "kind": window["kind"],
                "anchor_t": window["anchor_t"],
                "delay_anchor_t": statistics.median(source_times)
                if source_times
                else window["anchor_t"],
                "delay_fragment_ids": sorted(
                    {
                        fragment_by_id[observation_key(r)]
                        for r in rows
                        if observation_key(r) in fragment_by_id
                    }
                ),
                "start": window["start"],
                "end": window["end"],
                "weight": window["avl_weight"],
                "delay_states": states,
                "rows": rows,
                "quality_eligible": qualified_quality(rows, c, window=window)["status"]
                == "EVIDENCE",
            }
        )
    delay_path_diagnostics = {}
    delays = smooth_delay_viterbi(local, c, diagnostics=delay_path_diagnostics)
    for window in local:
        rows = window.pop("rows")
        window.update(
            score_window(rows, trip, delays[window["window_id"]], c, window=window)
            if window["window_id"] in delays
            else {
                "status": "NO_EVIDENCE",
                "score": None,
                "reasons": ["NO_SUPPORTED_DELAY_STATE"],
            }
        )
        window.update(coverage_metrics(rows, c, window=window))
        window["delay_path"] = delay_path_diagnostics.get(
            window["window_id"],
            {"status": "NO_SUPPORTED_DELAY_STATE", "block_id": None},
        )
        if window["delay_path"]["status"] == "UNREACHABLE_MONOTONE_TRANSITION":
            window["reasons"] = ["NO_REACHABLE_MONOTONE_DELAY_STATE"]
    return local


def coverage_metrics(rows, config=None, *, window=None):
    c = get_config(config)
    raw = sorted(
        (r for r in rows if r.get("source", "avl") == "avl" and r.get("t") is not None),
        key=lambda r: r["t"],
    )
    all_avl = [r for r in rows if r.get("source", "avl") == "avl"]
    groups = fragments(rows, c)
    raw_duration = (
        window["end"] - window["start"]
        if window
        else raw[-1]["t"] - raw[0]["t"]
        if raw
        else 0
    )
    valid_duration = sum(g[-1]["t"] - g[0]["t"] for g in groups)
    progress = sum(
        max(0, b["s_m"] - a["s_m"])
        for g in groups
        for a, b in zip(g, g[1:], strict=False)
    )
    return {
        "valid_projection_point_ratio": sum(len(g) for g in groups) / len(all_avl)
        if all_avl
        else 0,
        "valid_projection_duration_ratio": valid_duration / raw_duration
        if raw_duration
        else 0,
        "valid_projection_duration_s": valid_duration,
        "valid_projection_progress": progress,
        "off_reference_ratio": sum(
            r.get("projection_status") == "OFF_REFERENCE" for r in all_avl
        )
        / len(all_avl)
        if all_avl
        else 0,
        "longest_valid_contiguous_run": max(
            (g[-1]["t"] - g[0]["t"] for g in groups), default=0
        ),
        "longest_valid_contiguous_run_points": max((len(g) for g in groups), default=0),
        "coverage_duration_definition": "Valid adjacent-source duration divided "
        "by window duration (whole case: source time span); no bridge over "
        "rejected rows, scope separators or fragments.",
    }


def qualified_quality(rows, config=None, *, window=None):
    c = window_config(config)
    result = quality(rows, c) | coverage_metrics(rows, c, window=window)
    checks = [
        (result["point_count"], c["window_min_points"], "INSUFFICIENT_POINTS"),
        (
            result["valid_projection_duration_s"],
            c["window_min_duration_s"],
            "INSUFFICIENT_VALID_DURATION",
        ),
        (
            result["valid_projection_progress"],
            c["window_min_progress_m"],
            "INSUFFICIENT_PROGRESS",
        ),
        (
            result["valid_projection_point_ratio"],
            c["window_min_point_ratio"],
            "INSUFFICIENT_POINT_COVERAGE",
        ),
        (
            result["valid_projection_duration_ratio"],
            c["window_min_duration_ratio"],
            "INSUFFICIENT_DURATION_COVERAGE",
        ),
    ]
    reasons = [name for value, threshold, name in checks if value < threshold]
    return result | {
        "reasons": reasons,
        "status": "NO_EVIDENCE" if reasons else "EVIDENCE",
    }
