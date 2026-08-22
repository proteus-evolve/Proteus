"""Child process that drives one canonical snapshot through ``run_episode(ctx)``.

This file deliberately imports no Aki modules until the materialized snapshot is first
on ``sys.path``. It is an evaluator worker, not a compatibility layer.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_PREFIX = "AKI_BEHAVIOR_RESULT="


def _credential_file_readable() -> bool:
    try:
        Path("/Users/liujiaen/Documents/Codes/Proteus/.env").read_bytes()
    except OSError:
        return False
    return True


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--broker-fd", type=int, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    workspace = args.workspace.resolve()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    os.chdir(workspace)
    sys.path.insert(0, str(workspace))

    import aki
    from aki.config.settings import reset_settings
    from aki.hooks.types import EventType, HookResult
    from aki.models.base import ModelResponse, ToolCall

    if not Path(aki.__file__).resolve().is_relative_to(workspace):
        raise SystemExit("snapshot execution resolved Aki outside the materialized workspace")
    reset_settings()

    class BrokeredModel:
        def __init__(self, broker_fd: int) -> None:
            self.channel = socket.socket(fileno=broker_fd)
            self.stream = self.channel.makefile("rwb")
            self.calls = 0
            self.inputs: list[list[dict[str, Any]]] = []
            self.responses: list[dict[str, Any]] = []

        async def chat(self, **kwargs: Any) -> ModelResponse:
            messages = kwargs.get("messages") or []
            self.inputs.append(_json_value(messages))
            self.calls += 1
            request = {
                "v": 1,
                "id": self.calls,
                "op": "chat",
                "messages": _json_value(messages),
                "tools": _json_value(kwargs.get("tools") or []),
                "requested_max_tokens": kwargs.get("max_tokens"),
                "thinking": bool(kwargs.get("_bridge_thinking", False)),
            }
            self.stream.write(
                json.dumps(request, ensure_ascii=False, default=str).encode("utf-8") + b"\n"
            )
            self.stream.flush()
            raw = self.stream.readline()
            if not raw:
                raise RuntimeError("live model broker closed before responding")
            envelope = json.loads(raw)
            if not envelope.get("ok"):
                error = envelope.get("error") or {}
                raise RuntimeError(str(error.get("message") or "live model broker failed"))
            response = dict(envelope.get("response") or {})
            self.responses.append(_json_value(response))
            calls = [
                ToolCall(
                    id=str(proposal["id"]),
                    name=str(proposal["name"]),
                    input=dict(proposal.get("input") or {}),
                )
                for proposal in response.get("tool_calls") or []
            ]
            return ModelResponse(
                content=str(response.get("content") or ""),
                usage=response.get("usage"),
                model=str(response.get("model") or "unknown-live-model"),
                metadata={
                    "finish_reason": response.get("finish_reason"),
                    "raw_tool_calls": response.get("raw_tool_calls") or [],
                },
                tool_calls=calls,
            )

    class BehaviorTracer:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []
            self._engine: Any = None
            self._registered: list[tuple[Any, Any]] = []

        def emit(self, event: str, data: dict[str, Any]) -> None:
            self.events.append({"event": event, "data": _json_value(data)})

        def attach(self, agent: Any) -> None:
            engine = getattr(agent, "_hook_engine", None)
            if engine is None:
                self.emit("observer_missing_hook_engine", {})
                return
            self._engine = engine

            async def record(event: Any) -> HookResult:
                self.emit(event.event_type.value, dict(event.data))
                return HookResult()

            for name in (
                "SESSION_START",
                "PRE_TOOL_USE",
                "PERMISSION_DECISION",
                "POST_TOOL_USE",
                "SESSION_END",
            ):
                event_type = getattr(EventType, name, None)
                if event_type is None:
                    self.emit("observer_missing_event_type", {"name": name})
                    continue
                engine.register(event_type, record, priority=-100)
                self._registered.append((event_type, record))

        def detach(self) -> None:
            if self._engine is None:
                return
            for event_type, handler in self._registered:
                self._engine.unregister(event_type, handler)
            self._registered.clear()

    loop_path = workspace / "loop.py"
    spec = importlib.util.spec_from_file_location("evaluated_snapshot_loop", loop_path)
    if spec is None or spec.loader is None:
        raise SystemExit("canonical loop.py has no importable module specification")
    loop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loop)
    run_episode = getattr(loop, "run_episode", None)
    if not callable(run_episode):
        raise SystemExit("canonical loop.py does not expose run_episode(ctx)")

    prompts = plan["prompts"]
    terminal_status = "complete"
    error = ""
    return_values: list[Any] = []
    events: list[dict[str, Any]] = []
    model_inputs: list[list[dict[str, Any]]] = []
    model_responses: list[dict[str, Any]] = []
    for run_index in range(1):
        model = BrokeredModel(args.broker_fd)
        tracer = BehaviorTracer()
        ctx = SimpleNamespace(
            config=SimpleNamespace(
                root=workspace,
                persona="behavior-evaluation",
                max_turns=100,
                max_output_tokens=65_536,
            ),
            episode=int(plan["episode"]),
            tracer=tracer,
            prompts=SimpleNamespace(
                OBSERVE=prompts["observe"],
                PROPOSE=prompts["propose"],
                SELECT=prompts["select_and_act"],
                REFLECT=prompts["reflect"],
            ),
            new_llm=lambda model=model: model,
            thinking=lambda enabled: {"_bridge_thinking": enabled},
        )
        try:
            return_values.append(run_episode(ctx))
        except Exception as exc:
            terminal_status = "error"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            tracer.detach()
        events.extend({**event, "run_index": run_index} for event in tracer.events)
        model_inputs.extend(model.inputs)
        model_responses.extend(model.responses)
        if terminal_status == "error":
            break

    return_value: Any
    if len(return_values) == 1:
        return_value = return_values[0]
    else:
        return_value = {"runs": return_values}

    payload = {
        "terminal_status": terminal_status,
        "error": error,
        "return_value": _json_value(return_value),
        "events": events,
        "model_inputs": model_inputs,
        "model_responses": model_responses,
        "worker_has_openai_key": "OPENAI_API_KEY" in os.environ,
        "worker_can_read_credential_file": _credential_file_readable(),
    }
    print(_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
