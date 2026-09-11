"""Actual HTTP job: exact raw AVL deduplication, never timestamp-only deletion."""

import json
import threading
import time

import polars as pl
import pytest

from experiments.apc_avl_coverage_study.raw_audit import normalize_raw
from experiments.apc_avl_direct_evidence.evaluate import file_hash
from tools.merge_review.data import ReviewData
from tools.merge_review.debug import DebugWorkspace
from tools.merge_review.server import ReviewServer, run_fingerprint
from tools.merge_review.store import ReviewStore
from tools.merge_review.tests.test_debug import apc
from tools.merge_review.tests.test_server import request


@pytest.fixture
def dedup_site(tmp_path):
    audit = tmp_path / "audit"
    audit.mkdir()
    first = {
        "Date_Key": "20231106",
        "Message_DateTime": "2023-11-06 00:00:10.000",
        "Vehicle": "9027",
        "Latitude": "43.65",
        "Longitude": "-79.4",
        "Heading": "0",
        "Route": "29",
        "Run": "1",
        "Destination": "Source destination",
        "OffRoute": "false",
        "Delayed": "false",
        "KPH": "0",
        "tatripId": "source-trip",
    }
    raw = pl.from_dicts(
        [
            first,
            dict(first),
            {**first, "Latitude": "43.6500"},
            {**first, "Message_DateTime": "2023-11-06 00:00:20.000"},
            {**first, "Latitude": "43.651"},
        ]
    )
    csv = audit / "raw-avl.csv"
    raw.write_csv(csv)
    digest = file_hash(csv)
    normalize_raw(raw, "avl", digest).write_parquet(audit / "avl.parquet")
    pl.from_dicts([apc(1, 10), apc(2, 10, direction="SOUTH")]).write_parquet(
        audit / "apc.parquet"
    )
    (audit / "inventory.json").write_text(
        json.dumps({"sources": {"avl": {"path": str(csv), "sha256": digest}}})
    )
    workspace = DebugWorkspace(tmp_path / "debug", audit)
    baseline = workspace.create_run({"method": "vehicle_only"}, label="Experiment 01")
    folder = workspace.get_run(baseline["run_id"])
    store = ReviewStore(tmp_path / "notes", run_fingerprint(folder))
    server = ReviewServer(
        ("127.0.0.1", 0),
        ReviewData(folder),
        store,
        workspace=workspace,
        debug_state_dir=tmp_path / "notes",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield (server, store), baseline, csv, digest
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_http_dedup_removes_only_exact_raw_copies_and_preserves_baseline(
    dedup_site, monkeypatch
):
    site, baseline, csv, digest = dedup_site

    def forbidden(*args, **kwargs):
        raise AssertionError("AVL deduplication must not run trip or matching code")

    for target in (
        "segmentation.segment_vehicle",
        "matcher.build_index",
        "matcher.match_direct",
    ):
        monkeypatch.setattr("tools.merge_review.debug." + target, forbidden)
    status, payload, _ = request(
        site,
        "/api/debug/runs",
        {
            "config": {"method": "vehicle_only_dedup", "segmentation": {}, "merge": {}},
            "label": "Experiment 01 exact AVL deduplication",
            "parent_run_id": baseline["run_id"],
        },
    )
    assert status == 202, payload
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = request(site, "/api/debug/jobs?id=" + payload["job"]["id"])[1]["job"]
        if job["state"] in {"complete", "failed"}:
            break
        time.sleep(0.01)
    assert job["state"] == "complete", job
    run_id = job["run_id"]
    meta = request(site, "/api/meta?run=" + run_id)[1]
    assert meta["methods"] == ["vehicle_only_dedup"]
    assert not meta["trip_segmentation_used"] and not meta["observation_matching_used"]
    query = f"run={run_id}&method=vehicle_only_dedup&vehicle={meta['default_vehicle']}"
    cases = request(site, "/api/catalog?" + query)[1]["cases"]
    assert len(cases) == 1 and cases[0]["kind"] == "vehicle_stream"
    status, detail, _ = request(
        site, "/api/case?" + query + "&case_id=" + cases[0]["id"]
    )
    assert status == 200, detail
    assert len(detail["observations"]) == 6
    assert len(detail["avl"]) == 4 and len(detail["apc"]) == 2
    assert {row["source_row"] for row in detail["avl"]} == {1, 3, 4, 5}
    assert {row["rid"] for row in detail["apc"]} == {"apc:1", "apc:2"}
    assert all(row["status"] == "not_evaluated" for row in detail["apc"])
    assert all(row["progress_m"] is None for row in detail["apc"])
    assert detail["segments"] == []
    assert detail["provenance"]["deduplication_performed"]
    assert [row["display_order"] for row in detail["observations"]] == list(range(6))
    assert file_hash(csv) == digest
    old_query = (
        f"run={baseline['run_id']}&method=vehicle_only"
        f"&vehicle={meta['default_vehicle']}"
    )
    old_case = request(site, "/api/catalog?" + old_query)[1]["cases"][0]
    old_detail = request(site, "/api/case?" + old_query + "&case_id=" + old_case["id"])[
        1
    ]
    assert len(old_detail["avl"]) == 5 and len(old_detail["observations"]) == 7
    assert not old_detail["provenance"]["deduplication_performed"]
    status, saved, _ = request(
        site,
        "/api/reviews?run=" + run_id,
        {
            "run_id": run_id,
            "method": "vehicle_only_dedup",
            "vehicle": meta["default_vehicle"],
            "case_id": cases[0]["id"],
            "verdict": "uncertain",
            "notes": "Only the exact original AVL copy is removed.",
            "revision": 0,
        },
    )
    assert status == 200 and saved["revision"] == 1
    assert not request(site, "/api/reviews/export?run=" + baseline["run_id"])[1][
        "reviews"
    ]
