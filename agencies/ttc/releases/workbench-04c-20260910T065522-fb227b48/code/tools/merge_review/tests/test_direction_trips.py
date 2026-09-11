"""Direction evidence feasibility, not ground-truth trip/match accuracy."""

import copy
import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from tools.merge_review import direction_trips as dt

T = datetime(2023, 11, 6)


def at(seconds):
    return T + timedelta(seconds=seconds)


def ping(rid, seconds=0, direction="NORTH", **fields):
    return dict(rid=rid, vehicle="9027", source_row=1, time=at(seconds),
                destination=f"{direction} to terminal" if direction else "#N/A",
                route="29", **fields)


def apc(rid, seconds=0, end=None, direction="NORTH", **fields):
    row = dict(rid=rid, vehicle="9027", source_row=1, arrival=at(seconds),
               departure=at(seconds if end is None else end), direction=direction,
               route="29", branch="29D", boarding=2.5, arrival_load=9.5)
    row.update(fields)
    return row


def build(avl=(), counts=(), **kwargs):
    avl, counts = list(avl), list(counts)
    before = copy.deepcopy((avl, counts))
    result = dt.build_direction_trips(avl, counts, **kwargs)
    assert (avl, counts) == before
    check = dt.validate_direction_trips(avl, counts, result, kwargs.get("start", T),
                                       kwargs.get("end", T + timedelta(days=1)))
    assert not any(check["violations"].values()), check
    return result


def observation(result, rid):
    return next(r for r in result["observations"] if r["rid"] == rid)


def test_empty_and_all_filtered_have_no_fabricated_segment():
    assert build()["segments"] == []
    avl = [dict(ping("p"), route=" PI "), dict(ping("q", 10), route="po")]
    result = build(avl)
    assert result["observations"] == result["segments"] == result["evidence"] == []
    assert {r["rid"] for r in result["filtered_avl"]} == {"p", "q"}
    assert result["summary"]["retained_counts"] == {"avl": 0, "apc": 0}


def test_filter_is_exact_canonical_route_not_destination_or_apc_branch():
    avl = [dict(ping(str(i), i, direction=None), route=route)
           for i, route in enumerate(["PI", " PO ", "pi", "PILOT", "29PI", "PO29", "", None])]
    result = build(avl, [apc("a", branch="PI", direction=None)])
    assert [r["rid"] for r in result["filtered_avl"]] == ["0", "1", "2"]
    assert len(result["observations"]) == 6
    assert result["segments"][0]["direction"] is None
    assert observation(result, "a")["branch"] == "PI"


def test_no_heading_route_or_position_cut_and_original_payload_preserved():
    avl = [dict(ping("a", 0), route="29", heading=180, payload_hash=2**64 - 2,
                lat=0, extra={"x": [1, 2]}),
           dict(ping("b", 20), route="929", heading=0, lat=89)]
    result = build(avl, [apc("a", 0, 20)])
    assert len(result["segments"]) == 1
    assert result["observations"][0]["extra"] == {"x": [1, 2]}
    assert result["observations"][0]["payload_hash"] == 2**64 - 2
    assert {(r["source"], r["rid"]) for r in result["observations"]} == {("avl", "a"), ("avl", "b"), ("apc", "a")}
    assert not any("witnesses" in r or "score" in r or "segment_id" in r for r in result["observations"])


@pytest.mark.parametrize("value,label", [
    ("NORTH to X", "NORTH"), (" south - Y", "SOUTH"), ("North", "NORTH"),
    ("NORTHBOUND", None), ("X NORTH", None), ("#N/A", None), (None, None),
])
def test_destination_first_word_not_substring(value, label):
    result = build([dict(ping("p"), destination=value)])
    assert result["observations"][0]["direction_label"] == label


@pytest.mark.parametrize("value,label", [(" north ", "NORTH"), ("SOUTH", "SOUTH"), ("N", None), ("NORTH to X", None)])
def test_apc_exact_direction_label(value, label):
    result = build(counts=[apc("a", direction=value)])
    assert result["observations"][0]["direction_label"] == label


def test_unknown_gap_does_not_split_or_claim_full_trip():
    result = build([ping("a", 20), ping("unknown", 25, None), ping("b", 10000)])
    assert len(result["segments"]) == 1
    segment = result["segments"][0]
    assert segment["first_definite_at"] == at(20)
    assert segment["unknown_duration_s"] == 86395
    assert segment["complete_trip"] is False
    assert observation(result, "a")["direction_evidence_end"] == at(25)
    assert observation(result, "b")["direction_interval_kind"] == "avl_last_point"
    assert observation(result, "b")["direction_evidence_end"] == at(10000)


def test_long_gap_caps_hold_at_30_seconds():
    result = build([ping("a"), ping("b", 3600)])
    assert observation(result, "a")["direction_evidence_end"] == at(30)
    assert len(result["segments"]) == 1
    assert result["summary"]["unknown_duration_s"] == 86370


def test_pi_interruption_stops_prior_avl_hold():
    result = build([ping("a"), dict(ping("pi", 5), route="PI"), ping("b", 20)],
                   [apc("s", 6, 19, "SOUTH")])
    assert observation(result, "a")["direction_evidence_end"] == at(5)
    assert result["conflicts"] == []
    assert [s["direction"] for s in result["segments"]] == ["NORTH", "SOUTH", "NORTH"]
    assert [s["start"] for s in result["segments"]] == [at(0), at(6), at(20)]


def test_cross_source_conflict_keeps_prior_candidate_until_definite_reversal():
    result = build([ping("n"), ping("s", 20, "SOUTH")], [apc("a", 10, 25, "SOUTH")])
    assert len(result["segments"]) == 2
    assert result["segments"][1]["start"] == at(20)
    conflict = result["conflicts"][0]
    assert (conflict["start"], conflict["end"]) == (at(10), at(20))
    assert conflict["start_inclusive"] and not conflict["end_inclusive"]
    assert conflict["types"] == ["cross_source_direction"]
    assert conflict["north_source_rids"] == [{"source": "avl", "rid": "n"}]
    assert conflict["south_source_rids"] == [{"source": "apc", "rid": "a"}]
    assert len(observation(result, "a")["candidate_segment_ids"]) == 2
    assert observation(result, "a")["candidate_boundary_overlap"] is True


def test_closed_apc_endpoint_conflict_then_right_limit_reversal():
    result = build(counts=[apc("n", 0, 10), apc("s", 10, 20, "SOUTH")])
    assert len(result["segments"]) == 2
    old, new = result["segments"]
    assert old["end"] == new["start"] == at(10)
    assert old["end_inclusive"] and not new["start_inclusive"]
    assert new["boundary_phase"] == "right_limit"
    assert result["conflicts"][0]["kind"] == "point"
    assert result["conflicts"][0]["types"] == ["apc_internal_direction"]
    assert observation(result, "n")["candidate_segment_ids"] == [old["id"]]
    assert observation(result, "s")["candidate_segment_ids"] == [old["id"], new["id"]]


def test_same_time_apc_opposites_are_not_ordered_and_no_majority_vote():
    counts = [apc("n1", 0, 10), apc("n2", 0, 10), apc("s", 0, 10, "SOUTH")]
    result = build(counts=counts)
    assert result["segments"][0]["direction"] is None
    assert len(result["segments"]) == 1
    assert result["conflicts"][0]["types"] == ["apc_internal_direction", "apc_same_arrival_opposite"]
    assert result == build(counts=counts[::-1])
    assert len(result["observations"]) == 3


def test_same_time_avl_opposites_and_unknown_next_timestamp():
    avl = [ping("n", 0), ping("s", 0, "SOUTH"), ping("u", 5, None), ping("n2", 20)]
    result = build(avl)
    assert result["conflicts"][0]["types"] == ["avl_same_time_opposite"]
    assert result["conflicts"][0]["end"] == at(5)
    assert observation(result, "n")["direction_evidence_end"] == at(5)
    assert len(result["segments"]) == 1


def test_departure_fallback_tie_is_not_mislabeled_same_arrival():
    result = build(counts=[apc("n", arrival=None, departure=at(0)), apc("s", 0, 10, "SOUTH")])
    assert result["conflicts"][0]["types"] == ["apc_internal_direction"]


@pytest.mark.parametrize("fields,kind,expected", [
    ({"departure": at(-2)}, "apc_reversed_arrival_point", at(0)),
    ({"departure": None}, "apc_arrival_only_point", at(0)),
    ({"arrival": None, "departure": at(7)}, "apc_departure_fallback_point", at(7)),
    ({"arrival": None, "departure": None}, "missing_time", None),
    ({}, "apc_zero_duration_point", at(0)),
])
def test_invalid_and_missing_intervals_are_explicit_points(fields, kind, expected):
    result = build(counts=[apc("a", **fields)])
    row = result["observations"][0]
    assert row["direction_interval_kind"] == kind
    assert row["direction_evidence_start"] == row["direction_evidence_end"] == expected
    assert bool(row["candidate_segment_ids"]) is (expected is not None)
    assert bool(result["evidence"]) is (expected is not None)


@pytest.mark.parametrize("flag", ["invalid", "time_ambiguous", "time_nonexistent"])
def test_flagged_record_kept_without_trusted_direction(flag):
    result = build([dict(ping("p"), **{flag: True})], [apc("a", 0, 20, "SOUTH", **{flag: True})])
    assert len(result["observations"]) == 2
    assert result["evidence"] == result["conflicts"] == []
    assert result["segments"][0]["direction"] is None
    assert all(flag in r["direction_exclusion_reasons"] for r in result["observations"])


def test_cross_midnight_departure_preserved_but_evidence_day_censored():
    counts = [apc("a", 86390, 86460)]
    result = build(counts=counts)
    row = result["observations"][0]
    assert row["departure"] == at(86460)
    assert row["direction_evidence_end"] == at(86400)
    assert row["direction_day_censored"] is True
    assert row["direction_end_inclusive"] is False
    assert result["segments"][-1]["end"] == at(86400)


def test_missing_and_outside_scope_records_preserved_without_fake_membership():
    result = build([dict(ping("missing"), time=None), ping("outside", 86401)],
                   [apc("old", -20, -10)])
    assert len(result["observations"]) == 3
    assert all(r["candidate_segment_ids"] == [] for r in result["observations"])
    assert result["summary"]["unresolved_observations"] == 3


def test_subsecond_evidence_not_lost_in_reference_midpoint_rounding():
    result = build(counts=[apc("n", 0, 0.000001), apc("s", 0.000001, 0.000002, "SOUTH")])
    assert len(result["segments"]) == 2
    assert result["conflicts"][0]["kind"] == "point"


def test_permutation_invariance_random_overlaps_and_source_conservation():
    rng = random.Random(22)
    for trial in range(20):
        avl = [dict(ping(f"p{i}", rng.randrange(100), rng.choice(["NORTH", "SOUTH", None])),
                    route=rng.choice(["29", "929", "PI", "#N/A"])) for i in range(12)]
        counts = [apc(f"a{i}", rng.randrange(100), rng.randrange(120), rng.choice(["NORTH", "SOUTH", None])) for i in range(9)]
        result = build(avl, counts)
        rng.shuffle(avl)
        rng.shuffle(counts)
        assert result == build(avl, counts), trial


def test_validator_does_not_call_builder_or_builder_interval_and_sweep_helpers(monkeypatch):
    avl, counts = [ping("n"), ping("s", 20, "SOUTH")], [apc("a", 10, 25, "SOUTH")]
    result = build(avl, counts)
    def forbidden(*args, **kwargs):
        raise AssertionError("validator must not rerun builder")
    for name in ("build_direction_trips", "_describe", "_atoms", "_label", "_filtered"):
        monkeypatch.setattr(dt, name, forbidden)
    assert not any(dt.validate_direction_trips(avl, counts, result)["violations"].values())


@pytest.mark.parametrize("mutation,violation", [
    (lambda r: r["observations"].pop(), "stream_missing_source_records"),
    (lambda r: r["observations"][0].update(destination="changed"), "stream_original_field_changed"),
    (lambda r: r["observations"][0].update(direction_label="SOUTH"), "evidence_definition"),
    (lambda r: r["observations"][0].update(candidate_segment_ids=[]), "observation_membership"),
    (lambda r: r["observations"][0].update(direction_conflict_ids=[]), "observation_conflicts"),
    (lambda r: r["evidence"].pop(), "evidence_inventory"),
    (lambda r: r["segments"][1].update(start=at(19)), "segment_boundary_evidence"),
    (lambda r: r["segments"][0].update(direction="SOUTH"), "segment_boundary_evidence"),
    (lambda r: r["segments"][0].update(source_rids=[]), "segment_membership"),
    (lambda r: r["segments"][0].update(complete_trip=True), "fabricated_trip_certainty"),
    (lambda r: r["conflicts"].clear(), "conflict_coverage"),
    (lambda r: r["conflicts"][0].update(source_rids=[]), "conflict_evidence"),
    (lambda r: r["conflicts"][0].update(types=[]), "conflict_evidence"),
    (lambda r: r["summary"].update(accuracy=1), "summary_changed"),
])
def test_independent_validator_detects_structural_and_evidence_mutations(mutation, violation):
    avl, counts = [ping("n"), ping("s", 20, "SOUTH")], [apc("a", 10, 25, "SOUTH")]
    result = build(avl, counts)
    mutation(result)
    check = dt.validate_direction_trips(avl, counts, result)
    assert check["violations"][violation] > 0, check


def test_filtered_audit_and_json_roundtrip_keep_original_timestamp_and_uint64():
    avl = [dict(ping("pi"), route="pi", payload_hash=2**64 - 1,
                extra={"start": "2023-11-06T01:00:00"}), ping("p", 10)]
    counts = [apc("a", 10, 20)]
    result = build(avl, counts)
    saved = json.loads(json.dumps(result, default=lambda value: value.isoformat()))
    restored = dt.restore_metadata(saved)
    assert restored == result
    assert not any(dt.validate_direction_trips(avl, counts, restored)["violations"].values())
    restored["filtered_avl"][0]["route"] = "PO"
    assert dt.validate_direction_trips(avl, counts, restored)["violations"]["filtered_original_fields"] == 1


@pytest.mark.parametrize("changes", [
    {"vehicle": ""}, {"time": T.replace(tzinfo=UTC)}, {"time": "2023-11-06"},
    {"direction_label": "NORTH"}, {"candidate_segment_ids": []},
])
def test_unsafe_input_is_rejected_without_overwriting_original(changes):
    with pytest.raises(ValueError):
        dt.build_direction_trips([dict(ping("p"), **changes)], [])


def test_multiple_vehicles_or_duplicate_source_ids_are_rejected():
    with pytest.raises(ValueError):
        dt.build_direction_trips([ping("p"), ping("p")], [])
    with pytest.raises(ValueError):
        dt.build_direction_trips([ping("p")], [apc("a", vehicle="other")])
