"""Independent exhaustive oracles for deterministic, locked trip assignment."""

import copy
import itertools
import json
import random
from fractions import Fraction

import pytest

from tools.experiment05.assignment import MODES, assign_trips

GROUP = {
    "service_date": "2023-11-06",
    "route_id": "29",
    "direction_id": "0",
    "gtfs_feed_version": "synthetic-feed",
}


def performed(count, **overrides):
    return [
        {
            **GROUP,
            "trip_id_performed": f"p{index}",
            "actual_start_t": 1000 + index * 100,
            **overrides,
        }
        for index in range(count)
    ]


def scheduled(count, **overrides):
    return [
        {
            **GROUP,
            "trip_id_scheduled": f"s{index}",
            "scheduled_start_s": 1000 + index * 100,
            "scheduled_end_s": 1090 + index * 100,
            **overrides,
        }
        for index in range(count)
    ]


def candidate(p, s, rank=1, score=1, mode="global", **overrides):
    return {
        **GROUP,
        "trip_id_performed": f"p{p}",
        "trip_id_scheduled": f"s{s}",
        "candidate_id": f"candidate-s{s}",
        "candidate_rank": rank,
        "score": score,
        "score_components": {"unchanged_synthetic_cost": score},
        "match_mode": mode,
        "hard_constraints_valid": True,
        "hard_constraint_reasons": [],
        **overrides,
    }


def assigned_pairs(result):
    return {
        (row["trip_id_performed"], row["match_mode"]): row["trip_id_scheduled"]
        for row in result["assignments"]
        if row["trip_id_scheduled"] is not None
    }


def proposal(result, p, round_number):
    return next(
        row
        for row in result["audit"]
        if row["event"] == "proposal"
        and row["trip_id_performed"] == f"p{p}"
        and row["round"] == round_number
    )


def exhaustive_chain(rows, actual_order, schedule_order):
    """Enumerate every subset, independent of the production dynamic program."""
    best, best_key = [], (0, Fraction(), ())
    for mask in range(1 << len(rows)):
        chosen = sorted(
            (row for index, row in enumerate(rows) if mask & (1 << index)),
            key=lambda row: actual_order[row["trip_id_performed"]],
        )
        orders = [schedule_order[row["trip_id_scheduled"]] for row in chosen]
        if any(a >= b for a, b in zip(orders, orders[1:], strict=False)):
            continue
        key = (
            -len(chosen),
            sum((Fraction(row["score"]) for row in chosen), Fraction()),
            tuple(
                (row["score"], row["candidate_id"], row["trip_id_performed"])
                for row in chosen
            ),
        )
        if key < best_key:
            best, best_key = chosen, key
    return best


def test_cardinality_wins_over_cheaper_single_proposal():
    rows = [
        candidate(0, 2, score=0),
        candidate(1, 0, score=100),
        candidate(2, 1, score=100),
    ]
    result = assign_trips(performed(3), rows, scheduled(3), ["global"])
    assert assigned_pairs(result) == {("p1", "global"): "s0", ("p2", "global"): "s1"}
    assert proposal(result, 0, 1)["reasons"] == ["CROSSED_CURRENT_ROUND_ASSIGNMENT"]


def test_same_schedule_winner_is_jointly_chosen_not_greedy():
    rows = [
        candidate(0, 1, score=0),
        candidate(1, 0, score=1),
        candidate(2, 1, score=2),
    ]
    result = assign_trips(performed(3), rows, scheduled(2), ["global"])
    assert assigned_pairs(result) == {("p1", "global"): "s0", ("p2", "global"): "s1"}
    rejected = proposal(result, 0, 1)
    assert "LOST_SAME_SCHEDULE_CONFLICT" in rejected["reasons"]
    assert rejected["same_schedule_competitor_ids"] == ["p2"]


def test_existing_quality_breaks_cardinality_ties_without_score_threshold():
    result = assign_trips(
        performed(2),
        [candidate(0, 0, score=1e200), candidate(1, 0, score=1e100)],
        scheduled(1),
        ["global"],
    )
    assert assigned_pairs(result) == {("p1", "global"): "s0"}
    assert proposal(result, 0, 1)["reasons"] == ["LOST_SAME_SCHEDULE_CONFLICT"]


def test_exact_ties_use_candidate_id_then_stable_performed_id():
    rows = [candidate(0, 1, candidate_id="z"), candidate(1, 0, candidate_id="a")]
    result = assign_trips(performed(2), rows, scheduled(2), ["global"])
    assert assigned_pairs(result) == {("p1", "global"): "s0"}
    rows = [candidate(0, 0), candidate(1, 0)]
    result = assign_trips(performed(2), rows, scheduled(1), ["global"])
    assert assigned_pairs(result) == {("p0", "global"): "s0"}


def test_rounds_lock_predecessors_and_never_recalculate_ranks():
    rows = [
        candidate(0, 1, rank=1, score=1),
        candidate(0, 0, rank=2, score=0),
        candidate(1, 0, rank=1, score=10),
        candidate(1, 1, rank=2, score=0),
        candidate(1, 2, rank=3, score=100),
    ]
    result = assign_trips(performed(2), rows, scheduled(3), ["global"])
    assert assigned_pairs(result) == {("p0", "global"): "s1", ("p1", "global"): "s2"}
    assert [row["assignment_round"] for row in result["assignments"]] == [1, 3]
    rejected = proposal(result, 1, 2)
    assert rejected["lower_locked_performed_id"] == "p0"
    assert rejected["lower_scheduled_order"] == 2
    assert rejected["reasons"] == [
        "CANDIDATE_ALREADY_USED",
        "VIOLATED_LOWER_ORDER_BOUND",
    ]
    assert not any(
        row["trip_id_performed"] == "p0" and row.get("round") in (2, 3)
        for row in result["audit"]
    )


def test_successor_upper_bound_is_strict_and_recorded():
    rows = [
        candidate(0, 2, rank=1, score=50),
        candidate(0, 1, rank=2),
        candidate(0, 0, rank=3),
        candidate(1, 1, score=1),
    ]
    result = assign_trips(performed(2), rows, scheduled(3), ["global"])
    assert assigned_pairs(result) == {("p0", "global"): "s0", ("p1", "global"): "s1"}
    assert proposal(result, 0, 2)["upper_locked_performed_id"] == "p1"
    assert "VIOLATED_UPPER_ORDER_BOUND" in proposal(result, 0, 2)["reasons"]


def test_second_round_can_fill_three_locked_intervals_without_crossing():
    rows = [
        candidate(0, 4, score=100),
        candidate(0, 0, rank=2),
        candidate(1, 1, score=0),
        candidate(2, 0, score=100),
        candidate(2, 2, rank=2),
        candidate(3, 3, score=0),
        candidate(4, 2, score=100),
        candidate(4, 4, rank=2),
    ]
    result = assign_trips(performed(5), rows, scheduled(5), ["global"])
    assert assigned_pairs(result) == {
        (f"p{index}", "global"): f"s{index}" for index in range(5)
    }
    assert [row["assignment_round"] for row in result["assignments"]] == [2, 1, 2, 1, 2]
    middle = proposal(result, 2, 2)
    assert middle["lower_locked_performed_id"] == "p1"
    assert middle["upper_locked_performed_id"] == "p3"
    assert middle["lower_bound_passed"] and middle["upper_bound_passed"]


def test_all_four_modes_have_independent_occupancy():
    rows = [candidate(0, 0, mode=mode) for mode in MODES]
    result = assign_trips(performed(1), rows, scheduled(1), list(MODES))
    assert len(result["assignments"]) == result["summary"]["assigned_count"] == 4
    assert {row["trip_id_scheduled"] for row in result["assignments"]} == {"s0"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("service_date", "2023-11-07"),
        ("route_id", "x"),
        ("direction_id", "1"),
        ("gtfs_feed_version", "feed2"),
    ],
)
def test_groups_do_not_share_scheduled_occupancy(field, value):
    actual = performed(2)
    actual[1][field] = value
    lookup = scheduled(1) + scheduled(1, **{field: value})
    rows = [candidate(0, 0), candidate(1, 0, **{field: value})]
    result = assign_trips(actual, rows, lookup, ["global"])
    assert result["summary"]["assigned_count"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("service_date", "2023-11-05"),
        ("route_id", "x"),
        ("direction_id", "1"),
        ("gtfs_feed_version", "wrong-feed"),
    ],
)
def test_cross_group_candidate_is_rejected_even_if_reference_exists(field, value):
    rows = [candidate(0, 0, **{field: value})]
    result = assign_trips(
        performed(1), rows, scheduled(1, **{field: value}), ["global"]
    )
    record = proposal(result, 0, 1)
    assert record["reference_exists"] is True
    assert record["reference_group_matches"] is False
    assert record["reasons"] == ["INVALID_GTFS_REFERENCE"]
    assert result["assignments"][0]["trip_id_scheduled"] is None


def test_unknown_direction_and_explicit_hard_failure_cannot_be_assigned():
    for actual, rows, lookup in (
        (
            performed(1, direction_id="UNKNOWN"),
            [candidate(0, 0, direction_id="UNKNOWN")],
            scheduled(1, direction_id="UNKNOWN"),
        ),
        (
            performed(1),
            [
                candidate(
                    0,
                    0,
                    hard_constraints_valid=False,
                    hard_constraint_reasons=["GEOMETRY_INVALID"],
                )
            ],
            scheduled(1),
        ),
        (performed(1), [candidate(0, 99)], scheduled(1)),
        (performed(1), [candidate(0, 0, trip_id_scheduled=None)], scheduled(1)),
    ):
        result = assign_trips(actual, rows, lookup, ["global"])
        assert result["summary"]["assigned_count"] == 0
        assert proposal(result, 0, 1)["reasons"] == ["INVALID_GTFS_REFERENCE"]


def test_no_candidates_is_true_null_and_rank_four_is_audit_only():
    result = assign_trips(performed(1), [], scheduled(1), ["global"])
    row = result["assignments"][0]
    assert row["trip_id_scheduled"] is None
    assert row["candidate_id"] is None
    assert row["unmatched_reasons"] == ["NO_CANDIDATES"]
    rows = [candidate(0, 90 + rank, rank=rank) for rank in (1, 2, 3)]
    rows.append(candidate(0, 0, rank=4, score=0))
    result = assign_trips(performed(1), rows, scheduled(1), ["global"])
    assert result["assignments"][0]["trip_id_scheduled"] is None
    assert "TOP3_EXHAUSTED" in result["assignments"][0]["unmatched_reasons"]
    extra = result["audit"][-1]
    assert extra["candidate_rank"] == 4 and extra["selected"] is False
    assert extra["event"] == "candidate_retained_for_audit"
    json.dumps(result, allow_nan=False)


def test_authoritative_service_seconds_and_stable_time_ties():
    actual = performed(2, actual_start_t=1000)
    lookup = scheduled(2, scheduled_start_s=25 * 3600, scheduled_end_s=26 * 3600)
    rows = [
        candidate(0, 0, scheduled_start_s=0),
        candidate(1, 1, scheduled_start_s=-999),
    ]
    result = assign_trips(actual[::-1], rows[::-1], lookup[::-1], ["global"])
    assert [row["trip_id_performed"] for row in result["assignments"]] == ["p0", "p1"]
    assert [row["scheduled_order"] for row in result["assignments"]] == [1, 2]
    assert all(row["scheduled_start_s"] == 90000 for row in result["assignments"])


def test_all_small_round_one_proposal_sets_match_exhaustive_oracle():
    lookup = scheduled(3)
    schedule_order = {
        row["trip_id_scheduled"]: index for index, row in enumerate(lookup)
    }
    for count in range(1, 5):
        actual = performed(count)
        actual_order = {
            row["trip_id_performed"]: index for index, row in enumerate(actual)
        }
        for choices in itertools.product(range(3), repeat=count):
            rows = [
                candidate(i, s, score=[3, 0, 2, 1][i]) for i, s in enumerate(choices)
            ]
            oracle = exhaustive_chain(rows, actual_order, schedule_order)
            result = assign_trips(actual, rows, lookup, ["global"])
            assert assigned_pairs(result) == {
                (row["trip_id_performed"], "global"): row["trip_id_scheduled"]
                for row in oracle
            }


def exhaustive_rounds(actual, rows, lookup):
    actual_order = {row["trip_id_performed"]: index for index, row in enumerate(actual)}
    schedule_order = {
        row["trip_id_scheduled"]: index for index, row in enumerate(lookup)
    }
    locked = {}
    for round_number in (1, 2, 3):
        used = {row["trip_id_scheduled"] for row in locked.values()}
        feasible = []
        for row in rows:
            identity = row["trip_id_performed"]
            if row["candidate_rank"] != round_number or identity in locked:
                continue
            order = actual_order[identity]
            lower = [
                schedule_order[v["trip_id_scheduled"]]
                for k, v in locked.items()
                if actual_order[k] < order
            ]
            upper = [
                schedule_order[v["trip_id_scheduled"]]
                for k, v in locked.items()
                if actual_order[k] > order
            ]
            target = schedule_order[row["trip_id_scheduled"]]
            if (
                row["trip_id_scheduled"] in used
                or (lower and target <= max(lower))
                or (upper and target >= min(upper))
            ):
                continue
            feasible.append(row)
        for row in exhaustive_chain(feasible, actual_order, schedule_order):
            locked[row["trip_id_performed"]] = {**row, "round": round_number}
    return locked


def test_three_round_random_small_cases_match_exhaustive_locked_oracle():
    for seed in range(40):
        rng = random.Random(seed)
        actual, lookup = performed(6), scheduled(5)
        rows = [
            candidate(p, s, rank=rank, score=rng.randrange(20))
            for p in range(6)
            for rank, s in enumerate(rng.sample(range(5), 3), 1)
        ]
        oracle = exhaustive_rounds(actual, rows, lookup)
        result = assign_trips(actual, rows, lookup, ["global"])
        assert assigned_pairs(result) == {
            (identity, "global"): row["trip_id_scheduled"]
            for identity, row in oracle.items()
        }
        chosen = [
            row for row in result["assignments"] if row["trip_id_scheduled"] is not None
        ]
        assert all(
            a["scheduled_order"] < b["scheduled_order"]
            for a, b in zip(chosen, chosen[1:], strict=False)
        )
        assert len({row["trip_id_scheduled"] for row in chosen}) == len(chosen)
        assert all(
            row["assignment_round"] == oracle[row["trip_id_performed"]]["round"]
            for row in chosen
        )


def test_shuffles_are_byte_deterministic_and_inputs_are_unchanged():
    actual, lookup = performed(5), scheduled(4)
    rows = [
        candidate(p, (p + rank) % 4, rank=rank, mode=mode, score=p % 2)
        for p in range(5)
        for rank in (1, 2, 3)
        for mode in MODES
    ]
    original = copy.deepcopy((actual, rows, lookup))
    baseline = assign_trips(actual, rows, lookup, list(MODES))
    assert (actual, rows, lookup) == original
    for seed in range(10):
        rng = random.Random(seed)
        shuffled = copy.deepcopy(original)
        for values in shuffled:
            rng.shuffle(values)
        modes = list(MODES)
        rng.shuffle(modes)
        assert json.dumps(
            assign_trips(*shuffled, modes), allow_nan=False
        ) == json.dumps(baseline, allow_nan=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate_rank", True),
        ("candidate_rank", 1.0),
        ("candidate_rank", 0),
        ("score", float("nan")),
        ("score", float("inf")),
        ("score", True),
        ("hard_constraints_valid", None),
        ("hard_constraint_reasons", "invalid"),
    ],
)
def test_malformed_candidate_schema_fails_closed(field, value):
    with pytest.raises(ValueError):
        assign_trips(
            performed(1), [candidate(0, 0, **{field: value})], scheduled(1), ["global"]
        )


def test_duplicate_missing_rank_and_unknown_performed_inputs_fail_closed():
    for rows in (
        [candidate(0, 0), candidate(0, 1)],
        [candidate(0, 0), candidate(0, 1, rank=3)],
        [candidate(0, 0), candidate(0, 0, rank=2)],
        [candidate(99, 0)],
    ):
        with pytest.raises(ValueError):
            assign_trips(performed(1), rows, scheduled(2), ["global"])
    with pytest.raises(ValueError, match="Duplicate performed"):
        assign_trips(performed(1) * 2, [], scheduled(1), ["global"])
    with pytest.raises(ValueError, match="Duplicate authoritative"):
        assign_trips(performed(1), [], scheduled(1) * 2, ["global"])
    with pytest.raises(ValueError, match="consecutive"):
        assign_trips(performed(1), [candidate(0, 0, rank=2)], scheduled(1), ["global"])


@pytest.mark.parametrize("modes", [[], ["global", "global"], ["unknown"], "global"])
def test_invalid_mode_configuration_fails_closed(modes):
    with pytest.raises(ValueError, match="match_modes"):
        assign_trips(performed(1), [], scheduled(1), modes)


@pytest.mark.parametrize(
    "overrides",
    [
        {"scheduled_start_s": -1},
        {"scheduled_start_s": float("nan")},
        {"scheduled_end_s": 0},
        {"scheduled_end_s": None},
        {"service_date": "not-a-date"},
    ],
)
def test_unorderable_authoritative_schedules_are_not_silently_repaired(overrides):
    with pytest.raises(ValueError):
        assign_trips(performed(1), [], scheduled(1, **overrides), ["global"])


def test_empty_performed_list_has_no_invented_assignments():
    result = assign_trips([], [], scheduled(2), list(MODES))
    assert result["assignments"] == []
    assert result["audit"] == []
    assert result["summary"]["assigned_count"] == 0


def test_numeric_total_overflow_is_json_null_not_nonfinite_or_changed_ranking():
    result = assign_trips(
        performed(2),
        [candidate(0, 0, score=1e308), candidate(1, 1, score=1e308)],
        scheduled(2),
        ["global"],
    )
    assert result["summary"]["assigned_count"] == 2
    assert result["summary"]["rounds"][0]["total_existing_score"] is None
    assert result["summary"]["rounds"][0]["total_existing_score_exact"].endswith("/1")
    json.dumps(result, allow_nan=False)
