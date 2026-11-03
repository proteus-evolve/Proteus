"""Pi v3 session accounting, following the v0.84.2 usage-cost projection.

Reference: earendil-works/pi@914cf1472e715297caa30db4b9535d534a9eb718.
Only newly created, non-inherited session files are attributable to this phase.
"""

import base64
import hashlib
import json

from proteus.core.contracts import Usage, require
from proteus.core.serialization import canonical_json


KINDS = ('assistant', 'toolResult', 'compaction', 'branch_summary')
IGNORED = {'thinking_level_change', 'model_change', 'custom', 'custom_message', 'label', 'session_info'}


def fingerprint(path):
    try:
        if path.is_symlink():
            return None
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


def capture_pi_sources(paths, previous):
    """Freeze bytes before the outcome; existing-session changes invalidate totals."""
    complete = all(value is not None and fingerprint(path) == value for path, value in previous.items())
    logs = []
    for path in sorted(paths, key=str):
        before = fingerprint(path)
        try:
            data = path.read_bytes() if before is not None else None
        except OSError:
            data = None
        if data is None or fingerprint(path) != before:
            complete = False
        logs.append({'name': path.name, 'data': None if data is None else base64.b64encode(data).decode('ascii')})
    return canonical_json({'version': 1, 'capture_complete': complete, 'logs': logs}).encode('utf-8')


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _sum(values):
    return None if not values or None in values else sum(values)


def pi_fields(source, *, transport_complete):
    require(source is None or type(source) is bytes, 'Pi usage source must be bytes')
    fields = {'source_hash': None if source is None else 'sha256:'+hashlib.sha256(source).hexdigest(),
        'source_bytes': None if source is None else len(source), 'transport_complete': transport_complete,
        'stream_status': 'unavailable', 'session_ids': [], 'logs': 0,
        'samples': dict.fromkeys(KINDS, 0), 'unknown_samples': {'input_tokens': 0, 'output_tokens': 0},
        'input_tokens': None, 'output_tokens': None}
    if source is None:
        return fields
    inputs, outputs = [], []
    complete, inherited = True, False

    def sample(kind, usage):
        fields['samples'][kind] += 1
        usage = usage if isinstance(usage, dict) else {}
        incoming = _sum([_count(usage.get(key)) for key in ('input', 'cacheRead', 'cacheWrite')])
        outgoing = _count(usage.get('output'))
        # Pinned Pi providers initialize these fields to zero and can retain that
        # default when the provider omits usage. Session JSON cannot disambiguate
        # that case from a genuine reported zero; only positive totals are known.
        incoming = incoming or None
        outgoing = outgoing or None
        inputs.append(incoming)
        outputs.append(outgoing)
        fields['unknown_samples']['input_tokens'] += incoming is None
        fields['unknown_samples']['output_tokens'] += outgoing is None

    try:
        bundle = json.loads(source.decode('utf-8'))
        require(type(bundle) is dict and set(bundle) == {'version', 'capture_complete', 'logs'}
                and type(bundle['version']) is int and bundle['version'] == 1
                and type(bundle['capture_complete']) is bool and type(bundle['logs']) is list,
                'invalid Pi source bundle')
        complete = bundle['capture_complete']
        names = set()
        for log in bundle['logs']:
            require(type(log) is dict and set(log) == {'name', 'data'} and isinstance(log['name'], str)
                    and log['name'].endswith('.jsonl') and '/' not in log['name'] and '\\' not in log['name']
                    and log['name'] not in names, 'invalid Pi source log')
            names.add(log['name'])
            fields['logs'] += 1
            if log['data'] is None:
                complete = False
                continue
            require(type(log['data']) is str, 'invalid Pi source encoding')
            data = base64.b64decode(log['data'], validate=True)
            lines = data.splitlines(keepends=True)
            require(lines and all(line.endswith(b'\n') for line in lines), 'truncated Pi log')
            entries = [json.loads(line.decode('utf-8')) for line in lines if line.strip()]
            require(entries and type(entries[0]) is dict, 'missing Pi header')
            header = entries[0]
            require(header.get('type') == 'session' and type(header.get('version')) is int
                    and header['version'] == 3 and isinstance(header.get('id'), str) and header['id'].strip()
                    and header['id'] not in fields['session_ids'], 'invalid Pi session identity/version')
            fields['session_ids'].append(header['id'])
            inherited = inherited or bool(header.get('parentSession'))
            identities = set()
            last_assistant = None
            for entry in entries[1:]:
                require(type(entry) is dict and isinstance(entry.get('id'), str) and entry['id'].strip()
                        and entry['id'] not in identities and 'parentId' in entry
                        and (entry['parentId'] is None or entry['parentId'] in identities), 'invalid Pi entry identity')
                identities.add(entry['id'])
                kind = entry.get('type')
                if kind == 'message':
                    message = entry['message']
                    role = message['role']
                    if role == 'assistant':
                        last_assistant = message.get('stopReason')
                        complete = complete and last_assistant in ('stop', 'length', 'toolUse')
                        sample(role, message.get('usage'))
                    elif role == 'toolResult':
                        if message.get('usage') is not None:
                            sample(role, message['usage'])
                    else:
                        require(role in ('user', 'custom', 'bashExecution'), 'unsupported Pi message role')
                elif kind in ('compaction', 'branch_summary'):
                    sample(kind, entry.get('usage'))
                else:
                    require(kind in IGNORED and 'usage' not in entry, 'unsupported Pi entry type')
            complete = complete and last_assistant in ('stop', 'length')
        fields['stream_status'] = 'unsupported_history' if inherited else (
            'reported' if complete and fields['logs'] and inputs else 'incomplete')
        if fields['stream_status'] == 'reported' and transport_complete:
            fields.update(input_tokens=_sum(inputs), output_tokens=_sum(outputs))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        fields.update(stream_status='invalid', input_tokens=None, output_tokens=None)
    return fields


def pi_reported(fields, *, wall_seconds):
    import re
    require(type(fields) is dict and set(fields) == {'source_hash', 'source_bytes', 'transport_complete',
        'stream_status', 'session_ids', 'logs', 'samples', 'unknown_samples', 'input_tokens', 'output_tokens'},
        'invalid Pi usage fields')
    require(type(fields['transport_complete']) is bool and fields['stream_status'] in
        ('unavailable', 'incomplete', 'reported', 'unsupported_history', 'invalid'), 'invalid Pi usage status')
    require(type(fields['session_ids']) is list and all(isinstance(value, str) and value.strip()
        for value in fields['session_ids']) and len(fields['session_ids']) == len(set(fields['session_ids'])),
        'invalid Pi session identities')
    require(type(fields['logs']) is int and fields['logs'] >= len(fields['session_ids'])
        and type(fields['samples']) is dict and set(fields['samples']) == set(KINDS)
        and all(type(value) is int and value >= 0 for value in fields['samples'].values()), 'invalid Pi sample counts')
    count = sum(fields['samples'].values())
    require(type(fields['unknown_samples']) is dict and set(fields['unknown_samples']) == {'input_tokens', 'output_tokens'}
        and all(type(value) is int and 0 <= value <= count for value in fields['unknown_samples'].values()),
        'invalid Pi unknown counts')
    if fields['stream_status'] == 'unavailable':
        require(fields['source_hash'] is None and fields['source_bytes'] is None and not fields['logs']
                and not fields['session_ids'] and not count, 'unavailable Pi usage has a source')
    else:
        require(isinstance(fields['source_hash'], str) and re.fullmatch(r'sha256:[0-9a-f]{64}', fields['source_hash'])
                and type(fields['source_bytes']) is int and fields['source_bytes'] >= 0, 'invalid Pi usage source identity')
    if fields['stream_status'] == 'reported':
        require(fields['logs'] == len(fields['session_ids']) and count > 0, 'Pi usage has no complete sessions')
    for key in ('input_tokens', 'output_tokens'):
        if not fields['transport_complete'] or fields['stream_status'] != 'reported' or fields['unknown_samples'][key]:
            require(fields[key] is None, 'incomplete Pi usage cannot claim a total')
    return Usage(input_tokens=fields['input_tokens'], output_tokens=fields['output_tokens'], wall_seconds=wall_seconds)


def verify_pi_source(evidence, source):
    expected = pi_fields(source, transport_complete=evidence['fields']['transport_complete'])
    require(expected == evidence['fields'], 'Pi usage source differs from normalized evidence')
