"""Coding-workspace disclosures, real file proposals, adversarial outputs and recovery."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import OperationStatus, PluginRef
from proteus.core.methods import ActorResult, RunServices
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.serialization import ContractError, content_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.coding_workspace import FixtureFileActor, prepare, register
from proteus.execution.actors import HarnessOptions, WorkspaceHarnessActor, WORKSPACE_INSTRUCTIONS
from proteus.recipes import load_recipe
from proteus.recipes.builtins import Text, builtin_registry
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE


def configured(root):
    path = root / 'study/recipe.json'
    if not path.exists():
        prepare(root / 'study')
    registry = builtin_registry()
    register(registry)
    return registry, load_recipe(path)


def run(root, *, registry=None, recipe=None, resume=False):
    if registry is None:
        registry, recipe = configured(root)
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root/'run', resume=resume,
                        artifact_source=root/'study/artifact-store')
    return result, json.loads(sorted((root/'run').glob('execution-*.json'))[-1].read_text())


def stage_services(root, factory=FixtureFileActor, capabilities=('actor.workspace',), *, record_failure=False,
                   stage_options=None):
    registry, recipe = configured(root)
    registry.register(Plugin(ref=PluginRef(name='fixture.coder', version='1'), kind='actor',
        options=EmptyOptions, factory=lambda options: factory(), implementation_hash=content_hash('fixture-coder'),
        source='proteus', capabilities=('session.fresh', 'actor.execute', 'calls.bounded', *capabilities)))
    recipe = registry.resolve(replace(recipe, actors={'proposer': replace(recipe.actors['proposer'],
        runtime=plugin('fixture.coder'))}, stages=(replace(recipe.stages[0],
        failure_policy='record' if record_failure else 'stop',
        operator=plugin('proteus.workspace', **(stage_options or {}))),)))
    services = RunServices(run_id='run', recipe=recipe, registry=registry, root=root/'run')
    source = ArtifactStore(root/'study/artifact-store', registry)
    services.artifacts.copy_from(source, recipe.subject.initial_artifact)
    first = services.initial_candidate()
    delta = services.artifacts._codec(first.artifact.codec).delta_schema.from_dict(recipe.method.options['variants'][0]['delta'])
    second = services.fork_candidate(first, candidate_id='second', delta=delta)
    for candidate in (first, second):
        services.evaluate(candidate, 'validation')
    history = services.evidence_view('propose')
    inputs = {'prompt': services.values.publish('proteus.text@1', Text(text='fixture')),
        'seed': services.values.publish('proteus.artifact_ref@1', first.artifact), 'history': history}
    return services, inputs


def test_file_proposer_reads_multiple_codes_returns_three_candidates_and_resumes(tmp_path):
    result, records = run(tmp_path)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert len(records['candidates']) == 5 and len(records['rollouts']) == 12
    assert [row['output'] for row in records['rollouts'][-6:]] == [4,6,6,9,8,12]
    assert [len(c['parent_ids']) for c in records['candidates'][-3:]] == [2,2,2]
    calls = records['actor_calls']
    assert len(calls) == 1 and calls[0]['usage']['model_calls'] == 0
    assert len(calls[0]['result']['trace']) == 3
    assert calls[0]['result']['workspace_artifact'] is not None
    workspaces = list((tmp_path/'run/actors').glob('*/task'))
    assert len(workspaces) == 1
    view = json.loads((workspaces[0]/'history/view.json').read_text())
    assert view['candidate_ids'] == ['initial', 'doubled']
    assert all(entry['split'] != 'test' for entry in view['entries'])
    assert not any(p.name in ('private','ledger.sqlite3') for p in workspaces[0].rglob('*'))
    # Settled output is owned by the artifact store, not by mutable runtime files.
    shutil.rmtree(workspaces[0])
    resumed, records = run(tmp_path, resume=True)
    assert resumed == result and len(records['actor_calls']) == 1


@pytest.mark.parametrize('fault', ['seed', 'history', 'symlink', 'oversized', 'too_many', 'empty', 'forged'])
def test_invalid_proposer_files_fail_without_publishing_an_output(tmp_path, fault):
    class BadActor(FixtureFileActor):
        def execute(self, spec, request, workspace):
            result = super().execute(spec, request, workspace)
            task = workspace/'task'
            if fault == 'seed':
                (task/'input/main.py').write_text('changed')
            elif fault == 'history':
                (task/'history/view.json').chmod(0o644)
                (task/'history/view.json').write_text('{}')
            elif fault == 'symlink':
                (task/'output/leak').symlink_to(task/'input/main.py')
            elif fault == 'oversized':
                (task/'output/big').write_bytes(b'x' * (request.workspace.max_output_bytes + 1))
            elif fault == 'too_many':
                for i in range(request.workspace.max_output_files):
                    (task/'output'/str(i)).write_text('x')
            elif fault == 'empty':
                shutil.rmtree(task/'output')
                (task/'output').mkdir()
            elif fault == 'forged':
                return replace(result, workspace_artifact=request.workspace.seed)
            return result
    services, inputs = stage_services(tmp_path, BadActor, record_failure=True,
        stage_options={'max_output_bytes':4096, 'max_output_files':8})
    result = services.stage('propose', inputs)
    assert result.status == OperationStatus.FAILED and not result.outputs
    assert len(services.actor_calls) == 1
    assert services.ledger.balance()['active_slots'] == 0
    assert services.actor_calls[0]['error']


def test_workspace_capability_and_grant_are_checked_before_dispatch(tmp_path):
    with pytest.raises(ContractError, match='workspace-capable'):
        stage_services(tmp_path/'capability', capabilities=())
    services, inputs = stage_services(tmp_path/'grant')
    from proteus.execution.workspace import WorkspaceService
    from proteus.execution.workspace_records import WorkspaceInput
    from proteus.core.contracts import StageRequest
    stage = services.recipe.stages[0]
    seed = services.values.get(inputs['seed'])
    request = WorkspaceInput(seed=seed, history=inputs['history'])
    operation = StageRequest(run_id='run', operation_id='attempt', attempt_id='attempt-1', stage=stage.operator.plugin,
        role=stage.role, inputs={'history':inputs['history']})
    with pytest.raises(ContractError, match='seed was not supplied'):
        WorkspaceService(services).authorize(stage, operation, request)
    operation = replace(operation, inputs={'seed':inputs['seed']})
    with pytest.raises(ContractError, match='history was not supplied'):
        WorkspaceService(services).authorize(stage, operation, request)
    assert not services.actor_calls


def test_failed_actor_and_missing_settled_artifact_are_not_success(tmp_path):
    class Failed(FixtureFileActor):
        def execute(self, spec, request, workspace):
            return replace(super().execute(spec, request, workspace), status=OperationStatus.FAILED, diagnostic='fixture failure')
    services, inputs = stage_services(tmp_path/'failed', Failed, record_failure=True)
    result = services.stage('propose', inputs)
    assert result.status == OperationStatus.FAILED
    assert services.actor_calls[0]['result']['workspace_artifact'] is None
    result, records = run(tmp_path/'complete')
    assert result.status == OperationStatus.COMPLETED
    artifact = ActorResult.from_dict(records['actor_calls'][0]['result']).workspace_artifact
    registry, recipe = configured(tmp_path/'complete')
    store = ArtifactStore(tmp_path/'complete/run/artifacts', registry)
    store._path('blobs', artifact.components[0].content_hash).unlink()
    with pytest.raises((FileNotFoundError, ContractError)):
        run(tmp_path/'complete', resume=True)


@pytest.mark.parametrize('name', ['dsh', 'pi', 'codex'])
def test_container_actor_uses_neutral_instructions_and_readonly_task_mounts(tmp_path, name):
    from proteus.core.adapter import EpisodeResult
    from proteus.adapters.dsh import DshHarness
    from proteus.adapters.pi import PiHarness
    from proteus.adapters.codex import CodexHarness
    from proteus.core.methods import ActorRequest
    from proteus.execution.workspace import WorkspaceService
    from proteus.execution.workspace_records import WorkspaceInput
    services, inputs = stage_services(tmp_path)
    workspace = tmp_path/'adapter'
    workspace.mkdir()
    request = WorkspaceInput(seed=services.values.get(inputs['seed']), history=inputs['history'])
    WorkspaceService(services).prepare(request, workspace)
    observed = []
    class Adapter:
        supports_custom_phases = staged_activation = True
        def seed(self, harness, seed):
            (harness/'AGENTS.md').write_text('native self-evolution instructions')
        def run_episode(self, spec):
            observed.append(spec)
            assert (spec.active_root/'AGENTS.md').read_text() == WORKSPACE_INSTRUCTIONS
            assert spec.phases == ('act',) and spec.readonly_task_paths == ('input','history')
            write = spec.root/'task/output/done.txt'
            write.write_text('artifact')
            return EpisodeResult(episode=1, ok=True, turns=1)
        def read_trace(self, *args):
            return ()
    actor = WorkspaceHarnessActor(name, HarnessOptions())
    actor.adapter = lambda spec: Adapter()
    spec = replace(services.recipe.actors['proposer'], runtime=plugin('proteus.workspace.'+name), model='fixture-model')
    result = actor.execute(spec, ActorRequest(prompt='method-specific request',workspace=request), workspace)
    assert result.status == OperationStatus.COMPLETED and result.usage.model_calls is None
    assert observed[0].phase_prompts == {'act':'method-specific request'}
    adapter = {'dsh':DshHarness,'pi':PiHarness,'codex':CodexHarness}[name]
    mounts = adapter._task_mount(workspace, ('input','history'))
    assert mounts == ((str(workspace/'task'),'/workspace/task'),
        (str(workspace/'task/input'),'/workspace/task/input','ro'),
        (str(workspace/'task/history'),'/workspace/task/history','ro'))


@pytest.mark.parametrize('options,match', [
    ({}, 'immutable image'),
    ({'image':'example:latest'}, 'image pins'),
    ({'image':'example@sha256:'+'a'*64}, 'none or bridge'),
    ({'image':'example@sha256:'+'a'*64,'network':'bridge'}, 'phase timeout'),
])
def test_live_workspace_runtime_requires_explicit_image_network_and_deadline(tmp_path, options, match):
    registry, recipe = configured(tmp_path)
    actor = replace(recipe.actors['proposer'], runtime=plugin('proteus.workspace.pi', **options), model='fixture')
    with pytest.raises(ContractError, match=match):
        registry.resolve(replace(recipe, actors={'proposer':actor}))


def test_readonly_task_mount_paths_cannot_escape_or_follow_links(tmp_path):
    from proteus.core.adapter import task_workspace_mounts
    (tmp_path/'task/input').mkdir(parents=True)
    for name in ('../private','/private','input/x','input:rw','input\\x','.',''):
        with pytest.raises(ValueError):
            task_workspace_mounts(tmp_path,(name,))
    (tmp_path/'task/link').symlink_to(tmp_path/'task/input',target_is_directory=True)
    with pytest.raises(ValueError,match='real directory'):
        task_workspace_mounts(tmp_path,('link',))


def test_dsh_workspace_binds_requested_model_without_changing_legacy_selection(tmp_path, monkeypatch):
    from proteus.adapters.dsh import DshHarness
    from proteus.core.adapter import EpisodeSpec
    from proteus.execution.actors import HarnessActor

    _, recipe = configured(tmp_path)
    spec = replace(recipe.actors['proposer'], runtime=plugin('proteus.workspace.dsh'),
                   model='model-with-"quoted"-id')
    options = HarnessOptions(image='example@sha256:'+'a'*64, network='none', phase_timeout_seconds=30)
    seen = []

    def factory(**kwargs):
        seen.append(kwargs)
        return kwargs

    monkeypatch.setattr('proteus.execution.actors.harness_class', lambda name: factory)
    WorkspaceHarnessActor('dsh', options).adapter(spec)
    assert seen[-1]['model'] == spec.model
    HarnessActor('dsh', options).adapter(spec)
    assert 'model' not in seen[-1]

    class Container:
        def __init__(self, expected):
            self.expected, self.calls = expected, 0

        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            self.calls += 1
            selection_mounts = [m for m in mounts if m[1] == '/state/settings.yaml']
            if self.expected is None:
                assert selection_mounts == []
                assert not (root/'.dsh-state/settings.yaml').exists()
            else:
                assert selection_mounts == [(str(root/'.dsh-state/settings.yaml'), '/state/settings.yaml', 'ro')]
                settings = json.loads(Path(selection_mounts[0][0]).read_text())
                assert settings == {'agent-default-model': {'provider':'deepseek-official', 'model':self.expected}}
            from proteus.core.native_activation import FLAG, PREFIX, source_tree_hash
            assert command[:2] == [FLAG, source_tree_hash(root/'active/src')]
            assert command[2:4] == ['--profile', 'headless'] and len(command) == 5
            stdout = PREFIX+json.dumps({'version': 1, 'backend': 'dsh', 'mode': 'pristine',
                'source_sha256': command[1], 'cli_sha256': 'b'*64, 'boot_sha256': 'c'*64})+'\n'
            return subprocess.CompletedProcess(command, 0, stdout, '')

    for index, model in enumerate((None, spec.model)):
        root = tmp_path/str(index)
        (root/'active').mkdir(parents=True)
        (root/'active/src/apps').mkdir(parents=True)
        (root/'active/src/apps/fixture.js').write_text('// source fixture\n')
        (root/'harness').mkdir()
        sandbox = Container(model)
        adapter = DshHarness(key='fixture', sandbox=sandbox, model=model)
        result = adapter.run_episode(EpisodeSpec(root=root, episode=1, model=spec.model,
            phases=('act',), phase_prompts={'act':'fixture'}, active_root=root/'active'))
        assert result.ok and sandbox.calls == 1


@pytest.mark.skipif(not os.environ.get('PROTEUS_HARBOR_PYTHON'), reason='requires the real-Docker CI environment')
def test_live_workspace_docker_readonly_history_multi_candidate_capture_and_resume(tmp_path, monkeypatch):
    """Actual mounts and runtime bridge, using a deterministic process instead of a model."""
    import inspect
    from proteus.core.adapter import EpisodeResult, task_workspace_mounts
    from proteus.examples.coding_workspace import write_fixture
    from proteus.examples.harbor_evaluation import IMAGE
    from proteus.sandbox.docker import DockerSandbox, SandboxConfig
    root = Path(os.environ.get('PROTEUS_HARBOR_STUDY', str(tmp_path))) / 'coding-workspace'
    registry, recipe = configured(root)
    canary = root/'private-host-file'
    canary.write_text('must stay outside the proposer')
    monkeypatch.setenv('PROTEUS_WORKSPACE_SECRET', 'must-not-enter-fixture')
    script = 'import json,os\nfrom pathlib import Path\n' + inspect.getsource(write_fixture) + '\n'
    script += f'''
task=Path('/workspace/task')
assert not Path({str(canary)!r}).exists()
assert 'PROTEUS_WORKSPACE_SECRET' not in os.environ
assert not Path('/var/run/docker.sock').exists()
denied=[]
for path in (task/'input/main.py', task/'history/view.json'):
    try:
        path.write_text('tamper')
    except OSError as error:
        assert error.errno == 30,repr(error)
        denied.append(str(path))
    else:
        raise AssertionError('input is writable')
trace=write_fixture(task)
(task/'output/container-proof.json').write_text(json.dumps({{'denied':denied,'reads':list(trace)}}))
'''
    class ContainerAdapter:
        supports_custom_phases = staged_activation = True
        def seed(self, harness, seed):
            (harness/'AGENTS.md').write_text('unused native fixture instructions')
        def run_episode(self, spec):
            sandbox = DockerSandbox(SandboxConfig(image=IMAGE, network='none', cpus='1', mem_limit='512m',
                user=f'{os.getuid()}:{os.getgid()}', env_passthrough=(), workdir='/workspace/task'))
            process = sandbox.run(spec.root, ['python3','-I','-c',script], env={}, timeout_s=30,
                mounts=task_workspace_mounts(spec.root,spec.readonly_task_paths))
            name = process.args[process.args.index('--name')+1]
            remaining = subprocess.run(['docker','ps','-aq','--filter','name=^/'+name+'$'],
                capture_output=True,text=True,check=True,timeout=20).stdout.strip()
            assert not remaining
            (root/'workspace-docker-cleanup.json').write_text(json.dumps({'name':name,'remaining':remaining,
                'returncode':process.returncode,'image':IMAGE,'mounts':task_workspace_mounts(spec.root,spec.readonly_task_paths)}))
            return EpisodeResult(episode=1,ok=process.returncode==0,turns=0,error=process.stderr)
        def read_trace(self,*args):
            return ()
    def factory(options):
        actor = WorkspaceHarnessActor('pi',HarnessOptions())
        actor.adapter = lambda spec: ContainerAdapter()
        return actor
    registry.register(Plugin(ref=PluginRef(name='fixture.docker_workspace',version='1'),kind='actor',
        options=EmptyOptions,factory=factory,implementation_hash=content_hash('docker-workspace-fixture'),source='proteus',
        capabilities=('session.fresh','actor.execute','actor.workspace')))
    recipe = replace(recipe,actors={'proposer':replace(recipe.actors['proposer'],runtime=plugin('fixture.docker_workspace'))})
    result, records = run(root,registry=registry,recipe=recipe)
    assert result.status == OperationStatus.COMPLETED,result.diagnostic
    output = ActorResult.from_dict(records['actor_calls'][0]['result']).workspace_artifact
    proof = json.loads(ArtifactStore(root/'run/artifacts',registry).files(output)['container-proof.json'].data)
    assert len(proof['denied'])==2 and len(proof['reads'])==3
    assert len(records['candidates'])==5 and len(records['rollouts'])==12
    # Opaque native harness usage remains unknown, including this fixture wrapper.
    assert records['actor_calls'][0]['usage']['model_calls'] is None
    resumed, again = run(root,registry=registry,recipe=recipe,resume=True)
    assert resumed == result and len(again['actor_calls']) == 1


WORKER = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_workspace import run, configured
from proteus.examples.coding_workspace import FixtureFileActor
from proteus.core.ledger import Ledger
from proteus.execution.workspace import WorkspaceService
root, boundary, resume = Path(sys.argv[1]),sys.argv[2],sys.argv[3]=='resume'
root.mkdir(parents=True,exist_ok=True)
original = FixtureFileActor.execute
def counted(self,*args):
    with (root/'calls').open('a') as stream: stream.write('call\n')
    result = original(self,*args)
    if not resume and boundary=='uncertain': os._exit(86)
    return result
FixtureFileActor.execute=counted
if not resume:
    if boundary=='reserved':
        original_prepare=WorkspaceService.prepare
        def interrupted(self,*args,**kwargs):
            original_prepare(self,*args,**kwargs)
            os._exit(86)
        WorkspaceService.prepare=interrupted
    elif boundary=='settled':
        original_settle=Ledger.settle
        def interrupted(self,entry_id,*args,**kwargs):
            result=original_settle(self,entry_id,*args,**kwargs)
            if self.entry(entry_id)['identity']['metadata']['kind']=='actor': os._exit(86)
            return result
        Ledger.settle=interrupted
result,records=run(root,resume=resume)
(root/'result.json').write_text(result.to_json())
'''


@pytest.mark.parametrize('boundary', ['reserved','settled','uncertain'])
def test_hard_exit_recovery_does_not_regenerate_settled_or_uncertain_proposals(tmp_path, boundary):
    command = [sys.executable,'-c',WORKER,str(tmp_path),boundary]
    first = subprocess.run([*command,'start'],capture_output=True,text=True,timeout=30)
    assert first.returncode == 86, first.stdout+first.stderr
    assert (tmp_path/'calls').exists() == (boundary != 'reserved')
    second = subprocess.run([*command,'resume'],capture_output=True,text=True,timeout=30)
    assert second.returncode == 0, second.stdout+second.stderr
    result = json.loads((tmp_path/'result.json').read_text())
    assert (tmp_path/'calls').read_text().splitlines() == ['call']
    if boundary == 'uncertain':
        assert result['status']=='failed' and 'RecoveryRequired' in result['diagnostic']
    else:
        assert result['status']=='completed',result['diagnostic']
    third = subprocess.run([*command,'resume'],capture_output=True,text=True,timeout=30)
    assert third.returncode == 0,third.stderr
    assert (tmp_path/'calls').read_text().splitlines() == ['call']
