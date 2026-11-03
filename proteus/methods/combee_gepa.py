"""Combee reflection inside the pinned GEPA search and evaluation lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from proteus.core.contracts import OperationStatus, PluginRef, require
from proteus.core.plugins import Plugin, implementation_hash
from proteus.core.serialization import content_hash, json_value
from proteus.methods.gepa.method import (
    GepaMethod,
    GepaOptions,
    authoritative,
    register as register_gepa,
    validate_gepa,
)
from proteus.methods.gepa.state import GepaState
from proteus.operators import combee
from proteus.operators.combee.gepa import REVISION, aggregate_gepa


@dataclass(frozen=True, kw_only=True)
class CombeeGepaOptions(GepaOptions):
    minibatch_size: int = field(default=9, metadata={"minimum": 1})
    duplication_factor: int = field(default=2, metadata={"minimum": 1})
    max_workers: int = field(default=4, metadata={"minimum": 1})
    batch_reflection: bool = True
    aggregation_prompt_template: str | None = None
    operator_revision: Literal["f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819"] = REVISION


def validate_combee(recipe, options, registry):
    validate_gepa(recipe, options, registry)
    require(
        recipe.reference.profile in ("adapted", "extended") and bool(recipe.reference.deviations),
        "Combee GEPA requires an explicit adapted/extended profile and deviations",
    )
    if options.aggregation_prompt_template is not None:
        require(
            all(
                key in options.aggregation_prompt_template
                for key in ("<curr_param>", "<side_info>")
            ),
            "Combee aggregation template lacks required placeholders",
        )
    if options.batch_reflection:
        stage = next(stage for stage in recipe.stages if stage.id == options.reflection_stage)
        require(
            {"stage.parallel", "stage.replay"}
            <= set(registry.get(stage.operator.plugin).capabilities),
            "Combee map stages require parallel and replay capabilities",
        )
        actor = recipe.actors[stage.role]
        require(
            actor.session.value == "fresh"
            and "actor.parallel" in registry.get(actor.runtime.plugin).capabilities,
            "Combee map calls require a fresh parallel actor",
        )


@authoritative
def reflect(bridge, candidate, dataset, components):
    require(
        bridge.phase == "reflection" and bridge.parent is not None,
        "Combee reflection is outside the GEPA proposal phase",
    )
    require(candidate == bridge.texts(bridge.parent.id), "Combee reflection parent differs")
    options, services = bridge.options, bridge.services
    ref = aggregate_gepa(
        services,
        candidate,
        dataset,
        components,
        stage=options.reflection_stage,
        operation_id=f"gepa-combee/iteration-{bridge.state.i}",
        max_workers=options.max_workers,
        duplication_factor=options.duplication_factor,
        reflection_prompt_template=options.reflection_prompt_template,
        aggregation_prompt_template=options.aggregation_prompt_template,
        batch_reflection=options.batch_reflection,
    )
    result = services.values.get(ref)
    count = 0
    for wave_ref in result.waves:
        wave = services.values.get(wave_ref)
        for response_ref in wave.responses:
            require(
                services.values.get(response_ref).status == OperationStatus.COMPLETED,
                "Combee returned an incomplete actor result",
            )
        count += len(wave.responses)
    require(
        count == result.metadata["combee:total_lm_calls"],
        "Combee reported call count differs from its managed waves",
    )
    bridge.reflection_calls += count
    from gepa.proposer.reflective_mutation.reflection_lm import ReflectionProposal

    return ReflectionProposal(
        new_texts=dict(result.new_texts),
        prompts=json_value(result.prompts),
        raw_lm_outputs=json_value(result.raw_lm_outputs),
        metadata={**json_value(result.metadata), "proteus:aggregation": ref.to_dict()},
    )


class ManagedReflection:
    def __init__(self, bridge):
        self.bridge = bridge

    def reflect(self, candidate, reflective_dataset, components_to_update):
        return reflect(self.bridge, candidate, reflective_dataset, components_to_update), self


def register(registry):
    register_gepa(registry)
    combee.register(registry)
    root = Path(combee.__file__).parent
    registry.register(
        Plugin(
            ref=PluginRef(name="proteus.combee_gepa", version="1"),
            kind="method",
            options=CombeeGepaOptions,
            factory=lambda options: GepaMethod(
                options, reflection_strategy_factory=ManagedReflection
            ),
            source="proteus",
            implementation_hash=content_hash(
                {
                    "method": implementation_hash({"method": Path(__file__)}),
                    "operator": implementation_hash(
                        {str(path.relative_to(root)): path for path in root.rglob("*.py")}
                    ),
                    "operator_revision": REVISION,
                }
            ),
            capabilities=("run.resume", "run.journal"),
            state_schema=GepaState,
            validate=lambda recipe, options: validate_combee(recipe, options, registry),
        )
    )
