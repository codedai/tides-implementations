"""Local-only Experiment 05 launcher; delegates all processing to pipeline.py."""

from __future__ import annotations

import copy
import hashlib
import importlib
import io
import json
import re
import threading
import uuid
import zipfile
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit

from .assignment import MODES
from .config import DEFAULT_CONFIG, ROOT
from .io import json_safe

STATIC = Path(__file__).with_name("static05")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
MAX_BODY_BYTES = 16 * 1024


class WebError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def _stamp():
    return datetime.now(UTC).isoformat()


def _strict_date(value):
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError as exc:
        raise WebError(400, "INVALID_DATE", "service_date must be YYYY-MM-DD") from exc
    return value


def _no_symlinks(path, root):
    if not path.is_relative_to(root):
        raise WebError(404, "INVALID_PATH", "Path is outside the output directory")
    cursor = path
    while True:
        if cursor.is_symlink():
            raise WebError(404, "INVALID_PATH", "Symlinks are not downloadable")
        if cursor == root:
            return path
        cursor = cursor.parent


def _relative(value):
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise WebError(400, "INVALID_PATH", "Invalid relative output filename")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or value in {".", ".."}
    ):
        raise WebError(
            400, "INVALID_PATH", "Output filename must be a canonical relative path"
        )
    return path


class Workbench:
    def __init__(self, root, config_path, pipeline_module=None):
        self.root = Path(root).resolve()
        self.config_path = Path(config_path).resolve()
        self.runs_root = self.root / "outputs/experiment_05/runs"
        self.pipeline = pipeline_module or importlib.import_module(
            "tools.experiment05.pipeline"
        )
        self.jobs, self.active_job = {}, None
        self.lock = threading.RLock()

    def availability(self):
        return self.pipeline.availability(root=self.root, config_path=self.config_path)

    def run_path(self, run_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise WebError(400, "INVALID_RUN_ID", "Invalid run identifier")
        return _no_symlinks(self.runs_root / run_id, self.root)

    def manifest(self, run_id):
        path = _no_symlinks(self.run_path(run_id) / "run_manifest.json", self.root)
        if not path.is_file():
            raise WebError(404, "RUN_NOT_FOUND", "Run manifest is unavailable")
        value = json.loads(path.read_bytes())
        if (
            not isinstance(value, dict)
            or value.get("run_id") != run_id
            or value.get("schema_version", value.get("schema")) != "experiment05-run-v1"
        ):
            raise WebError(
                409, "INVALID_MANIFEST", "Run manifest identity/schema is invalid"
            )
        return value

    def verified_file(self, run_id, name, manifest=None):
        relative = _relative(name)
        manifest = manifest or self.manifest(run_id)
        pins = manifest.get("output_sha256", {})
        if not isinstance(pins, dict) or name not in pins:
            raise WebError(
                404, "OUTPUT_UNAVAILABLE", "Output is not published in this run"
            )
        digest = pins[name]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise WebError(409, "INVALID_MANIFEST", "Output hash is invalid")
        path = _no_symlinks(self.run_path(run_id).joinpath(*relative.parts), self.root)
        if not path.is_file():
            raise WebError(
                404, "OUTPUT_UNAVAILABLE", "Published output file is missing"
            )
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise WebError(
                409, "OUTPUT_HASH_CHANGED", "Output hash differs from its published pin"
            )
        return payload

    def summary(self, run_id):
        manifest = self.manifest(run_id)
        summary = None
        if "summary.json" in manifest.get("output_sha256", {}):
            summary = json.loads(self.verified_file(run_id, "summary.json", manifest))
        downloads = []
        complete = manifest.get("status") == "complete"
        if (
            complete
            and isinstance(summary, dict)
            and summary.get("validation_ok") is True
        ):
            for name in sorted(manifest.get("output_sha256", {})):
                _relative(name)
                if name.endswith((".csv", ".parquet", ".json")):
                    downloads.append({"file": name, "format": name.rsplit(".", 1)[1]})
        return {
            "run_id": run_id,
            "status": manifest.get("status"),
            "run_manifest": manifest,
            "summary": summary,
            "downloads": downloads,
        }

    def list_runs(self):
        _no_symlinks(self.runs_root, self.root)
        if not self.runs_root.exists():
            return {"runs": []}
        rows = []
        for path in sorted(
            self.runs_root.iterdir(), key=lambda path: path.name, reverse=True
        ):
            if (
                not path.is_dir()
                or path.is_symlink()
                or not RUN_ID.fullmatch(path.name)
            ):
                continue
            try:
                manifest = self.manifest(path.name)
            except (WebError, ValueError, OSError):
                continue
            rows.append(
                {
                    "run_id": path.name,
                    "status": manifest.get("status"),
                    "service_date": manifest.get("service_date"),
                    "route_id": manifest.get("route_id"),
                    "match_modes": manifest.get("match_modes", []),
                }
            )
        return {"runs": rows}

    def download(self, run_id, name=None, mode=None):
        detail = self.summary(run_id)
        if (
            detail["status"] != "complete"
            or (detail["summary"] or {}).get("validation_ok") is not True
        ):
            raise WebError(
                409,
                "RUN_NOT_PUBLISHED",
                "Only completed, validated runs are downloadable",
            )
        manifest = detail["run_manifest"]
        if mode is not None:
            if mode not in MODES:
                raise WebError(400, "INVALID_MODE", "Unknown decision mode")
            if (
                detail["summary"]
                .get("modes", {})
                .get(mode, {})
                .get("tides_validation_passed")
                is not True
            ):
                raise WebError(
                    404, "OUTPUT_UNAVAILABLE", "This mode has no validated TIDES export"
                )
            names = sorted(
                name
                for name in manifest.get("output_sha256", {})
                if name.startswith(f"tides/{mode}/")
            )
            if f"tides/{mode}/datapackage.json" not in names:
                raise WebError(
                    404,
                    "OUTPUT_UNAVAILABLE",
                    "This mode has no published TIDES package",
                )
            buffer = io.BytesIO()
            with zipfile.ZipFile(
                buffer, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for relative in names:
                    payload = self.verified_file(run_id, relative, manifest)
                    entry = zipfile.ZipInfo(relative.removeprefix(f"tides/{mode}/"))
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(entry, payload)
            return buffer.getvalue(), "application/zip", f"{run_id}-{mode}-tides.zip"
        allowed = {row["file"] for row in detail["downloads"]}
        if name not in allowed:
            _relative(name)
            raise WebError(
                404, "OUTPUT_UNAVAILABLE", "Requested output is not downloadable"
            )
        mime = {
            ".json": "application/json",
            ".csv": "text/csv; charset=utf-8",
            ".parquet": "application/vnd.apache.parquet",
        }.get(Path(name).suffix, "application/octet-stream")
        return self.verified_file(run_id, name, manifest), mime, Path(name).name

    def submit(self, body):
        if not isinstance(body, dict):
            raise WebError(400, "INVALID_REQUEST", "Run request must be a JSON object")
        allowed = {
            "service_date",
            "date",
            "route_id",
            "route",
            "match_mode",
            "mode",
            "match_modes",
            "force",
        }
        if set(body) - allowed:
            raise WebError(
                400,
                "INVALID_REQUEST",
                "Unknown run parameters; filesystem paths are not accepted",
            )
        normalized = dict(body)
        for alias, canonical in (
            ("date", "service_date"),
            ("route", "route_id"),
            ("mode", "match_mode"),
        ):
            if alias in normalized:
                if canonical in normalized:
                    raise WebError(
                        400, "INVALID_REQUEST", "Conflicting parameter aliases"
                    )
                normalized[canonical] = normalized.pop(alias)
        service_date = _strict_date(normalized.get("service_date"))
        route = str(normalized.get("route_id", "29"))
        if route != "29":
            raise WebError(
                400, "INVALID_ROUTE", "This workbench is limited to route 29"
            )
        if "match_mode" in normalized and "match_modes" in normalized:
            raise WebError(
                400, "INVALID_MODE", "Choose match_mode or match_modes, not both"
            )
        mode = normalized.get("match_mode", "all")
        modes = normalized.get("match_modes", list(MODES) if mode == "all" else [mode])
        if (
            not isinstance(modes, list)
            or not modes
            or any(item not in MODES for item in modes)
            or len(set(modes)) != len(modes)
        ):
            raise WebError(
                400, "INVALID_MODE", "Select global, early, middle, late or all"
            )
        modes = [item for item in MODES if item in modes]
        force = normalized.get("force", False)
        if not isinstance(force, bool):
            raise WebError(400, "INVALID_FORCE", "force must be a JSON boolean")
        available = self.availability()
        day = next(
            (
                row
                for row in available.get("dates", [])
                if row.get("service_date") == service_date
            ),
            None,
        )
        if day is None or day.get("available") is not True:
            reason = (day or {}).get(
                "reason"
            ) or "Raw observations or an active GTFS feed are unavailable for this date"
            raise WebError(422, "INPUT_UNAVAILABLE", str(reason))
        request = {
            "service_date": service_date,
            "route_id": route,
            "match_modes": modes,
            "force": force,
        }
        with self.lock:
            if self.active_job is not None:
                raise WebError(
                    409,
                    "JOB_ALREADY_RUNNING",
                    f"A run is already in progress: {self.active_job}",
                )
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {
                "job_id": job_id,
                "state": "queued",
                "created_at": _stamp(),
                "request": request,
                "progress": [],
                "progress_events_truncated": 0,
                "result": None,
                "error": None,
            }
            self.active_job = job_id
            thread = threading.Thread(
                target=self._execute, args=(job_id, request), daemon=True
            )
            thread.start()
            return copy.deepcopy(self.jobs[job_id])

    def _execute(self, job_id, request):
        def progress(event):
            with self.lock:
                job = self.jobs[job_id]
                job["progress"].append(json_safe({"received_at": _stamp(), **event}))
                if len(job["progress"]) > 250:
                    job["progress"].pop(0)
                    job["progress_events_truncated"] += 1

        with self.lock:
            self.jobs[job_id].update(state="running", started_at=_stamp())
        try:
            result = self.pipeline.run_pipeline(
                **request,
                root=self.root,
                config_path=self.config_path,
                progress=progress,
            )
            run_path = Path(result["run_dir"])
            if not run_path.is_absolute():
                run_path = self.root / run_path
            if (
                run_path.parent != self.runs_root
                or self.run_path(run_path.name) != run_path
            ):
                raise ValueError(
                    "Pipeline returned a run outside the configured output directory"
                )
            detail = self.summary(run_path.name)
            if (
                detail["status"] != "complete"
                or (detail["summary"] or {}).get("validation_ok") is not True
            ):
                raise ValueError("Pipeline did not publish a completed, validated run")
            with self.lock:
                self.jobs[job_id].update(
                    state="complete",
                    finished_at=_stamp(),
                    result={
                        "run_id": run_path.name,
                        "cached": result.get("cached") is True,
                        "summary": detail["summary"],
                    },
                )
        except Exception as exc:
            with self.lock:
                self.jobs[job_id].update(
                    state="failed",
                    finished_at=_stamp(),
                    error=f"{type(exc).__name__}: {exc}",
                )
        finally:
            with self.lock:
                self.active_job = None

    def job(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise WebError(404, "JOB_NOT_FOUND", "Unknown background job")
            return copy.deepcopy(self.jobs[job_id])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _origin(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            raise WebError(
                400, "INVALID_HOST", "A single local Host header is required"
            )
        try:
            host = urlsplit("//" + hosts[0])
            local = (
                host.hostname in {"127.0.0.1", "localhost"}
                and host.port == self.server.server_port
                and not (host.username or host.password or host.path)
            )
        except ValueError:
            local = False
        if not local:
            raise WebError(
                403, "INVALID_HOST", "Only this localhost workbench is allowed"
            )
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1:
            raise WebError(403, "INVALID_ORIGIN", "Multiple origins are not allowed")
        origin = origins[0] if origins else None
        if origin:
            try:
                parsed = urlsplit(origin)
                valid = (
                    parsed.scheme == "http"
                    and parsed.hostname in {"127.0.0.1", "localhost"}
                    and parsed.port == self.server.server_port
                    and not (
                        parsed.username
                        or parsed.password
                        or parsed.path
                        or parsed.query
                        or parsed.fragment
                    )
                )
            except ValueError:
                valid = False
            if not valid:
                raise WebError(
                    403, "INVALID_ORIGIN", "Cross-origin requests are not allowed"
                )
        if (
            self.command == "POST"
            and self.headers.get("Sec-Fetch-Site") == "cross-site"
        ):
            raise WebError(
                403, "INVALID_ORIGIN", "Cross-site run creation is not allowed"
            )

    def _send(self, status, payload, mime="application/json", filename=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.send_header("Connection", "close")
        if filename is not None:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(payload)

    def _json(self, status, payload):
        self._send(
            status,
            json.dumps(
                json_safe(payload), ensure_ascii=False, allow_nan=False
            ).encode(),
        )

    def _dispatch(self):
        self._origin()
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            raise WebError(
                400, "INVALID_PATH", "Only local relative request targets are accepted"
            )
        path = unquote(parsed.path)
        if "\\" in path or "\x00" in path or ".." in path.split("/"):
            raise WebError(400, "INVALID_PATH", "Invalid request path")
        query = parse_qs(parsed.query, keep_blank_values=True)
        app = self.server.workbench
        if self.command == "POST":
            if path != "/api/runs" or query:
                raise WebError(404, "NOT_FOUND", "Unknown run endpoint")
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1:
                raise WebError(
                    400, "INVALID_BODY", "A single Content-Length is required"
                )
            try:
                length = int(lengths[0])
            except ValueError as exc:
                raise WebError(400, "INVALID_BODY", "Invalid Content-Length") from exc
            if not 0 < length <= MAX_BODY_BYTES:
                raise WebError(
                    413,
                    "BODY_TOO_LARGE",
                    "JSON request must be between 1 and 16384 bytes",
                )
            if (
                self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise WebError(
                    415,
                    "INVALID_CONTENT_TYPE",
                    "Run creation requires application/json",
                )
            self.connection.settimeout(15)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise WebError(400, "INVALID_BODY", "Incomplete request body")
            try:
                body = json.loads(
                    raw,
                    parse_constant=lambda _value: (_ for _ in ()).throw(
                        ValueError("Nonfinite JSON")
                    ),
                )
            except (ValueError, UnicodeError) as exc:
                raise WebError(400, "INVALID_JSON", "Malformed JSON request") from exc
            return self._json(202, app.submit(body))
        if self.command != "GET":
            raise WebError(
                405, "METHOD_NOT_ALLOWED", "Only GET and JSON POST are supported"
            )
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "application/javascript"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        if path in assets and not query:
            name, mime = assets[path]
            return self._send(200, (STATIC / name).read_bytes(), mime)
        if path == "/api/health" and not query:
            return self._json(200, {"ok": True, "local_only": True, "experiment": "05"})
        if path == "/api/availability" and not query:
            return self._json(200, app.availability())
        if path == "/api/runs" and not query:
            return self._json(200, app.list_runs())
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})", path)
        if match and not query:
            return self._json(200, app.job(match[1]))
        match = re.fullmatch(r"/api/runs/([A-Za-z0-9_-]+)/summary", path)
        if match and not query:
            return self._json(200, app.summary(match[1]))
        match = re.fullmatch(r"/api/runs/([A-Za-z0-9_-]+)/download", path)
        if (
            match
            and set(query) in ({"file"}, {"mode"})
            and all(len(values) == 1 for values in query.values())
        ):
            payload, mime, filename = app.download(
                match[1],
                name=query.get("file", [None])[0],
                mode=query.get("mode", [None])[0],
            )
            return self._send(200, payload, mime, filename)
        raise WebError(404, "NOT_FOUND", "Endpoint or output is unavailable")

    def _handle(self):
        try:
            self._dispatch()
        except WebError as exc:
            self._json(exc.status, {"error": exc.code, "message": str(exc)})
        except (ValueError, OSError) as exc:
            self._json(500, {"error": "WORKBENCH_ERROR", "message": str(exc)})

    do_GET = do_POST = do_OPTIONS = do_DELETE = do_PUT = do_PATCH = _handle


def create_server(
    *,
    host="127.0.0.1",
    port=0,
    root=ROOT,
    config_path=DEFAULT_CONFIG,
    pipeline_module=None,
):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Experiment 05 web may bind only to localhost")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer between zero and 65535")
    workbench = Workbench(root, config_path, pipeline_module)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.workbench = workbench
    return server


def serve(*, root=ROOT, config_path=DEFAULT_CONFIG, port=8767, host="127.0.0.1"):
    server = create_server(host=host, port=port, root=root, config_path=config_path)
    print(f"Experiment 05: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
