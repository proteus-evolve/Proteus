"""Native file evolution through the common journal, actor and evaluation services.

This explicit profile uses last-valid activation without outer score selection.
Legacy `proteus.native` keeps its original CLI protocol and evaluator schedules.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

from proteus.artifacts.codecs import components
from proteus.core.budget import PHASES, make_budget_plan, validate_phases
from proteus.core.contracts import DataRef, FeedbackVisibility, OperationStatus, require
from proteus.core.methods import MethodResult, supported_budget
from proteus.core.serialization import Contract, content_hash
from proteus.execution.workspace_records import HarnessEpisode


@dataclass(frozen=True, kw_only=True)
class NativeEvolutionOptions(Contract):
    episode_stage: str
    evaluations: tuple[str, ...] = field(metadata={'minItems': 1, 'uniqueItems': True})
    test_evaluations: tuple[str, ...] = field(default=(), metadata={'uniqueItems': True})
    episodes: int = field(default=2, metadata={'minimum': 1})
    goal: str = 'none'
    phases: tuple[str, ...] = PHASES
    phase_prompts: Mapping[str, str] = field(default_factory=dict)
    max_turns: int = field(default=100, metadata={'minimum': 0})
    min_turns_per_phase: int = field(default=0, metadata={'minimum': 0})
    phase_turns: Mapping[str, int] = field(default_factory=dict)
    hard_max_turns: int = field(default=0, metadata={'minimum': 0})
    checkpoint_turns: int = field(default=0, metadata={'minimum': 0})
    announce_budget: bool = False
    max_inline_evidence_bytes: int = field(default=65536, metadata={"minimum": 0})

    def _validate(self):
        validate_phases(self.phases)
        make_budget_plan(max_turns=self.max_turns, min_turns_per_phase=self.min_turns_per_phase,
            phase_turns=self.phase_turns, hard_max_turns=self.hard_max_turns,
            checkpoint_turns=self.checkpoint_turns, phases=self.phases)
        require(not self.checkpoint_turns or self.announce_budget, 'checkpoint turns require announce_budget')
        require(not set(self.evaluations) & set(self.test_evaluations), 'Native evaluation IDs overlap')
        require(not set(self.phase_prompts) - set(self.phases), 'unused phase prompts')


@dataclass(frozen=True, kw_only=True)
class NativeEvolutionState(Contract):
    working: str
    active: str
    baseline_complete: bool = False
    episode_cursor: int = 0
    repair: str = ''
    outputs: Mapping[str, DataRef] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class NativeEvolutionSummary(Contract):
    episodes_complete: int
    working_candidate_id: str
    active_candidate_id: str
    pending_repair: str


def validate_native_evolution(recipe, options, registry):
    from proteus.execution.harnesses import IntegratedHarnessActor
    require(recipe.subject is not None and recipe.execution is not None, 'Native evolution needs subject/execution')
    require(recipe.subject.runner.plugin.name == 'proteus.harness_subject'
            and recipe.subject.initial_artifact.codec.name == 'proteus.file_tree',
            'Native evolution requires the common harness subject and complete file-tree candidates')
    require(recipe.tasks is None and not recipe.search, 'Native evolution consumes declared evaluation pools')
    require(len(recipe.stages) == 1 and recipe.stages[0].id == options.episode_stage,
            'Native evolution requires exactly its episode stage')
    stage = recipe.stages[0]
    require('stage.harness_episode' in registry.get(stage.operator.plugin).capabilities and stage.max_attempts == 1
            and stage.failure_policy == 'record', 'Native episode stage must record a single managed attempt')
    actor, target = recipe.actors[stage.role], recipe.actors[recipe.subject.actor]
    runtime, subject = (registry.create(spec.runtime, 'actor') for spec in (actor, target))
    require(isinstance(runtime, IntegratedHarnessActor) and runtime.role == 'evolution',
            'episode role needs a registered harness evolution binding')
    require(isinstance(subject, IntegratedHarnessActor) and subject.role == 'actor'
            and runtime.integration == subject.integration, 'self evolution must execute the subject integration')
    require(actor.session.value == target.session.value == 'fresh' and actor.prompt_hash is None,
            'Native evolution uses fresh outer roles and explicit episode prompts')
    require(recipe.subject.initial_artifact.runtime in (None, target.runtime.plugin),
            'initial harness artifact runtime differs from subject role')
    if options.checkpoint_turns:
        require(runtime.integration.continuity != 'fresh', 'checkpoint turns need adapter continuity')
    evaluations = {item.id: item for item in recipe.evaluations}
    require(set(evaluations) == set((*options.evaluations, *options.test_evaluations)), 'unused Native evaluations')
    require(all(evaluations[key].visibility == FeedbackVisibility.OBSERVED
                and evaluations[key].purpose != 'test' for key in options.evaluations),
            'Native optimization requires observed evaluations')
    require(all(evaluations[key].purpose == 'test' for key in options.test_evaluations),
            'Native final evaluations must be hidden test')
    require(stage.evidence is not None and set(stage.evidence.evaluation_ids) == set(options.evaluations)
            and set(stage.evidence.splits) == {evaluations[key].tasks.split for key in options.evaluations},
            'Native episode evidence must cover exactly the observed evaluation pools')
    supported_budget(recipe.budget, allow_evaluations=True)
    supported_budget(stage.budget)
    for item in recipe.actors.values():
        supported_budget(item.budget)


def inline_evidence(services, history, maximum):
    """Only the stage's immutable released view may enter inline prompts."""
    from proteus.evidence.service import EvidenceService
    view, evidence = services.values.get(history), EvidenceService(services)
    evidence.bound(view, view.stage_id, verify_files=True)
    priorities = {'feedback': 0, 'evaluation': 1, 'rollout': 2, 'task': 3, 'trace': 4}
    entries = sorted((e for e in view.entries if e.kind in priorities),
                     key=lambda e: (priorities[e.kind], e.path))
    parts, used, omitted = [], 0, 0
    for entry in entries:
        item = entry.path + '\n' + evidence.store.read(history, entry.path).decode('utf-8')
        size = len(item.encode('utf-8'))
        if size > maximum - used:
            omitted += 1
            continue
        parts.append(item)
        used += size
    if not parts and not omitted:
        return ''
    notice = ('Released evidence from the declared task/history/ view. '
              f'{omitted} entries omitted from inline context by the explicit byte cap; '
              'the complete same view is available in task/history/.')
    return notice + '\n\n' + '\n\n'.join(parts)


class NativeEvolutionMethod:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        options = self.options
        stage = services.recipe.stages[0]
        def candidate(identity):
            return next(item for item in services.candidates if item.id == identity)

        def evaluate(current, prefix, ids, outputs):
            for key in ids:
                receipt = services.evaluate(current, key, operation_id=f'{prefix}-{key}')
                outputs = {**outputs, f'{prefix}:{key}': services.values.publish('proteus.eval.receipt@1', receipt)}
            return outputs

        if restored_state is None:
            initial = services.initial_candidate()
            state = NativeEvolutionState(working=initial.id, active=initial.id)
            checkpoint = services.checkpoint(state)
        else:
            state = NativeEvolutionState.from_dict(restored_state.state.payload)
            checkpoint = restored_state
        if not state.baseline_complete:
            outputs = evaluate(candidate(state.active), 'initial', options.evaluations, state.outputs)
            state = replace(state, baseline_complete=True, outputs=outputs)
            checkpoint = services.checkpoint(state)
        # Reuse the core phase protocol without passing hidden grades or a private
        # RunConfig root into the actor. Actual adapter construction is dispatch-only.
        from proteus.cli import _goal
        from proteus.core.disposition import NEUTRAL
        from proteus.core.episode import _phase_prompts
        runtime = services.registry.create(services.recipe.actors[stage.role].runtime, 'actor')
        integration = runtime.integration
        cfg = SimpleNamespace(goal=_goal(options.goal, ()), phases=options.phases, phase_prompts=options.phase_prompts,
            adapter=SimpleNamespace(continuity_mode={'fresh': 'none', 'handoff': 'framework', 'native': 'native'}[
                integration.continuity], staged_activation='harness.staged_activation' in integration.capabilities),
            disposition=NEUTRAL, announce_budget=options.announce_budget, max_turns=options.max_turns,
            hard_max_turns=options.hard_max_turns, min_turns_per_phase=options.min_turns_per_phase,
            phase_turns=options.phase_turns)
        for index in range(state.episode_cursor, options.episodes):
            number = index + 1
            history = services.evidence_view(stage.id, candidate_ids=(state.active,))
            prompts = _phase_prompts(cfg, inline_evidence(services, history, options.max_inline_evidence_bytes),
                f'Repair the exact pending candidate; viability error: {state.repair}' if state.repair else '')
            control = HarnessEpisode(number=number, prompts=prompts, phases=options.phases, seed=services.recipe.seed,
                **{key: getattr(options, key) for key in ('max_turns', 'min_turns_per_phase', 'phase_turns',
                    'hard_max_turns', 'checkpoint_turns', 'announce_budget')})
            inputs = {'seed': services.values.publish('proteus.artifact_ref@1', candidate(state.working).artifact),
                'active': services.values.publish('proteus.artifact_ref@1', candidate(state.active).artifact),
                'episode': services.values.publish('proteus.harness_episode@1', control),
                'history': history}
            result = services.stage(stage.id, inputs, operation_id=f'episode-{number}')
            if result.status != OperationStatus.COMPLETED:
                return MethodResult(status=result.status, state=checkpoint, diagnostic=result.diagnostic)
            response = services.values.get(result.outputs['response'])
            artifact = response.workspace_artifact
            proof = response.runtime.settings.get('candidate_viability', {})
            require(type(proof.get('valid')) is bool and proof.get('tree_hash') == content_hash(
                components(services.artifacts.files(artifact))), 'candidate viability is not bound to the published output')
            next_candidate = services.create_candidate(artifact, candidate_id=f'episode-{number}',
                parents=(candidate(state.working),), operation_id=f'candidate-{number}')
            outputs = dict(state.outputs)
            active, repair = state.active, proof['diagnostic']
            if proof['valid']:
                active = next_candidate.id
                outputs = evaluate(next_candidate, f'episode-{number}', options.evaluations, outputs)
            state = replace(state, working=next_candidate.id, active=active, repair=repair,
                episode_cursor=number, outputs=outputs)
            checkpoint = services.checkpoint(state)
        outputs = evaluate(candidate(state.active), 'final', options.test_evaluations, state.outputs)
        summary = NativeEvolutionSummary(episodes_complete=state.episode_cursor, working_candidate_id=state.working,
            active_candidate_id=state.active, pending_repair=state.repair)
        outputs = {**outputs, 'summary': services.values.publish('proteus.native_evolution_summary@1', summary)}
        state = replace(state, outputs=outputs)
        checkpoint = services.checkpoint(state)
        return MethodResult(outputs=outputs, state=checkpoint)
