"""Model grading has private role state, measured usage and durable replay."""

from dataclasses import replace
import json
import subprocess
import sys

import pytest

from proteus.core.contracts import OperationStatus, Outcome, PluginRef
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.recovery import RecoveryRequired
from proteus.core.serialization import ContractError, content_hash
from proteus.evaluation.records import Grade
from proteus.execution.actors import ChatActor
from proteus.recipes.config import ActorSpec
from proteus.recipes.export import report_run
from proteus.recipes.runner import run_recipe
from test_native_usage import endpoint
from test_v1_evaluation import SOURCE, plugin, setup
from test_v1_ledger import run_ledger


SECRET = 'PRIVATE_REFERENCE_SENTINEL'


def configured(tmp_path, url):
    registry, store, _, recipe = setup(tmp_path, hidden=True, references=(SECRET,))
    judge = ActorSpec(model='fixture-judge', runtime=plugin('proteus.chat', base_url=url,
        key_env='PROTEUS_PRIVATE_GRADER_KEY'))
    spec = replace(recipe.evaluations[0], evaluator=plugin('proteus.model_judge'), actor='judge')
    recipe = replace(recipe, actors={'judge': judge}, evaluations=(spec,))
    return registry, store, recipe


def response(text):
    return {'model': 'fixture-resolved-judge', 'choices': [{'message': {'content': text}}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 5, 'cost_usd': 999}}


def execute(tmp_path, registry, store, recipe):
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / 'run', artifact_source=store.root)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    records = json.loads((tmp_path / 'run/execution-0001.json').read_text())
    path = next((tmp_path / 'run').glob('evaluations/*/private/grades/*.json'))
    grade = Grade.from_dict(json.loads(path.read_text())['grade'])
    return result, records, grade, lock


@pytest.mark.parametrize('malformed', ['valid', 'json', 'content'])
def test_model_grade_is_private_attributed_and_unknown_cost_is_not_zero(tmp_path, monkeypatch, malformed):
    monkeypatch.setenv('PROTEUS_PRIVATE_GRADER_KEY', 'fixture')
    text = 'not json' if malformed == 'json' else json.dumps({'score': .5, 'feedback': SECRET})
    body = response(text)
    if malformed == 'content':
        body['choices'] = []
    with endpoint([body]) as url:
        registry, store, recipe = configured(tmp_path, url)
        result, records, grade, lock = execute(tmp_path, registry, store, recipe)
    assert grade.status == (Outcome.SUCCEEDED if malformed == 'valid' else Outcome.INFRA_ERROR)
    assert grade.score == (.5 if malformed == 'valid' else None)
    assert grade.usage.model_calls == 1 and grade.usage.input_tokens == 11 and grade.usage.output_tokens == 5
    assert grade.usage.cost_usd is None
    assert grade.runtime.runtime == recipe.actors['judge'].runtime.plugin
    assert grade.runtime.model == 'fixture-resolved-judge'
    assert records['actor_calls'] == []
    assert records['evaluations'] == []
    assert SECRET not in json.dumps(records)
    public = report_run(tmp_path / 'run')
    assert SECRET not in json.dumps(public)
    entries = [entry for entry in run_ledger(tmp_path / 'run').entries()
               if entry['identity']['metadata']['kind'] == 'grader']
    assert len(entries) == 1 and entries[0]['identity']['metadata']['role'] == 'judge'
    assert entries[0]['identity']['scopes'] == ['role:judge', 'run']
    assert entries[0]['settlement']['actual']['model_calls'] == 1
    assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / 'run', resume=True) == result


def test_tampered_private_actor_input_blocks_export_and_completed_resume(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTEUS_PRIVATE_GRADER_KEY', 'fixture')
    with endpoint([response('{"score":1}')]) as url:
        registry, store, recipe = configured(tmp_path, url)
        _, _, _, lock = execute(tmp_path, registry, store, recipe)
    path = next((tmp_path / 'run').glob('evaluations/*/private/grader-actors/*/input.json'))
    value = json.loads(path.read_text())
    value['value']['role'] = 'other-role'
    path.chmod(0o644)  # Explicit owner tampering of the immutable published record.
    path.write_text(json.dumps(value))
    with pytest.raises(ContractError, match='private grading evidence identity differs'):
        report_run(tmp_path / 'run')
    with pytest.raises(ContractError, match='private grading evidence identity differs'):
        run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / 'run', resume=True)


def test_unknown_model_grader_dispatch_is_not_repeated(tmp_path):
    calls = []

    class Unknown:
        model_call_bound = 1

        def execute(self, *args):
            calls.append(1)
            raise RecoveryRequired('judge result unknown')

    registry, store, recipe = configured(tmp_path, 'http://127.0.0.1:1')
    ref = PluginRef(name='fixture.unknown_judge', version='1')
    registry.register(Plugin(ref=ref, kind='actor', options=EmptyOptions, factory=lambda options: Unknown(),
        implementation_hash=content_hash('explicit unknown-judge fault fixture'), source='proteus',
        capabilities=('session.fresh', 'actor.execute', 'calls.bounded')))
    recipe = replace(recipe, actors={'judge': replace(recipe.actors['judge'], runtime=plugin(ref.name))})
    lock = registry.make_lock(recipe, [SOURCE])
    for resume in (False, True, True):
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / 'run',
            artifact_source=store.root, resume=resume)
        assert result.status == OperationStatus.FAILED and 'RecoveryRequired' in result.diagnostic
    assert calls == [1]
    entries = [entry for entry in run_ledger(tmp_path / 'run').entries()
               if entry['identity']['metadata']['kind'] == 'grader']
    assert len(entries) == 1 and entries[0]['state'] == 'started'
    assert entries[0]['identity']['metadata']['role'] == 'judge'


def test_grading_requires_an_explicit_role_and_cannot_claim_deterministic(tmp_path):
    registry, _, recipe = configured(tmp_path, 'http://127.0.0.1:1')
    missing = replace(recipe, evaluations=(replace(recipe.evaluations[0], actor=None),))
    with pytest.raises(ContractError, match='grading actor role'):
        registry.resolve(missing)
    unused = replace(recipe, evaluations=(replace(recipe.evaluations[0], evaluator=plugin('proteus.exact_match')),))
    with pytest.raises(ContractError, match='only a model evaluator'):
        registry.resolve(unused)


def test_settled_model_grade_survives_controller_hard_exit_without_second_request(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTEUS_PRIVATE_GRADER_KEY', 'fixture')
    with endpoint([response('{"score":1}')]) as url:
        registry, store, recipe = configured(tmp_path, url)
        lock = registry.make_lock(recipe, [SOURCE])
        (tmp_path / 'recipe.json').write_text(recipe.to_json())
        (tmp_path / 'lock.json').write_text(lock.to_json())
        program = '''
import os,sys
from pathlib import Path
from proteus.core.ledger import Ledger
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import DependencyLock
from proteus.recipes.runner import run_recipe
root=Path(sys.argv[1])
original=Ledger.settle
def settle(self, entry_id, *args, **kwargs):
    value=original(self,entry_id,*args,**kwargs)
    if entry_id.startswith('grader-'):os._exit(86)
    return value
Ledger.settle=settle
run_recipe(load_recipe(root/'recipe.json'), registry=builtin_registry(),
    lock=DependencyLock.from_json((root/'lock.json').read_text()), root=root/'run',
    artifact_source=root/'input-artifacts')
'''
        crashed = subprocess.run([sys.executable, '-c', program, str(tmp_path)], capture_output=True, text=True, timeout=30)
        assert crashed.returncode == 86, crashed.stdout + crashed.stderr
    # The provider has stopped. Resume must use the settled private grade.
    result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / 'run', resume=True)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    report_run(tmp_path / 'run')


def test_grader_role_prompt_is_not_added_to_public_actor_records(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTEUS_PRIVATE_GRADER_KEY', 'fixture')
    original = ChatActor.execute
    prompts = []

    def capture(self, spec, request, workspace):
        prompts.append(request.prompt)
        return original(self, spec, request, workspace)

    monkeypatch.setattr(ChatActor, 'execute', capture)
    with endpoint([response('{"score":1}')]) as url:
        registry, store, recipe = configured(tmp_path, url)
        _, records, _, _ = execute(tmp_path, registry, store, recipe)
    assert len(prompts) == 1 and SECRET in prompts[0]
    assert records['actor_calls'] == [] and SECRET not in json.dumps(records)
