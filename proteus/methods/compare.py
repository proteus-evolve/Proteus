"""A small executable comparison recipe using the same services as search methods."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from proteus.core.contracts import TEXT, UNIQUE, DataRef, require, unique
from proteus.core.methods import MethodResult
from proteus.core.serialization import Contract


@dataclass(frozen=True, kw_only=True)
class Variant(Contract):
    id: str = field(metadata=TEXT)
    delta: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class CompareOptions(Contract):
    evaluations: tuple[str, ...] = field(metadata={**UNIQUE, "minItems": 1})
    variants: tuple[Variant, ...] = ()

    def _validate(self):
        unique((item.id for item in self.variants), "comparison variant")
        require(all(item.id != "initial" for item in self.variants), "variant cannot reuse the initial ID")


@dataclass(frozen=True, kw_only=True)
class CompareState(Contract):
    candidate_ids: tuple[str, ...]
    variant_cursor: int = field(default=0, metadata={"minimum": 0})
    evaluation_cursor: int = field(default=0, metadata={"minimum": 0})
    outputs: Mapping[str, DataRef] = field(default_factory=dict)


def validate_compare(recipe, options):
    require(recipe.subject is not None and recipe.execution is not None, "comparison needs a subject and execution")
    require(not recipe.stages and not recipe.search and recipe.tasks is None,
            "comparison uses declared evaluations; stages/search and a separate task suite are not consumed")
    require(set(options.evaluations) <= {spec.id for spec in recipe.evaluations}, "unknown comparison evaluation")


class CompareMethod:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        if restored_state is None:
            initial = services.initial_candidate()
            state = CompareState(candidate_ids=(initial.id,))
            checkpoint = services.checkpoint(state)
        else:
            state, checkpoint = CompareState.from_dict(restored_state.state.payload), restored_state
            initial = next(candidate for candidate in services.candidates if candidate.id == "initial")
        codec = services.artifacts._codec(initial.artifact.codec)
        for index in range(state.variant_cursor, len(self.options.variants)):
            variant = self.options.variants[index]
            candidate = services.fork_candidate(initial, candidate_id=variant.id,
                delta=codec.delta_schema.from_dict(variant.delta))
            state = replace(state, candidate_ids=(*state.candidate_ids, candidate.id), variant_cursor=index + 1)
            checkpoint = services.checkpoint(state)
        comparisons = [(candidate_id, evaluation_id) for candidate_id in state.candidate_ids
                       for evaluation_id in self.options.evaluations]
        for index in range(state.evaluation_cursor, len(comparisons)):
            candidate_id, evaluation_id = comparisons[index]
            candidate = next(item for item in services.candidates if item.id == candidate_id)
            receipt = services.evaluate(candidate, evaluation_id)
            outputs = {**state.outputs, candidate_id + ":" + evaluation_id:
                       services.values.publish("proteus.eval.receipt@1", receipt)}
            state = replace(state, outputs=outputs, evaluation_cursor=index + 1)
            checkpoint = services.checkpoint(state)
        return MethodResult(outputs=state.outputs, state=checkpoint)
