"""Single-island controller: actual independent workers, shared findings and exact evaluations."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from proteus.core.methods import RunServices
from proteus.core.serialization import ContractError, content_hash
from proteus.examples.coral_search import prepare, register
from proteus.examples.worker_lifecycle import wait
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE
from test_v1_ledger import run_ledger


def setup(root, *, workers=2, change=lambda recipe: recipe):
    path = prepare(root / 'study', workers=workers)
    registry = builtin_registry()
    register(registry)
    recipe = registry.resolve(change(load_recipe(path)))
    lock = registry.make_lock(recipe, [SOURCE])
    (root / 'recipe.json').write_text(recipe.to_json())
    (root / 'lock.json').write_text(lock.to_json())
    return recipe, registry, lock


def current(root, recipe, registry, lock):
    return RunServices(run_id='run', recipe=recipe, registry=registry, root=root/'run', lock_hash=content_hash(lock))


def cleanup(services):
    for spec in services.recipe.workers:
        worker = services.worker(spec.id)
        head = worker.controller.head()
        if head is not None and worker.poll(head).completion is None:
            worker.controller.control(head, 'stop')
            wait(worker, head)


def records(root, services):
    execution = json.loads(sorted((root/'run').glob('execution-*.json'))[-1].read_text())
    services.values.restore(execution['values'])
    return execution, next(value for ref, value in services.values._values.values() if ref.kind == 'coral.summary@1')


@pytest.mark.parametrize('workers', [1, 2])
def test_cooperative_search_intervenes_selects_and_tests_after_shutdown(tmp_path, workers):
    recipe, registry, lock = setup(tmp_path, workers=workers)
    services = current(tmp_path, recipe, registry, lock)
    try:
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run',
            artifact_source=tmp_path/'study/artifact-store')
        assert result.status.value == 'completed', result.diagnostic
        execution, summary = records(tmp_path, services)
        assert summary.reason == 'submission_limit' and len(summary.results) == workers + 1
        assert not summary.cancelled and not summary.restarts
        decisions = [services.values.get(ref) for ref in summary.decisions]
        assert sorted(item.observation.score for item in decisions) == [0] + [1] * workers
        assert services.values.get(summary.final_evaluations[0]).batches == ()  # Hidden test.
        selected = services.values.get(summary.selection)
        view = services.values.get(selected.view)
        assert len(selected.result.selected) == 1
        assert next(vector for vector in view.vectors if vector.candidate_id == selected.result.selected[0]).scores == (1,)
        assert len(execution['rollouts']) == 2 + 2 * (workers + 1) + 1
        assert services.gateway('formal').store.closure().reason == 'submission_limit'
        dispatches = [value for ref, value in services.values._values.values() if ref.kind == 'proteus.event.dispatch@1']
        assert len(dispatches) == workers
        for dispatch in dispatches:
            plan = services.values.get(dispatch.plan)
            assert 'Formal evaluation result' in plan.context and 'Submission:' in plan.context
            with pytest.raises(ContractError, match='context changed'):
                services.events('heartbeat').apply(plan.decision, context='different feedback')
        for name in summary.workers:
            worker = services.worker(name)
            assert worker.poll(worker.controller.head()).completion is not None
            launches = [json.loads(line) for line in (worker.controller.workspace/'launches.jsonl').read_text().splitlines()]
            assert len(launches) == 2 and launches[1]['continuity'] == 'native'
            assert launches[1]['session_id'] == 'coral-fixture-' + name
        if workers == 2:
            assert (services.worker('b').controller.workspace/'learned.json').is_file()
            snapshot = services.values.get(summary.knowledge)
            revisions = services.knowledge('research').store.records(snapshot)
            assert any(record.write.kind == 'skills' and len(record.write.sources) == 2 for record in revisions)
        ledger = run_ledger(tmp_path/'run')
        before = ledger.events()
        assert ledger.balance()['active_slots'] == 0
        assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True) == result
        assert ledger.events() == before
    finally:
        cleanup(services)


def test_fresh_continuation_requires_and_records_explicit_orientation(tmp_path):
    def change(recipe):
        policy = replace(recipe.event_policies[0], resume_policy='fresh',
            fresh_orientation='This is a fresh generation. Read candidate/ and the shared notes before continuing.')
        return replace(recipe, event_policies=(policy,))
    recipe, registry, lock = setup(tmp_path, workers=1, change=change)
    services = current(tmp_path, recipe, registry, lock)
    try:
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run',
            artifact_source=tmp_path/'study/artifact-store')
        assert result.status.value == 'completed', result.diagnostic
        launches = [json.loads(line) for line in (services.worker('a').controller.workspace/'launches.jsonl').read_text().splitlines()]
        assert len(launches) == 2 and launches[1]['continuity'] == 'fresh_fallback'
        assert launches[1]['session_id'] is None
        assert launches[1]['prompt'].startswith('This is a fresh generation.')
    finally:
        cleanup(services)


def test_controller_cancels_only_undispatched_pending_work_before_stopping(tmp_path, monkeypatch):
    from proteus.gateway.records import GatewaySubmission
    from proteus.gateway.service import GatewayService
    from proteus.artifacts.codecs import FileWrite
    recipe, registry, lock = setup(tmp_path, workers=1, change=lambda recipe: replace(recipe,
        gateways=(replace(recipe.gateways[0], max_evaluations_per_worker=6),)))
    services = current(tmp_path, recipe, registry, lock)
    original = GatewayService.close

    def close(self, *args):
        # An already-uploaded future proposal is present at the exact close boundary.
        self.submit('a', GatewaySubmission(operation_id='queued-extra', evaluation_id='validation',
            files=(FileWrite.from_bytes('main.py', b"raise AssertionError('must not execute')\n"),)))
        return original(self, *args)

    monkeypatch.setattr(GatewayService, 'close', close)
    try:
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run',
            artifact_source=tmp_path/'study/artifact-store')
        assert result.status.value == 'completed', result.diagnostic
        execution, summary = records(tmp_path, services)
        assert len(summary.cancelled) == 1
        cancelled = services.values.get(summary.cancelled[0])
        assert cancelled.candidate is cancelled.evidence is None
        assert 'not dispatched' in cancelled.diagnostic
        assert len(execution['rollouts']) == 7 and not services.gateway('formal').store.pending()
        assert run_ledger(tmp_path/'run').balance()['charged']['evaluations'] == 7
    finally:
        cleanup(services)


def fixture_options(recipe, *args, **method):
    workers = tuple(replace(worker, runtime=replace(worker.runtime,
        options={**worker.runtime.options, 'command': (*worker.runtime.options['command'], *args)}))
        for worker in recipe.workers)
    return replace(recipe, workers=workers, method=replace(recipe.method, options={**recipe.method.options, **method}))


def test_poll_limit_stops_idle_workers_and_selects_the_evaluated_seed(tmp_path):
    recipe, registry, lock = setup(tmp_path, workers=1,
        change=lambda recipe: fixture_options(recipe, '--idle', max_polls=3))
    services = current(tmp_path, recipe, registry, lock)
    try:
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run',
            artifact_source=tmp_path/'study/artifact-store')
        assert result.status.value == 'completed', result.diagnostic
        execution, summary = records(tmp_path, services)
        assert summary.reason == 'poll_limit' and summary.polls == 3 and not summary.results
        assert len(execution['rollouts']) == 3
        assert run_ledger(tmp_path/'run').balance()['active_slots'] == 0
    finally:
        cleanup(services)


@pytest.mark.parametrize('restart_limit', [0, 1])
def test_dead_worker_restarts_with_recorded_native_session_or_closes_at_bound(tmp_path, restart_limit):
    def change(recipe):
        recipe = fixture_options(recipe, '--exit-first', max_restarts_per_worker=restart_limit)
        policy = recipe.event_policies[0]
        return replace(recipe, event_policies=(replace(policy, operator=replace(policy.operator,
            options={**policy.operator.options, 'rules': [{**rule, 'every': 100}
                for rule in policy.operator.options['rules']]})),))
    recipe, registry, lock = setup(tmp_path, workers=1, change=change)
    services = current(tmp_path, recipe, registry, lock)
    try:
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run',
            artifact_source=tmp_path/'study/artifact-store')
        assert result.status.value == 'completed', result.diagnostic
        _, summary = records(tmp_path, services)
        assert summary.reason == ('submission_limit' if restart_limit else 'worker_failure')
        assert summary.restarts.get('a', 0) == restart_limit
        assert len(summary.results) == restart_limit + 1
        launches = [json.loads(line) for line in (services.worker('a').controller.workspace/'launches.jsonl').read_text().splitlines()]
        if restart_limit:
            assert launches[-1]['continuity'] == 'native'
            assert 'Formal evaluation result' in launches[-1]['prompt']
        assert run_ledger(tmp_path/'run').balance()['active_slots'] == 0
    finally:
        cleanup(services)


def test_configuration_rejects_missing_knowledge_and_incomparable_protocols(tmp_path):
    recipe, registry, _ = setup(tmp_path)
    with pytest.raises(ContractError, match='shared notes'):
        registry.resolve(replace(recipe, gateways=(replace(recipe.gateways[0], knowledge_access='read'),)))
    with pytest.raises(ContractError, match='unknown worker prompt'):
        registry.resolve(fixture_options(recipe, worker_prompts={'foreign': 'hello'}))
    with pytest.raises(ContractError, match='directions differ'):
        registry.resolve(fixture_options(recipe, minimize=True))


CRASH = r'''
import os, sys
from pathlib import Path
from proteus.core.methods import RunServices
from proteus.gateway.service import GatewayService
from proteus.gateway.store import GatewayStore
from proteus.events.service import EventService
from proteus.recipes.manifest import DependencyLock
from proteus.workers.service import WorkerService
from test_v1_coral_method import load_recipe, builtin_registry, register, run_recipe
root, boundary = Path(sys.argv[1]), sys.argv[2]
recipe = load_recipe(root/'recipe.json')
lock = DependencyLock.from_json((root/'lock.json').read_text())
registry = builtin_registry(); register(registry)

def wrap(cls, name, match):
    original = getattr(cls, name)
    def call(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if match(args, kwargs, result): os._exit(86)
        return result
    setattr(cls, name, call)

if boundary == 'baseline':
    wrap(RunServices, 'evaluate', lambda a,k,r:k.get('operation_id')=='coral:baseline')
elif boundary == 'checkpoint':
    wrap(RunServices, 'checkpoint', lambda a,k,r:a[0].phase=='search')
elif boundary == 'evaluation':
    wrap(GatewayService, 'process', lambda a,k,r:True)
elif boundary == 'intervention':
    wrap(EventService, 'apply', lambda a,k,r:bool(r))
elif boundary == 'poll':
    wrap(RunServices, '_record', lambda a,k,r:a[0]=='coral.poll')
elif boundary == 'closure':
    wrap(GatewayStore, 'close', lambda a,k,r:True)
elif boundary == 'shutdown':
    wrap(WorkerService, 'control', lambda a,k,r:len(a)>1 and a[1]=='stop')
elif boundary == 'selection':
    wrap(RunServices, 'select_final', lambda a,k,r:True)
elif boundary == 'hidden_test':
    wrap(RunServices, 'evaluate', lambda a,k,r:k.get('operation_id','').startswith('coral:final-test:'))
result = run_recipe(recipe, registry=registry, lock=lock, root=root/'run', artifact_source=root/'study/artifact-store')
raise RuntimeError(result.diagnostic)
'''


@pytest.mark.parametrize('boundary', ['baseline', 'checkpoint', 'evaluation', 'intervention', 'poll',
    'closure', 'shutdown', 'selection', 'hidden_test'])
def test_hard_exit_recovers_controller_without_duplicate_trials_or_worker_generations(tmp_path, boundary):
    recipe, registry, lock = setup(tmp_path)
    services = current(tmp_path, recipe, registry, lock)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).parent), str(Path(__file__).parents[1])]))
    try:
        process = subprocess.run([sys.executable, '-c', CRASH, str(tmp_path), boundary],
            env=env, capture_output=True, timeout=60)
        assert process.returncode == 86, process.stdout.decode() + process.stderr.decode()
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True)
        assert result.status.value == 'completed', result.diagnostic
        execution, summary = records(tmp_path, services)
        assert summary.reason == 'submission_limit' and len(summary.results) == 3
        assert len(execution['rollouts']) == 9
        assert len({rollout['id'] for rollout in execution['rollouts']}) == 9
        for name in ('a', 'b'):
            assert len((services.worker(name).controller.workspace/'launches.jsonl').read_text().splitlines()) == 2
        ledger = run_ledger(tmp_path/'run')
        assert ledger.balance()['active_slots'] == 0
        assert ledger.balance()['charged']['evaluations'] == 9
        before = ledger.events()
        assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True) == result
        assert ledger.events() == before
    finally:
        cleanup(services)
