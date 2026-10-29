"""One live Native or GEPA cycle through a real, pinned Pi/DSH harness.

This is an engineering pilot, not a benchmark or a paper reproduction. Native's
goal deliberately specifies the source/format change. GEPA learns a trivial
format rule from observed feedback. No model response or tool action is mocked.
"""

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from proteus.api.v1 import (
    ActorSpec, ArtifactStore, BudgetLimits, EnvironmentSpec, EvaluationSpec, ExecutionSpec,
    FeedbackVisibility, FileContent, NativeEvolutionOptions, PluginRef, PluginSpec,
    RecipeConfig, ReferenceSpec, StageSpec, SubjectSpec, Task, TaskDataset, TaskRecord,
    TaskSuiteSpec, WorkspaceOptions, builtin_registry, content_hash, require,
)
from proteus.artifacts.codecs import TextComponents
from proteus.core.native_inputs import read_native_inputs
from proteus.evidence.records import EvidenceAccess
from proteus.execution.harness_candidate import HarnessEnvironmentOptions
from proteus.methods.gepa.method import GepaOptions, register as register_gepa
from proteus.methods.gepa.reference import REPOSITORY, REVISION
from proteus.recipes.export import report_run
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe

ENTRY = {'pi': 'packages/coding-agent/src/cli.ts', 'dsh': 'apps/cli/src/bin.ts'}
PINS = {'pi': '914cf1472e715297caa30db4b9535d534a9eb718',
        'dsh': '99f6f02fecdb7dff40c3fbc9470f5907c29f74ca'}
MARKER = 'PROTEUS_ROLE_PILOT_ACTIVATED'
LIMITS = {'max_bytes': 128 * 1024 * 1024, 'max_files': 10000}
POLICY = 'Transform the supplied text to lowercase. Return only the transformed text.\n'


def plugin(name, **options):
    return PluginSpec(plugin=PluginRef(name=name, version='1'), options=options)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare(root, *, backend, image, method, model, seed_files=None):
    """Prepare and preflight; only source extraction/boot, no inference here."""
    root.mkdir(parents=True, exist_ok=False)
    registry = builtin_registry()
    if method == 'gepa':
        register_gepa(registry)
    store = ArtifactStore(root / 'input-artifacts', registry)
    target = ActorSpec(model=model, runtime=plugin(f'proteus.harness.{backend}',
        image=image, network='host', phase_timeout_seconds=240, max_turns=10,
        model_selection='framework'))
    if seed_files is None:
        adapter = registry.create(target.runtime, 'actor').adapter(target)
        adapter.seed(root / 'seed')
        files = ArtifactStore.read_tree(root / 'seed', **LIMITS)
    else:
        files = dict(seed_files)  # Preparation-only test input; never used by live CLI.
    task_instructions = ('\n\n# Task execution mode\n'
        'When the request contains Task input, perform only that task. Read the rule '
        'in /workspace/notes/policy.md and apply it to the supplied text. Write only '
        'the result to the requested prediction path. Do not improve or change the '
        'policy during task execution. Finish immediately after writing the result.\n')
    files['AGENTS.md'] = FileContent(files['AGENTS.md'].data + task_instructions.encode())
    files['notes/policy.md'] = FileContent(POLICY.encode())
    base = store.put(PluginRef(name='proteus.file_tree', version='1'), files)
    save(root / 'seed-summary.json', {'artifact': base.to_dict(), 'source_revision': PINS[backend]})
    splits = ('train', 'validation', 'test') if method == 'gepa' else ('train', 'test')
    words = {'train': 'aLpHa', 'validation': 'bEtA', 'test': 'oMeGa'}
    dataset = TaskDataset(version='live-harness-mechanism-1', records=tuple(
        TaskRecord(task=Task(id=split, version='1', split=split,
            input={'operation': 'Apply the loaded notes/policy.md transformation to this text.',
                   'text': words[split]}), reference=words[split].upper()) for split in splits))
    tasks = root / 'tasks.json'
    tasks.write_text(dataset.to_json() + '\n')
    evaluations = tuple(EvaluationSpec(id=split, evaluator=plugin('proteus.exact_match'),
        tasks=TaskSuiteSpec(suite=plugin('proteus.json_tasks', path=str(tasks)),
            dataset_version=dataset.version, dataset_hash=content_hash(dataset), split=split,
            task_ids=(split,)), purpose='optimization' if split == 'train' else split,
        selection_eligible=method == 'gepa' and split != 'test',
        visibility=FeedbackVisibility.HIDDEN if split == 'test' else FeedbackVisibility.OBSERVED,
        feedback_timing='never' if split == 'test' else 'after_batch') for split in splits)
    env_options = HarnessEnvironmentOptions(**LIMITS)
    actors = {'target': target}
    search = {}
    if method == 'native':
        actors['evolver'] = ActorSpec(model=model, runtime=plugin(f'proteus.evolution.{backend}',
            image=image, network='host', phase_timeout_seconds=300, model_selection='framework'))
        entry = '/workspace/candidate/src/' + ENTRY[backend]
        goal = ('This is a minimal engineering acceptance cycle, not an open-ended task. '
            'During act only, make exactly two changes: (1) change '
            '/workspace/candidate/notes/policy.md to transform any supplied text to uppercase '
            'and return only that text; (2) insert console.error("' + MARKER + '"); '
            'on a new line after the shebang (or at the beginning if none) of ' + entry + '. '
            'Preserve all other source behavior. Use the real read/edit/write/bash tools. '
            'Do not compile or execute writable candidate source. Proteus performs the boundary '
            'build. Do not add prediction files to the evolving candidate. In observe/propose '
            'inspect and plan briefly; in reflect verify the two edits and finish. '
            'Do not inspect credentials or model configuration. Keep every phase short.')
        options = NativeEvolutionOptions(episode_stage='evolve', evaluations=('train',),
            test_evaluations=('test',), episodes=1, goal=goal, max_turns=32,
            phase_turns={'observe': 6, 'propose': 6, 'act': 14, 'reflect': 6}, announce_budget=True)
        stages = (StageSpec(id='evolve', role='evolver', failure_policy='record',
            operator=plugin('proteus.harness_episode', **WorkspaceOptions(
                max_output_bytes=LIMITS['max_bytes'], max_output_files=LIMITS['max_files']).to_dict()),
            input_types={'seed': 'proteus.artifact_ref@1', 'active': 'proteus.artifact_ref@1',
                'episode': 'proteus.harness_episode@1', 'history': 'proteus.evidence.view@1'},
            output_types={'response': 'proteus.actor_result@1'},
            read_scopes=('proteus.artifact_ref@1', 'proteus.harness_episode@1', 'proteus.evidence.view@1'),
            write_scopes=('proteus.actor_result@1',),
            evidence=EvidenceAccess(evaluation_ids=('train',), splits=('train',))),)
        method_plugin = plugin('proteus.native_evolution', **options.to_dict())
        initial = base
        reference = ReferenceSpec(profile='native', deviations=(
            'One live engineering episode with an explicitly requested source/format change; not quality evidence.',))
        budget = BudgetLimits(max_evaluations=3)
    else:
        initial = store.put(PluginRef(name='proteus.text_components', version='1'),
            TextComponents(components={'notes/policy.md': POLICY}))
        env_options = replace(env_options, base=base, mutable_paths=('notes/policy.md',))
        actors['reflection'] = ActorSpec(model=model, runtime=plugin('proteus.chat',
            base_url='https://api.deepseek.com/v1', key_env='DEEPSEEK_API_KEY', timeout_seconds=180),
            model_settings={'temperature': 0, 'max_tokens': 1024, 'thinking': {'type': 'disabled'}})
        stages = (StageSpec(id='reflect', operator=plugin('proteus.prompt'), role='reflection',
            input_types={'prompt': 'proteus.text@1'}, output_types={'response': 'proteus.actor_result@1'},
            read_scopes=('proteus.text@1',), write_scopes=('proteus.actor_result@1',)),)
        options = GepaOptions(train='train', validation='validation', reflection_stage='reflect',
            max_metric_calls=4, minibatch_size=1, test_evaluations=('test',), skip_perfect_score=False)
        method_plugin = plugin('proteus.gepa', **options.to_dict())
        search = {'parent': plugin('proteus.gepa_parent'), 'frontier': plugin('proteus.gepa_frontier'),
                  'final': plugin('proteus.gepa_best')}
        reference = ReferenceSpec(profile='adapted', code=REPOSITORY, revision=REVISION,
            deviations=('One GEPA mutation of a real coding harness policy on synthetic cases; not a quality study.',))
        budget = BudgetLimits(max_evaluations=7)
    recipe = RecipeConfig(name=f'live-{backend}-{method}-one-cycle', method=method_plugin,
        reference=reference, actors=actors, stages=stages, search=search,
        subject=SubjectSpec(runner=plugin('proteus.harness_subject'), initial_artifact=initial, actor='target'),
        execution=ExecutionSpec(environment=EnvironmentSpec(provider='host', network='unrestricted',
            backend=plugin('proteus.harness_environment', **env_options.to_dict()))),
        evaluations=evaluations, budget=budget)
    recipe = registry.resolve(recipe)
    (root / 'recipe.json').write_text(recipe.to_json() + '\n')
    return registry, store, recipe


def verify(root, *, method):
    records = json.loads(sorted((root / 'run').glob('execution-*.json'))[-1].read_text())
    require(len(records['candidates']) >= 2, 'no candidate was produced')
    require(len({r['artifact_hash'] for r in records['rollouts']}) >= 2,
            'changed candidate was not executed')
    require(all(r['status'] == 'succeeded' and r['output'] is not None for r in records['rollouts']),
            'a target rollout failed or produced no output')
    initial = [r for r in records['rollouts'] if r['candidate_id'] == 'initial']
    changed = [r for r in records['rollouts'] if r['candidate_id'] != 'initial']
    require(initial and changed and all(r['output'].islower() for r in initial)
            and all(r['output'].isupper() for r in changed), 'observed behavior did not change as expected')
    require(all(r['runtime']['settings'].get('native_usage') for r in records['rollouts']),
            'target execution lacks native usage receipts')
    activations = []
    for path in sorted((root / 'run').glob('evaluations/*/trials/*')):
        for row in read_native_inputs(path):
            if row.get('outcome') and row['outcome'].get('activation'):
                activations.append(row['outcome']['activation'])
    require(activations, 'no source activation receipts')
    if method == 'native':
        calls = records['actor_calls']
        require(len(calls) == 1 and calls[0]['role'] == 'evolver', 'expected exactly one evolution episode')
        require(calls[0]['result']['runtime']['settings']['candidate_viability']['valid'],
                'evolved source failed its build/viability gate')
        require(len({a['cli_sha256'] for a in activations}) >= 2,
                'the changed compiled harness was not activated in task execution')
    else:
        summaries = [v['value'] for v in records['values'] if v['ref']['kind'] == 'proteus.gepa.summary@1']
        require(len(summaries) == 1 and summaries[0]['reflection_calls'] == 1,
                'expected one real GEPA reflection cycle')
    report = report_run(root / 'run')
    save(root / 'report.json', report)
    return {'status': 'passed', 'method': method, 'candidate_count': len(records['candidates']),
            'rollout_count': len(records['rollouts']),
            'predictions': [{'candidate': r['candidate_id'], 'task': r['task_id'], 'output': r['output']}
                            for r in records['rollouts']],
            'activations': activations, 'accounting': report.get('accounting'),
            'scope': 'Live-model mechanism acceptance on synthetic tasks, not benchmark improvement.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=tuple(PINS), required=True)
    parser.add_argument('--method', choices=('native', 'gepa'), required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--model', default='deepseek-v4-flash')
    args = parser.parse_args()
    require(bool(os.environ.get('DEEPSEEK_API_KEY')), 'DEEPSEEK_API_KEY is required')
    root = args.out.resolve()
    require(not root.exists(), 'fresh run only; uncertain/failed studies must not be blindly restarted')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', args.image], text=True))[0]
    require(image['Config']['Labels']['org.opencontainers.image.revision'] == PINS[args.backend],
            'image source revision differs from the declared harness pin')
    registry, store, recipe = prepare(root, backend=args.backend, image=image['Id'],
        method=args.method, model=args.model)
    lock = registry.make_lock(recipe, [SourcePin(name='proteus',
        repository='https://github.com/proteus-evolve/Proteus', revision=head)])
    (root / 'lock.json').write_text(lock.to_json() + '\n')
    evidence = {'started_utc': datetime.now(timezone.utc).isoformat(), 'proteus_head': head,
        'backend': args.backend, 'method': args.method, 'requested_model': args.model,
        'image_id': image['Id'], 'source_revision': PINS[args.backend], 'status': 'started'}
    save(root / 'evidence.json', evidence)
    try:
        result = run_recipe(recipe, registry=registry, lock=lock, root=root / 'run', artifact_source=store.root)
        save(root / 'result.json', result.to_dict())
        require(result.status.value == 'completed', result.diagnostic)
        evidence.update(verify(root, method=args.method))
        before = {str(p.relative_to(root / 'run')): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (root / 'run').rglob('*') if p.is_file() and p.name.endswith('.input.json')}
        resumed = run_recipe(recipe, registry=registry, lock=lock, root=root / 'run',
            artifact_source=store.root, resume=True)
        after = {str(p.relative_to(root / 'run')): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (root / 'run').rglob('*') if p.is_file() and p.name.endswith('.input.json')}
        require(resumed == result and before == after, 'completed resume changed dispatch receipts')
        evidence['completed_resume_new_dispatches'] = 0
    except Exception as exc:
        evidence.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        evidence['finished_utc'] = datetime.now(timezone.utc).isoformat()
        save(root / 'evidence.json', evidence)
        print(json.dumps(evidence, ensure_ascii=False))


if __name__ == '__main__':
    main()
