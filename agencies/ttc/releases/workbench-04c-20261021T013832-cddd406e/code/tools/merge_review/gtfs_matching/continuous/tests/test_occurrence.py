"""Local-confusability and occurrence retrieval without historical special cases."""

import copy

import pytest

from tools.merge_review.gtfs_matching.canonical import geometry
from tools.merge_review.gtfs_matching.continuous.config import get_config
from tools.merge_review.gtfs_matching.continuous.retrieval import SchedulePool, retrieve
from tools.merge_review.gtfs_matching.continuous.tests.test_continuous_pipeline import (
    apc,
    points,
    schedule,
)
from tools.merge_review.gtfs_matching.retrieval import trip_key
from tools.merge_review.gtfs_matching.windows import observation_key


def pool(trips, references=None, service_date=None, **changes):
    c = get_config({"continuous_occurrence_model_enabled": True, **changes})
    return SchedulePool(trips, c, references=references, service_date=service_date), c


def trip(name, offset=0, pattern=None, **changes):
    result = schedule(name, offset)
    result.update(pattern_id=pattern or name, **changes)
    return result


def context(p, t, observed_t=720, bounds=(0, 1800), anchor=600, stops=()):
    return p.confusability(t, bounds, anchor, observed_t, stops)


def test_different_pattern_ids_share_local_geometry_stop_class_and_headway():
    trips = [trip(str(i), i * 600) for i in range(4)]
    p, _ = pool(trips)
    contexts = [context(p, t) for t in trips]
    assert len({c["class_id"] for c in contexts}) == 1
    assert all(c["member_ids"] == sorted(map(trip_key, trips)) for c in contexts)
    assert all(c["local_headway_s"] == 600 for c in contexts)
    assert contexts[1]["previous_headway_s"] == contexts[1]["next_headway_s"] == 600
    assert contexts[1]["headway_source"] == "local_confusability_time_bin"
    assert contexts[1]["exact_pattern_filter_used"] is False


def test_branch_difference_outside_observed_range_is_not_pattern_filter():
    first, other = trip("first"), trip("branch", 600)
    other["scheduled"][-1]["stop_id"] = "different-terminal"
    p, _ = pool([first, other])
    assert context(p, first)["member_ids"] == context(p, other)["member_ids"]


def test_unobserved_stop_order_conflicts_remain_shared_competition():
    first, bad = trip("first"), trip("reversed-stop-order", 600)
    bad["scheduled"][1]["stop_id"], bad["scheduled"][2]["stop_id"] = "stop-2", "stop-1"
    p, _ = pool([first, bad])
    for t in (first, bad):
        c = context(p, t)
        assert c["member_ids"] == sorted(map(trip_key, [first, bad]))
        assert c["stop_order_ambiguous"]
        assert c["pairwise_stop_order_conflicts"] == 1
    assert context(p, first, stops=("stop-1", "stop-2"))["member_ids"] == [
        trip_key(first)
    ]
    assert not context(p, bad, stops=("stop-1", "stop-2"))["candidate_compatible"]
    first["scheduled"][2]["stop_id"] = "stop-0"
    bad["scheduled"][1]["stop_id"] = "stop-0"
    bad["scheduled"][2]["stop_id"] = "stop-1"
    p, _ = pool([first, bad])
    assert context(p, first)["member_ids"] == sorted(map(trip_key, [first, bad]))
    assert context(p, first, stops=("stop-0", "stop-1", "stop-0"))["member_ids"] == [
        trip_key(first)
    ]


def test_nontransitive_hypothetical_stop_orders_cannot_choose_private_headway():
    trips = [trip("a", 0), trip("b", 1800), trip("c", 600)]
    for t, labels in zip(
        trips, [("a", "b", "c"), ("a", "x", "c"), ("b", "a", "x")], strict=True
    ):
        t["scheduled"] = t["scheduled"][:3]
        for stop, label in zip(t["scheduled"], labels, strict=True):
            stop["stop_id"] = label
    p, _ = pool(trips)
    contexts = [context(p, t, bounds=(0, 1200)) for t in trips]
    assert len({c["class_id"] for c in contexts}) == 1
    assert len({c["local_headway_s"] for c in contexts}) == 1
    assert all(c["member_ids"] == sorted(map(trip_key, trips)) for c in contexts)
    assert all(
        c["stop_order_ambiguous"] and c["headway_confidence"] < 0.9 for c in contexts
    )
    e = p.occurrence_evidence(trips[0], 600, 720, context=contexts[0])
    assert e["crossed_occurrences"] == 1


def test_invalid_schedule_cannot_hide_ready_sibling_with_same_stop_structure():
    invalid, ready = trip("invalid"), trip("ready", 600)
    invalid["scheduled"][1]["arrival_t"] = -1
    p, _ = pool([invalid, ready])
    assert context(p, ready)["member_ids"] == [trip_key(ready)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("feed_version", "other-feed"),
        ("route_id", "other-route"),
        ("direction_id", "southbound"),
    ],
)
def test_namespace_excludes_other_feed_route_and_direction(field, value):
    first, other = trip("first"), trip("other", 600, **{field: value})
    p, _ = pool([first, other])
    assert context(p, first)["member_ids"] == [trip_key(first)]


def test_active_previous_day_tail_competes_on_shared_clock_unless_date_scoped():
    previous = trip("tail", 26 * 3600, service_date="2019-05-02")
    current = trip("today", 26 * 3600 + 600, service_date="2019-05-03")
    third = trip("later", 26 * 3600 + 1200, service_date="2019-05-03")
    trips = [previous, current, third]
    p, _ = pool(trips)
    observed_t = 26 * 3600 + 720
    c = context(p, previous, observed_t=observed_t)
    assert c["member_ids"] == sorted(map(trip_key, trips))
    assert c["local_headway_s"] == 600
    assert c["service_date_scope"] is None
    assert (
        p.occurrence_evidence(previous, 600, observed_t, context=c)[
            "crossed_occurrences"
        ]
        == 1
    )
    scoped, _ = pool(trips, service_date="2019-05-03")
    c = context(scoped, current, observed_t=observed_t)
    assert c["member_ids"] == sorted(map(trip_key, [current, third]))
    assert c["active_service_dates"] == ["2019-05-03"]


def test_no_sparse_pattern_3600_substitution_and_real_low_frequency_supported():
    sparse = [trip("sparse-a"), trip("sparse-b", 12000)]
    p, c = pool(sparse)
    actual = context(p, sparse[0])
    assert actual["local_headway_s"] == 600
    assert actual["headway_source"] == "configured_default"
    legacy = SchedulePool(sparse, {**c, "continuous_occurrence_model_enabled": False})
    # Put both in the same legacy pattern to expose its historical clipped gap.
    for t in sparse:
        t["pattern_id"] = "same"
    legacy = SchedulePool(sparse, {**c, "continuous_occurrence_model_enabled": False})
    assert legacy.headway(sparse[0], 600) == 3600
    low_frequency = [trip(str(i), i * 2700) for i in range(4)]
    p, _ = pool(low_frequency)
    assert context(p, low_frequency[0])["local_headway_s"] == 2700


def test_service_break_is_not_averaged_with_regular_headway():
    trips = [trip("a", 0), trip("b", 600), trip("after-break", 26000)]
    p, _ = pool(trips)
    c = context(p, trips[0])
    assert c["local_headway_s"] == 600
    assert c["headway_source"] == "configured_default"


def test_route_direction_timebin_fallback_has_explicit_lower_confidence():
    first = trip("isolated-branch")
    trunk = [
        trip(f"trunk-{i}", i * 600, reference_id="separate-reference") for i in range(4)
    ]
    p, _ = pool([first, *trunk])
    c = context(p, first)
    assert c["member_ids"] == [trip_key(first)]
    assert c["headway_source"] == "route_direction_time_bin"
    assert c["local_headway_s"] == 600 and c["headway_confidence"] < 0.9


def test_crossed_occurrences_plus_midpoint_cell_distance_cost():
    trips = [trip(str(i), i * 600) for i in range(4)]
    p, _ = pool(trips)
    late = p.occurrence_evidence(trips[0], 600, 720, context=context(p, trips[0]))
    ontime = p.occurrence_evidence(trips[1], 600, 720, context=context(p, trips[1]))
    early = p.occurrence_evidence(trips[2], 600, 720, context=context(p, trips[2]))
    assert late["crossed_occurrences"] == early["crossed_occurrences"] == 1
    assert late["outside_cell_s"] == early["outside_cell_s"] == 300
    assert late["displacement_cost"] == early["displacement_cost"] == 1.5
    assert ontime["displacement_cost"] == 0
    assert late["independent_delay_proof"] is False


def test_simultaneous_occurrences_are_ties_not_extra_crossings():
    trips = [trip("a"), trip("simultaneous-a"), trip("b", 600), trip("c", 1200)]
    p, _ = pool(trips)
    evidence = p.occurrence_evidence(trips[0], 600, 720, context=context(p, trips[0]))
    assert evidence["crossed_occurrences"] == 1
    assert len(evidence["simultaneous_candidate_ids"]) == 2
    assert evidence["next_headway_s"] == 600


def test_dwell_interval_is_not_replaced_by_a_midpoint_residual():
    first, other = trip("a"), trip("b", 30)
    first["scheduled"][1]["departure_t"] = 180
    other["scheduled"][1]["departure_t"] = 210
    p, _ = pool([first, other])
    for t in (first, other):
        e = p.occurrence_evidence(t, 600, 170, context=context(p, t, observed_t=170))
        assert e["crossed_occurrences"] == 0 and e["displacement_cost"] == 0


def test_incompatible_candidate_has_no_private_singleton_privilege():
    first = trip("wrong")
    p, _ = pool([first])
    c = context(p, first, stops=("missing-observed-stop",))
    assert c["candidate_compatible"] is False and c["member_ids"] == []
    e = p.occurrence_evidence(first, 600, 720, context=c)
    assert e["status"] == "NO_OCCURRENCE_EVIDENCE"
    assert e["crossed_occurrences"] is e["outside_cell_s"] is None


def reference(name, *, offset=0, reverse=False, midpoint=False):
    pts = [{"lat": 43.65, "lon": -79.4 + offset}, {"lat": 43.68, "lon": -79.4 + offset}]
    if midpoint:
        pts.insert(1, {"lat": 43.665, "lon": -79.4 + offset})
    if reverse:
        pts.reverse()
    return dict(
        reference_id=name, origin=[43.65, -79.4], points=geometry(pts, [43.65, -79.4])
    )


def test_distinct_s_axes_require_validated_geometric_mapping():
    first, other = trip("a", reference_id="a"), trip("b", 600, reference_id="b")
    without, _ = pool([first, other])
    assert context(without, first)["member_ids"] == [trip_key(first)]
    with_refs, _ = pool([first, other], [reference("a"), reference("b", offset=0.0001)])
    assert context(with_refs, first)["member_ids"] == sorted(
        map(trip_key, [first, other])
    )
    apart, _ = pool([first, other], [reference("a"), reference("b", offset=0.01)])
    assert context(apart, first)["member_ids"] == [trip_key(first)]
    reversed_pool, _ = pool(
        [first, other], [reference("a"), reference("b", reverse=True)]
    )
    assert context(reversed_pool, first)["member_ids"] == [trip_key(first)]


def test_collinear_reference_vertices_do_not_change_class_or_cost():
    trips = [
        trip("a", reference_id="a"),
        trip("b", 600, reference_id="b"),
        trip("c", 1200, reference_id="b"),
    ]
    results = []
    for midpoint in (False, True):
        p, _ = pool(
            trips,
            [reference("a", midpoint=midpoint), reference("b", midpoint=midpoint)],
        )
        c = context(p, trips[0])
        results.append(
            (
                c["member_ids"],
                p.occurrence_evidence(trips[0], 600, 720, context=c)[
                    "displacement_cost"
                ],
            )
        )
    assert results[0][0] == results[1][0]
    assert results[0][1] == pytest.approx(results[1][1], abs=1e-8)


def retrieve_fixture(trips=None, apc_rows=None, rescue=None, **changes):
    trips = trips or [trip(str(i), i * 600) for i in range(10)]
    p, c = pool(
        trips,
        continuous_retrieval_top_k=1,
        continuous_retrieval_delta=0,
        continuous_core_headways=0.01,
        continuous_rescue_headways=0.01,
        continuous_rescue_delay_s=0,
        continuous_occurrence_rescue_neighbors=0,
        **changes,
    )
    rows = points(step=30, delay=1800)
    windows = {
        "windows": [{"id": "w", "observation_ids": [observation_key(r) for r in rows]}]
    }
    return (
        retrieve(
            p, rows, windows, c, route_id="route-A", apc=apc_rows, rescue_context=rescue
        ),
        p,
        c,
    )


def test_core_nearest_previous_next_cannot_be_removed_by_top_k():
    result, _, _ = retrieve_fixture()
    assert {t["trip_id"] for t in result["trips"]} == {"2", "3", "4"}
    assert all(r["protected_from_top_k"] for r in result["windows"][0]["candidates"])


def test_core_includes_simultaneous_ties():
    trips = [trip(str(i), i * 600) for i in range(7)] + [trip("same-time", 1800)]
    result, _, _ = retrieve_fixture(trips)
    assert {t["trip_id"] for t in result["trips"]} == {"2", "3", "4", "same-time"}


def test_extra_neighbor_rescue_and_sequence_rescue_are_provenance_tagged():
    result, p, c = retrieve_fixture()
    index = p.occurrence_index()
    index.config["continuous_occurrence_rescue_neighbors"] = 3
    nearest = next(t for t in p.trips.values() if t["trip_id"] == "3")
    neighbors, _ = index.protected_neighbors(
        context(p, nearest, observed_t=1920), 600, 1920
    )
    assert len(neighbors) == 7
    assert (
        "RESCUE_EXTRA_NEIGHBOR"
        in neighbors[trip_key(next(t for t in p.trips.values() if t["trip_id"] == "0"))]
    )
    far = trip_key(next(t for t in p.trips.values() if t["trip_id"] == "9"))
    result, _, _ = retrieve_fixture(rescue={"sequence_candidate_ids": [far]})
    row = next(
        r for r in result["windows"][0]["candidates"] if r["candidate_id"] == far
    )
    assert "RESCUE_VEHICLE_SEQUENCE_EVIDENCE" in row["retrieval_reasons"]
    assert row["protected_from_top_k"]


@pytest.mark.parametrize("calibrated", [False, True])
def test_only_validated_empirical_tail_expands_retrieval(calibrated):
    trips = [trip(str(i), i * 600) for i in range(10)] + [trip("old", -5400)]
    rescue = dict(
        calibrated=calibrated,
        lower_s=-30,
        upper_s=7500,
        source="independent-history",
        validation_artifact="audit",
        artifact_sha256="a" * 64,
    )
    result, _, _ = retrieve_fixture(trips, rescue=rescue)
    ids = {t["trip_id"] for t in result["trips"]}
    assert ("old" in ids) is calibrated


@pytest.mark.parametrize("confidence", [0, 0.2])
def test_weak_apc_does_not_hard_exclude_occurrence_class(confidence):
    record = apc(stop=2)
    record.update(
        stop_id="unknown", confidence=confidence, interval_start=2000, interval_end=2050
    )
    original = copy.deepcopy(record)
    result, _, _ = retrieve_fixture(apc_rows=[record])
    assert result["windows"][0]["candidates"]
    assert all(r["observed_stop_ids"] == [] for r in result["windows"][0]["candidates"])
    assert record == original


def test_apc_outside_time_or_progress_window_not_used_as_hard_stop_constraint():
    record = apc(stop=2)
    record.update(
        stop_id="unknown", confidence=1, interval_start=-500, interval_end=-400
    )
    result, _, _ = retrieve_fixture(apc_rows=[record])
    assert all(r["observed_stop_ids"] == [] for r in result["windows"][0]["candidates"])


def test_after_midnight_progress_passages_are_not_wrapped():
    trips = [trip(str(i), 26 * 3600 + i * 600) for i in range(4)]
    p, _ = pool(trips)
    at = 26 * 3600 + 720
    e = p.occurrence_evidence(
        trips[0], 600, at, context=context(p, trips[0], observed_t=at)
    )
    assert e["crossed_occurrences"] == 1
    assert e["candidate_passage_interval"][0] > 86400


def test_legacy_flag_does_not_initialize_occurrence_index():
    trips = [trip(str(i), i * 600, pattern="same") for i in range(4)]
    p = SchedulePool(trips, get_config())
    rows = points()
    windows = {
        "windows": [{"id": "w", "observation_ids": [observation_key(r) for r in rows]}]
    }
    retrieve(p, rows, windows, p.config)
    assert p._occurrences is None


def test_empty_evidence_produces_no_candidates_even_with_schedule_neighbors():
    trips = [trip("a"), trip("b", 600)]
    p, c = pool(trips)
    result = retrieve(p, [], {"windows": [{"id": "w", "observation_ids": []}]}, c)
    assert result["candidate_ids"] == []
    assert result["windows"][0]["status"] == "NO_EVIDENCE"
