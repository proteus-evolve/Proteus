"""Configured persistent workers through the method journal and public runner."""

from dataclasses import replace
import json
import subprocess
import sys

import pytest

from proteus.core.contracts import BudgetLimits
from proteus.core.methods import MethodResult, RunServices
from proteus.core.serialization import ContractError, content_hash
from proteus.examples.worker_lifecycle import prepare, register, wait
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE
from test_v1_ledger import run_ledger


def setup(root):
    path = prepare(root / 'study')
    registry = builtin_registry()
    register(registry)
    recipe = registry.resolve(load_recipe(path))
    lock = registry.make_lock(recipe, [SOURCE])
    (root / 'recipe.json').write_text(recipe.to_json())
    (root / 'lock.json').write_text(lock.to_json())
    return recipe, registry, lock


def execute(root, recipe, registry, lock, *, resume=False):
    return run_recipe(recipe, registry=registry, lock=lock, root=root / 'run', resume=resume)


def services(root, recipe, registry, lock):
    return RunServices(run_id='run', recipe=recipe, registry=registry, root=root / 'run', lock_hash=content_hash(lock))


def test_recipe_runs_two_workers_pauses_resumes_and_exports_terminal_evidence(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    assert any(pin.plugin.name == 'proteus.worker.command' for pin in lock.plugins)
    result = execute(tmp_path, recipe, registry, lock)
    assert result.status.value == 'completed', result.diagnostic
    events = run_ledger(tmp_path / 'run').events()
    assert execute(tmp_path, recipe, registry, lock, resume=True) == result
    assert run_ledger(tmp_path / 'run').events() == events
    current = services(tmp_path, recipe, registry, lock)
    a, b = current.worker('a').controller, current.worker('b').controller
    assert len((a.workspace / 'launches.jsonl').read_text().splitlines()) == 2
    assert len((b.workspace / 'launches.jsonl').read_text().splitlines()) == 1
    assert a.poll(a.head()).completion.resumed_from == 'a:first'
    assert len(current.ledger.entries()) == 3
    assert current.ledger.balance()['active_slots'] == 0


def test_preflight_rejects_unknown_roles_and_unsupported_caps(tmp_path):
    recipe, registry, _ = setup(tmp_path)
    with pytest.raises(ContractError, match='unknown actor role'):
        replace(recipe, workers=(replace(recipe.workers[0], role='missing'),))
    with pytest.raises(ContractError, match='finite budgets'):
        registry.resolve(replace(recipe, budget=BudgetLimits(max_model_calls=5, max_concurrency=2)))


WORKER = r'''
import os, sys
from pathlib import Path
from proteus.core.recovery import Journal
from proteus.examples.worker_lifecycle import register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import DependencyLock
from proteus.recipes.runner import run_recipe
from proteus.workers.runtime import CommandWorkerRuntime
root, boundary = Path(sys.argv[1]), sys.argv[2]
registry = builtin_registry(); register(registry)
recipe = load_recipe(root/'recipe.json')
lock = DependencyLock.from_json((root/'lock.json').read_text())
if boundary in ('dispatch.before', 'dispatch.after'):
    original = CommandWorkerRuntime.start
    def start(self, *args):
        if boundary == 'dispatch.before': os._exit(86)
        original(self, *args)
        os._exit(86)
    CommandWorkerRuntime.start = start
else:
    original = Journal.call
    def call(self, kind, *args, **kwargs):
        result = original(self, kind, *args, **kwargs)
        if kind == boundary: os._exit(86)
        return result
    Journal.call = call
run_recipe(recipe, registry=registry, lock=lock, root=root/'run')
'''


@pytest.mark.parametrize('boundary', ['dispatch.before', 'dispatch.after', 'worker.start', 'worker.control'])
def test_method_hard_exit_reconnects_original_generation_and_its_budget(tmp_path, boundary):
    recipe, registry, lock = setup(tmp_path)
    stopped = subprocess.run([sys.executable, '-c', WORKER, str(tmp_path), boundary],
                             text=True, capture_output=True, timeout=30)
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    result = execute(tmp_path, recipe, registry, lock, resume=True)
    assert result.status.value == 'completed', result.diagnostic
    current = services(tmp_path, recipe, registry, lock)
    assert len(current.ledger.entries()) == 3
    a = current.worker('a').controller
    assert len((a.workspace / 'launches.jsonl').read_text().splitlines()) == 2
    assert json.loads((a.workspace / 'session.json').read_text())['turns'] == 2
    events = current.ledger.events()
    assert execute(tmp_path, recipe, registry, lock, resume=True) == result
    assert current.ledger.events() == events


@pytest.mark.parametrize('raises', [False, True])
def test_method_cannot_publish_terminal_result_with_live_workers(tmp_path, monkeypatch, raises):
    from proteus.examples.worker_lifecycle import WorkerMethod
    recipe, registry, lock = setup(tmp_path)
    original = WorkerMethod.run

    def early(self, services, state):
        worker = services.worker('a')
        ref = worker.start('first', 'wait for intervention')
        wait(worker, ref, ready=True)
        if raises:
            raise ValueError('method failed after starting worker')
        return MethodResult()

    monkeypatch.setattr(WorkerMethod, 'run', early)
    result = execute(tmp_path, recipe, registry, lock)
    assert result.status.value == 'failed' and 'active or uncertain worker' in result.diagnostic
    # No terminal method.result was committed; the real method can resume the
    # same generation, complete the remaining protocol and close the run.
    monkeypatch.setattr(WorkerMethod, 'run', original)
    result = execute(tmp_path, recipe, registry, lock, resume=True)
    assert result.status.value == 'completed', result.diagnostic


def test_resume_verifies_previous_generation_outputs_not_only_the_current_head(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    result = execute(tmp_path, recipe, registry, lock)
    assert result.status.value == 'completed', result.diagnostic
    a = services(tmp_path, recipe, registry, lock).worker('a').controller
    (a._directory('first') / 'stdout.log').write_text('tampered historical output')
    with pytest.raises(ContractError, match='output changed'):
        execute(tmp_path, recipe, registry, lock, resume=True)
