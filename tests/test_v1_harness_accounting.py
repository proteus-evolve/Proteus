"""Actual harness receipts reach managed actor accounting and survive recovery."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from proteus.adapters.llm import LLMHarness
from proteus.core.contracts import BudgetLimits, OperationStatus, PluginRef
from proteus.core.native_inputs import input_directory, read_native_inputs
from proteus.core.serialization import ContractError
from proteus.execution.actors import HarnessActor
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import PluginSpec
from proteus.recipes.export import report_run
from proteus.recipes.runner import run_recipe
from test_native_usage import endpoint, response
from test_v1_plugins import SOURCE, stage_recipe


def configured(name='minimal', **options):
    registry = builtin_registry()
    recipe = stage_recipe()
    actor = replace(recipe.actors['optimizer'], model=None if name == 'minimal' else 'fixture',
        runtime=PluginSpec(plugin=PluginRef(name='proteus.harness.'+name, version='1'), options=options))
    recipe = replace(recipe, budget=BudgetLimits(), actors={'optimizer': actor})
    return recipe, registry, registry.make_lock(recipe, [SOURCE])


def execute(root, configuration, *, resume=False):
    recipe, registry, lock = configuration
    result = run_recipe(recipe, registry=registry, lock=lock, root=root, resume=resume)
    snapshot = json.loads(sorted(root.glob('execution-*.json'))[-1].read_text())
    return result, snapshot


def test_mock_actor_reports_zero_and_resume_survives_deleted_disposable_workspace(tmp_path):
    root = tmp_path/'run'
    configuration = configured()
    result, snapshot = execute(root, configuration)
    assert result.status == OperationStatus.COMPLETED
    call = snapshot['actor_calls'][0]
    assert call['usage']['model_calls'] == call['usage']['input_tokens'] == call['usage']['cost_usd'] == 0
    assert call['result']['runtime']['settings']['native_usage']['boundary_count'] == 1
    before = read_native_inputs(root/'actors'/call['id'])
    shutil.rmtree(root/'actors'/call['id'])
    resumed, saved = execute(root, configuration, resume=True)
    assert resumed == result and saved['actor_calls'] == snapshot['actor_calls']
    assert read_native_inputs(root/'actors'/call['id']) == before
    public, owner = report_run(root), report_run(root, include_private=True)
    assert public['accounting']['actor_tokens']['reported'] == {'input_tokens': 0., 'output_tokens': 0.}
    assert len(public['accounting']['native_receipts']) == 1
    assert not any('.proteus-records' in path for path in public['evidence'])
    assert sum('.proteus-records' in path for path in owner['evidence']) == 2
    assert public['native_accounting']['coverage'] == 'unavailable'  # Already counted in managed ledger.
    relocated = tmp_path/'moved'
    shutil.copytree(root, relocated)
    assert report_run(relocated)['accounting'] == public['accounting']


@pytest.mark.parametrize('broken', [False, True])
def test_http_success_and_failure_keep_observed_tokens_in_role_accounting(tmp_path, monkeypatch, broken):
    monkeypatch.setenv('LOCAL_FIXTURE_KEY', 'fixture')
    body = response({'prompt_tokens': 13, 'completion_tokens': 7})
    if broken:
        body['choices'] = []
    with endpoint([body]) as url:
        config = configured('llm', base_url=url, key_env='LOCAL_FIXTURE_KEY')
        result, snapshot = execute(tmp_path/'run', config)
    assert result.status == (OperationStatus.FAILED if broken else OperationStatus.COMPLETED)
    call = snapshot['actor_calls'][0]
    assert call['usage']['model_calls'] == 1
    assert call['usage']['input_tokens'] == 13 and call['usage']['output_tokens'] == 7
    report = report_run(tmp_path/'run')
    row = report['accounting']['by_role_stage'][0]
    assert row['role'] == 'optimizer' and row['input_tokens'] == 13 and row['output_tokens'] == 7
    assert report['accounting']['actual']['model_calls'] == 1 and row['cost_usd'] is None
    if not broken:
        assert execute(tmp_path/'run', config, resume=True)[0] == result  # Endpoint is already closed.


def test_adapter_exception_preserves_usage_while_stage_still_fails(tmp_path, monkeypatch):
    class Exploding(LLMHarness):
        def run_episode(self, spec):
            super().run_episode(spec)
            raise RuntimeError('post-response failure')
    with endpoint([response({'prompt_tokens': 5, 'completion_tokens': 2})]) as url:
        adapter = Exploding(key='fixture', base_url=url)
        monkeypatch.setattr(HarnessActor, 'adapter', lambda self, spec: adapter)
        result, snapshot = execute(tmp_path/'run', configured('llm'))
    assert result.status == OperationStatus.FAILED
    assert 'ActorFailed' in snapshot['actor_calls'][0]['error']
    assert snapshot['actor_calls'][0]['usage']['input_tokens'] == 5
    assert report_run(tmp_path/'run')['accounting']['actor_tokens']['reported']['output_tokens'] == 2


def test_role_token_total_keeps_unknown_instead_of_summing_only_known_calls(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCAL_FIXTURE_KEY', 'fixture')
    with endpoint([response({'prompt_tokens': 9, 'completion_tokens': 2}),
                   response({'completion_tokens': 3})]) as url:
        recipe, registry, _ = configured('llm', base_url=url, key_env='LOCAL_FIXTURE_KEY')
        recipe = replace(recipe, method=replace(recipe.method, options={**dict(recipe.method.options), 'iterations': 2}))
        result, _ = execute(tmp_path/'run', (recipe, registry, registry.make_lock(recipe, [SOURCE])))
    assert result.status == OperationStatus.COMPLETED
    accounting = report_run(tmp_path/'run')['accounting']
    assert accounting['actual']['model_calls'] == 2
    assert accounting['actor_tokens']['reported'] == {'input_tokens': None, 'output_tokens': 5.}
    assert accounting['by_role_stage'][0]['input_tokens'] is None


@pytest.mark.parametrize('name,expected', [('codex', (13, 7)), ('pi', (30, 14)), ('dsh', (24, 7))])
def test_real_coding_adapters_use_session_receipts_despite_absent_legacy_token_counters(tmp_path, monkeypatch, name, expected):
    from proteus.adapters.codex import CodexHarness
    from proteus.adapters.pi import PiHarness
    from proteus.adapters.dsh import DshHarness
    from check_native_pi_package import entries, raw as pi_raw
    from check_native_dsh_package import Sandbox as DshSandbox
    from test_native_codex_usage import raw as codex_raw, stream
    from proteus.core.native_activation import PREFIX
    class Sandbox:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            if name == 'pi':
                events = entries()
                (root/'.pi-state'/(events[0]['id']+'.jsonl')).write_bytes(pi_raw(events))
            elif name == 'dsh':
                DshSandbox().run(root, command, env, timeout_s, mounts, stop_check)
            else:
                (root/'.codex-state/sessions'/Path(command[1]).name).write_bytes(codex_raw(stream()))
            stdout = '' if name == 'codex' else PREFIX+json.dumps({
                'version': 1, 'backend': name, 'mode': 'pristine', 'source_sha256': command[1],
                'cli_sha256': 'b'*64, 'boot_sha256': 'c'*64})+'\n'
            return subprocess.CompletedProcess(command, 0, stdout, '')
    if name == 'codex':
        auth = tmp_path/'auth.json'
        auth.write_text('{}')
        adapter = CodexHarness(auth_file=auth, sandbox=Sandbox())
        def publication(active, root):
            path = root/'publication'
            path.mkdir()
            return '', path
        monkeypatch.setattr(adapter, '_ensure_validated', publication)
    elif name == 'pi':
        adapter = PiHarness(key='fixture', sandbox=Sandbox())
    else:
        pytest.importorskip('zstandard')
        adapter = DshHarness(key='fixture', sandbox=Sandbox())
    monkeypatch.setattr(adapter, 'seed', lambda root, seed: (root/'src').mkdir())
    monkeypatch.setattr(HarnessActor, 'adapter', lambda self, spec: adapter)
    result, snapshot = execute(tmp_path/'run', configured(name))
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    usage = snapshot['actor_calls'][0]['usage']
    assert (usage['input_tokens'], usage['output_tokens']) == expected
    assert usage['model_calls'] is None and usage['cost_usd'] is None
    report = report_run(tmp_path/'run')
    assert tuple(report['accounting']['actor_tokens']['reported'][key] for key in ('input_tokens', 'output_tokens')) == expected
    assert len(report['accounting']['native_receipts']) == 1


def test_legacy_adapter_without_receipts_retains_explicit_counters(tmp_path, monkeypatch):
    from proteus.adapters.minimal import MinimalHarness
    from proteus.core.adapter import EpisodeResult
    class Legacy(MinimalHarness):
        def run_episode(self, spec):
            return EpisodeResult(episode=1, ok=True, turns=1, counters={'tokens_in': 6, 'tokens_out': 2})
        def read_trace(self, *args):
            return ()
    monkeypatch.setattr(HarnessActor, 'adapter', lambda self, spec: Legacy())
    result, _ = execute(tmp_path/'run', configured())
    assert result.status == OperationStatus.COMPLETED
    report = report_run(tmp_path/'run')['accounting']
    assert report['native_receipts'] == [] and report['actual']['model_calls'] is None
    assert report['actor_tokens']['reported'] == {'input_tokens': 6., 'output_tokens': 2.}


def test_duplicate_fresh_session_across_actor_calls_is_not_exported_as_valid_usage(tmp_path, monkeypatch):
    from proteus.adapters.minimal import MinimalHarness
    from proteus.core.adapter import EpisodeResult
    from proteus.core.native_inputs import native_input
    from proteus.core.native_usage import codex_usage
    from test_native_codex_usage import raw, stream
    source = raw(stream(identity='12345678-1234-4234-8234-123456789abc'))
    class Reused(MinimalHarness):
        def run_episode(self, spec):
            with native_input(spec, backend='codex', phase='act', scope='harness_cli',
                              payload=['exec', '--json', '--ephemeral', 'fixture']) as receipt:
                receipt['usage_source'] = source
                receipt['usage'] = codex_usage(source, transport_complete=True)
            return EpisodeResult(episode=1, ok=True, turns=1)
        def read_trace(self, *args):
            return ()
    monkeypatch.setattr(HarnessActor, 'adapter', lambda self, spec: Reused())
    recipe, registry, _ = configured('codex')
    recipe = replace(recipe, method=replace(recipe.method, options={**dict(recipe.method.options), 'iterations': 2}))
    result, snapshot = execute(tmp_path/'run', (recipe, registry, registry.make_lock(recipe, [SOURCE])))
    assert result.status == OperationStatus.COMPLETED and len(snapshot['actor_calls']) == 2
    with pytest.raises(ContractError, match='reused a session'):
        report_run(tmp_path/'run')


def test_workspace_actor_keeps_file_capture_and_receipt_accounting(tmp_path):
    from proteus.adapters.minimal import MinimalHarness
    from proteus.execution.actors import HarnessOptions, WorkspaceHarnessActor
    from proteus.execution.harness_usage import verify
    from test_v1_workspace import stage_services
    class Adapter(MinimalHarness):
        def run_episode(self, spec):
            result = super().run_episode(spec)
            (spec.root/'task/output/result.txt').write_text('fixture artifact')
            return result
    class Runtime(WorkspaceHarnessActor):
        model_call_bound = 0  # This fixture uses only the bundled offline mock.
        def __init__(self):
            super().__init__('minimal', HarnessOptions())
        def adapter(self, spec):
            return Adapter()
    services, inputs = stage_services(tmp_path, factory=Runtime)
    result = services.stage('propose', inputs)
    assert result.status == OperationStatus.COMPLETED
    record = services.actor_calls[0]
    assert record['result']['workspace_artifact'] is not None and record['usage']['model_calls'] == 0
    assert len(verify(services.root, record)) == 1


@pytest.mark.parametrize('mutation', ['delete', 'tamper', 'extra', 'binding_removed', 'symbolic'])
def test_export_rejects_missing_changed_or_unbound_private_receipts(tmp_path, mutation):
    root = tmp_path/'run'
    config = configured()
    _, snapshot = execute(root, config)
    call = snapshot['actor_calls'][0]
    directory = input_directory(root/'actors'/call['id'])
    if mutation == 'delete':
        for path in directory.iterdir():
            path.unlink()
    elif mutation == 'tamper':
        next(directory.glob('*.input.json')).write_text('{}')
    elif mutation == 'extra':
        from proteus.adapters.minimal import MinimalHarness
        from proteus.core.adapter import EpisodeSpec
        adapter = MinimalHarness()
        workspace = root/'actors'/call['id']
        adapter.run_episode(EpisodeSpec(root=workspace, episode=1, model='mock', phases=('act',), phase_prompts={'act':'extra'}))
    elif mutation == 'binding_removed':
        del call['result']['runtime']['settings']['native_usage']
        (root/'execution-0001.json').write_text(json.dumps(snapshot))
    else:
        elsewhere = tmp_path/'elsewhere'
        directory.rename(elsewhere)
        directory.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ContractError):
        report_run(root)
    if mutation in ('delete', 'tamper', 'extra', 'symbolic'):
        with pytest.raises(ContractError):
            execute(root, config, resume=True)
