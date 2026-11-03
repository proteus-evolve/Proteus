"""A parent-policy ablation: one fixed update is applied to the selected program."""

import argparse
from dataclasses import dataclass, field, replace
import json
from pathlib import Path

from proteus.api.v1 import (
    ArtifactStore, BudgetLimits, Contract, DataRef, EmptyOptions, EnvironmentSpec, EvaluationSpec, ExecutionSpec,
    FeedbackVisibility, FileContent, FileDelta, FileWrite, MethodResult, Plugin, PluginRef, PluginSpec,
    RecipeConfig, ReferenceSpec, SubjectSpec, Task, TaskDataset, TaskRecord, TaskSuiteSpec,
    builtin_registry, content_hash, implementation_hash, require,
)
from proteus.examples.search_policy import NumericScore


@dataclass(frozen=True, kw_only=True)
class Variant(Contract):
    id: str
    values: tuple[float, ...] = field(metadata={'minItems': 3, 'maxItems': 3})


@dataclass(frozen=True, kw_only=True)
class Options(Contract):
    variants: tuple[Variant, ...]
    increment: float = field(default=.05, metadata={'minimum': 0, 'maximum': 1})


@dataclass(frozen=True, kw_only=True)
class State(Contract):
    parents: tuple[str, ...]
    evaluated: int = field(default=0, metadata={'minimum': 0})
    parent_decision: DataRef | None = None
    parent_id: str | None = None
    child_id: str | None = None
    validation: DataRef | None = None
    test: DataRef | None = None


def spec(name, **options):
    return PluginSpec(plugin=PluginRef(name=name, version='1'), options=options)


def validate(recipe, options):
    require(recipe.subject is not None and recipe.execution is not None, 'parent study needs candidate execution')
    require(set(recipe.search) == {'parent'} and not recipe.stages and recipe.tasks is None,
            'parent study consumes one parent policy and formal evaluations')
    require({item.id for item in recipe.evaluations} == {'validation', 'test'}, 'parent study needs validation/test')
    ids = [variant.id for variant in options.variants]
    require(len(set(ids)) == len(ids) and not set(ids).intersection({'initial', 'child'}), 'variant ids collide')


class ParentStudy:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        if restored_state is None:
            initial = services.initial_candidate()
            parents = [initial]
            for variant in self.options.variants:
                delta = FileDelta(base_hash=initial.artifact.content_hash, put=(FileWrite.from_bytes(
                    'values.json', json.dumps(list(variant.values)).encode()),))
                parents.append(services.fork_candidate(initial, delta=delta, candidate_id=variant.id))
            state = State(parents=tuple(item.id for item in parents))
            checkpoint = services.checkpoint(state)
        else:
            state, checkpoint = State.from_dict(restored_state.state.payload), restored_state
        candidates = {item.id: item for item in services.candidates}
        for index in range(state.evaluated, len(state.parents)):
            services.evaluate(candidates[state.parents[index]], 'validation', operation_id=f'parent-validation-{index}')
            state = replace(state, evaluated=index+1)
            checkpoint = services.checkpoint(state)
        if state.parent_decision is None:
            archive = services.archive_snapshot()
            view = services.selection_view(archive, 'validation', candidate_ids=state.parents)
            decision = services.select_parents('parent', view)
            require(len(decision.result.selected) == 1, 'parent study needs exactly one parent')
            state = replace(state, parent_id=decision.result.selected[0],
                parent_decision=services.values.publish('proteus.search.decision@1', decision))
            checkpoint = services.checkpoint(state)
        if state.child_id is None:
            parent = candidates[state.parent_id]
            delta = FileDelta(base_hash=parent.artifact.content_hash, put=(FileWrite.from_bytes(
                'increment.json', json.dumps(self.options.increment).encode()),))
            child = services.fork_candidate(parent, delta=delta, candidate_id='child', operation_id='selected-parent-child')
            state = replace(state, child_id=child.id)
            checkpoint = services.checkpoint(state)
        child = next(item for item in services.candidates if item.id == state.child_id)
        if state.validation is None:
            receipt = services.evaluate(child, 'validation', operation_id='child-validation')
            state = replace(state, validation=services.values.publish('proteus.eval.receipt@1', receipt))
            checkpoint = services.checkpoint(state)
        if state.test is None:
            receipt = services.evaluate(child, 'test', operation_id='child-test')
            state = replace(state, test=services.values.publish('proteus.eval.receipt@1', receipt))
            checkpoint = services.checkpoint(state)
        return MethodResult(outputs={'parent': state.parent_decision, 'validation': state.validation, 'test': state.test},
                            state=checkpoint)


def register(registry):
    digest = implementation_hash({'parent_selection.py': Path(__file__),
                                  'search_policy.py': Path(__file__).with_name('search_policy.py')})
    registry.register(Plugin(ref=PluginRef(name='example.parent_numeric_score', version='1'), kind='evaluator',
        options=EmptyOptions,
        factory=lambda options: NumericScore(), source='proteus', implementation_hash=digest,
        capabilities=('evaluator.deterministic',)))
    registry.register(Plugin(ref=PluginRef(name='example.parent_study', version='1'), kind='method',
        options=Options, factory=ParentStudy, source='proteus', implementation_hash=digest, validate=validate,
        state_schema=State, capabilities=('run.resume', 'run.journal')))


def prepare(root, *, parent_policy='proteus.scalar_best'):
    root = Path(root).resolve()
    require(not root.exists() or not any(root.iterdir()), 'study destination already contains data')
    root.mkdir(parents=True, exist_ok=True)
    registry = builtin_registry()
    register(registry)
    program = b'''import json,sys
from pathlib import Path
request=json.load(sys.stdin)
root=Path(__file__).parent
values=json.loads((root/'values.json').read_text())
increment=json.loads((root/'increment.json').read_text())
print(json.dumps({'output':min(1.0,values[request['task']['input']]+increment),'state':request['state']}))
'''
    initial = ArtifactStore(root/'artifact-store', registry).put(PluginRef(name='proteus.file_tree', version='1'),
        {'main.py': FileContent(program), 'values.json': FileContent(b'[0.9,0.1,0.2]'),
         'increment.json': FileContent(b'0')})
    dataset = TaskDataset(version='offline-parent-policy-v1', records=tuple(TaskRecord(
        task=Task(id=name, version='1', split=split, input=index), reference=1.0)
        for index, (name, split) in enumerate([('easy', 'validation'), ('hard', 'validation'), ('unseen', 'test')])))
    data = root/'tasks.json'
    data.write_text(dataset.to_json()+'\n')
    evaluations = tuple(EvaluationSpec(id=split, evaluator=spec('example.parent_numeric_score'),
        tasks=TaskSuiteSpec(suite=spec('proteus.json_tasks', path=str(data)), dataset_version=dataset.version,
            dataset_hash=content_hash(dataset), split=split, task_ids=ids), purpose=split,
        visibility=FeedbackVisibility.OBSERVED if split == 'validation' else FeedbackVisibility.HIDDEN,
        selection_eligible=split == 'validation', feedback_timing='after_batch' if split == 'validation' else 'never')
        for split, ids in [('validation', ('easy', 'hard')), ('test', ('unseen',))])
    recipe = RecipeConfig(name='offline-parent-policy-ablation', actors={}, reference=ReferenceSpec(profile='native'),
        method=spec('example.parent_study', variants=[Variant(id=name, values=tuple(values)).to_dict()
            for name, values in [('balanced', [.6,.6,.4]), ('spiky', [1,.3,.8])]], increment=.05),
        subject=SubjectSpec(runner=spec('proteus.python_subject'), initial_artifact=initial),
        execution=ExecutionSpec(environment=EnvironmentSpec(backend=spec('proteus.local_json'), provider='host',
            network='unrestricted'), timeout_seconds=10), evaluations=evaluations,
        search={'parent': spec(parent_policy)}, budget=BudgetLimits(max_evaluations=9))
    path = root/'recipe.json'
    path.write_text(recipe.to_json()+'\n')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    print(prepare(parser.parse_args().out))


if __name__ == '__main__':
    main()
