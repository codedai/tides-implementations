"""Read-only HTTP acceptance for retained runs, tied to a fresh Linux server PID.

This verifies cold-process provenance and API-renderable raw/GTFS evidence. It
does not claim to replace the separate real-browser visual acceptance suite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

VERSION = "retained-workbench-http-acceptance-v1"
MODES = ("global", "early", "middle", "late")
RETAINED_RUNS = (
    "run-20231106-20260909T183705-d67ffa30723d",
    "run-20231106-20260910T030622-57e42718b0c1",
    "run-20231106-20260910T041452-de4609d0825b",
)
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def _origin(value):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Acceptance origin must be plain http://127.0.0.1:PORT")
    port = parsed.port or 80
    if not 1 <= port <= 65535:
        raise ValueError("Invalid local server port")
    return f"http://127.0.0.1:{port}", port


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _launch_identity(command):
    if b"tools.merge_review.server" in command:
        return {"launch_kind": "server_module", "module": "tools.merge_review.server"}
    args = [value.decode("utf-8") for value in command if value]

    def option(name):
        positions = [index for index, value in enumerate(args) if value == name]
        if not positions or positions[-1] + 1 >= len(args):
            raise ValueError(
                "Frozen server process lacks an explicit release/data root"
            )
        return args[positions[-1] + 1]

    def safe(path):
        path = Path(path)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or any(part.is_symlink() for part in (path, *path.parents))
        ):
            raise ValueError("Unsafe frozen server release path")
        return path

    release, data_root = safe(option("--frozen")), safe(option("--data-root"))
    script = safe(release / "code/tools/merge_review/workbench.py")
    if (
        str(script) not in args
        or "serve" not in args
        or "-I" not in args
        or "-B" not in args
    ):
        raise ValueError("PID is not the isolated frozen workbench serve process")
    pointer = json.loads(safe(data_root / "config/workbench-release.json").read_text())
    selected = safe(data_root / pointer["release"])
    if selected != release:
        raise ValueError("Frozen server PID is not using the selected release")
    lock = safe(release / "release.json")
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    checksum = json.loads(safe(release / "release.sha256.json").read_text())
    if checksum.get("release.json") != digest or pointer.get("sha256") != digest:
        raise ValueError(
            "Frozen server release manifest differs from its selected hash"
        )
    manifest = json.loads(lock.read_text())
    script_digest = hashlib.sha256(script.read_bytes()).hexdigest()
    if (
        manifest.get("schema") != "frozen-workbench-v1"
        or manifest.get("code_sha256", {}).get("tools/merge_review/workbench.py")
        != script_digest
    ):
        raise ValueError("Frozen server entry point differs from the pinned release")
    return {
        "launch_kind": "frozen_workbench",
        "module": "tools.merge_review.workbench",
        "release_path": str(release),
        "release_sha256": digest,
        "entry_script_sha256": script_digest,
        "data_root": str(data_root),
    }


def inspect_server_identity(
    pid,
    origin,
    fresh_after,
    validation_started,
    *,
    proc_root=Path("/proc"),
    wall_clock=time.time,
    boot_clock=None,
    clock_ticks=None,
):
    """Prove this PID started after the barrier and owns the loopback listener.

    Linux starttime uses boot-clock ticks. CLOCK_BOOTTIME avoids the whole-second
    rounding in /proc/stat's btime. One tick of startup-time uncertainty is explicit.
    """
    _, port = _origin(origin)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("A positive server PID is required")
    if (
        not _finite(fresh_after)
        or not _finite(validation_started)
        or fresh_after > validation_started
    ):
        raise ValueError("Fresh-start barrier must precede this validation")
    proc = Path(proc_root) / str(pid)
    try:
        fields = (proc / "stat").read_text().rsplit(") ", 1)[1].split()
        start_ticks = int(fields[19])
        command = (proc / "cmdline").read_bytes().split(b"\0")
        launch = _launch_identity(command)
        ticks = clock_ticks if clock_ticks is not None else os.sysconf("SC_CLK_TCK")
        if boot_clock is not None:
            boot_now, boot_resolution = boot_clock(), 0
            clock_source = "injected_boot_clock"
        elif hasattr(time, "CLOCK_BOOTTIME"):
            boot_now = time.clock_gettime(time.CLOCK_BOOTTIME)
            boot_resolution, clock_source = 0, "CLOCK_BOOTTIME"
        else:
            # Some otherwise Linux-compatible Python builds omit this constant.
            # /proc/uptime is the same boot-time clock, exposed to 0.01 seconds.
            boot_now = float((Path(proc_root) / "uptime").read_text().split()[0])
            boot_resolution, clock_source = 0.01, "proc_uptime"
        sampled_at = wall_clock()
        started_at = sampled_at - boot_now + start_ticks / ticks
        resolution = 1 / ticks + boot_resolution
        if (
            started_at + resolution < fresh_after
            or started_at > validation_started + resolution
        ):
            raise ValueError(
                "Server PID did not start inside the required fresh-start interval"
            )
        owned = set()
        for descriptor in (proc / "fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue  # A unrelated short-lived descriptor may close mid-scan.
            if target.startswith("socket:[") and target.endswith("]"):
                owned.add(target[8:-1])
        listeners = set()
        for line in (proc / "net/tcp").read_text().splitlines()[1:]:
            columns = line.split()
            if len(columns) < 10:
                continue
            address, hex_port = columns[1].split(":")
            if (
                address == "0100007F"
                and int(hex_port, 16) == port
                and columns[3] == "0A"
            ):
                if columns[9] in owned:
                    listeners.add(columns[9])
        if not listeners:
            raise ValueError(
                "Server PID does not own the requested loopback listening socket"
            )
    except (OSError, IndexError, AttributeError) as error:
        raise ValueError(
            "Cannot verify fresh Linux server process identity via /proc"
        ) from error
    return {
        "pid": pid,
        "start_ticks": start_ticks,
        "started_at_epoch": started_at,
        "fresh_after_epoch": fresh_after,
        "validation_started_epoch": validation_started,
        "clock_resolution_s": resolution,
        "boot_clock_source": clock_source,
        "port": port,
        "listener_inodes": sorted(listeners),
        **launch,
        "proc_start_verified": True,
        "listener_owner_verified": True,
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Acceptance refuses HTTP redirects")


class LocalJSONClient:
    def __init__(self, origin, *, timeout=60):
        self.origin, _ = _origin(origin)
        self.timeout = timeout
        self.requests = []
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def __call__(self, endpoint, **query):
        if endpoint not in {
            "/api/health",
            "/api/meta",
            "/api/catalog",
            "/api/case",
            "/api/day-overview",
        }:
            raise ValueError(
                "Acceptance only permits the explicit read-only API allowlist"
            )
        url = self.origin + endpoint
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url, method="GET", headers={"Cache-Control": "no-cache"}
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            if response.status != 200 or "application/json" not in response.headers.get(
                "Content-Type", ""
            ):
                raise ValueError(f"Unexpected HTTP response for {endpoint}")
            if "no-store" not in response.headers.get("Cache-Control", ""):
                raise ValueError(f"Missing no-store response policy for {endpoint}")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError("Acceptance response exceeded its byte limit")
        self.requests.append(
            {"endpoint": endpoint, "method": "GET", "bytes": len(payload)}
        )
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise ValueError(f"Expected JSON object for {endpoint}")
        return result


class _Failure(ValueError):
    pass


def validate_retained(
    origin,
    server_pid,
    fresh_after,
    *,
    run_ids=RETAINED_RUNS,
    expected_vehicles=31,
    expected_cases=306,
    fetch=None,
    process_probe=None,
    progress=None,
):
    """Return an auditable PASS/FAIL report; perform no server-side writes."""
    started = time.time()
    report = {
        "version": VERSION,
        "status": "FAIL",
        "cold_start": False,
        "started_at_epoch": started,
        "origin": origin,
        "checks": [],
        "matrix": [],
        "matrix_counts": {
            "runs": 0,
            "vehicle_catalogs": 0,
            "vehicle_raw_details": 0,
            "mode_overviews": 0,
            "mode_representative_details": 0,
            "candidate_trajectories": 0,
        },
        "server_writes_performed": False,
        "matching_recomputed": False,
        "visual_browser_acceptance_performed": False,
        "cold_start_definition": (
            "The same Linux process started after the supplied fresh-start barrier "
            "and owned this loopback listener before and after all uncached GET checks."
        ),
    }

    def check(condition, name):
        report["checks"].append({"name": name, "passed": bool(condition)})
        if not condition:
            raise _Failure(name)

    def validate_detail(detail, run, vehicle, case_id, mode):
        check(detail.get("case", {}).get("id") == case_id, "detail exact case identity")
        check(
            detail.get("provenance", {}).get("run_id") == run,
            "detail exact run identity",
        )
        check(
            detail.get("case", {}).get("vehicle") == vehicle,
            "detail exact vehicle identity",
        )
        gtfs = detail.get("gtfs", {})
        check(gtfs.get("matching_mode") == mode, "detail exact matching mode")
        check(
            set(gtfs.get("available_matching_modes", [])) == set(MODES),
            "detail four computed modes",
        )
        observations, avl, apc = (
            detail.get(key) for key in ("observations", "avl", "apc")
        )
        check(
            all(isinstance(rows, list) for rows in (observations, avl, apc)),
            "raw arrays present",
        )
        check(bool(observations), "raw detail contains actual source observations")
        identities = [(row.get("source"), str(row.get("rid"))) for row in observations]
        check(
            len(identities) == len(set(identities)), "raw observation identities unique"
        )
        check(
            set(identities)
            == {("avl", str(row.get("rid"))) for row in avl}
            | {("apc", str(row.get("rid"))) for row in apc},
            "raw APC and AVL arrays conserve combined observations",
        )
        check(
            all(str(row.get("route", "")).strip() == "29" for row in observations),
            "raw observations remain route 29 only",
        )
        check(
            all(isinstance(row.get("source_fields"), dict) for row in observations),
            "raw source fields retained",
        )
        counts = detail.get("counts", {})
        check(
            counts.get("observations") == len(observations),
            "raw observation count agrees",
        )
        check(
            counts.get("avl_returned") == len(avl)
            and counts.get("apc_returned") == len(apc),
            "raw source counts agree",
        )
        check(
            isinstance(gtfs.get("projection", {}).get("points"), list),
            "projection diagnostic points readable",
        )
        check(
            isinstance(gtfs.get("apc_evidence"), list),
            "APC stop interval evidence readable",
        )
        check(
            isinstance(gtfs.get("references"), list) and bool(gtfs["references"]),
            "canonical reference geometry readable",
        )
        check(
            isinstance(gtfs.get("result"), dict)
            and isinstance(gtfs.get("candidates"), list)
            and isinstance(gtfs.get("diagnostic_candidates"), list),
            "formal and diagnostic candidate structures readable",
        )
        return {
            "avl_rows": len(avl),
            "apc_rows": len(apc),
            "observation_rows": len(observations),
        }

    try:
        origin, _ = _origin(origin)
        if not run_ids or len(set(run_ids)) != len(run_ids):
            raise ValueError("Acceptance requires distinct explicit retained runs")
        if expected_vehicles <= 0 or expected_cases <= 0:
            raise ValueError("Expected vehicle/case counts must be positive")
        probe = process_probe or inspect_server_identity
        initial_identity = probe(server_pid, origin, fresh_after, started)
        report["server_identity_before"] = initial_identity
        check(
            initial_identity.get("proc_start_verified") is True
            and initial_identity.get("listener_owner_verified") is True,
            "fresh server process and socket ownership verified",
        )
        get = fetch or LocalJSONClient(origin)
        health = get("/api/health")
        check(
            health.get("ok") is True and health.get("local_only") is True,
            "local server health",
        )
        for run in run_ids:
            if progress:
                progress(f"Checking retained run {run}")
            meta = get("/api/meta", run=run)
            check(meta.get("run_id") == run, "metadata exact retained run")
            check(
                meta.get("debug_date") == "2023-11-06"
                and meta.get("route_scope") == ["29"],
                "metadata date and route scope",
            )
            check(
                set(meta.get("available_matching_modes", [])) == set(MODES),
                "metadata advertises all computed modes",
            )
            vehicles = meta.get("vehicles", [])
            check(
                len(vehicles) == expected_vehicles
                and len({row["id"] for row in vehicles}) == expected_vehicles,
                "metadata complete unique vehicle coverage",
            )
            method = meta.get("default_method")
            check(bool(method), "GTFS review method available")
            record = {"run_id": run, "vehicles": [], "modes": []}
            report["matrix"].append(record)
            catalog_ids = set()
            for vehicle in vehicles:
                vehicle_id = vehicle["id"]
                catalog = get(
                    "/api/catalog",
                    run=run,
                    method=method,
                    vehicle=vehicle_id,
                    matching_mode="global",
                )
                cases = catalog.get("cases", [])
                check(
                    bool(cases) and len({row["id"] for row in cases}) == len(cases),
                    "vehicle catalog nonempty with unique case identities",
                )
                check(
                    catalog.get("matching_mode") == "global"
                    and catalog.get("stats", {}).get("total") == len(cases),
                    "vehicle catalog mode and total agree",
                )
                catalog_ids.update((vehicle_id, row["id"]) for row in cases)
                # Prefer a substantial case with both sources; do not require every
                # split segment to have APC when the original data does not.
                chosen = max(
                    cases,
                    key=lambda row: (
                        bool(row.get("avl_count") and row.get("apc_count")),
                        row.get("avl_count", 0) + row.get("apc_count", 0),
                    ),
                )
                detail = get(
                    "/api/case",
                    run=run,
                    method=method,
                    vehicle=vehicle_id,
                    case_id=chosen["id"],
                    matching_mode="global",
                    context_s=180,
                )
                counts = validate_detail(
                    detail, run, vehicle_id, chosen["id"], "global"
                )
                record["vehicles"].append(
                    {
                        "vehicle": vehicle_id,
                        "vehicle_label": vehicle.get("label"),
                        "case_count": len(cases),
                        "raw_detail_case_id": chosen["id"],
                        **counts,
                    }
                )
                report["matrix_counts"]["vehicle_catalogs"] += 1
                report["matrix_counts"]["vehicle_raw_details"] += 1
            check(
                len(catalog_ids) == expected_cases,
                "all vehicle catalogs contain the expected whole-day case set",
            )
            for mode in MODES:
                overview = get("/api/day-overview", run=run, matching_mode=mode)
                cases = overview.get("cases", [])
                check(
                    overview.get("run_id") == run
                    and overview.get("matching_mode") == mode,
                    "overview exact run and matching mode",
                )
                check(
                    overview.get("day") == "2023-11-06"
                    and overview.get("route_scope") == ["29"],
                    "overview original day and route",
                )
                check(
                    len(cases) == expected_cases
                    and {(row["vehicle"], row["id"]) for row in cases} == catalog_ids,
                    "overview contains every catalogued vehicle/case",
                )
                check(
                    overview.get("summary", {}).get("vehicle_count")
                    == expected_vehicles,
                    "overview covers every retained vehicle",
                )
                check(
                    overview.get("matching_recomputed") is False
                    and overview.get("schedule_time_basis")
                    == "original_scheduled_wall_clock",
                    "overview reuses original-clock stored results",
                )
                references = {
                    row["reference_id"] for row in overview.get("reference_facets", [])
                }
                check(bool(references), "overview canonical reference facets present")
                check(
                    all(
                        group.get("reference_id") in references
                        and isinstance(group.get("points"), list)
                        and all(
                            isinstance(point, list)
                            and len(point) >= 2
                            and _finite(point[0])
                            and _finite(point[1])
                            for point in group["points"]
                        )
                        for row in cases
                        for group in row.get("fragments", [])
                    ),
                    "overview valid projected fragment coordinates and reference axes",
                )
                chosen = next(
                    (
                        row
                        for row in cases
                        if isinstance(row.get("candidate"), dict)
                        and row["candidate"].get("trajectory")
                    ),
                    None,
                )
                check(
                    chosen is not None,
                    "mode has a renderable candidate or explicit diagnostic fallback",
                )
                candidate = chosen["candidate"]
                check(
                    candidate.get("source") in {"accepted", "fallback"}
                    and candidate.get("reference_id") in references,
                    "overview candidate source and canonical reference explicit",
                )
                trajectory = candidate.get("trajectory", [])
                check(
                    len(trajectory) >= 2
                    and all(
                        _finite(point.get("t")) and _finite(point.get("s"))
                        for point in trajectory
                    ),
                    "overview candidate trajectory finite and nonempty",
                )
                detail = get(
                    "/api/case",
                    run=run,
                    method=method,
                    vehicle=chosen["vehicle"],
                    case_id=chosen["id"],
                    matching_mode=mode,
                    context_s=180,
                )
                counts = validate_detail(
                    detail, run, chosen["vehicle"], chosen["id"], mode
                )
                cards = (
                    detail["gtfs"]["candidates"]
                    + detail["gtfs"]["diagnostic_candidates"]
                )
                matching = next(
                    (
                        card
                        for card in cards
                        if card.get("candidate_id") == candidate.get("candidate_id")
                    ),
                    None,
                )
                check(
                    matching is not None,
                    "overview representative candidate exists in the exact-mode detail",
                )
                check(
                    _finite(matching.get("score"))
                    and math.isclose(
                        matching["score"],
                        candidate["score"],
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    ),
                    "overview and detail candidate costs unchanged",
                )
                pairs = [
                    (point.get("t"), point.get("s"))
                    for point in matching.get("trajectory", [])
                    if _finite(point.get("t")) and _finite(point.get("s"))
                ]
                check(
                    pairs == [(point["t"], point["s"]) for point in trajectory],
                    "overview and detail trajectories share the original clock",
                )
                record["modes"].append(
                    {
                        "matching_mode": mode,
                        "overview_cases": len(cases),
                        "overview_vehicles": expected_vehicles,
                        "representative_vehicle": chosen["vehicle"],
                        "representative_case_id": chosen["id"],
                        "candidate_id": candidate["candidate_id"],
                        "candidate_source": candidate["source"],
                        "trajectory_points": len(trajectory),
                        **counts,
                    }
                )
                for key in (
                    "mode_overviews",
                    "mode_representative_details",
                    "candidate_trajectories",
                ):
                    report["matrix_counts"][key] += 1
            report["matrix_counts"]["runs"] += 1
            if progress:
                progress(
                    f"Verified {run}: {len(vehicles)} vehicle raw details "
                    "and all four mode comparisons"
                )
        final_identity = probe(server_pid, origin, fresh_after, started)
        report["server_identity_after"] = final_identity
        check(
            all(
                initial_identity.get(key) == final_identity.get(key)
                for key in (
                    "pid",
                    "start_ticks",
                    "port",
                    "listener_inodes",
                    "launch_kind",
                    "release_sha256",
                    "entry_script_sha256",
                )
            ),
            "same fresh server process and listener throughout acceptance",
        )
        report["cold_start"] = True
        report["status"] = "PASS"
        requests = getattr(get, "requests", [])
        report["http_request_counts"] = dict(
            Counter(row["endpoint"] for row in requests)
        )
        report["http_response_bytes"] = sum(row.get("bytes", 0) for row in requests)
    except (ValueError, OSError, KeyError, TypeError, StopIteration) as error:
        report["error"] = str(error)
    report["finished_at_epoch"] = time.time()
    report["elapsed_s"] = report["finished_at_epoch"] - started
    report["passed_check_count"] = sum(row["passed"] for row in report["checks"])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:8767")
    parser.add_argument("--server-pid", type=int, required=True)
    parser.add_argument(
        "--fresh-after",
        type=float,
        required=True,
        help="Unix epoch seconds recorded immediately before restarting the server",
    )
    parser.add_argument("--run", action="append", dest="runs")
    parser.add_argument("--expected-vehicles", type=int, default=31)
    parser.add_argument("--expected-cases", type=int, default=306)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Refusing to overwrite an existing acceptance report")
    report = validate_retained(
        args.origin,
        args.server_pid,
        args.fresh_after,
        run_ids=args.runs or RETAINED_RUNS,
        expected_vehicles=args.expected_vehicles,
        expected_cases=args.expected_cases,
        progress=lambda message: print(message, file=sys.stderr, flush=True),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "cold_start": report["cold_start"],
                "matrix_counts": report["matrix_counts"],
                "report": str(args.output),
                "error": report.get("error"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
