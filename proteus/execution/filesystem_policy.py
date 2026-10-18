"""Standalone Linux filesystem policy applied before candidate code or threads run.

Landlock ABI 3 controls file reads/execution and content/tree writes. Metadata,
network, device ioctls and already-open protocol descriptors are separate controls.
"""
import base64
import ctypes
import json
import os
from pathlib import PurePosixPath
import platform
import stat
import sys


MIN_ABI = 3
READ = (1 << 0) | (1 << 2) | (1 << 3)
HANDLED = (1 << 15) - 1  # ABI 3: EXECUTE through TRUNCATE, including REFER.
WRITE = HANDLED & ~READ
MAX_RECEIPT = 65536


def policy(read, write):
    if not read and not write:
        return None
    for paths in (read, write):
        if not isinstance(paths, (list, tuple)) or len(paths) > 64 or len(set(paths)) != len(paths):
            raise ValueError('filesystem scopes require at most 64 distinct paths per access mode')
        for path in paths:
            if (not isinstance(path, str) or not path.startswith('/') or '\x00' in path
                    or len(path.encode()) > 4096 or str(PurePosixPath(path)) != path
                    or '..' in PurePosixPath(path).parts or path.startswith('//')):
                raise ValueError('filesystem scopes must be canonical absolute container paths')
    if not read:
        raise ValueError('filesystem scopes require an explicit readable runtime hierarchy')
    value = {'version': 1, 'read': list(read), 'write': list(write)}
    if len(json.dumps(value).encode()) > MAX_RECEIPT // 2:
        raise ValueError('filesystem scope declaration exceeds its byte limit')
    return value


def validate_receipt(receipt, expected, *, uid=None, gid=None):
    if (not isinstance(receipt, dict) or set(receipt) != {'version', 'backend', 'abi', 'handled_access_fs', 'policy', 'uid', 'gid'}
            or type(receipt['version']) is not int or receipt['version'] != 1
            or receipt['backend'] != 'landlock' or type(receipt['abi']) is not int or receipt['abi'] < MIN_ABI
            or type(receipt['handled_access_fs']) is not int or receipt['handled_access_fs'] != HANDLED
            or receipt['policy'] != expected or expected is None
            or any(type(receipt[key]) is not int or receipt[key] < 0 for key in ('uid', 'gid'))
            or (uid is not None and receipt['uid'] != uid) or (gid is not None and receipt['gid'] != gid)):
        raise ValueError('filesystem policy receipt differs from the declared enforcement')
    return receipt


def enforce(value):
    if not isinstance(value, dict) or set(value) != {'version', 'read', 'write'} or type(value['version']) is not int:
        raise ValueError('invalid filesystem policy')
    if value != policy(value['read'], value['write']) or value is None:
        raise ValueError('invalid filesystem policy')
    if sys.platform != 'linux' or platform.machine() not in ('x86_64', 'aarch64'):
        raise RuntimeError('filesystem scopes require Linux x86_64/aarch64 with Landlock ABI 3 or newer')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def syscall(number, *args):
        result = libc.syscall(ctypes.c_long(number), *args)
        if result < 0:
            code = ctypes.get_errno()
            raise OSError(code, 'Landlock filesystem policy: ' + os.strerror(code))
        return result

    abi = syscall(444, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1))
    if abi < MIN_ABI:
        raise RuntimeError('filesystem scopes require Landlock ABI 3 or newer; refusing partial enforcement')

    class Ruleset(ctypes.Structure):
        _fields_ = [('handled_access_fs', ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int)]

    ruleset = Ruleset(HANDLED)
    descriptor = syscall(444, ctypes.byref(ruleset), ctypes.c_size_t(ctypes.sizeof(ruleset)), ctypes.c_uint(0))
    try:
        for paths, access in ((value['read'], READ), (value['write'], WRITE)):
            for path in paths:
                scope = os.open(path, os.O_PATH | os.O_CLOEXEC)
                try:
                    if not stat.S_ISDIR(os.fstat(scope).st_mode):
                        raise ValueError('filesystem scope must identify an existing container directory: ' + path)
                    rule = PathRule(access, scope)
                    syscall(445, ctypes.c_int(descriptor), ctypes.c_int(1), ctypes.byref(rule), ctypes.c_uint(0))
                finally:
                    os.close(scope)
        # The bootstrap is single-threaded; later exec and all child threads inherit.
        if libc.prctl(38, ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, 'cannot set no_new_privs: ' + os.strerror(code))
        syscall(446, ctypes.c_int(descriptor), ctypes.c_uint(0))
    finally:
        os.close(descriptor)
    return {'version': 1, 'backend': 'landlock', 'abi': abi, 'handled_access_fs': HANDLED,
            'policy': value, 'uid': os.getuid(), 'gid': os.getgid()}


def main():
    value = json.loads(base64.b64decode(sys.argv[1], validate=True))
    if sys.argv[2] != '--' or len(sys.argv) < 4:
        raise ValueError('filesystem bootstrap requires an executable')
    receipt = enforce(value)
    # The owner consumes this line before forwarding any candidate RPC input.
    print(json.dumps(receipt, ensure_ascii=True, separators=(',', ':')), flush=True)
    os.execvpe(sys.argv[3], sys.argv[3:], os.environ)


if __name__ == '__main__':
    main()
