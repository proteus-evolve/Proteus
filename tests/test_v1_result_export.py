"""Rebuild research tables from saved evidence, including corruption and privacy checks."""

import json
import sqlite3
import subprocess
import sys

import pytest

from check_recipe_catalog import check_example
from proteus.core.serialization import ContractError, content_hash
from proteus.recipes.export import export_run, report_run


@pytest.mark.parametrize('name', ['candidate-comparison', 'search-policy', 'ace', 'meta-harness',
                                 'combee-ace', 'coral', 'gepa', 'combee-gepa'])
def test_saved_examples_export_without_plugins_or_duplicate_resume_trials(tmp_path, name):
    if name in ('gepa', 'combee-gepa'):
        pytest.importorskip('gepa')
    records = check_example(tmp_path/name, name)
    root = tmp_path/name/'run'
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    public = report_run(root)
    private = report_run(root, include_private=True)
    assert len(private['tasks']) == len(records['rollouts'])
    assert len(private['candidates']) == len(records['candidates'])
    assert len({row['rollout_id'] for row in private['tasks']}) == len(private['tasks'])
    assert private['snapshot'] == 'execution-0002.json'
    assert private['accounting']['actual']['evaluations'] == len(private['tasks'])
    assert not private['unreported_rollout_ids']
    assert any(row['visibility'] == 'hidden' for row in public['tasks'])
    assert all(row['score'] is None and row['grade_status'] is None and not row['metrics']
               for row in public['tasks'] if row['visibility'] == 'hidden')
    assert all(not path.startswith('private/') and '/private/' not in path for path in public['evidence'])
    for cohort in private['cohorts']:
        rows = [row for row in private['tasks'] if row['batch_id'] == cohort['batch_id']]
        assert len(rows) == cohort['tasks']
        expected = None if any(row['score'] is None for row in rows) else sum(row['score'] for row in rows)/len(rows)
        assert cohort['mean_score'] == pytest.approx(expected) if expected is not None else cohort['mean_score'] is None
    assert before == {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    assert private['report_hash'] == content_hash({k: v for k, v in private.items() if k != 'report_hash'})


def test_export_cli_writes_reusable_tables_and_refuses_overwrite(tmp_path):
    check_example(tmp_path/'example', 'search-policy')
    root = tmp_path/'example/run'
    command = [sys.executable, '-m', 'proteus.cli', 'recipe', 'export', str(root)]
    subprocess.run([*command, '--out', str(tmp_path/'public')], check=True, capture_output=True)
    subprocess.run([*command, '--out', str(tmp_path/'owner'), '--include-private'], check=True, capture_output=True)
    public = json.loads((tmp_path/'public/report.json').read_text())
    owner = json.loads((tmp_path/'owner/report.json').read_text())
    assert owner['source_run'] == str(root.resolve())
    assert next(row['score'] for row in owner['tasks'] if row['visibility'] == 'hidden') == pytest.approx(.8)
    assert next(row['score'] for row in public['tasks'] if row['visibility'] == 'hidden') is None
    assert 'null' in (tmp_path/'public/tasks.csv').read_text()
    assert {path.name for path in (tmp_path/'owner').iterdir()} == {
        'report.json', 'tasks.csv', 'cohorts.csv', 'candidates.csv', 'costs.csv', 'role-stage-costs.csv', 'stage-attempts.csv', 'controller-intervals.csv', 'native-usage.csv'}
    assert subprocess.run([*command, '--out', str(tmp_path/'owner')], capture_output=True).returncode == 2
    with pytest.raises(ContractError, match='outside'):
        export_run(root, root/'export')


@pytest.mark.parametrize('target', ['prediction', 'hidden_grade', 'artifact', 'ledger', 'values'])
def test_inconsistent_saved_evidence_is_rejected(tmp_path, target):
    check_example(tmp_path, 'search-policy')
    root = tmp_path/'run'
    if target == 'artifact':
        path = next((root/'artifacts/blobs').iterdir())
        path.chmod(0o644)
        path.write_bytes(b'changed artifact')
    elif target == 'ledger':
        with sqlite3.connect(root/'private/ledger.sqlite3') as db:
            key, raw = db.execute('SELECT id, settlement_json FROM entries LIMIT 1').fetchone()
            record = json.loads(raw)
            record['actual']['cost_usd'] = 123
            db.execute('UPDATE entries SET settlement_json=? WHERE id=?', (json.dumps(record), key))
    else:
        if target == 'prediction':
            path = next((root/'evaluations').glob('*/predictions/*.json'))
            raw = json.loads(path.read_text())
            raw['rollout']['output'] = 'changed'
        elif target == 'hidden_grade':
            directory = next(p.parent for p in (root/'evaluations').glob('*/protocol.json')
                             if json.loads(p.read_text())['evaluation']['visibility'] == 'hidden')
            path = next((directory/'private/batches').glob('*.json'))
            raw = json.loads(path.read_text())
            raw['rows'][0]['score'] = .99
        else:
            path = root/'execution-0002.json'
            raw = json.loads(path.read_text())
            raw['values'][0]['value'] = {'schema_version': 1, 'tampered': True}
        path.chmod(0o644)
        path.write_text(json.dumps(raw))
    with pytest.raises(ContractError):
        report_run(root, include_private=True)


def test_unknown_actual_usage_is_not_replaced_with_reserved_budget(tmp_path):
    from proteus.core.ledger import Amounts, Ledger
    from proteus.recipes.manifest import RunManifest
    check_example(tmp_path, 'ace')
    root = tmp_path/'run'
    manifest = RunManifest.from_json((root/'manifest.json').read_text())
    ledger = Ledger(root/'private/ledger.sqlite3', run_id=manifest.run_id, config_hash=manifest.config_hash,
                    lock_hash=manifest.lock_hash)
    ledger.reserve('unknown-model-call', scopes={'run': manifest.recipe.budget},
                   reservation=Amounts(model_calls=2, cost_usd=3, wall_seconds=4),
                   metadata={'kind': 'test-call', 'role': 'reflection', 'stage': 'reflect'}, request={})
    ledger.start('unknown-model-call')
    ledger.settle('unknown-model-call', actual=Amounts(), status='failed')
    costs = report_run(root)['accounting']
    assert costs['unknown_actual_entries']['model_calls'] == 1
    assert costs['actual']['model_calls'] is None
    assert costs['budget_charged']['model_calls'] == 2
    assert costs['actual']['cost_usd'] is None and costs['budget_charged']['cost_usd'] == 3
    assert any(row['model_calls'] is None for row in costs['by_role_stage'])
    assert costs['ledger_span_seconds'] >= 0


def test_export_refuses_active_operations_and_controller_lease(tmp_path):
    from proteus.core.ledger import Amounts, Ledger
    from proteus.core.run_lock import run_lock
    from proteus.recipes.manifest import RunManifest
    check_example(tmp_path, 'search-policy')
    root = tmp_path/'run'
    with run_lock(root), pytest.raises(ValueError, match='controller'):
        report_run(root)
    manifest = RunManifest.from_json((root/'manifest.json').read_text())
    ledger = Ledger(root/'private/ledger.sqlite3', run_id=manifest.run_id, config_hash=manifest.config_hash,
                    lock_hash=manifest.lock_hash)
    ledger.reserve('unfinished-export-check', scopes={'run': manifest.recipe.budget}, reservation=Amounts.zero(),
                   metadata={'kind': 'test'}, request={})
    ledger.start('unfinished-export-check')
    with pytest.raises(ContractError, match='settled'):
        report_run(root)


def test_failed_candidate_keeps_missing_grades_and_does_not_shrink_the_denominator(tmp_path):
    from dataclasses import replace
    from proteus.artifacts.codecs import FileContent
    from proteus.artifacts.store import ArtifactStore
    from proteus.core.contracts import PluginRef
    from proteus.examples.search_policy import prepare, register
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.manifest import SourcePin
    from proteus.recipes.runner import run_recipe
    registry = builtin_registry()
    register(registry)
    recipe = load_recipe(prepare(tmp_path/'study'))
    store = ArtifactStore(tmp_path/'study/artifact-store', registry)
    bad = store.put(PluginRef(name='proteus.file_tree', version='1'), {'main.py': FileContent(b'raise RuntimeError("failed candidate")')})
    recipe = registry.resolve(replace(recipe, subject=replace(recipe.subject, initial_artifact=bad),
                                      method=replace(recipe.method, options={'variants': []})))
    lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/export-fixture', revision='0'*40)])
    result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', artifact_source=tmp_path/'study/artifact-store')
    assert result.status.value == 'failed'
    report = report_run(tmp_path/'run')
    assert report['status'] == 'failed'
    assert len(report['tasks']) == 2 and all(row['score'] is None for row in report['tasks'])
    assert report['cohorts'][0]['tasks'] == 2 and report['cohorts'][0]['mean_score'] is None
    assert all(row['score_availability'] == 'unavailable' for row in report['tasks'])


def test_stage_retry_counts_are_actual_attempts_not_resume_snapshots(tmp_path):
    from dataclasses import replace
    from proteus.core.contracts import PluginRef
    from proteus.core.plugins import EmptyOptions, Plugin
    from proteus.examples.candidate_evaluation import plugin
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import Text, builtin_registry
    from proteus.recipes.manifest import SourcePin
    from proteus.recipes.runner import run_recipe
    from pathlib import Path
    class RetryOnce:
        calls = 0
        def run(self, context, inputs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('first attempt failed')
            return {'response': Text(text='second attempt succeeded')}
    operator = RetryOnce()
    registry = builtin_registry()
    registry.register(Plugin(ref=PluginRef(name='example.retry_export', version='1'), kind='stage',
        options=EmptyOptions, factory=lambda options: operator, implementation_hash='sha256:'+'0'*64,
        source='proteus', inputs={'prompt':'proteus.text@1'}, outputs={'response':'proteus.text@1'}))
    recipe = load_recipe(Path(__file__).resolve().parents[1]/'recipes/examples/stages.toml')
    recipe = registry.resolve(replace(recipe, stages=(replace(recipe.stages[0], operator=plugin('example.retry_export'),
        output_types={'response':'proteus.text@1'}, write_scopes=('proteus.text@1',), max_attempts=2, failure_policy='retry'),)))
    lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/export-fixture', revision='0'*40)])
    result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run')
    assert result.status.value == 'completed', result.diagnostic
    assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True) == result
    report = report_run(tmp_path/'run')
    assert report['attempts']['additional_attempts'] == 1
    assert report['attempts']['statuses'] == {'failed':1,'completed':1}
    assert len(report['stages']) == 2 and not report['tasks']
