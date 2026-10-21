"""The persisted run must match each gap child, not its unsplit parent rows."""

import collections
import json

import polars as pl
import pytest

from experiments.apc_avl_coverage_study.raw_audit import normalize_raw
from tools.merge_review import avl_dedup
from tools.merge_review.gtfs_matching import METHOD, canonical, feed, runner
from tools.merge_review.tests.test_direction_runner import (
    direction_workspace,  # noqa: F401
)


def test_gap_children_are_matched_independently_and_parent_rows_are_unchanged(
    request, monkeypatch
):
    workspace = request.getfixturevalue("direction_workspace")
    source = workspace.audit / "avl.csv"
    raw = pl.read_csv(source, infer_schema=False)
    later = dict(
        raw.row(0, named=True),
        Message_DateTime="2023-11-06 01:01:00.000",
        Destination="SOUTH - 29 DUFFERIN",
    )
    raw = raw.vstack(pl.from_dicts([later]))
    raw.write_csv(source)
    digest = avl_dedup.file_sha256(source)
    normalize_raw(raw, "avl", digest).write_parquet(workspace.audit / "avl.parquet")
    (workspace.audit / "inventory.json").write_text(
        json.dumps({"sources": {"avl": {"path": str(source), "sha256": digest}}})
    )
    parent = workspace.create_run({"method": "direction_destination"})
    folder = workspace.get_run(parent["run_id"])
    originals = {
        p.name: p.read_bytes() for p in (folder / "observations").glob("*.parquet")
    }
    points = canonical.geometry(
        [{"lat": 43.65, "lon": -79.4}, {"lat": 43.66, "lon": -79.4}],
        (43.65, -79.4),
    )
    ref = dict(
        reference_id="r",
        family_id="f",
        feed_version="test",
        route_id="29",
        direction_label="NORTH",
        points=points,
        origin=(43.65, -79.4),
        length_m=points[-1]["s_m"],
        stop_positions=[],
    )
    monkeypatch.setattr(
        feed,
        "load_feed",
        lambda *a, **k: dict(
            feed_version="test",
            route_id="29",
            selection={},
            diagnostics={},
            archive_sha256=digest,
            feed_path=str(source),
            manifest_path=str(source),
            manifest_sha256=digest,
        ),
    )
    monkeypatch.setattr(
        canonical,
        "build_references",
        lambda *a, **k: dict(
            references=[ref],
            trips=[],
            validations=[],
            route_id="29",
            diagnostics={},
            direction_crosswalk=[],
        ),
    )
    seen = []
    evaluate = runner.evaluate_segment

    def observe(rows, *args, **kwargs):
        seen.append({(r["source"], r["rid"]) for r in rows})
        return evaluate(rows, *args, **kwargs)

    monkeypatch.setattr(runner, "evaluate_segment", observe)
    record = workspace.create_run(
        {"method": METHOD, "gtfs": {"execution_workers": 1}},
        parent_run_id=parent["run_id"],
    )
    result = workspace.get_run(record["run_id"])
    catalog = [
        row
        for path in (result / "catalog").glob("*.json")
        for row in json.loads(path.read_text())
    ]
    children = [row for row in catalog if row.get("gap_split")]
    assert len(children) == 2
    assert record["summary"]["gap_split_parent_cases"] == 1
    assert record["summary"]["joint_gap_count"] == 1
    assert record["summary"]["cases"] == record["summary"]["parent_cases"] + 1
    for child in children:
        own = {(r["source"], r["rid"]) for r in child["source_rids"]}
        assert own in seen
        artifact = runner.read_case(
            result / "cases" / f"{runner.artifact_key(child['id'])}.json.gz"
        )
        actual = {("avl", p["rid"]) for p in artifact["projection"]["points"]}
        actual |= {("apc", p["rid"]) for p in artifact["apc_evidence"]}
        assert actual == own
    assert {
        (r["source"], r["rid"]) for c in children for r in c["source_rids"]
    } not in seen
    assert len(seen) == len(catalog)
    assert {
        p.name: p.read_bytes() for p in (folder / "observations").glob("*.parquet")
    } == originals
    for path in (result / "observations").glob("*.parquet"):
        assert pl.read_parquet(path).equals(
            pl.read_parquet(folder / "observations" / path.name)
        )
    membership = collections.Counter(
        r["rid"] for c in children for r in c["source_rids"]
    )
    assert max(membership.values()) == 1

    # A corrupted refinement must abort before publishing a completed result.
    from tools.merge_review.gtfs_matching import gaps

    monkeypatch.setattr(
        gaps,
        "validate_gap_split",
        lambda *a, **k: {
            "valid": False,
            "violations": {"injected": 1},
        },
    )
    with pytest.raises(ValueError, match="gap invariant failure"):
        workspace.create_run(
            {"method": METHOD, "gtfs": {"execution_workers": 1}},
            parent_run_id=parent["run_id"],
        )
    failures = list((workspace.root / "runs").glob("*/failure.json"))
    assert len(failures) == 1
    assert not (failures[0].parent / "completion.json").exists()
