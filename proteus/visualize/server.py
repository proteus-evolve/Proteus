"""Loopback-only authenticated workspace with an opt-in, CLI-backed launch API.

The viewer serves a fixed asset allowlist, not run files. Host/Origin checks and a
session token prevent cross-origin reads, DNS rebinding, and drive-by paid launches.
This is a trusted local operator UI, not a multi-user remote orchestration service.
"""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit

from proteus.visualize import config
from proteus.visualize.data import Workspace, clean, confined

ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/model.js": ("model.js", "text/javascript; charset=utf-8")}


class Launches:
    def __init__(self, destination, *, allowed_adapters=()):
        self.destination = Path(destination).resolve()
        self.allowed_adapters = tuple(allowed_adapters)
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, draft):
        c = config.validate(draft, allowed_adapters=self.allowed_adapters)
        with self.lock:
            if any(job["process"].poll() is None for job in self.jobs.values()):
                raise ValueError("a viewer-launched sweep is already running; wait for it to finish")
            identity = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4)
            output = confined(self.destination, "visualize-" + identity)
            output.mkdir(parents=True, exist_ok=False)
            # Private raw CLI logs are never exposed through the HTTP API.
            log_path = output / "controller.log"
            with log_path.open("xb") as log:
                log_path.chmod(0o600)
                process = subprocess.Popen(config.arguments(c, output), stdout=log, stderr=log)
            self.jobs[identity] = {"process": process, "output": output.name}
            return {"id": identity, "output": output.name, "status": "running"}

    def status(self):
        with self.lock:
            return [{"id": key, "output": job["output"],
                     "status": "running" if job["process"].poll() is None else
                     "completed" if job["process"].returncode == 0 else "failed",
                     "exitCode": job["process"].returncode} for key, job in self.jobs.items()]

    def controller_for(self, sweep):
        with self.lock:
            for identity, job in self.jobs.items():
                if self.destination / job["output"] == sweep:
                    code = job["process"].poll()
                    return {"id": identity, "pid": job["process"].pid,
                            "running": code is None, "exitCode": code}
        return None


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root, port=8301, *, allow_run_control=False, launch_out=None,
                 allowed_adapters=()):
        self.workspace = Workspace(root)
        self.control = allow_run_control
        self.token = secrets.token_urlsafe(32)
        self.launches = Launches(launch_out or root, allowed_adapters=allowed_adapters)
        self.asset_root = Path(__file__).parent / "static"
        self.evidence_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)
        self.port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.origins = {f"http://{host}" for host in self.hosts}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Request paths/configuration can contain sensitive strings.

    def reply(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(clean(body), allow_nan=False).encode("utf8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def permitted(self, *, api=False):
        if self.headers.get("Host") not in self.server.hosts:
            self.reply(403, {"error": "invalid local Host"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.origins:
            self.reply(403, {"error": "cross-origin access is not allowed"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.reply(403, {"error": "cross-site access is not allowed"})
            return False
        token = self.headers.get("Authorization", "")
        if api and not secrets.compare_digest(token, "Bearer " + self.server.token):
            self.reply(401, {"error": "local session token required"})
            return False
        return True

    def do_GET(self):
        url = urlsplit(self.path)
        api = url.path.startswith("/api/") and url.path != "/api/session"
        if not self.permitted(api=api):
            return
        query = parse_qs(url.query)
        identity = query.get("id", [""])[0]
        try:
            if url.path in ASSETS:
                asset, kind = ASSETS[url.path]
                self.reply(200, (self.server.asset_root / asset).read_bytes(), kind)
            elif url.path == "/api/session":
                self.reply(200, {"token": self.server.token, "runControl": self.server.control,
                                 "defaultConfig": config.DEFAULT,
                                 "allowedAdapters": [*config.BUILTINS, *self.server.launches.allowed_adapters]})
            elif url.path == "/api/workspace":
                with self.server.evidence_lock:
                    self.reply(200, self.server.workspace.summary())
            elif url.path in ("/api/run", "/api/export", "/api/population"):
                with self.server.evidence_lock:
                    if url.path == "/api/run":
                        result = self.server.workspace.load(identity)
                        entries, _ = self.server.workspace.catalog()
                        sweep = next((row["sweep"] for row in entries if row["id"] == identity), None)
                        controller = self.server.launches.controller_for(sweep)
                        if controller is not None:
                            result["controller"] = controller
                            result["liveProcessConfirmed"] = controller["running"]
                            result["statusNote"] = "Viewer-owned CLI process state is confirmed; episode completion still comes from committed snapshots."
                            if controller["running"] and result["status"] == "incomplete":
                                result["status"] = "running"
                    elif url.path == "/api/population":
                        result = self.server.workspace.population(identity)
                    else:
                        result = self.server.workspace.export(identity, include_hidden=query.get("hidden") == ["1"])
                self.reply(200, result)
            elif url.path == "/api/status":
                self.reply(200, {"jobs": self.server.launches.status()})
            else:
                self.reply(404, {"error": "not found"})
        except KeyError:
            self.reply(404, {"error": "unknown run"})
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
            self.reply(422, {"error": str(exc)})

    def do_POST(self):
        if not self.permitted(api=True):
            return
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("application/json required")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 128 * 1024:
                raise ValueError("configuration body must be between 1 byte and 128 KiB")
            self.connection.settimeout(5)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete configuration")
            draft = json.loads(raw)
            path = urlsplit(self.path).path
            if path == "/api/validate":
                c = config.validate(draft, allowed_adapters=self.server.launches.allowed_adapters)
                self.reply(200, {"valid": True, "command": config.preview(c),
                                 "note": "Configuration checked; provider/environment preflight has not run."})
            elif path == "/api/runs":
                if not self.server.control:
                    self.reply(403, {"error": "run control is disabled; restart with --allow-run-control"})
                    return
                self.reply(201, self.server.launches.start(draft))
            else:
                self.reply(404, {"error": "not found"})
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            self.reply(422, {"error": str(exc)})


def serve(root, port=8301, **kwargs):
    with ViewerServer(root, port, **kwargs) as server:
        print(f"Proteus Visualize: http://127.0.0.1:{server.port}", flush=True)
        print("Viewing never calls a model. " + ("Explicit run control enabled." if server.control
              else "Read-only; run control disabled."), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
