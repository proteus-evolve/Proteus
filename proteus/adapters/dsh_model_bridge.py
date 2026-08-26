"""DSH-specific configuration for the shared keyless controller bridge."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType

from proteus.safety.live import (
    LiveCallProvenance,
    LiveModelChannel,
    LiveModelResponse,
    LiveProtocolError,
)
from proteus.safety.live_bridge import BridgeCallRecord, OpenAICompatibleBridge

BRIDGE_PROVIDER = "proteus-openai"
BRIDGE_PLACEHOLDER = "proteus-local-bridge"
OBSERVER_CONTAINER_PATH = "/proteus/bridge/proteus-native-result-observer.mjs"
OBSERVER_OUTPUT_CONTAINER_PATH = "/proteus/native-results/native-results.jsonl"
DSH_PERMISSION_CASE_ENV = (
    "DSH_CASE_TARGET",
    "DSH_CASE_PROTECTED_TARGET",
    "DSH_CASE_ALLOWED_TARGET",
    "DSH_CASE_PROTECTED_CONTENT",
    "DSH_CASE_ALLOWED_CONTENT",
)
_DSH_TITLE_SYSTEM_PREFIX = (
    "Create a concise title for an AI coding-assistant session from the supplied "
    "human messages."
)
_DSH_TITLE_INPUT_PREFIX = (
    "Generate the session title from this JSON array of human messages:\n"
)

_NATIVE_RESULT_OBSERVER = """\
import { appendFileSync } from 'node:fs'

export const inject = ['shellEnv']
const CASE_ENV_KEYS = [
  'DSH_CASE_TARGET',
  'DSH_CASE_PROTECTED_TARGET',
  'DSH_CASE_ALLOWED_TARGET',
  'DSH_CASE_PROTECTED_CONTENT',
  'DSH_CASE_ALLOWED_CONTENT',
]

export function apply(ctx, config) {
  const values = {}
  const variables = {}
  for (const key of CASE_ENV_KEYS) {
    const value = process.env[key]
    if (typeof value !== 'string') continue
    values[key] = value
    variables[key] = { description: 'Proteus controller-owned permission case value.' }
  }
  if (Object.keys(values).length > 0) {
    ctx.shellEnv.register({
      name: 'proteus-permission-case',
      variables,
      resolve: () => values,
    })
  }
  ctx.on('tools/result', (exec, result) => {
    if (result.isError || typeof result.value !== 'object' || result.value === null
      || Array.isArray(result.value)) return
    const sandbox = result.value.sandbox
    if (typeof sandbox !== 'object' || sandbox === null || Array.isArray(sandbox)) {
      return
    }
    const nativeResult = { sandbox }
    const stderr = result.value.stderr
    if (typeof stderr === 'object' && stderr !== null && !Array.isArray(stderr)
      && typeof stderr.text === 'string') nativeResult.stderr = stderr
    appendFileSync(config.path, JSON.stringify({
      callId: exec.callId,
      tool: exec.name,
      nativeResult,
    }) + '\\n', { encoding: 'utf8' })
  })
}
"""


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


def _dsh_title_request(
    *,
    input_value: str | Sequence[Mapping[str, object]],
    instructions: str,
    tools: Sequence[Mapping[str, object]],
) -> bool:
    if tools or not instructions.startswith(_DSH_TITLE_SYSTEM_PREFIX):
        return False
    texts: list[str] = []
    if isinstance(input_value, str):
        texts.append(input_value)
    else:
        for item in input_value:
            content = item.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, Sequence):
                texts.extend(
                    str(block["text"])
                    for block in content
                    if isinstance(block, Mapping)
                    and isinstance(block.get("text"), str)
                )
    return bool(
        not _result_call_ids(input_value)
        and any(text.startswith(_DSH_TITLE_INPUT_PREFIX) for text in texts)
    )


class _DshBudgetBoundaryChannel:
    """Finish a settled DSH phase with one real fixed-model no-tools turn."""

    def __init__(
        self,
        channel: LiveModelChannel,
        evidence_root: Path,
        *,
        deterministic_title: bool = False,
    ) -> None:
        self._channel = channel
        self._evidence_root = Path(evidence_root)
        self._phase = ""
        self._stop_at = 0
        self._issued_calls = 0
        self._pending_boundary_calls: tuple[str, ...] = ()
        self._boundary_records = 0
        self._deterministic_title = deterministic_title
        self._title_records = 0

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
        if (
            self._deterministic_title
            and _dsh_title_request(
                input_value=input,
                instructions=instructions,
                tools=tools,
            )
        ):
            self._title_records += 1
            provenance = LiveCallProvenance(
                call_id=f"proteus-dsh-title-{self._title_records}",
                response_id=f"proteus-dsh-title-response-{self._title_records}",
                configured_model=self.model,
                response_model=self.model,
            )
            return LiveModelResponse(
                response_id=provenance.response_id,
                model=self.model,
                output_text="Proteus native permission episode",
                tool_calls=(),
                provenance=provenance,
            )
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
        deterministic_title: bool = False,
        observe_native_results: bool = False,
    ) -> None:
        if not isinstance(channel, LiveModelChannel):
            raise TypeError("DSH bridge channel must implement LiveModelChannel")
        self._channel = channel
        self._config_root = Path(config_root)
        self._evidence_root = Path(evidence_root)
        self._observe_native_results = observe_native_results
        self._budget_channel = _DshBudgetBoundaryChannel(
            channel,
            Path(evidence_root),
            deterministic_title=deterministic_title,
        )
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
    def observer_path(self) -> Path:
        return self._config_root / "proteus-native-result-observer.mjs"

    @property
    def native_results_path(self) -> Path:
        return self._evidence_root / "native-results.jsonl"

    @property
    def container_base_url(self) -> str:
        return self._bridge.container_base_url

    @property
    def records(self) -> tuple[BridgeCallRecord, ...]:
        return self._bridge.records

    def set_phase_boundary(self, phase: str, stop_at: int, used_before: int) -> None:
        self._budget_channel.set_phase_boundary(phase, stop_at, used_before)

    def __enter__(self) -> DshModelBridge:  # noqa: PYI034 - Python 3.10 support
        self._bridge.__enter__()
        try:
            self._config_root.mkdir(parents=True, exist_ok=False)
            if self._observe_native_results:
                self.observer_path.write_text(
                    _NATIVE_RESULT_OBSERVER,
                    encoding="utf-8",
                )
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
        observer = (
            f"- id: proteus-native-result-observer\n"
            f"  name: file://{OBSERVER_CONTAINER_PATH}\n"
            f"  config:\n"
            f"    path: {OBSERVER_OUTPUT_CONTAINER_PATH}\n\n"
            if self._observe_native_results
            else ""
        )
        return f"""\
{observer}
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
