"""Read shared discoveries and publish version-checked notes or skills as a worker."""

import argparse
import json
from pathlib import Path

from proteus.gateway.client import GatewayClient, snapshot
from proteus.gateway.knowledge import KnowledgeCapture, KnowledgeSubmission, revision_ref


class KnowledgeClient(GatewayClient):
    def capture(self, operation_id):
        return self._request('/v1/knowledge/snapshots', KnowledgeCapture(operation_id=operation_id).to_dict())

    def read(self, ref):
        # Validate the entire reference before encoding its digest into a URL.
        from proteus.knowledge.records import revision_ref as validate_revision
        validate_revision(ref)
        return self._request('/v1/knowledge/revisions/' + ref.content_hash.split(':')[1])

    def write(self, submission):
        return self._request('/v1/knowledge/writes', submission.to_dict())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    capture = actions.add_parser('capture')
    capture.add_argument('--operation', required=True)
    read = actions.add_parser('read')
    read.add_argument('revision', help='64-character revision content hash, without sha256:')
    for action in ('publish', 'delete'):
        command = actions.add_parser(action)
        command.add_argument('kind', choices=('notes', 'skills'))
        command.add_argument('key')
        if action == 'publish':
            command.add_argument('directory', type=Path)
        command.add_argument('--operation', required=True)
        command.add_argument('--expected', required=action == 'delete')
        command.add_argument('--source', action='append', default=[])
    args = parser.parse_args()
    client = KnowledgeClient()
    if args.action == 'capture':
        result = client.capture(args.operation)
    elif args.action == 'read':
        result = client.read(revision_ref(args.revision))
    else:
        result = client.write(KnowledgeSubmission(action=args.action, kind=args.kind, key=args.key,
            operation_id=args.operation, files=snapshot(args.directory) if args.action == 'publish' else (),
            expected=revision_ref(args.expected) if args.expected else None,
            sources=tuple(revision_ref(digest) for digest in args.source)))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
