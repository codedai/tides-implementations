"""Spike-02 score-contract loader: Spike-01 base + anchor extension.

The Spike-02 contract is the UNMODIFIED Spike-01 contract file deep-
merged with an extension file that registers the anchor feature
families (terminal residuals, per-stop alignment, direction polarity)
and re-weights the ``segment_selection`` families per variant. Merging
at load time keeps a single source of truth for the shared features —
Spike 01's file and its hash are untouched, so the frozen ``r1c``
baseline remains exactly reproducible.

Every numeric value remains an unapproved experimental hypothesis.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from .scoring import ScoreContract


def _deep_merge(base: dict, ext: dict) -> dict:
    out = dict(base)
    for key, value in ext.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def merged_contract_raw(base_path: Path, ext_path: Path) -> dict:
    base = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    ext = yaml.safe_load(ext_path.read_text(encoding="utf-8"))
    return _deep_merge(base, ext)


def load_spike02_contract(
    base_path: Path, ext_path: Path, variant: str
) -> ScoreContract:
    return ScoreContract(merged_contract_raw(base_path, ext_path), variant)


def spike02_contract_hash(base_path: Path, ext_path: Path) -> str:
    canonical = json.dumps(
        merged_contract_raw(base_path, ext_path),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
