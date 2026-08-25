"""Keyless controller bridge from native OpenAI clients to a live model channel."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from proteus.safety.live import (
    LiveCallProvenance,
    LiveModelChannel,
    LiveModelResponse,
    LiveProtocolError,
)


@dataclass(frozen=True)
class BridgeCallRecord:
    """Controller-owned ownership record for one native Responses request."""

    sequence: int
    requested_model: str
    returned_model: str
    response_id: str
    provenance: LiveCallProvenance
    tool_call_ids: tuple[str, ...]
    tool_result_call_ids: tuple[str, ...]
    linked_tool_result_call_ids: tuple[str, ...]
    request_ref: str
    response_ref: str


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LiveProtocolError(f"{label} must be non-empty text")
    return value


def _sequence_of_mappings(value: object, label: str) -> list[Mapping[str, object]]:
    if value is None:
        return []
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not all(isinstance(item, Mapping) for item in value)
    ):
        raise LiveProtocolError(f"{label} must be a sequence of mappings")
    return list(value)


def _tool_result_ids(input_value: object) -> tuple[str, ...]:
    if not isinstance(input_value, Sequence) or isinstance(input_value, (str, bytes)):
        return ()
    result: list[str] = []
    for item in input_value:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") not in {"function_call_output", "custom_tool_call_output"}:
            continue
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id:
            result.append(call_id)
    return tuple(result)


def _safe_item_id(prefix: str, response_id: str, index: int) -> str:
    body = re.sub(r"[^A-Za-z0-9_-]", "_", response_id)
    return f"{prefix}_{body}_{index}"[:64]


def _response_items(response: LiveModelResponse) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    if response.output_text:
        items.append(
            {
                "type": "message",
                "id": _safe_item_id("msg", response.response_id, len(items)),
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
    for call in response.tool_calls:
        items.append(
            {
                "type": "function_call",
                "id": _safe_item_id("fc", response.response_id, len(items)),
                "status": "completed",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(
                    dict(call.arguments), separators=(",", ":"), ensure_ascii=False
                ),
            }
        )
    return items


def _response_object(
    response: LiveModelResponse, items: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "id": response.response_id,
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": response.model,
        "output": items,
        "error": None,
        "incomplete_details": None,
        "usage": {
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,
        },
    }


def _stream_events(
    response: LiveModelResponse, items: list[dict[str, object]]
) -> list[dict[str, object]]:
    created = _response_object(response, [])
    created["status"] = "in_progress"
    events: list[dict[str, object]] = [
        {"type": "response.created", "sequence_number": 0, "response": created}
    ]
    sequence = 1
    for output_index, item in enumerate(items):
        added = dict(item)
        if item["type"] == "message":
            added["content"] = []
        elif item["type"] == "function_call":
            added["arguments"] = ""
            added["status"] = "in_progress"
        events.append(
            {
                "type": "response.output_item.added",
                "sequence_number": sequence,
                "output_index": output_index,
                "item": added,
            }
        )
        sequence += 1
        if item["type"] == "message":
            content = item["content"]
            assert isinstance(content, list)
            text = str(content[0]["text"])
            events.append(
                {
                    "type": "response.output_text.delta",
                    "sequence_number": sequence,
                    "output_index": output_index,
                    "content_index": 0,
                    "item_id": item["id"],
                    "delta": text,
                    "logprobs": [],
                }
            )
            sequence += 1
        else:
            arguments = str(item["arguments"])
            events.extend(
                [
                    {
                        "type": "response.function_call_arguments.delta",
                        "sequence_number": sequence,
                        "output_index": output_index,
                        "item_id": item["id"],
                        "delta": arguments,
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "sequence_number": sequence + 1,
                        "output_index": output_index,
                        "item_id": item["id"],
                        "arguments": arguments,
                    },
                ]
            )
            sequence += 2
        events.append(
            {
                "type": "response.output_item.done",
                "sequence_number": sequence,
                "output_index": output_index,
                "item": item,
            }
        )
        sequence += 1
    events.append(
        {
            "type": "response.completed",
            "sequence_number": sequence,
            "response": _response_object(response, items),
        }
    )
    return events


class OpenAICompatibleBridge:
    """Serve a contained native client without giving it the provider credential."""

    def __init__(self, *, channel: LiveModelChannel, evidence_root: Path) -> None:
        if not isinstance(channel, LiveModelChannel):
            raise TypeError("bridge channel must implement LiveModelChannel")
        self._channel = channel
        self._evidence_root = Path(evidence_root)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._records: list[BridgeCallRecord] = []
        self._issued_tool_calls: set[str] = set()
        self._lock = threading.Lock()

    @property
    def records(self) -> tuple[BridgeCallRecord, ...]:
        with self._lock:
            return tuple(self._records)

    @property
    def host_base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("bridge is not running")
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    @property
    def container_base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("bridge is not running")
        return f"http://host.docker.internal:{self._server.server_port}/v1"

    def __enter__(self) -> OpenAICompatibleBridge:
        if self._server is not None:
            raise RuntimeError("bridge is already running")
        self._evidence_root.mkdir(parents=True, exist_ok=False)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                owner._handle(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="proteus-live-bridge",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.path.rstrip("/") != "/v1/responses":
            self._write_error(handler, HTTPStatus.NOT_FOUND, "unknown bridge endpoint")
            return
        try:
            raw_length = handler.headers.get("Content-Length", "")
            length = int(raw_length)
            payload = json.loads(handler.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, Mapping):
                raise LiveProtocolError("native Responses request must be a mapping")
            response, items = self._forward(dict(payload))
            if payload.get("stream") is True:
                body = b"".join(
                    b"event: "
                    + str(event["type"]).encode("utf-8")
                    + b"\ndata: "
                    + _json_bytes(event)
                    + b"\n\n"
                    for event in _stream_events(response, items)
                ) + b"data: [DONE]\n\n"
                content_type = "text/event-stream"
            else:
                body = _json_bytes(_response_object(response, items))
                content_type = "application/json"
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(body)
        except Exception as exc:  # noqa: BLE001 - sanitize the native HTTP boundary
            self._write_error(handler, HTTPStatus.BAD_REQUEST, f"{type(exc).__name__}: {exc}")

    def _forward(
        self, payload: dict[str, object]
    ) -> tuple[LiveModelResponse, list[dict[str, object]]]:
        requested_model = _required_text(payload.get("model"), "requested model")
        if requested_model != self._channel.model:
            raise LiveProtocolError("native requested model does not match configured model")
        input_value = payload.get("input", "")
        if not isinstance(input_value, str):
            _sequence_of_mappings(input_value, "native Responses input")
        instructions = payload.get("instructions", "")
        if not isinstance(instructions, str):
            raise LiveProtocolError("native Responses instructions must be text")
        tools = _sequence_of_mappings(payload.get("tools"), "native Responses tools")
        with self._lock:
            sequence = len(self._records) + 1
            request_ref = f"bridge-request-{sequence:03d}.json"
            response_ref = f"bridge-response-{sequence:03d}.json"
            self._write_json(self._evidence_root / request_ref, payload)
            result_ids = _tool_result_ids(input_value)
            linked_ids = tuple(
                call_id for call_id in result_ids if call_id in self._issued_tool_calls
            )
            response = self._channel.respond(
                input=input_value,
                instructions=instructions,
                tools=tools,
            )
            if (
                response.model != requested_model
                or response.provenance.configured_model != requested_model
                or response.provenance.response_model != requested_model
            ):
                raise LiveProtocolError("bridge response model provenance does not match request")
            tool_call_ids = tuple(call.call_id for call in response.tool_calls)
            self._issued_tool_calls.update(tool_call_ids)
            items = _response_items(response)
            record = BridgeCallRecord(
                sequence=sequence,
                requested_model=requested_model,
                returned_model=response.model,
                response_id=response.response_id,
                provenance=response.provenance,
                tool_call_ids=tool_call_ids,
                tool_result_call_ids=result_ids,
                linked_tool_result_call_ids=linked_ids,
                request_ref=request_ref,
                response_ref=response_ref,
            )
            self._records.append(record)
            self._write_json(
                self._evidence_root / response_ref,
                {
                    "configured_model": self._channel.model,
                    "requested_model": requested_model,
                    "returned_model": response.model,
                    "response_id": response.response_id,
                    "provenance": asdict(response.provenance),
                    "output": items,
                    "tool_result_call_ids": list(result_ids),
                    "linked_tool_result_call_ids": list(linked_ids),
                },
            )
        return response, items

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, Any]) -> None:
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_error(
        handler: BaseHTTPRequestHandler, status: HTTPStatus, message: str
    ) -> None:
        body = _json_bytes(
            {"error": {"type": "invalid_request_error", "message": message}}
        )
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return
