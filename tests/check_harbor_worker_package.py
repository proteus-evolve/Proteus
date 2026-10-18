"""Installed optional Harbor worker declaration and bundled standalone helpers."""

from pathlib import Path
import subprocess
import sys
import tempfile

from proteus.core.contracts import PluginRef
from proteus.core.serialization import content_hash
from proteus.recipes.builtins import builtin_registry
from proteus.workers.harbor import HarborWorkerOptions, HarborWorkerRuntime
from proteus.workers.records import WorkerIdentity, WorkerLaunch


def check():
    registry = builtin_registry()
    registered = registry.get(PluginRef(name='proteus.worker.harbor', version='1'), 'worker')
    assert 'worker.container_isolation' in registered.capabilities
    options = HarborWorkerOptions(python=sys.executable, runtime_hash=content_hash('fixture'),
        image_digest='python@sha256:'+'0'*64, command=('/usr/local/bin/python3', '/workspace/provider.py'),
        command_files={'/usr/local/bin/python3': content_hash('fixture')}, user='1001:1001')
    assert HarborWorkerOptions.from_json(options.to_json()) == options
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        launch = WorkerLaunch(identity=WorkerIdentity(run_id='installed', config_hash=content_hash('fixture'),
            lock_hash=content_hash('lock'), worker_id='a', role='researcher'), runtime=registered.ref,
            options=options.to_dict(), operation_id='first', prompt='fixture', workspace=str(root/'workspace'))
        executable = HarborWorkerRuntime().command_options(launch, root)
        assert len(executable.command_files) == 5 and all(Path(name).is_file() for name in executable.command_files)
        helper = executable.command[2]
        code = 'import runpy,sys; runpy.run_path('+repr(helper)+',run_name="packaged_import"); assert "harbor" not in sys.modules'
        subprocess.run([sys.executable, '-I', '-c', code], cwd=root, check=True, timeout=10)
    print('Installed Harbor worker: optional plugin, typed declaration and five pinned helper files passed.')


if __name__ == '__main__':
    check()
