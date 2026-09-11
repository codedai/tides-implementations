"""No live server required: cold-PID proofs and full retained API matrix checks."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.merge_review.retained_acceptance import (
    MODES,
    RETAINED_RUNS,
    LocalJSONClient,
    inspect_server_identity,
    validate_retained,
)


@pytest.fixture
def process(tmp_path):
    proc = tmp_path / "proc"
    directory = proc / "42"
    (directory / "fd").mkdir(parents=True)
    (directory / "net").mkdir()
    fields = ["S"] + ["0"] * 18 + ["50000"]
    (directory / "stat").write_text("42 (python server) " + " ".join(fields))
    (directory / "cmdline").write_bytes(b"python\0-m\0tools.merge_review.server\0")
    (directory / "fd/3").symlink_to("socket:[12345]")
    (directory / "net/tcp").write_text(
        "sl local_address rem_address st tx_queue tr retrnsmt uid timeout inode\n"
        "0: 0100007F:223F 00000000:0000 0A 00000000:00000000 "
        "00:00000000 00000000 1000 0 12345\n"
    )
    return SimpleNamespace(root=proc, directory=directory)


def probe(process, **overrides):
    arguments = {
        "pid": 42,
        "origin": "http://127.0.0.1:8767",
        "fresh_after": 9400,
        "validation_started": 9600,
        "proc_root": process.root,
        "wall_clock": lambda: 10000,
        "boot_clock": lambda: 1000,
        "clock_ticks": 100,
    }
    return inspect_server_identity(**(arguments | overrides))


def test_cold_process_identity_requires_new_python_pid_and_its_exact_socket(process):
    result = probe(process)
    assert result["started_at_epoch"] == 9500
    assert result["start_ticks"] == 50000
    assert result["listener_inodes"] == ["12345"]
    assert result["clock_resolution_s"] == 0.01
    assert result["proc_start_verified"] and result["listener_owner_verified"]


def test_frozen_launcher_is_bound_to_selected_release_and_entry_script(
    process, tmp_path
):
    root = tmp_path / "data"
    release = root / "releases" / "frozen-04b"
    script = release / "code/tools/merge_review/workbench.py"
    script.parent.mkdir(parents=True)
    script.write_text("# frozen entry point")
    script_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    manifest = {
        "schema": "frozen-workbench-v1",
        "code_sha256": {"tools/merge_review/workbench.py": script_sha},
    }
    (release / "release.json").write_text(json.dumps(manifest))
    digest = hashlib.sha256((release / "release.json").read_bytes()).hexdigest()
    (release / "release.sha256.json").write_text(json.dumps({"release.json": digest}))
    (root / "config").mkdir()
    pointer = root / "config/workbench-release.json"
    pointer.write_text(json.dumps({"release": "releases/frozen-04b", "sha256": digest}))
    args = [
        "python",
        "-I",
        "-B",
        str(script),
        "serve",
        "--frozen",
        str(release),
        "--data-root",
        str(root),
    ]
    (process.directory / "cmdline").write_bytes(
        b"\0".join(value.encode() for value in args)
    )
    result = probe(process)
    assert result["launch_kind"] == "frozen_workbench"
    assert result["release_sha256"] == digest
    assert result["entry_script_sha256"] == script_sha
    script.write_text("# changed after startup")
    with pytest.raises(ValueError, match="entry point differs"):
        probe(process)


@pytest.mark.parametrize(
    "change", ["old", "future", "different_socket", "wrapper", "wildcard"]
)
def test_old_or_unowned_server_cannot_claim_cold_start(process, change):
    if change == "old":
        with pytest.raises(ValueError, match="fresh-start interval"):
            probe(process, fresh_after=9501)
    elif change == "future":
        with pytest.raises(ValueError, match="fresh-start interval"):
            probe(process, validation_started=9499)
    else:
        if change == "different_socket":
            (process.directory / "fd/3").unlink()
            (process.directory / "fd/3").symlink_to("socket:[98765]")
        elif change == "wrapper":
            (process.directory / "cmdline").write_bytes(b"uv\0run\0python\0")
        else:
            path = process.directory / "net/tcp"
            path.write_text(path.read_text().replace("0100007F", "00000000"))
        with pytest.raises(ValueError):
            probe(process)


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:8767",
        "http://example.com",
        "http://127.0.0.1:8767/path",
        "http://secret@127.0.0.1:8767",
        "http://127.0.0.1:8767?query=1",
    ],
)
def test_acceptance_never_connects_to_external_or_ambiguous_origins(process, origin):
    with pytest.raises(ValueError, match="Acceptance origin"):
        probe(process, origin=origin)


class FakeAPI:
    def __init__(self, mutate=None):
        self.requests = []
        self.mutate = mutate

    @staticmethod
    def candidate():
        return {
            "candidate_id": "feed|2023-11-06|trip",
            "score": 1.0,
            "source": "fallback",
            "reference_id": "ref",
            "trajectory": [{"t": 1.0, "s": 2.0}, {"t": 2.0, "s": 3.0}],
        }

    def __call__(self, endpoint, **query):
        self.requests.append({"endpoint": endpoint, "method": "GET", "bytes": 20})
        run = query.get("run")
        mode = query.get("matching_mode", "global")
        if endpoint == "/api/health":
            result = {"ok": True, "local_only": True}
        elif endpoint == "/api/meta":
            result = {
                "run_id": run,
                "debug_date": "2023-11-06",
                "route_scope": ["29"],
                "available_matching_modes": list(MODES),
                "default_method": "gtfs_trip_candidates",
                "vehicles": [
                    {"id": "a", "label": "9042"},
                    {"id": "b", "label": "9021"},
                ],
                "csrf_token": "must-never-appear-in-the-report",
            }
        elif endpoint == "/api/catalog":
            result = {
                "matching_mode": mode,
                "cases": [
                    {"id": "case-" + query["vehicle"], "avl_count": 1, "apc_count": 1}
                ],
                "stats": {"total": 1},
            }
        elif endpoint == "/api/day-overview":
            result = {
                "run_id": run,
                "day": "2023-11-06",
                "route_scope": ["29"],
                "matching_mode": mode,
                "matching_recomputed": False,
                "schedule_time_basis": "original_scheduled_wall_clock",
                "reference_facets": [{"reference_id": "ref"}],
                "summary": {"vehicle_count": 2},
                "cases": [
                    {
                        "id": "case-" + vehicle,
                        "vehicle": vehicle,
                        "candidate": self.candidate(),
                        "fragments": [
                            {"reference_id": "ref", "points": [[1.0, 2.0], [2.0, 3.0]]}
                        ],
                    }
                    for vehicle in ("a", "b")
                ],
            }
        elif endpoint == "/api/case":
            rows = [
                {
                    "source": source,
                    "rid": source + "-rid",
                    "route": "29",
                    "source_fields": {"original": "source"},
                }
                for source in ("avl", "apc")
            ]
            result = {
                "case": {"id": query["case_id"], "vehicle": query["vehicle"]},
                "provenance": {"run_id": run},
                "observations": rows,
                "avl": [rows[0]],
                "apc": [rows[1]],
                "counts": {"observations": 2, "avl_returned": 1, "apc_returned": 1},
                "gtfs": {
                    "matching_mode": mode,
                    "available_matching_modes": list(MODES),
                    "projection": {"points": []},
                    "apc_evidence": [],
                    "references": [{"reference_id": "ref"}],
                    "result": {"status": "PATTERN_ONLY"},
                    "candidates": [],
                    "diagnostic_candidates": [self.candidate()],
                },
            }
        else:
            raise AssertionError(endpoint)
        result = copy.deepcopy(result)
        if self.mutate:
            self.mutate(endpoint, query, result)
        return result


def verified_probe(pid, origin, fresh_after, started):
    return {
        "pid": pid,
        "start_ticks": 12345,
        "port": 8767,
        "listener_inodes": ["321"],
        "proc_start_verified": True,
        "listener_owner_verified": True,
    }


def validate(api, **extra):
    return validate_retained(
        "http://127.0.0.1:8767",
        42,
        1.0,
        expected_vehicles=2,
        expected_cases=2,
        fetch=api,
        process_probe=verified_probe,
        **extra,
    )


def test_complete_three_run_all_vehicle_four_mode_http_matrix():
    api = FakeAPI()
    result = validate(api)
    assert result["status"] == "PASS"
    assert result["cold_start"] is True
    assert result["matrix_counts"] == {
        "runs": 3,
        "vehicle_catalogs": 6,
        "vehicle_raw_details": 6,
        "mode_overviews": 12,
        "mode_representative_details": 12,
        "candidate_trajectories": 12,
    }
    assert [row["run_id"] for row in result["matrix"]] == list(RETAINED_RUNS)
    assert all(
        len(row["vehicles"]) == 2 and len(row["modes"]) == 4 for row in result["matrix"]
    )
    assert result["server_writes_performed"] is False
    assert result["visual_browser_acceptance_performed"] is False
    assert all(row["method"] == "GET" for row in api.requests)
    assert "must-never-appear-in-the-report" not in json.dumps(result)


@pytest.mark.parametrize(
    "change",
    [
        "missing_vehicle",
        "missing_case",
        "wrong_mode",
        "altered_trajectory",
        "lost_raw_fields",
        "no_candidate",
        "wrong_run",
    ],
)
def test_incomplete_or_mismatched_retained_matrix_cannot_pass(change):
    def mutate(endpoint, query, result):
        if change == "missing_vehicle" and endpoint == "/api/meta":
            result["vehicles"].pop()
        if change == "missing_case" and endpoint == "/api/day-overview":
            result["cases"].pop()
        if (
            change == "wrong_mode"
            and endpoint == "/api/case"
            and query["matching_mode"] == "middle"
        ):
            result["gtfs"]["matching_mode"] = "global"
        if change == "altered_trajectory" and endpoint == "/api/case":
            result["gtfs"]["diagnostic_candidates"][0]["trajectory"][0]["t"] = 9999
        if change == "lost_raw_fields" and endpoint == "/api/case":
            del result["observations"][0]["source_fields"]
        if change == "no_candidate" and endpoint == "/api/day-overview":
            for row in result["cases"]:
                row["candidate"] = None
        if change == "wrong_run" and endpoint == "/api/meta":
            result["run_id"] = "other-run"

    result = validate(FakeAPI(mutate))
    assert result["status"] == "FAIL"
    assert result["cold_start"] is False
    assert result["error"]


def test_pid_restart_during_acceptance_cannot_pass():
    calls = []

    def changing_probe(*args):
        calls.append(True)
        return verified_probe(*args) | {"start_ticks": len(calls)}

    result = validate_retained(
        "http://127.0.0.1:8767",
        42,
        1,
        expected_vehicles=2,
        expected_cases=2,
        fetch=FakeAPI(),
        process_probe=changing_probe,
    )
    assert result["status"] == "FAIL" and result["cold_start"] is False
    assert "same fresh server process" in result["error"]


def test_cold_probe_failure_prevents_every_http_request():
    api = FakeAPI()

    def invalid_probe(*args):
        raise ValueError("Old process")

    result = validate_retained(
        "http://127.0.0.1:8767", 42, 1, fetch=api, process_probe=invalid_probe
    )
    assert result["status"] == "FAIL"
    assert not api.requests


def test_json_client_only_issues_get_and_requires_uncached_json():
    class Response:
        status = 200
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return b'{"ok":true}'

    requests = []
    client = LocalJSONClient("http://127.0.0.1:8767")

    def open_request(request, timeout):
        requests.append(request)
        return Response()

    client.opener = SimpleNamespace(open=open_request)
    assert client("/api/health") == {"ok": True}
    assert requests[0].method == "GET" and requests[0].data is None
    with pytest.raises(ValueError, match="read-only API allowlist"):
        client("/api/reviews")
    Response.headers = {"Content-Type": "application/json"}
    with pytest.raises(ValueError, match="no-store"):
        client("/api/health")


def test_browser_optional_legacy_skip_does_not_skip_four_mode_checks():
    source = Path(__file__).with_name("browser_gtfs_modes_smoke.mjs").read_text()
    assert "args.includes('--skip-legacy-global')" in source
    assert "if (!skipLegacyGlobal) {\n    await switchRun(legacy);" in source
    assert "legacy_global_skipped: skipLegacyGlobal" in source
    assert source.index(
        "for (const mode of ['early', 'middle', 'late'])"
    ) < source.index("if (!skipLegacyGlobal)")
