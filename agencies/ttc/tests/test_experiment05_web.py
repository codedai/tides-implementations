"""Real localhost HTTP, but only synthetic pipeline outputs and no matching."""

import hashlib
import http.client
import io
import json
import threading
import time
import zipfile
from urllib.parse import urlencode

import pytest

from tools.experiment05.web import MAX_BODY_BYTES, STATIC, create_server


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False))


def publish(root, run_id="run-synthetic", modes=("global",)):
    folder = root / "outputs/experiment_05/runs" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    summary = {
        "service_date": "2023-11-06",
        "route_id": "29",
        "match_modes": list(modes),
        "performed_trip_count": 3,
        "source_counts": {"avl": 7, "apc": 2},
        "stage_row_counts": {"assignments": 3 * len(modes)},
        "modes": {
            mode: {
                "matched_count": 2,
                "unmatched_count": 1,
                "unmatched_reasons": {"TOP3_EXHAUSTED": 1},
                "tides_validation_passed": True,
                "table_row_counts": {"trips_performed": 3},
            }
            for mode in modes
        },
        "validation_ok": True,
    }
    write_json(folder / "summary.json", summary)
    (folder / "assignments.parquet").write_bytes(b"synthetic-parquet-output")
    for mode in modes:
        base = folder / "tides" / mode
        write_json(base / "datapackage.json", {"resources": []})
        write_json(base / "validation_report.json", {"valid": True})
        (base / "trips_performed.csv").write_text("trip_id_performed\np1\n")
    pins = {
        str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in folder.rglob("*")
        if path.is_file() and path.name != "run_manifest.json"
    }
    manifest = {
        "schema_version": "experiment05-run-v1",
        "run_id": run_id,
        "status": "complete",
        "service_date": "2023-11-06",
        "route_id": "29",
        "match_modes": list(modes),
        "output_sha256": pins,
    }
    write_json(folder / "run_manifest.json", manifest)
    return folder, manifest, summary


class StubPipeline:
    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.failure = None

    def availability(self, *, root, config_path):
        return {
            "route_ids": ["29"],
            "match_modes": ["global", "early", "middle", "late"],
            "dates": [
                {
                    "service_date": "2023-11-06",
                    "avl_rows": 7,
                    "apc_rows": 2,
                    "raw_available": True,
                    "gtfs_available": True,
                    "available": True,
                    "reason": None,
                },
                {
                    "service_date": "2023-11-07",
                    "avl_rows": 0,
                    "apc_rows": 0,
                    "raw_available": False,
                    "gtfs_available": True,
                    "available": False,
                    "reason": "NO_RAW_DATA",
                },
                {
                    "service_date": "2023-11-08",
                    "avl_rows": 7,
                    "apc_rows": 2,
                    "raw_available": True,
                    "gtfs_available": False,
                    "available": False,
                    "reason": "NO_ACTIVE_GTFS_FEED",
                },
            ],
        }

    def run_pipeline(self, **kwargs):
        self.calls.append(
            {key: value for key, value in kwargs.items() if key != "progress"}
        )
        kwargs["progress"](
            {"stage": "assignment", "state": "running", "row_counts": {"candidates": 6}}
        )
        if not self.release.wait(5):
            raise ValueError("Synthetic test gate timed out")
        if self.failure:
            raise self.failure
        run_id = "run-synthetic-" + "-".join(kwargs["match_modes"])
        if kwargs["force"]:
            run_id += "-forced"
        existed = (kwargs["root"] / "outputs/experiment_05/runs" / run_id).exists()
        folder, manifest, summary = publish(
            kwargs["root"], run_id, kwargs["match_modes"]
        )
        kwargs["progress"](
            {"stage": "export", "state": "complete", "row_counts": {"trips": 3}}
        )
        return {
            "run_dir": str(folder),
            "run_manifest": manifest,
            "summary": summary,
            "cached": existed and not kwargs["force"],
        }


@pytest.fixture
def web(tmp_path):
    pipeline = StubPipeline()
    server = create_server(
        root=tmp_path,
        config_path=tmp_path / "test-config.toml",
        pipeline_module=pipeline,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, pipeline, tmp_path
    finally:
        pipeline.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(web, method="GET", path="/api/health", body=None, headers=None):
    server, _, _ = web
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    provided = dict(headers or {})
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
        provided.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=body, headers=provided)
    response = connection.getresponse()
    payload = response.read()
    metadata = dict(response.getheaders())
    connection.close()
    return response.status, payload, metadata


def post(web, **overrides):
    return request(
        web,
        "POST",
        "/api/runs",
        {
            "service_date": "2023-11-06",
            "route_id": "29",
            "match_mode": "global",
            **overrides,
        },
    )


def finish_job(web, payload):
    identity = json.loads(payload)["job_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, data, _ = request(web, path=f"/api/jobs/{identity}")
        assert status == 200
        job = json.loads(data)
        if job["state"] in {"complete", "failed"}:
            return job
        time.sleep(0.01)
    pytest.fail("Synthetic background job did not complete")


def test_local_assets_health_availability_have_no_external_dependencies(web):
    assert json.loads(request(web)[1]) == {
        "ok": True,
        "local_only": True,
        "experiment": "05",
    }
    for path in ("/", "/app.js", "/style.css"):
        status, body, headers = request(web, path=path)
        assert status == 200 and body
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert b"https://" not in body and b"http://" not in body
    value = json.loads(request(web, path="/api/availability")[1])
    assert value["dates"][0]["avl_rows"] == 7
    assert value["dates"][1]["reason"] == "NO_RAW_DATA"


def test_post_runs_same_core_background_job_progress_and_cached_result(web):
    status, payload, _ = post(web)
    assert status == 202
    job = finish_job(web, payload)
    assert job["state"] == "complete" and job["result"]["cached"] is False
    assert [event["stage"] for event in job["progress"]] == ["assignment", "export"]
    call = web[1].calls[0]
    assert call["match_modes"] == ["global"] and call["force"] is False
    assert call["root"] == web[2] and "output_dir" not in call
    second = finish_job(web, post(web)[1])
    assert second["result"]["cached"] is True
    assert len(web[1].calls) == 2  # Cache decisions belong to the core, not the web.
    status, payload, _ = request(web, path="/api/runs")
    assert status == 200 and len(json.loads(payload)["runs"]) == 1


def test_all_mode_force_selection_reaches_core_unchanged(web):
    job = finish_job(web, post(web, match_mode="all", force=True)[1])
    assert job["state"] == "complete" and job["result"]["cached"] is False
    assert web[1].calls[0]["match_modes"] == ["global", "early", "middle", "late"]
    assert web[1].calls[0]["force"] is True


def test_only_one_background_job_is_allowed(web):
    web[1].release.clear()
    first = post(web)
    status, payload, _ = post(web, match_mode="early")
    assert status == 409 and json.loads(payload)["error"] == "JOB_ALREADY_RUNNING"
    web[1].release.set()
    assert finish_job(web, first[1])["state"] == "complete"
    assert len(web[1].calls) == 1


@pytest.mark.parametrize(
    "day,reason",
    [
        ("2023-11-07", "NO_RAW_DATA"),
        ("2023-11-08", "NO_ACTIVE_GTFS_FEED"),
        ("2023-11-09", "unavailable"),
    ],
)
def test_missing_inputs_are_clear_and_never_start_processing(web, day, reason):
    status, payload, _ = post(web, service_date=day)
    assert status == 422 and reason in json.loads(payload)["message"]
    assert web[1].calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"service_date": "2023-02-30"},
        {"service_date": "20231106"},
        {"route_id": "929"},
        {"force": "true"},
        {"match_mode": "unknown"},
        {"output_dir": "/tmp/unsafe"},
        {"root": "/"},
        {"config_path": "/tmp/unsafe"},
        {"date": "2023-11-06"},
    ],
)
def test_bad_request_parameters_and_filesystem_injection_are_rejected(web, overrides):
    assert post(web, **overrides)[0] == 400
    assert not web[1].calls


def test_core_failure_is_auditable_and_releases_single_job_slot(web):
    web[1].failure = ValueError("Synthetic source changed")
    job = finish_job(web, post(web)[1])
    assert job["state"] == "failed" and "source changed" in job["error"]
    web[1].failure = None
    assert finish_job(web, post(web)[1])["state"] == "complete"


def test_summary_and_each_download_are_backed_by_published_pins(web):
    folder, manifest, summary = publish(web[2])
    status, payload, _ = request(web, path="/api/runs/run-synthetic/summary")
    detail = json.loads(payload)
    assert status == 200 and detail["summary"] == summary
    assert {row["file"] for row in detail["downloads"]} == set(
        manifest["output_sha256"]
    )
    for name in manifest["output_sha256"]:
        status, body, headers = request(
            web, path="/api/runs/run-synthetic/download?" + urlencode({"file": name})
        )
        assert status == 200 and body == (folder / name).read_bytes()
        assert "attachment;" in headers["Content-Disposition"]
    status, payload, _ = request(
        web, path="/api/runs/run-synthetic/download?mode=global"
    )
    assert status == 200
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert set(archive.namelist()) == {
            "datapackage.json",
            "validation_report.json",
            "trips_performed.csv",
        }
        assert (
            archive.read("trips_performed.csv")
            == (folder / "tides/global/trips_performed.csv").read_bytes()
        )
    assert request(web, path="/api/runs/run-synthetic/download?mode=late")[0] == 404


def test_download_tamper_is_not_masked_by_previous_success(web):
    folder, _, _ = publish(web[2])
    path = "/api/runs/run-synthetic/download?file=assignments.parquet"
    assert request(web, path=path)[0] == 200
    (folder / "assignments.parquet").write_bytes(b"modified")
    status, payload, _ = request(web, path=path)
    assert status == 409 and json.loads(payload)["error"] == "OUTPUT_HASH_CHANGED"
    (folder / "summary.json").write_text("{}")
    assert request(web, path="/api/runs/run-synthetic/summary")[0] == 409


@pytest.mark.parametrize(
    "name",
    [
        "../secret.json",
        "/tmp/secret.json",
        "tides/../../secret.json",
        "a\\b.json",
        "./summary.json",
        "private.json",
        ".writing",
    ],
)
def test_unpublished_and_traversal_downloads_are_rejected(web, name):
    folder, _, _ = publish(web[2])
    (folder / "private.json").write_text("synthetic-unpublished-secret")
    status, payload, _ = request(
        web, path="/api/runs/run-synthetic/download?" + urlencode({"file": name})
    )
    assert status in {400, 404}
    assert b"synthetic-unpublished-secret" not in payload


def test_symlink_file_and_ancestor_cannot_escape_published_run(web):
    folder, manifest, _ = publish(web[2])
    outside = web[2] / "outside.json"
    outside.write_text("synthetic-outside-secret")
    (folder / "evil.json").symlink_to(outside)
    (folder / "alias").symlink_to(web[2], target_is_directory=True)
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    manifest["output_sha256"].update(
        {"evil.json": digest, "alias/outside.json": digest}
    )
    write_json(folder / "run_manifest.json", manifest)
    for name in ("evil.json", "alias/outside.json"):
        status, payload, _ = request(
            web, path="/api/runs/run-synthetic/download?" + urlencode({"file": name})
        )
        assert status == 404 and b"synthetic-outside-secret" not in payload


def test_noncompleted_runs_are_listed_but_not_downloadable(web):
    folder, manifest, _ = publish(web[2])
    manifest["status"] = "failed"
    write_json(folder / "run_manifest.json", manifest)
    assert (
        json.loads(request(web, path="/api/runs")[1])["runs"][0]["status"] == "failed"
    )
    assert (
        request(web, path="/api/runs/run-synthetic/download?file=assignments.parquet")[
            0
        ]
        == 409
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://attacker.invalid"},
        {"Origin": "null"},
        {"Origin": "http://127.0.0.1:1"},
        {"Host": "attacker.invalid"},
        {"Host": "attacker.invalid:8767"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_origin_and_dns_rebinding_requests_cannot_launch_runs(web, headers):
    status, _, _ = request(
        web, "POST", "/api/runs", {"service_date": "2023-11-06"}, headers=headers
    )
    assert status == 403 and not web[1].calls


def test_same_origin_post_and_strict_body_limits(web):
    origin = f"http://127.0.0.1:{web[0].server_port}"
    status, payload, _ = request(
        web,
        "POST",
        "/api/runs",
        {"service_date": "2023-11-06", "match_mode": "early"},
        {"Origin": origin},
    )
    assert status == 202 and finish_job(web, payload)["state"] == "complete"
    assert (
        request(
            web,
            "POST",
            "/api/runs",
            "x" * (MAX_BODY_BYTES + 1),
            {"Content-Type": "application/json"},
        )[0]
        == 413
    )
    assert (
        request(web, "POST", "/api/runs", "{}", {"Content-Type": "text/plain"})[0]
        == 415
    )
    assert (
        request(web, "POST", "/api/runs", "{bad", {"Content-Type": "application/json"})[
            0
        ]
        == 400
    )
    assert (
        request(
            web,
            "POST",
            "/api/runs",
            '{"force":NaN}',
            {"Content-Type": "application/json"},
        )[0]
        == 400
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/../summary",
        "/api/runs/%2e%2e/summary",
        "/api/runs/run-synthetic/../../config.json",
        "/api/runs/run-synthetic/download?file=summary.json&file=private.json",
        "/api/jobs/not-a-job",
    ],
)
def test_bad_paths_are_not_filesystem_routes(web, path):
    assert request(web, path=path)[0] in {400, 404}


def test_unsupported_methods_and_nonlocal_binding_are_rejected(web):
    assert request(web, "DELETE", "/api/runs")[0] == 405
    assert request(web, "OPTIONS", "/api/runs")[0] == 405
    with pytest.raises(ValueError, match="localhost"):
        create_server(host="0.0.0.0", root=web[2], pipeline_module=web[1])


def test_assets_cover_modes_force_progress_downloads_and_bilingual_ui():
    html = (STATIC / "index.html").read_text()
    script = (STATIC / "app.js").read_text()
    for mode in ("global", "early", "middle", "late", "all"):
        assert f'value="{mode}"' in html
    for identity in (
        "force",
        "stage-progress",
        "validator-status",
        "download-filter",
        "language-switch",
    ):
        assert f'id="{identity}"' in html
    assert "run_pipeline" not in script and "innerHTML" not in script
    assert "matched_count" in script and "unmatched_reasons" in script
    assert "localStorage" in script and "tides_validation_passed" in script
