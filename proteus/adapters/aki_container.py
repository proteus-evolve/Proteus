"""Host controller for the contained Aki native runtime."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Mapping

from proteus.adapters.aki_live_worker import (
    AkiWorkerResult,
    BoundaryRecord,
    BrokerCallRecord,
    ModelToolLink,
)
from proteus.sandbox import DockerSandbox
from proteus.safety.live import (
    BoundedLiveModelChannel,
    LiveCallProvenance,
    LiveModelChannel,
    LiveModelRequestOptions,
    LiveModelResponse,
)


FRAME_HEADER_BYTES = 8
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 32 * 1024 * 1024
AKI_CONTROLLER_BASE_URL = "controller://openai-responses"
AKI_TEMPERATURE = 0.7
_ORDINARY_ENTRYPOINT = "experiments.runner.supervisor.run_episode"


def _annotate_cleanup_failure(
    primary: BaseException, cleanup: BaseException
) -> BaseException:
    """Attach cleanup context without requiring Python 3.11 ``add_note``."""
    context = f"Aki abort cleanup failed: {cleanup}"
    setattr(primary, "cleanup_context", context)
    primary.__cause__ = cleanup
    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        add_note(context)
    return primary


@dataclass(frozen=True)
class AkiContainerPlan:
    """One action executed by the image-owned Aki entrypoint."""

    action: Literal["inspect", "init", "ordinary_episode", "safety_episode"]
    payload: Mapping[str, object]


@dataclass
class _ToolLinkState:
    native_request_id: str
    call_id: str
    name: str
    arguments: dict[str, object]
    provenance: LiveCallProvenance
    assistant_reproduced: bool = False
    result_delivered: bool = False
    function_output: object = None

    def freeze(self) -> ModelToolLink:
        return ModelToolLink(
            native_request_id=self.native_request_id,
            call_id=self.call_id,
            name=self.name,
            arguments=dict(self.arguments),
            provenance=self.provenance,
            assistant_reproduced=self.assistant_reproduced,
            result_delivered=self.result_delivered,
            function_output=self.function_output,
            native_completion_observed=False,
        )


def encode_frame(payload: Mapping[str, object]) -> bytes:
    """Encode one compact JSON object with an unsigned big-endian length prefix."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(body).to_bytes(FRAME_HEADER_BYTES, "big") + body


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError(f"Aki container frame ended with {remaining} bytes missing")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def decode_frame(stream: BinaryIO, *, max_bytes: int) -> dict[str, object]:
    """Read one bounded length-prefixed JSON object."""
    size = int.from_bytes(_read_exact(stream, FRAME_HEADER_BYTES), "big")
    if size <= 0 or size > max_bytes:
        raise ValueError(f"invalid Aki container frame size {size}")
    value = json.loads(_read_exact(stream, size))
    if not isinstance(value, dict):
        raise TypeError("Aki container frame payload must be a JSON object")
    version = value.get("protocol_version")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ValueError("unsupported Aki container protocol version")
    return value


class AkiContainerController:
    """Own the framed request/terminal exchange for one contained Aki action."""

    def __init__(self, sandbox: DockerSandbox) -> None:
        self.sandbox = sandbox

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("Aki container action", 0)
        return remaining

    def run_once(
        self,
        *,
        run_root: Path,
        plan: AkiContainerPlan,
        mounts: tuple[tuple[str, ...], ...],
        timeout_s: float,
    ) -> Mapping[str, object]:
        request_id = uuid.uuid4().hex
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "kind": "request",
            "payload": {**dict(plan.payload), "action": plan.action},
        }
        encoded = encode_frame(request)
        if len(encoded) - FRAME_HEADER_BYTES > MAX_FRAME_BYTES:
            raise ValueError("Aki container request exceeds the frame limit")

        session = self.sandbox.open_session(run_root, [], {}, mounts)
        finished = False
        try:
            deadline = time.monotonic() + timeout_s
            session.write(encoded)
            header = session.read_exact(
                FRAME_HEADER_BYTES, timeout_s=self._remaining(deadline)
            )
            size = int.from_bytes(header, "big")
            if size <= 0 or size > MAX_FRAME_BYTES:
                raise ValueError(f"invalid Aki container frame size {size}")
            body = session.read_exact(size, timeout_s=self._remaining(deadline))
            raw_terminal = header + body
            terminal = decode_frame(io.BytesIO(raw_terminal), max_bytes=MAX_FRAME_BYTES)
            if terminal.get("request_id") != request_id:
                raise ValueError("Aki container terminal request ID does not match")
            if terminal.get("kind") != "terminal":
                raise ValueError("Aki container response is not a terminal frame")
            payload = terminal.get("payload")
            if not isinstance(payload, dict):
                raise TypeError("Aki container terminal payload must be a JSON object")

            session.close_input()
            completed = session.finish(timeout_s=self._remaining(deadline))
            finished = True
            if completed.returncode != 0:
                error = completed.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"Aki container action exited with {completed.returncode}: {error}"
                )
            if completed.stdout != raw_terminal:
                raise ValueError("Aki container emitted data outside its terminal frame")
            return payload
        except BaseException as primary:
            if not finished:
                try:
                    session.abort()
                except BaseException as cleanup_error:
                    _annotate_cleanup_failure(primary, cleanup_error)
            raise

    @staticmethod
    def _responses_tools(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            raise TypeError("Aki native model tools must be a list")
        tools: list[dict[str, object]] = []
        for item in value:
            if not isinstance(item, dict):
                raise TypeError("Aki native model tool must be an object")
            function = item.get("function")
            if item.get("type") != "function" or not isinstance(function, dict):
                raise ValueError("Aki native model tool must be a function")
            tools.append({"type": "function", **function})
        return tools

    @staticmethod
    def _responses_input(messages: object) -> tuple[str, list[dict[str, object]]]:
        if not isinstance(messages, list):
            raise TypeError("Aki native model messages must be a list")
        instructions: list[str] = []
        inputs: list[dict[str, object]] = []
        for message in messages:
            if not isinstance(message, dict):
                raise TypeError("Aki native model message must be an object")
            role = message.get("role")
            content = message.get("content")
            if role == "system":
                instructions.append(str(content or ""))
            elif role in {"user", "assistant"} and content:
                inputs.append({"role": str(role), "content": str(content)})
            if role == "assistant" and isinstance(message.get("tool_calls"), list):
                for call in message["tool_calls"]:
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(call, dict) or not isinstance(function, dict):
                        raise TypeError("Aki native tool call must be an object")
                    inputs.append(
                        {
                            "type": "function_call",
                            "call_id": str(call.get("id", "")),
                            "name": str(function.get("name", "")),
                            "arguments": str(function.get("arguments", "{}")),
                        }
                    )
            elif role == "tool":
                inputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(message.get("tool_call_id", "")),
                        "output": str(content or ""),
                    }
                )
        return "\n\n".join(instructions), inputs

    @staticmethod
    def _request_options(
        payload: Mapping[str, object], *, expected_max_output_tokens: int
    ) -> LiveModelRequestOptions:
        max_tokens = payload.get("max_tokens")
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("Aki native max tokens must be a positive integer")
        if max_tokens != expected_max_output_tokens:
            raise ValueError("Aki native max tokens do not match the episode configuration")
        temperature = payload.get("temperature")
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or temperature != AKI_TEMPERATURE
        ):
            raise ValueError("Aki native temperature is unsupported")
        kwargs = payload.get("kwargs")
        if not isinstance(kwargs, dict) or set(kwargs) != {"extra_body"}:
            raise ValueError("Aki native thinking controls are unsupported")
        extra_body = kwargs.get("extra_body")
        if not isinstance(extra_body, dict) or set(extra_body) != {"thinking"}:
            raise ValueError("Aki native thinking controls are unsupported")
        thinking = extra_body.get("thinking")
        if not isinstance(thinking, dict) or set(thinking) != {"type"}:
            raise ValueError("Aki native thinking controls are unsupported")
        thinking_type = thinking.get("type")
        if thinking_type not in {"disabled", "enabled"}:
            raise ValueError("Aki native thinking controls are unsupported")
        return LiveModelRequestOptions(
            max_output_tokens=max_tokens,
            temperature=float(temperature) if thinking_type == "disabled" else None,
            reasoning_effort="none" if thinking_type == "disabled" else "medium",
        )

    @staticmethod
    def _normalized_json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def _native_history(
        cls, messages: object
    ) -> tuple[dict[str, tuple[str, dict[str, object]]], dict[str, object]]:
        if not isinstance(messages, list):
            raise TypeError("Aki native model messages must be a list")
        assistant_calls: dict[str, tuple[str, dict[str, object]]] = {}
        outputs: dict[str, object] = {}
        for message in messages:
            if not isinstance(message, dict):
                raise TypeError("Aki native model message must be an object")
            if message.get("role") == "assistant":
                calls = message.get("tool_calls")
                if calls is None:
                    continue
                if not isinstance(calls, list):
                    raise TypeError("Aki native assistant tool calls must be a list")
                for call in calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    call_id = call.get("id") if isinstance(call, dict) else None
                    if not isinstance(call_id, str) or not call_id or not isinstance(function, dict):
                        raise ValueError("Aki native assistant tool call is malformed")
                    if call_id in assistant_calls:
                        raise ValueError("Aki native assistant reused a tool call ID")
                    name = function.get("name")
                    raw_arguments = function.get("arguments")
                    if not isinstance(name, str) or not name or not isinstance(raw_arguments, str):
                        raise ValueError("Aki native assistant tool call is malformed")
                    try:
                        arguments = json.loads(raw_arguments)
                    except json.JSONDecodeError:
                        raise ValueError("Aki native assistant tool arguments are invalid") from None
                    if not isinstance(arguments, dict):
                        raise ValueError("Aki native assistant tool arguments must be an object")
                    assistant_calls[call_id] = (name, arguments)
            elif message.get("role") == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("Aki native function output needs a call ID")
                if call_id in outputs:
                    raise ValueError("Aki native function output ID was reused")
                raw_output = message.get("content")
                if not isinstance(raw_output, str):
                    raise ValueError("Aki native function output must be text")
                try:
                    outputs[call_id] = json.loads(raw_output)
                except json.JSONDecodeError:
                    outputs[call_id] = raw_output
        return assistant_calls, outputs

    @classmethod
    def _validate_native_history(
        cls,
        messages: object,
        links: dict[str, _ToolLinkState],
    ) -> None:
        assistant_calls, outputs = cls._native_history(messages)
        for call_id, (name, arguments) in assistant_calls.items():
            link = links.get(call_id)
            if link is None:
                raise ValueError("Aki native assistant reproduced an unknown tool call")
            if name != link.name:
                raise ValueError("Aki native assistant tool name does not match the controller")
            if cls._normalized_json(arguments) != cls._normalized_json(link.arguments):
                raise ValueError("Aki native assistant tool arguments do not match the controller")
            link.assistant_reproduced = True
        for call_id, output in outputs.items():
            link = links.get(call_id)
            if link is None or call_id not in assistant_calls:
                raise ValueError("Aki native function output has no controller tool call")
            if link.result_delivered and cls._normalized_json(
                link.function_output
            ) != cls._normalized_json(output):
                raise ValueError("Aki candidate-delivered function output changed")
            link.function_output = output
            link.result_delivered = True

    @staticmethod
    def _trace_path(run_root: Path, episode: int) -> Path:
        return Path(run_root) / "traces" / f"ep{episode:03d}.jsonl"

    @classmethod
    def _trace_events(cls, run_root: Path, episode: int) -> list[dict[str, object]]:
        path = cls._trace_path(run_root, episode)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events: list[dict[str, object]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    @classmethod
    def _ordinary_tool_link_is_valid(
        cls,
        *,
        trace: list[dict[str, object]],
        link: _ToolLinkState,
        later_same_session_request_observed: bool,
    ) -> bool:
        """Require delivery except for the final request in one broker session."""
        call_rows = [
            (index, event.get("data"))
            for index, event in enumerate(trace)
            if event.get("event") == "tool_call"
            and isinstance(event.get("data"), dict)
            and event["data"].get("call_id") == link.call_id
        ]
        result_rows = [
            (index, event.get("data"))
            for index, event in enumerate(trace)
            if event.get("event") == "tool_result"
            and isinstance(event.get("data"), dict)
            and event["data"].get("call_id") == link.call_id
        ]
        if len(call_rows) != 1 or len(result_rows) != 1:
            return False
        call_index, call = call_rows[0]
        result_index, result = result_rows[0]
        if (
            result_index <= call_index
            or call.get("tool_name") != link.name
            or result.get("tool_name") != link.name
            or not isinstance(call.get("params"), dict)
            or cls._normalized_json(call["params"]) != cls._normalized_json(link.arguments)
            or "result" not in result
        ):
            return False
        if link.result_delivered:
            return bool(
                link.assistant_reproduced
                and cls._normalized_json(link.function_output)
                == cls._normalized_json(result["result"])
            )
        if later_same_session_request_observed:
            return False

        for event in trace[result_index + 1 :]:
            kind = event.get("event")
            data = event.get("data")
            if kind == "llm_call":
                return False
            if kind == "session_end":
                return bool(isinstance(data, dict) and data.get("status") == "maximum_iterations")
        return False

    @classmethod
    def _broker_native_session_key(cls, input_value: object) -> str | None:
        """Identify one native run-turn from its controller-recorded input prefix."""
        if not isinstance(input_value, list):
            return None
        prefix: list[dict[str, object]] = []
        for item in input_value:
            if not isinstance(item, dict):
                return None
            if item.get("type") in {"function_call", "function_call_output"}:
                break
            role = item.get("role")
            if role in {"user", "assistant"}:
                prefix.append({"role": role, "content": item.get("content", "")})
        if not prefix:
            return None
        return cls._normalized_json(prefix)

    @staticmethod
    def _model_response_payload(response: LiveModelResponse) -> dict[str, object]:
        if response.usage is None:
            raise ValueError("Aki controller response has no measured usage")
        return {
            "content": response.output_text,
            "model": response.model,
            "tool_calls": [
                {
                    "id": call.call_id,
                    "name": call.name,
                    "input": dict(call.arguments),
                }
                for call in response.tool_calls
            ],
            "metadata": {
                "raw_tool_calls": [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                dict(call.arguments),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                    for call in response.tool_calls
                ],
            },
            "usage": {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
            },
        }

    @staticmethod
    def _call_channel(
        *,
        channel: BoundedLiveModelChannel,
        input_value: list[dict[str, object]],
        instructions: str,
        tools: list[dict[str, object]],
        options: LiveModelRequestOptions,
        timeout_s: float,
        on_timeout,
    ) -> LiveModelResponse:
        try:
            return channel.respond_bounded(
                input=input_value,
                instructions=instructions,
                tools=tools,
                options=options,
                timeout_s=timeout_s,
            )
        except TimeoutError:
            cleanup_error: BaseException | None = None
            try:
                on_timeout()
            except BaseException as exc:
                cleanup_error = exc
            failure = subprocess.TimeoutExpired("Aki controller model call", timeout_s)
            if cleanup_error is not None:
                _annotate_cleanup_failure(failure, cleanup_error)
            raise failure

    @staticmethod
    def _validate_model_response(
        response: LiveModelResponse, *, expected_model: str
    ) -> None:
        provenance = response.provenance
        if response.model != expected_model or provenance.response_model != expected_model:
            raise ValueError("Aki controller response model does not match the requested model")
        if provenance.configured_model != expected_model:
            raise ValueError("Aki controller provenance configured model does not match")
        if response.response_id != provenance.response_id:
            raise ValueError("Aki controller response provenance ID does not match")
        if not provenance.call_id or not response.response_id:
            raise ValueError("Aki controller response provenance IDs must be non-empty")
        if response.usage is None:
            raise ValueError("Aki controller response has no measured usage")

    @staticmethod
    def _expected_ordinary_config(payload: Mapping[str, object]) -> dict[str, object]:
        required = {
            "persona": payload.get("persona"),
            "model": payload.get("model"),
            "base_url": payload.get("base_url"),
            "max_turns": payload.get("max_turns"),
            "max_output_tokens": payload.get("max_output_tokens"),
        }
        if (
            not all(isinstance(required[name], str) and required[name] for name in (
                "persona",
                "model",
                "base_url",
            ))
            or type(required["max_turns"]) is not int
            or required["max_turns"] <= 0
            or type(required["max_output_tokens"]) is not int
            or required["max_output_tokens"] <= 0
        ):
            raise ValueError("Aki ordinary episode plan has incomplete native config")
        root = "/workspace/candidate"
        return {
            "root": root,
            **required,
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

    @classmethod
    def _validate_safety_evidence(
        cls,
        *,
        evidence: Mapping[str, object],
        links: dict[str, _ToolLinkState],
        plan: AkiContainerPlan,
    ) -> tuple[tuple[BoundaryRecord, ...], bool]:
        if evidence.get("action") != "safety_episode":
            raise ValueError("Aki safety evidence action is invalid")
        if evidence.get("terminal_status") != "complete":
            raise ValueError("Aki safety evidence is incomplete")
        if evidence.get("entrypoint") != "run_episode(ctx)+frozen_native_worker":
            raise ValueError("Aki safety evidence entrypoint is invalid")
        if type(evidence.get("candidate_process_status")) is not int:
            raise ValueError("Aki safety candidate status is missing")
        raw_boundaries = evidence.get("native_boundaries")
        if not isinstance(raw_boundaries, list):
            raise TypeError("Aki safety boundaries must be a list")
        boundaries: list[BoundaryRecord] = []
        structurally_complete = (
            evidence.get("candidate_process_status") == 0
            and evidence.get("listener_threads_stopped") is True
        )
        operations = plan.payload.get("native_operations")
        if not isinstance(operations, list):
            raise TypeError("Aki safety plan operations must be a list")

        def identity(call_id: str, name: str, arguments: Mapping[str, object]):
            return call_id, name, cls._normalized_json(arguments)

        link_identities = {
            identity(call_id, link.name, link.arguments)
            for call_id, link in links.items()
            if link.name in {"memory_read", "memory_write", "file_write"}
        }
        if operations:
            planned_identities = set()
            for operation in operations:
                if not isinstance(operation, dict):
                    raise TypeError("Aki planned safety operation must be an object")
                call_id = operation.get("operation_id")
                name = operation.get("tool_name")
                arguments = operation.get("arguments")
                if (
                    not isinstance(call_id, str)
                    or not isinstance(name, str)
                    or not isinstance(arguments, dict)
                ):
                    raise ValueError("Aki planned safety operation is malformed")
                planned_identities.add(identity(call_id, name, arguments))
            expected_identities = planned_identities
            structurally_complete = (
                structurally_complete and link_identities == planned_identities
            )
        else:
            expected_identities = link_identities
        boundary_identities: set[tuple[str, str, str]] = set()
        seen: set[str] = set()
        for item in raw_boundaries:
            if not isinstance(item, dict):
                raise TypeError("Aki safety boundary must be an object")
            call_id = item.get("call_id")
            tool_name = item.get("tool_name")
            arguments = item.get("arguments")
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in seen
                or not isinstance(tool_name, str)
                or not tool_name
                or not isinstance(arguments, dict)
            ):
                raise ValueError("Aki safety boundary identity is malformed")
            seen.add(call_id)
            for field in (
                "proposed",
                "authorized",
                "attempted",
                "completed",
                "pre_observed",
                "executor_observed",
                "post_observed",
            ):
                if type(item.get(field)) is not bool:
                    raise ValueError(f"Aki safety boundary {field} must be Boolean")
            boundary_identities.add(identity(call_id, tool_name, arguments))
            link = links.get(call_id)
            linked_proposal = (
                link is not None
                and link.name == tool_name
                and cls._normalized_json(link.arguments)
                == cls._normalized_json(arguments)
            )
            if item["proposed"] is not linked_proposal:
                raise ValueError("Aki safety boundary proposal does not match controller input")
            result = item.get("result")
            result_delivered = bool(
                linked_proposal
                and link is not None
                and link.result_delivered
                and cls._normalized_json(link.function_output)
                == cls._normalized_json(result)
            )
            if item["completed"] and not item["attempted"]:
                raise ValueError("Aki frozen safety worker completed an unattempted operation")
            structurally_complete = (
                structurally_complete
                and linked_proposal
                and result is not None
                and result_delivered
                and item["pre_observed"] is True
                and item["executor_observed"] is True
                and item["post_observed"] is True
            )
            boundaries.append(
                BoundaryRecord(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    proposed=linked_proposal,
                    authorized=bool(item["authorized"]),
                    attempted=bool(item["attempted"]),
                    completed=bool(item["completed"]),
                    result_delivered=result_delivered,
                    result=result,
                    effect_id=str(item.get("effect_id", "")),
                    pre_observed=bool(item["pre_observed"]),
                    executor_observed=bool(item["executor_observed"]),
                    post_observed=bool(item["post_observed"]),
                )
            )
        structurally_complete = (
            structurally_complete
            and boundary_identities == expected_identities
            and len(boundaries) == len(expected_identities)
        )
        return tuple(boundaries), structurally_complete

    @classmethod
    def _validate_terminal_evidence(
        cls,
        *,
        run_root: Path,
        plan: AkiContainerPlan,
        terminal: Mapping[str, object],
        broker_calls: list[BrokerCallRecord],
        links: dict[str, _ToolLinkState],
        safety_evidence: Mapping[str, object] | None,
    ) -> tuple[
        list[dict[str, object]],
        dict[str, object],
        dict[str, object],
        tuple[BoundaryRecord, ...],
        bool,
    ]:
        if terminal.get("action") != plan.action:
            raise ValueError("Aki terminal action does not match the plan")
        if terminal.get("terminal_status") != "complete":
            raise ValueError("Aki terminal status is incomplete")
        if plan.action == "ordinary_episode" and terminal.get("entrypoint") != _ORDINARY_ENTRYPOINT:
            raise ValueError("Aki terminal entrypoint is not the native supervisor")
        native_config = terminal.get("native_config")
        expected_config = cls._expected_ordinary_config(plan.payload)
        if not isinstance(native_config, dict) or native_config != expected_config:
            raise ValueError("Aki terminal native config does not match the plan")
        credential_names = terminal.get("credential_environment_names")
        if credential_names != []:
            raise ValueError("Aki terminal credential environment is not empty")
        if terminal.get("network_blocked") is not True:
            raise ValueError("Aki terminal network containment is missing")
        if terminal.get("controller_artifacts_blocked") is not True:
            raise ValueError("Aki terminal controller artifacts are not blocked")
        if terminal.get("host_repository_blocked") is not True:
            raise ValueError("Aki worker can see an undeclared host repository path")
        if terminal.get("listener_threads_stopped") is not True:
            raise ValueError("Aki listener cleanup is incomplete")
        if not broker_calls:
            raise ValueError("Aki episode has no controller model provenance")

        call_ids = [call.provenance.call_id for call in broker_calls]
        response_ids = [call.provenance.response_id for call in broker_calls]
        if len(set(call_ids)) != len(call_ids) or len(set(response_ids)) != len(response_ids):
            raise ValueError("Aki controller provenance ID was reused")
        expected_model = plan.payload.get("model")
        if any(
            not provenance.call_id
            or not provenance.response_id
            or provenance.configured_model != expected_model
            or provenance.response_model != expected_model
            for provenance in (call.provenance for call in broker_calls)
        ):
            raise ValueError("Aki controller provenance is incomplete")

        if plan.action == "safety_episode":
            if safety_evidence is None:
                raise ValueError("Aki safety controller evidence is missing")
            boundaries, structurally_complete = cls._validate_safety_evidence(
                evidence=safety_evidence,
                links=links,
                plan=plan,
            )
            return [], {}, dict(native_config), boundaries, structurally_complete

        episode = plan.payload.get("episode")
        if type(episode) is not int or episode <= 0:
            raise ValueError("Aki ordinary episode plan has an invalid episode")
        trace = cls._trace_events(run_root, episode)
        if not trace:
            raise ValueError("Aki native trace is missing")
        statuses = [
            event.get("data")
            for event in trace
            if event.get("event") == "episode_status"
            and isinstance(event.get("data"), dict)
        ]
        endings = [
            event.get("data")
            for event in trace
            if event.get("event") == "episode_end" and isinstance(event.get("data"), dict)
        ]
        if len(statuses) != 1 or statuses[0].get("status") != "complete" or len(endings) != 1:
            raise ValueError("Aki native trace has no complete terminal status")
        counters = endings[0].get("counters")
        if not isinstance(counters, dict):
            raise ValueError("Aki native trace terminal counters are missing")

        supervisor = terminal.get("supervisor_result")
        if (
            not isinstance(supervisor, dict)
            or supervisor.get("episode") != episode
            or supervisor.get("subprocess_status") != "complete"
        ):
            raise ValueError("Aki native supervisor result is incomplete")
        rolled_back = supervisor.get("rolled_back")
        rejected_diff = supervisor.get("rejected_diff")
        viability = supervisor.get("viability")
        if type(rolled_back) is not bool:
            raise ValueError("Aki native rolled back evidence must be Boolean")
        if not isinstance(rejected_diff, str):
            raise ValueError("Aki native rollback diff evidence must be text")
        if not isinstance(viability, dict) or set(viability) != {
            "alive",
            "failures",
            "detail",
        }:
            raise ValueError("Aki native viability evidence is malformed")
        alive = viability.get("alive")
        failures = viability.get("failures")
        detail = viability.get("detail")
        if (
            type(alive) is not bool
            or not isinstance(failures, list)
            or not all(isinstance(item, str) and item for item in failures)
            or not isinstance(detail, dict)
        ):
            raise ValueError("Aki native viability evidence has invalid types")
        if alive and failures:
            raise ValueError("Aki live viability cannot contain failures")
        if not rolled_back and not alive:
            raise ValueError("Aki nonviable candidate was not rolled back")
        if rolled_back and not rejected_diff:
            raise ValueError("Aki rollback is missing its rejected diff evidence")
        expected_input = sum(call.usage.input_tokens for call in broker_calls if call.usage)
        expected_output = sum(call.usage.output_tokens for call in broker_calls if call.usage)
        if counters.get("tokens_in") != expected_input or counters.get(
            "tokens_out"
        ) != expected_output:
            raise ValueError("Aki native trace usage does not match controller measurements")
        if supervisor.get("tokens_in") != counters.get("tokens_in") or supervisor.get(
            "tokens_out"
        ) != counters.get("tokens_out"):
            raise ValueError("Aki native token counters do not match the terminal trace")

        native_request_positions = {
            call.native_request_id: index for index, call in enumerate(broker_calls)
        }
        native_session_keys = [
            cls._broker_native_session_key(call.input) for call in broker_calls
        ]
        for link in links.values():
            request_position = native_request_positions.get(link.native_request_id)
            native_session_key = (
                native_session_keys[request_position]
                if request_position is not None
                else None
            )
            if native_session_key is None or not cls._ordinary_tool_link_is_valid(
                trace=trace,
                link=link,
                later_same_session_request_observed=(
                    native_session_key in native_session_keys[request_position + 1 :]
                ),
            ):
                raise ValueError("Aki controller tool call has no exact later candidate delivery")
        return trace, dict(supervisor), dict(native_config), (), True

    def run_model_episode(
        self,
        *,
        run_root: Path,
        plan: AkiContainerPlan,
        channel: LiveModelChannel,
        mounts: tuple[tuple[str, ...], ...],
        episode_timeout_s: float,
        call_timeout_s: float,
    ) -> AkiWorkerResult:
        """Run one native episode while proxying one exact model request at a time."""
        if plan.action not in {"ordinary_episode", "safety_episode"}:
            raise ValueError("Aki model episode requires an episode action")
        if episode_timeout_s <= 0 or call_timeout_s <= 0:
            raise ValueError("Aki episode and model-call timeouts must be positive")
        expected_model = plan.payload.get("model")
        if not isinstance(expected_model, str) or not expected_model:
            raise ValueError("Aki model episode plan needs a model")
        if channel.model != expected_model:
            raise ValueError("Aki host channel model does not match the episode model")
        if not isinstance(channel, BoundedLiveModelChannel):
            raise TypeError("Aki host channel must implement bounded model calls")
        expected_native_config = self._expected_ordinary_config(plan.payload)
        if expected_native_config["base_url"] != AKI_CONTROLLER_BASE_URL:
            raise ValueError("Aki ordinary episode must use the controller base URL")
        if any(len(mount) >= 2 and mount[1] == "/state" for mount in mounts):
            raise ValueError("Aki controller owns the private /state mount")

        request_id = uuid.uuid4().hex
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "kind": "request",
            "payload": {**dict(plan.payload), "action": plan.action},
        }
        encoded_request = encode_frame(request)
        if len(encoded_request) - FRAME_HEADER_BYTES > MAX_FRAME_BYTES:
            raise ValueError("Aki container request exceeds the frame limit")

        with tempfile.TemporaryDirectory(prefix="proteus-aki-state-") as state_root:
            session = self.sandbox.open_session(
                run_root,
                [],
                {},
                (*mounts, (state_root, "/state")),
            )
            finished = False
            protocol_output = bytearray()
            broker_calls: list[BrokerCallRecord] = []
            links: dict[str, _ToolLinkState] = {}
            seen_request_ids: set[str] = set()
            seen_provenance_call_ids: set[str] = set()
            seen_provenance_response_ids: set[str] = set()
            try:
                deadline = time.monotonic() + episode_timeout_s
                session.write(encoded_request)
                terminal_payload: dict[str, object] | None = None
                safety_evidence: dict[str, object] | None = None
                while terminal_payload is None:
                    header = session.read_exact(
                        FRAME_HEADER_BYTES, timeout_s=self._remaining(deadline)
                    )
                    size = int.from_bytes(header, "big")
                    if size <= 0 or size > MAX_FRAME_BYTES:
                        raise ValueError(f"invalid Aki container frame size {size}")
                    body = session.read_exact(size, timeout_s=self._remaining(deadline))
                    raw_frame = header + body
                    protocol_output.extend(raw_frame)
                    frame = decode_frame(io.BytesIO(raw_frame), max_bytes=MAX_FRAME_BYTES)
                    frame_request_id = frame.get("request_id")
                    kind = frame.get("kind")
                    payload = frame.get("payload")
                    if not isinstance(frame_request_id, str) or not frame_request_id:
                        raise ValueError("Aki container frame needs a request ID")
                    if not isinstance(payload, dict):
                        raise TypeError("Aki container frame payload must be an object")
                    if kind == "terminal":
                        if frame_request_id != request_id:
                            raise ValueError("Aki container terminal request ID does not match")
                        terminal_payload = payload
                        continue
                    if kind == "controller_evidence":
                        if plan.action != "safety_episode":
                            raise ValueError(
                                "Aki model transport rejects controller evidence frames"
                            )
                        if frame_request_id != request_id:
                            raise ValueError(
                                "Aki safety evidence request ID does not match"
                            )
                        if safety_evidence is not None:
                            raise ValueError("Aki safety evidence frame was repeated")
                        safety_evidence = dict(payload)
                        continue
                    if kind != "model_request":
                        raise ValueError("Aki container emitted an unsupported protocol frame")
                    if frame_request_id in seen_request_ids:
                        raise ValueError("Aki model request ID was reused")
                    seen_request_ids.add(frame_request_id)
                    requested_model = payload.get("model")
                    if requested_model != expected_model:
                        raise ValueError("Aki native requested model does not match the plan")
                    messages = payload.get("messages")
                    episode = plan.payload.get("episode")
                    if type(episode) is not int:
                        raise ValueError("Aki ordinary episode plan has an invalid episode")
                    self._validate_native_history(
                        messages,
                        links,
                    )
                    instructions, input_value = self._responses_input(messages)
                    tools = self._responses_tools(payload.get("tools"))
                    options = self._request_options(
                        payload,
                        expected_max_output_tokens=int(
                            expected_native_config["max_output_tokens"]
                        ),
                    )
                    response = self._call_channel(
                        channel=channel,
                        input_value=input_value,
                        instructions=instructions,
                        tools=tools,
                        options=options,
                        timeout_s=min(call_timeout_s, self._remaining(deadline)),
                        on_timeout=session.abort,
                    )
                    self._validate_model_response(response, expected_model=expected_model)
                    provenance = response.provenance
                    if (
                        provenance.call_id in seen_provenance_call_ids
                        or provenance.response_id in seen_provenance_response_ids
                    ):
                        raise ValueError("Aki controller provenance ID was reused")
                    seen_provenance_call_ids.add(provenance.call_id)
                    seen_provenance_response_ids.add(provenance.response_id)
                    broker_calls.append(
                        BrokerCallRecord(
                            input=input_value,
                            tool_calls=tuple(response.tool_calls),
                            provenance=provenance,
                            native_request_id=frame_request_id,
                            usage=response.usage,
                        )
                    )
                    for call in response.tool_calls:
                        if call.call_id in links:
                            raise ValueError("Aki controller reused a tool call ID")
                        links[call.call_id] = _ToolLinkState(
                            native_request_id=frame_request_id,
                            call_id=call.call_id,
                            name=call.name,
                            arguments=dict(call.arguments),
                            provenance=provenance,
                        )
                    response_frame = {
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": frame_request_id,
                        "kind": "model_response",
                        "payload": self._model_response_payload(response),
                    }
                    session.write(encode_frame(response_frame))

                session.close_input()
                completed = session.finish(timeout_s=self._remaining(deadline))
                if completed.returncode != 0:
                    error = completed.stderr.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(
                        f"Aki container action exited with {completed.returncode}: {error}"
                    )
                if completed.stdout != bytes(protocol_output):
                    raise ValueError("Aki container emitted data outside its protocol frames")
                (
                    trace,
                    supervisor_result,
                    native_config,
                    boundaries,
                    structurally_complete,
                ) = self._validate_terminal_evidence(
                    run_root=run_root,
                    plan=plan,
                    terminal=terminal_payload,
                    broker_calls=broker_calls,
                    links=links,
                    safety_evidence=safety_evidence,
                )
                finished = True
                return AkiWorkerResult(
                    terminal=structurally_complete,
                    entrypoint=str(terminal_payload.get("entrypoint", "")),
                    events=tuple(trace),
                    model_inputs=tuple(
                        tuple(
                            item
                            for item in call.input
                            if isinstance(item, dict)
                        )
                        for call in broker_calls
                        if isinstance(call.input, list)
                    ),
                    model_provenance=tuple(call.provenance for call in broker_calls),
                    broker_calls=tuple(broker_calls),
                    tool_links=tuple(link.freeze() for link in links.values()),
                    boundaries=boundaries,
                    native_config=native_config,
                    supervisor_result=supervisor_result,
                    credential_environment_names=tuple(
                        str(item)
                        for item in terminal_payload.get(
                            "credential_environment_names", ()
                        )
                    ),
                    network_blocked=terminal_payload.get("network_blocked") is True,
                    controller_artifacts_blocked=(
                        terminal_payload.get("controller_artifacts_blocked") is True
                    ),
                    host_repository_blocked=(
                        terminal_payload.get("host_repository_blocked") is True
                    ),
                    structural_bijection_complete=structurally_complete,
                    listener_threads_stopped=(
                        terminal_payload.get("listener_threads_stopped") is True
                    ),
                    error=str(terminal_payload.get("error", "")),
                    containment="docker_network_none",
                )
            except BaseException as primary:
                if not finished:
                    try:
                        session.abort()
                    except BaseException as cleanup_error:
                        _annotate_cleanup_failure(primary, cleanup_error)
                raise
