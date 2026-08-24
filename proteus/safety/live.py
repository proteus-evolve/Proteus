"""Trusted fixed-live model channel for candidate safety executors."""

from __future__ import annotations

import json
import socket
import threading
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast, runtime_checkable


def _require_text(label: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty")


@dataclass(frozen=True)
class LiveToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_text("live tool call ID", self.call_id)
        _require_text("live tool name", self.name)


@dataclass(frozen=True)
class LiveCallProvenance:
    call_id: str
    response_id: str
    configured_model: str
    response_model: str

    def __post_init__(self) -> None:
        for label, value in (
            ("live call ID", self.call_id),
            ("live response ID", self.response_id),
            ("configured model", self.configured_model),
            ("response model", self.response_model),
        ):
            _require_text(label, value)


@dataclass(frozen=True)
class LiveModelResponse:
    response_id: str
    model: str
    output_text: str
    tool_calls: tuple[LiveToolCall, ...]
    provenance: LiveCallProvenance

    def __post_init__(self) -> None:
        _require_text("live response ID", self.response_id)
        _require_text("live response model", self.model)
        if not isinstance(self.output_text, str):
            raise TypeError("live response output text must be a string")


@runtime_checkable
class LiveModelChannel(Protocol):
    @property
    def model(self) -> str: ...

    def respond(
        self,
        *,
        input: str | Sequence[Mapping[str, object]],
        instructions: str = "",
        tools: Sequence[Mapping[str, object]] = (),
    ) -> LiveModelResponse: ...


@dataclass(frozen=True)
class LiveCellBudget:
    max_calls: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        for label, value in (
            ("maximum live calls", self.max_calls),
            ("maximum output tokens", self.max_output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")


@dataclass(frozen=True)
class LiveModelConfig:
    model: str
    credential_env: str = "OPENAI_API_KEY"
    api_base: str = "https://api.openai.com/v1"
    timeout_seconds: float = 180.0
    budget: LiveCellBudget = field(
        default_factory=lambda: LiveCellBudget(max_calls=4, max_output_tokens=1200)
    )

    def __post_init__(self) -> None:
        _require_text("live model", self.model)
        _require_text("credential environment name", self.credential_env)
        _require_text("Responses API base", self.api_base)
        if self.timeout_seconds <= 0:
            raise ValueError("live model timeout must be positive")
        if not isinstance(self.budget, LiveCellBudget):
            raise TypeError("live model config requires a LiveCellBudget")


class ResponsesTransport(Protocol):
    def create(
        self,
        *,
        api_base: str,
        credential: str,
        timeout_seconds: float,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]: ...


class StdlibResponsesTransport:
    """POST one non-streaming request to the OpenAI Responses endpoint."""

    def create(
        self,
        *,
        api_base: str,
        credential: str,
        timeout_seconds: float,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        request = urllib.request.Request(
            f"{api_base.rstrip('/')}/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not isinstance(body, dict):
            raise TypeError("Responses API returned a non-object payload")
        return body


def _parse_dotenv_value(line: str, name: str) -> str | None:
    candidate = line.strip()
    if not candidate or candidate.startswith("#"):
        return None
    if candidate.startswith("export "):
        candidate = candidate[len("export ") :].lstrip()
    key, separator, raw_value = candidate.partition("=")
    if not separator or key.strip() != name:
        return None
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value.strip()


def load_repository_credential(repository_root: Path, name: str) -> str:
    """Load one credential from the explicitly selected repository-root ``.env``."""
    root = Path(repository_root)
    env_path = root / ".env"
    if not env_path.is_file():
        raise ValueError(f"repository-root credential file is missing: {env_path}")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        value = _parse_dotenv_value(line, name)
        if value is not None:
            if not value:
                raise ValueError(f"repository-root credential {name} is empty")
            return value
    raise ValueError(f"repository-root credential {name} is missing")


def preflight_live_model(config: LiveModelConfig, repository_root: Path) -> None:
    """Validate explicit model, budget, and repository credential without making a call."""
    if not isinstance(config, LiveModelConfig):
        raise TypeError("fixed-live preflight requires a LiveModelConfig")
    load_repository_credential(repository_root, config.credential_env)


def _response_output_text(body: Mapping[str, object]) -> str:
    direct = body.get("output_text")
    if isinstance(direct, str):
        return direct
    text: list[str] = []
    output = body.get("output", ())
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content", ())
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                text.append(cast(str, part["text"]))
    return "".join(text)


def _response_tool_calls(body: Mapping[str, object]) -> tuple[LiveToolCall, ...]:
    calls: list[LiveToolCall] = []
    output = body.get("output", ())
    if not isinstance(output, list):
        return ()
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        raw_arguments = item.get("arguments", "{}")
        if not isinstance(raw_arguments, str):
            raise TypeError("Responses API function arguments must be JSON text")
        arguments = json.loads(raw_arguments)
        if not isinstance(arguments, dict):
            raise TypeError("Responses API function arguments must decode to an object")
        calls.append(
            LiveToolCall(
                call_id=str(item.get("call_id", "")),
                name=str(item.get("name", "")),
                arguments=arguments,
            )
        )
    return tuple(calls)


def _send_message(connection: socket.socket, message: Mapping[str, object]) -> None:
    encoded = json.dumps(message, separators=(",", ":")).encode("utf-8")
    connection.sendall(len(encoded).to_bytes(8, "big") + encoded)


def _receive_exact(connection: socket.socket, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _receive_message(connection: socket.socket) -> Mapping[str, object] | None:
    header = _receive_exact(connection, 8)
    if header is None:
        return None
    payload = _receive_exact(connection, int.from_bytes(header, "big"))
    if payload is None:
        return None
    message = json.loads(payload.decode("utf-8"))
    if not isinstance(message, dict):
        raise TypeError("live channel message must be an object")
    return message


def _response_to_message(response: LiveModelResponse) -> dict[str, object]:
    return {
        "response_id": response.response_id,
        "model": response.model,
        "output_text": response.output_text,
        "tool_calls": [
            {
                "call_id": item.call_id,
                "name": item.name,
                "arguments": dict(item.arguments),
            }
            for item in response.tool_calls
        ],
        "provenance": {
            "call_id": response.provenance.call_id,
            "response_id": response.provenance.response_id,
            "configured_model": response.provenance.configured_model,
            "response_model": response.provenance.response_model,
        },
    }


def _response_from_message(message: Mapping[str, object]) -> LiveModelResponse:
    raw_calls = message.get("tool_calls")
    raw_provenance = message.get("provenance")
    if not isinstance(raw_calls, list) or not isinstance(raw_provenance, dict):
        raise TypeError("live channel returned malformed normalized evidence")
    tool_calls: list[LiveToolCall] = []
    for item in raw_calls:
        if not isinstance(item, dict) or not isinstance(item.get("arguments"), dict):
            raise TypeError("live channel returned malformed normalized tool calls")
        tool_calls.append(
            LiveToolCall(
                call_id=str(item.get("call_id", "")),
                name=str(item.get("name", "")),
                arguments=cast(dict[str, object], item["arguments"]),
            )
        )
    provenance = LiveCallProvenance(
        call_id=str(raw_provenance.get("call_id", "")),
        response_id=str(raw_provenance.get("response_id", "")),
        configured_model=str(raw_provenance.get("configured_model", "")),
        response_model=str(raw_provenance.get("response_model", "")),
    )
    return LiveModelResponse(
        response_id=str(message.get("response_id", "")),
        model=str(message.get("model", "")),
        output_text=str(message.get("output_text", "")),
        tool_calls=tuple(tool_calls),
        provenance=provenance,
    )


class _SocketLiveModelChannel:
    def __init__(
        self,
        *,
        connection: socket.socket,
        model: str,
        timeout_seconds: float,
    ) -> None:
        self._connection = connection
        self._connection.settimeout(timeout_seconds)
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def respond(
        self,
        *,
        input: str | Sequence[Mapping[str, object]],
        instructions: str = "",
        tools: Sequence[Mapping[str, object]] = (),
    ) -> LiveModelResponse:
        _send_message(
            self._connection,
            {
                "input": input,
                "instructions": instructions,
                "tools": list(tools),
            },
        )
        result = _receive_message(self._connection)
        if result is None:
            raise RuntimeError("fixed-live broker closed the channel")
        if result.get("ok") is not True:
            raise RuntimeError(str(result.get("error", "fixed-live broker request failed")))
        response = result.get("response")
        if not isinstance(response, dict):
            raise TypeError("fixed-live broker returned no normalized response")
        return _response_from_message(response)

    def close(self) -> None:
        self._connection.close()

    def __del__(self) -> None:
        self._connection.close()


def _normalized_response(
    *,
    body: Mapping[str, object],
    cell_id: str,
    call_number: int,
    configured_model: str,
) -> LiveModelResponse:
    response_id = body.get("id")
    response_model = body.get("model")
    if not isinstance(response_id, str) or not response_id.strip():
        raise ValueError("Responses API payload has no response ID")
    if not isinstance(response_model, str) or not response_model.strip():
        raise ValueError("Responses API payload has no model provenance")
    provenance = LiveCallProvenance(
        call_id=f"{cell_id}.call-{call_number}",
        response_id=response_id,
        configured_model=configured_model,
        response_model=response_model,
    )
    return LiveModelResponse(
        response_id=response_id,
        model=response_model,
        output_text=_response_output_text(body),
        tool_calls=_response_tool_calls(body),
        provenance=provenance,
    )


def _request_payload(
    request: Mapping[str, object],
    config: LiveModelConfig,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": config.model,
        "input": request.get("input", ""),
        "max_output_tokens": config.budget.max_output_tokens,
        "store": False,
    }
    instructions = request.get("instructions")
    tools = request.get("tools")
    if isinstance(instructions, str) and instructions:
        payload["instructions"] = instructions
    if isinstance(tools, list) and tools:
        payload["tools"] = tools
    return payload


class LiveModelBroker:
    """Trusted owner of credentials, transport, provenance, and per-cell budgets."""

    def __init__(
        self,
        config: LiveModelConfig,
        credential: str,
        *,
        transport: ResponsesTransport | None = None,
    ) -> None:
        if not isinstance(config, LiveModelConfig):
            raise TypeError("live model broker requires a LiveModelConfig")
        _require_text("live model credential", credential)
        self.config = config
        self._credential = credential
        self._transport = transport or StdlibResponsesTransport()

    @classmethod
    def from_repository(
        cls,
        config: LiveModelConfig,
        repository_root: Path,
        *,
        transport: ResponsesTransport | None = None,
    ) -> LiveModelBroker:
        credential = load_repository_credential(repository_root, config.credential_env)
        return cls(config, credential, transport=transport)

    def channel(self, cell_id: str) -> LiveModelChannel:
        _require_text("live cell ID", cell_id)
        executor_connection, broker_connection = socket.socketpair()
        thread = threading.Thread(
            target=self._serve_channel,
            args=(broker_connection, cell_id),
            name=f"proteus-live-{cell_id}",
            daemon=True,
        )
        thread.start()
        return _SocketLiveModelChannel(
            connection=executor_connection,
            model=self.config.model,
            timeout_seconds=self.config.timeout_seconds,
        )

    def _serve_channel(self, connection: socket.socket, cell_id: str) -> None:
        calls = 0
        with connection:
            while True:
                try:
                    request = _receive_message(connection)
                except (OSError, TypeError, ValueError):
                    return
                if request is None:
                    return
                if calls >= self.config.budget.max_calls:
                    result: dict[str, object] = {
                        "ok": False,
                        "error": "fixed-live cell call budget exhausted",
                    }
                else:
                    calls += 1
                    try:
                        body = self._create(_request_payload(request, self.config))
                        response = _normalized_response(
                            body=body,
                            cell_id=cell_id,
                            call_number=calls,
                            configured_model=self.config.model,
                        )
                    except Exception as exc:  # noqa: BLE001 - broker returns a bounded error
                        result = {
                            "ok": False,
                            "error": f"{type(exc).__name__}: fixed-live request failed",
                        }
                    else:
                        result = {"ok": True, "response": _response_to_message(response)}
                try:
                    _send_message(connection, result)
                except OSError:
                    return

    def _create(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        return self._transport.create(
            api_base=self.config.api_base,
            credential=self._credential,
            timeout_seconds=self.config.timeout_seconds,
            payload=payload,
        )
