"""A bounded sequential recipe; researchers can replace it with any Python Method."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from proteus.core.contracts import TEXT, OperationStatus, require
from proteus.core.methods import MethodResult, supported_budget
from proteus.core.serialization import Contract, content_hash


@dataclass(frozen=True, kw_only=True)
class InitialValue(Contract):
    kind: str = field(metadata=TEXT)
    value: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class Step(Contract):
    stage: str = field(metadata=TEXT)
    inputs: Mapping[str, str] = field(default_factory=dict)
    outputs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class Condition(Contract):
    slot: str = field(metadata=TEXT)
    field: str = field(metadata=TEXT)
    equals: Any


@dataclass(frozen=True, kw_only=True)
class SequenceOptions(Contract):
    steps: tuple[Step, ...] = field(metadata={"minItems": 1})
    initial: Mapping[str, InitialValue] = field(default_factory=dict)
    iterations: int = field(default=1, metadata={"minimum": 1})
    stop_when: Condition | None = None
    outputs: Mapping[str, str] = field(default_factory=dict)


def validate_sequence(recipe, options, registry):
    require(recipe.subject is None and recipe.tasks is None and recipe.execution is None
            and not recipe.evaluations and not recipe.search,
            "sequence method consumes stage data; use a candidate method for subject/task evaluation")
    supported_budget(recipe.budget, allow_evaluations=True)
    for actor in recipe.actors.values():
        supported_budget(actor.budget)
    stages = {stage.id: stage for stage in recipe.stages}
    slots = {name: value.kind for name, value in options.initial.items()}
    for value in options.initial.values():
        registry.data_schema(value.kind).from_dict(value.value)
    for step in options.steps:
        require(step.stage in stages, f"unknown sequence stage: {step.stage}")
        stage = stages[step.stage]
        supported_budget(stage.budget)
        require(set(step.inputs) == set(stage.input_types) and set(step.outputs) == set(stage.output_types),
                f"{step.stage}: sequence bindings differ from stage ports")
        for port, slot in step.inputs.items():
            require(slots.get(slot) == stage.input_types[port], f"{step.stage}: unbound/mistyped slot {slot}")
        require(len(set(step.outputs.values())) == len(step.outputs), "outputs cannot overwrite one slot twice")
        for port, slot in step.outputs.items():
            require(slot not in slots or slots[slot] == stage.output_types[port],
                    "a loop slot cannot change its data kind")
            slots[slot] = stage.output_types[port]
    require(all(slot in slots for slot in options.outputs.values()), "unknown sequence result slot")
    require(options.stop_when is None or options.stop_when.slot in slots, "unknown stop condition slot")
    if options.stop_when is not None:
        schema = registry.data_schema(slots[options.stop_when.slot]).json_schema()
        require(options.stop_when.field in schema["properties"], "unknown stop condition field")


class SequenceMethod:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        require(restored_state is None, "sequence recovery needs the durable operation ledger")
        slots = {name: services.values.publish(value.kind,
                    services.registry.data_schema(value.kind).from_dict(value.value))
                 for name, value in self.options.initial.items()}
        for iteration in range(self.options.iterations):
            for i, step in enumerate(self.options.steps):
                result = services.stage(step.stage, {port: slots[slot] for port, slot in step.inputs.items()},
                                        operation_id=f"iteration-{iteration + 1}-step-{i + 1}")
                if result.status != OperationStatus.COMPLETED:
                    return MethodResult(status=result.status, diagnostic=result.diagnostic)
                slots.update({slot: result.outputs[port] for port, slot in step.outputs.items()})
            condition = self.options.stop_when
            if condition is not None:
                record = services.values.get(slots[condition.slot]).to_dict()
                require(condition.field in record, "stop condition field is absent")
                if content_hash(record[condition.field]) == content_hash(condition.equals):
                    break
        return MethodResult(outputs={name: slots[slot] for name, slot in self.options.outputs.items()})
