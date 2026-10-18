"""Local authenticated transport; handlers admit work, the method thread evaluates."""

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import threading

from proteus.core.contracts import require
from proteus.core.recovery import RecoveryRequired
from proteus.core.serialization import ContractError, canonical_json, content_hash, read_json
from proteus.gateway.records import GatewaySubmission
from proteus.workers.runtime import lease, write_json


class GatewayHost:
    def __init__(self, gateway):
        self.gateway = gateway
        self.resources = ExitStack()
        self.server = self.thread = None

    def __enter__(self):
        gateway = self.gateway
        store = gateway.store
        ownership = self.resources.enter_context(lease(gateway.root / "host.lock", blocking=False))
        if ownership is None:
            self.resources.close()
            raise RecoveryRequired("evaluation gateway is already served by another controller")
        try:
            endpoint_path = gateway.root / "endpoint.json"
            port = 0
            if endpoint_path.exists():
                endpoint = read_json(endpoint_path.read_text())
                require(endpoint["identity_hash"] == content_hash(gateway.identity)
                        and endpoint["host"] == "127.0.0.1" and type(endpoint["port"]) is int
                        and 0 < endpoint["port"] < 65536, "gateway endpoint identity differs")
                port = endpoint["port"]
            else:
                require(not store.has_requests() and not any((gateway.services.root / "private/workers"
                    / content_hash(worker).split(":")[1] / "head.json").exists() for worker in gateway.spec.members),
                    "gateway endpoint is missing after worker activity; refusing to replace its address")

            class Handler(BaseHTTPRequestHandler):
                def setup(self):
                    super().setup()
                    self.connection.settimeout(5)

                def log_message(self, *_):
                    pass  # Never log credentials, request bodies or evaluation feedback.

                def reply(self, status, body):
                    encoded = (canonical_json(body) + "\n").encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(encoded)

                def handle_request(self, submit):
                    authorization = self.headers.get("Authorization", "")
                    if not authorization.startswith("Bearer "):
                        self.reply(401, {"error": "authentication_required"})
                        return
                    try:
                        worker = store.authenticate(authorization[7:])
                    except ContractError:
                        self.reply(401, {"error": "invalid_credential"})
                        return
                    try:
                        if submit:
                            if self.path != "/v1/submissions":
                                self.reply(404, {"error": "unknown_route"})
                                return
                            size = int(self.headers.get("Content-Length", "-1"))
                            require(0 < size <= gateway.spec.max_request_bytes,
                                    "request exceeds configured size limits")
                            require(self.headers.get("Content-Type", "").split(";")[0] == "application/json",
                                    "gateway requests must be JSON")
                            body = self.rfile.read(size)
                            require(len(body) == size, "incomplete gateway request")
                            submission = GatewaySubmission.from_json(body.decode())
                            ticket = gateway.submit(worker, submission)
                            self.reply(202, {"ticket_id": ticket.id(), "ticket": ticket.to_dict()})
                        else:
                            match = re.fullmatch(r"/v1/submissions/([0-9a-f]{64})", self.path)
                            if match is None:
                                self.reply(404, {"error": "unknown_route"})
                                return
                            self.reply(200, gateway.response(worker, match[1]))
                    except (ContractError, ValueError, UnicodeError):
                        self.reply(400, {"error": "invalid_or_unavailable_submission"})
                    except Exception:
                        self.reply(503, {"error": "gateway_operation_unavailable"})

                def do_POST(self):
                    self.handle_request(True)

                def do_GET(self):
                    self.handle_request(False)

            class Server(ThreadingHTTPServer):
                allow_reuse_address = True
                daemon_threads = False
                block_on_close = True

            self.server = Server(("127.0.0.1", port), Handler)
            write_json(endpoint_path, {"identity_hash": content_hash(gateway.identity),
                "host": "127.0.0.1", "port": self.server.server_address[1]})
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.025}, daemon=True)
            self.thread.start()
            return self
        except BaseException:
            if self.server is not None:
                self.server.server_close()
            self.resources.close()
            raise

    def __exit__(self, *_):
        try:
            if self.server is not None:
                self.server.shutdown()
                self.server.server_close()  # drain admitted requests before terminal validation
                self.thread.join()
        finally:
            self.resources.close()

