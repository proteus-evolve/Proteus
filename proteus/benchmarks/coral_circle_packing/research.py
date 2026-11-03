"""Standalone worker CLI for the authenticated Proteus research gateway.

Only the scoped connection environment reaches this program. Operation IDs must
be retained when retrying a submission or write; use a new ID for new content.
"""

import argparse
import base64
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


def request(path, body=None):
    deadline = time.monotonic()+30
    while True:
        wire = urllib.request.Request(os.environ['PROTEUS_EVALUATION_URL']+path,
            data=None if body is None else json.dumps(body, allow_nan=False).encode(),
            headers={'Authorization': 'Bearer '+os.environ['PROTEUS_EVALUATION_TOKEN'],
                     'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(wire, timeout=2) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 503:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError('gateway unavailable; retry the same operation ID')
        time.sleep(.1)


def file(path, name):
    if path.is_symlink() or not path.is_file():
        raise ValueError('only regular files may be submitted')
    return {'path': name, 'base64': base64.b64encode(path.read_bytes()).decode(),
            'executable': bool(path.stat().st_mode & 0o111)}


def tree(directory):
    directory = Path(directory).resolve()
    files = []
    for path in sorted(directory.rglob('*')):
        relative = path.relative_to(directory)
        if set(relative.parts) & {'.git', '.proteus-deps', '__pycache__', '.pytest_cache'}:
            continue
        if path.is_symlink():
            raise ValueError('candidate trees cannot contain symbolic links')
        if path.is_file():
            files.append(file(path, relative.as_posix()))
    if not files:
        raise ValueError('candidate directory is empty')
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    submit = commands.add_parser('submit')
    submit.add_argument('--operation', required=True)
    submit.add_argument('--evaluation', default='packing')
    submit.add_argument('--directory', type=Path, default=Path('candidate'))
    poll = commands.add_parser('poll')
    poll.add_argument('ticket_id')
    snapshot = commands.add_parser('snapshot')
    snapshot.add_argument('--operation', required=True)
    read = commands.add_parser('read')
    read.add_argument('revision_hash')
    publish = commands.add_parser('publish')
    publish.add_argument('--operation', required=True)
    publish.add_argument('--kind', choices=('notes', 'skills'), required=True)
    publish.add_argument('--key', required=True)
    publish.add_argument('--file', type=Path, required=True)
    publish.add_argument('--sources', type=Path)
    publish.add_argument('--expected', type=Path)
    args = parser.parse_args()
    if args.command == 'submit':
        result = request('/v1/submissions', {'operation_id': args.operation, 'evaluation_id': args.evaluation,
                                            'files': tree(args.directory)})
    elif args.command == 'poll':
        result = request('/v1/submissions/'+args.ticket_id)
    elif args.command == 'snapshot':
        result = request('/v1/knowledge/snapshots', {'operation_id': args.operation})
    elif args.command == 'read':
        result = request('/v1/knowledge/revisions/'+args.revision_hash.removeprefix('sha256:'))
    else:
        name = 'SKILL.md' if args.kind == 'skills' else args.file.name
        result = request('/v1/knowledge/writes', {'operation_id': args.operation, 'action': 'publish',
            'kind': args.kind, 'key': args.key, 'files': [file(args.file, name)],
            'sources': json.loads(args.sources.read_text()) if args.sources else [],
            'expected': json.loads(args.expected.read_text()) if args.expected else None})
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
