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
from proteus.core.native_usage import RAW_SOURCES, usage_evidence, validate_usage, verify_usage_source
from proteus.core.serialization import canonical_json, content_hash


def input_directory(root):
    from proteus.core.episode import private_record_dir
    return private_record_dir(Path(root)) / 'native-inputs'


def _publish(path, value):
    # Each attempt owns fresh names. A killed writer leaves only an ignored .tmp;
    # a published input always precedes dispatch and is never rewritten on resume.
    value = dict(value)
    value.setdefault('hash', content_hash(value))
    _publish_bytes(path, (canonical_json(value)+'\n').encode('utf-8'))


def _publish_bytes(path, data):
    temporary = path.with_suffix(path.suffix+'.tmp')
    with temporary.open('xb') as stream:
        stream.write(data)
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
    request = {'version': 2, 'id': identity, 'backend': backend, 'episode': spec.episode,
        'phase': phase, 'model': model, 'scope': scope,
        'protocol_version': DEFAULT_EPISODE_PROTOCOL_VERSION,
        'prepared_ns': time.time_ns(), 'payload': json.loads(canonical_json(payload)),
        'payload_hash': content_hash(payload)}
    request['hash'] = content_hash(request)
    _publish(root/(identity+'.input.json'), request)
    receipt = {}
    started = time.monotonic()

    def publish_outcome(**status):
        # Usage metadata is a fixed projection. CLI adapters additionally supply a
        # hash-bound raw stream, kept separately from the JSON outcome.
        evidence = validate_usage(receipt.get('usage', usage_evidence()), request)
        if evidence['source'] in RAW_SOURCES:
            source = receipt.get('usage_source')
            verify_usage_source(evidence, source)
            if source is not None:
                _publish_bytes(root/(identity+'.usage-source'), source)
        evidence = usage_evidence(evidence['source'], evidence['fields'],
                                  wall_seconds=time.monotonic()-started)
        if receipt.get('activation') is not None:
            from proteus.core.native_activation import validate_activation
            status['activation'] = validate_activation(receipt['activation'], request)
        _publish(root/(identity+'.outcome.json'), {'version': 2, 'id': identity,
            'input_hash': request['hash'], 'finished_ns': time.time_ns(), 'usage': evidence, **status})

    try:
        yield receipt
    except BaseException as exc:
        publish_outcome(status='raised', error_type=type(exc).__name__)
        raise
    else:
        # Explicitly whitelist the optional transport exit code. The JSON outcome
        # contains no arbitrary response, traceback or authentication fields.
        code = receipt.get('returncode')
        require(code is None or type(code) is int, 'native transport return code must be an integer')
        publish_outcome(status='returned', returncode=code)


def read_native_inputs(root):
    """Read cumulative prepared inputs, preserving interrupted attempts as unknown."""
    directory = input_directory(root)
    result = []
    for path in directory.glob('*.input.json'):
        identity = path.name.removesuffix('.input.json')
        require(re.fullmatch('[0-9a-f]{32}', identity) is not None and not path.is_symlink(),
                'invalid native input path')
        request = json.loads(path.read_text(encoding='utf-8'))
        require(type(request.get('version')) is int and request['version'] in (1, 2)
                and request.get('id') == identity,
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
            require(type(outcome.get('version')) is int and outcome['version'] == request['version']
                    and outcome.get('id') == identity
                    and outcome.get('input_hash') == digest
                    and outcome.get('status') in ('returned', 'raised'), 'native outcome identity mismatch')
            if request['version'] == 2:
                validate_usage(outcome.get('usage'), request)
                if outcome['usage']['source'] in RAW_SOURCES:
                    source_path = directory/(identity+'.usage-source')
                    require(not source_path.is_symlink(), 'invalid native usage source path')
                    expected_source = outcome['usage']['fields']['source_hash'] is not None
                    require(source_path.is_file() == expected_source, 'native usage source is missing or unexpected')
                    verify_usage_source(outcome['usage'], source_path.read_bytes() if expected_source else None)
            if 'activation' in outcome:
                from proteus.core.native_activation import validate_activation
                validate_activation(outcome['activation'], request)
            if request['backend'] in ('pi','dsh') and request['scope'] == 'harness_cli':
                from proteus.core.native_activation import split_source_command
                _, expected = split_source_command(request['payload'])
                if expected is not None and outcome['status'] == 'returned' and outcome.get('returncode') == 0:
                    require('activation' in outcome, 'successful native source dispatch lacks activation evidence')
        result.append({'input': request, 'outcome': outcome,
                       'completion': outcome['status'] if outcome else 'unknown'})
    return sorted(result, key=lambda row: (row['input']['prepared_ns'], row['input']['id']))
