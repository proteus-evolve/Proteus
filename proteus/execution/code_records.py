"""Managed candidate-code calls and separately versioned memory state."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from proteus.core.contracts import DIGEST, TEXT, UNIQUE, Outcome, Usage, require
from proteus.core.serialization import Contract
from proteus.evaluation.records import Trace


def identifier(value):
    return re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', value) is not None


@dataclass(frozen=True, kw_only=True)
class CodeOptions(Contract):
    module: str = field(default='candidate', metadata=TEXT)
    class_name: str | None = field(default=None, metadata=TEXT)
    methods: tuple[str, ...] = field(default=('predict', 'learn_from_batch'), metadata={**UNIQUE, 'minItems': 1})
    state_getter: str = 'get_state'
    state_setter: str = 'set_state'
    prediction_method: str = 'predict'
    prediction_output: Literal['value', 'answer_metadata'] = 'answer_metadata'
    max_frame_bytes: int = field(default=4 * 1024 * 1024, metadata={'minimum': 1024})
    max_output_bytes: int = field(default=1024 * 1024, metadata={'minimum': 1024})
    max_trace_bytes: int = field(default=16 * 1024 * 1024, metadata={'minimum': 1024})
    max_model_requests: int = field(default=1000, metadata={'minimum': 0})

    def _validate(self):
        require(all(identifier(part) for part in self.module.split('.')), 'code module must be a dotted Python identifier')
        require(self.class_name is None or identifier(self.class_name), 'invalid code class name')
        require(all(identifier(name) for name in (*self.methods, self.state_getter, self.state_setter)),
                'code interface methods must be public Python identifiers')
        require(self.prediction_method in self.methods, 'prediction method must be declared')
        require(self.state_getter != self.state_setter and not {self.state_getter, self.state_setter}.intersection(self.methods),
                'state accessors must be separate from candidate operations')


@dataclass(frozen=True, kw_only=True)
class CodeState(Contract):
    artifact_hash: str = field(metadata=DIGEST)
    initialized: bool = False
    value: Any = None

    def _validate(self):
        require(self.initialized or self.value is None, 'uninitialized code state cannot contain memory')


@dataclass(frozen=True, kw_only=True)
class CodeCall(Contract):
    method: str = field(metadata=TEXT)
    state: CodeState
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    seed: int = 0

    def _validate(self):
        require(identifier(self.method), 'code operation must be a public Python identifier')
        require(all(identifier(name) for name in self.kwargs), 'invalid candidate keyword argument')


@dataclass(frozen=True, kw_only=True)
class CodeResult(Contract):
    candidate_id: str = field(metadata=TEXT)
    operation_id: str = field(metadata=TEXT)
    call_hash: str = field(metadata=DIGEST)
    state: CodeState
    status: Outcome
    output: Any = None
    trace: Trace = field(default_factory=Trace)
    usage: Usage = field(default_factory=Usage)
    diagnostic: str = ''
