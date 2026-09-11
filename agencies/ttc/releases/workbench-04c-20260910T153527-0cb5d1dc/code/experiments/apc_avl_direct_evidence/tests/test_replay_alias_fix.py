"""Guard the narrow equivalence claim used by optional composite replay."""

import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from experiments.apc_avl_direct_evidence import matcher
from experiments.apc_avl_direct_evidence.validation.replay_alias_fix import (
    screen_alias_context,
    screen_motion_counters,
    validate_narrow_change,
)


def snapshot():
    return {
        "function_sha256": {
            name: hashlib.sha256(inspect.getsource(value).encode()).hexdigest()
            for name, value in vars(matcher).items()
            if inspect.isfunction(value) and value.__module__ == matcher.__name__
        },
        "default_config": dict(matcher.DEFAULT_CONFIG),
        "code": Path(matcher.__file__).read_text(),
    }


def test_identical_core_passes_without_an_exemption():
    assert (
        validate_narrow_change(snapshot(), set(), set())["changed_existing_functions"]
        == []
    )


def test_primitive_hash_change_cannot_hide_inside_alias_exemption():
    old = snapshot()
    old["function_sha256"]["distance_m"] = "wrong"
    with pytest.raises(ValueError, match="exceeds approved"):
        validate_narrow_change(old, {"_candidate", "match_direct"}, set())


def test_global_change_is_detected_even_when_function_hashes_match():
    old = snapshot()
    old["code"] += "\nUNREVIEWED_GLOBAL = 123\n"
    with pytest.raises(ValueError, match="globals changed"):
        validate_narrow_change(old, set(), set())


def test_configuration_change_disallows_bounded_reuse():
    old = snapshot()
    old["default_config"]["radius_m"] += 1
    with pytest.raises(ValueError, match="Configuration changed"):
        validate_narrow_change(old, set(), set())


def test_alias_screen_catches_multiple_owners_but_not_duplicate_votes():
    index = SimpleNamespace(
        source_ids={"p", "q"}, groups=[{"valid": True, "rids": ["p", "q"], "key": "k"}]
    )
    assert not screen_alias_context(index, {"ping_segment": {"p": "s", "q": "s"}})
    assert len(screen_alias_context(index, {"ping_segment": {"p": "s", "q": "t"}})) == 1
    with pytest.raises(ValueError, match="complete exact raw"):
        screen_alias_context(index, {"ping_segment": {"p": "s"}})


def test_motion_screen_only_selects_point_counters_inside_travel_allowance(tmp_path):
    path = tmp_path / "decisions.parquet"
    pl.from_dicts(
        [
            {
                "counter_witnesses": [
                    {"kind": "point", "distance_m": 300.0, "gap_s": 10.0},
                    {"kind": "point", "distance_m": 300.0, "gap_s": 0.0},
                    {"kind": "interpolation", "distance_m": 300.0, "gap_s": 10.0},
                ]
            }
        ]
    ).write_parquet(path)
    assert screen_motion_counters(path, matcher.DEFAULT_CONFIG) == 1
