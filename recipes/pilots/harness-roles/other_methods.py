"""Live minimum cycles for ACE, Combee-ACE, Meta-Harness and CORAL.

Synthetic mechanism checks, not benchmark reproductions. Each method retains its
own candidate representation. A real Pi/DSH/Codex actor executes the relevant role.
"""

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from proteus.api.v1 import (ActorSpec, ArtifactStore, BudgetLimits,
    Task, TaskDataset, TaskRecord, builtin_registry, content_hash, require)
from proteus.examples.candidate_evaluation import plugin
from proteus.recipes.config import load_recipe
from proteus.recipes.export import report_run
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe

CODEX_PIN = 'a7b86b6201d35200280e3d24ca5ebccd8122d6ca'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def chat(model):
    return ActorSpec(model=model, runtime=plugin('proteus.chat',
        base_url='https://api.deepseek.com/v1', key_env='DEEPSEEK_API_KEY', timeout_seconds=180),
        model_settings={'temperature': 0, 'max_tokens': 4096, 'thinking': {'type': 'disabled'}})


def harness(model, backend, image, *, workspace=False):
    return ActorSpec(model=model, runtime=plugin(
        f'proteus.{"workspace" if workspace else "harness"}.{backend}', image=image,
        network='bridge' if workspace else 'host', phase_timeout_seconds=240,
        max_turns=16, model_selection='framework'))


def ace(root, *, method, model, backend, image, harness_model=None):
    from proteus.examples.ace_playbook import prepare as prepare_ace, register as register_fixture
    from proteus.examples.combee_ace import prepare as prepare_combee
    from proteus.methods.ace.method import register as register_ace
    from proteus.methods.ace.batch import register as register_combee
    from proteus.methods.ace.playbook import CODEC, Playbook
    path = (prepare_combee(root, workers=2, batch_size=2) if method == 'combee-ace'
            else prepare_ace(root))
    registry = builtin_registry()
    if method == 'combee-ace':
        register_combee(registry)
    else:
        register_ace(registry)
    register_fixture(registry)  # Only its independent exact-answer evaluator is used.
    recipe = load_recipe(path)
    count = 2 if method == 'combee-ace' else 1
    words = {'train': ('aLpHa', 'dElTa'), 'validation': ('bEtA',), 'test': ('oMeGa',)}
    data = TaskDataset(version='live-playbook-mechanism-1', records=tuple(
        TaskRecord(task=Task(id=f'{split}-{i}', version='1', split=split, input={
            'question': f'Apply the transformation policy to the word {word}. Use bash or a file tool to check your transformation. '
                'The current playbook is authoritative; if it has no learned policy, default to lowercase. '
                'Return the required JSON with final_answer equal to the transformed word only.',
            'context': 'A synthetic formatting task. Learn the general policy from released training feedback.'}),
            reference=word.upper()) for split, items in words.items()
        for i, word in enumerate(items[:count] if split == 'train' else items)))
    (root / 'tasks.json').write_text(data.to_json() + '\n')
    evaluations = tuple(replace(e, tasks=replace(e.tasks, dataset_version=data.version,
        dataset_hash=content_hash(data), task_ids=tuple(r.task.id for r in data.records if r.task.split == e.tasks.split)))
        for e in recipe.evaluations)
    store = ArtifactStore(root / 'artifact-store', registry)
    initial = store.put(CODEC, Playbook.from_text())
    options = dict(recipe.method.options, max_num_rounds=1, eval_steps=1, json_mode=False)
    workflows = recipe.workflows
    if method == 'combee-ace':
        options['max_workers'] = 2
        options['grouping'] = {'profile': 'ace', 'duplication_factor': 2, 'num_groups': 2}
        workflows = tuple(replace(w, operator=replace(w.operator,
            options=dict(w.operator.options, max_num_rounds=1, json_mode=False))) for w in workflows)
    recipe = replace(recipe, name=f'live-{backend}-{method}-one-cycle',
        method=replace(recipe.method, options=options),
        actors={'generator': replace(harness(harness_model or model, backend, image), budget=BudgetLimits(max_concurrency=2)),
                'reflector': replace(chat(model), budget=BudgetLimits(max_concurrency=2)),
                'curator': replace(chat(model), budget=BudgetLimits(max_concurrency=2))},
        subject=replace(recipe.subject, initial_artifact=initial), evaluations=evaluations, workflows=workflows,
        execution=replace(recipe.execution, max_workers=2 if method == 'combee-ace' else 1),
        budget=BudgetLimits(max_evaluations=20, max_concurrency=3),
        reference=replace(recipe.reference, deviations=(
            'Synthetic formatting tasks; one offline sample or one two-sample Combee batch, not a benchmark.',
            'Real coding harness is the generator; reflector and curator are managed DeepSeek chat roles.',
            'The playbook is rendered by the ACE subject into the harness prompt; harness source is fixed.',
            'Combee uses one two-sample batch with two independent harness generator workspaces; no speedup claim.',
            'Managed failures stop without fabricated scores; no paper reproduction or efficiency claim.')))
    return registry, store, recipe


def meta(root, *, model, backend, image, harness_model=None):
    from proteus.examples.meta_harness_fixture import prepare
    from proteus.methods.meta_harness.method import register
    registry = builtin_registry()
    register(registry)
    path = prepare(root, task_count=1, batch_size=1)
    recipe = load_recipe(path)
    data_path = next(root.glob('Symptom2Disease-*.json'))
    data = TaskDataset(version='live-memory-mechanism-1', records=tuple(
        TaskRecord(task=Task(id=f'{split}-0', version='1', split=split,
            input='Synthetic label task: respond with final_answer equal to "pilot-label".'),
            reference={'target': 'pilot-label', 'extras': {'raw_question': f'synthetic {split}'}})
        for split in ('train', 'val', 'test')))
    data_path.write_text(data.to_json() + '\n')
    recipe = replace(recipe, name=f'live-{backend}-meta-one-iteration',
        actors={'model': chat(model), 'proposer': replace(harness(harness_model or model, backend, image, workspace=True),
            runtime=plugin(f'proteus.workspace.{backend}', image=image, network='bridge',
                phase_timeout_seconds=480, max_turns=64, model_selection='framework'))},
        execution=replace(recipe.execution, timeout_seconds=180),
        evaluations=tuple(replace(e, tasks=replace(e.tasks, dataset_hash=content_hash(data),
            dataset_version=data.version)) for e in recipe.evaluations),
        budget=BudgetLimits(max_evaluations=40),
        reference=replace(recipe.reference, deviations=(*recipe.reference.deviations,
            'One synthetic example per split and one real coding proposer iteration; not benchmark quality.',
            'Candidate Python runs in managed local processes on an ephemeral CI runner; no isolation claim.',
            'Pi/DSH/Codex is the coding proposer; generated memory programs call managed DeepSeek chat.')))
    return registry, ArtifactStore(root / 'artifact-store', registry), recipe


def coral(root, *, model, backend, image, harness_model=None):
    from proteus.examples.coral_search import prepare, register
    from proteus.workers.records import CommandWorkerOptions
    registry = builtin_registry()
    register(registry)
    recipe = load_recipe(prepare(root, workers=1))
    bridge = Path(__file__).with_name('coral_worker.py').resolve()
    actor_path = root / 'worker-actor.json'
    actor_path.write_text(harness(harness_model or model, backend, image, workspace=True).to_json() + '\n')
    command = (sys.executable, str(bridge), '--actor', str(actor_path))
    pins = (command[0], command[1], str(actor_path))
    runtime = plugin('proteus.worker.command', **CommandWorkerOptions(command=command,
        command_files={p: 'sha256:' + hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in pins},
        credential_env=(('CODEX_API_KEY',) if backend == 'codex' else ('DEEPSEEK_API_KEY',)),
        timeout_seconds=650, shutdown_grace_seconds=10).to_dict())
    recipe = replace(recipe, name=f'live-{backend}-coral-one-worker',
        workers=(replace(recipe.workers[0], runtime=runtime),),
        actors={'proposer': chat(model)},  # Worker service owns accounting; no hidden actor dispatch.
        method=replace(recipe.method, options=dict(recipe.method.options, max_submissions=2,
            max_polls=3600, poll_seconds=.2,
            bootstrap_prompt='Improve candidate/main.py using released formal feedback. Preserve its JSON stdin/stdout contract.')),
        event_policies=(replace(recipe.event_policies[0], operator=plugin('coral.heartbeat', rules=[{
            'name': 'outside-pilot', 'trigger': 'evaluations', 'every': 100,
            'prompt': 'Review released feedback.'}])),),
        reference=replace(recipe.reference, deviations=(
            'Synthetic integer transformation, one real coding worker and two bounded submissions.',
            'Command worker invokes fresh Pi/DSH/Codex coding workspaces; explicit files/feedback carry state.',
            'The pilot bridge submits exact actor-produced files, not invented candidate code or scores.',
            'No persistent provider session, worker intervention, multi-worker cooperation or paper performance claim.')))
    return registry, ArtifactStore(root / 'artifact-store', registry), recipe


def prepare(root, *, method, **kwargs):
    if method in ('ace', 'combee-ace'):
        result = ace(root, method=method, **kwargs)
    else:
        result = {'meta': meta, 'coral': coral}[method](root, **kwargs)
    registry, store, recipe = result
    recipe = registry.resolve(recipe)
    (root / 'recipe.json').write_text(recipe.to_json() + '\n')
    return registry, store, recipe


def verify(root, method):
    records = json.loads(sorted((root / 'run').glob('execution-*.json'))[-1].read_text())
    candidates = records['candidates']
    initial = next(c for c in candidates if c['id'] == 'initial')
    changed = {c['id'] for c in candidates if c['artifact']['content_hash'] != initial['artifact']['content_hash']}
    require(changed and any(r['candidate_id'] in changed for r in records['rollouts']),
            'no changed candidate was executed')
    require(all(r['status'] == 'succeeded' for r in records['rollouts']), 'a target rollout failed')
    values = records['values']
    kinds = [v['ref']['kind'] for v in values]
    if method in ('ace', 'combee-ace'):
        require(all(r['runtime']['settings'].get('native_usage') for r in records['rollouts']),
                'real generator harness usage receipts are missing')
        store = ArtifactStore(root / 'run/artifacts', registry=None)
        from proteus.core.contracts import ArtifactRef
        texts = [store.files(ArtifactRef.from_dict(c['artifact']))['playbook.txt'].data for c in candidates]
        require(any(text != texts[0] and b'helpful=' in text for text in texts[1:]),
                'curation did not add a learned playbook bullet')
        require(('proteus.ace.curation@1' if method == 'ace' else 'proteus.combee.ace_result@1') in kinds,
                'learning evidence missing')
        for rollout in records['rollouts']:
            payload = json.loads(rollout['output']['response'])
            require(payload.get('final_answer') == rollout['output']['answer'],
                    'generator response was truncated, malformed, or parsed incorrectly')
    if method == 'combee-ace':
        require('proteus.combee.ace_summary@1' in kinds, 'not a Combee method run')
        batches = [v['value'] for v in values if v['ref']['kind'] == 'proteus.combee.ace_batch@1']
        require(batches and len(batches) == 1 and len(batches[0]['task_ids']) == 2,
                'expected one actual two-sample Combee batch')
    if method == 'meta':
        calls = [c for c in records['actor_calls'] if c['role'] == 'proposer']
        require(len(calls) == 1 and calls[0]['result']['runtime']['settings'].get('native_usage'),
                'expected one real coding proposer')
        generated = changed - {'fewshot_all'}
        require(any(r['candidate_id'] in generated for r in records['rollouts']),
                'generated memory program was never evaluated')
    if method == 'coral':
        require(len(list((root / 'run').rglob('pilot-feedback-*.json'))) == 2,
                'worker did not receive two real formal feedback records')
        require(len(list((root / 'run').rglob('pilot-actor-result-*.json'))) == 2,
                'two real coding worker turns are missing')
    batches = [json.loads(p.read_text()) for p in (root / 'run').glob('evaluations/*/private/batches/*.json')]
    require(any(b['visibility'] == 'hidden' for b in batches), 'private final evaluation missing')
    return {'candidate_count': len(candidates), 'rollout_count': len(records['rollouts']),
        'changed_candidate_ids': sorted(changed), 'actor_roles': sorted({c['role'] for c in records['actor_calls']}),
        'scores': [{'candidate': b['candidate_id'], 'visibility': b['visibility'],
                    'rows': [{'task': r['task_id'], 'score': r['score']} for r in b['rows']]} for b in batches]}


def verify_fixed_runtime(root, backend, expected_hash):
    witnesses = []
    if backend == 'codex':
        for path in (root / 'run').rglob('ep001-runtime.json'):
            value = json.loads(path.read_text())
            require(value['active_source_sha256'] == expected_hash, 'fixed Codex source identity changed')
            witnesses.append({'path': path.relative_to(root).as_posix(), **value})
    else:
        for path in (root / 'run').rglob('*.outcome.json'):
            if path.parent.name != 'native-inputs':
                continue
            value = json.loads(path.read_text()).get('activation')
            require(value and value['backend'] == backend and value['source_sha256'] == expected_hash
                    and value['mode'] == 'pristine', 'fixed runtime missed its pinned pristine activation')
            witnesses.append({'path': path.relative_to(root).as_posix(), **value})
    require(witnesses, 'no fixed-runtime activation witnesses')
    return witnesses


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--method', choices=('ace', 'combee-ace', 'meta', 'coral'), required=True)
    parser.add_argument('--backend', choices=('pi', 'dsh', 'codex'), default='pi')
    parser.add_argument('--image', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--model', default='deepseek-v4-flash')
    parser.add_argument('--harness-model', help='Coding harness model; required separately for Codex')
    args = parser.parse_args()
    root = args.out.resolve()
    require(not root.exists(), 'fresh run required; never replay uncertain dispatches blindly')
    require(bool(os.environ.get('DEEPSEEK_API_KEY')), 'missing provider key')
    if args.backend == 'codex':
        require(bool(args.harness_model), 'Codex needs an explicit separate harness model')
        require(bool(os.environ.get('CODEX_API_KEY') or os.environ.get('OPENAI_API_KEY')), 'missing Codex API key')
        os.environ['CODEX_API_KEY'] = os.environ.get('CODEX_API_KEY') or os.environ.get('OPENAI_API_KEY', '')
    image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', args.image], text=True))[0]
    from run import PINS
    pins = dict(PINS, codex=CODEX_PIN)
    require(image['Config']['Labels']['org.opencontainers.image.revision'] == pins[args.backend], 'upstream pin differs')
    source_marker = '/opt/codex-source.sha256' if args.backend == 'codex' else '/opt/pristine-hash'
    pristine = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--entrypoint',
        'cat', image['Id'], source_marker], text=True).strip()
    require(len(pristine) == 64 and all(c in '0123456789abcdef' for c in pristine), 'invalid image source marker')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    evidence = dict(method=args.method, backend=args.backend, image_id=image['Id'], source_revision=pins[args.backend],
        proteus_head=head, requested_model=args.model, harness_model=args.harness_model or args.model,
        pristine_source_sha256=pristine,
        started_utc=datetime.now(timezone.utc).isoformat(), status='started')
    try:
        registry, store, recipe = prepare(root, method=args.method, model=args.model, backend=args.backend,
            image=image['Id'], harness_model=args.harness_model)
        lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://github.com/proteus-evolve/Proteus', revision=head)])
        (root / 'lock.json').write_text(lock.to_json() + '\n')
        save(root / 'evidence.json', evidence)
        result = run_recipe(recipe, registry=registry, lock=lock, root=root / 'run', artifact_source=store.root)
        save(root / 'result.json', result.to_dict())
        require(result.status.value == 'completed', result.diagnostic)
        evidence.update(verify(root, args.method))
        evidence['fixed_runtime_witnesses'] = verify_fixed_runtime(root, args.backend, pristine)
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'run').rglob('*.input.json')}
        before_workers = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'run').rglob('dispatch.json')}
        resumed = run_recipe(recipe, registry=registry, lock=lock, root=root / 'run', artifact_source=store.root, resume=True)
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'run').rglob('*.input.json')}
        after_workers = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'run').rglob('dispatch.json')}
        require(resumed == result and before == after and before_workers == after_workers and (before or before_workers),
                'completed resume changed dispatch evidence')
        save(root / 'report.json', report_run(root / 'run'))
        evidence.update(status='passed', completed_resume_new_dispatches=0)
    except Exception as exc:
        evidence.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        root.mkdir(parents=True, exist_ok=True)
        evidence['finished_utc'] = datetime.now(timezone.utc).isoformat()
        save(root / 'evidence.json', evidence)
        print(json.dumps(evidence))


if __name__ == '__main__':
    main()
