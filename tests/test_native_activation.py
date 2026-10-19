"""Actual source-wrapper control flow with tiny local CLIs; no upstream/model claim."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from proteus.core.adapter import EpisodeSpec
from proteus.core.native_activation import (
    FLAG, PREFIX, capture_activation, source_command, source_tree_hash,
)
from proteus.core.native_inputs import read_native_inputs
from proteus.core.serialization import ContractError

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(params=['pi', 'dsh'])
def runtime(tmp_path, request):
    if shutil.which('node') is None or shutil.which('npm') is None:
        pytest.skip('source-wrapper tests require Node and npm')
    backend = request.param
    source = tmp_path/'workspace/src'
    source.mkdir(parents=True)
    for name in ('apps', 'packages', 'vendor'):
        (source/name).mkdir()
    cli = 'packages/coding-agent/dist/cli.js' if backend == 'pi' else 'apps/cli/lib/bin.js'
    marker = tmp_path/'cli-started'
    generator = (
        "const fs=require('fs'),path=require('path');"
        f"const cli={json.dumps(cli)};"
        "fs.mkdirSync(path.dirname(cli),{recursive:true});"
        f"fs.writeFileSync(cli,\"require('fs').writeFileSync(\"+JSON.stringify({json.dumps(str(marker))})+"
        "\",'started');console.log(JSON.stringify(process.argv.slice(2)));\");"
    )
    (source/'generate.cjs').write_text(generator)
    (source/'package.json').write_text(json.dumps({'scripts': {
        'build:offline': 'node generate.cjs', 'build:lib': 'node generate.cjs'}}))
    (source/'packages/source.txt').write_text('pristine')
    original = REPO/f'environments/{backend}-src/boot.sh'
    boot = tmp_path/'boot.sh'
    text = original.read_text()
    for directory in ('workspace', 'opt', 'state'):
        text = text.replace('/'+directory, str(tmp_path/directory))
    boot.write_text(text)
    (tmp_path/'opt').mkdir()
    (tmp_path/'state').mkdir()
    shutil.copytree(source, tmp_path/'opt/src')
    subprocess.run(['node', 'generate.cjs'], cwd=tmp_path/'opt/src', check=True)
    (tmp_path/'opt/pristine-hash').write_text(source_tree_hash(source))
    if backend == 'dsh':
        digest = subprocess.check_output(['sh', str(boot), '--proteus-dependency-hash', str(source)], text=True)
        (tmp_path/'opt/pristine-dependency-hash').write_text(digest.strip())
    return backend, tmp_path, source, boot, tmp_path/'opt/src'/cli, marker


def boot_run(runtime, expected=None):
    _, _, source, boot, _, _ = runtime
    return subprocess.run(['sh', str(boot), FLAG, expected or source_tree_hash(source), 'CLI-ARG'],
                          capture_output=True, text=True, timeout=30)


def test_actual_wrappers_record_pristine_build_and_cache(runtime):
    backend, _, source, boot, cli, marker = runtime
    for mode in ('pristine', 'build', 'cache'):
        if mode == 'build':
            (source/'packages/source.txt').write_text('edited')
        result = boot_run(runtime)
        assert result.returncode == 0, result.stderr
        receipt = capture_activation(result.stdout, [FLAG, source_tree_hash(source), 'CLI-ARG'], backend)
        assert receipt == {'version': 1, 'backend': backend, 'mode': mode,
            'source_sha256': source_tree_hash(source),
            'cli_sha256': hashlib.sha256(cli.read_bytes()).hexdigest(),
            'boot_sha256': hashlib.sha256(boot.read_bytes()).hexdigest()}
        assert result.stdout.splitlines()[1] == '["CLI-ARG"]'
        assert marker.read_text() == 'started'
        marker.unlink()


@pytest.mark.parametrize('failure', ['mismatch', 'missing-src', 'missing-project'])
def test_actual_wrappers_refuse_fallback_before_cli_starts(runtime, failure):
    backend, _, source, _, _, marker = runtime
    expected = source_tree_hash(source)
    if failure == 'mismatch':
        expected = '0'*64
    elif failure == 'missing-src':
        shutil.rmtree(source)
    else:
        shutil.rmtree(source/('packages' if backend == 'pi' else 'apps'))
    result = boot_run(runtime, expected)
    assert result.returncode == 97 and not marker.exists()
    assert PREFIX not in result.stdout


def test_hash_matches_both_actual_node_wrappers(tmp_path):
    source = tmp_path/'source'
    source.mkdir()
    (source/'é.txt').write_bytes(b'a\0b')
    (source/'empty').touch()
    (source/'exec').write_bytes(b'#!/bin/sh\n')
    (source/'exec').chmod(0o755)
    (source/'link').symlink_to('missing')
    (source/'node_modules').mkdir()
    (source/'node_modules/ignored').write_text('excluded')
    if shutil.which('node') is None:
        pytest.skip('wrapper hash parity requires Node')
    for backend in ('pi', 'dsh'):
        digest = subprocess.check_output(['sh', str(REPO/f'environments/{backend}-src/boot.sh'),
                                          '--proteus-tree-hash', str(source)], text=True).strip()
        assert digest == source_tree_hash(source)


@pytest.mark.parametrize('failure', ['missing', 'backend', 'source', 'mode', 'version', 'extra', 'prefix'])
def test_startup_receipts_fail_closed(failure):
    value = {'version': 1, 'backend': 'pi', 'mode': 'pristine',
             'source_sha256': 'a'*64, 'cli_sha256': 'b'*64, 'boot_sha256': 'c'*64}
    if failure in ('backend', 'mode', 'version'):
        value[failure] = {'backend': 'dsh', 'mode': 'later-check', 'version': True}[failure]
    elif failure == 'source':
        value['source_sha256'] = 'd'*64
    elif failure == 'extra':
        value['unbound'] = 1
    stdout = PREFIX+json.dumps(value)+'\n'
    if failure == 'missing':
        stdout = ''
    elif failure == 'prefix':
        stdout = 'untrusted CLI text\n'+stdout
    with pytest.raises((ContractError, ValueError)):
        capture_activation(stdout, [FLAG, 'a'*64, '-p', 'input'], 'pi')


@pytest.mark.parametrize('exit_code', [0, 137])
def test_adapter_persists_actual_wrapper_receipt(runtime, exit_code):
    from proteus.adapters.dsh import DshHarness
    from proteus.adapters.pi import PiHarness
    backend, root, _, boot, cli, _ = runtime
    with cli.open('a') as stream:
        stream.write(f'process.exit({exit_code});')
    shutil.copytree(root/'workspace', root/'harness')

    class Sandbox:
        def run(self, run_root, command, **kwargs):
            return subprocess.run(['sh', str(boot), *command], capture_output=True,
                                  text=True, timeout=30)

    adapter = (PiHarness if backend == 'pi' else DshHarness)(key='fixture', sandbox=Sandbox())
    spec = EpisodeSpec(root=root, active_root=root/'workspace', episode=1, model='fixture',
                       phases=('act',), phase_prompts={'act': 'input'})
    assert adapter.run_episode(spec).ok == (exit_code == 0)
    rows = read_native_inputs(root)
    assert len(rows) == 1 and rows[0]['outcome']['activation']['backend'] == backend
    assert rows[0]['outcome']['activation']['source_sha256'] == source_tree_hash(root/'workspace/src')
    assert rows[0]['input']['payload'][0] == FLAG
    assert rows[0]['outcome']['returncode'] == exit_code


def test_staged_missing_source_cannot_dispatch(tmp_path):
    spec = EpisodeSpec(root=tmp_path, active_root=tmp_path, episode=1, model='fixture', phase_prompts={})
    with pytest.raises(ContractError, match='real directory'):
        source_command(spec, tmp_path, ['-p', 'input'])
    from proteus.adapters.dsh import DshHarness
    from proteus.adapters.pi import PiHarness
    for cls in (PiHarness, DshHarness):
        assert 'source surface is missing' in cls(key='fixture').check_boot(tmp_path)


@pytest.mark.parametrize('backend', ['pi', 'dsh'])
def test_old_source_image_cannot_silently_succeed(tmp_path, backend):
    from proteus.adapters.dsh import DshHarness
    from proteus.adapters.pi import PiHarness
    (tmp_path/'active/src').mkdir(parents=True)
    (tmp_path/'harness').mkdir()

    class OldImage:
        def run(self, root, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, 'CLI output without startup receipt', '')

    adapter = (PiHarness if backend == 'pi' else DshHarness)(key='fixture', sandbox=OldImage())
    spec = EpisodeSpec(root=tmp_path, active_root=tmp_path/'active', episode=1, model='fixture',
                       phases=('act',), phase_prompts={'act': 'input'})
    with pytest.raises(ContractError, match='rebuild the source image'):
        adapter.run_episode(spec)
    row, = read_native_inputs(tmp_path)
    assert row['completion'] == 'raised' and 'activation' not in row['outcome']
