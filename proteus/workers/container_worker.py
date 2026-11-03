"""Trusted stdlib bootstrap uploaded into a pinned research-worker container."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    request = json.loads(sys.stdin.readline())
    for path, expected in request['command_files'].items():
        if 'sha256:'+hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise ValueError('container executable or bridge pin changed')
    environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TMPDIR': '/tmp'}
    environment.update(request['environment'])
    with tempfile.TemporaryFile() as payload:
        payload.write((json.dumps(request['launch'])+'\n').encode())
        payload.seek(0)
        child = subprocess.Popen(request['command'], cwd='/workspace', env=environment, stdin=payload,
                                 stdout=subprocess.PIPE, stderr=sys.stderr, start_new_session=True)
        print(json.dumps({'worker_pid': child.pid}), flush=True)
        try:
            while chunk := os.read(child.stdout.fileno(), 65536):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            return child.wait()
        finally:
            child.stdout.close()


if __name__ == '__main__':
    raise SystemExit(main())
