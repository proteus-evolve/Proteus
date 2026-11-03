"""Managed HTTP actors retain observed usage when a response fails validation."""
from dataclasses import replace
import json
import os
import subprocess
import sys

import pytest

from proteus.core.contracts import BudgetLimits, OperationStatus, PluginRef
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import PluginSpec
from proteus.recipes.export import report_run
from proteus.recipes.runner import run_recipe
from test_native_usage import endpoint, response
from test_v1_ledger import run_ledger
from test_v1_plugins import SOURCE, stage_recipe


def configured(url, *, retry=False):
    registry = builtin_registry()
    recipe = stage_recipe()
    actor = replace(recipe.actors['optimizer'], model='fixture', runtime=PluginSpec(
        plugin=PluginRef(name='proteus.chat', version='1'),
        options={'base_url': url, 'key_env': 'CHAT_ACCOUNTING_FIXTURE_KEY'}))
    stage = replace(recipe.stages[0], max_attempts=2 if retry else 1,
                    failure_policy='retry' if retry else 'stop')
    recipe = replace(recipe, actors={'optimizer': actor}, stages=(stage,),
                     budget=BudgetLimits(max_model_calls=2 if retry else 1))
    return recipe, registry, registry.make_lock(recipe, [SOURCE])


def execute(root, configuration, *, resume=False):
    recipe, registry, lock = configuration
    result = run_recipe(recipe, registry=registry, lock=lock, root=root, resume=resume)
    snapshot = json.loads(sorted(root.glob('execution-*.json'))[-1].read_text())
    return result, snapshot


@pytest.mark.parametrize('broken', [
    'missing_choices', 'empty_choices', 'invalid_choices', 'invalid_choice',
    'invalid_message', 'nontext_content', 'tool_calls', 'invalid_model', 'invalid_finish_reason',
])
def test_invalid_response_preserves_usage_in_ledger_and_export(tmp_path, monkeypatch, broken):
    monkeypatch.setenv('CHAT_ACCOUNTING_FIXTURE_KEY', 'PRIVATE-KEY')
    body = response({'prompt_tokens': 13, 'completion_tokens': 7})
    if broken == 'missing_choices':
        del body['choices']
    elif broken == 'empty_choices':
        body['choices'] = []
    elif broken == 'invalid_choices':
        body['choices'] = 'PRIVATE-PROVIDER-DATA'
    elif broken == 'invalid_choice':
        body['choices'] = ['PRIVATE-PROVIDER-DATA']
    elif broken == 'invalid_message':
        body['choices'][0]['message'] = None
    elif broken == 'nontext_content':
        body['choices'][0]['message']['content'] = ['PRIVATE-PROVIDER-DATA']
    elif broken == 'tool_calls':
        body['choices'][0]['message']['tool_calls'] = [{'secret': 'PRIVATE-PROVIDER-DATA'}]
    elif broken == 'invalid_model':
        body['model'] = {'secret': 'PRIVATE-PROVIDER-DATA'}
    else:
        body['choices'][0]['finish_reason'] = {'secret': 'PRIVATE-PROVIDER-DATA'}
    with endpoint([body]) as url:
        result, snapshot = execute(tmp_path/'run', configured(url))
    assert result.status == OperationStatus.FAILED
    call = snapshot['actor_calls'][0]
    assert call['result']['status'] == 'failed'
    assert call['usage']['model_calls'] == 1
    assert (call['usage']['input_tokens'], call['usage']['output_tokens']) == (13, 7)
    assert call['usage']['cost_usd'] is None and call['usage']['wall_seconds'] >= 0
    report = report_run(tmp_path/'run')
    assert report['accounting']['actual']['model_calls'] == 1
    assert report['accounting']['actor_tokens']['reported'] == {'input_tokens': 13., 'output_tokens': 7.}
    assert run_ledger(tmp_path/'run').entries()[0]['state'] == 'failed'
    assert 'PRIVATE-KEY' not in json.dumps(snapshot)
    assert 'PRIVATE-PROVIDER-DATA' not in json.dumps(snapshot)


@pytest.mark.parametrize('usage,expected', [
    ({'prompt_tokens': 0, 'completion_tokens': 0}, (0, 0)),
    (None, (None, None)),
    ({'prompt_tokens': 9}, (9, None)),
    ({'prompt_tokens': True, 'completion_tokens': 4}, (None, 4)),
    ({'prompt_tokens': -1, 'completion_tokens': 4}, (None, 4)),
    ({'prompt_tokens': 2.5, 'completion_tokens': '7'}, (None, None)),
    ({'prompt_tokens': 13, 'completion_tokens': 7, 'cost_usd': 99,
      'prompt_tokens_details': {'cached_tokens': 8}, 'completion_tokens_details': {'reasoning_tokens': 2}}, (13, 7)),
])
def test_reported_counts_are_independent_and_subcounts_are_not_added(tmp_path, monkeypatch, usage, expected):
    monkeypatch.setenv('CHAT_ACCOUNTING_FIXTURE_KEY', 'fixture')
    with endpoint([response(usage)]) as url:
        result, snapshot = execute(tmp_path/'run', configured(url))
    assert result.status == OperationStatus.COMPLETED
    observed = snapshot['actor_calls'][0]['usage']
    assert (observed['input_tokens'], observed['output_tokens']) == expected
    assert observed['model_calls'] == 1 and observed['cost_usd'] is None


def test_unreadable_response_retains_unknown_usage_and_charged_reservation(tmp_path, monkeypatch):
    monkeypatch.setenv('CHAT_ACCOUNTING_FIXTURE_KEY', 'fixture')
    with endpoint([b'not-json']) as url:
        result, snapshot = execute(tmp_path/'run', configured(url))
    assert result.status == OperationStatus.FAILED
    assert snapshot['actor_calls'][0]['usage']['model_calls'] is None
    assert snapshot['actor_calls'][0]['usage']['input_tokens'] is None
    assert run_ledger(tmp_path/'run').balance()['charged']['model_calls'] == 1


CRASH = r'''
import os,sys
from pathlib import Path
from proteus.core.ledger import Ledger
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import DependencyLock
from proteus.recipes.runner import run_recipe
root=Path(sys.argv[1])
original=Ledger.settle
def settled(self, entry_id, **kwargs):
    result=original(self,entry_id,**kwargs)
    if self.entry(entry_id)['identity']['metadata'].get('kind')=='actor':
        os._exit(86)
    return result
Ledger.settle=settled
run_recipe(load_recipe(root/'recipe.json'),registry=builtin_registry(),
    lock=DependencyLock.from_json((root/'lock.json').read_text()),root=root/'run')
'''


def test_failed_call_settlement_survives_hard_exit_and_retry_without_replay(tmp_path, monkeypatch):
    monkeypatch.setenv('CHAT_ACCOUNTING_FIXTURE_KEY', 'fixture')
    first = {'choices': [], 'usage': {'prompt_tokens': 13, 'completion_tokens': 7}}
    second = response({'prompt_tokens': 5, 'completion_tokens': 2})
    with endpoint([first, second]) as url:
        recipe, registry, lock = configuration = configured(url, retry=True)
        (tmp_path/'recipe.json').write_text(recipe.to_json())
        (tmp_path/'lock.json').write_text(lock.to_json())
        child = subprocess.run([sys.executable, '-c', CRASH, str(tmp_path)],
            capture_output=True, text=True, timeout=30, env=dict(os.environ))
        assert child.returncode == 86, child.stderr
        assert run_ledger(tmp_path/'run').entries()[0]['settlement']['result']['usage']['input_tokens'] == 13
        result, snapshot = execute(tmp_path/'run', configuration, resume=True)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert len(snapshot['actor_calls']) == 2
    book = run_ledger(tmp_path/'run')
    assert [entry['state'] for entry in book.entries()].count('failed') == 1
    assert book.balance()['charged']['model_calls'] == 2 and book.balance()['active_slots'] == 0
    report = report_run(tmp_path/'run')
    assert report['accounting']['actor_tokens']['reported'] == {'input_tokens': 18., 'output_tokens': 9.}
    before = book.events()
    assert execute(tmp_path/'run', configuration, resume=True)[0] == result
    assert book.events() == before
