"""End-to-end local HTTP integration for unpaired, unsplit vehicle streams."""

import threading
import time

import polars as pl
import pytest

from tools.merge_review.data import ReviewData
from tools.merge_review.debug import DebugWorkspace
from tools.merge_review.server import ReviewServer, run_fingerprint
from tools.merge_review.store import ReviewStore
from tools.merge_review.tests.test_debug import apc, ping
from tools.merge_review.tests.test_server import request


@pytest.fixture
def stream_site(tmp_path):
    audit = tmp_path / "audit"
    audit.mkdir()
    pl.from_dicts(
        [
            ping("avl:later", 40, source_row=2),
            ping("avl:first", 10, source_row=1),
            ping("avl:bad", 20, source_row=3, invalid=True),
        ]
    ).write_parquet(audit / "avl.parquet")
    pl.from_dicts([apc(1, 30), apc(2, 10)]).write_parquet(audit / "apc.parquet")
    workspace = DebugWorkspace(tmp_path / "debug", audit)
    baseline = workspace.create_run({"method": "avl_context"}, label="paired baseline")
    run = workspace.get_run(baseline["run_id"])
    store = ReviewStore(tmp_path / "notes", run_fingerprint(run))
    server = ReviewServer(
        ("127.0.0.1", 0),
        ReviewData(run),
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


def test_vehicle_only_job_roundtrip_preserves_rows_without_claiming_matches(
    stream_site,
    monkeypatch,
):
    site, baseline = stream_site

    def forbidden(*args, **kwargs):
        raise AssertionError("vehicle_only must not segment or match observations")

    monkeypatch.setattr(
        "tools.merge_review.debug.segmentation.segment_vehicle", forbidden
    )
    monkeypatch.setattr("tools.merge_review.debug.matcher.build_index", forbidden)
    monkeypatch.setattr("tools.merge_review.debug.matcher.match_direct", forbidden)
    status, payload, _ = request(
        site,
        "/api/debug/runs",
        {
            "config": {"method": "vehicle_only", "segmentation": {}, "merge": {}},
            "label": "Vehicle-only experiment",
            "parent_run_id": baseline["run_id"],
        },
    )
    assert status == 202
    job_id = payload["job"]["id"]
    for _ in range(200):
        job = request(site, "/api/debug/jobs?id=" + job_id)[1]["job"]
        if job["state"] in {"complete", "failed"}:
            break
        time.sleep(0.01)
    assert job["state"] == "complete", job
    run_id = job["run_id"]
    status, meta, _ = request(site, "/api/meta?run=" + run_id)
    assert status == 200 and meta["methods"] == ["vehicle_only"]
    assert meta["debug_date"] == "2023-11-06"
    vehicle = meta["default_vehicle"]
    query = f"run={run_id}&method=vehicle_only&vehicle={vehicle}"
    status, catalog, _ = request(site, "/api/catalog?" + query)
    assert status == 200 and len(catalog["cases"]) == 1
    case = catalog["cases"][0]
    assert case["kind"] == "vehicle_stream"
    status, detail, _ = request(site, "/api/case?" + query + "&case_id=" + case["id"])
    assert status == 200
    observations = detail["observations"]
    assert len(observations) == 5
    assert {r["rid"] for r in observations} == {
        "avl:later",
        "avl:first",
        "avl:bad",
        "apc:1",
        "apc:2",
    }
    assert [r["event_time"] for r in observations] == sorted(
        r["event_time"] for r in observations
    )
    assert len(detail["avl"]) == 3 and len(detail["apc"]) == 2
    assert detail["segments"] == []
    assert all(r["status"] == "not_evaluated" for r in detail["apc"])
    assert all(not r["decision"].get("witnesses") for r in detail["apc"])
    assert all(not r["decision"].get("segment_ids") for r in detail["apc"])
    status, comparison, _ = request(
        site,
        f"/api/debug/compare?base={baseline['run_id']}&run={run_id}&vehicle={vehicle}",
    )
    assert status == 200 and comparison["comparable"] is False
    assert comparison["comparison_status"] == "not_comparable"
    assert comparison["reason"]
    status, saved, _ = request(
        site,
        "/api/reviews?run=" + run_id,
        {
            "run_id": run_id,
            "method": "vehicle_only",
            "vehicle": vehicle,
            "case_id": case["id"],
            "verdict": "uncertain",
            "notes": "未执行匹配",
            "revision": 0,
        },
    )
    assert status == 200 and saved["revision"] == 1
    assert (
        request(site, "/api/reviews/export?run=" + baseline["run_id"])[1]["reviews"]
        == []
    )
    assert (
        request(site, "/api/reviews/export?run=" + run_id)[1]["ground_truth"] is False
    )


def test_vehicle_only_rejects_hidden_matching_parameters_before_execution(stream_site):
    site, _ = stream_site
    for config in (
        {"method": "vehicle_only", "merge": {"radius_m": 100}},
        {"method": "vehicle_only", "segmentation": {"gap_seconds": 1800}},
    ):
        assert request(site, "/api/debug/runs", {"config": config})[0] == 400
    assert not site[0].jobs.jobs
