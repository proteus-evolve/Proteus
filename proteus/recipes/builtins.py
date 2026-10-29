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
from proteus.core.parallel import StageJob, StageMapResult
from proteus.core.plugins import EmptyOptions, Plugin, Registry, implementation_hash
from proteus.core.serialization import Contract
from proteus.evidence.records import EvidenceView
from proteus.execution.code_records import CodeCall, CodeOptions, CodeResult, CodeState
from proteus.execution.workspace import WorkspaceStage
from proteus.execution.workspace_records import HarnessEpisode, WorkspaceInput, WorkspaceOptions
from proteus.execution.harness_evolution import HarnessEpisodeStage
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
from proteus.methods.native_evolution import (NativeEvolutionMethod, NativeEvolutionOptions,
    NativeEvolutionState, NativeEvolutionSummary, validate_native_evolution)
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
    from proteus.evaluation.parallel import EvaluationMap
    registry.register_data("proteus.eval.map@1", EvaluationMap)
    root = Path(__file__).resolve().parents[1]
    digest = implementation_hash({str(path.relative_to(root)): path for path in root.rglob("*.py")})
    registry.register_data("proteus.text@1", Text)
    registry.register_data("proteus.actor_result@1", ActorResult)
    registry.register_data("proteus.stage_job@1", StageJob)
    registry.register_data("proteus.stage_map@1", StageMapResult)
    from proteus.core.workflows import WorkflowJob, WorkflowResult, WorkflowMapResult
    registry.register_data("proteus.workflow_job@1", WorkflowJob)
    registry.register_data("proteus.workflow_result@1", WorkflowResult)
    registry.register_data("proteus.workflow_map@1", WorkflowMapResult)
    from proteus.core.timing import TimingStart, TimingMeasurement
    registry.register_data("proteus.timing.start@1", TimingStart)
    registry.register_data("proteus.timing.measurement@1", TimingMeasurement)
    registry.register_data("proteus.native_summary@1", NativeSummary)
    registry.register_data("proteus.harness_episode@1", HarnessEpisode)
    registry.register_data("proteus.native_evolution_summary@1", NativeEvolutionSummary)
    registry.register_data("proteus.artifact_ref@1", ArtifactRef)
    registry.register_data("proteus.candidate@1", Candidate)
    registry.register_data("proteus.artifact_proposal@1", ArtifactProposal)
    registry.register_data("proteus.archive@1", ArchiveSnapshot)
    registry.register_data("proteus.evidence.view@1", EvidenceView)
    from proteus.knowledge.records import KnowledgeRevision, KnowledgeSnapshot
    registry.register_data("proteus.knowledge.revision@1", KnowledgeRevision)
    registry.register_data("proteus.knowledge.snapshot@1", KnowledgeSnapshot)
    from proteus.workers.records import CommandWorkerOptions, WorkerCompletion, WorkerRef, WorkerSeed
    registry.register_data("proteus.worker.ref@1", WorkerRef)
    registry.register_data("proteus.worker.completion@1", WorkerCompletion)
    registry.register_data("proteus.worker.seed@1", WorkerSeed)
    from proteus.gateway.records import GatewayClosure, GatewayResult, GatewayTicket
    registry.register_data("proteus.gateway.ticket@1", GatewayTicket)
    registry.register_data("proteus.gateway.result@1", GatewayResult)
    registry.register_data("proteus.gateway.closure@1", GatewayClosure)
    from proteus.events.records import EventDecision, InterventionPlan, InterventionDispatch
    registry.register_data("proteus.event.decision@1", EventDecision)
    registry.register_data("proteus.event.plan@1", InterventionPlan)
    registry.register_data("proteus.event.dispatch@1", InterventionDispatch)
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
    from proteus.workers.runtime import CommandWorkerRuntime
    from proteus.events.policies import CadenceOptions, CadencePolicy, CadenceState
    add("proteus.event.cadence", "event_policy", CadenceOptions, CadencePolicy,
        capabilities=("event.pure_transition",), state_schema=CadenceState)
    add("proteus.worker.command", "worker", CommandWorkerOptions, lambda options: CommandWorkerRuntime(),
        capabilities=("worker.lifecycle", "worker.reconcile", "worker.session_protocol", "worker.connection_environment"))
    from proteus.workers.harbor import HarborWorkerOptions, HarborWorkerRuntime, validate_harbor_workers
    add("proteus.worker.harbor", "worker", HarborWorkerOptions, lambda options: HarborWorkerRuntime(),
        capabilities=("worker.lifecycle", "worker.reconcile", "worker.session_protocol", "worker.connection_environment",
                      "worker.container_isolation"), validate=validate_harbor_workers)
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
                                             "environment.code_protocol", "environment.parallel"))
    add("proteus.harbor_code", "environment", HarborCodeOptions, HarborCodeEnvironment,
        validate=validate_harbor_code, capabilities=("state.independent", "state.sequence", "state.explicit_json",
            "candidate.exact_tree", "environment.code_protocol", "environment.source_build", "environment.parallel",
            "environment.filesystem_scopes", "environment.pids"))
    add("proteus.python_subject", "subject", PythonSubjectOptions, PythonSubject, validate=validate_python,
        capabilities=("subject.parallel",))
    from proteus.execution.harness_candidate import (
        HarnessEnvironment, HarnessEnvironmentOptions, HarnessSubject, HarnessSubjectOptions,
        validate_environment as validate_harness_environment, validate_subject as validate_harness_subject,
    )
    add('proteus.harness_environment', 'environment', HarnessEnvironmentOptions, HarnessEnvironment,
        validate=validate_harness_environment,
        capabilities=('state.independent', 'candidate.exact_tree', 'environment.harness_tree'))
    add('proteus.harness_subject', 'subject', HarnessSubjectOptions, HarnessSubject,
        validate=lambda recipe, options: validate_harness_subject(recipe, options, registry),
        capabilities=('subject.runtime_actor',))
    add("proteus.code_subject", "subject", CodeOptions, CodeSubject, validate=validate_code,
        capabilities=("subject.managed_actor", "subject.actor_timeout", "subject.code_protocol", "subject.parallel"),
        inputs={"memory": "proteus.code.state@1"})
    add("proteus.prompt_subject", "subject", PromptSubjectOptions, PromptSubject, validate=validate_prompt,
        capabilities=("subject.managed_actor", "subject.parallel"))
    add("proteus.exact_match", "evaluator", EmptyOptions, lambda options: ExactMatch(),
        capabilities=("evaluator.deterministic",))
    from proteus.evaluation.graders import ModelJudge, ModelJudgeOptions
    add('proteus.model_judge', 'evaluator', ModelJudgeOptions, ModelJudge,
        capabilities=('evaluator.managed_actor',))
    add("proteus.harbor", "environment", HarborEnvironmentOptions, HarborEnvironment,
        validate=validate_harbor_environment,
        capabilities=("state.independent", "candidate.exact_tree", "environment.source_build", "environment.parallel"))
    add("proteus.harbor_subject", "subject", HarborSubjectOptions, HarborSubject,
        validate=validate_harbor_subject, capabilities=(BACKEND_CAPABILITY, "subject.reconcile", "subject.parallel"))
    add("proteus.harbor_reward", "evaluator", HarborRewardOptions, HarborReward,
        capabilities=(BACKEND_CAPABILITY,))
    add("proteus.sequence", "method", SequenceOptions, SequenceMethod,
        validate=lambda recipe, options: validate_sequence(recipe, options, registry),
        capabilities=("control.sequence", "control.bounded_loop", "control.condition", "run.resume", "run.journal"),
        state_schema=SequenceState)
    add("proteus.native", "method", NativeOptions, NativeMethod,
        validate=lambda recipe, options: validate_native(recipe, options, registry),
        capabilities=("run.resume", "legacy.episode"))
    add("proteus.native_evolution", "method", NativeEvolutionOptions, NativeEvolutionMethod,
        validate=lambda recipe, options: validate_native_evolution(recipe, options, registry),
        capabilities=("run.resume", "run.journal", "control.native_episode"), state_schema=NativeEvolutionState)
    add("proteus.prompt", "stage", EmptyOptions, lambda options: PromptStage(),
        capabilities=("stage.replay", "stage.parallel"),
        inputs={"prompt": "proteus.text@1"}, outputs={"response": "proteus.actor_result@1"})
    add('proteus.workspace', 'stage', WorkspaceOptions, WorkspaceStage,
        capabilities=('stage.replay', 'stage.workspace', 'stage.parallel'),
        inputs={'prompt': 'proteus.text@1', 'seed': 'proteus.artifact_ref@1', 'history': 'proteus.evidence.view@1'},
        outputs={'response': 'proteus.actor_result@1'})
    add('proteus.harness_episode', 'stage', WorkspaceOptions, HarnessEpisodeStage,
        capabilities=('stage.replay', 'stage.workspace', 'stage.harness_episode'),
        inputs={'seed': 'proteus.artifact_ref@1', 'active': 'proteus.artifact_ref@1',
                'episode': 'proteus.harness_episode@1', 'history': 'proteus.evidence.view@1'},
        outputs={'response': 'proteus.actor_result@1'})
    add("proteus.echo", "actor", EmptyOptions, lambda options: EchoActor(), validate=validate_echo,
        capabilities=("session.fresh", "actor.execute", "calls.bounded", "request.timeout", "actor.parallel"))
    add("proteus.chat", "actor", ChatOptions, ChatActor, validate=validate_chat,
        capabilities=("session.fresh", "actor.execute", "calls.bounded", "request.timeout", "actor.parallel"))
    from proteus.execution.harnesses import HarnessIntegration, register_harness
    for name in ("minimal", "llm", "dsh", "pi", "codex"):
        roles = {'actor': HarnessOptions, 'native': AdapterOptions}
        bindings = {'actor': f'proteus.harness.{name}', 'native': f'proteus.native.{name}'}
        capabilities = ['harness.custom_phases']
        if name == 'minimal':
            capabilities.append('harness.model_free')
        else:
            capabilities.append('harness.candidate_tree')
            roles['evolution'] = AdapterOptions
            bindings['evolution'] = f'proteus.evolution.{name}'
        if name in ('dsh', 'pi', 'codex'):
            roles['workspace'] = HarnessOptions
            bindings['workspace'] = f'proteus.workspace.{name}'
            capabilities.extend(('harness.workspace', 'harness.staged_activation'))

        def adapter(role, spec, options, name=name):
            runtime = WorkspaceHarnessActor(name, options) if role == 'workspace' else HarnessActor(name, options)
            return runtime.adapter(spec)

        def validate(role, spec, options, name=name):
            return (validate_workspace_harness(name, spec, options) if role == 'workspace'
                    else validate_harness(name, spec, options))

        def model_selection(role, spec, options, name=name):
            runtime = WorkspaceHarnessActor(name, options) if role == 'workspace' else HarnessActor(name, options)
            return runtime.model_selection(spec)

        continuity = "fresh" if name in ("minimal", "llm") else "handoff"
        register_harness(registry, HarnessIntegration(name=name, factory=adapter, options=roles,
            implementation_hash=digest, source='proteus', capabilities=tuple(capabilities),
            continuity=continuity, validate=validate, model_selection=model_selection), bindings=bindings)
    return registry
