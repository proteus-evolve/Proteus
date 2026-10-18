"""A small executable comparison recipe using the same services as search methods."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from proteus.core.contracts import TEXT, UNIQUE, require, unique
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


def validate_compare(recipe, options):
    require(recipe.subject is not None and recipe.execution is not None, "comparison needs a subject and execution")
    require(not recipe.stages and not recipe.search and recipe.tasks is None,
            "comparison uses declared evaluations; stages/search and a separate task suite are not consumed")
    require(set(options.evaluations) <= {spec.id for spec in recipe.evaluations}, "unknown comparison evaluation")


class CompareMethod:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        require(restored_state is None, "comparison recovery requires the durable operation ledger")
        initial = services.initial_candidate()
        candidates = [initial]
        codec = services.artifacts._codec(initial.artifact.codec)
        for variant in self.options.variants:
            candidates.append(services.fork_candidate(initial, candidate_id=variant.id,
                delta=codec.delta_schema.from_dict(variant.delta)))
        outputs = {}
        for candidate in candidates:
            for evaluation_id in self.options.evaluations:
                receipt = services.evaluate(candidate, evaluation_id)
                outputs[candidate.id + ":" + evaluation_id] = services.values.publish("proteus.eval.receipt@1", receipt)
        return MethodResult(outputs=outputs)
