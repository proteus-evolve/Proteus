"""Strict entrypoint for actions executed by the contained Aki runtime."""

from __future__ import annotations

import asyncio
import importlib.util
import importlib.metadata
import inspect
import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO


PROTOCOL_VERSION = 1
FRAME_HEADER_BYTES = 8
MAX_FRAME_BYTES = 32 * 1024 * 1024
_ACTIONS = frozenset({"inspect", "init", "ordinary_episode", "safety_episode"})
_CREDENTIAL_NAMES = ("OPENAI_API_KEY", "ZAI_KEY", "DEEPSEEK_KEY")
_CREDENTIAL_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_BASE_IMAGE_CREDENTIAL_NAMES = ("GPG_KEY",)
_CONTROLLER_SOCKET = Path("/state/proteus-controller.sock")
_SAFETY_NATIVE_SOCKET = Path("/state/proteus-safety-native.sock")
_SAFETY_PLAN = Path("/state/proteus-safety-plan.json")
_IMAGE_AKI_ROOT = Path("/opt/aki")

# Aki's native experiment runner is source-only and is not part of the installed wheel.
sys.path.insert(0, "/opt/aki")


def _read_exact(size: int, stream: BinaryIO | None = None) -> bytes:
    source = stream or sys.stdin.buffer
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = source.read(remaining)
        if not chunk:
            raise EOFError(f"Aki container frame ended with {remaining} bytes missing")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream: BinaryIO | None = None) -> dict[str, object]:
    source = stream or sys.stdin.buffer
    size = int.from_bytes(_read_exact(FRAME_HEADER_BYTES, source), "big")
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise ValueError(f"invalid Aki container frame size {size}")
    value = json.loads(_read_exact(size, source))
    if not isinstance(value, dict):
        raise TypeError("Aki container frame must contain a JSON object")
    version = value.get("protocol_version")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ValueError("unsupported Aki container protocol version")
    return value


def _request() -> tuple[str, dict[str, object]]:
    request = _read_frame()
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("Aki container request needs a non-empty request ID")
    if request.get("kind") != "request":
        raise ValueError("Aki container input is not a request frame")
    payload = request.get("payload")
    if not isinstance(payload, dict):
        raise TypeError("Aki container request payload must be a JSON object")
    if payload.get("action") not in _ACTIONS:
        raise ValueError("unsupported Aki container action")
    return request_id, payload


def _write_frame(stream: BinaryIO, value: dict[str, object]) -> None:
    body = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) <= 0 or len(body) > MAX_FRAME_BYTES:
        raise ValueError("Aki container frame exceeds the frame limit")
    stream.write(len(body).to_bytes(FRAME_HEADER_BYTES, "big"))
    stream.write(body)
    stream.flush()


def _write_terminal(
    request_id: str, payload: dict[str, object], stream: BinaryIO | None = None
) -> None:
    _write_frame(
        stream or sys.stdout.buffer,
        {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "kind": "terminal",
            "payload": payload,
        },
    )


def _inspect() -> dict[str, object]:
    from experiments.persona_gen import CONDITIONS_BY_NAME  # noqa: F401
    from experiments.runner import supervisor  # noqa: F401
    from experiments.runner.config import RunConfig  # noqa: F401
    from experiments.runner.controller_model import ControllerLLM  # noqa: F401

    archive = Path("/opt/aki-source.tar")
    manifest = Path("/opt/source-manifest.txt")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "action": "inspect",
        "aki_version": importlib.metadata.version("aki"),
        "native_api": "persona_gen+runner.config+runner.supervisor",
        "controller_module": "experiments.runner.controller_model",
        "credential_environment_names": [
            name for name in _CREDENTIAL_NAMES if name in os.environ
        ],
        "source_archive_readable": archive.is_file() and os.access(archive, os.R_OK),
        "source_manifest_readable": manifest.is_file() and os.access(manifest, os.R_OK),
    }


def _init(request: dict[str, object]) -> dict[str, object]:
    from experiments.persona_gen import CONDITIONS_BY_NAME
    from experiments.runner import supervisor
    from experiments.runner.config import RunConfig

    condition_name = request.get("condition")
    if not isinstance(condition_name, str) or condition_name not in CONDITIONS_BY_NAME:
        raise ValueError("Aki init condition is not in CONDITIONS_BY_NAME")
    seed = request.get("seed", 0)
    episodes = request.get("episodes", 1)
    if type(seed) is not int or type(episodes) is not int or episodes <= 0:
        raise ValueError("Aki init seed and episodes must be integers")
    if request.get("root") != "/run":
        raise ValueError("Aki init root must be the container path /run")

    config = RunConfig(
        condition=CONDITIONS_BY_NAME[condition_name],
        seed=seed,
        episodes=episodes,
        root=Path("/run"),
    )
    supervisor.init_run(config)
    episode = config.for_episode()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "action": "init",
        "condition": condition_name,
        "native_config": {
            "condition": config.condition.name,
            "seed": config.seed,
            "episodes": config.episodes,
            "root": str(config.root),
            "model": config.model,
            "base_url": config.base_url,
            "persona": config.persona_name,
            "max_turns": config.max_turns,
        },
        "episode_config": {
            "root": str(episode.root),
            "persona": episode.persona,
            "model": episode.model,
            "base_url": episode.base_url,
            "max_turns": episode.max_turns,
            "max_output_tokens": episode.max_output_tokens,
            "snapshot_dir": str(episode.snapshot_dir),
            "memory_dir": str(episode.memory_dir),
            "skills_dir": str(episode.skills_dir),
            "tools_dir": str(episode.tools_dir),
            "trace_dir": str(episode.trace_dir),
            "loop_path": str(episode.loop_path),
            "package_dir": str(episode.package_dir),
            "integrity_path": str(episode.integrity_path),
            "aki_root": str(episode.aki_root),
            "persona_dir": str(episode.persona_dir),
        },
    }


def _socket_receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = connection.recv(size)
        if not chunk:
            raise EOFError("native controller socket closed an incomplete frame")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _socket_receive_frame(connection: socket.socket) -> dict[str, object]:
    size = int.from_bytes(_socket_receive_exact(connection, FRAME_HEADER_BYTES), "big")
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise ValueError(f"invalid native controller frame size {size}")
    value = json.loads(_socket_receive_exact(connection, size))
    if not isinstance(value, dict):
        raise TypeError("native controller frame must contain a JSON object")
    version = value.get("protocol_version")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ValueError("native controller frame has the wrong protocol version")
    return value


def _socket_send_frame(connection: socket.socket, value: dict[str, object]) -> None:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) <= 0 or len(body) > MAX_FRAME_BYTES:
        raise ValueError("native controller frame exceeds the frame limit")
    connection.sendall(len(body).to_bytes(FRAME_HEADER_BYTES, "big") + body)


class _ControllerProxy:
    """Serialize the native child socket onto the entrypoint's private protocol stream."""

    def __init__(
        self,
        *,
        listener: socket.socket,
        protocol_output: BinaryIO,
        expected_model: str,
        safety_executor=None,
    ) -> None:
        self._listener = listener
        self._protocol_output = protocol_output
        self._expected_model = expected_model
        self._safety_executor = safety_executor
        self._stop = threading.Event()
        self._failures: list[BaseException] = []
        self._connection: socket.socket | None = None
        self._connection_lock = threading.Lock()
        self.controller_tool_calls: list[dict[str, object]] = []
        self._thread = threading.Thread(
            target=self._run,
            name="aki-container-controller-proxy",
            daemon=False,
        )
        self.stopped = False

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        connection: socket.socket | None = None
        try:
            self._listener.settimeout(0.1)
            while not self._stop.is_set():
                try:
                    connection, _ = self._listener.accept()
                    break
                except TimeoutError:
                    continue
            if connection is None:
                return
            with self._connection_lock:
                self._connection = connection
            with connection:
                while True:
                    try:
                        request = _socket_receive_frame(connection)
                    except EOFError:
                        return
                    request_id = request.get("request_id")
                    payload = request.get("payload")
                    if not isinstance(request_id, str) or not request_id:
                        raise ValueError("native controller request needs a request ID")
                    request_kind = request.get("kind")
                    if request_kind != "model_request":
                        raise ValueError("native controller emitted an unsupported request")
                    if not isinstance(payload, dict):
                        raise TypeError("native controller request payload must be an object")
                    if payload.get("model") != self._expected_model:
                        raise ValueError("native model request uses the wrong model")
                    _write_frame(self._protocol_output, request)
                    response = _read_frame()
                    if response.get("request_id") != request_id:
                        raise ValueError("host controller response has the wrong request ID")
                    if response.get("kind") != "model_response":
                        raise ValueError("host controller emitted the wrong response kind")
                    response_payload = response.get("payload")
                    if not isinstance(response_payload, dict):
                        raise TypeError("host controller response payload must be an object")
                    if response_payload.get("model") != self._expected_model:
                        raise ValueError("host model response uses the wrong model")
                    raw_calls = response_payload.get("tool_calls")
                    if not isinstance(raw_calls, list):
                        raise TypeError("host model response tool calls must be a list")
                    for item in raw_calls:
                        if not isinstance(item, dict):
                            raise TypeError("host model response tool call must be an object")
                        call_id = item.get("id")
                        name = item.get("name")
                        arguments = item.get("input")
                        if (
                            not isinstance(call_id, str)
                            or not call_id
                            or not isinstance(name, str)
                            or not name
                            or not isinstance(arguments, dict)
                        ):
                            raise ValueError("host model response tool call is malformed")
                        self.controller_tool_calls.append(
                            {
                                "operation_id": call_id,
                                "tool_name": name,
                                "arguments": dict(arguments),
                            }
                        )
                    if self._safety_executor is not None:
                        self._safety_executor.execute_controller_calls(raw_calls)
                    _socket_send_frame(connection, response)
        except BaseException as exc:
            self._failures.append(exc)
            if connection is not None:
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
        finally:
            with self._connection_lock:
                self._connection = None

    def finish(self) -> None:
        self._stop.set()
        try:
            wake = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            wake.connect(str(self._listener.getsockname()))
            wake.close()
        except OSError:
            pass
        with self._connection_lock:
            connection = self._connection
        if connection is not None:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            self._listener.close()
        self._thread.join(timeout=1.0)
        if self._thread.is_alive():
            raise RuntimeError("Aki model listener did not stop")
        if self._failures:
            raise self._failures[0]
        self.stopped = True


def _credential_environment_names() -> list[str]:
    return sorted(
        name
        for name in os.environ
        if any(marker in name.upper() for marker in _CREDENTIAL_MARKERS)
    )


def _network_is_blocked() -> bool:
    probe = socket.socket()
    probe.settimeout(0.05)
    try:
        probe.connect(("1.1.1.1", 53))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def _json_value(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _safety_native_config(request: dict[str, object]) -> dict[str, object]:
    episode = request.get("episode")
    model = request.get("model")
    base_url = request.get("base_url")
    persona = request.get("persona")
    max_turns = request.get("max_turns")
    max_output_tokens = request.get("max_output_tokens")
    if type(episode) is not int or episode <= 0:
        raise ValueError("Aki safety episode must be a positive integer")
    if not isinstance(model, str) or not model:
        raise ValueError("Aki safety model must be non-empty")
    if base_url != "controller://openai-responses":
        raise ValueError("Aki safety base URL must use the controller route")
    if not isinstance(persona, str) or not persona:
        raise ValueError("Aki safety persona must be non-empty")
    if type(max_turns) is not int or max_turns <= 0:
        raise ValueError("Aki safety max turns must be positive")
    if type(max_output_tokens) is not int or max_output_tokens <= 0:
        raise ValueError("Aki safety max output tokens must be positive")
    root = "/workspace/candidate"
    return {
        "root": root,
        "persona": persona,
        "model": model,
        "base_url": base_url,
        "max_turns": max_turns,
        "max_output_tokens": max_output_tokens,
        "snapshot_dir": f"{root}/harness",
        "memory_dir": f"{root}/harness/memory",
        "skills_dir": f"{root}/harness/skills",
        "tools_dir": f"{root}/harness/tools",
        "trace_dir": f"{root}/traces",
        "loop_path": f"{root}/harness/loop.py",
        "package_dir": f"{root}/harness/aki",
        "integrity_path": f"{root}/integrity.json",
        "aki_root": f"{root}/.aki",
        "persona_dir": f"{root}/.persona",
    }


def _safety_child_plan(request: dict[str, object]) -> dict[str, object]:
    keys = {
        "episode",
        "prompts",
        "model",
        "base_url",
        "persona",
        "max_turns",
        "max_output_tokens",
    }
    return {key: request[key] for key in keys}


class _SafetyTracer:
    """Candidate-visible lifecycle aid; never a source of safety truth."""

    def emit(self, event: str, data: dict[str, object]) -> None:
        del event, data

    def attach(self, agent: object) -> None:
        del agent


def _safety_child(plan_path: Path) -> int:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise TypeError("Aki safety child plan must be an object")
        native_config = _safety_native_config(plan)
        snapshot = Path(str(native_config["snapshot_dir"])).resolve()
        os.chdir(snapshot)
        sys.path.insert(0, str(snapshot))
        import aki
        from experiments.runner.controller_model import ControllerLLM
        from experiments.runner.model import _pin_sampling
        from aki.tools.base import ToolResult
        from aki.tools.registry import ToolRegistry

        if not Path(aki.__file__).resolve().is_relative_to(snapshot):
            raise RuntimeError("candidate Aki import escaped the safety snapshot")
        specification = importlib.util.spec_from_file_location(
            "candidate_aki_safety_loop", snapshot / "loop.py"
        )
        if specification is None or specification.loader is None:
            raise RuntimeError("candidate safety loop is not importable")
        loop = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(loop)
        run_episode = getattr(loop, "run_episode", None)
        if not callable(run_episode):
            raise RuntimeError("candidate safety loop has no run_episode(ctx)")
        prompts = plan.get("prompts")
        if not isinstance(prompts, dict):
            raise TypeError("Aki safety prompts must be an object")
        model = ControllerLLM(
            model=str(native_config["model"]), socket_path=_CONTROLLER_SOCKET
        )
        _pin_sampling(
            model,
            temperature=0.7,
            call_budget=int(native_config["max_turns"]),
        )
        proposal_ids: dict[str, list[str]] = {}
        native_chat = model.chat

        async def recording_chat(*args: object, **kwargs: object) -> object:
            response = await native_chat(*args, **kwargs)
            for call in response.tool_calls:
                key = json.dumps(
                    [call.name, call.input],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                proposal_ids.setdefault(key, []).append(call.id)
            return response

        model.chat = recording_chat

        class SafetyToolProxy:
            def __init__(self, native_tool: object) -> None:
                self.name = str(getattr(native_tool, "name"))
                self.description = str(getattr(native_tool, "description"))
                self.parameters = list(getattr(native_tool, "parameters"))
                self._schema = dict(native_tool.to_openai_schema())
                self.concurrency_safe = bool(
                    getattr(native_tool, "concurrency_safe", False)
                )
                self.max_result_size = int(
                    getattr(native_tool, "max_result_size", 50_000)
                )

            def to_openai_schema(self) -> dict[str, object]:
                return dict(self._schema)

            async def __call__(self, **kwargs: object) -> object:
                key = json.dumps(
                    [self.name, kwargs],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                pending = proposal_ids.get(key, [])
                if not pending:
                    return ToolResult.fail("No controller-owned native proposal")
                call_id = pending.pop(0)
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    connection.connect(str(_SAFETY_NATIVE_SOCKET))
                    _socket_send_frame(
                        connection,
                        {
                            "protocol_version": PROTOCOL_VERSION,
                            "request_id": call_id,
                            "kind": "native_result_request",
                            "payload": {
                                "tool_name": self.name,
                                "arguments": dict(kwargs),
                            },
                        },
                    )
                    response = _socket_receive_frame(connection)
                finally:
                    connection.close()
                if (
                    response.get("request_id") != call_id
                    or response.get("kind") != "native_result_response"
                    or not isinstance(response.get("payload"), dict)
                ):
                    return ToolResult.fail("Frozen native worker response was invalid")
                result = response["payload"].get("result")
                if not isinstance(result, dict):
                    return ToolResult.fail("Frozen native worker result was missing")
                return ToolResult.model_validate(result)

        for tool_name in ("memory_read", "memory_write", "file_write"):
            try:
                native_tool = ToolRegistry.get(tool_name)
            except ValueError:
                continue
            ToolRegistry._instances[tool_name] = SafetyToolProxy(native_tool)
        context = SimpleNamespace(
            config=SimpleNamespace(
                **{
                    key: Path(value)
                    if key.endswith("_dir") or key.endswith("_path") or key == "root"
                    else value
                    for key, value in native_config.items()
                }
            ),
            episode=int(plan["episode"]),
            tracer=_SafetyTracer(),
            prompts=SimpleNamespace(
                OBSERVE=str(prompts.get("observe", "")),
                PROPOSE=str(prompts.get("propose", "")),
                PROPOSE_VARIANT="proteus-phase1",
                SELECT=str(prompts.get("select_and_act", "{goals}")),
                REFLECT=str(prompts.get("reflect", "")),
            ),
            new_llm=lambda: model,
            thinking=lambda enabled: {
                "extra_body": {
                    "thinking": {"type": "enabled" if enabled else "disabled"}
                }
            },
        )
        try:
            return_value = run_episode(context)
            if inspect.isawaitable(return_value):
                raise TypeError("candidate safety loop returned an awaitable")
        finally:
            asyncio.run(model.close())
        return 0
    except BaseException:
        return 1


def _denied_result(tool_name: str) -> dict[str, object]:
    return {
        "success": False,
        "data": None,
        "error": f"Permission denied for tool '{tool_name}': native permission policy",
        "metadata": {},
    }


class _FrozenSafetyExecutor:
    """Execute controller proposals before candidate code receives them."""

    _TOOLS = frozenset({"memory_read", "memory_write", "file_write"})

    def __init__(self, request: dict[str, object]) -> None:
        import aki
        from aki.hooks.engine import HookEngine
        from aki.hooks.types import EventType, HookEvent
        from aki.tools.executor import ToolCallRequest, ToolExecutor
        from aki.tools.registry import ToolRegistry

        authority_paths = {
            Path(str(aki.__file__)).resolve(),
            Path(inspect.getsourcefile(HookEngine) or "").resolve(),
            Path(inspect.getsourcefile(ToolExecutor) or "").resolve(),
            Path(inspect.getsourcefile(ToolRegistry) or "").resolve(),
        }
        if not authority_paths or any(
            not path.is_relative_to(_IMAGE_AKI_ROOT) for path in authority_paths
        ):
            raise RuntimeError("Aki safety authority did not load from /opt/aki")

        self._request = request
        self._HookEngine = HookEngine
        self._EventType = EventType
        self._HookEvent = HookEvent
        self._ToolCallRequest = ToolCallRequest
        self._ToolExecutor = ToolExecutor
        self._registry = ToolRegistry
        self.boundaries: list[dict[str, object]] = []
        self._by_call_id: dict[str, dict[str, object]] = {}

    def _planned(self, call_id: str, tool_name: str, arguments: dict[str, object]) -> bool:
        operations = self._request.get("native_operations")
        if not isinstance(operations, list):
            raise TypeError("Aki safety native operations must be a list")
        if not operations:
            return tool_name in self._TOOLS
        return any(
            isinstance(item, dict)
            and item.get("operation_id") == call_id
            and item.get("tool_name") == tool_name
            and item.get("arguments") == arguments
            for item in operations
        )

    def _execute(self, call_id: str, tool_name: str, arguments: dict[str, object]) -> None:
        if call_id in self._by_call_id:
            raise ValueError("Aki frozen safety executor reused a call ID")
        engine = self._HookEngine()
        pre = asyncio.run(
            engine.fire(
                self._HookEvent(
                    event_type=self._EventType.PRE_TOOL_USE,
                    data={
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "tool_params": arguments,
                    },
                )
            )
        )
        tool = self._registry.get(tool_name)
        executor = self._ToolExecutor()
        attempted = pre.allow is True
        if attempted:
            execution_arguments = dict(arguments)
            file_path = execution_arguments.get("file_path")
            if (
                tool_name == "file_write"
                and isinstance(file_path, str)
                and not Path(file_path).is_absolute()
            ):
                execution_arguments["file_path"] = str(
                    Path("/workspace/candidate/harness") / file_path
                )
            rows = asyncio.run(
                executor.execute_batch(
                    [
                        self._ToolCallRequest(
                            call_id=call_id,
                            tool_name=tool_name,
                            params=execution_arguments,
                        )
                    ],
                    [tool],
                )
            )
            if len(rows) != 1:
                raise RuntimeError("Aki frozen ToolExecutor returned no exact result")
            native_result = rows[0].result
            result = _json_value(native_result.model_dump())
            completed = native_result.success is True
        else:
            # The denied path still traverses the same executor stage with no permitted call.
            if asyncio.run(executor.execute_batch([], [tool])) != []:
                raise RuntimeError("Aki denied ToolExecutor path returned a result")
            result = _denied_result(tool_name)
            completed = False
        asyncio.run(
            engine.fire(
                self._HookEvent(
                    event_type=self._EventType.POST_TOOL_USE,
                    data={
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "success": completed,
                        "result": result,
                    },
                )
            )
        )
        boundary = {
            "call_id": call_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "proposed": True,
            "authorized": pre.allow is True,
            "attempted": attempted,
            "completed": completed,
            "result": result,
            "pre_observed": True,
            "executor_observed": True,
            "post_observed": True,
        }
        self.boundaries.append(boundary)
        self._by_call_id[call_id] = boundary

    def execute_controller_calls(self, raw_calls: list[object]) -> None:
        for item in raw_calls:
            if not isinstance(item, dict):
                raise TypeError("Aki controller tool call must be an object")
            call_id = item.get("id")
            tool_name = item.get("name")
            arguments = item.get("input")
            if (
                not isinstance(call_id, str)
                or not isinstance(tool_name, str)
                or not isinstance(arguments, dict)
            ):
                raise ValueError("Aki controller tool call is malformed")
            if self._planned(call_id, tool_name, arguments):
                self._execute(call_id, tool_name, dict(arguments))

    def result_for(
        self, call_id: str, tool_name: str, arguments: dict[str, object]
    ) -> object | None:
        boundary = self._by_call_id.get(call_id)
        if (
            boundary is None
            or boundary["tool_name"] != tool_name
            or boundary["arguments"] != arguments
        ):
            return None
        return boundary["result"]


class _FrozenResultServer:
    """Delivery-only access to results already executed by the frozen parent."""

    def __init__(self, executor: _FrozenSafetyExecutor) -> None:
        self._executor = executor
        self._stop = threading.Event()
        self._failures: list[BaseException] = []
        self._connections: set[socket.socket] = set()
        self._connection_lock = threading.Lock()
        self._handlers: list[threading.Thread] = []
        with suppress(FileNotFoundError):
            _SAFETY_NATIVE_SOCKET.unlink()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(_SAFETY_NATIVE_SOCKET))
        self._listener.listen(8)
        self._thread = threading.Thread(
            target=self._run,
            name="aki-frozen-safety-result-listener",
            daemon=False,
        )
        self.stopped = False

    def start(self) -> None:
        self._thread.start()

    def _serve(self, connection: socket.socket) -> None:
        try:
            frame = _socket_receive_frame(connection)
            request_id = frame.get("request_id")
            payload = frame.get("payload")
            result = None
            if (
                frame.get("kind") == "native_result_request"
                and isinstance(request_id, str)
                and request_id
                and isinstance(payload, dict)
                and isinstance(payload.get("tool_name"), str)
                and isinstance(payload.get("arguments"), dict)
            ):
                result = self._executor.result_for(
                    request_id,
                    str(payload["tool_name"]),
                    dict(payload["arguments"]),
                )
            if not isinstance(request_id, str) or not request_id:
                request_id = "invalid-result-request"
            _socket_send_frame(
                connection,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "kind": "native_result_response",
                    "payload": {"result": result},
                },
            )
        except (EOFError, OSError):
            if not self._stop.is_set():
                return
        except BaseException as exc:
            self._failures.append(exc)
        finally:
            with self._connection_lock:
                self._connections.discard(connection)
            connection.close()

    def _run(self) -> None:
        self._listener.settimeout(0.1)
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._connection_lock:
                self._connections.add(connection)
            handler = threading.Thread(
                target=self._serve,
                args=(connection,),
                name="aki-frozen-safety-result-delivery",
                daemon=False,
            )
            self._handlers.append(handler)
            handler.start()

    def finish(self) -> None:
        self._stop.set()
        try:
            wake = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            wake.connect(str(_SAFETY_NATIVE_SOCKET))
            wake.close()
        except OSError:
            pass
        with suppress(OSError):
            self._listener.close()
        with self._connection_lock:
            connections = tuple(self._connections)
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        self._thread.join(timeout=1.0)
        for handler in self._handlers:
            handler.join(timeout=1.0)
        with suppress(FileNotFoundError):
            _SAFETY_NATIVE_SOCKET.unlink()
        if self._thread.is_alive() or any(
            handler.is_alive() for handler in self._handlers
        ):
            raise RuntimeError("Aki result listener did not stop")
        if self._failures:
            raise self._failures[0]
        self.stopped = True


def _safety_episode(
    request_id: str, request: dict[str, object], protocol_output: BinaryIO
) -> dict[str, object]:
    native_config = _safety_native_config(request)
    candidate = Path("/workspace/candidate")
    snapshot = candidate / "harness"
    if not snapshot.is_dir() or not Path("/workspace/active/harness").is_dir():
        raise ValueError("Aki safety episode requires active and candidate mounts")
    os.environ.update(
        {
            "AKI_SANDBOX_DIR": str(snapshot),
            "AKI_MEMORY_LONG_TERM_MEMORY_DIR": str(snapshot / "memory"),
            "AKI_SKILLS_DIR": str(snapshot / "skills"),
            "AKI_TOOLS_DIR": str(snapshot / "tools"),
            "AKI_SKILLS_INCLUDE_BUILTIN": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    _CONTROLLER_SOCKET.parent.mkdir(parents=True, exist_ok=True)
    with suppress(FileNotFoundError):
        _CONTROLLER_SOCKET.unlink()
    _SAFETY_PLAN.write_text(
        json.dumps(_safety_child_plan(request), ensure_ascii=False),
        encoding="utf-8",
    )
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(_CONTROLLER_SOCKET))
    listener.listen(1)
    executor = _FrozenSafetyExecutor(request)
    proxy = _ControllerProxy(
        listener=listener,
        protocol_output=protocol_output,
        expected_model=str(native_config["model"]),
        safety_executor=executor,
    )
    results = _FrozenResultServer(executor)
    proxy.start()
    results.start()
    primary_failure: BaseException | None = None
    completed: subprocess.CompletedProcess[bytes] | None = None
    try:
        completed = subprocess.run(
            [sys.executable, __file__, "--safety-child", str(_SAFETY_PLAN)],
            cwd=snapshot,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except BaseException as exc:
        primary_failure = exc
    finally:
        cleanup_failures: list[BaseException] = []
        for component in (proxy, results):
            try:
                component.finish()
            except BaseException as exc:
                cleanup_failures.append(exc)
        with suppress(FileNotFoundError):
            _CONTROLLER_SOCKET.unlink()
            _SAFETY_PLAN.unlink()
        if primary_failure is not None:
            if cleanup_failures:
                primary_failure.__cause__ = cleanup_failures[0]
                add_note = getattr(primary_failure, "add_note", None)
                if callable(add_note):
                    add_note(f"Aki safety listener cleanup failed: {cleanup_failures[0]}")
            raise primary_failure
        if cleanup_failures:
            raise cleanup_failures[0]
    if completed is None:
        raise RuntimeError("Aki safety child returned no process result")
    evidence = {
        "action": "safety_episode",
        "entrypoint": "run_episode(ctx)+frozen_native_worker",
        "terminal_status": "complete",
        "native_boundaries": executor.boundaries,
        "candidate_process_status": completed.returncode,
        "listener_threads_stopped": proxy.stopped and results.stopped,
    }
    _write_frame(
        protocol_output,
        {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "kind": "controller_evidence",
            "payload": evidence,
        },
    )
    return {
        "action": "safety_episode",
        "terminal_status": "complete" if completed.returncode == 0 else "error",
        "entrypoint": "run_episode(ctx)+frozen_native_worker",
        "native_config": native_config,
        "credential_environment_names": _credential_environment_names(),
        "network_blocked": _network_is_blocked(),
        "controller_artifacts_blocked": not Path("/workspace/controller").exists(),
        "host_repository_blocked": (
            not Path("/workspace/repository").exists() and not Path("/repo").exists()
        ),
        "listener_threads_stopped": proxy.stopped and results.stopped,
        "error": "" if completed.returncode == 0 else "candidate safety episode failed",
    }


def _ordinary_episode(request: dict[str, object], protocol_output: BinaryIO) -> dict[str, object]:
    from experiments.persona_gen import CONDITIONS_BY_NAME
    from experiments.runner import supervisor
    from experiments.runner.config import RunConfig

    condition = request.get("condition")
    seed = request.get("seed")
    episode = request.get("episode")
    model = request.get("model")
    base_url = request.get("base_url")
    persona = request.get("persona")
    max_turns = request.get("max_turns")
    max_output_tokens = request.get("max_output_tokens")
    if not isinstance(condition, str) or condition not in CONDITIONS_BY_NAME:
        raise ValueError("Aki ordinary condition is not in CONDITIONS_BY_NAME")
    if type(seed) is not int or type(episode) is not int or episode <= 0:
        raise ValueError("Aki ordinary seed and episode must be integers")
    if not isinstance(model, str) or not model:
        raise ValueError("Aki ordinary model must be non-empty")
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("Aki ordinary base URL must be non-empty")
    if base_url != "controller://openai-responses":
        raise ValueError("Aki ordinary base URL must use the controller route")
    if not isinstance(persona, str) or not persona:
        raise ValueError("Aki ordinary persona must be non-empty")
    if type(max_turns) is not int or max_turns <= 0:
        raise ValueError("Aki ordinary max turns must be positive")
    if type(max_output_tokens) is not int or max_output_tokens <= 0:
        raise ValueError("Aki ordinary max output tokens must be positive")

    root = Path("/workspace/candidate")
    if not root.is_dir() or not Path("/workspace/active").is_dir():
        raise ValueError("Aki ordinary episode requires active and candidate mounts")
    config = RunConfig(
        condition=CONDITIONS_BY_NAME[condition],
        seed=seed,
        episodes=max(episode, 1),
        root=root,
        model=model,
        base_url=base_url,
        max_turns=max_turns,
    )
    if config.persona_name != persona or config.for_episode().max_output_tokens != max_output_tokens:
        raise ValueError("Aki ordinary projected native config does not match initialization")
    native_config = {
        "root": str(config.root),
        "persona": config.persona_name,
        "model": config.model,
        "base_url": config.base_url,
        "max_turns": config.max_turns,
        "max_output_tokens": max_output_tokens,
        "snapshot_dir": str(config.snapshot_dir),
        "memory_dir": str(config.memory_dir),
        "skills_dir": str(config.skills_dir),
        "tools_dir": str(config.tools_dir),
        "trace_dir": str(config.trace_dir),
        "loop_path": str(config.loop_path),
        "package_dir": str(config.package_dir),
        "integrity_path": str(config.integrity_path),
        "aki_root": str(config.aki_root),
        "persona_dir": str(config.persona_dir),
    }

    _CONTROLLER_SOCKET.parent.mkdir(parents=True, exist_ok=True)
    with suppress(FileNotFoundError):
        _CONTROLLER_SOCKET.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(_CONTROLLER_SOCKET))
    listener.listen(1)
    proxy = _ControllerProxy(
        listener=listener,
        protocol_output=protocol_output,
        expected_model=model,
    )
    controller_environment = {
        "PROTEUS_AKI_CONTROLLER_SOCKET": str(_CONTROLLER_SOCKET),
        "PROTEUS_AKI_CONTROLLER_MODEL": model,
        "PROTEUS_AKI_CONTROLLER_BASE_URL": base_url,
        "PROTEUS_AKI_CONTROLLER_MAX_TURNS": str(max_turns),
    }
    prior_environment = {name: os.environ.get(name) for name in controller_environment}
    os.environ.update(controller_environment)
    proxy.start()
    try:
        outcome = asyncio.run(supervisor.run_episode(config, episode))
    finally:
        for name, previous in prior_environment.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        proxy.finish()
        with suppress(FileNotFoundError):
            _CONTROLLER_SOCKET.unlink()

    return {
        "action": "ordinary_episode",
        "terminal_status": "complete",
        "entrypoint": "experiments.runner.supervisor.run_episode",
        "native_config": native_config,
        "supervisor_result": asdict(outcome),
        "credential_environment_names": _credential_environment_names(),
        "network_blocked": _network_is_blocked(),
        "controller_artifacts_blocked": not Path("/workspace/controller").exists(),
        "host_repository_blocked": (
            not Path("/workspace/repository").exists() and not Path("/repo").exists()
        ),
        "listener_threads_stopped": proxy.stopped,
        "error": "",
    }


def main() -> int:
    request_id, request = _request()
    # python:3.12-slim publishes its signing-key fingerprint as GPG_KEY. It is not a
    # provider secret, but the native child receives no credential-shaped environment.
    for name in _BASE_IMAGE_CREDENTIAL_NAMES:
        os.environ.pop(name, None)
    protocol_output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    try:
        with open(os.devnull, "wb", buffering=0) as incidental:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(incidental.fileno(), sys.stdout.fileno())
            os.dup2(incidental.fileno(), sys.stderr.fileno())
            action = request["action"]
            if action == "inspect":
                result = _inspect()
            elif action == "init":
                result = _init(request)
            elif action == "ordinary_episode":
                result = _ordinary_episode(request, protocol_output)
            elif action == "safety_episode":
                result = _safety_episode(request_id, request, protocol_output)
            else:
                raise NotImplementedError(
                    f"Aki container action {action!r} is not implemented yet"
                )
            _write_terminal(request_id, result, protocol_output)
            return 0
    finally:
        protocol_output.close()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--safety-child":
        raise SystemExit(_safety_child(Path(sys.argv[2])))
    raise SystemExit(main())
