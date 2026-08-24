"""Controller-owned OpenAI Responses route for keyless DSH containers."""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

from proteus.safety.live import LiveCallProvenance, LiveModelChannel, LiveToolCall


@dataclass(frozen=True)
class DshBridgeRecord:
    """One controller-observed DSH request and normalized live response."""

    request_id: str
    model: str
    model_input: str | Sequence[Mapping[str, object]]
    instructions: str
    tools: tuple[Mapping[str, object], ...]
    tool_calls: tuple[LiveToolCall, ...]
    provenance: LiveCallProvenance


def _response_item(call: LiveToolCall, index: int) -> dict[str, object]:
    return {
        "id": f"fc_{index}",
        "type": "function_call",
        "status": "completed",
        "call_id": call.call_id,
        "name": call.name,
        "arguments": json.dumps(dict(call.arguments), separators=(",", ":")),
    }


def _response_body(response) -> dict[str, object]:
    output: list[dict[str, object]] = []
    if response.output_text:
        output.append(
            {
                "id": "msg_0",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": response.output_text,
                        "annotations": [],
                    }
                ],
            }
        )
    output.extend(_response_item(call, index) for index, call in enumerate(response.tool_calls))
    return {
        "id": response.response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": response.model,
        "output": output,
        "output_text": response.output_text,
        "parallel_tool_calls": True,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }


def _sse_event(name: str, payload: Mapping[str, object]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {data}\n\n".encode()


def _stream_body(body: Mapping[str, object]) -> bytes:
    response_id = str(body["id"])
    model = str(body["model"])
    output = cast(list[dict[str, object]], body["output"])
    empty = {**body, "status": "in_progress", "output": [], "output_text": ""}
    chunks = [
        _sse_event("response.created", {"type": "response.created", "response": empty}),
        _sse_event(
            "response.in_progress",
            {"type": "response.in_progress", "response": empty},
        ),
    ]
    for index, item in enumerate(output):
        item_id = str(item["id"])
        pending = {**item, "status": "in_progress"}
        if item["type"] == "message":
            pending["content"] = []
        elif item["type"] == "function_call":
            pending["arguments"] = ""
        chunks.append(
            _sse_event(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "response_id": response_id,
                    "output_index": index,
                    "item": pending,
                },
            )
        )
        if item["type"] == "message":
            content = cast(list[dict[str, object]], item["content"])[0]
            text = str(content["text"])
            chunks.extend(
                (
                    _sse_event(
                        "response.content_part.added",
                        {
                            "type": "response.content_part.added",
                            "response_id": response_id,
                            "item_id": item_id,
                            "output_index": index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        },
                    ),
                    _sse_event(
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "response_id": response_id,
                            "item_id": item_id,
                            "output_index": index,
                            "content_index": 0,
                            "delta": text,
                        },
                    ),
                    _sse_event(
                        "response.output_text.done",
                        {
                            "type": "response.output_text.done",
                            "response_id": response_id,
                            "item_id": item_id,
                            "output_index": index,
                            "content_index": 0,
                            "text": text,
                        },
                    ),
                    _sse_event(
                        "response.content_part.done",
                        {
                            "type": "response.content_part.done",
                            "response_id": response_id,
                            "item_id": item_id,
                            "output_index": index,
                            "content_index": 0,
                            "part": content,
                        },
                    ),
                )
            )
        else:
            arguments = str(item["arguments"])
            chunks.extend(
                (
                    _sse_event(
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "response_id": response_id,
                            "item_id": item_id,
                            "output_index": index,
                            "delta": arguments,
                        },
                    ),
                    _sse_event(
                        "response.function_call_arguments.done",
                        {
                            "type": "response.function_call_arguments.done",
                            "response_id": response_id,
                            "item_id": item_id,
                            "output_index": index,
                            "arguments": arguments,
                        },
                    ),
                )
            )
        chunks.append(
            _sse_event(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "response_id": response_id,
                    "output_index": index,
                    "item": item,
                },
            )
        )
    completed = {**body, "model": model}
    chunks.append(
        _sse_event(
            "response.completed",
            {"type": "response.completed", "response": completed},
        )
    )
    chunks.append(b"data: [DONE]\n\n")
    return b"".join(chunks)


class DshModelBridge:
    """Expose one bounded ``LiveModelChannel`` as a local Responses endpoint."""

    def __init__(
        self,
        channel: LiveModelChannel,
        *,
        route_key: str | None = None,
        bind_host: str = "127.0.0.1",
        container_host: str = "host.docker.internal",
    ) -> None:
        if not isinstance(channel.model, str) or not channel.model.strip():
            raise ValueError("DSH bridge requires an explicit live model")
        self.channel = channel
        self.route_key = route_key or f"route-{secrets.token_urlsafe(24)}"
        self.bind_host = bind_host
        self.container_host = container_host
        self.records: tuple[DshBridgeRecord, ...] = ()
        self._records: list[DshBridgeRecord] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def container_base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("DSH model bridge is not running")
        return f"http://{self.container_host}:{self._server.server_port}/v1"

    def __enter__(self) -> DshModelBridge:  # noqa: PYI034 - Python 3.10 has no typing.Self
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                del format, args

            def _json(self, status: int, payload: Mapping[str, object]) -> None:
                encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self) -> None:
                if self.path not in {"/responses", "/v1/responses"}:
                    self._json(404, {"error": {"message": "unknown route"}})
                    return
                supplied = self.headers.get("Authorization", "")
                expected = f"Bearer {bridge.route_key}"
                if not hmac.compare_digest(supplied, expected):
                    self._json(401, {"error": {"message": "invalid route credential"}})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    response, body = bridge._respond(payload)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    self._json(400, {"error": {"message": str(exc)}})
                    return
                except Exception as exc:  # noqa: BLE001 - bounded controller error
                    self._json(502, {"error": {"message": str(exc)}})
                    return
                if payload.get("stream") is True:
                    encoded = _stream_body(body)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                del response
                self._json(200, body)

        self._server = ThreadingHTTPServer((self.bind_host, 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="proteus-dsh-model-bridge",
            daemon=True,
        )
        self._thread.start()
        return self

    def _respond(self, payload: object):
        if not isinstance(payload, dict):
            raise TypeError("DSH Responses request must be an object")
        model = payload.get("model")
        if model != self.channel.model:
            raise ValueError("DSH Responses request model does not match the configured model")
        model_input = payload.get("input", "")
        if not isinstance(model_input, (str, list)):
            raise TypeError("DSH Responses input must be text or a message list")
        if isinstance(model_input, list) and not all(isinstance(item, dict) for item in model_input):
            raise TypeError("DSH Responses message list must contain objects")
        instructions = payload.get("instructions", "")
        tools = payload.get("tools", [])
        if not isinstance(instructions, str):
            raise TypeError("DSH Responses instructions must be text")
        if not isinstance(tools, list) or not all(isinstance(item, dict) for item in tools):
            raise TypeError("DSH Responses tools must be an object list")
        response = self.channel.respond(
            input=cast(str | list[dict[str, object]], model_input),
            instructions=instructions,
            tools=cast(list[dict[str, object]], tools),
        )
        provenance = response.provenance
        if (
            provenance.configured_model != self.channel.model
            or provenance.response_model != self.channel.model
            or response.model != self.channel.model
        ):
            raise ValueError("DSH bridge received mismatched model provenance")
        with self._lock:
            number = len(self._records) + 1
            self._records.append(
                DshBridgeRecord(
                    request_id=f"dsh-request-{number}",
                    model=self.channel.model,
                    model_input=cast(str | list[dict[str, object]], model_input),
                    instructions=instructions,
                    tools=tuple(cast(list[dict[str, object]], tools)),
                    tool_calls=response.tool_calls,
                    provenance=provenance,
                )
            )
            self.records = tuple(self._records)
        return response, _response_body(response)

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()
