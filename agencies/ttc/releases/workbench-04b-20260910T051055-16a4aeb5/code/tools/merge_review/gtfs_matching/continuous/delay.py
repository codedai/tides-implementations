"""One robust physical-time delay process per performed-trip/candidate pair.

The data objective is integrated with temporal support, not counted once per
sample or overlapping window. IRLS solves a robust first-order state-space
objective with a meaningful headway-normalized occurrence prior. Fragment
separators remove state transitions, not source observations from coverage.
"""

from __future__ import annotations

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
}


def get_config(config=None):
    c = DEFAULTS | (config or {})
    for key in DEFAULTS:
        if key == "continuous_delay_historical_prior":
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


def fit_delay(rows, config=None):
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
