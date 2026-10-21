"""Duplicate measurement aliases retain all supplied chunk-context uncertainty.

Raw copies are not extra physical evidence, but their different frozen owners
cannot be discarded by selecting a lexicographically canonical witness row ID.
These constructed contracts concern context integrity, not verified trip IDs.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from experiments.apc_avl_direct_evidence.matcher import build_index, match_direct

BASE = datetime(2024, 1, 1, 13)
METERS_PER_LON_DEGREE = 6371008.8 * math.pi / 180 * math.cos(math.radians(43.7))


def _ping(rid: str, second: int, meters: float = 0) -> dict:
    return {
        "rid": rid,
        "vehicle": "alias-context-fixture",
        "time": BASE + timedelta(seconds=second),
        "lat": 43.7,
        "lon": -79.4 + meters / METERS_PER_LON_DEGREE,
        "route": "10",
        "invalid": False,
        "time_ambiguous": False,
        "time_nonexistent": False,
    }


def _event(second: int) -> dict:
    return {
        "rid": "apc-observation",
        "vehicle": "alias-context-fixture",
        "arrival": BASE + timedelta(seconds=second),
        "departure": BASE + timedelta(seconds=second),
        "lat": 43.7,
        "lon": -79.4,
        "route": "10",
        "stop": "observed-stop",
        "direction": "E",
        "invalid": False,
        "time_ambiguous": False,
        "time_nonexistent": False,
    }


def _match(avl: list[dict], event: dict, mapping: dict[str, str]) -> dict:
    segmentation = {
        "ping_segment": mapping,
        "segments": [
            {
                "segment_id": owner,
                "start": min(r["time"] for r in avl if mapping[r["rid"]] == owner),
                "end": max(r["time"] for r in avl if mapping[r["rid"]] == owner),
            }
            for owner in sorted(set(mapping.values()))
        ],
    }
    return match_direct([event], build_index(avl), segmentation)[0]


def test_duplicate_point_aliases_preserve_direct_evidence_but_not_single_owner():
    avl = [_ping("point-a", 0), _ping("point-z", 0), _ping("later", 20)]
    result = _match(
        avl,
        _event(0),
        {"point-a": "owner-a", "point-z": "owner-b", "later": "owner-a"},
    )

    # The two copies describe the same real near-stop measurement: retain it.
    assert result["status"] == "direct"
    assert result["support_level"] == "instant"
    assert result["support_kind"] == "point"
    # Its representative ID must not hide the other supplied owner context.
    assert result["segment_id"] is None
    assert result["context_status"] == "multiple_segments"
    assert set(result["segment_ids"]) == {"owner-a", "owner-b"}


def test_interpolation_bracket_with_split_owner_alias_cannot_claim_single_owner():
    # Both measured positions are 150 m from APC, outside the 100 m point radius.
    # Only the legal 60 s midpoint interpolation could produce local evidence.
    avl = [
        _ping("left-a", 0, -150),
        _ping("left-z", 0, -150),
        _ping("right", 60, 150),
    ]
    event = _event(30)
    control = _match(avl, event, dict.fromkeys((r["rid"] for r in avl), "owner-a"))
    assert control["status"] == "direct"
    assert control["support_kind"] == "interpolation"

    result = _match(
        avl,
        event,
        {"left-a": "owner-a", "left-z": "owner-b", "right": "owner-a"},
    )
    assert result["status"] not in {"direct", "partial"}
    assert result["segment_id"] is None
    assert not result["witnesses"]
