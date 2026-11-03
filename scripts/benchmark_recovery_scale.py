#!/usr/bin/env python3
"""Exercise managed recovery storage and ledger accounting without a model/provider.

Use an output directory outside the repository. Work fixtures are deleted after
measurement; the JSON summary records sizes, recovery time and acceptance checks.
"""

import argparse
from dataclasses import dataclass, replace
import json
from pathlib import Path
import platform
import resource
import subprocess
import sys
import tempfile
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proteus.core.contracts import BudgetLimits, PluginRef
from proteus.core.ledger import Amounts, Ledger
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.serialization import Contract, content_hash
from proteus.recipes import load_recipe
from proteus.recipes.builtins import Text, builtin_registry
from proteus.recipes.config import PluginSpec
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, kw_only=True)
class ScaleState(Contract):
    position: int = 0


def size(root):
    files = [path for path in root.rglob('*') if path.is_file()]
    return {'files': len(files), 'bytes': sum(path.stat().st_size for path in files)}


def stage_study(root, count, prompt_bytes):
    class Method:
        def run(self, services, restored):
            state = ScaleState.from_dict(restored.state.payload) if restored else ScaleState()
            checkpoint = restored
            for i in range(state.position, count):
                for role in ('generator', 'reflector', 'curator'):
                    prompt = services.values.publish('proteus.text@1', Text(text=f'{i}:{role}:' + 'x' * prompt_bytes))
                    services.stage('propose', {'prompt': prompt}, operation_id=f'case-{i}-{role}')
                checkpoint = services.checkpoint(ScaleState(position=i + 1))
            return MethodResult(state=checkpoint)
    registry = builtin_registry()
    ref = PluginRef(name='validation.scale', version='1')
    registry.register(Plugin(ref=ref, kind='method', options=EmptyOptions, factory=lambda options: Method(),
        source='proteus', implementation_hash=content_hash('offline-scale-v1'),
        capabilities=('run.resume', 'run.journal'), state_schema=ScaleState))
    recipe = load_recipe(ROOT / 'recipes/examples/stages.toml')
    recipe = replace(recipe, method=PluginSpec(plugin=ref))
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    lock = registry.make_lock(recipe, [SourcePin(name='proteus',
        repository='https://github.com/proteus-evolve/Proteus.git', revision=revision)])
    before = time.perf_counter()
    result = run_recipe(recipe, registry=registry, lock=lock, root=root)
    elapsed = time.perf_counter() - before
    assert result.status.value == 'completed', result.diagnostic
    book = Ledger(root / 'private/ledger.sqlite3', run_id=root.name,
                  config_hash=registry.resolve(recipe).config_hash, lock_hash=content_hash(lock))
    calls = book.balance()['entries']
    before = time.perf_counter()
    replayed = run_recipe(recipe, registry=registry, lock=lock, root=root, resume=True)
    recovery = time.perf_counter() - before
    assert replayed == result and book.balance()['entries'] == calls == 3 * count
    assert book.balance()['active_slots'] == 0
    return {'examples': count, 'managed_stages': calls, 'prompt_bytes_per_stage': prompt_bytes,
            'run_seconds': elapsed, 'completed_resume_seconds': recovery,
            'journal': size(root / 'private/recovery'), 'run': size(root),
            'same_completed_result': True, 'resume_added_dispatches': 0, 'provider_calls': 0}


def ledger_study(root, count, prompt_bytes):
    book = Ledger(root / 'ledger.sqlite3', run_id='scale', config_hash='fixture', lock_hash=None)
    limits = {'run': BudgetLimits(max_model_calls=count, max_concurrency=8),
              'role:agent': BudgetLimits(max_model_calls=count, max_concurrency=8)}
    before = time.perf_counter()
    for i in range(count):
        key = f'op-{i}'
        book.reserve(key, scopes=limits, reservation=Amounts(model_calls=1, cost_usd=0.1),
                     metadata={'operation_id': key}, request={'prompt': 'x' * prompt_bytes})
        book.start(key)
        book.settle(key, actual=Amounts(model_calls=None if i % 5 == 0 else 1,
                    cost_usd=None if i % 5 == 0 else 0.05), status='completed')
        assert book.balance()['entries'] == i + 1
    elapsed = time.perf_counter() - before
    before = time.perf_counter()
    with book._connection() as db:
        assert book._balance(db, 'run') == book._scan_balance(db, 'run')
    audit = time.perf_counter() - before
    assert book.balance()['active_slots'] == 0
    return {'synthetic_entries': count, 'request_bytes': prompt_bytes,
            'reserve_start_settle_and_check_seconds': elapsed, 'independent_scan_seconds': audit,
            'balance_matches_independent_scan': True, 'database': size(root),
            'provider_calls': 0, 'usage_values_are_synthetic': True}


def cohort_study(root, sizes, prompt_bytes):
    from proteus.artifacts.codecs import FileContent
    from proteus.artifacts.store import ArtifactStore
    from proteus.core.contracts import FeedbackVisibility
    from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
    from proteus.examples.candidate_evaluation import prepare, plugin
    from proteus.methods.compare import CompareOptions
    from proteus.recipes.config import TaskSuiteSpec
    recipe = load_recipe(prepare(root / 'study'))
    splits = ('train', 'validation', 'test')
    dataset = TaskDataset(version='scale-500-300-200-v1', records=tuple(
        TaskRecord(task=Task(id=f'{split}-{i}', version='1', split=split,
            input={'value': i, 'payload': 'x' * prompt_bytes}), reference=i)
        for split, count in zip(splits, sizes) for i in range(count)))
    data_path = root / 'study/tasks.json'
    data_path.write_text(dataset.to_json())
    registry = builtin_registry()
    store = ArtifactStore(root / 'study/artifact-store', registry)
    code = "import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({'output':r['task']['input']['value']}))\n"
    artifact = store.put(PluginRef(name='proteus.file_tree', version='1'),
                         {'main.py': FileContent(code.encode())})
    template = recipe.evaluations[0]
    evaluations = tuple(replace(template, id=split, purpose=purpose,
        selection_eligible=split != 'test',
        tasks=TaskSuiteSpec(suite=plugin('proteus.json_tasks', path=str(data_path)),
            dataset_version=dataset.version, dataset_hash=content_hash(dataset), split=split,
            task_ids=tuple(f'{split}-{i}' for i in range(count))),
        visibility=FeedbackVisibility.HIDDEN if split == 'test' else FeedbackVisibility.OBSERVED,
        feedback_timing='never' if split == 'test' else 'after_batch')
        for split, purpose, count in zip(splits, ('optimization', 'validation', 'test'), sizes))
    recipe = replace(recipe, method=plugin('proteus.compare', **CompareOptions(evaluations=splits).to_dict()),
        subject=replace(recipe.subject, initial_artifact=artifact), evaluations=evaluations,
        execution=replace(recipe.execution, max_workers=8),
        budget=BudgetLimits(max_evaluations=sum(sizes), max_concurrency=8))
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    lock = registry.make_lock(recipe, [SourcePin(name='proteus',
        repository='https://github.com/proteus-evolve/Proteus.git', revision=revision)])
    before = time.perf_counter()
    result = run_recipe(recipe, registry=registry, lock=lock, root=root / 'run', artifact_source=store.root)
    elapsed = time.perf_counter() - before
    assert result.status.value == 'completed', result.diagnostic
    manifest = json.loads((root / 'run/manifest.json').read_text())
    book = Ledger(root / 'run/private/ledger.sqlite3', run_id='run',
                  config_hash=manifest['config_hash'], lock_hash=manifest['lock_hash'])
    count = book.balance()['entries']
    before = time.perf_counter()
    assert run_recipe(recipe, registry=registry, lock=lock, root=root / 'run', resume=True) == result
    recovery = time.perf_counter() - before
    assert book.balance()['entries'] == count and book.balance()['active_slots'] == 0
    with book._connection() as db:
        assert book._balance(db, 'run') == book._scan_balance(db, 'run')
    return {'split_sizes': dict(zip(splits, sizes)), 'max_workers': 8, 'task_payload_bytes': prompt_bytes,
            'run_seconds': elapsed, 'completed_resume_seconds': recovery,
            'journal': size(root / 'run/private/recovery'), 'run': size(root / 'run'),
            'ledger_entries': count, 'resume_added_dispatches': 0, 'provider_calls': 0,
            'same_completed_result': True, 'balance_matches_independent_scan': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--mode', choices=('all', 'cohort'), default='all')
    parser.add_argument('--cohort-sizes', type=int, nargs=3, default=[500, 300, 200])
    parser.add_argument('--examples', type=int, nargs='+', default=[80, 160, 500])
    parser.add_argument('--ledger-entries', type=int, default=5000)
    parser.add_argument('--prompt-bytes', type=int, default=8192)
    args = parser.parse_args()
    output = args.out.resolve()
    assert not output.is_relative_to(ROOT), 'keep validation artifacts outside the checkout'
    output.mkdir(parents=True, exist_ok=True)
    report = {'python': platform.python_version(), 'platform': platform.platform(),
              'source_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'working_tree_diff_hash': content_hash(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT, text=True)),
              'scope': 'offline managed stages and accounting; no model latency or Docker performance claim',
              'stages': []}
    for count in (args.examples if args.mode == 'all' else []):
        with tempfile.TemporaryDirectory(prefix='stages-', dir=output) as folder:
            row = stage_study(Path(folder) / 'run', count, args.prompt_bytes)
        report['stages'].append(row)
        (output / 'scale.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(row), flush=True)
    if args.mode == 'all':
        with tempfile.TemporaryDirectory(prefix='ledger-', dir=output) as folder:
            report['ledger'] = ledger_study(Path(folder), args.ledger_entries, args.prompt_bytes)
    with tempfile.TemporaryDirectory(prefix='cohort-', dir=output) as folder:
        report['cohort'] = cohort_study(Path(folder), args.cohort_sizes, args.prompt_bytes)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report['peak_rss_bytes'] = peak if platform.system() == 'Darwin' else peak * 1024
    (output / 'scale.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ('ledger', 'cohort') if key in report}), flush=True)


if __name__ == '__main__':
    main()
