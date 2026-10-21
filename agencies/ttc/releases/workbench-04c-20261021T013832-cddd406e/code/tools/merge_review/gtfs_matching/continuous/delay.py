"""One robust physical-time delay process per performed-trip/candidate pair.

The data objective is integrated with temporal support, not counted once per
sample or overlapping window. IRLS solves a robust first-order state-space
objective with a meaningful headway-normalized occurrence prior. Fragment
separators remove state transitions, not source observations from coverage.
"""

from __future__ import annotations

import bisect
import math

from . import operational_prior
from .evidence import finite, identity, temporal_weights

DEFAULTS = {
    "continuous_delay_prior_weight": 1.0,
    "continuous_delay_smooth_weight": 0.3,
    "continuous_delay_rate_scale_s_per_s": 0.2,
    "continuous_delay_max_rate_s_per_s": 0.95,
    "continuous_delay_max_decrease_rate_s_per_s": 3.0,
    "continuous_delay_max_rate_s_per_m": 0.5,
    "continuous_delay_stationary_rate_s_per_s": 0.95,
    "continuous_delay_huber_delta": 1.5,
    "continuous_delay_min_uncertainty_s": 15.0,
    "continuous_delay_default_headway_s": 600.0,
    "continuous_delay_min_headway_s": 60.0,
    "continuous_delay_max_gap_s": 90.0,
    "continuous_delay_max_iterations": 40,
    "continuous_delay_tolerance_s": 0.02,
    "continuous_delay_historical_prior": None,
    "continuous_occurrence_model_enabled": False,
    "continuous_delay_atomic_duration_s": 60.0,
    "continuous_delay_min_event_spacing_s": 15.0,
    "continuous_delay_dynamic_uncertainty_floor_s": 20.0,
    "continuous_delay_projection_speed_floor_mps": 2.0,
    "continuous_delay_terminal_uncertainty_s": 30.0,
    "continuous_delay_stop_uncertainty_s": 10.0,
    "continuous_delay_gap_uncertainty_s_per_s": 0.25,
    "continuous_delay_gap_uncertainty_after_s": 30.0,
}
DEFAULTS.update(operational_prior.DEFAULTS)


def get_config(config=None):
    c = DEFAULTS | (config or {})
    for key, default in DEFAULTS.items():
        if default is None:
            continue
        if isinstance(default, bool):
            if not isinstance(c[key], bool):
                raise ValueError(f"{key} must be a boolean")
            continue
        if not finite(c[key]) or c[key] < 0:
            raise ValueError(f"{key} must be finite and nonnegative")
    for key in (
        "continuous_delay_rate_scale_s_per_s",
        "continuous_delay_min_uncertainty_s",
        "continuous_delay_default_headway_s",
        "continuous_delay_min_headway_s",
        "continuous_delay_huber_delta",
        "continuous_delay_max_gap_s",
        "continuous_delay_atomic_duration_s",
        "continuous_delay_projection_speed_floor_mps",
    ):
        if c[key] <= 0:
            raise ValueError(f"{key} must be positive")
    if c["continuous_delay_max_rate_s_per_s"] >= 1:
        raise ValueError(
            "Maximum delay rate must be below one to preserve corrected-time order"
        )
    if (
        not isinstance(c["continuous_delay_max_iterations"], int)
        or c["continuous_delay_max_iterations"] < 1
    ):
        raise ValueError("Delay iteration count must be a positive integer")
    return c


def huber(value, delta=1.5):
    value = abs(value)
    return value * value / 2 if value <= delta else delta * (value - delta / 2)


def weighted_quantile(values, weights, fraction=0.5):
    pairs = sorted(
        (value, weight)
        for value, weight in zip(values, weights, strict=False)
        if finite(value) and finite(weight) and weight > 0
    )
    if not pairs:
        return None
    target = sum(weight for _, weight in pairs) * fraction
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= target:
            return value
    return pairs[-1][0]


def _nearest(value, lower, upper):
    return max(lower, min(upper, value))


def _robust_weight(value, delta):
    return 1.0 if abs(value) <= delta else delta / abs(value)


def _solve_tridiagonal(diagonal, off_diagonal, right):
    """Positive-definite scalar state-space normal equations, O(n)."""
    d, b = list(diagonal), list(right)
    for index in range(1, len(d)):
        multiplier = off_diagonal[index - 1] / max(d[index - 1], 1e-18)
        d[index] -= multiplier * off_diagonal[index - 1]
        b[index] -= multiplier * b[index - 1]
    result = [0.0] * len(d)
    if result:
        result[-1] = b[-1] / max(d[-1], 1e-18)
    for index in range(len(d) - 2, -1, -1):
        result[index] = (b[index] - off_diagonal[index] * result[index + 1]) / max(
            d[index], 1e-18
        )
    return result


def _history(c):
    prior = c.get("continuous_delay_historical_prior")
    if not isinstance(prior, dict):
        return None
    if not (
        prior.get("independently_validated") is True
        and prior.get("validation_artifact")
        and finite(prior.get("median_s"))
        and finite(prior.get("scale_s"))
        and prior["scale_s"] > 0
    ):
        raise ValueError(
            "Historical delay prior requires independent validation provenance, "
            "finite median and positive scale"
        )
    return prior


def fit_delay(rows, config=None, *, operational_context=None):
    """Dispatch without changing legacy floating-point operations or outputs."""
    c = get_config(config)
    if c["continuous_occurrence_model_enabled"]:
        return _fit_operational_delay(rows, c, operational_context)
    return _fit_legacy_delay(rows, config)


def _fit_legacy_delay(rows, config=None):
    """Fit once over all supplied candidate evidence, never separately by window.

    Required rows: t, s_m, residual_lower_s, residual_upper_s, source identity.
    Optional weight is dimensionless quality (duration weights are internal).
    Intervals encode scheduled dwell exactly; no arbitrary arrival/departure
    choice enters the data loss. Metadata such as upstream OTP is not read.
    """
    c = get_config(config)
    history = _history(c)
    accepted, excluded = [], []
    for row in rows:
        required = ("t", "s_m", "residual_lower_s", "residual_upper_s")
        if row.get("source", "avl").lower() != "avl":
            excluded.append(
                {
                    "source_id": identity(row),
                    "reason": "APC_IS_INTERVAL_EVIDENCE_NOT_AN_AVL_TRAJECTORY_POINT",
                }
            )
        elif (
            not all(finite(row.get(key)) for key in required)
            or row["residual_lower_s"] > row["residual_upper_s"]
        ):
            excluded.append(
                {"source_id": identity(row), "reason": "INVALID_RESIDUAL_INTERVAL"}
            )
        elif (
            not finite(row.get("weight", row.get("quality", 1.0)))
            or row.get("weight", row.get("quality", 1.0)) <= 0
        ):
            excluded.append(
                {
                    "source_id": identity(row),
                    "reason": "ZERO_OR_INVALID_EVIDENCE_WEIGHT",
                }
            )
        else:
            accepted.append(dict(row))
    accepted.sort(key=lambda row: (row["t"], identity(row)))
    if not accepted:
        return {
            "status": "NO_EVIDENCE",
            "rows": [],
            "components": {},
            "excluded": excluded,
            "delay_median_s": None,
            "delay_mad_s": None,
            "delay_uncertainty_s": None,
            "diagnostics": ["NO_VALID_CONTINUOUS_RESIDUALS"],
        }
    support = temporal_weights(accepted, c)
    weights = [
        support[identity(row)] * min(1.0, row.get("weight", row.get("quality", 1.0)))
        for row in accepted
    ]
    total_weight = sum(weights)
    weights = [weight / total_weight for weight in weights]
    sigma = [
        max(
            c["continuous_delay_min_uncertainty_s"],
            row.get("uncertainty_s")
            if finite(row.get("uncertainty_s"))
            else c["continuous_delay_min_uncertainty_s"],
        )
        for row in accepted
    ]
    headway = [
        max(
            c["continuous_delay_min_headway_s"],
            row.get("headway_s")
            if finite(row.get("headway_s")) and row["headway_s"] > 0
            else c["continuous_delay_default_headway_s"],
        )
        for row in accepted
    ]
    prior_centres = [history["median_s"] if history else 0.0] * len(accepted)
    prior_scales = [history["scale_s"] if history else value for value in headway]
    initial = [
        (
            _nearest(
                prior_centres[index], row["residual_lower_s"], row["residual_upper_s"]
            )
        )
        for index, row in enumerate(accepted)
    ]
    delay = initial[:]
    links = []
    for index, (first, second) in enumerate(zip(accepted, accepted[1:], strict=False)):
        dt = second["t"] - first["t"]
        same_fragment = (first.get("fragment_id"), first.get("reference_id")) == (
            second.get("fragment_id"),
            second.get("reference_id"),
        )
        if (
            not same_fragment
            or second.get("scope_separator_before")
            or dt > c["continuous_delay_max_gap_s"]
        ):
            links.append(None)
            continue
        ds = abs(second["s_m"] - first["s_m"])
        progress_allowance = (
            c["continuous_delay_stationary_rate_s_per_s"] * dt
            + c["continuous_delay_max_rate_s_per_m"] * ds
        )
        maximum_increase = min(
            c["continuous_delay_max_rate_s_per_s"] * dt, progress_allowance
        )
        maximum_decrease = min(
            c["continuous_delay_max_decrease_rate_s_per_s"] * dt, progress_allowance
        )
        links.append(
            {
                "dt": max(dt, 1e-6),
                "max_increase": maximum_increase,
                "max_decrease": maximum_decrease,
                "index": index,
            }
        )
    linked_duration = max(1.0, sum(link["dt"] for link in links if link is not None))
    converged = False
    constrained_count = 0
    for iteration in range(c["continuous_delay_max_iterations"]):
        iteration_count = iteration + 1
        diagonal, right = [1e-14] * len(accepted), [0.0] * len(accepted)
        off = [0.0] * max(0, len(accepted) - 1)
        for index, row in enumerate(accepted):
            target = _nearest(
                delay[index], row["residual_lower_s"], row["residual_upper_s"]
            )
            error = (delay[index] - target) / sigma[index]
            # A true dwell interval has zero data curvature in its interior.
            active = (
                row["residual_lower_s"] == row["residual_upper_s"] or abs(error) > 1e-10
            )
            coefficient = (
                weights[index]
                * _robust_weight(error, c["continuous_delay_huber_delta"])
                / sigma[index] ** 2
                if active
                else 0.0
            )
            diagonal[index] += coefficient
            right[index] += coefficient * target
            prior_error = (delay[index] - prior_centres[index]) / prior_scales[index]
            coefficient = (
                c["continuous_delay_prior_weight"]
                * weights[index]
                * _robust_weight(prior_error, c["continuous_delay_huber_delta"])
                / prior_scales[index] ** 2
            )
            diagonal[index] += coefficient
            right[index] += coefficient * prior_centres[index]
        for index, link in enumerate(links):
            if link is None:
                continue
            scale = link["dt"] * c["continuous_delay_rate_scale_s_per_s"]
            rate = (delay[index + 1] - delay[index]) / scale
            coefficient = (
                c["continuous_delay_smooth_weight"]
                * link["dt"]
                / linked_duration
                * _robust_weight(rate, c["continuous_delay_huber_delta"])
                / scale**2
            )
            diagonal[index] += coefficient
            diagonal[index + 1] += coefficient
            off[index] = -coefficient
        proposal = _solve_tridiagonal(diagonal, off, right)
        # Bidirectional projection to physical slew bounds. No reference-array
        # indices or resampling frequencies enter this feasibility constraint.
        for _ in range(4):
            for sequence in (range(len(links)), range(len(links) - 1, -1, -1)):
                for index in sequence:
                    link = links[index]
                    if link is None:
                        continue
                    difference = proposal[index + 1] - proposal[index]
                    maximum = (
                        link["max_increase"]
                        if difference >= 0
                        else link["max_decrease"]
                    )
                    excess = abs(difference) - maximum
                    if excess > 1e-9:
                        direction = 1 if difference > 0 else -1
                        mass = weights[index] + weights[index + 1]
                        proposal[index] += (
                            direction * excess * weights[index + 1] / mass
                        )
                        proposal[index + 1] -= (
                            direction * excess * weights[index] / mass
                        )
                        constrained_count += 1
        change = max(abs(a - b) for a, b in zip(proposal, delay, strict=False))
        delay = proposal
        if change < c["continuous_delay_tolerance_s"]:
            converged = True
            break
    # A final forward pass guarantees feasibility even on a dense long chain;
    # report any active constraints, do not pretend incompatible data aligned.
    for index, link in enumerate(links):
        if link is not None:
            delay[index + 1] = _nearest(
                delay[index + 1],
                delay[index] - link["max_decrease"],
                delay[index] + link["max_increase"],
            )
    conditioned = [
        delay[index]
        - _nearest(delay[index], row["residual_lower_s"], row["residual_upper_s"])
        for index, row in enumerate(accepted)
    ]
    fit_costs = [
        huber(value / uncertainty, c["continuous_delay_huber_delta"])
        for value, uncertainty in zip(conditioned, sigma, strict=False)
    ]
    prior_costs = [
        huber((value - centre) / scale, c["continuous_delay_huber_delta"])
        for value, centre, scale in zip(delay, prior_centres, prior_scales, strict=True)
    ]
    dynamics = []
    for index, link in enumerate(links):
        if link is not None:
            dynamics.append(
                (
                    huber(
                        (delay[index + 1] - delay[index])
                        / (link["dt"] * c["continuous_delay_rate_scale_s_per_s"]),
                        c["continuous_delay_huber_delta"],
                    ),
                    link["dt"],
                )
            )
    components = {
        "absolute_delay_level_cost": sum(
            weight * cost for weight, cost in zip(weights, prior_costs, strict=False)
        ),
        "conditioned_fit_cost": sum(
            weight * cost for weight, cost in zip(weights, fit_costs, strict=False)
        ),
        "delay_dynamics_cost": sum(cost * duration for cost, duration in dynamics)
        / linked_duration,
    }
    median = weighted_quantile(delay, weights)
    mad = weighted_quantile([abs(value - median) for value in delay], weights)
    residual_mad = weighted_quantile([abs(value) for value in conditioned], weights)
    # This is a conservative diagnostic scale, not a calibrated confidence CI.
    uncertainties = [
        max(sigma[index], 1.4826 * residual_mad) for index in range(len(delay))
    ]
    output_rows = []
    for index, row in enumerate(accepted):
        output_rows.append(
            dict(
                row,
                source_id=identity(row),
                delay_s=delay[index],
                corrected_t=row["t"] - delay[index],
                conditioned_residual_s=-conditioned[index],
                conditioned_fit_cost=fit_costs[index],
                absolute_delay_level_cost=prior_costs[index],
                normalized_weight=weights[index],
                temporal_support_s=support[identity(row)],
                delay_uncertainty_s=uncertainties[index],
                headway_s=headway[index],
            )
        )
    diagnostics = []
    if not converged:
        diagnostics.append("DELAY_SOLVER_ITERATION_LIMIT")
    if constrained_count:
        diagnostics.append("PHYSICAL_DELAY_RATE_CONSTRAINT_ACTIVE")
    if not history:
        diagnostics.append("UNCALIBRATED_HEADWAY_NORMALIZED_DELAY_PRIOR")
    return {
        "status": "FITTED",
        "rows": output_rows,
        "components": components,
        "delay_median_s": median,
        "delay_mad_s": mad,
        "delay_uncertainty_s": weighted_quantile(uncertainties, weights),
        "delay_p10_s": weighted_quantile(delay, weights, 0.1),
        "delay_p90_s": weighted_quantile(delay, weights, 0.9),
        "prior_source": "independently_validated_history"
        if history
        else "local_scheduled_headway",
        "prior_validation_artifact": history.get("validation_artifact")
        if history
        else None,
        "objective": components["conditioned_fit_cost"]
        + c["continuous_delay_prior_weight"] * components["absolute_delay_level_cost"]
        + c["continuous_delay_smooth_weight"] * components["delay_dynamics_cost"],
        "fit_count": len(output_rows),
        "excluded": excluded,
        "iterations": iteration_count,
        "diagnostics": diagnostics,
        "uncertainty_calibrated": False,
        "formulation": (
            "duration-weighted interval Huber observations + physical-time robust "
            "first-order state dynamics + headway/history occurrence prior; "
            "constrained IRLS"
        ),
    }


def _operational_rows(rows):
    accepted, excluded = [], []
    for original in rows:
        row = dict(original)
        required = ("t", "s_m", "residual_lower_s", "residual_upper_s")
        reason = None
        if row.get("source", "avl").lower() != "avl":
            reason = "APC_IS_INTERVAL_EVIDENCE_NOT_AN_AVL_TRAJECTORY_POINT"
        elif not all(finite(row.get(key)) for key in required) or (
            row["residual_lower_s"] > row["residual_upper_s"]
        ):
            reason = "INVALID_RESIDUAL_INTERVAL"
        elif (
            not finite(row.get("weight", row.get("quality", 1.0)))
            or row.get("weight", row.get("quality", 1.0)) <= 0
        ):
            reason = "ZERO_OR_INVALID_EVIDENCE_WEIGHT"
        if reason:
            excluded.append({"source_id": identity(row), "reason": reason})
        else:
            accepted.append(row)
    accepted.sort(key=lambda row: (row["t"], identity(row)))
    return accepted, excluded


def _separator(first, second, config):
    if (first.get("fragment_id"), first.get("reference_id")) != (
        second.get("fragment_id"),
        second.get("reference_id"),
    ):
        return "FRAGMENT_OR_REFERENCE_CHANGE"
    if second.get("scope_separator_before"):
        return "EXPLICIT_SCOPE_SEPARATOR"
    if second["t"] - first["t"] > config["continuous_delay_max_gap_s"]:
        return "GPS_GAP"
    return None


def _combined_uncertainty(row, adjacent_gap, config, context):
    """Schedule + projection + event/clock uncertainty, never sqrt(ping count)."""
    schedule = max(
        config["continuous_delay_min_uncertainty_s"],
        row["uncertainty_s"] if finite(row.get("uncertainty_s")) else 0.0,
    )
    if row.get("schedule_timepoint_reliable") is False:
        schedule = max(
            schedule, config.get("continuous_nontimepoint_uncertainty_s", 90.0)
        )
    projection = row.get("projection_uncertainty_s")
    if not finite(projection) or projection < 0:
        distance = row.get("projection_distance_m")
        distance = max(0.0, distance) if finite(distance) else 0.0
        speed = row.get("scheduled_speed_mps")
        speed = max(
            config["continuous_delay_projection_speed_floor_mps"],
            speed if finite(speed) else 0.0,
        )
        projection = distance / speed
    clock = (context or {}).get("clock_uncertainty_s", 0.0)
    clock = max(0.0, clock) if finite(clock) else 0.0
    stop = (
        config["continuous_delay_stop_uncertainty_s"]
        if row.get("timepoint_kind") == "dwell" or row.get("stop_id") is not None
        else 0.0
    )
    terminal = (
        config["continuous_delay_terminal_uncertainty_s"]
        if row.get("terminal")
        else 0.0
    )
    gap = config["continuous_delay_gap_uncertainty_s_per_s"] * min(
        config["continuous_delay_max_gap_s"],
        max(0.0, adjacent_gap - config["continuous_delay_gap_uncertainty_after_s"]),
    )
    pieces = {
        "schedule_s": schedule,
        "projection_s": projection,
        "clock_s": clock,
        "stop_s": stop,
        "terminal_s": terminal,
        "gap_s": gap,
    }
    return math.sqrt(sum(value * value for value in pieces.values())), pieces


def _event_identity(row):
    return (
        row.get("stop_id"),
        row.get("schedule_segment_id"),
        row.get("timepoint_kind"),
        bool(row.get("terminal")),
    )


def _interpolate_observed(rows, timestamp, values):
    times = [row["t"] for row in rows]
    index = bisect.bisect_left(times, timestamp)
    if index == 0:
        return values[0]
    if index == len(times):
        return values[-1]
    fraction = (timestamp - times[index - 1]) / max(
        1e-12, times[index] - times[index - 1]
    )
    return values[index - 1] * (1 - fraction) + values[index] * fraction


def _operational_states(rows, sigma, weights, prior, config):
    """Nonoverlapping physical-time atoms with stop/segment event boundaries.

    Knots bound atoms, not GTFS resampling points. Observation likelihoods are
    evaluated at their original timestamps using the shared interpolated latent
    delay. This yields tridiagonal normal equations while removing noisy
    individual-ping derivatives from the dynamics penalty.
    """
    groups, separators = [], []
    for index, row in enumerate(rows):
        reason = _separator(rows[index - 1], row, config) if index else "START"
        if reason:
            groups.append([])
            if index:
                separators.append(
                    {"before_source_id": identity(row), "t": row["t"], "reason": reason}
                )
        groups[-1].append(index)
    nodes, links, basis = [], [], [None] * len(rows)
    atom_s = config["continuous_delay_atomic_duration_s"]
    for group_index, indices in enumerate(groups):
        group = [rows[index] for index in indices]
        start, end = group[0]["t"], group[-1]["t"]
        knot_reasons = {start: {"FRAGMENT_START"}, end: {"FRAGMENT_END"}}
        boundary = (math.floor(start / atom_s) + 1) * atom_s
        while boundary < end:
            knot_reasons[boundary] = {"ATOMIC_TIME_BOUNDARY"}
            boundary += atom_s
        last_event = start
        for first, second in zip(group, group[1:], strict=False):
            if (
                _event_identity(first) != _event_identity(second)
                and second["t"] - last_event
                >= config["continuous_delay_min_event_spacing_s"]
            ):
                knot_reasons.setdefault(second["t"], set()).add(
                    "STOP_SEGMENT_OR_TERMINAL_EVENT"
                )
                last_event = second["t"]
        times = sorted(knot_reasons)
        offset = len(nodes)
        raw_delay = [
            _nearest(
                prior["centre_s"], row["residual_lower_s"], row["residual_upper_s"]
            )
            for row in group
        ]
        for timestamp in times:
            nodes.append(
                {
                    "t": timestamp,
                    "s_m": _interpolate_observed(
                        group, timestamp, [row["s_m"] for row in group]
                    ),
                    "initial_s": _interpolate_observed(group, timestamp, raw_delay),
                    "group": group_index,
                    "reasons": sorted(knot_reasons[timestamp]),
                    "mass": 0.0,
                    "sigma_squared_sum": 0.0,
                }
            )
        for index in indices:
            position = bisect.bisect_right(times, rows[index]["t"]) - 1
            left = offset + position
            right = min(left + 1, offset + len(times) - 1)
            fraction = (
                (rows[index]["t"] - nodes[left]["t"])
                / (nodes[right]["t"] - nodes[left]["t"])
                if right > left
                else 0.0
            )
            basis[index] = [(left, 1 - fraction)]
            if fraction > 0:
                basis[index].append((right, fraction))
            for node_index, value in basis[index]:
                nodes[node_index]["mass"] += weights[index] * value
                nodes[node_index]["sigma_squared_sum"] += (
                    weights[index] * value * sigma[index] ** 2
                )
        if offset:
            links.append(None)
        for left in range(offset, len(nodes) - 1):
            first, second = nodes[left], nodes[left + 1]
            dt, ds = second["t"] - first["t"], abs(second["s_m"] - first["s_m"])
            allowance = (
                config["continuous_delay_stationary_rate_s_per_s"] * dt
                + config["continuous_delay_max_rate_s_per_m"] * ds
            )
            links.append(
                {
                    "dt": dt,
                    "max_increase": min(
                        config["continuous_delay_max_rate_s_per_s"] * dt, allowance
                    ),
                    "max_decrease": min(
                        config["continuous_delay_max_decrease_rate_s_per_s"] * dt,
                        allowance,
                    ),
                }
            )
    for node in nodes:
        node["uncertainty_s"] = (
            math.sqrt(node["sigma_squared_sum"] / node["mass"])
            if node["mass"]
            else max(sigma)
        )
    for index, link in enumerate(links):
        if link is not None:
            link["scale_s"] = math.sqrt(
                (config["continuous_delay_rate_scale_s_per_s"] * link["dt"]) ** 2
                + nodes[index]["uncertainty_s"] ** 2
                + nodes[index + 1]["uncertainty_s"] ** 2
                + config["continuous_delay_dynamic_uncertainty_floor_s"] ** 2
            )
    return nodes, links, basis, separators


def _basis_values(states, basis):
    return [
        sum(states[index] * coefficient for index, coefficient in row) for row in basis
    ]


def _add_observation(diagonal, off, right, basis, coefficient, target):
    for node, fraction in basis:
        diagonal[node] += coefficient * fraction * fraction
        right[node] += coefficient * target * fraction
    if len(basis) == 2:
        off[basis[0][0]] += coefficient * basis[0][1] * basis[1][1]


def _project_states(values, nodes, links, *, final=False):
    count = 0
    if final:
        for index, link in enumerate(links):
            if link is not None:
                clipped = _nearest(
                    values[index + 1],
                    values[index] - link["max_decrease"],
                    values[index] + link["max_increase"],
                )
                count += abs(clipped - values[index + 1]) > 1e-9
                values[index + 1] = clipped
        return count
    for _ in range(4):
        for sequence in (range(len(links)), range(len(links) - 1, -1, -1)):
            for index in sequence:
                link = links[index]
                if link is None:
                    continue
                difference = values[index + 1] - values[index]
                maximum = (
                    link["max_increase"] if difference >= 0 else link["max_decrease"]
                )
                excess = abs(difference) - maximum
                if excess <= 1e-9:
                    continue
                sign = 1 if difference > 0 else -1
                first_mass, second_mass = (
                    max(nodes[k]["mass"], 1e-12) for k in (index, index + 1)
                )
                values[index] += (
                    sign * excess * second_mass / (first_mass + second_mass)
                )
                values[index + 1] -= (
                    sign * excess * first_mass / (first_mass + second_mass)
                )
                count += 1
    return count


def _fit_operational_delay(rows, config, operational_context):
    prior = operational_prior.resolve_prior(config, operational_context)
    accepted, excluded = _operational_rows(rows)
    if not accepted:
        return {
            "status": "NO_EVIDENCE",
            "rows": [],
            "components": {},
            "excluded": excluded,
            "delay_median_s": None,
            "delay_mad_s": None,
            "delay_uncertainty_s": None,
            "prior_provenance": prior,
            "dynamics_diagnostics": {},
            "diagnostics": ["NO_VALID_CONTINUOUS_RESIDUALS", *prior["diagnostics"]],
        }
    # Use the stricter separator limit for temporal support as well as fitting.
    weight_config = config | {
        "continuous_coverage_max_gap_s": min(
            config.get("continuous_coverage_max_gap_s", 90.0),
            config["continuous_delay_max_gap_s"],
        )
    }
    support = temporal_weights(accepted, weight_config)
    weights = [
        support[identity(row)] * min(1.0, row.get("weight", row.get("quality", 1.0)))
        for row in accepted
    ]
    mass = sum(weights)
    weights = [weight / mass for weight in weights]
    context = (
        operational_context
        if operational_context is not None
        else config.get("continuous_delay_operational_context")
    )
    sigma, uncertainty_components = [], []
    for index, row in enumerate(accepted):
        gaps = [
            abs(row["t"] - accepted[neighbor]["t"])
            for neighbor in (index - 1, index + 1)
            if 0 <= neighbor < len(accepted)
        ]
        uncertainty, pieces = _combined_uncertainty(
            row, max(gaps, default=0.0), config, context
        )
        sigma.append(uncertainty)
        uncertainty_components.append(pieces)
    nodes, links, basis, separators = _operational_states(
        accepted, sigma, weights, prior, config
    )
    states = [node["initial_s"] for node in nodes]
    linked_duration = max(1.0, sum(link["dt"] for link in links if link is not None))
    constrained_count, converged = 0, False
    for iteration in range(config["continuous_delay_max_iterations"]):
        iteration_count = iteration + 1
        delay = _basis_values(states, basis)
        diagonal, right = [1e-14] * len(nodes), [0.0] * len(nodes)
        off = [0.0] * max(0, len(nodes) - 1)
        for index, row in enumerate(accepted):
            target = _nearest(
                delay[index], row["residual_lower_s"], row["residual_upper_s"]
            )
            error = (delay[index] - target) / sigma[index]
            active = (
                row["residual_lower_s"] == row["residual_upper_s"] or abs(error) > 1e-10
            )
            coefficient = (
                weights[index]
                * _robust_weight(error, config["continuous_delay_huber_delta"])
                / sigma[index] ** 2
                if active
                else 0.0
            )
            _add_observation(diagonal, off, right, basis[index], coefficient, target)
            term = operational_prior.prior_terms(delay[index], prior)
            coefficient = (
                config["continuous_delay_prior_weight"]
                * weights[index]
                * term["quadratic_weight"]
            )
            _add_observation(
                diagonal, off, right, basis[index], coefficient, term["centre_s"]
            )
        for index, link in enumerate(links):
            if link is None:
                continue
            error = (states[index + 1] - states[index]) / link["scale_s"]
            coefficient = (
                config["continuous_delay_smooth_weight"]
                * link["dt"]
                / linked_duration
                * _robust_weight(error, config["continuous_delay_huber_delta"])
                / link["scale_s"] ** 2
            )
            diagonal[index] += coefficient
            diagonal[index + 1] += coefficient
            off[index] -= coefficient
        proposal = _solve_tridiagonal(diagonal, off, right)
        constrained_count += _project_states(proposal, nodes, links)
        change = max(abs(a - b) for a, b in zip(proposal, states, strict=True))
        states = proposal
        if change < config["continuous_delay_tolerance_s"]:
            converged = True
            break
    constrained_count += _project_states(states, nodes, links, final=True)
    delay = _basis_values(states, basis)
    conditioned = [
        value - _nearest(value, row["residual_lower_s"], row["residual_upper_s"])
        for value, row in zip(delay, accepted, strict=True)
    ]
    fit_costs = [
        huber(error / scale, config["continuous_delay_huber_delta"])
        for error, scale in zip(conditioned, sigma, strict=True)
    ]
    prior_terms = [operational_prior.prior_terms(value, prior) for value in delay]
    dynamics = []
    for index, link in enumerate(links):
        if link is not None:
            difference = states[index + 1] - states[index]
            dynamics.append(
                {
                    "start_t": nodes[index]["t"],
                    "end_t": nodes[index + 1]["t"],
                    "duration_s": link["dt"],
                    "delay_change_s": difference,
                    "uncertainty_scale_s": link["scale_s"],
                    "cost": huber(
                        difference / link["scale_s"],
                        config["continuous_delay_huber_delta"],
                    ),
                    "normalized_weight": link["dt"] / linked_duration,
                    "fragment_group": nodes[index]["group"],
                }
            )
    components = {
        "absolute_delay_level_cost": sum(
            weight * term["cost"]
            for weight, term in zip(weights, prior_terms, strict=True)
        ),
        "conditioned_fit_cost": sum(
            weight * cost for weight, cost in zip(weights, fit_costs, strict=True)
        ),
        "delay_dynamics_cost": sum(
            row["cost"] * row["normalized_weight"] for row in dynamics
        ),
    }
    median = weighted_quantile(delay, weights)
    mad = weighted_quantile([abs(value - median) for value in delay], weights)
    residual_mad = weighted_quantile([abs(value) for value in conditioned], weights)
    uncertainty = [max(value, 1.4826 * residual_mad) for value in sigma]
    output_rows = [
        dict(
            row,
            source_id=identity(row),
            delay_s=delay[index],
            corrected_t=row["t"] - delay[index],
            conditioned_residual_s=-conditioned[index],
            conditioned_fit_cost=fit_costs[index],
            absolute_delay_level_cost=prior_terms[index]["cost"],
            normalized_weight=weights[index],
            temporal_support_s=support[identity(row)],
            delay_uncertainty_s=uncertainty[index],
            schedule_uncertainty_s=uncertainty_components[index]["schedule_s"],
            uncertainty_s=sigma[index],
            uncertainty_components=uncertainty_components[index],
            operational_disruption_responsibility=prior_terms[index][
                "disruption_responsibility"
            ],
            delay_state_indices=[node for node, _ in basis[index]],
        )
        for index, row in enumerate(accepted)
    ]
    diagnostics = list(prior["diagnostics"])
    if not converged:
        diagnostics.append("DELAY_SOLVER_ITERATION_LIMIT")
    if constrained_count:
        diagnostics.append("PHYSICAL_DELAY_RATE_CONSTRAINT_ACTIVE")
    if separators:
        diagnostics.append("DELAY_DYNAMICS_NOT_BRIDGED_ACROSS_SEPARATORS")
    if config.get("continuous_delay_historical_prior") is not None:
        diagnostics.append("LEGACY_FLAT_HISTORY_NOT_USED_BY_OPERATIONAL_MODEL")
    disruption_responsibility = sum(
        weight * term["disruption_responsibility"]
        for weight, term in zip(weights, prior_terms, strict=True)
    )
    return {
        "status": "FITTED",
        "rows": output_rows,
        "components": components,
        "delay_median_s": median,
        "delay_mad_s": mad,
        "delay_uncertainty_s": weighted_quantile(uncertainty, weights),
        "delay_p10_s": weighted_quantile(delay, weights, 0.1),
        "delay_p90_s": weighted_quantile(delay, weights, 0.9),
        "prior_source": prior["source"],
        "prior_validation_artifact": prior["validation_artifact"],
        "prior_provenance": prior,
        "operational_disruption_responsibility": disruption_responsibility,
        "inferred_delay_regime": (
            "DISRUPTION_LIKE" if disruption_responsibility >= 0.5 else "ROUTINE_LIKE"
        ),
        "delay_regime_is_ground_truth": False,
        "objective": components["conditioned_fit_cost"]
        + config["continuous_delay_prior_weight"]
        * components["absolute_delay_level_cost"]
        + config["continuous_delay_smooth_weight"] * components["delay_dynamics_cost"],
        "fit_count": len(output_rows),
        "excluded": excluded,
        "iterations": iteration_count,
        "diagnostics": diagnostics,
        "uncertainty_calibrated": False,
        "dynamics_diagnostics": {
            "definition": (
                "nonoverlapping physical-time atoms with stop/segment/terminal "
                "event boundaries"
            ),
            "atomic_duration_s": config["continuous_delay_atomic_duration_s"],
            "per_observation_derivatives_used": False,
            "observation_count": len(accepted),
            "state_count": len(nodes),
            "interval_count": len(dynamics),
            "fragment_count": len(separators) + 1,
            "separators": separators,
            "states": [
                {
                    "t": node["t"],
                    "s_m": node["s_m"],
                    "delay_s": states[index],
                    "uncertainty_s": node["uncertainty_s"],
                    "fragment_group": node["group"],
                    "reasons": node["reasons"],
                }
                for index, node in enumerate(nodes)
            ],
            "intervals": dynamics,
            "linked_duration_s": sum(row["duration_s"] for row in dynamics),
            "overlapping_window_votes": False,
        },
        "formulation": (
            "duration-weighted interval Huber observations + asymmetric "
            "routine/disruption operational NLL + uncertainty-aware atomic "
            "state dynamics; shared physical-time constrained IRLS"
        ),
    }
