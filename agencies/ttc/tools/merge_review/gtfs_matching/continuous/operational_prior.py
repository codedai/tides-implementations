"""Candidate-independent, asymmetric operational delay priors.

The routine/disruption mixture uses two-piece Student-t (three degrees of
freedom) densities in physical seconds. Its negative log density relative to
the mode is nonnegative and unbounded, but no delay is forbidden. Early and
late running have different scales. A candidate's own headway is deliberately
not an input: all scheduled occurrences receive the same observed-context
prior. Defaults are engineering assumptions, never calibrated probabilities.

An optional independently validated artifact supplies a hierarchy of complete
parameter records. Context selection is feed -> route -> direction -> observed
service-local hour (0..47); scheduled candidate times cannot select a record.
Records do not partially overwrite one another, avoiding accidental mixtures
of parameters trained on unrelated populations.
"""

from __future__ import annotations

import hashlib
import json
import math

from .evidence import finite

DEFAULTS = {
    "continuous_operational_prior_artifact": None,
    "continuous_delay_operational_context": None,
    "continuous_operational_routine_early_scale_s": 120.0,
    "continuous_operational_routine_late_scale_s": 300.0,
    "continuous_operational_disruption_early_scale_s": 600.0,
    "continuous_operational_disruption_late_scale_s": 1800.0,
    "continuous_operational_disruption_probability": 0.05,
    "continuous_operational_prior_rescue_probability": 0.995,
}

CONTEXT_KEYS = (
    "feed_version",
    "route_id",
    "direction",
    "observed_time_bucket",
)
SCALE_KEYS = (
    "routine_early_scale_s",
    "routine_late_scale_s",
    "disruption_early_scale_s",
    "disruption_late_scale_s",
)
PARAMETER_KEYS = ("centre_s", *SCALE_KEYS, "disruption_probability")


def _parameters(record):
    result = {key: record.get(key) for key in PARAMETER_KEYS}
    if not finite(result["centre_s"]):
        raise ValueError("Operational prior centre must be finite seconds")
    for key in SCALE_KEYS:
        if not finite(result[key]) or result[key] <= 0:
            raise ValueError(f"Operational prior {key} must be positive seconds")
    for side in ("early", "late"):
        if result[f"disruption_{side}_scale_s"] < result[f"routine_{side}_scale_s"]:
            raise ValueError("Disruption scales cannot be narrower than routine scales")
    probability = result["disruption_probability"]
    if not finite(probability) or not 0 < probability < 1:
        raise ValueError("Disruption probability must be strictly between zero and one")
    return result


def _context(context, *, record=False):
    if context is None:
        return {}
    if not isinstance(context, dict):
        raise ValueError("Operational context must be an observed-context object")
    result = {key: context[key] for key in CONTEXT_KEYS if key in context}
    if record and set(context) - set(CONTEXT_KEYS):
        raise ValueError("Historical context contains non-hierarchy fields")
    for key, value in result.items():
        if key == "observed_time_bucket":
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value < 48
            ):
                raise ValueError(
                    "Observed time bucket must be service-local hour 0..47"
                )
        elif (
            not isinstance(value, (str, int))
            or isinstance(value, bool)
            or str(value) == ""
        ):
            raise ValueError(f"Invalid operational context identity: {key}")
        else:
            result[key] = str(value)
    # A hierarchy record must be a prefix, not e.g. direction across all routes.
    if record and set(result) != set(CONTEXT_KEYS[: len(result)]):
        raise ValueError(
            "Historical records must follow feed/route/direction/hour hierarchy"
        )
    return result


def resolve_prior(config=None, operational_context=None):
    """Resolve one immutable prior for a performed episode's observed context.

    ``operational_context`` is a keyword-compatible mapping shared by all
    competing candidates. It may carry audit-only service_date, observed_t,
    clock_uncertainty_s and local_headway_s; none changes the default scales.
    Candidate row headway, trip ID, scheduled clock and residuals are not read.
    """
    c = DEFAULTS | (config or {})
    rescue_probability = c["continuous_operational_prior_rescue_probability"]
    if not finite(rescue_probability) or not 0 < rescue_probability < 1:
        raise ValueError("Operational rescue probability must be between zero and one")
    supplied_context = (
        operational_context
        if operational_context is not None
        else c["continuous_delay_operational_context"]
    )
    context = _context(supplied_context)
    defaults = _parameters(
        {"centre_s": 0.0}
        | {
            key: c[f"continuous_operational_{key}"]
            for key in (*SCALE_KEYS, "disruption_probability")
        }
    )
    result = {
        **defaults,
        "schema_version": "operational-delay-prior-v1",
        "family": "two_piece_student_t3_routine_disruption",
        "context": context,
        "selected_context": {},
        "calibrated": False,
        "source": "uncalibrated_operational_defaults",
        "validation_artifact": None,
        "artifact_sha256": None,
        "record_id": None,
        "candidate_headway_used": False,
        "diagnostics": ["UNCALIBRATED_OPERATIONAL_DELAY_PRIOR"],
    }
    artifact = c["continuous_operational_prior_artifact"]
    if artifact is None:
        return result
    if (
        not isinstance(artifact, dict)
        or artifact.get("schema_version") != result["schema_version"]
    ):
        raise ValueError("Unsupported operational delay prior artifact schema")
    if (
        artifact.get("independently_validated") is not True
        or not isinstance(artifact.get("validation_artifact"), str)
        or not artifact["validation_artifact"].strip()
    ):
        raise ValueError(
            "Operational history requires independent validation provenance"
        )
    # Shared with calibration: reject overlapping train/validation/test episodes,
    # route/date leakage, absent sample counts, and missing label-set identity.
    from .evaluation import validate_training_provenance

    validate_training_provenance(artifact.get("training_provenance"), require_test=True)
    records = artifact.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("Operational history requires nonempty hierarchy records")
    seen, matches = set(), []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Operational history record must be an object")
        scope = _context(record.get("context", {}), record=True)
        key = tuple(scope.items())
        if key in seen:
            raise ValueError("Duplicate operational prior hierarchy context")
        seen.add(key)
        params = _parameters(record)
        count = record.get("sample_count")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("Historical prior sample_count must be positive")
        if all(context.get(name) == value for name, value in scope.items()):
            matches.append((len(scope), scope, params, record))
    result["artifact_sha256"] = hashlib.sha256(
        json.dumps(
            artifact, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    result["validation_artifact"] = artifact["validation_artifact"]
    if not matches:
        result["diagnostics"].append("NO_VALIDATED_OPERATIONAL_HISTORY_FOR_CONTEXT")
        return result
    _, scope, params, selected = max(matches, key=lambda item: item[0])
    result.update(
        params,
        selected_context=scope,
        source="independently_validated_operational_hierarchy",
        calibrated=True,
        sample_count=selected["sample_count"],
        record_id=selected.get("record_id"),
        diagnostics=[],
    )
    return result


def _log_terms(delay_s, prior):
    difference = delay_s - prior["centre_s"]
    side = "early" if difference < 0 else "late"
    p = prior["disruption_probability"]
    terms = []
    for name, mass in (("routine", 1 - p), ("disruption", p)):
        scale = prior[f"{name}_{side}_scale_s"]
        normalizer = prior[f"{name}_early_scale_s"] + prior[f"{name}_late_scale_s"]
        log_peak = math.log(mass) - math.log(normalizer)
        log_density = log_peak - 2 * math.log1p((difference / scale) ** 2 / 3)
        terms.append((log_density, log_peak, scale))
    return difference, terms


def prior_terms(delay_s, prior):
    """Return NLL and exact-gradient IRLS curvature for the same objective.

    ``gradient = quadratic_weight * (delay_s - centre_s)``. The mixture
    responsibilities are updated at every solver iteration, not merely added
    to an output score after fitting a different headway-only objective.
    """
    if not finite(delay_s):
        raise ValueError("Prior evaluation requires finite delay seconds")
    difference, terms = _log_terms(delay_s, prior)
    maximum = max(term[0] for term in terms)
    masses = [math.exp(term[0] - maximum) for term in terms]
    mass_sum = sum(masses)
    responsibilities = [mass / mass_sum for mass in masses]
    log_density = maximum + math.log(mass_sum)
    peak_max = max(term[1] for term in terms)
    log_peak = peak_max + math.log(sum(math.exp(term[1] - peak_max) for term in terms))
    coefficient = sum(
        responsibility * 4 / (3 * term[2] ** 2 + difference**2)
        for responsibility, term in zip(responsibilities, terms, strict=True)
    )
    return {
        "cost": max(0.0, log_peak - log_density),
        "quadratic_weight": coefficient,
        "gradient": coefficient * difference,
        "centre_s": prior["centre_s"],
        "disruption_responsibility": responsibilities[1],
    }


def _t3_cdf(value):
    z = value / math.sqrt(3)
    return 0.5 + (math.atan(z) + z / (1 + z * z)) / math.pi


def _cdf(value, prior):
    difference = value - prior["centre_s"]
    p = prior["disruption_probability"]
    total = 0.0
    for name, mass in (("routine", 1 - p), ("disruption", p)):
        early, late = (prior[f"{name}_{side}_scale_s"] for side in ("early", "late"))
        if difference <= 0:
            value_cdf = 2 * early / (early + late) * _t3_cdf(difference / early)
        else:
            value_cdf = early / (early + late) + 2 * late / (early + late) * (
                _t3_cdf(difference / late) - 0.5
            )
        total += mass * value_cdf
    return total


def rescue_delay_bounds(prior, probability=0.995):
    """Mixture central interval for high-recall retrieval, with honest source.

    The signed bounds are delay residuals (observed minus scheduled time).
    This does not override a caller's clock/gap/terminal rescue allowance.
    Bounds from default assumptions must never be called empirical quantiles.
    """
    if not finite(probability) or not 0 < probability < 1:
        raise ValueError("Rescue probability must be strictly between zero and one")
    bounds = []
    for target in ((1 - probability) / 2, 1 - (1 - probability) / 2):
        width = max(prior[key] for key in SCALE_KEYS)
        while (
            _cdf(prior["centre_s"] - width, prior) > target
            or _cdf(prior["centre_s"] + width, prior) < target
        ):
            width *= 2
        lower, upper = prior["centre_s"] - width, prior["centre_s"] + width
        for _ in range(80):
            middle = (lower + upper) / 2
            if _cdf(middle, prior) < target:
                lower = middle
            else:
                upper = middle
        bounds.append((lower + upper) / 2)
    return {
        "lower_s": bounds[0],
        "upper_s": bounds[1],
        "probability": probability,
        "calibrated": prior["calibrated"],
        "source": prior["source"],
        "validation_artifact": prior["validation_artifact"],
        "artifact_sha256": prior["artifact_sha256"],
        "selected_context": prior["selected_context"],
        "candidate_headway_used": False,
    }
