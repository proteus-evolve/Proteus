"""ACE's phase-2 ADD proposals and reducer over one shared playbook version."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from proteus.core.contracts import ArtifactRef, DataRef, require
from proteus.core.serialization import Contract, freeze_json, json_value
from proteus.methods.ace import parsing, prompts
from proteus.methods.ace.playbook import CODEC, PlaybookDelta
from proteus.methods.ace.roles import CurationInput
from proteus.operators.combee.grouping import Grouping, plan_groups
from proteus.operators.combee.managed import ManagedLM


@dataclass(frozen=True, kw_only=True)
class AceRecord(Contract):
    sample_number: int = field(metadata={"minimum": 1})
    reflection: str
    context: str = ""


@dataclass(frozen=True, kw_only=True)
class AddProposal(Contract):
    operations: tuple[Any, ...] = ()
    accepted: bool = False
    diagnostic: str = ""


@dataclass(frozen=True, kw_only=True)
class AceAggregation(Contract):
    base: ArtifactRef
    plan: DataRef
    waves: tuple[DataRef, ...]
    proposals: tuple[AddProposal, ...]
    reducer: AddProposal | None
    delta: PlaybookDelta


def parse_adds(response):
    """Parse proposals, without applying them before the reducer (upstream rule)."""
    try:
        if response.startswith("INCORRECT_DUE_TO_EMPTY_RESPONSE"):
            raise ValueError("upstream empty-response sentinel")
        operations = parsing.Curator()._extract_and_validate_operations(response)["operations"]
        return AddProposal(
            operations=freeze_json([op for op in operations if op.get("type") == "ADD"]),
            accepted=True,
        )
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        return AddProposal(diagnostic=str(exc))


def aggregate_ace(
    services,
    base,
    records,
    *,
    stage,
    operation_id,
    step,
    total_samples,
    max_workers,
    grouping=None,
    token_budget=80000,
    use_ground_truth=True,
):
    """Return one delta; caller owns counters, final application and batch rollback.

    ``base`` must already contain the once-per-batch counter update. Original
    reflection records remain distinct occurrences even if their bytes match.
    Infrastructure failures stop the operator; malformed model proposals follow
    upstream skip/filter behavior and remain visible in the recorded responses.
    """
    require(base.codec == CODEC, "ACE aggregation requires an ACE playbook artifact")
    playbook = services.artifacts.get(base)
    records = tuple(records)
    require(
        all(ref.kind == "proteus.combee.ace_record@1" for ref in records),
        "ACE reflection record kind differs",
    )
    values = [services.values.get(ref) for ref in records]
    grouping = grouping or Grouping(profile="ace")
    require(grouping.profile == "ace", "ACE aggregation requires ACE grouping")
    # Validate all context fields before consuming RNG or issuing any calls.
    item = CurationInput(
        playbook=playbook,
        artifact_hash=base.content_hash,
        reflection="",
        question_context="",
        step=step,
        total_samples=total_samples,
        token_budget=token_budget,
        use_ground_truth=use_ground_truth,
    )
    lm = ManagedLM(services, stage, operation_id=operation_id, max_workers=max_workers)
    plan_ref = plan_groups(services, records, grouping, operation_id=operation_id)
    plan = services.values.get(plan_ref)
    template = prompts.CURATOR_PROMPT if use_ground_truth else prompts.CURATOR_PROMPT_NO_GT
    common = dict(
        current_step=item.step,
        total_samples=item.total_samples,
        token_budget=item.token_budget,
        playbook_stats=json.dumps(playbook.stats(), indent=2),
        current_playbook=playbook.text,
    )
    group_records, rendered = [], []
    for group in plan.groups:
        selected = [values[entry.source_index] for entry in group]
        group_records.append(selected)
        reflection = (
            "\n\n---\n\n".join(
                f"[Sample {row.sample_number}] {row.reflection}"
                for row in selected
                if row.reflection != "(empty)"
            )
            or "(empty)"
        )
        context = "\n\n---\n\n".join(
            f"[Sample {row.sample_number}] {row.context}" for row in selected if row.context
        )
        rendered.append(
            template.format(**common, recent_reflection=reflection, question_context=context)
        )
    proposals = tuple(parse_adds(raw) for raw in lm.complete(rendered))
    # Stable group order, independent of completion order. A lone surviving
    # proposal still goes through ACE's reducer; GEPA deliberately differs.
    groups = [
        {
            "group_id": index + 1,
            "sample_numbers": [row.sample_number for row in group_records[index]],
            "operations": json_value(proposal.operations),
        }
        for index, proposal in enumerate(proposals)
        if proposal.operations
    ]
    reducer = None
    if groups:
        prompt = prompts.CURATOR_OPERATIONS_AGGREGATION_PROMPT.format(
            **common, proposed_operations=json.dumps(groups, indent=2, ensure_ascii=False)
        )
        reducer = parse_adds(lm(prompt))
    delta = PlaybookDelta(
        base_hash=base.content_hash, kind="curate", records=reducer.operations if reducer else ()
    )
    result = AceAggregation(
        base=base,
        plan=plan_ref,
        waves=tuple(lm.waves),
        proposals=proposals,
        reducer=reducer,
        delta=delta,
    )
    result = services._record(
        "aggregation.result",
        {
            "operation_id": operation_id,
            "plan": plan_ref.to_dict(),
            "base": base.to_dict(),
            "result": result.to_dict(),
        },
        lambda: result,
        decode=AceAggregation.from_dict,
    )
    return services.values.publish("proteus.combee.ace_result@1", result)
