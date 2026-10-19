"""Durable adapter-boundary inputs, separate from core prompts and provider internals."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import time
import uuid

from proteus.core.contracts import require
from proteus.core.episode_protocol import DEFAULT_EPISODE_PROTOCOL_VERSION
from proteus.core.serialization import canonical_json, content_hash


def input_directory(root):
    from proteus.core.episode import private_record_dir
    return private_record_dir(Path(root)) / 'native-inputs'


def _publish(path, value):
    # Each attempt owns fresh names. A killed writer leaves only an ignored .tmp;
    # a published input always precedes dispatch and is never rewritten on resume.
    value = dict(value)
    value.setdefault('hash', content_hash(value))
    temporary = path.with_suffix(path.suffix+'.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        stream.write(canonical_json(value)+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    if os.name != 'nt':
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def native_input(spec, *, backend, phase, scope, payload, model=None):
    """Publish before dispatch; an absent outcome means completion is unknown.

    The caller supplies only model input, never credentials, environment mappings
    or the complete process invocation. CLI payloads exclude authentication flags.
    A returned transport is not proof of successful inference or model-call usage.
    """
    root = input_directory(spec.root)
    root.mkdir(parents=True, exist_ok=True)
    identity = uuid.uuid4().hex
    request = {'version': 1, 'id': identity, 'backend': backend, 'episode': spec.episode,
        'phase': phase, 'model': model, 'scope': scope,
        'protocol_version': DEFAULT_EPISODE_PROTOCOL_VERSION,
        'prepared_ns': time.time_ns(), 'payload': json.loads(canonical_json(payload)),
        'payload_hash': content_hash(payload)}
    request['hash'] = content_hash(request)
    _publish(root/(identity+'.input.json'), request)
    receipt = {}
    try:
        yield receipt
    except BaseException as exc:
        _publish(root/(identity+'.outcome.json'), {'version': 1, 'id': identity,
            'input_hash': request['hash'], 'status': 'raised', 'error_type': type(exc).__name__,
            'finished_ns': time.time_ns()})
        raise
    else:
        # Explicitly whitelist the optional transport exit code. No response,
        # traceback, URL, authentication header or arbitrary receipt is persisted.
        code = receipt.get('returncode')
        require(code is None or type(code) is int, 'native transport return code must be an integer')
        _publish(root/(identity+'.outcome.json'), {'version': 1, 'id': identity,
            'input_hash': request['hash'], 'status': 'returned', 'returncode': code,
            'finished_ns': time.time_ns()})


def read_native_inputs(root):
    """Read cumulative prepared inputs, preserving interrupted attempts as unknown."""
    directory = input_directory(root)
    result = []
    for path in directory.glob('*.input.json'):
        identity = path.name.removesuffix('.input.json')
        require(re.fullmatch('[0-9a-f]{32}', identity) is not None and not path.is_symlink(),
                'invalid native input path')
        request = json.loads(path.read_text(encoding='utf-8'))
        require(request.get('version') == 1 and request.get('id') == identity,
                'unsupported native input identity/version')
        digest = request.get('hash')
        require(digest == content_hash({key: value for key, value in request.items() if key != 'hash'})
                and request.get('payload_hash') == content_hash(request.get('payload')),
                'native input hash mismatch')
        outcome_path = directory/(identity+'.outcome.json')
        outcome = None
        require(not outcome_path.is_symlink(), 'invalid native outcome path')
        if outcome_path.exists():
            outcome = json.loads(outcome_path.read_text(encoding='utf-8'))
            require(outcome.get('hash') == content_hash({key: value for key, value in outcome.items() if key != 'hash'}),
                    'native outcome hash mismatch')
            require(outcome.get('version') == 1 and outcome.get('id') == identity
                    and outcome.get('input_hash') == digest
                    and outcome.get('status') in ('returned', 'raised'), 'native outcome identity mismatch')
        result.append({'input': request, 'outcome': outcome,
                       'completion': outcome['status'] if outcome else 'unknown'})
    return sorted(result, key=lambda row: (row['input']['prepared_ns'], row['input']['id']))
