"""Counterevidence must account for permitted movement around an event time."""

import pytest

from .test_matcher import event, ping, run


@pytest.mark.parametrize("direction", [-1, 1])
def test_permitted_approach_or_departure_does_not_overrule_exact_stop_observation(
    direction,
):
    # 300 m in 10 s is 108 km/h: below the configured 130 km/h ceiling.
    avl = [
        ping("at-stop", 0, 0),
        ping("moving", direction * 10, direction * 300),
        ping("further", direction * 20, direction * 600),
    ]
    result = run(avl, [event("stop", 0)])[0]
    assert result["status"] == "direct"
    assert result["support_level"] == "instant"
    assert result["ping_ids"] == ["at-stop"]
    assert result["counter_witnesses"] == []
    assert result["endpoint_conflicts"] == []


def test_far_point_beyond_motion_allowance_still_forces_uncertainty():
    avl = [ping("at-stop", 0, 0), ping("far", 5, 400), ping("further", 10, 800)]
    result = run(avl, [event("stop", 0)])[0]
    assert result["status"] == "ambiguous"
    assert result["counter_witnesses"]
    assert result["endpoint_conflicts"]
