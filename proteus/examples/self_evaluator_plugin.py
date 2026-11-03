"""A minimal third-party-style stage: local self evidence, not a benchmark score."""

from dataclasses import dataclass
from pathlib import Path

from proteus.core.contracts import PluginRef
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.serialization import Contract


@dataclass(frozen=True, kw_only=True)
class Assessment(Contract):
    has_proposal: bool
    explanation: str


class SelfEvaluator:
    def run(self, context, inputs):
        present = bool(inputs["answer"].text.strip())
        return {"assessment": Assessment(has_proposal=present,
            explanation="A proposal was produced." if present else "The proposal is empty.")}


def register(registry):
    registry.register_data("example.self_assessment@1", Assessment)
    registry.register(Plugin(ref=PluginRef(name="example.self_evaluator", version="1"),
        kind="stage", options=EmptyOptions, factory=lambda options: SelfEvaluator(), source="proteus",
        implementation_hash=implementation_hash({"self_evaluator_plugin.py": Path(__file__)}),
        inputs={"answer": "proteus.actor_result@1"}, outputs={"assessment": "example.self_assessment@1"}))
