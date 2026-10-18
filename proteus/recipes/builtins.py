"""Opt-in registry of bundled methods, stages and actors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from proteus.execution.harbor import (
    BACKEND_CAPABILITY, HarborEnvironment, HarborEnvironmentOptions, HarborReward, HarborRewardOptions,
    HarborSubject, HarborSubjectOptions, validate_environment as validate_harbor_environment,
    validate_subject as validate_harbor_subject,
)

from proteus.artifacts.codecs import BUILTIN_CODECS
from proteus.core.contracts import ArtifactRef, Candidate, OperationStatus, PluginRef, RuntimeIdentity, Usage, require
from proteus.core.methods import ActorResult, ArtifactProposal
from proteus.core.plugins import EmptyOptions, Plugin, Registry, implementation_hash
from proteus.core.serialization import Contract
from proteus.evidence.records import EvidenceView
from proteus.execution.code_records import CodeCall, CodeOptions, CodeResult, CodeState
from proteus.execution.workspace import WorkspaceStage
from proteus.execution.workspace_records import WorkspaceInput, WorkspaceOptions
from proteus.execution.actors import WorkspaceHarnessActor, validate_workspace_harness
from proteus.execution.code_subject import CodeSubject, validate_code
from proteus.execution.harbor_code import HarborCodeEnvironment, HarborCodeOptions, validate_harbor_code
from proteus.core.search import ArchiveSnapshot, KeepAll, ParetoFront, ScalarBest, SearchDecision, SelectionView, UniformParent
from proteus.execution.actors import (
    AdapterOptions, ChatActor, ChatOptions, HarnessActor, HarnessOptions, validate_chat, validate_harness,
)
from proteus.evaluation.records import EvaluationReceipt, Feedback, Observation, Trace
from proteus.evaluation.runners import (
    ExactMatch, LocalJsonEnvironment, PromptSubject, PromptSubjectOptions, PythonSubject,
    PythonSubjectOptions, validate_local, validate_prompt, validate_python,
)
from proteus.evaluation.tasks import JsonSuiteOptions, JsonTaskSuite, StateValue, SupervisedBatch, TaskBatch, TaskSelection
from proteus.core.contracts import EvaluationBatch
from proteus.methods.compare import CompareMethod, CompareOptions, CompareState, validate_compare
from proteus.methods.native import NativeMethod, NativeOptions, NativeSummary, validate_native
from proteus.methods.sequence import SequenceMethod, SequenceOptions, SequenceState, validate_sequence


@dataclass(frozen=True, kw_only=True)
class Text(Contract):
    text: str


class EchoActor:
    """Explicit offline fixture runtime; never presented as a live model."""

    model_call_bound = 0

    def execute(self, spec, request, workspace):
        return ActorResult(text=request.prompt, runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
                           usage=Usage(model_calls=0, input_tokens=0, output_tokens=0, cost_usd=0))


def validate_echo(spec, options):
    require(spec.model is None and not spec.model_settings and not spec.tools,
            "echo is an offline fixture with no model, settings or tools")


class PromptStage:
    def run(self, context, inputs):
        result = context.actor(inputs["prompt"].text)
        require(result.status == OperationStatus.COMPLETED, result.diagnostic or "actor failed")
        return {"response": result}


def builtin_registry() -> Registry:
    registry = Registry()
    root = Path(__file__).resolve().parents[1]
    digest = implementation_hash({str(path.relative_to(root)): path for path in root.rglob("*.py")})
    registry.register_data("proteus.text@1", Text)
    registry.register_data("proteus.actor_result@1", ActorResult)
    registry.register_data("proteus.native_summary@1", NativeSummary)
    registry.register_data("proteus.artifact_ref@1", ArtifactRef)
    registry.register_data("proteus.candidate@1", Candidate)
    registry.register_data("proteus.artifact_proposal@1", ArtifactProposal)
    registry.register_data("proteus.archive@1", ArchiveSnapshot)
    registry.register_data("proteus.evidence.view@1", EvidenceView)
    registry.register_data("proteus.code.call@1", CodeCall)
    registry.register_data("proteus.code.state@1", CodeState)
    registry.register_data("proteus.code.result@1", CodeResult)
    registry.register_data("proteus.workspace.input@1", WorkspaceInput)
    registry.register_data("proteus.search.view@1", SelectionView)
    registry.register_data("proteus.search.decision@1", SearchDecision)
    registry.register_data("proteus.eval.selection@1", TaskSelection)
    registry.register_data("proteus.eval.supervised_batch@1", SupervisedBatch)
    for kind, schema in (("task_batch", TaskBatch), ("state", StateValue), ("feedback", Feedback),
                         ("trace", Trace), ("batch", EvaluationBatch), ("receipt", EvaluationReceipt),
                         ("observation", Observation)):
        registry.register_data(f"proteus.eval.{kind}@1", schema)

    def add(name, kind, options, factory, *, capabilities=(), inputs=None, outputs=None, validate=None, state_schema=None):
        registry.register(Plugin(ref=PluginRef(name=name, version="1"), kind=kind, options=options,
            factory=factory, implementation_hash=digest, source="proteus", capabilities=capabilities,
            inputs=inputs or {}, outputs=outputs or {}, validate=validate, state_schema=state_schema))

    for name, codec in BUILTIN_CODECS.items():
        add(name, "codec", EmptyOptions, lambda options, codec=codec: codec(),
            capabilities=("artifact.immutable", "artifact.delta", "artifact.materialize"))
    for name, factory, roles in (("scalar_best", ScalarBest, ("parent", "final")),
                                 ("uniform_parent", UniformParent, ("parent",)),
                                 ("keep_all", KeepAll, ("archive",)),
                                 ("pareto_front", ParetoFront, ("archive", "final"))):
        add("proteus." + name, "policy", EmptyOptions, lambda options, factory=factory: factory(),
            capabilities=tuple("search." + role for role in roles))
    add("proteus.compare", "method", CompareOptions, CompareMethod, validate=validate_compare,
        capabilities=("run.resume", "run.journal"), state_schema=CompareState)
    add("proteus.json_tasks", "task_suite", JsonSuiteOptions, JsonTaskSuite)
    add("proteus.local_json", "environment", EmptyOptions, lambda options: LocalJsonEnvironment(),
        validate=validate_local, capabilities=("state.independent", "state.sequence", "state.explicit_json", "candidate.exact_tree",
                                             "environment.code_protocol"))
    add("proteus.harbor_code", "environment", HarborCodeOptions, HarborCodeEnvironment,
        validate=validate_harbor_code, capabilities=("state.independent", "state.sequence", "state.explicit_json",
            "candidate.exact_tree", "environment.code_protocol", "environment.source_build"))
    add("proteus.python_subject", "subject", PythonSubjectOptions, PythonSubject, validate=validate_python)
    add("proteus.code_subject", "subject", CodeOptions, CodeSubject, validate=validate_code,
        capabilities=("subject.managed_actor", "subject.actor_timeout", "subject.code_protocol"),
        inputs={"memory": "proteus.code.state@1"})
    add("proteus.prompt_subject", "subject", PromptSubjectOptions, PromptSubject, validate=validate_prompt,
        capabilities=("subject.managed_actor",))
    add("proteus.exact_match", "evaluator", EmptyOptions, lambda options: ExactMatch(),
        capabilities=("evaluator.deterministic",))
    add("proteus.harbor", "environment", HarborEnvironmentOptions, HarborEnvironment,
        validate=validate_harbor_environment,
        capabilities=("state.independent", "candidate.exact_tree", "environment.source_build"))
    add("proteus.harbor_subject", "subject", HarborSubjectOptions, HarborSubject,
        validate=validate_harbor_subject, capabilities=(BACKEND_CAPABILITY, "subject.reconcile"))
    add("proteus.harbor_reward", "evaluator", HarborRewardOptions, HarborReward,
        capabilities=(BACKEND_CAPABILITY,))
    add("proteus.sequence", "method", SequenceOptions, SequenceMethod,
        validate=lambda recipe, options: validate_sequence(recipe, options, registry),
        capabilities=("control.sequence", "control.bounded_loop", "control.condition", "run.resume", "run.journal"),
        state_schema=SequenceState)
    add("proteus.native", "method", NativeOptions, NativeMethod, validate=validate_native,
        capabilities=("run.resume", "legacy.episode"))
    add("proteus.prompt", "stage", EmptyOptions, lambda options: PromptStage(),
        capabilities=("stage.replay",),
        inputs={"prompt": "proteus.text@1"}, outputs={"response": "proteus.actor_result@1"})
    add('proteus.workspace', 'stage', WorkspaceOptions, WorkspaceStage,
        capabilities=('stage.replay', 'stage.workspace'),
        inputs={'prompt': 'proteus.text@1', 'seed': 'proteus.artifact_ref@1', 'history': 'proteus.evidence.view@1'},
        outputs={'response': 'proteus.actor_result@1'})
    add("proteus.echo", "actor", EmptyOptions, lambda options: EchoActor(), validate=validate_echo,
        capabilities=("session.fresh", "actor.execute", "calls.bounded", "request.timeout"))
    add("proteus.chat", "actor", ChatOptions, ChatActor, validate=validate_chat,
        capabilities=("session.fresh", "actor.execute", "calls.bounded", "request.timeout"))
    for name in ("minimal", "llm", "dsh", "pi", "codex"):
        if name in ('dsh', 'pi', 'codex'):
            add(f'proteus.workspace.{name}', 'actor', HarnessOptions,
                lambda options, name=name: WorkspaceHarnessActor(name, options),
                validate=lambda spec, options, name=name: validate_workspace_harness(name, spec, options),
                capabilities=('session.fresh', 'actor.execute', 'actor.workspace', 'trace.normalized'))
        def validate(spec, options, name=name):
            return validate_harness(name, spec, options)

        def factory(options, name=name):
            return HarnessActor(name, options)
        add(f"proteus.harness.{name}", "actor", HarnessOptions, factory, validate=validate,
            capabilities=("session.fresh", "actor.execute", "trace.normalized"))
        continuity = "fresh" if name in ("minimal", "llm") else "handoff"
        add(f"proteus.native.{name}", "actor", AdapterOptions, factory, validate=validate,
            capabilities=(f"session.{continuity}", "legacy.episode", "trace.normalized")
            + (("candidate.staged_activation",) if name in ("dsh", "pi", "codex") else ()))
    return registry
