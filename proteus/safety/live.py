"""Trusted fixed-live model channel for candidate safety executors."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping, Sequence
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


class _BudgetedLiveModelChannel:
    def __init__(
        self,
        *,
        cell_id: str,
        config: LiveModelConfig,
        request: Callable[[Mapping[str, object]], Mapping[str, object]],
    ) -> None:
        _require_text("live cell ID", cell_id)
        self._cell_id = cell_id
        self._config = config
        self._request = request
        self._calls = 0

    @property
    def model(self) -> str:
        return self._config.model

    def respond(
        self,
        *,
        input: str | Sequence[Mapping[str, object]],
        instructions: str = "",
        tools: Sequence[Mapping[str, object]] = (),
    ) -> LiveModelResponse:
        if self._calls >= self._config.budget.max_calls:
            raise RuntimeError("fixed-live cell call budget exhausted")
        self._calls += 1
        payload: dict[str, object] = {
            "model": self._config.model,
            "input": input,
            "max_output_tokens": self._config.budget.max_output_tokens,
            "store": False,
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = list(tools)
        body = self._request(payload)
        response_id = body.get("id")
        response_model = body.get("model")
        if not isinstance(response_id, str) or not response_id.strip():
            raise ValueError("Responses API payload has no response ID")
        if not isinstance(response_model, str) or not response_model.strip():
            raise ValueError("Responses API payload has no model provenance")
        call_id = f"{self._cell_id}.call-{self._calls}"
        provenance = LiveCallProvenance(
            call_id=call_id,
            response_id=response_id,
            configured_model=self._config.model,
            response_model=response_model,
        )
        return LiveModelResponse(
            response_id=response_id,
            model=response_model,
            output_text=_response_output_text(body),
            tool_calls=_response_tool_calls(body),
            provenance=provenance,
        )


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
        return _BudgetedLiveModelChannel(
            cell_id=cell_id,
            config=self.config,
            request=self._create,
        )

    def _create(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        return self._transport.create(
            api_base=self.config.api_base,
            credential=self._credential,
            timeout_seconds=self.config.timeout_seconds,
            payload=payload,
        )
