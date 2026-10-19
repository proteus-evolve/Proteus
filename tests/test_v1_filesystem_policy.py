"""Configuration cannot claim scopes without the matching kernel enforcement."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from proteus.execution import filesystem_policy as fs
from proteus.core.serialization import ContractError
from test_v1_harbor_code import LIVE, configured
from check_filesystem_policy_package import kernel_check


@pytest.mark.parametrize('read,write', [
    (('relative',), ()), (('/tmp/../etc',), ()), (('//etc',), ()), (('/tmp/',), ()),
    (('/tmp//a',), ()), (('/tmp/./a',), ()), (('/tmp\x00',), ()), ((), ('/tmp',)),
    (('/tmp', '/tmp'), ()), (('/usr',), ('/tmp/../opt',)),
])
def test_invalid_scopes_fail_during_resolution(tmp_path, read, write):
    registry, recipe = configured(tmp_path)
    with pytest.raises(ValueError):
        env = replace(recipe.execution.environment, read_scopes=read, write_scopes=write)
        registry.resolve(replace(recipe, execution=replace(recipe.execution, environment=env)))


@pytest.mark.parametrize('pids', [True, 7, 0, -1, 32.5, '32'])
def test_invalid_pid_limit_fails_before_dispatch(tmp_path, pids):
    registry, recipe = configured(tmp_path)
    env = replace(recipe.execution.environment, resources={**dict(recipe.execution.environment.resources), 'pids': pids})
    with pytest.raises(ContractError, match='pids'):
        registry.resolve(replace(recipe, execution=replace(recipe.execution, environment=env)))


def test_old_empty_scopes_keep_legacy_policy_and_unknown_resources_are_rejected(tmp_path):
    registry, recipe = configured(tmp_path)
    assert fs.policy((), ()) is None
    registry.resolve(recipe)
    env = replace(recipe.execution.environment, resources={**dict(recipe.execution.environment.resources), 'gpu': 1})
    with pytest.raises(ValueError, match='resource'):
        registry.resolve(replace(recipe, execution=replace(recipe.execution, environment=env)))


@pytest.mark.parametrize('change', [{'abi': 2}, {'abi': True}, {'handled_access_fs': 0}, {'uid': 0},
                                  {'gid': 0}, {'policy': None}, {'backend': 'unrestricted'}, {'extra': True}])
def test_incomplete_or_different_bootstrap_proof_is_rejected(change):
    policy = fs.policy(('/usr',), ())
    receipt = {'version': 1, 'backend': 'landlock', 'abi': 3, 'handled_access_fs': fs.HANDLED,
               'policy': policy, 'uid': 65534, 'gid': 65534}
    assert fs.validate_receipt(receipt, policy, uid=65534, gid=65534) == receipt
    with pytest.raises(ValueError, match='receipt'):
        fs.validate_receipt({**receipt, **change}, policy, uid=65534, gid=65534)


def test_unsupported_platform_refuses_before_any_application_launch(tmp_path):
    import base64
    marker = tmp_path/'launched'
    policy = fs.policy(('/usr',), ())
    code = f'''import runpy,sys
sys.platform='unsupported-test-platform'
sys.argv=[{fs.__file__!r},{base64.b64encode(json.dumps(policy).encode()).decode()!r},'--',
          {sys.executable!r},'-c',"from pathlib import Path; Path({str(marker)!r}).touch()"]
runpy.run_path({fs.__file__!r},run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and 'require Linux' in result.stderr and not result.stdout
    assert not marker.exists()


@pytest.mark.skipif(sys.platform != 'linux', reason='actual Landlock kernel enforcement requires Linux; mandatory in Linux CI')
def test_kernel_denies_outside_reads_writes_truncation_and_child_escape(tmp_path):
    assert kernel_check(tmp_path)['abi'] >= 3


def test_standalone_helper_has_no_proteus_or_harbor_import(tmp_path):
    code = f'import runpy,sys;runpy.run_path({fs.__file__!r},run_name="import_only");assert "proteus" not in sys.modules and "harbor" not in sys.modules'
    subprocess.run([sys.executable, '-I', '-c', code], cwd=tmp_path, check=True, timeout=10)
    assert Path(fs.__file__).is_file()


@LIVE
def test_live_harbor_filesystem_scopes_and_pid_resource_survive_sequence_and_resume(tmp_path):
    import os
    from proteus.artifacts.codecs import FileContent
    from proteus.artifacts.store import ArtifactStore
    from proteus.core.contracts import OperationStatus, PluginRef
    from proteus.examples.code_memory import SOURCE, register
    from proteus.examples.harbor_code_memory import prepare
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.runner import run_recipe
    from test_v1_evaluation import SOURCE as SOURCE_PIN
    from test_v1_harbor_code import evidence
    from test_v1_ledger import run_ledger
    root = Path(os.environ.get('PROTEUS_HARBOR_STUDY', str(tmp_path)))/'filesystem-scopes'
    recipe = load_recipe(prepare(root/'study', os.environ['PROTEUS_HARBOR_PYTHON']))
    checks = '''import os, subprocess, sys
from pathlib import Path
assert os.getuid() == 65534
assert not Path('previous-step').exists()
Path('previous-step').write_text('allowed')
Path('previous-step').rename('renamed-step')
assert Path('renamed-step').read_text() == 'allowed'
Path('renamed-step').unlink()
Path('previous-step').write_text('must not survive the sequence step')
for operation in (lambda: Path('/etc/hostname').read_text(),
                  lambda: Path('/tmp/proteus-denied').write_text('not allowed')):
    try: operation()
    except PermissionError: pass
    else: raise AssertionError('filesystem scope escaped')
child = subprocess.run([sys.executable, '-I', '-c', "open('/etc/hostname').read()"], capture_output=True, text=True)
assert child.returncode != 0 and 'PermissionError' in child.stderr
'''
    registry = builtin_registry()
    register(registry)
    store = ArtifactStore(root/'study/artifact-store', registry)
    artifact = store.put(PluginRef(name='proteus.file_tree', version='1'), {'candidate.py': FileContent((checks+SOURCE).encode())})
    env = replace(recipe.execution.environment, resources={'cpus': 1, 'memory_mb': 512, 'pids': 32},
                  read_scopes=('/usr', '/opt/proteus'), write_scopes=('/opt/proteus/candidate',))
    recipe = registry.resolve(replace(recipe, subject=replace(recipe.subject, initial_artifact=artifact),
                                      execution=replace(recipe.execution, environment=env)))
    lock = registry.make_lock(recipe, [SOURCE_PIN])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root/'run', artifact_source=store.root)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    snapshot = json.loads((root/'run/execution-0001.json').read_text())
    assert [r['output'] for r in snapshot['rollouts']] == [2, 3, 4, 6, 14]
    paths = evidence(root/'run', 6)
    identities = set()
    for path in paths:
        request, loaded = json.loads(path.read_text()), json.loads(path.with_name('loaded.json').read_text())
        assert request['max_pids'] == loaded['pids_limit'] == 32
        assert loaded['filesystem_helper_hash'] == request['filesystem_helper_hash']
        fs.validate_receipt(loaded['filesystem_policy'], fs.policy(env.read_scopes, env.write_scopes), uid=65534, gid=65534)
        identities.add(loaded['container_id'])
    assert len(identities) == 6 and len(snapshot['actor_calls']) == 7
    assert run_recipe(recipe, registry=registry, lock=lock, root=root/'run', resume=True) == result
    assert json.loads((root/'run/execution-0002.json').read_text())['actor_calls'] == snapshot['actor_calls']
    assert run_ledger(root/'run').balance()['active_slots'] == 0
