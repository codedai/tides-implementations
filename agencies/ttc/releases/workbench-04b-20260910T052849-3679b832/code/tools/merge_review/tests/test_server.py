"""Local server safety, independent note persistence, and concurrency contract."""

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from tools.merge_review.server import ReviewServer, json_safe
from tools.merge_review.store import ReviewStore, RevisionConflict


class FakeData:
    def metadata(self):
        return {"run_id": "fixture", "vehicles": [{"id": "vehicle", "label": "1"}]}

    def catalog(self, method, vehicle):
        if (method, vehicle) != ("avl_context", "vehicle"):
            raise ValueError("unknown context")
        return [{"id": "case", "kind": "segment"}]

    def has_case(self, method, vehicle, case_id):
        return any(c["id"] == case_id for c in self.catalog(method, vehicle))

    def case(self, method, vehicle, case_id, context_s=180):
        if not self.has_case(method, vehicle, case_id):
            raise ValueError("unknown case")
        return {"case": {"id": case_id}, "apc": [], "avl": [], "context_s": context_s}


@pytest.fixture
def site(tmp_path):
    store = ReviewStore(tmp_path / "notes", "run-A")
    server = ReviewServer(("127.0.0.1", 0), FakeData(), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, store
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def request(site, path, body=None, headers=None):
    server, _ = site
    url = f"http://127.0.0.1:{server.server_port}{path}"
    merged = {"Content-Type": "application/json"}
    if body is not None:
        merged["X-Review-Token"] = server.csrf_token
    merged.update(headers or {})
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=merged,
    )
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        value = json.loads(raw) if "json" in response.headers["Content-Type"] else raw
        return response.status, value, response.headers


def review(**kwargs):
    return {
        "method": "avl_context",
        "vehicle": "vehicle",
        "case_id": "case",
        "verdict": "uncertain",
        "notes": "源记录粒度待确认",
        "revision": 0,
        **kwargs,
    }


def test_meta_health_and_security_headers(site):
    status, meta, headers = request(site, "/api/meta")
    assert status == 200 and meta["csrf_token"] == site[0].csrf_token
    assert meta["run_key"] == "run-A"
    assert headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert request(site, "/api/health")[1]["local_only"]


def test_notes_are_independent_persisted_and_exported_with_audit(site):
    status, saved, _ = request(site, "/api/reviews", review())
    assert status == 200 and saved["revision"] == 1
    catalog = request(site, "/api/catalog?method=avl_context&vehicle=vehicle")[1]
    assert catalog["stats"] == {"total": 1, "reviewed": 1}
    detail = request(site, "/api/case?method=avl_context&vehicle=vehicle&case_id=case")[
        1
    ]
    assert detail["review"]["notes"] == "源记录粒度待确认"
    exported = request(site, "/api/reviews/export")
    assert exported[0] == 200
    assert exported[1]["ground_truth"] is False
    assert exported[1]["unblinded"] is True
    assert len(exported[1]["events"]) == 1
    assert exported[2]["Content-Disposition"].startswith("attachment")
    assert (
        ReviewStore(site[1].path.parent, "run-A").get("avl_context", "vehicle", "case")[
            "revision"
        ]
        == 1
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example"},
        {"Origin": "https://evil.example"},
        {"X-Review-Token": ""},
        {"X-Review-Token": "wrong"},
    ],
)
def test_cross_origin_dns_rebinding_and_forged_saves_rejected(site, headers):
    assert request(site, "/api/reviews", review(), headers)[0] == 403
    assert not site[1].export()["events"]


def test_untrusted_get_host_cannot_read_private_evidence(site):
    assert request(site, "/api/meta", headers={"Host": "evil.example"})[0] == 403


def test_unknown_paths_cannot_read_source_files(site):
    for path in ("/../server.py", "/%2e%2e/server.py", "/artifacts/raw.parquet"):
        assert request(site, path)[0] == 404


@pytest.mark.parametrize(
    "name,mime",
    [
        ("i18n.js", "text/javascript"),
        ("i18n-static.js", "text/javascript"),
        ("i18n-dynamic.js", "text/javascript"),
        ("i18n.css", "text/css"),
    ],
)
def test_language_assets_are_local_read_only_allowlisted_files(site, name, mime):
    status, body, headers = request(site, "/" + name)
    assert status == 200 and body
    assert headers["Content-Type"].startswith(mime)
    assert not site[1].export()["events"]
    assert request(site, "/../" + name)[0] == 404


@pytest.mark.parametrize(
    "change",
    [
        {"case_id": "../other"},
        {"method": "other"},
        {"verdict": "direct"},
        {"notes": "x" * 10001},
        {"revision": True},
        {"revision": -1},
        {"vehicle": ["vehicle"]},
    ],
)
def test_invalid_reviews_rejected_without_writes(site, change):
    assert request(site, "/api/reviews", review(**change))[0] == 400
    assert not site[1].export()["events"]


def test_duplicate_parameters_context_bounds_and_unknown_cases(site):
    for query in (
        "method=avl_context&method=gap_only&vehicle=vehicle&case_id=case",
        "method=avl_context&vehicle=vehicle&case_id=case&context_s=99999",
        "method=avl_context&vehicle=vehicle&case_id=other",
    ):
        assert request(site, "/api/case?" + query)[0] == 400


def test_conflicting_revision_returns409_and_keeps_first_note(site):
    assert request(site, "/api/reviews", review(notes="first"))[0] == 200
    assert request(site, "/api/reviews", review(notes="stale"))[0] == 409
    assert site[1].get("avl_context", "vehicle", "case")["notes"] == "first"
    assert len(site[1].export()["events"]) == 1


def test_concurrent_store_saves_are_serialized_and_run_scoped(tmp_path):
    store = ReviewStore(tmp_path, "run-A")

    def save_one(number):
        try:
            store.save("avl_context", "vehicle", "case", "problem", str(number), 0)
            return "saved"
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save_one, [1, 2])) == ["conflict", "saved"]
    assert len(store.export()["events"]) == 1
    assert ReviewStore(tmp_path, "run-B").export()["reviews"] == []
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM reviews").fetchone()[0] == 1


def test_unknown_numbers_remain_null_not_zero():
    assert json_safe({"load": float("nan"), "distance": float("inf")}) == {
        "load": None,
        "distance": None,
    }


def test_non_loopback_listener_forbidden(tmp_path):
    with pytest.raises(ValueError, match="回环"):
        ReviewServer(("0.0.0.0", 0), FakeData(), ReviewStore(tmp_path, "fixture"))
