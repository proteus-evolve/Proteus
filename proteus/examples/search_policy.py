"""An offline policy ablation using identical candidates and formal task scores."""

import argparse
from dataclasses import dataclass, field, replace
import math
from pathlib import Path

from proteus.artifacts.codecs import FileContent, FileDelta, FileWrite
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import BudgetLimits, DataRef, FeedbackVisibility, Outcome, PluginRef, require
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.search import PolicyResult
from proteus.core.serialization import Contract, content_hash
from proteus.evaluation.records import Grade, zero_usage
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.examples.candidate_evaluation import plugin
from proteus.methods.compare import Variant
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import (
    EnvironmentSpec, EvaluationSpec, ExecutionSpec, RecipeConfig, ReferenceSpec, SubjectSpec, TaskSuiteSpec,
)


class Maximin:
    """Prefer the best worst-task score; stable archive order breaks exact ties."""

    def select_final(self, view, rng):
        require(bool(view.vectors) and not view.objectives, 'maximin requires visible per-task scores')
        best = max(view.vectors, key=lambda item: min(item.scores))
        return PolicyResult(selected=(best.candidate_id,), reasons={item.candidate_id:
            'highest minimum task score; earliest tie' if item is best else 'lower minimum or later tie'
            for item in view.vectors})


class NumericScore:
    def grade(self, task, rollout, reference):
        if rollout.status != Outcome.SUCCEEDED:
            return Grade(status=rollout.status, score=None, usage=zero_usage())
        try:
            error = abs(float(reference)-float(rollout.output))
            score = max(0.0, 1.0-error) if math.isfinite(error) else 0.0
        except (ValueError, TypeError, OverflowError):
            score = 0.0
        return Grade(status=Outcome.SUCCEEDED, score=score, usage=zero_usage())


@dataclass(frozen=True, kw_only=True)
class Options(Contract):
    variants: tuple[Variant, ...]


@dataclass(frozen=True, kw_only=True)
class State(Contract):
    candidates: tuple[str, ...]
    evaluated: int = field(default=0, metadata={'minimum': 0})
    chosen: str | None = None
    decision: DataRef | None = None
    test: DataRef | None = None


def validate(recipe, options):
    require(recipe.subject is not None and recipe.execution is not None, 'example needs candidate execution')
    require(set(recipe.search) == {'final'} and not recipe.stages and recipe.tasks is None,
            'example consumes one final policy and declared evaluations')
    require({item.id for item in recipe.evaluations} == {'validation', 'test'}, 'example needs validation/test')
    require(len({v.id for v in options.variants}) == len(options.variants)
            and all(v.id != 'initial' for v in options.variants), 'variant ids must be distinct')


class PolicyStudy:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        if restored_state is None:
            initial = services.initial_candidate()
            candidates = [initial]
            for variant in self.options.variants:
                candidates.append(services.fork_candidate(initial, candidate_id=variant.id,
                    delta=FileDelta.from_dict(variant.delta)))
            state = State(candidates=tuple(item.id for item in candidates))
            checkpoint = services.checkpoint(state)
        else:
            state = State.from_dict(restored_state.state.payload)
            checkpoint = restored_state
        candidates = {item.id: item for item in services.candidates}
        for index in range(state.evaluated, len(state.candidates)):
            services.evaluate(candidates[state.candidates[index]], 'validation', operation_id=f'validation-{index}')
            state = replace(state, evaluated=index+1)
            checkpoint = services.checkpoint(state)
        if state.decision is None:
            archive = services.archive_snapshot()
            view = services.selection_view(archive, 'validation')
            decision = services.select_final('final', view)
            require(len(decision.result.selected) == 1, 'example requires one final candidate')
            state = replace(state, chosen=decision.result.selected[0],
                decision=services.values.publish('proteus.search.decision@1', decision))
            checkpoint = services.checkpoint(state)
        if state.test is None:
            receipt = services.evaluate(candidates[state.chosen], 'test', operation_id='final-test')
            state = replace(state, test=services.values.publish('proteus.eval.receipt@1', receipt))
            checkpoint = services.checkpoint(state)
        return MethodResult(outputs={'selection': state.decision, 'test': state.test}, state=checkpoint)


def register(registry):
    digest = implementation_hash({'search_policy.py': Path(__file__)})
    registry.register(Plugin(ref=PluginRef(name='example.maximin', version='1'), kind='policy',
        options=EmptyOptions, factory=lambda options: Maximin(), source='proteus', implementation_hash=digest,
        capabilities=('search.final',)))
    registry.register(Plugin(ref=PluginRef(name='example.numeric_score', version='1'), kind='evaluator',
        options=EmptyOptions, factory=lambda options: NumericScore(), source='proteus', implementation_hash=digest,
        capabilities=('evaluator.deterministic',)))
    registry.register(Plugin(ref=PluginRef(name='example.policy_study', version='1'), kind='method',
        options=Options, factory=PolicyStudy, source='proteus', implementation_hash=digest, validate=validate,
        state_schema=State, capabilities=('run.resume', 'run.journal')))


def prepare(root, *, policy='proteus.scalar_best'):
    root = Path(root).resolve()
    require(policy in ('proteus.scalar_best', 'example.maximin'), 'choose scalar_best or example.maximin')
    require(not root.exists() or not any(root.iterdir()), 'example directory already contains data')
    root.mkdir(parents=True, exist_ok=True)
    registry = builtin_registry()
    register(registry)
    store = ArtifactStore(root/'artifact-store', registry)

    def program(values):
        return ('import json,sys\nr=json.load(sys.stdin)\n'
                f'values={values!r}\n'
                'print(json.dumps({"output":values[r["task"]["input"]],"state":r["state"]}))\n').encode()

    initial = store.put(PluginRef(name='proteus.file_tree', version='1'),
        {'main.py': FileContent(program([.9, .1, .2]))})
    variants = tuple(Variant(id=name, delta=FileDelta(base_hash=initial.content_hash,
        put=(FileWrite.from_bytes('main.py', program(values)),)).to_dict())
        for name, values in [('balanced', [.6, .6, .4]), ('spiky', [1.0, .3, .8])])
    dataset = TaskDataset(version='offline-policy-ablation-v1', records=tuple(
        TaskRecord(task=Task(id=name, version='1', split=split, input=index), reference=1.0)
        for index, (name, split) in enumerate([('easy', 'validation'), ('hard', 'validation'), ('unseen', 'test')])))
    data = root/'tasks.json'
    data.write_text(dataset.to_json()+'\n')
    evaluations = tuple(EvaluationSpec(id=split, evaluator=plugin('example.numeric_score'),
        tasks=TaskSuiteSpec(suite=plugin('proteus.json_tasks', path=str(data)), dataset_version=dataset.version,
            dataset_hash=content_hash(dataset), split=split, task_ids=ids), purpose=split,
        visibility=FeedbackVisibility.OBSERVED if split == 'validation' else FeedbackVisibility.HIDDEN,
        selection_eligible=split == 'validation', feedback_timing='after_batch' if split == 'validation' else 'never')
        for split, ids in [('validation', ('easy', 'hard')), ('test', ('unseen',))])
    recipe = RecipeConfig(name='offline-search-policy-ablation', actors={}, reference=ReferenceSpec(profile='native'),
        method=plugin('example.policy_study', variants=[v.to_dict() for v in variants]),
        subject=SubjectSpec(runner=plugin('proteus.python_subject'), initial_artifact=initial),
        execution=ExecutionSpec(environment=EnvironmentSpec(backend=plugin('proteus.local_json'), provider='host',
            network='unrestricted'), timeout_seconds=10), evaluations=evaluations,
        search={'final': plugin(policy)}, budget=BudgetLimits(max_evaluations=7))
    path = root/'recipe.json'
    path.write_text(registry.resolve(recipe).to_json()+'\n')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--policy', choices=('proteus.scalar_best', 'example.maximin'), default='proteus.scalar_best')
    args = parser.parse_args()
    print(prepare(args.out, policy=args.policy))


if __name__ == '__main__':
    main()
