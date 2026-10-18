"""Installed scope contract and actual Linux kernel enforcement, without inference."""
import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from proteus.core.serialization import content_hash
from proteus.execution import filesystem_policy as fs
from proteus.examples.harbor_code_memory import prepare
from proteus.examples.code_memory import register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry


def kernel_check(root):
    allowed = root/'allowed'
    allowed.mkdir()
    secret = root/'secret'
    secret.write_text('outside declared read scopes')
    read_only = root/'read-only'
    read_only.mkdir()
    (read_only/'visible').write_text('readable but not writable')
    (allowed/'escape').symlink_to(secret)
    denied_write = root/'outside-write'
    value = fs.policy(tuple(dict.fromkeys(('/usr', sys.base_prefix, str(allowed), str(read_only)))), (str(allowed),))
    encoded = base64.b64encode(json.dumps(value).encode()).decode()
    program = f'''
import os, subprocess, sys
from pathlib import Path
allowed=Path({str(allowed)!r}); secret=Path({str(secret)!r}); outside=Path({str(denied_write)!r})
read_only=Path({str(read_only)!r})
def refused(action):
    try: action()
    except PermissionError: return
    raise AssertionError('filesystem access escaped its declared scope')
(allowed/'first').write_text('allowed')
(allowed/'first').rename(allowed/'second')
assert (allowed/'second').read_text() == 'allowed'
(allowed/'second').unlink()
refused(lambda: secret.read_text())
refused(lambda: (allowed/'escape').read_text())
assert (read_only/'visible').read_text() == 'readable but not writable'
refused(lambda: (read_only/'visible').write_text('must fail'))
refused(lambda: (read_only/'new').write_text('must fail'))
refused(lambda: outside.write_text('must fail'))
refused(lambda: os.truncate(secret, 0))
child = subprocess.run([sys.executable, '-I', '-c', 'from pathlib import Path; Path('+repr(str(secret))+').read_text()'], capture_output=True, text=True)
assert child.returncode != 0 and 'PermissionError' in child.stderr
print('scoped reads/writes, rename/remove, truncate denial and child inheritance passed')
'''
    result = subprocess.run([sys.executable, '-I', fs.__file__, encoded, '--', sys.executable, '-I', '-c', program],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    receipt = fs.validate_receipt(json.loads(lines[0]), value, uid=os.getuid(), gid=os.getgid())
    assert len(lines) == 2 and 'child inheritance passed' in lines[1]
    assert secret.read_text() == 'outside declared read scopes' and not denied_write.exists()
    assert (read_only/'visible').read_text() == 'readable but not writable' and not (read_only/'new').exists()
    return receipt


def check(require_kernel=False):
    with tempfile.TemporaryDirectory(prefix='proteus-scopes-') as directory:
        root = Path(directory)
        recipe = load_recipe(prepare(root/'study', sys.executable, runtime={'runtime_hash': content_hash('fixture')}))
        registry = builtin_registry()
        register(registry)
        env = replace(recipe.execution.environment, read_scopes=('/usr', '/opt/proteus'),
                      write_scopes=('/opt/proteus/candidate',), resources={'cpus': 1, 'memory_mb': 512, 'pids': 32})
        resolved = registry.resolve(replace(recipe, execution=replace(recipe.execution, environment=env)))
        assert resolved.execution.environment.resources['pids'] == 32
        assert 'environment.filesystem_scopes' in registry.get(env.backend.plugin).capabilities
        if sys.platform == 'linux':
            receipt = kernel_check(root)
            print('installed filesystem scopes: actual Landlock ABI', receipt['abi'], 'passed')
        else:
            assert not require_kernel, 'required filesystem enforcement needs Linux'
            try:
                fs.enforce(fs.policy(('/usr',), ()))
            except RuntimeError as exc:
                assert 'require Linux' in str(exc)
            else:
                raise AssertionError('unsupported operating system accepted filesystem scopes')
            print('installed filesystem scope contracts and explicit platform refusal passed; Linux enforcement required in CI')


if __name__ == '__main__':
    check('--require-kernel' in sys.argv)
