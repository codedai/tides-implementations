"""Actual HTTP experiment job and read-back for direction candidates."""

import threading
import time
from urllib.parse import urlencode

import pytest

from tools.merge_review.data import ReviewData
from tools.merge_review.server import ReviewServer, run_fingerprint
from tools.merge_review.store import ReviewStore
from tools.merge_review.tests import test_direction_runner
from tools.merge_review.tests.test_server import request

direction_workspace = test_direction_runner.direction_workspace


@pytest.fixture
def direction_site(direction_workspace, tmp_path):
    workspace = direction_workspace
    baseline = workspace.create_run({"method": "vehicle_only_dedup"}, label="01b")
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
    yield (server, store), baseline
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_http_direction_job_and_source_review(direction_site):
    site, baseline = direction_site
    status, payload, _ = request(
        site,
        "/api/debug/runs",
        {
            "config": {
                "method": "direction_destination",
                "segmentation": {},
                "merge": {},
            },
            "label": "Disposable direction experiment",
            "parent_run_id": baseline["run_id"],
        },
    )
    assert status == 202, payload
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = request(site, "/api/debug/jobs?id=" + payload["job"]["id"])[1]["job"]
        if job["state"] in {"complete", "failed"}:
            break
        time.sleep(0.01)
    assert job["state"] == "complete", job
    run_id = job["run_id"]
    status, meta, _ = request(site, "/api/meta?run=" + run_id)
    assert status == 200, meta
    assert meta["methods"] == ["direction_destination"]
    assert meta["representation"] == "direction_candidate_segments"
    assert meta["trip_segmentation_used"] and not meta["observation_matching_used"]
    seen_apc, seen_avl, conflicts = set(), set(), set()
    for vehicle in meta["vehicles"]:
        query = {
            "run": run_id,
            "method": "direction_destination",
            "vehicle": vehicle["id"],
        }
        status, catalog, _ = request(site, "/api/catalog?" + urlencode(query))
        assert status == 200, catalog
        for case in catalog["cases"]:
            status, detail, _ = request(
                site, "/api/case?" + urlencode({**query, "case_id": case["id"]})
            )
            assert status == 200, detail
            assert not detail["observation_matching_used"]
            for row in detail["observations"]:
                (seen_apc if row["source"] == "apc" else seen_avl).add(row["rid"])
                conflicts.update(row.get("direction_conflict_ids") or [])
                if row["source"] == "avl":
                    assert row["route"] not in {"PI", "PO"}
            assert all(
                row["status"] == "not_evaluated" and row["progress_m"] is None
                for row in detail["apc"]
            )
    assert seen_apc == {"apc:1", "apc:2", "apc:3"}
    assert len(seen_avl) == 4
    assert conflicts
    status, diff, _ = request(
        site,
        "/api/debug/compare?" + urlencode({"base": baseline["run_id"], "run": run_id}),
    )
    assert status == 200, diff
    assert diff["summary"]["avl_removed_by_pi_po_filter"] == 3
    assert diff["summary"]["apc_unchanged"]
    assert request(site, "/api/reviews/export?run=" + run_id)[1]["reviews"] == []
    assert (
        request(site, "/api/reviews/export?run=" + baseline["run_id"])[1]["reviews"]
        == []
    )
