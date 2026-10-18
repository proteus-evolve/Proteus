"""Managed ACE roles with pinned prompts and parsers, and explicit typed inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from proteus.artifacts.codecs import FileContent
from proteus.core.contracts import DIGEST, OperationStatus, Outcome, PluginRef, SessionPolicy, require
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.serialization import Contract, freeze_json
from proteus.evaluation.records import SubjectResult, Trace
from proteus.core.serialization import content_hash
from proteus.methods.ace import parsing, prompts
from proteus.methods.ace.playbook import CODEC, Playbook, PlaybookCodec, PlaybookDelta


@dataclass(frozen=True, kw_only=True)
class AceTask(Contract):
    question: str
    context: str = ""


@dataclass(frozen=True, kw_only=True)
class GenerationInput(Contract):
    reflection: str = "(empty)"
    json_mode: bool = False


@dataclass(frozen=True, kw_only=True)
class Generation(Contract):
    response: str
    answer: str
    bullet_ids: Any


@dataclass(frozen=True, kw_only=True)
class ReflectionInput(Contract):
    question: str
    reasoning_trace: str
    predicted_answer: str
    ground_truth: str | None
    environment_feedback: str
    bullets_used: str
    use_ground_truth: bool = True
    json_mode: bool = False


@dataclass(frozen=True, kw_only=True)
class Reflection(Contract):
    response: str
    bullet_tags: Any


@dataclass(frozen=True, kw_only=True)
class CurationInput(Contract):
    playbook: Playbook
    artifact_hash: str = field(metadata=DIGEST)
    reflection: str
    question_context: str
    step: int = field(metadata={"minimum": 1})
    total_samples: int = field(metadata={"minimum": 1})
    token_budget: int = field(default=80000, metadata={"minimum": 1})
    use_ground_truth: bool = True


@dataclass(frozen=True, kw_only=True)
class Curation(Contract):
    response: str
    operations: tuple[Any, ...] = ()
    accepted: bool = False
    diagnostic: str = ""


class AceSubject:
    def run(self, context):
        task = AceTask.from_dict(context.task.input)
        generation = context.inputs["generation"]
        root = context.lease.local_root
        require(root is not None, "ACE subject requires a materialized playbook")
        playbook = PlaybookCodec().decode({name: FileContent((root / name).read_bytes())
                                          for name in ("playbook.txt", "index.json")})
        prompt = prompts.GENERATOR_PROMPT.format(playbook.text, generation.reflection, task.question, task.context)
        answer = context.actor(prompt)
        output = None
        if answer.status == OperationStatus.COMPLETED:
            output = Generation(response=answer.text, answer=parsing.extract_answer(answer.text),
                bullet_ids=parsing.Generator()._extract_bullet_ids(answer.text, generation.json_mode)).to_dict()
        return SubjectResult(loaded_artifact_hash=context.lease.loaded_artifact_hash,
            runtime=replace(answer.runtime, environment_hash=content_hash(context.execution.environment)),
            status=Outcome.SUCCEEDED if answer.status == OperationStatus.COMPLETED else Outcome.INFRA_ERROR,
            output=output, state=context.state, usage=answer.usage, trace=Trace(events=answer.trace),
            diagnostic=answer.diagnostic)


class ReflectStage:
    def run(self, context, inputs):
        item = inputs["request"]
        prefix = (item.question, item.reasoning_trace, item.predicted_answer)
        suffix = (item.environment_feedback, item.bullets_used)
        if item.use_ground_truth and item.ground_truth:
            prompt = prompts.REFLECTOR_PROMPT.format(*prefix, item.ground_truth, *suffix)
        else:
            prompt = prompts.REFLECTOR_PROMPT_NO_GT.format(*prefix, *suffix)
        answer = context.actor(prompt)
        require(answer.status == OperationStatus.COMPLETED, answer.diagnostic or "ACE reflector failed")
        return {"reflection": Reflection(response=answer.text,
            bullet_tags=parsing.Reflector()._extract_bullet_tags(answer.text, item.json_mode))}


class CurateStage:
    def run(self, context, inputs):
        item = inputs["request"]
        template = prompts.CURATOR_PROMPT if item.use_ground_truth else prompts.CURATOR_PROMPT_NO_GT
        # Compute stats from text here: serializing a stats mapping through a
        # checkpoint would sort its keys and change the upstream prompt bytes.
        prompt = template.format(current_step=item.step, total_samples=item.total_samples,
            token_budget=item.token_budget, playbook_stats=json.dumps(item.playbook.stats(), indent=2),
            recent_reflection=item.reflection, current_playbook=item.playbook.text,
            question_context=item.question_context)
        answer = context.actor(prompt)
        require(answer.status == OperationStatus.COMPLETED, answer.diagnostic or "ACE curator failed")
        operations = ()
        try:
            if answer.text.startswith("INCORRECT_DUE_TO_EMPTY_RESPONSE"):
                raise ValueError("upstream empty-response sentinel")
            # json.loads accepts non-standard NaN/Infinity. Retain such a raw
            # response but do not assign unrepresentable records to the result.
            operations = freeze_json(parsing.Curator()._extract_and_validate_operations(answer.text)["operations"])
            delta = PlaybookDelta(base_hash=item.artifact_hash, kind="curate", records=operations)
            PlaybookCodec().apply(item.playbook, delta)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            # The reference skips rejected curator responses, without retrying
            # the model. Preserve both the raw response and any parsed records.
            result = Curation(response=answer.text, operations=operations, diagnostic=str(exc))
        else:
            result = Curation(response=answer.text, operations=operations, accepted=True)
        return {"curation": result}


def validate_subject(recipe, options):
    require(recipe.subject.actor is not None and recipe.subject.initial_artifact.codec == CODEC,
            "ACE subject requires a managed actor and ACE playbook")
    require(recipe.execution is not None and recipe.execution.environment.backend.plugin.name == "proteus.local_json",
            "ACE text subject requires the local JSON environment")
    require(not recipe.subject.mutable_scopes and recipe.execution.timeout_seconds is None,
            "ACE text subject uses explicit state and actor transport timeouts")
    require(recipe.actors[recipe.subject.actor].session == SessionPolicy.FRESH,
            "ACE generator requires fresh requests")


def register(registry):
    for name, schema in (("generation_input", GenerationInput), ("generation", Generation),
                         ("reflection_input", ReflectionInput), ("reflection", Reflection),
                         ("curation_input", CurationInput), ("curation", Curation)):
        registry.register_data(f"proteus.ace.{name}@1", schema)
    root = Path(__file__).parent
    digest = implementation_hash({path.name: path for path in root.glob("*.py")})
    registry.register(Plugin(ref=PluginRef(name="proteus.ace_subject", version="1"), kind="subject",
        options=EmptyOptions, factory=lambda options: AceSubject(), source="proteus", implementation_hash=digest,
        inputs={"generation": "proteus.ace.generation_input@1"}, validate=validate_subject,
        capabilities=("subject.managed_actor", "subject.parallel")))
    for name, factory, input_kind, output in (
        ("reflect", ReflectStage, "reflection_input", "reflection"),
        ("curate", CurateStage, "curation_input", "curation"),
    ):
        registry.register(Plugin(ref=PluginRef(name=f"proteus.ace_{name}", version="1"), kind="stage",
            options=EmptyOptions, factory=lambda options, factory=factory: factory(), source="proteus",
            implementation_hash=digest, inputs={"request": f"proteus.ace.{input_kind}@1"},
            outputs={output: f"proteus.ace.{output}@1"}, capabilities=("stage.replay", "stage.parallel")))
