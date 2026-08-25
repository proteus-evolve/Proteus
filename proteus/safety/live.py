"""Trusted live-channel contracts used by model-mediated safety runtimes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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
class LiveModelResponse:
    response_id: str
    model: str
    output_text: str
    tool_calls: tuple[LiveToolCall, ...]
    provenance: LiveCallProvenance


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

    def close(self) -> None: ...
