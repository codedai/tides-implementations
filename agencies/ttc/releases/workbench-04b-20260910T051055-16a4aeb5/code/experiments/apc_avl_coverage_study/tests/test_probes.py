"""Checks that probes measure source membership without circular accuracy claims."""

from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl

from experiments.apc_avl_coverage_study.probes import (
    aggregate_summaries,
    compare_membership,
    owner_partition,
    perturbations,
    select_edge_windows,
    select_windows,
)


def result(owners: dict, assignments: list[tuple]) -> dict:
    return {
        "segmentation": {
            "segments": [
                {"segment_id": sid, "ping_ids": members}
                for sid, members in owners.items()
            ]
        },
        "assignments": [
            {
                "apc_rid": rid,
                "segment_id": owner,
                "status": status,
                "reason": status,
                "ping_ids": [],
            }
            for rid, owner, status in assignments
        ],
    }


def test_renamed_chunk_ids_do_not_mean_a_neighbor_switch():
    old = result({"old-id": ["p1", "p2"]}, [("a1", "old-id", "direct")])
    new = result({"new-id": ["p1", "p2"]}, [("a1", "new-id", "direct")])
    summary, details = compare_membership(old, new)
    assert summary["unchanged_owner_membership"] == 1
    assert details[0]["surviving_owner_membership_jaccard"] == 1
    assert summary["disjoint_owner_switch"] == 0


def test_masked_tail_uses_common_lineage_for_primary_owner_comparison():
    old = result({"old": ["p1", "p2", "p3"]}, [("a1", "old", "direct")])
    new = result({"new": ["p1", "p2"]}, [("a1", "new", "inferred")])
    summary, details = compare_membership(old, new)
    assert details[0]["surviving_owner_membership_jaccard"] == 1
    assert details[0]["full_owner_membership_jaccard"] == 2 / 3
    assert summary["direct_to_inferred_same_membership"] == 1


def test_surviving_disjoint_owner_is_reported_separately_from_abstention():
    owners = {"old": ["p1", "p2"], "neighbor": ["p3", "p4"]}
    old = result(owners, [("a1", "old", "direct"), ("a2", "old", "direct")])
    new = result(owners, [("a1", "neighbor", "direct"), ("a2", None, "ambiguous")])
    summary, details = compare_membership(old, new)
    assert summary["disjoint_owner_switch"] == 1
    assert summary["reference_assigned_then_abstained"] == 1
    assert details[0]["surviving_owner_membership_jaccard"] == 0
    assert details[1]["surviving_owner_membership_jaccard"] is None


def test_entire_reference_owner_removed_is_not_declared_a_wrong_neighbor():
    old = result({"old": ["p1"], "other": ["p2"]}, [("a1", "old", "direct")])
    new = result({"other-new": ["p2"]}, [("a1", "other-new", "direct")])
    summary, details = compare_membership(old, new)
    assert summary["owner_support_not_comparable"] == 1
    assert summary.get("disjoint_owner_switch", 0) == 0
    assert details[0]["surviving_owner_membership_jaccard"] is None


def test_duplicate_lineage_origin_mapping_avoids_inflating_membership_change():
    old = result({"old": ["p1", "p2"]}, [("a1", "old", "direct")])
    new = result({"new": ["p1", "p2", "d1", "d2"]}, [("a1", "new", "direct")])
    summary, _ = compare_membership(old, new, origins={"d1": "p1", "d2": "p2"})
    assert summary["unchanged_owner_membership"] == 1


def test_projected_duplicate_uncertain_singletons_are_one_owner_membership_set():
    old = result({"uncertain-1": ["p1"], "uncertain-2": ["p2"]}, [])
    new = result({"u1": ["p1"], "u2": ["p2"], "d1": ["copy1"], "d2": ["copy2"]}, [])
    assert owner_partition(old) == owner_partition(new, {"copy1": "p1", "copy2": "p2"})


def test_selection_is_deterministic_and_does_not_require_algorithm_assignments():
    start = datetime(2024, 1, 1)
    avl, apc = [], []
    for vehicle in ("a", "b", "c"):
        for day in range(2):
            base = start + timedelta(days=day)
            avl.extend(
                {"vehicle": vehicle, "time": base + timedelta(minutes=i)}
                for i in range(120)
            )
            apc.extend(
                {"vehicle": vehicle, "arrival": base + timedelta(minutes=i * 5)}
                for i in range(24)
            )
    aa, pp = pl.from_dicts(avl), pl.from_dicts(apc)
    chosen = select_windows(aa, pp, limit=2)
    assert chosen == select_windows(aa.reverse(), pp.reverse(), limit=2)
    assert len(chosen) == 2
    assert all(r["overlap_envelope_seconds"] >= 3600 for r in chosen)
    assert all(
        (r["window_end"] - r["window_start"]).total_seconds() == 7200 for r in chosen
    )


def test_masking_preserves_source_intervals_and_leaves_apc_tail_rows():
    base = datetime(2024, 1, 1)
    avl = [{"rid": f"p{i}", "time": base + timedelta(minutes=i)} for i in range(120)]
    apc = [
        {
            "rid": f"a{i}",
            "arrival": base + timedelta(minutes=i),
            "departure": base + timedelta(minutes=i, seconds=15),
        }
        for i in range(120)
    ]
    selection = {
        "window_start": base,
        "apc_prefix_cutoff": base + timedelta(minutes=15),
        "avl_suffix_cutoff": base + timedelta(minutes=106),
    }
    aa, pp = perturbations(avl, apc, selection)["offset_coverage_15m_13m"]
    assert aa[-1]["time"] < pp[-1]["arrival"]
    assert pp[0] == apc[15]
    assert pp[-1] == apc[-1]
    assert len(avl) == len(apc) == 120


def test_aggregation_weights_jaccard_by_comparable_rows_not_vehicle_means():
    rows = [
        {
            "combination": "s__m",
            "probe": "mask",
            "membership_jaccard_sum": 1,
            "membership_jaccard_count": 1,
            "mean_surviving_owner_membership_jaccard": 1,
        },
        {
            "combination": "s__m",
            "probe": "mask",
            "membership_jaccard_sum": 0,
            "membership_jaccard_count": 3,
            "mean_surviving_owner_membership_jaccard": 0,
        },
    ]
    summary = aggregate_summaries(rows)["s__m__mask"]
    assert summary["mean_surviving_owner_membership_jaccard"] == 0.25
    assert summary["accuracy"] is None


def test_clock_probe_marks_a_shift_into_dst_ambiguity():
    base = datetime(2024, 11, 3, 0, 59)
    row = {"rid": "a", "arrival": base, "departure": base}
    selection = {
        "window_start": base.replace(minute=0),
        "apc_prefix_cutoff": base,
        "avl_suffix_cutoff": base,
    }
    _, changed = perturbations([], [row], selection)["apc_clock_+120s"]
    assert changed[0]["time_ambiguous"]
    assert changed[0]["arrival"] == base + timedelta(minutes=2)
    assert "time_ambiguous" not in row


def test_edge_selection_is_raw_feature_driven_deterministic_and_stratified():
    avl, apc = [], []
    for vehicle, base in (
        ("terminal", datetime(2023, 11, 30, 12)),
        ("dst", datetime(2023, 11, 5, 0, 30)),
    ):
        for minute in range(120):
            instant = base + timedelta(minutes=minute)
            if vehicle == "terminal" and 45 <= minute <= 50:
                continue
            avl.append(
                {
                    "rid": f"{vehicle}-p-{minute}",
                    "vehicle": vehicle,
                    "time": instant,
                    "time_ambiguous": vehicle == "dst" and instant.hour == 1,
                    "time_nonexistent": False,
                }
            )
        for minute in range(0, 120, 5):
            instant = base + timedelta(minutes=minute)
            apc.append(
                {
                    "rid": f"{vehicle}-a-{minute}",
                    "vehicle": vehicle,
                    "arrival": instant,
                    "departure": instant + timedelta(seconds=10),
                    "stop": str(minute),
                    "direction": "E",
                    "time_ambiguous": vehicle == "dst" and instant.hour == 1,
                    "time_nonexistent": False,
                }
            )
    for i, minute, direction in ((0, 60, "E"), (1, 61, "W"), (2, 130, "W")):
        instant = datetime(2023, 11, 30, 12) + timedelta(minutes=minute)
        apc.append(
            {
                "rid": f"terminal-special-{i}",
                "vehicle": "terminal",
                "arrival": instant,
                "departure": instant + timedelta(minutes=3),
                "stop": "terminal",
                "direction": direction,
                "time_ambiguous": False,
                "time_nonexistent": False,
            }
        )
    aa, pp = pl.from_dicts(avl), pl.from_dicts(apc)
    selected, status = select_edge_windows(aa, pp, per_stratum=1)
    assert len(selected) == 4
    assert all(s["selected_windows"] == 1 for s in status.values())
    assert (selected, status) == select_edge_windows(aa.reverse(), pp.reverse(), 1)
    assert len({s["window_case"] for s in selected}) == 4
    assert all("raw_feature_evidence" in s for s in selected)


def test_dst_selector_centers_the_ambiguous_departure_not_the_unique_arrival_hour():
    base = datetime(2023, 11, 5)
    avl = [
        {
            "rid": f"p{i}",
            "vehicle": "dst",
            "time": base + timedelta(minutes=i),
            "time_ambiguous": False,
            "time_nonexistent": False,
        }
        for i in [0, 30, 59, 120, 150]
    ]
    apc = [
        {
            "rid": "a",
            "vehicle": "dst",
            "arrival": base + timedelta(minutes=59),
            "departure": base + timedelta(minutes=62),
            "stop": "t",
            "direction": "E",
            "time_ambiguous": True,
            "time_nonexistent": False,
        }
    ]
    selected, _ = select_edge_windows(pl.from_dicts(avl), pl.from_dicts(apc), 1)
    dst = next(
        row for row in selected if row["selection_stratum"] == "dst_ambiguous_hour"
    )
    assert dst["window_start"] == base + timedelta(minutes=30)
    assert dst["window_end"] == base + timedelta(minutes=150)
    assert dst["raw_feature_evidence"]["hour"].hour == 1
