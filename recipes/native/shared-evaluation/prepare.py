"""Prepare a zero-model-call Native/external-evaluation engineering recipe."""

import argparse
from pathlib import Path

from proteus.api.v1 import (
    ActorSpec, ArtifactStore, BudgetLimits, EnvironmentSpec, EvaluationSpec, ExecutionSpec,
    FeedbackVisibility, FileContent, NativeEvolutionOptions, PluginRef, PluginSpec,
    RecipeConfig, ReferenceSpec, StageSpec, SubjectSpec, Task, TaskDataset, TaskRecord,
    TaskSuiteSpec, WorkspaceOptions, builtin_registry, content_hash, require,
)
from proteus.evidence.records import EvidenceAccess
from proteus.examples.harness_roles import PROGRAM, register


def plugin(name, **options):
    return PluginSpec(plugin=PluginRef(name=name, version='1'), options=options)


def prepare(root):
    root = Path(root).resolve()
    require(not root.exists() or not any(root.iterdir()), 'use a new output directory')
    root.mkdir(parents=True, exist_ok=True)
    registry = builtin_registry()
    roles = register(registry)
    store = ArtifactStore(root / 'artifact-store', registry)
    initial = store.put(PluginRef(name='proteus.file_tree', version='1'),
                        {'controller.py': FileContent(PROGRAM.encode())})
    dataset = TaskDataset(version='offline-role-mechanism-1', records=tuple(
        TaskRecord(task=Task(id=split+'-0', version='1', split=split, input=2), reference='4')
        for split in ('train', 'test')))
    tasks = root / 'tasks.json'
    tasks.write_text(dataset.to_json()+'\n')
    evaluations = tuple(EvaluationSpec(id=split, evaluator=plugin('proteus.exact_match'),
        tasks=TaskSuiteSpec(suite=plugin('proteus.json_tasks', path=str(tasks)),
            dataset_version=dataset.version, dataset_hash=content_hash(dataset), split=split,
            task_ids=(split+'-0',)), purpose='optimization' if split=='train' else 'test',
        selection_eligible=False, visibility=FeedbackVisibility.OBSERVED if split=='train' else FeedbackVisibility.HIDDEN,
        feedback_timing='after_batch' if split=='train' else 'never') for split in ('train', 'test'))
    options = NativeEvolutionOptions(episode_stage='evolve', evaluations=('train',),
        test_evaluations=('test',), episodes=3, max_turns=20,
        goal='Improve the loaded program using only the observed task evidence.')
    stage = StageSpec(id='evolve', operator=plugin('proteus.harness_episode', **WorkspaceOptions().to_dict()),
        role='evolver', failure_policy='record',
        input_types={'seed':'proteus.artifact_ref@1','active':'proteus.artifact_ref@1',
            'episode':'proteus.harness_episode@1','history':'proteus.evidence.view@1'},
        output_types={'response':'proteus.actor_result@1'},
        read_scopes=('proteus.artifact_ref@1','proteus.harness_episode@1','proteus.evidence.view@1'),
        write_scopes=('proteus.actor_result@1',),
        evidence=EvidenceAccess(evaluation_ids=('train',),splits=('train',)))
    recipe = RecipeConfig(name='offline-shared-harness-roles',
        method=plugin('proteus.native_evolution', **options.to_dict()),
        reference=ReferenceSpec(profile='native', deviations=(
            'Deterministic subprocess programs, two synthetic cases and zero model calls; not a quality study.',
            'Explicit neutral/last-valid Native profile; legacy schedules and score selection are not translated.')),
        actors={'target':ActorSpec(runtime=PluginSpec(plugin=roles['actor'])),
                'evolver':ActorSpec(runtime=PluginSpec(plugin=roles['evolution'], options={'break_first':True}))},
        subject=SubjectSpec(runner=plugin('proteus.harness_subject'), initial_artifact=initial,actor='target'),
        execution=ExecutionSpec(environment=EnvironmentSpec(provider='host',network='unrestricted',
            backend=plugin('proteus.harness_environment'))),
        stages=(stage,), evaluations=evaluations, budget=BudgetLimits(max_model_calls=0,max_evaluations=4))
    path = root / 'recipe.json'
    path.write_text(recipe.to_json()+'\n')
    return path


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    print(prepare(parser.parse_args().out))
