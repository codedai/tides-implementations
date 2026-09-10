"""Independent review regressions; no production edits during frozen batch runs."""

import pytest

from tools.experiment05 import plateau
from tools.experiment05.config import load_config
from tools.experiment05.segmentation import segment_trips


@pytest.mark.parametrize(
    "arrival,departure,instant,expected",
    [
        (90, 130, 90, True),  # Arrival predates first AVL observation.
        (90, 130, None, True),  # Full interval does not require a manufactured t.
        (90, 100, 90, True),  # Closed interval touches segment start.
        (180, 200, 180, True),  # Closed interval touches segment end.
        (90, 200, 90, True),  # Encloses the whole segment.
        (90, 99, 90, False),
        (181, 200, 181, False),
        (110, None, 110, True),  # Unknown duration retained for diagnostics.
        (None, 130, 120, True),
        (130, 110, 120, True),  # Reversed interval cannot invent service duration.
        (130, 110, 90, False),
        (None, None, None, False),
    ],
)
def test_extent_receives_closed_service_interval_overlap(
    monkeypatch, arrival, departure, instant, expected
):
    """A continuing APC visit is evidence even if its arrival predates first AVL."""
    captured = []

    def capture(points, apc, canonical, config):
        captured.extend(apc)
        return {"extent": {}, "points": points, "diagnostics": []}

    monkeypatch.setattr(plateau, "assess_trip_extent", capture)
    observations = [
        {
            "rid": f"avl-{timestamp}",
            "source": "avl",
            "vehicle_id": "synthetic-vehicle",
            "route_eligible": True,
            "t": timestamp,
            "direction_id": "outbound",
        }
        for timestamp in (100, 140, 180)
    ]
    service = {
        "rid": "apc-crossing-start",
        "source": "apc",
        "vehicle_id": "synthetic-vehicle",
        "route_eligible": True,
        "t": instant,
        "arrival": arrival,
        "departure": departure,
        "boarding": 2,
        "alighting": 0,
    }
    cfg, _ = load_config()
    segment_trips(
        [*observations, service],
        {"route_id": "synthetic-route", "feed_version": "synthetic-feed"},
        "synthetic-service-date",
        cfg,
    )
    assert [row["rid"] for row in captured] == ([service["rid"]] if expected else [])
    if expected:
        assert captured[0]["arrival"] == arrival
        # Do not clip raw interval evidence.
        assert captured[0]["departure"] == departure


@pytest.mark.parametrize(
    "real_coordinate,expected", [(False, "colocated"), (True, "away")]
)
def test_apc_coordinates_take_precedence_over_stop_identity(real_coordinate, expected):
    cfg, _ = load_config()
    apc = {
        "rid": "interval",
        "arrival": 90,
        "departure": 130,
        "t": 90,
        "stop_id": "terminal",
        "boarding": 2,
        "alighting": 0,
    }
    if real_coordinate:
        apc.update(lat=44.0, lon=-79.4)
    metrics = {
        "raw_center": {"lat": 43.65, "lon": -79.4},
        "progress": {"ref": {"median_s_m": 600}},
    }
    references = {"ref": {"stop_positions": [{"stop_id": "terminal", "s_m": 600}]}}
    result = plateau._apc_activity([apc], 100, 180, metrics, references, cfg["plateau"])
    assert bool(result["colocated_service_intervals"]) == (expected == "colocated")
    assert bool(result["noncolocated_service_rids"]) == (expected == "away")
    assert not result["unknown_activity_rids"]
    if expected == "colocated":
        assert result["last_service_end"] == 130
