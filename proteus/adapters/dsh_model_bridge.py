"""DSH-specific configuration for the shared keyless controller bridge."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType

from proteus.safety.live import (
    LiveModelChannel,
    LiveModelResponse,
    LiveProtocolError,
)
from proteus.safety.live_bridge import BridgeCallRecord, OpenAICompatibleBridge

BRIDGE_PROVIDER = "proteus-openai"
BRIDGE_PLACEHOLDER = "proteus-local-bridge"


def _result_call_ids(
    input_value: str | Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    if isinstance(input_value, str):
        return ()
    return tuple(
        str(item["call_id"])
        for item in input_value
        if item.get("type") in {"function_call_output", "custom_tool_call_output"}
        and isinstance(item.get("call_id"), str)
        and item["call_id"]
    )


class _DshBudgetBoundaryChannel:
    """Finish a settled DSH phase with one real fixed-model no-tools turn."""

    def __init__(self, channel: LiveModelChannel, evidence_root: Path) -> None:
        self._channel = channel
        self._evidence_root = Path(evidence_root)
        self._phase = ""
        self._stop_at = 0
        self._issued_calls = 0
        self._pending_boundary_calls: tuple[str, ...] = ()
        self._boundary_records = 0

    @property
    def model(self) -> str:
        return self._channel.model

    def set_phase_boundary(self, phase: str, stop_at: int, used_before: int) -> None:
        if self._pending_boundary_calls:
            raise LiveProtocolError("previous DSH budget boundary was not settled")
        if used_before != self._issued_calls:
            raise LiveProtocolError("native DSH call count does not match controller budget state")
        if stop_at and stop_at < used_before:
            raise LiveProtocolError("DSH phase stop precedes calls already issued")
        self._phase = phase
        self._stop_at = stop_at

    def respond(
        self,
        *,
        input: str | Sequence[Mapping[str, object]],
        instructions: str = "",
        tools: Sequence[Mapping[str, object]] = (),
    ) -> LiveModelResponse:
        if not tools:
            return self._channel.respond(
                input=input,
                instructions=instructions,
                tools=tools,
            )

        result_ids = _result_call_ids(input)
        if self._pending_boundary_calls:
            missing = tuple(
                call_id
                for call_id in self._pending_boundary_calls
                if call_id not in result_ids
            )
            if missing:
                raise LiveProtocolError(
                    "DSH budget boundary request is missing the exact settled result"
                )
            response = self._channel.respond(
                input=input,
                instructions=instructions,
                tools=(),
            )
            if response.tool_calls:
                raise LiveProtocolError(
                    "DSH budget boundary model returned a tool call with tools disabled"
                )
            self._record_boundary(response, result_ids)
            self._pending_boundary_calls = ()
            return response

        response = self._channel.respond(
            input=input,
            instructions=instructions,
            tools=tools,
        )
        issued = tuple(call.call_id for call in response.tool_calls)
        next_count = self._issued_calls + len(issued)
        if self._stop_at and next_count > self._stop_at:
            raise LiveProtocolError("DSH controller response exceeds the phase tool budget")
        self._issued_calls = next_count
        if self._stop_at and issued and self._issued_calls == self._stop_at:
            self._pending_boundary_calls = issued
        return response

    def close(self) -> None:
        self._channel.close()

    def _record_boundary(
        self, response: LiveModelResponse, result_ids: tuple[str, ...]
    ) -> None:
        self._boundary_records += 1
        path = self._evidence_root / f"budget-boundary-{self._boundary_records:03d}.json"
        path.write_text(
            json.dumps(
                {
                    "phase": self._phase,
                    "stop_at": self._stop_at,
                    "issued_calls": self._issued_calls,
                    "settled_call_ids": list(self._pending_boundary_calls),
                    "result_call_ids": list(result_ids),
                    "forwarded_tools": 0,
                    "input_preserved": True,
                    "response_id": response.response_id,
                    "configured_model": response.provenance.configured_model,
                    "returned_model": response.model,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


class DshModelBridge:
    """Mount an exact-model DSH route over the shared controller bridge."""

    provider = BRIDGE_PROVIDER

    def __init__(
        self,
        *,
        channel: LiveModelChannel,
        evidence_root: Path,
        config_root: Path,
    ) -> None:
        if not isinstance(channel, LiveModelChannel):
            raise TypeError("DSH bridge channel must implement LiveModelChannel")
        self._channel = channel
        self._config_root = Path(config_root)
        self._budget_channel = _DshBudgetBoundaryChannel(channel, Path(evidence_root))
        self._bridge = OpenAICompatibleBridge(
            channel=self._budget_channel,
            evidence_root=Path(evidence_root),
        )

    @property
    def model(self) -> str:
        return self._channel.model

    @property
    def patch_path(self) -> Path:
        return self._config_root / "cordis.patch.yml"

    @property
    def container_base_url(self) -> str:
        return self._bridge.container_base_url

    @property
    def records(self) -> tuple[BridgeCallRecord, ...]:
        return self._bridge.records

    def set_phase_boundary(self, phase: str, stop_at: int, used_before: int) -> None:
        self._budget_channel.set_phase_boundary(phase, stop_at, used_before)

    def __enter__(self) -> DshModelBridge:
        self._bridge.__enter__()
        try:
            self._config_root.mkdir(parents=True, exist_ok=False)
            self.patch_path.write_text(self._patch_text(), encoding="utf-8")
        except Exception:
            self._bridge.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._bridge.__exit__(exc_type, exc, traceback)

    def _patch_text(self) -> str:
        model = self.model
        forbidden = {"\r", "\n", ":", '"'}
        if not model or any(character in model for character in forbidden):
            raise ValueError("DSH bridge model must be a non-empty YAML-safe ID")
        return f"""\
- id: agent-default-model
  config:
    provider: {self.provider}
    model: {model}

- id: tools
  config:
    mode: native

- id: llm-pi-ai
  config:
    providers:
      {self.provider}:
        displayName: Proteus controller bridge
        api: openai-responses
        baseURL: {self.container_base_url}
        headers:
          Authorization: Bearer {BRIDGE_PLACEHOLDER}
        retryPolicy:
          mode: normal
          maxRetries: 0
        models:
          - id: {model}
            name: {model}
            contextWindow: 128000
            maxTokens: 4096
            reasoningEfforts: false
"""
