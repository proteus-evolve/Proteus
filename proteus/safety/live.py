"""Trusted live-channel contracts used by model-mediated safety runtimes."""

from __future__ import annotations

import json
import multiprocessing
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Protocol, runtime_checkable

RESPONSES_URL = "https://api.openai.com/v1/responses"
_TRANSPORT_ATTEMPTS = 3
_TRANSIENT_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})


class LiveConfigurationError(ValueError):
    """Raised when the trusted controller cannot load live credentials."""


class LiveProtocolError(RuntimeError):
    """Raised when a live request or response violates the controller contract."""


class _TransientTransportError(Exception):
    """Retryable Responses transport failure; never shown to the subject."""


class LiveCallCategory(str, Enum):
    ORDINARY = "ordinary"
    SAFETY = "safety"


@dataclass(frozen=True)
class LiveCallBudgetPlan:
    harness: str
    ordinary_cap: int
    safety_cap: int

    def __post_init__(self) -> None:
        if not self.harness.strip():
            raise ValueError("live call plan requires a harness")
        for label, value in (
            ("ordinary", self.ordinary_cap),
            ("safety", self.safety_cap),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{label} live-call cap must be a non-negative integer")

    @property
    def total_cap(self) -> int:
        return self.ordinary_cap + self.safety_cap


def derive_builtin_live_call_plan(
    *,
    harness: str,
    episodes: int,
    ordinary_hard_limit: int,
    permission_supported_cases: int,
    include_memory_families: bool = False,
    collapse_episode_count: int | None = None,
) -> LiveCallBudgetPlan:
    """Derive whole-run ordinary/safety caps from current runtime contracts.

    Safety caps sum family call budgets over the baseline and each scheduled episode.
    """
    if type(episodes) is not int or episodes < 1:
        raise ValueError("live call plan episodes must be a positive integer")
    if type(ordinary_hard_limit) is not int or ordinary_hard_limit < 0:
        raise ValueError("ordinary hard limit must be a non-negative integer")
    if type(permission_supported_cases) is not int or permission_supported_cases < 0:
        raise ValueError("permission supported cases must be a non-negative integer")
    name = harness.strip().lower()
    if name == "minimal":
        ordinary = 0
    elif name == "llm":
        ordinary = 4 * episodes
    elif name == "pi":
        ordinary = (ordinary_hard_limit + 4) * episodes
    elif name == "dsh":
        ordinary = (ordinary_hard_limit + 8) * episodes
    elif name == "aki":
        ordinary = ordinary_hard_limit * episodes
    else:
        raise ValueError(f"unsupported live-call plan harness: {harness}")
    # These are provider-call caps, not the number of executable permission
    # cases.  Every built-in adapter issues fixed permission requests
    # controller-locally through its real parser/bridge/dispatcher boundary;
    # permission measurement therefore reserves no provider calls.
    permission_case_caps = {
        "minimal": (0, 0),
        "llm": (0, 0),
        "pi": (0, 0, 0, 0),
        "dsh": (0, 0, 0),
        "aki": (0, 0, 0, 0),
    }[name]
    if permission_supported_cases > len(permission_case_caps):
        raise ValueError(
            "permission supported-case count exceeds the harness-native capability matrix"
        )
    permission_evals = episodes + 1
    safety = sum(permission_case_caps[:permission_supported_cases]) * permission_evals
    if include_memory_families:
        if name == "dsh":
            # DSH's exact-memory and pressure probes are controller-native. Only the
            # scheduled bad-admission behavior trial opens the provider channel, using
            # one provider response after a controller-administered native read.
            behavior_episodes = {1, episodes}
            behavior_episodes.update(range(5, episodes + 1, 5))
            safety += len(behavior_episodes)
            return LiveCallBudgetPlan(name, ordinary, safety)
        # Minimal's memory runtime is deterministic. The native Pi/Aki probes
        # need sixteen provider calls per family evaluation; LLM needs eight.
        memory_calls = {
            "minimal": 0,
            "llm": 8,
            "pi": 16,
            "aki": 16,
        }[name]
        admission_evals = episodes + 1
        if collapse_episode_count is None:
            collapse_evals = 2 + (episodes // 5)
        else:
            collapse_evals = collapse_episode_count + 1
        safety += (admission_evals + collapse_evals) * memory_calls
    return LiveCallBudgetPlan(name, ordinary, safety)


class ControllerLiveCallBudget:
    """Claim ordinary and safety live calls before any provider request."""

    def __init__(self, plan: LiveCallBudgetPlan, ledger_path: Path) -> None:
        self._plan = plan
        self._ledger_path = Path(ledger_path)
        self._actual = {"ordinary": 0, "safety": 0, "total": 0}
        self._lock = RLock()
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._write()

    def wrap(
        self,
        channel: LiveModelChannel,
        *,
        category: LiveCallCategory,
        cell_id: str,
        channel_cap: int,
    ) -> LiveModelChannel:
        del cell_id
        if type(channel_cap) is not int or channel_cap <= 0:
            raise ValueError("live channel cap must be a positive integer")
        return _BudgetedLiveChannel(self, channel, category, channel_cap)

    def snapshot(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "harness": self._plan.harness,
                "call_budget": {
                    "ordinary_cap": self._plan.ordinary_cap,
                    "safety_cap": self._plan.safety_cap,
                    "total_cap": self._plan.total_cap,
                },
                "actual": dict(self._actual),
                "actual_calls": dict(self._actual),
            }

    def claim(self, category: LiveCallCategory) -> None:
        with self._lock:
            remaining_category = (
                self._plan.ordinary_cap
                if category is LiveCallCategory.ORDINARY
                else self._plan.safety_cap
            ) - self._actual[category.value]
            remaining_total = self._plan.total_cap - self._actual["total"]
            if remaining_category <= 0 or remaining_total <= 0:
                raise LiveProtocolError(f"{category.value} live-call cap exhausted")
            self._actual[category.value] += 1
            self._actual["total"] += 1
            self._write()

    def _write(self) -> None:
        self._ledger_path.write_text(
            json.dumps(self.snapshot(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


class _BudgetedLiveChannel:
    def __init__(
        self,
        budget: ControllerLiveCallBudget,
        channel: LiveModelChannel,
        category: LiveCallCategory,
        channel_cap: int,
    ) -> None:
        self._budget = budget
        self._channel = channel
        self._category = category
        self._channel_cap = channel_cap
        self._claimed = 0

    @property
    def model(self) -> str:
        return self._channel.model

    def _claim(self) -> None:
        if self._claimed >= self._channel_cap:
            raise LiveProtocolError(
                f"{self._category.value} live-call cap exhausted"
            )
        self._budget.claim(self._category)
        self._claimed += 1

    def respond(self, **kwargs):
        self._claim()
        return self._channel.respond(**kwargs)

    def respond_bounded(self, **kwargs):
        self._claim()
        if hasattr(self._channel, "respond_bounded"):
            return self._channel.respond_bounded(**kwargs)
        return self._channel.respond(**kwargs)

    def close(self) -> None:
        self._channel.close()


@dataclass(frozen=True)
class LiveCallProvenance:
    call_id: str
    response_id: str
    configured_model: str
    response_model: str


@dataclass(frozen=True)
class LiveToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class LiveModelRequestOptions:
    """Optional controller-owned generation controls for one live model call."""

    max_output_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or self.max_output_tokens <= 0
        ):
            raise ValueError("live max output tokens must be a positive integer")
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not 0 <= self.temperature <= 2
        ):
            raise ValueError("live temperature must be between 0 and 2")
        if self.reasoning_effort is not None and self.reasoning_effort not in {
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        }:
            raise ValueError("unsupported live reasoning effort")


@dataclass(frozen=True)
class LiveModelUsage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        for name, value in (
            ("input tokens", self.input_tokens),
            ("output tokens", self.output_tokens),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"live model {name} must be a non-negative integer")


@dataclass(frozen=True)
class LiveModelResponse:
    response_id: str
    model: str
    output_text: str
    tool_calls: tuple[LiveToolCall, ...]
    provenance: LiveCallProvenance
    usage: LiveModelUsage | None = None


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
        options: LiveModelRequestOptions | None = None,
    ) -> LiveModelResponse: ...

    def close(self) -> None: ...


@runtime_checkable
class BoundedLiveModelChannel(LiveModelChannel, Protocol):
    """Trusted channel whose deadline is enforced before the call returns."""

    def respond_bounded(
        self,
        *,
        input: str | Sequence[Mapping[str, object]],
        instructions: str = "",
        tools: Sequence[Mapping[str, object]] = (),
        options: LiveModelRequestOptions | None = None,
        timeout_s: float,
    ) -> LiveModelResponse: ...


ResponsesTransport = Callable[
    [str, Mapping[str, object], Mapping[str, str], float], Mapping[str, object]
]

_CALL_PROCESS_STOP_TIMEOUT_S = 0.5


def _isolated_transport_main(
    connection,
    transport: ResponsesTransport,
    url: str,
    payload: Mapping[str, object],
    headers: Mapping[str, str],
    timeout_s: float,
) -> None:
    try:
        connection.send(("ok", transport(url, payload, headers, timeout_s)))
    except BaseException:  # noqa: BLE001 - report every worker failure before cleanup.
        connection.send(("error", None))
    finally:
        connection.close()


def load_repository_openai_key(repository_root: Path) -> str:
    """Read only ``OPENAI_API_KEY`` from the common repository's ``.env``."""
    try:
        lines = (Path(repository_root) / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, raw_value = line.partition("=")
        if separator and name.strip() == "OPENAI_API_KEY":
            value = raw_value.strip()
            if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
                value = value[1:-1]
            if value:
                return value
            break
    raise LiveConfigurationError("OPENAI_API_KEY is absent from the repository .env")


def common_repository_root(start: Path) -> Path:
    """Resolve the owning checkout root when ``start`` is a linked Git worktree."""
    for directory in (Path(start).resolve(), *Path(start).resolve().parents):
        marker = directory / ".git"
        if marker.is_dir():
            return directory
        if not marker.is_file():
            continue
        try:
            line = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        prefix = "gitdir:"
        if not line.lower().startswith(prefix):
            continue
        git_dir = Path(line[len(prefix):].strip())
        if not git_dir.is_absolute():
            git_dir = (directory / git_dir).resolve()
        common_marker = git_dir / "commondir"
        try:
            common_dir = (git_dir / common_marker.read_text(encoding="utf-8").strip()).resolve()
        except OSError:
            common_dir = git_dir.parent.parent if git_dir.parent.name == "worktrees" else git_dir
        return common_dir.parent
    raise LiveConfigurationError("cannot locate the common repository root")


def _http_error_payload(exc: urllib.error.HTTPError) -> dict[str, object]:
    raw = exc.read().decode("utf-8", errors="replace")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {"error": {"type": "http_error", "message": raw[:200]}}
    if not isinstance(body, dict):
        body = {"error": {"type": "http_error", "message": "non-object error body"}}
    body["http_status"] = int(exc.code)
    return body


def _http_error_message(payload: Mapping[str, object]) -> str:
    status = payload.get("http_status")
    err = payload.get("error")
    if isinstance(err, Mapping):
        kind = str(err.get("code") or err.get("type") or "error")
        detail = str(err.get("message") or "").strip()[:180]
        suffix = f"{kind}: {detail}" if detail else kind
        return f"OpenAI Responses HTTP {status}: {suffix}"
    return f"OpenAI Responses HTTP {status}"


def _stdlib_responses_transport(
    url: str,
    payload: Mapping[str, object],
    headers: Mapping[str, str],
    timeout: float,
) -> Mapping[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = _http_error_payload(exc)
        if int(exc.code) in _TRANSIENT_HTTP_STATUS:
            raise _TransientTransportError(_http_error_message(payload)) from None
        raise LiveProtocolError(_http_error_message(payload)) from None
    except urllib.error.URLError as exc:
        # Connection resets and DNS blips are retryable; do not leak resolver details.
        del exc
        raise _TransientTransportError("OpenAI Responses request failed") from None
    if not isinstance(decoded, Mapping):
        raise LiveProtocolError("OpenAI Responses result must be a mapping")
    return decoded


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LiveProtocolError(f"{label} must be non-empty text")
    return value


class OpenAIResponsesChannel:
    """One controller-owned Responses session with raw request/response ledgers."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        evidence_dir: Path,
        transport: ResponsesTransport,
        timeout_s: int = 180,
    ) -> None:
        if not model.strip():
            raise LiveConfigurationError("live model must be non-empty")
        if not api_key:
            raise LiveConfigurationError("OPENAI_API_KEY is absent from the repository .env")
        self._model = model
        self._api_key = api_key
        self._evidence_dir = evidence_dir
        self._transport = transport
        self._timeout_s = timeout_s
        self._closed = False
        self._calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        evidence_dir.mkdir(parents=True, exist_ok=False)

    @property
    def model(self) -> str:
        return self._model

    def respond(
        self,
        *,
        input: str | Sequence[Mapping[str, object]],
        instructions: str = "",
        tools: Sequence[Mapping[str, object]] = (),
        options: LiveModelRequestOptions | None = None,
    ) -> LiveModelResponse:
        return self._respond(
            input=input,
            instructions=instructions,
            tools=tools,
            options=options,
            bounded_timeout_s=None,
        )

    def respond_bounded(
        self,
        *,
        input: str | Sequence[Mapping[str, object]],
        instructions: str = "",
        tools: Sequence[Mapping[str, object]] = (),
        options: LiveModelRequestOptions | None = None,
        timeout_s: float,
    ) -> LiveModelResponse:
        if timeout_s <= 0:
            raise ValueError("live model call timeout must be positive")
        return self._respond(
            input=input,
            instructions=instructions,
            tools=tools,
            options=options,
            bounded_timeout_s=timeout_s,
        )

    @staticmethod
    def _stop_call_process(process: multiprocessing.Process) -> None:
        process.join(_CALL_PROCESS_STOP_TIMEOUT_S)
        if process.is_alive():
            process.terminate()
            process.join(_CALL_PROCESS_STOP_TIMEOUT_S)
        if process.is_alive():
            process.kill()
            process.join(_CALL_PROCESS_STOP_TIMEOUT_S)
        if process.is_alive():
            raise LiveProtocolError("bounded Responses transport process did not stop")

    def _bounded_transport(
        self,
        *,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> Mapping[str, object]:
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(
            target=_isolated_transport_main,
            args=(
                send,
                self._transport,
                RESPONSES_URL,
                payload,
                headers,
                min(float(self._timeout_s), timeout_s),
            ),
            name=f"proteus-live-call-{self._calls:03d}",
        )
        started = False
        try:
            process.start()
            started = True
            send.close()
            if not receive.poll(timeout_s):
                self._stop_call_process(process)
                raise TimeoutError(
                    f"OpenAI Responses request timed out after {timeout_s} seconds"
                )
            try:
                status, raw = receive.recv()
            except EOFError:
                raise LiveProtocolError("OpenAI Responses request failed") from None
            self._stop_call_process(process)
            if status != "ok" or not isinstance(raw, Mapping):
                raise LiveProtocolError("OpenAI Responses request failed")
            return raw
        finally:
            receive.close()
            send.close()
            if started and process.is_alive():
                self._stop_call_process(process)

    def _respond(
        self,
        *,
        input: str | Sequence[Mapping[str, object]],
        instructions: str,
        tools: Sequence[Mapping[str, object]],
        options: LiveModelRequestOptions | None,
        bounded_timeout_s: float | None,
    ) -> LiveModelResponse:
        if self._closed:
            raise LiveProtocolError("live model channel is closed")
        if not isinstance(input, str) and (
            not isinstance(input, Sequence)
            or isinstance(input, (bytes, bytearray))
            or not all(isinstance(item, Mapping) for item in input)
        ):
            raise LiveProtocolError("Responses input must be text or a sequence of mappings")
        if not all(isinstance(tool, Mapping) for tool in tools):
            raise LiveProtocolError("Responses tools must be mappings")
        self._calls += 1
        call_id = f"call-{self._calls:03d}"
        max_output_tokens = (
            options.max_output_tokens
            if options is not None and options.max_output_tokens is not None
            else 65_536
        )
        reasoning_effort = (
            options.reasoning_effort
            if options is not None and options.reasoning_effort is not None
            else "none"
        )
        payload: dict[str, object] = {
            "model": self._model,
            "input": deepcopy(input),
            "reasoning": {"effort": reasoning_effort},
            "max_output_tokens": max_output_tokens,
            "parallel_tool_calls": False,
            "store": False,
        }
        if options is not None and options.temperature is not None:
            payload["temperature"] = options.temperature
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = deepcopy(list(tools))
        self._write_json(self._evidence_dir / f"request-{self._calls:03d}.json", payload)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        raw: Mapping[str, object] | None = None
        last_timeout: TimeoutError | None = None
        for attempt in range(_TRANSPORT_ATTEMPTS):
            try:
                raw = (
                    self._transport(RESPONSES_URL, payload, headers, self._timeout_s)
                    if bounded_timeout_s is None
                    else self._bounded_transport(
                        payload=payload,
                        headers=headers,
                        timeout_s=bounded_timeout_s,
                    )
                )
                break
            except TimeoutError as exc:
                last_timeout = exc
                # The bounded worker is already stopped before this exception
                # escapes. Retrying would turn one controller deadline into
                # several full-length calls.
                if bounded_timeout_s is not None:
                    raise
                if attempt + 1 < _TRANSPORT_ATTEMPTS:
                    continue
                raise
            except _TransientTransportError:
                if attempt + 1 < _TRANSPORT_ATTEMPTS:
                    continue
                raise LiveProtocolError("OpenAI Responses request failed") from None
            except LiveProtocolError:
                raise
            except Exception:  # noqa: BLE001 - never expose transport details or credentials
                if attempt + 1 < _TRANSPORT_ATTEMPTS:
                    continue
                raise LiveProtocolError("OpenAI Responses request failed") from None
        else:
            if last_timeout is not None:
                raise last_timeout
            raise LiveProtocolError("OpenAI Responses request failed")
        if raw is None:
            raise LiveProtocolError("OpenAI Responses request failed")
        if not isinstance(raw, Mapping):
            raise LiveProtocolError("OpenAI Responses result must be a mapping")
        self._write_json(self._evidence_dir / f"response-{self._calls:03d}.json", raw)
        response_id = _required_text(raw.get("id"), "response id")
        status = _required_text(raw.get("status"), "response status")
        if status != "completed":
            details = raw.get("incomplete_details")
            reason = (
                details.get("reason")
                if isinstance(details, Mapping)
                else None
            )
            # Truncation at the output-token cap is recoverable for ordinary evolution:
            # keep usable text/tool calls instead of aborting the whole seed.
            if not (status == "incomplete" and reason == "max_output_tokens"):
                raise LiveProtocolError("OpenAI Responses request did not complete")
        response_model = _required_text(raw.get("model"), "response model")
        if response_model != self._model:
            raise LiveProtocolError("returned model does not match configured model")
        usage = raw.get("usage")
        if not isinstance(usage, Mapping):
            raise LiveProtocolError("response usage must be a mapping")
        input_tokens = self._token_count(usage, "input_tokens")
        output_tokens = self._token_count(usage, "output_tokens")
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        output_text, tool_calls = self._normalize_output(raw.get("output"))
        provenance = LiveCallProvenance(
            call_id=call_id,
            response_id=response_id,
            configured_model=self._model,
            response_model=response_model,
        )
        return LiveModelResponse(
            response_id=response_id,
            model=response_model,
            output_text=output_text,
            tool_calls=tool_calls,
            provenance=provenance,
            usage=LiveModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        )

    def close(self) -> None:
        self._closed = True
        self._api_key = ""

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, object]) -> None:
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    @staticmethod
    def _token_count(usage: Mapping[str, object], name: str) -> int:
        value = usage.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise LiveProtocolError(f"response usage {name} must be a non-negative integer")
        return value

    @staticmethod
    def _normalize_output(value: object) -> tuple[str, tuple[LiveToolCall, ...]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise LiveProtocolError("response output must be a sequence")
        text_parts: list[str] = []
        tool_calls: list[LiveToolCall] = []
        saw_empty_message = False
        for item in value:
            if not isinstance(item, Mapping):
                raise LiveProtocolError("response output items must be mappings")
            item_type = item.get("type")
            if item_type == "reasoning":
                continue
            if item_type == "message":
                content = item.get("content")
                if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
                    raise LiveProtocolError("response message content must be a sequence")
                for part in content:
                    if not isinstance(part, Mapping):
                        raise LiveProtocolError("response message items must be mappings")
                    part_type = part.get("type")
                    if part_type == "output_text":
                        output_text = part.get("text")
                        if not isinstance(output_text, str):
                            raise LiveProtocolError("output text must be non-empty text")
                        if output_text:
                            text_parts.append(output_text)
                        else:
                            saw_empty_message = True
                    elif part_type == "refusal":
                        text_parts.append(_required_text(part.get("refusal"), "refusal"))
                    else:
                        raise LiveProtocolError("unsupported response message content")
                continue
            if item_type == "function_call":
                raw_arguments = _required_text(item.get("arguments"), "function arguments")
                try:
                    arguments = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    raise LiveProtocolError("function arguments must be valid JSON") from None
                if not isinstance(arguments, Mapping):
                    raise LiveProtocolError("function arguments must be a JSON object")
                tool_calls.append(
                    LiveToolCall(
                        call_id=_required_text(item.get("call_id"), "function call ID"),
                        name=_required_text(item.get("name"), "function name"),
                        arguments=dict(arguments),
                    )
                )
                continue
            raise LiveProtocolError(f"unsupported response output type: {item_type!r}")
        if not text_parts and not tool_calls:
            # After a tool result the model may emit a completed message with empty
            # text. That is a finished turn, not a missing response. Rejecting it
            # aborted Aki permission trials that already had native proposals.
            if saw_empty_message:
                return "", ()
            raise LiveProtocolError("response output must contain text or tool calls")
        return "".join(text_parts), tuple(tool_calls)


class OpenAIResponsesChannelFactory:
    """Trusted credential owner that opens independently-ledgered live channels."""

    def __init__(
        self,
        *,
        api_key: str,
        evidence_root: Path,
        transport: ResponsesTransport | None = None,
    ) -> None:
        if not api_key:
            raise LiveConfigurationError("OPENAI_API_KEY is absent from the repository .env")
        self._api_key = api_key
        self._evidence_root = Path(evidence_root)
        self._transport = transport or _stdlib_responses_transport

    @classmethod
    def from_repository(
        cls,
        *,
        repository_root: Path,
        evidence_root: Path,
        transport: ResponsesTransport | None = None,
    ) -> OpenAIResponsesChannelFactory:
        return cls(
            api_key=load_repository_openai_key(repository_root),
            evidence_root=evidence_root,
            transport=transport,
        )

    def __call__(self, model: str, cell_id: str) -> OpenAIResponsesChannel:
        safe_cell = "".join(
            character if character.isalnum() or character in "-_." else "-"
            for character in cell_id
        ).strip(".-") or "model-cell"
        cell_root = self._evidence_root / safe_cell
        cell_root.mkdir(parents=True, exist_ok=True)
        attempts = []
        for path in cell_root.iterdir():
            prefix, separator, raw_number = path.name.partition("-")
            if path.is_dir() and prefix == "attempt" and separator and raw_number.isdigit():
                attempts.append(int(raw_number))
        attempt = max(attempts, default=0) + 1
        evidence_dir = cell_root / f"attempt-{attempt:06d}"
        return OpenAIResponsesChannel(
            model=model,
            api_key=self._api_key,
            evidence_dir=evidence_dir,
            transport=self._transport,
        )
