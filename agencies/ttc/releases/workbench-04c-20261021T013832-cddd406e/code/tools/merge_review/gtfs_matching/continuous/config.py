"""Explicit feature flag; legacy experiments retain their original scorer."""

import importlib
import math

DEFAULTS = {
    "continuous_matching_enabled": False,
    # 04c is opt-in; retained 04/04b profiles preserve their scoring semantics.
    "continuous_occurrence_model_enabled": False,
    "continuous_occurrence_displacement_weight": 1.0,
    "continuous_occurrence_missing_cost": 4.0,
    "continuous_local_evidence_cache_size": 8192,
    "continuous_match_cost_max": 3.0,
    "continuous_min_projection_ratio": 0.35,
    "continuous_min_schedule_ratio": 0.35,
    "continuous_min_end_to_end_ratio": 0.25,
    "continuous_min_points": 3,
    "continuous_min_duration_s": 20.0,
    "continuous_min_progress_m": 50.0,
    "continuous_headway_default_s": 600.0,
    "continuous_headway_min_s": 60.0,
    "continuous_headway_max_s": 3600.0,
    "continuous_retrieval_top_k": 12,
    "continuous_retrieval_delta": 1.0,
    "continuous_core_headways": 2.0,
    "continuous_rescue_headways": 4.0,
    "continuous_rescue_gap_s": 600.0,
    "continuous_rescue_delay_s": 3600.0,
    "continuous_clock_uncertainty_s": 0.0,
    "continuous_absolute_delay_weight": 1.0,
    "continuous_conditioned_fit_weight": 1.0,
    "continuous_delay_dynamics_weight": 0.5,
    "continuous_apc_weight": 0.5,
    "continuous_projection_coverage_weight": 2.0,
    "continuous_pattern_progress_weight": 1.0,
    "continuous_vehicle_sequence_weight": 0.0,
    "continuous_alignment_sigma_max": 4.0,
    "continuous_apc_time_scale_s": 180.0,
    "continuous_pattern_stop_penalty": 2.0,
    "continuous_pattern_discriminative_stop_weight": 1.0,
    "continuous_direction_prior_cost": 1.0,
    "continuous_backtrack_scale_m": 100.0,
    "continuous_pattern_heading_scale_deg": 45.0,
    "continuous_local_support_cost_max": 3.0,
    # Raw APC residuals and missing local evidence are not whole-score units.
    # Keep separate budgets when the outer AVL component weights change.
    "continuous_apc_support_cost_max": 3.0,
    "continuous_local_missing_cost": 4.0,
    "continuous_trim_fraction": 0.1,
    "continuous_boundary_min_windows": 3,
    "continuous_boundary_penalty": 0.5,
    "continuous_boundary_gain_min": 0.5,
    "continuous_event_windows_enabled": True,
    "continuous_event_radius_s": 90.0,
    "continuous_terminal_radius_m": 200.0,
}


def defaults():
    result = dict(DEFAULTS)
    for name in (
        "schedule",
        "delay",
        "evidence",
        "calibration",
        "sequence",
        "occurrence",
    ):
        module = importlib.import_module(f"{__package__}.{name}")
        result.update(getattr(module, "DEFAULTS", {}))
    return result


def get_config(config=None):
    result = defaults() | (config or {})
    for key, default in defaults().items():
        value = result[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} requires a boolean")
        elif isinstance(default, (int, float)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"Invalid physical configuration: {key}")
            if isinstance(default, int) and int(value) != value:
                raise ValueError(f"Integer required: {key}")
            if (
                any(
                    token in key
                    for token in (
                        "ratio",
                        "probability",
                        "coverage",
                        "credible_mass",
                        "max_ece",
                    )
                )
                and value > 1
                and default <= 1
            ):
                raise ValueError(f"Ratio cannot exceed one: {key}")
            if (
                default > 0
                and any(
                    token in key
                    for token in (
                        "scale",
                        "uncertainty",
                        "headway",
                        "top_k",
                        "min_points",
                        "max_iterations",
                    )
                )
                and value <= 0
            ):
                raise ValueError(f"Positive scale/count required: {key}")
    if not 0 < result["continuous_delay_max_rate_s_per_s"] < 1:
        raise ValueError("Delay increase rate must be positive and below one")
    if not 0 <= result["continuous_trim_fraction"] < 0.5:
        raise ValueError("Trim fraction must be below one half")
    if result["continuous_headway_min_s"] > result["continuous_headway_max_s"]:
        raise ValueError("Minimum headway exceeds maximum")
    if result["continuous_boundary_min_windows"] < 1:
        raise ValueError("Boundary models require positive support-window count")
    if (
        result["continuous_local_missing_cost"]
        <= result["continuous_local_support_cost_max"]
    ):
        raise ValueError("Missing local evidence cost must exceed local support gate")
    if (
        result["continuous_event_windows_enabled"]
        and result["continuous_event_radius_s"] <= 0
    ):
        raise ValueError("Event windows require positive physical duration")
    return result
