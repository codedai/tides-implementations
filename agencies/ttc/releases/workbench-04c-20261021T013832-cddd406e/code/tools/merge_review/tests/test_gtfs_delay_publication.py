"""A completed run must never contain a fitted clock that reverses time."""

import copy

from tools.merge_review.gtfs_matching.runner import delay_clock_violations


def example():
    points = [
        dict(source="avl", rid=str(i), t=t) for i, t in enumerate((100, 110, 120))
    ]
    process = [
        dict(
            source_id=f"avl|{i}",
            t=p["t"],
            delay_s=50,
            corrected_t=p["t"] - 50,
            fragment_id="f",
        )
        for i, p in enumerate(points)
    ]
    return {"all_candidates": [{"whole_alignment": {"delay_process": process}}]}, points


def test_publish_gate_rejects_clock_reversal_without_deleting_original_points():
    result, points = example()
    original = copy.deepcopy(points)
    assert delay_clock_violations(result, points) == 0
    process = result["all_candidates"][0]["whole_alignment"]["delay_process"]
    process[1].update(delay_s=70, corrected_t=40)
    assert delay_clock_violations(result, points) == 1
    assert points == original
    process[1]["fragment_id"] = "separate"
    assert delay_clock_violations(result, points) == 0


def test_publish_gate_rejects_detached_or_inconsistent_clock_values():
    result, points = example()
    process = result["all_candidates"][0]["whole_alignment"]["delay_process"]
    process[0]["corrected_t"] = 49
    process[1]["t"] = 111
    process[2]["source_id"] = "unknown"
    assert delay_clock_violations(result, points) == 3


def test_publish_gate_allows_unfitted_diagnostic_rows():
    result, points = example()
    process = result["all_candidates"][0]["whole_alignment"]["delay_process"]
    process[1].update(corrected_t=None, delay_s=None, fragment_id=None)
    assert delay_clock_violations(result, points) == 0
