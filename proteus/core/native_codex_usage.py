"""Usage from one fresh Codex exec stream; raw evidence remains controller-private."""

import hashlib
import json
import re

from proteus.core.contracts import Usage, require


def codex_fields(source, *, transport_complete):
    from proteus.workers.codex_bridge import CodexStream
    require(source is None or type(source) is bytes, 'Codex usage source must be bytes')
    fields = {'source_hash': None if source is None else 'sha256:'+hashlib.sha256(source).hexdigest(),
        'source_bytes': None if source is None else len(source), 'transport_complete': transport_complete,
        'stream_status': 'unavailable', 'session_id': None, 'input_tokens': None, 'output_tokens': None}
    if source is None:
        return fields
    stream = CodexStream(None)  # native phases use fresh --ephemeral threads
    try:
        for line in source.splitlines(keepends=True):
            if not line.strip():
                continue
            if not line.endswith(b'\n'):
                raise ValueError('truncated Codex event')
            stream.accept(json.loads(line.decode('utf-8')))
        fields['session_id'] = stream.session
        fields['stream_status'] = {'turn.completed': 'completed', 'turn.failed': 'failed'}.get(
            stream.terminal, 'incomplete')
        if stream.session is not None:
            usage = stream.result(interrupted=not transport_complete)['usage']
            fields.update({key: usage[key] for key in ('input_tokens', 'output_tokens')})
    except (ValueError, TypeError, AttributeError):
        fields.update(stream_status='invalid', input_tokens=None, output_tokens=None)
    return fields


def codex_reported(fields, *, wall_seconds):
    from proteus.workers.codex_bridge import session_id
    require(type(fields) is dict and set(fields) == {'source_hash', 'source_bytes', 'transport_complete',
        'stream_status', 'session_id', 'input_tokens', 'output_tokens'}, 'invalid Codex usage fields')
    require(type(fields['transport_complete']) is bool and fields['stream_status'] in
        ('unavailable', 'incomplete', 'completed', 'failed', 'invalid'), 'invalid Codex usage completion')
    if fields['stream_status'] == 'unavailable':
        require(fields['source_hash'] is None and fields['source_bytes'] is None
                and fields['session_id'] is None, 'unavailable Codex usage has a source')
    else:
        require(isinstance(fields['source_hash'], str)
                and re.fullmatch(r'sha256:[0-9a-f]{64}', fields['source_hash']) is not None
                and type(fields['source_bytes']) is int and fields['source_bytes'] >= 0,
                'invalid Codex usage source identity')
    if fields['session_id'] is not None:
        session_id(fields['session_id'])
    if fields['stream_status'] == 'completed':
        require(fields['session_id'] is not None, 'completed Codex usage needs a confirmed session')
    if not fields['transport_complete'] or fields['stream_status'] != 'completed':
        require(fields['input_tokens'] is None and fields['output_tokens'] is None,
                'incomplete Codex transport cannot claim final tokens')
    return Usage(input_tokens=fields['input_tokens'], output_tokens=fields['output_tokens'],
                 wall_seconds=wall_seconds)


def verify_codex_source(evidence, source):
    expected = codex_fields(source, transport_complete=evidence['fields']['transport_complete'])
    require(expected == evidence['fields'], 'Codex usage source differs from normalized evidence')
