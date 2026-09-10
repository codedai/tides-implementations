"""Versioned TOML is the authoritative configuration, not CLI hidden defaults."""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

from tools.merge_review.gtfs_matching.partial import MODES

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config/experiment05-v1.toml"


def load_config(path=DEFAULT_CONFIG):
    path = Path(path).resolve()
    raw = path.read_bytes()
    config = tomllib.loads(raw.decode("utf-8"))
    if config.get("schema_version") != "experiment05-config-v1":
        raise ValueError("Unsupported Experiment 05 config schema")
    for section in (
        "inputs",
        "normalization",
        "projection",
        "segmentation",
        "plateau",
        "apc",
        "matcher",
        "execution",
        "export",
    ):
        if not isinstance(config.get(section), dict):
            raise ValueError(f"Missing configuration section: {section}")
    if config.get("match_modes") != list(MODES):
        raise ValueError("Mode names/order must match 04c exactly")
    ZoneInfo(config["normalization"]["timezone"])
    if config["allowed_routes"] != ["29"]:
        raise ValueError("Current on-demand batch deployment allows only route 29")
    for section in ("projection", "segmentation", "plateau", "apc", "execution"):
        for key, value in config[section].items():
            flag = key in {
                "reset_at_raw_direction_change",
                "cache_success_only",
                "persist_alignment_traces",
            }
            if flag:
                if not isinstance(value, bool):
                    raise ValueError(f"Expected boolean: {section}.{key}")
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"Invalid nonnegative threshold: {section}.{key}")
            if key.endswith("points") or key == "workers":
                if not isinstance(value, int) or value < 1:
                    raise ValueError(f"Expected positive integer: {section}.{key}")
    for key in (
        "atomic_window_s",
        "max_adjacent_gap_s",
        "minimum_plateau_s",
        "censor_min_duration_s",
        "counterfactual_delay_prior_window_s",
    ):
        if config["plateau"][key] <= 0:
            raise ValueError(f"Expected positive duration: plateau.{key}")
    if (
        config["plateau"]["censor_max_duration_s"]
        < config["plateau"]["censor_min_duration_s"]
    ):
        raise ValueError("Censor maximum duration cannot be below minimum")
    if config["normalization"]["observation_window"] != "local_calendar_day":
        raise ValueError("Unsupported observation window policy")
    for key in ("include_devices", "include_vehicles"):
        if not isinstance(config["export"][key], bool):
            raise ValueError(f"Expected boolean: export.{key}")
    if not config["export"]["include_vehicles"]:
        raise ValueError("Vehicles are required for package referential integrity")
    if not config["execution"]["cache_success_only"]:
        raise ValueError("Failed or unvalidated runs must never be reusable caches")
    return config, hashlib.sha256(raw).hexdigest()


def configuration_identity(config):
    return hashlib.sha256(
        json.dumps(
            config, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
