"""Managed pinned GEPA ComBEE operator, independent of the base optimizer pin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import random
import json
from typing import Any

from proteus.core.contracts import DataRef, require
from proteus.core.serialization import Contract, content_hash, read_json
from proteus.methods.gepa.reference import load_reference
from proteus.methods.gepa.state import ReflectionBatch
from proteus.operators.combee.grouping import Grouping, plan_groups
from proteus.operators.combee.managed import ManagedLM


REVISION = "f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819"


@dataclass(frozen=True, kw_only=True)
class GepaAggregation(Contract):
    revision: str
    components: tuple[str, ...]
    plans: tuple[DataRef, ...]
    waves: tuple[DataRef, ...]
    new_texts: Mapping[str, str]
    prompts: Any
    raw_lm_outputs: Any
    metadata: Any

    def _validate(self):
        require(self.revision == REVISION, "unsupported GEPA ComBEE reference")
        require(len(self.components) == len(self.plans), "GEPA component plans are not aligned")


def aggregate_gepa(
    services,
    candidate,
    reflective_dataset,
    components,
    *,
    stage,
    operation_id,
    max_workers,
    duplication_factor=2,
    reflection_prompt_template=None,
    aggregation_prompt_template=None,
    batch_reflection=True,
):
    """Run the upstream operator with managed text calls and occurrence-level plans.

    Inputs are text components and ordered JSON feedback mappings. This standalone
    layer does not select a parent, run tasks or accept a candidate: the GEPA
    method retains those responsibilities. Provider use/cost comes exclusively
    from the Proteus ledger, never upstream TrackingLM's estimated counters.
    """
    load_reference()  # Verify existing optional GEPA without upgrading its engine.
    from proteus.operators.combee._gepa.combee import ComBEEReflectionLM

    grouping = Grouping(profile="gepa", duplication_factor=duplication_factor)
    require(
        isinstance(candidate, Mapping)
        and all(type(k) is str and type(v) is str for k, v in candidate.items()),
        "GEPA aggregation requires text components",
    )
    require(type(batch_reflection) is bool, "batch_reflection must be a boolean")
    components = tuple(components)
    require(all(type(key) is str for key in components), "GEPA component names must be strings")
    dataset = {
        name: ReflectionBatch.from_examples(rows) for name, rows in reflective_dataset.items()
    }
    lm = ManagedLM(services, stage, operation_id=operation_id, max_workers=max_workers)
    rng = random.Random()
    strategy = ComBEEReflectionLM(
        lm,
        duplication_factor=duplication_factor,
        rng=rng,
        reflection_prompt_template=reflection_prompt_template,
        aggregation_prompt_template=aggregation_prompt_template,
        batch_reflection=batch_reflection,
    )
    # Publish each row separately so exact duplicate values still have distinct
    # occurrence indexes. ReflectionBatch preserves mapping insertion order.
    refs = {
        name: tuple(
            services.values.publish(
                "proteus.gepa.reflection_batch@1", ReflectionBatch.from_examples([row])
            )
            for row in batch.examples
        )
        for name, batch in dataset.items()
    }
    plans, names = [], []
    for index, name in enumerate(components):
        if name not in candidate or not refs.get(name):
            continue
        plan_ref = plan_groups(
            services, refs[name], grouping, operation_id=f"{operation_id}/component-{index}"
        )
        plans.append(plan_ref)
        names.append(name)
    if plans:
        rng.setstate(services.values.get(plans[0]).rng_before)
    before = rng.getstate()
    proposal, _ = strategy.reflect(
        dict(candidate),
        {name: list(batch.examples) for name, batch in dataset.items()},
        list(components),
    )
    expected_rng = services.values.get(plans[-1]).rng_after if plans else before
    require(
        rng.getstate() == expected_rng, "upstream GEPA grouping consumed a different RNG sequence"
    )
    # Upstream's parser may preserve the str subclass on salvaged responses.
    # Normalize only AFTER parsing; finish_reason remains in recorded ActorResults.
    payload = read_json(
        json.dumps(
            {
                "new_texts": proposal.new_texts,
                "prompts": proposal.prompts,
                "raw_lm_outputs": proposal.raw_lm_outputs,
                "metadata": proposal.metadata,
            },
            allow_nan=False,
        )
    )
    result = GepaAggregation(
        revision=REVISION,
        components=tuple(names),
        plans=tuple(plans),
        waves=tuple(lm.waves),
        **payload,
    )
    identity = {
        "operation_id": operation_id,
        "candidate": content_hash(candidate),
        "records": {name: [ref.to_dict() for ref in rows] for name, rows in refs.items()},
        "components": components,
        "result": result.to_dict(),
    }
    result = services._record(
        "aggregation.result", identity, lambda: result, decode=GepaAggregation.from_dict
    )
    return services.values.publish("proteus.combee.gepa_result@1", result)
