"""AIME solver, metric and reflective-data stage; all model calls are managed."""

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from proteus.benchmarks import aime_format
from proteus.core.contracts import OperationStatus, Outcome, PluginRef, RuntimeIdentity, StatePolicy, require
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.serialization import Contract, content_hash
from proteus.evaluation.records import Feedback, Grade, SubjectResult, Trace, zero_usage
from proteus.execution.actors import ChatActor, ChatOptions, validate_chat
from proteus.methods.gepa.state import ReflectionBatch, register_reflection_data


@dataclass(frozen=True, kw_only=True)
class SolverInput(Contract):
    instructions: str
    problem: str


class AimeActor(ChatActor):
    def execute(self, spec, request, workspace):
        require(not request.system, "AIME solver owns its reference system message")
        query = SolverInput.from_json(request.prompt)
        messages = aime_format.messages(query.instructions, query.problem)
        response = super().execute(spec, replace(request, system=messages[0]["content"], prompt=messages[1]["content"]), workspace)
        return replace(response, trace=({"kind": "aime-solver-request", "messages": messages,
            "format": "dspy-" + aime_format.DSPY_VERSION, "response": response.text},))


def validate_actor(spec, options):
    validate_chat(spec, options)
    require(spec.prompt_hash is None, "AIME solver does not consume a static actor prompt")


class AimeSubject:
    def run(self, context):
        instructions = (context.lease.local_root / "system.txt").read_text(encoding="utf-8")
        response = context.actor(SolverInput(instructions=instructions, problem=context.task.input["problem"]).to_json())
        require(response.status == OperationStatus.COMPLETED, response.diagnostic or "AIME solver failed")
        try:
            output = {**aime_format.parse(response.text), "format_error": None}
        except ValueError as exc:
            # An actual malformed model answer is scored behavior, never an
            # imputed infrastructure failure. No hidden formatter retry occurs.
            output = {"answer": None, "reasoning": "", "format_error": str(exc)}
        return SubjectResult(loaded_artifact_hash=context.candidate.artifact.content_hash,
            runtime=RuntimeIdentity(runtime=response.runtime.runtime, model=response.runtime.model,
                settings=response.runtime.settings, environment_hash=content_hash(context.execution.environment)),
            status=Outcome.TASK_FAILED if output["format_error"] else Outcome.SUCCEEDED,
            output=output, trace=Trace(events=response.trace), usage=response.usage)


def validate_subject(recipe, options, registry):
    require(recipe.subject.actor in recipe.actors, "AIME subject needs a managed actor")
    require("actor.aime_solver" in registry.get(recipe.actors[recipe.subject.actor].runtime.plugin).capabilities,
            "AIME subject needs its solver input protocol")
    artifact = recipe.subject.initial_artifact
    require(artifact.codec == PluginRef(name="proteus.text_components", version="1")
            and tuple(item.path for item in artifact.components) == ("system.txt",), "AIME mutates only system.txt")
    require(recipe.execution is not None and recipe.execution.environment.backend.plugin.name == "proteus.local_json",
            "AIME text subject currently uses the local JSON environment")
    require(recipe.execution.state_policy == StatePolicy.INDEPENDENT, "AIME problems must be independent")
    require(not recipe.subject.mutable_scopes, "AIME has no filesystem learning state")


class AimeMetric:
    def grade(self, task, rollout, reference):
        score, feedback = aime_format.math_metric(SimpleNamespace(**reference), SimpleNamespace(**rollout.output))
        return Grade(status=Outcome.SUCCEEDED if score else Outcome.TASK_FAILED, score=score,
                     feedback=Feedback(value={"text": feedback}), usage=zero_usage())


class AimeSideInfo:
    def run(self, context, inputs):
        examples = []
        for item in inputs["batch"].examples:
            output, feedback = item["Generated Outputs"], item["Feedback"]
            require(len(feedback["feedback"]) == 1, "AIME requires one metric feedback record")
            examples.append({"score": feedback["score"], "input": item["Inputs"]["problem"],
                "output": output["answer"], "reasoning": output["reasoning"],
                "execution_feedback": feedback["feedback"][0]["text"]})
        return {"dataset": ReflectionBatch.from_examples(examples)}


def register(registry):
    digest = implementation_hash({p.name: p for p in Path(__file__).parent.glob("aime*.py")})
    kind = "proteus.gepa.reflection_batch@1"
    # Domain plugins can be used by the baseline without registering a GEPA engine.
    register_reflection_data(registry)
    definitions = [
        ("aime_solver", "actor", ChatOptions, AimeActor, ("actor.execute", "actor.aime_solver", "calls.bounded", "session.fresh"), validate_actor),
        ("aime_subject", "subject", EmptyOptions, lambda options: AimeSubject(), ("subject.managed_actor",),
         lambda recipe, options: validate_subject(recipe, options, registry)),
        ("aime_metric", "evaluator", EmptyOptions, lambda options: AimeMetric(), ("evaluator.deterministic",), None),
        ("aime_side_info", "stage", EmptyOptions, lambda options: AimeSideInfo(), ("stage.replay",), None),
    ]
    for name, category, options, factory, capabilities, validate in definitions:
        registry.register(Plugin(ref=PluginRef(name="proteus." + name, version="1"), kind=category,
            options=options, factory=factory, implementation_hash=digest, source="proteus", capabilities=capabilities,
            validate=validate, inputs={"batch": kind} if category == "stage" else {},
            outputs={"dataset": kind} if category == "stage" else {}))
