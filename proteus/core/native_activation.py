"""Source-mode startup evidence for Pi/DSH, bound to the native dispatch receipt."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from proteus.core.contracts import require

FLAG = '--proteus-expect-source'
PREFIX = 'PROTEUS_RUNTIME_V1 '


def source_tree_hash(root):
    """Match the length-framed Node tree_hash used by both source image wrappers."""
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), 'native source root must be a real directory')
    digest = hashlib.sha256()

    def walk(directory, relative=''):
        for path in sorted(directory.iterdir(), key=lambda p: p.name.encode('utf-8')):
            if path.name == 'node_modules':
                continue
            name = (relative+'/'+path.name if relative else path.name)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                kind, data = b'L', os.readlink(path).encode('utf-8')
            elif stat.S_ISDIR(mode):
                walk(path, name)
                continue
            elif stat.S_ISREG(mode):
                kind, data = (b'X' if mode & 0o111 else b'F'), path.read_bytes()
            else:
                raise ValueError('unsupported native source entry: '+name)
            encoded = name.encode('utf-8')
            for value in (kind, str(len(encoded)).encode(), encoded, str(len(data)).encode(), data):
                digest.update(value)
                digest.update(b'\0')
    walk(root)
    return digest.hexdigest()


def source_command(spec, active, command):
    source = Path(active)/'src'
    # Legacy direct transports remain readable but cannot pass activation gates.
    # A staged source snapshot must never silently fall back to the baked CLI.
    if spec.active_root is None:
        return command
    return [FLAG, source_tree_hash(source), *command]


def split_source_command(command):
    require(type(command) is list and all(type(item) is str for item in command), 'invalid native command')
    if command[:1] != [FLAG]:
        return command, None
    require(len(command) > 2 and re.fullmatch('[0-9a-f]{64}', command[1]) is not None,
            'invalid expected native source hash')
    return command[2:], command[1]


def validate_activation(value, request):
    _, expected = split_source_command(request['payload'])
    require(expected is not None and request['backend'] in ('pi','dsh') and request['scope'] == 'harness_cli',
            'native activation is not bound to a source dispatch')
    require(type(value) is dict and set(value) == {'version','backend','source_sha256','cli_sha256','boot_sha256','mode'}
            and type(value['version']) is int and value['version'] == 1
            and value['backend'] == request['backend'] and value['source_sha256'] == expected
            and value['mode'] in ('pristine','cache','build')
            and all(type(value[key]) is str and re.fullmatch('[0-9a-f]{64}',value[key]) is not None
                    for key in ('source_sha256','cli_sha256','boot_sha256')),
            'native activation receipt differs from its source dispatch')
    return value


def capture_activation(stdout, command, backend, *, required=True):
    _, expected = split_source_command(command)
    if expected is None:
        return None
    if not required and (type(stdout) is not str or not stdout.startswith(PREFIX)):
        return None  # A build failure can precede startup; preserve its transport error.
    require(type(stdout) is str, 'native source image did not return a startup receipt')
    line = stdout.partition('\n')[0]
    require(line.startswith(PREFIX) and len(line) <= 4096,
            'native source image lacks its startup receipt; rebuild the source image')
    return validate_activation(json.loads(line[len(PREFIX):]),
        {'payload': command, 'backend': backend, 'scope': 'harness_cli'})
