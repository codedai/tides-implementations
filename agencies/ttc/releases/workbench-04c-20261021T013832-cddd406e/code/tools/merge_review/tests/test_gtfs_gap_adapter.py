"""Child membership is persisted evidence, never inferred from parent annotations."""

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.merge_review.gtfs_matching import METHOD
from tools.merge_review.gtfs_matching.adapter import GTFSReviewData, narrow_gap_detail


def observation(source, rid, t, **extra):
    row = dict(
        source=source,
        rid=rid,
        route="29",
        event_time=t,
        t=t,
        lat=43.65,
        lon=-79.4,
        valid=t is not None,
        in_focus=True,
        candidate_segment_ids=["parent"],
        direction_conflict_ids=[],
        source_fields={"opaque": "unchanged"},
        **extra,
    )
    if source == "apc":
        row.update(arrival=t, departure=t, decision={"mode": "direction_candidate"})
    return row


def parent_detail(rows):
    return dict(
        observations=copy.deepcopy(rows),
        avl=[copy.deepcopy(r) for r in rows if r["source"] == "avl"],
        apc=[copy.deepcopy(r) for r in rows if r["source"] == "apc"],
        window={"start": 0, "end": 86400},
        direction_conflicts=[],
        counts={},
    )


def child(identities, **extra):
    return dict(
        id="parent:gap:0001",
        parent_case_id="parent",
        gap_split=True,
        kind="direction_segment",
        start=0,
        end=86400,
        source_start=100,
        source_end=200,
        source_rids=[{"source": source, "rid": rid} for source, rid in identities],
        **extra,
    )


def test_exact_child_membership_context_and_parent_annotations_are_separate():
    rows = [
        observation("avl", "left", 20),
        observation("avl", "context", 90),
        observation("avl", "shared", 100),
        observation("apc", "shared", 150),
        observation("avl", "right", 8000),
        observation("apc", "missing", None),
    ]
    source = parent_detail(rows)
    original = copy.deepcopy(source)
    summary = child([("avl", "shared"), ("apc", "shared"), ("apc", "missing")])
    result = narrow_gap_detail(source, summary, [summary], 20)
    assert source == original
    assert result["window"]["start"] == 80
    assert result["window"]["end"] == 220  # Not the inherited midnight boundary.
    assert [(r["source"], r["rid"]) for r in result["observations"]] == [
        ("avl", "context"),
        ("avl", "shared"),
        ("apc", "shared"),
        ("apc", "missing"),
    ]
    for key in ("observations", "avl", "apc"):
        for row in result[key]:
            assert row["in_focus"] == (row["rid"] != "context")
            assert row["is_member"] == row["in_focus"]
            assert row["candidate_segment_ids"] == ["parent"]
            assert row["parent_candidate_segment_ids"] == ["parent"]
            assert row["review_candidate_ids"] == (
                [summary["id"]] if row["in_focus"] else []
            )
            assert row["source_fields"] == {"opaque": "unchanged"}
    assert result["avl"][0]["relation"] == "context"
    assert result["avl"][0]["segment_id"] is None
    assert result["avl"][1]["segment_id"] == summary["id"]
    assert result["apc"][-1]["in_window"] is False
    assert all(r["progress_m"] is None for r in result["apc"])


def test_apc_interval_context_overlaps_without_fabricating_avl():
    row = observation("apc", "context", 50)
    row["departure"] = 110
    summary = child([("apc", "focus")])
    result = narrow_gap_detail(
        parent_detail([row, observation("apc", "focus", 150)]), summary, [summary], 0
    )
    assert not result["avl"]
    assert len(result["apc"]) == 2
    assert result["apc"][0]["in_window"]
    assert not result["apc"][0]["is_member"]
    assert result["apc"][0]["arrival"] == 50
    assert result["apc"][0]["departure"] == 110


def test_unlocated_keeps_missing_time_focus_without_whole_parent_context():
    summary = child([("apc", "missing")], gap_unlocated=True)
    summary.update(
        kind="direction_unresolved",
        start=None,
        end=None,
        source_start=None,
        source_end=None,
    )
    result = narrow_gap_detail(
        parent_detail(
            [observation("avl", "unrelated", 150), observation("apc", "missing", None)]
        ),
        summary,
        [summary],
        180,
    )
    assert result["window"]["start"] is None
    assert not result["avl"]
    assert len(result["observations"]) == 1
    assert result["apc"][0]["in_focus"]
    assert not result["apc"][0]["is_candidate_member"]
    assert result["apc"][0]["membership_relation"] == "unresolved"


@pytest.mark.parametrize(
    "identities", [[("avl", "absent")], [("avl", "x"), ("avl", "x")]]
)
def test_missing_or_duplicate_child_identity_fails_closed(identities):
    summary = child(identities)
    with pytest.raises(ValueError):
        narrow_gap_detail(
            parent_detail([observation("avl", "x", 100)]), summary, [summary], 0
        )


def test_conflicts_are_child_local_with_full_parent_audit():
    source = parent_detail(
        [
            observation("avl", "a", 100, direction_label="NORTH"),
            observation("apc", "p", 150),
        ]
    )
    source["direction_conflicts"] = [
        dict(
            id="near",
            start=50,
            end=120,
            source_rids=[dict(source="avl", rid="a"), dict(source="apc", rid="p")],
        ),
        dict(id="far", start=7000, end=8000, source_rids=[dict(source="avl", rid="a")]),
    ]
    for row in source["observations"]:
        row["direction_conflict_ids"] = ["near", "far"]
    summary = child([("avl", "a")])
    result = narrow_gap_detail(source, summary, [summary], 0)
    assert len(result["parent_direction_conflicts"]) == 2
    assert len(result["direction_conflicts"]) == 1
    assert result["direction_conflicts"][0]["start"] == 100
    assert result["direction_conflicts"][0]["parent_start"] == 50
    assert result["observations"][0]["direction_conflict_ids"] == ["near"]


def test_progress_keeps_parent_break_after_child_selection():
    rows = [
        observation("avl", "a", 100),
        observation("avl", "b", 110, break_before=True),
    ]
    summary = child([("avl", "a"), ("avl", "b")])
    result = narrow_gap_detail(parent_detail(rows), summary, [summary], 0)
    assert result["avl"][1]["break_before"]
    assert result["avl"][0]["progress_run"] != result["avl"][1]["progress_run"]


@pytest.mark.parametrize("gap", [False, True])
def test_adapter_loads_parent_and_preserves_legacy_case_contract(monkeypatch, gap):
    from tools.merge_review.gtfs_matching import adapter

    source = parent_detail(
        [observation("avl", "a", 100), observation("avl", "far", 5000)]
    )
    summary = (
        child([("avl", "a")])
        if gap
        else dict(id="parent", kind="direction_segment", start=0, end=86400)
    )
    summary.update(avl_count=1 if gap else 2, apc_count=0)
    loaded = []
    obj = GTFSReviewData.__new__(GTFSReviewData)
    obj.run = Path("/tmp/test-gap-adapter")
    obj.vehicles = {"v": "9027"}
    obj._catalog = {"v": [summary]}
    obj.manifest = dict(
        gtfs_selection={}, gtfs_feed_version="test", gtfs_archive_sha256="hash"
    )
    obj.output_hashes = {f"cases/{adapter.artifact_key(summary['id'])}.json": "hash"}
    obj.canonical = {}
    obj._verify_parent = lambda: None
    obj._verify_file = lambda *args: None
    obj.parent = SimpleNamespace(
        run=Path("/tmp/parent"),
        case=lambda method, vehicle, case_id, context: (
            loaded.append(case_id) or copy.deepcopy(source)
        ),
    )
    monkeypatch.setattr(
        adapter,
        "read",
        lambda path: (
            {"selected_count": 2, "excluded_count": 0} if "scope" in str(path) else {}
        ),
    )
    monkeypatch.setattr(
        adapter, "public_gtfs", lambda *args: {"projection": {"points": []}}
    )
    result = obj.case(METHOD, "v", summary["id"], context_s=0)
    assert loaded == ["parent"]
    assert result["case"]["id"] == summary["id"]
    assert len(result["observations"]) == (1 if gap else 2)
    assert result["counts"]["avl_focus"] == (1 if gap else 2)
    assert obj.has_case(METHOD, "v", summary["id"])
    assert not obj.has_case(METHOD, "v", "../../bad")
