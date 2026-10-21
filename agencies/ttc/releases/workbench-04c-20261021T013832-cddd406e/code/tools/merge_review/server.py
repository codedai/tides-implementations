"""Loopback-only HTTP server; no dependencies beyond the existing TTC env."""

from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import json
import math
import os
import secrets
import threading
import time
import uuid
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tools.merge_review.paths import project_root

from .store import ReviewStore, RevisionConflict

TTC_ROOT = project_root()
DEFAULT_RUN = TTC_ROOT / "artifacts/apc-avl-direct-evidence-20260908/full02"
DEFAULT_STATE = TTC_ROOT / "artifacts/merge-review-website-20260908/reviews"
DEFAULT_DEBUG = TTC_ROOT / "artifacts/merge-debug-20231106"
DEFAULT_AUDIT = TTC_ROOT / "artifacts/apc-avl-coverage-study-20260908/raw_audit"
STATIC = Path(__file__).with_name("static")


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def run_fingerprint(run: Path):
    digest = hashlib.sha256()
    for name in ("manifest.json", "config.json", "completion.json"):
        digest.update(name.encode())
        digest.update((run / name).read_bytes())
    return digest.hexdigest()


class DebugBusy(ValueError):
    """Keep expensive daily experiments serial without blocking review reads."""


class DebugJobs:
    def __init__(self, workspace):
        self.workspace = workspace
        self.lock = threading.RLock()
        self.jobs = collections.OrderedDict()

    def submit(self, body):
        if not isinstance(body, dict) or not {"config"} <= set(body) <= {
            "config",
            "label",
            "parent_run_id",
        }:
            raise ValueError(
                "Experiment requests require config; label and parent_run_id "
                "are optional"
            )
        config = self.workspace.validate_config(body["config"])
        label = body.get("label", "")
        if not isinstance(label, str) or len(label) > 160:
            raise ValueError("Experiment name must be text of at most 160 characters")
        parent = body.get("parent_run_id")
        if parent is not None:
            if not isinstance(parent, str):
                raise ValueError("parent_run_id must identify a completed experiment")
            self.workspace.get_run(parent)
        with self.lock:
            if any(j["state"] in {"queued", "running"} for j in self.jobs.values()):
                raise DebugBusy(
                    "A one-day experiment is already running. "
                    "Wait for it to finish before submitting another"
                )
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {
                "id": job_id,
                "state": "queued",
                "progress": 0,
                "message": "Queued; processing 2023-11-06 only",
                "run_id": None,
                "error": None,
            }
            while len(self.jobs) > 100:
                self.jobs.popitem(last=False)
            thread = threading.Thread(
                target=self._run, args=(job_id, config, label, parent), daemon=True
            )
            thread.start()
            return copy.deepcopy(self.jobs[job_id])

    def _run(self, job_id, config, label, parent):
        started = time.monotonic()

        def progress(details):
            with self.lock:
                total = details.get("total") or 0
                done = details.get("completed") or 0
                self.jobs[job_id].update(
                    state="running",
                    progress=min(99, max(0, 100 * done / total)) if total else 0,
                    message=(
                        details.get("message")
                        or details.get("stage")
                        or "Processing"
                    ),
                    details=copy.deepcopy(details),
                    elapsed_s=round(time.monotonic() - started, 2),
                )

        try:
            progress(
                {"stage": "Preparing the day's inputs", "completed": 0, "total": 1}
            )
            result = self.workspace.create_run(
                config, label=label, parent_run_id=parent, progress=progress
            )
            with self.lock:
                self.jobs[job_id].update(
                    state="complete",
                    progress=100,
                    message="One-day experiment complete",
                    run_id=result.get("run_id") or result["id"],
                    result=result,
                    elapsed_s=round(time.monotonic() - started, 2),
                )
        except Exception as exc:
            with self.lock:
                self.jobs[job_id].update(
                    state="failed",
                    message=(
                        "Experiment incomplete; existing versions and reviews "
                        "are unchanged"
                    ),
                    error=str(exc),
                    elapsed_s=round(time.monotonic() - started, 2),
                )

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError(
                    "Unknown experiment job. After a server restart, check the "
                    "completed-run list for results"
                )
            return copy.deepcopy(self.jobs[job_id])


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, data, store, workspace=None, debug_state_dir=None):
        if address[0] not in {"127.0.0.1", "localhost"}:
            raise ValueError(
                "This review tool may bind only to a local loopback address"
            )
        self.data = data
        self.store = store
        self.data_lock = threading.RLock()
        self.workspace = workspace
        self.jobs = DebugJobs(workspace) if workspace is not None else None
        self.debug_state_dir = debug_state_dir
        self.run_cache = collections.OrderedDict()
        if workspace is not None:
            self.run_cache[data.run.name] = (data, store)
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(address, ReviewHandler)

    def resolve_run(self, run_id=None):
        """A request owns its explicit run; never mutate a shared active run."""
        if self.workspace is None:
            if run_id is not None:
                raise ValueError(
                    "Frozen review mode does not support switching experiment versions"
                )
            return self.data, self.store
        if run_id is None:
            runs = self.workspace.metadata()["runs"]
            if not runs:
                raise ValueError("No completed one-day experiments yet")
            latest = max(
                runs, key=lambda r: (r.get("created_at", ""), r.get("run_id", ""))
            )
            run_id = latest.get("run_id") or latest["id"]
        path = self.workspace.get_run(run_id)
        if run_id not in self.run_cache:
            from .gtfs_matching.adapter import load_review_data

            self.run_cache[run_id] = (
                load_review_data(path),
                ReviewStore(self.debug_state_dir, run_fingerprint(path)),
            )
        self.run_cache.move_to_end(run_id)
        while len(self.run_cache) > 3:
            self.run_cache.popitem(last=False)
        return self.run_cache[run_id]


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "MergeReview/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        # Avoid printing source identifiers from query strings to access logs.
        if fmt == '"%s" %s %s':
            args = (str(args[0]).split("?")[0], *args[1:])
        super().log_message(fmt, *args)

    def _trusted_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _send(self, payload, content_type, status=200, attachment=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://tile.openstreetmap.org; "
            "connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        if attachment:
            self.send_header(
                "Content-Disposition", 'attachment; filename="merge-reviews.json"'
            )
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, value, status=200, attachment=False):
        payload = json.dumps(
            json_safe(value), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self._send(payload, "application/json; charset=utf-8", status, attachment)

    @staticmethod
    def _required(query, key):
        values = query.get(key)
        if not values or len(values) != 1 or not values[0]:
            raise ValueError(f"Missing or repeated parameter: {key}")
        return values[0]

    @classmethod
    def _optional(cls, query, key):
        return cls._required(query, key) if key in query else None

    @classmethod
    def _run_id(cls, query):
        run = cls._optional(query, "run")
        run_id = cls._optional(query, "run_id")
        if run is not None and run_id is not None and run != run_id:
            raise ValueError(
                "run and run_id must refer to the same experiment version"
            )
        return run if run is not None else run_id

    @classmethod
    def _matching_kwargs(cls, query, data):
        mode = cls._optional(query, "matching_mode")
        if mode is None:
            return {}
        from .gtfs_matching.overview import validate_matching_mode

        validate_matching_mode(mode)
        if not hasattr(data, "available_matching_modes"):
            raise ValueError(
                "This version does not support selecting a GTFS matching mode"
            )
        return {"matching_mode": mode}

    def do_GET(self):  # noqa: N802
        if not self._trusted_host():
            self._json({"error": "Untrusted Host; use the local address"}, 403)
            return
        path = urlsplit(self.path)
        query = parse_qs(path.query, keep_blank_values=True)
        try:
            if path.path == "/api/health":
                self._json({"ok": True, "local_only": True})
            elif path.path == "/api/debug":
                if self.server.workspace is None:
                    self._json({"enabled": False})
                else:
                    metadata = self.server.workspace.metadata()
                    for run in metadata["runs"]:
                        run["id"] = run.get("run_id") or run.get("id")
                    self._json({"enabled": True, **metadata})
            elif path.path == "/api/debug/jobs":
                if self.server.jobs is None:
                    raise ValueError("One-day debug mode is not enabled")
                self._json({"job": self.server.jobs.get(self._required(query, "id"))})
            elif path.path == "/api/debug/compare":
                if self.server.workspace is None:
                    raise ValueError("One-day debug mode is not enabled")
                self._json(
                    self.server.workspace.compare(
                        self._required(query, "base"),
                        self._required(query, "run"),
                        vehicle=self._optional(query, "vehicle"),
                    )
                )
            elif path.path == "/api/meta":
                with self.server.data_lock:
                    data, store = self.server.resolve_run(self._optional(query, "run"))
                    metadata = dict(data.metadata())
                metadata.update(
                    csrf_token=self.server.csrf_token,
                    run_key=store.run_key,
                    debug_enabled=self.server.workspace is not None,
                )
                self._json(metadata)
            elif path.path == "/api/catalog":
                method = self._required(query, "method")
                vehicle = self._required(query, "vehicle")
                with self.server.data_lock:
                    data, store = self.server.resolve_run(self._run_id(query))
                    cases = data.catalog(
                        method, vehicle, **self._matching_kwargs(query, data)
                    )
                reviews = store.catalog(method, vehicle)
                valid_ids = {c["id"] for c in cases}
                self._json(
                    {
                        "cases": cases,
                        "matching_mode": self._optional(query, "matching_mode")
                        or "global",
                        "reviews": reviews,
                        "stats": {
                            "total": len(cases),
                            "reviewed": sum(
                                r["verdict"] != "unreviewed" and cid in valid_ids
                                for cid, r in reviews.items()
                            ),
                        },
                    }
                )
            elif path.path == "/api/case":
                method = self._required(query, "method")
                vehicle = self._required(query, "vehicle")
                case_id = self._required(query, "case_id")
                context_s = int(self._optional(query, "context_s") or "180")
                if not 0 <= context_s <= 1800:
                    raise ValueError(
                        "Context window must be between 0 and 1800 seconds"
                    )
                with self.server.data_lock:
                    data, store = self.server.resolve_run(self._run_id(query))
                    detail = dict(
                        data.case(
                            method,
                            vehicle,
                            case_id,
                            context_s=context_s,
                            **self._matching_kwargs(query, data),
                        )
                    )
                detail["review"] = store.get(method, vehicle, case_id)
                if detail.get("gtfs"):
                    from .gtfs_matching.ui_payload import compact_gtfs_detail

                    detail = compact_gtfs_detail(detail)
                self._json(detail)
            elif path.path == "/api/day-overview":
                from .gtfs_matching.overview import validate_matching_mode

                mode = self._optional(query, "matching_mode") or "global"
                validate_matching_mode(mode)
                with self.server.data_lock:
                    data, _ = self.server.resolve_run(self._run_id(query))
                    if not hasattr(data, "day_overview"):
                        raise ValueError(
                            "This version has no GTFS day overview; "
                            "select a GTFS matching experiment"
                        )
                    overview = data.day_overview(matching_mode=mode)
                self._json(overview)
            elif path.path == "/api/reviews/export":
                with self.server.data_lock:
                    _, store = self.server.resolve_run(self._optional(query, "run"))
                self._json(store.export(), attachment=True)
            else:
                assets = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/index.html": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                }
                if path.path not in assets:
                    self._json({"error": "Page or endpoint not found"}, 404)
                    return
                name, mime = assets[path.path]
                self._send((STATIC / name).read_bytes(), mime)
        except (ValueError, KeyError) as exc:
            self._json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self.log_error("Request failed (%s)", type(exc).__name__)
            self._json(
                {
                    "error": (
                        "Failed to read local evidence; "
                        "check the terminal and input directory"
                    )
                },
                500,
            )

    def do_POST(self):  # noqa: N802
        if not self._trusted_host():
            self._json({"error": "Untrusted Host"}, 403)
            return
        origin = self.headers.get("Origin")
        allowed_origins = {
            f"http://127.0.0.1:{self.server.server_port}",
            f"http://localhost:{self.server.server_port}",
        }
        token = self.headers.get("X-Review-Token", "")
        if (origin is not None and origin not in allowed_origins) or not (
            secrets.compare_digest(token, self.server.csrf_token)
        ):
            self._json(
                {"error": "Save-request validation failed; refresh the local page"}, 403
            )
            return
        request_path = urlsplit(self.path)
        query = parse_qs(request_path.query, keep_blank_values=True)
        if request_path.path not in {"/api/reviews", "/api/debug/runs"}:
            self._json({"error": "Endpoint not found"}, 404)
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._json({"error": "A JSON request is required"}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("Request body is empty or exceeds the 64 KiB limit")
            body = json.loads(self.rfile.read(length))
            if request_path.path == "/api/debug/runs":
                if self.server.jobs is None:
                    raise ValueError("One-day debug mode is not enabled")
                self._json({"job": self.server.jobs.submit(body)}, 202)
                return
            expected = {"method", "vehicle", "case_id", "verdict", "notes", "revision"}
            if self.server.workspace is not None:
                expected.add("run_id")
            if not isinstance(body, dict) or set(body) != expected:
                raise ValueError(
                    "Review fields are incomplete or contain unknown fields"
                )
            if not all(isinstance(body[k], str) for k in expected - {"revision"}):
                raise ValueError("Invalid review field format")
            run_id = body.pop("run_id", None)
            query_run = self._optional(query, "run")
            if query_run is not None and query_run != run_id:
                raise ValueError(
                    "The experiment version in the review request URL differs "
                    "from the request body"
                )
            with self.server.data_lock:
                data, store = self.server.resolve_run(run_id)
                exists = data.has_case(body["method"], body["vehicle"], body["case_id"])
            if not exists:
                raise ValueError(
                    "The review target is not in the specified result version"
                )
            result = store.save(**body)
            self._json(result)
        except (RevisionConflict, DebugBusy) as exc:
            self._json({"error": str(exc)}, 409)
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            self._json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self.log_error("Save failed (%s)", type(exc).__name__)
            self._json(
                {
                    "error": (
                        "Save failed; retain your notes and check the local disk "
                        "and terminal"
                    )
                },
                500,
            )


def main():
    from .gtfs_matching.adapter import load_review_data

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--debug-date",
        choices=["2023-11-06"],
        help=(
            "Re-run only the specified calendar day; "
            "do not extend to the full dataset"
        ),
    )
    parser.add_argument("--debug-dir", type=Path, default=DEFAULT_DEBUG)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    args = parser.parse_args()
    os.umask(0o077)
    run = args.run.resolve()
    workspace = None
    try:
        if args.debug_date:
            from .debug import DebugWorkspace

            workspace = DebugWorkspace(
                args.debug_dir.resolve(), args.audit.resolve(), args.debug_date
            )
            print(
                "Preparing local-day debug workspace: 2023-11-06 (no adjacent-day GPS)",
                flush=True,
            )
            workspace.ensure_fixture()
            metadata = workspace.metadata()
            if not metadata["runs"]:
                print("Building first daily baseline…", flush=True)
                record = workspace.create_run(
                    metadata["defaults"],
                    label="Initial baseline · 2023-11-06",
                    progress=lambda value: print(
                        json.dumps(json_safe(value), ensure_ascii=False), flush=True
                    ),
                )
            else:
                record = max(
                    metadata["runs"],
                    key=lambda r: (r.get("created_at", ""), r.get("run_id", "")),
                )
            run = workspace.get_run(record.get("run_id") or record["id"])
        state_dir = (
            args.state_dir
            or (args.debug_dir / "reviews" if workspace else DEFAULT_STATE)
        ).resolve()
        data = load_review_data(run)
        store = ReviewStore(state_dir, run_fingerprint(run))
        server = ReviewServer(
            ("127.0.0.1", args.port or (8767 if workspace else 8765)),
            data,
            store,
            workspace=workspace,
            debug_state_dir=state_dir,
        )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Cannot start the review site: {exc}\n")
    print(f"Merge Review: http://127.0.0.1:{server.server_port}", flush=True)
    print(f"Read-only results: {run}", flush=True)
    print(f"Independent review notes: {store.path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
