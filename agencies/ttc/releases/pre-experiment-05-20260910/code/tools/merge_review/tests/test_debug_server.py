"""Run-scoped HTTP and serial background execution, independent of real data."""

import json
import threading
import time

import pytest

from tools.merge_review.server import DebugJobs, ReviewServer, run_fingerprint
from tools.merge_review.store import ReviewStore
from tools.merge_review.tests.test_server import FakeData, request, review


class RunData(FakeData):
    def __init__(self, run):
        self.run = run

    def metadata(self):
        return {**super().metadata(), "run_id": self.run.name}


class Workspace:
    def __init__(self, root):
        self.root = root
        self.release = threading.Event()
        self.started = threading.Event()
        self.fail = False
        for run_id in ("run-a", "run-b"):
            path = root / run_id
            path.mkdir(parents=True)
            for name in ("manifest.json", "config.json", "completion.json"):
                (path / name).write_text(json.dumps({"id": run_id}))

    @staticmethod
    def validate_config(config):
        if not isinstance(config, dict) or set(config) != {"method"}:
            raise ValueError("bad config")
        if config["method"] != "avl_context":
            raise ValueError("bad method")
        return dict(config)

    def metadata(self):
        return {
            "date": "2023-11-06",
            "defaults": {"method": "avl_context"},
            "runs": [{"run_id": r, "created_at": r} for r in ("run-a", "run-b")],
        }

    def get_run(self, run_id):
        if run_id not in {"run-a", "run-b"}:
            raise ValueError("unknown run")
        return self.root / run_id

    def create_run(self, config, label="", parent_run_id=None, progress=None):
        self.started.set()
        progress({"stage": "matching", "completed": 1, "total": 2})
        self.release.wait(timeout=10)
        if self.fail:
            raise ValueError("deliberate validation failure")
        return {"run_id": "run-b", "label": label}

    def compare(self, base_id, run_id, vehicle=None):
        self.get_run(base_id)
        self.get_run(run_id)
        return {"base_run_id": base_id, "run_id": run_id, "vehicle": vehicle}


@pytest.fixture
def debug_site(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "runs")
    monkeypatch.setattr("tools.merge_review.data.ReviewData", RunData)
    store = ReviewStore(
        tmp_path / "reviews", run_fingerprint(workspace.get_run("run-a"))
    )
    server = ReviewServer(
        ("127.0.0.1", 0),
        RunData(workspace.get_run("run-a")),
        store,
        workspace=workspace,
        debug_state_dir=tmp_path / "reviews",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, store
    workspace.release.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_debug_metadata_and_explicit_run_are_request_scoped(debug_site):
    status, body, _ = request(debug_site, "/api/debug")
    assert status == 200 and body["enabled"]
    assert body["date"] == "2023-11-06"
    assert [r["id"] for r in body["runs"]] == ["run-a", "run-b"]
    assert request(debug_site, "/api/meta")[1]["run_id"] == "run-b"
    assert request(debug_site, "/api/meta?run=run-a")[1]["run_id"] == "run-a"
    assert request(debug_site, "/api/meta?run=run-b")[1]["run_id"] == "run-b"
    assert request(debug_site, "/api/meta?run=run-a")[1]["run_id"] == "run-a"


def test_notes_require_run_and_do_not_follow_latest_or_other_tabs(debug_site):
    assert request(debug_site, "/api/reviews", review())[0] == 400
    assert (
        request(debug_site, "/api/reviews", review(run_id="run-a", notes="A only"))[0]
        == 200
    )
    assert (
        request(debug_site, "/api/reviews", review(run_id="run-b", notes="B only"))[0]
        == 200
    )
    for run_id, notes in (("run-a", "A only"), ("run-b", "B only")):
        exported = request(debug_site, "/api/reviews/export?run=" + run_id)[1]
        assert len(exported["events"]) == 1
        assert exported["reviews"][0]["notes"] == notes
        assert exported["ground_truth"] is False


def test_review_post_can_pin_run_in_both_url_and_body(debug_site):
    assert (
        request(debug_site, "/api/reviews?run=run-a", review(run_id="run-a"))[0] == 200
    )
    assert (
        request(
            debug_site, "/api/reviews?run=run-b", review(run_id="run-a", revision=1)
        )[0]
        == 400
    )
    assert (
        request(
            debug_site,
            "/api/reviews?run=run-a&run=run-b",
            review(run_id="run-a", revision=1),
        )[0]
        == 400
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/meta?run=../../other",
        "/api/meta?run=run-a&run=run-b",
        "/api/debug/compare?base=../other&run=run-a",
        "/api/debug/jobs?id=missing",
    ],
)
def test_unknown_or_ambiguous_debug_identifiers_rejected(debug_site, path):
    assert request(debug_site, path)[0] == 400


def test_compare_uses_explicit_two_runs(debug_site):
    status, body, _ = request(
        debug_site, "/api/debug/compare?base=run-a&run=run-b&vehicle=vehicle"
    )
    assert status == 200 and body == {
        "base_run_id": "run-a",
        "run_id": "run-b",
        "vehicle": "vehicle",
    }


def test_async_jobs_do_not_block_reads_and_reject_overlapping_execution(debug_site):
    body = {
        "config": {"method": "avl_context"},
        "parent_run_id": "run-a",
        "label": "trial",
    }
    status, submitted, _ = request(debug_site, "/api/debug/runs", body)
    assert status == 202
    server = debug_site[0]
    assert server.workspace.started.wait(timeout=2)
    job_id = submitted["job"]["id"]
    job = request(debug_site, "/api/debug/jobs?id=" + job_id)[1]["job"]
    assert job["state"] == "running" and job["progress"] == 50
    assert request(debug_site, "/api/debug/runs", body)[0] == 409
    assert request(debug_site, "/api/meta?run=run-a")[0] == 200
    assert request(debug_site, "/api/reviews", review(run_id="run-a"))[0] == 200
    server.workspace.release.set()
    for _ in range(100):
        job = server.jobs.get(job_id)
        if job["state"] == "complete":
            break
        time.sleep(0.01)
    assert job["state"] == "complete" and job["run_id"] == "run-b"
    assert job["progress"] == 100


@pytest.mark.parametrize(
    "body",
    [
        {"config": {"method": "bad"}},
        {"config": {"date": "2023-11-07"}},
        {"config": {"method": "avl_context"}, "shell": "bad"},
        {"config": {"method": "avl_context"}, "label": "x" * 161},
        {"config": {"method": "avl_context"}, "parent_run_id": "../other"},
    ],
)
def test_bad_experiment_rejected_before_start(debug_site, body):
    assert request(debug_site, "/api/debug/runs", body)[0] == 400
    assert not debug_site[0].workspace.started.is_set()


def test_experiment_posts_require_same_security_as_reviews(debug_site):
    body = {"config": {"method": "avl_context"}}
    for headers in (
        {"X-Review-Token": ""},
        {"Origin": "https://evil.example"},
        {"Host": "evil.example"},
    ):
        assert request(debug_site, "/api/debug/runs", body, headers)[0] == 403
    assert not debug_site[0].workspace.started.is_set()


def test_failed_job_is_not_advertised_as_success(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.fail = True
    workspace.release.set()
    jobs = DebugJobs(workspace)
    submitted = jobs.submit({"config": {"method": "avl_context"}})
    for _ in range(100):
        job = jobs.get(submitted["id"])
        if job["state"] == "failed":
            break
        time.sleep(0.01)
    assert job["state"] == "failed" and job["run_id"] is None
    assert "validation failure" in job["error"]
