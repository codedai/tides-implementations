"""Bounded web configuration; filesystem locations are never request parameters."""

from __future__ import annotations

import copy
import importlib
import math

MODULES = (
    "gaps",
    "feed",
    "canonical",
    "projection",
    "off_reference",
    "windows",
    "retrieval",
    "scoring",
    "integration",
    "calibration",
)
PINNED_FEED = "ttc-d092742e6a5b"


def defaults():
    result = {}
    for name in MODULES:
        module = importlib.import_module(f"{__package__}.{name}")
        result.update(copy.deepcopy(getattr(module, "DEFAULTS", {})))
    from .continuous.config import defaults as continuous_defaults

    result.update(copy.deepcopy(continuous_defaults()))
    # An explicitly experimental opt-in, never an approval of the draft manifest.
    result.update(feed_id=PINNED_FEED, feed_allow_candidate=True, execution_workers=4)
    result["matching_partial_modes_enabled"] = True
    return {k: v for k, v in result.items() if not k.endswith(("_path", "_root"))}


def parameters():
    result = []
    for key, value in defaults().items():
        spec = {"key": key, "label": key.replace("_", " "), "default": value}
        if isinstance(value, bool):
            spec.update(type="boolean")
        elif isinstance(value, (int, float)):
            ratio = any(
                token in key
                for token in ("ratio", "confidence", "quantile", "probability")
            )
            spec.update(
                type="integer" if isinstance(value, int) else "number",
                min=1 if isinstance(value, int) and value > 0 else 0,
                max=1 if ratio and 0 <= value <= 1 else max(1000, value * 10),
                step=1 if isinstance(value, int) else 0.01,
            )
            if key == "max_projection_distance_m":
                spec.update(min=1, max=1000, step=10)
            if key == "score_delay_prior_weight":
                spec.update(
                    label="绝对时间偏移权重（每小时）",
                    description=(
                        "惩罚 = 权重 × |模型时移秒数| / 3600；提前与晚点对称。"
                        "同时用于延误拟合、整趟评分及边界模型。"
                        "实验 03c 为 0.05，实验 03d 为 1.0；修改后需新运行。"
                    ),
                )
            if key == "segmentation_joint_gap_s":
                spec.update(
                    min=1,
                    max=86400,
                    step=1,
                    label="APC + AVL 联合观测空白切分阈值（秒，严格大于）",
                )
            if any(
                token in key
                for token in ("_scale", "_sigma", "_step", "_slope", "_sample")
            ):
                spec["min"] = 1 if isinstance(value, int) else 0.01
            if key in {
                "aggregation_min_coverage",
                "canonical_max_ambiguity_fraction",
                "window_min_quality",
            }:
                spec.update(min=0, max=1)
            if key == "aggregation_output_max":
                spec.update(min=0, max=3)
            if key == "execution_workers":
                spec.update(min=1, max=8)
            if key == "score_max_points":
                spec.update(min=3, max=240)
            if key in {"projection_top_k", "projection_beam_width", "retrieval_top_k"}:
                spec["max"] = 128
        else:
            # Structured lists and identity settings are fixed in the web API.
            continue
        if key in {
            "feed_allow_candidate",
            "feed_include_previous_day_tail",
            "window_atomic_s",
        }:
            continue
        result.append(spec)
    return result


def validate(supplied=None):
    result = defaults()
    if not isinstance(supplied, (dict, type(None))) or set(supplied or {}) - set(
        result
    ):
        raise ValueError("Unknown GTFS parameter; filesystem paths are not allowed")
    specs = {s["key"]: s for s in parameters()}
    for key, value in (supplied or {}).items():
        spec = specs.get(key)
        if spec is None:
            if type(value) is not type(result[key]) or value != result[key]:
                raise ValueError(f"{key} is fixed for the route-29 experiment")
        elif spec["type"] == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be boolean")
        elif (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not spec["min"] <= value <= spec["max"]
            or (spec["type"] == "integer" and int(value) != value)
        ):
            raise ValueError(f"{key} is outside its allowed numeric range")
        result[key] = copy.deepcopy(value)
    if result.get("score_warp_min_slope", 0) > result.get("score_warp_max_slope", 1):
        raise ValueError("Minimum warping slope cannot exceed maximum")
    if result.get("continuous_matching_enabled"):
        from .continuous.config import get_config

        get_config(result)
    return result
