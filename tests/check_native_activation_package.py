"""Installed-package source command/receipt roundtrip; no CLI/model acceptance claim."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from proteus.core.native_activation import PREFIX, capture_activation, source_command, source_tree_hash
from proteus.core.native_inputs import native_input, input_directory, read_native_inputs
from proteus.core.native_usage import dsh_usage, pi_usage
from proteus.core.serialization import ContractError, content_hash


def check():
    with tempfile.TemporaryDirectory(prefix='proteus-activation-package-') as directory:
        for backend in ('pi', 'dsh'):
            root = Path(directory)/backend
            source = root/'active/src'
            source.mkdir(parents=True)
            (source/'entry.js').write_text('fixture')
            spec = SimpleNamespace(root=root, active_root=root/'active', episode=1)
            argv = ['--profile', 'headless', 'input'] if backend == 'dsh' else ['--session-dir', '/state', '-p', 'input']
            command = source_command(spec, spec.active_root, argv)
            value = {'version': 1, 'backend': backend, 'mode': 'cache',
                'source_sha256': source_tree_hash(source), 'cli_sha256': 'b'*64, 'boot_sha256': 'c'*64}
            with native_input(spec, backend=backend, phase='act', scope='harness_cli', payload=command) as receipt:
                receipt['returncode'] = 0
                receipt['activation'] = capture_activation(PREFIX+json.dumps(value)+'\n', command, backend)
                receipt['usage'] = (pi_usage if backend == 'pi' else dsh_usage)(None, transport_complete=True)
            rows = read_native_inputs(root)
            assert len(rows) == 1 and rows[0]['outcome']['activation'] == value
            path = next(input_directory(root).glob('*.outcome.json'))
            saved = json.loads(path.read_text())
            del saved['activation']
            saved['hash'] = content_hash({k:v for k,v in saved.items() if k != 'hash'})
            path.write_text(json.dumps(saved))
            try:
                read_native_inputs(root)
            except ContractError as exc:
                assert 'lacks activation evidence' in str(exc)
            else:
                raise AssertionError('missing source activation accepted')
    print('installed Pi/DSH startup receipt roundtrip and missing-evidence rejection passed')


if __name__ == '__main__':
    check()
