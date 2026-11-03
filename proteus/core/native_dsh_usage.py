"""Recorded DSH format-0 usage, with explicit gaps for unreported auxiliary work."""

import base64
import hashlib
import json
from pathlib import PurePosixPath
import re

from proteus.core.contracts import Usage, require
from proteus.core.native_dsh_storage import decode_log, decompress_complete
from proteus.core.native_pi_usage import fingerprint
from proteus.core.serialization import canonical_json


KINDS = ('steps', 'summaries', 'unreported')
IGNORED = frozenset(('agent-preset/selected', 'agent/inbox/spliced', 'approval/asked',
    'approval/decided', 'approval/policy', 'command/done', 'command/run', 'feedback/record',
    'goal/change', 'hook/invoked', 'hook/result', 'permission/preset', 'plan/mode',
    'request/context', 'request/header', 'sandbox/mode', 'schedule/change', 'session/end-seed',
    'session/title', 'subagent/descriptor', 'todo/write', 'tool-workflow/agent-end',
    'tool-workflow/agent-start', 'tool-workflow/run-end', 'tool-workflow/run-start',
    'tool/call', 'tool/code-dispatch', 'tool/code-dispatch-start', 'tool/result', 'user/message', 'compaction/prune'))
UNREPORTED = frozenset(('llm/retry', 'session/title-llm-request', 'web/deepseek-search-llm-request'))
CHUNKS = frozenset(('block-start', 'text-delta', 'reasoning-delta', 'tool-call-delta', 'block-end'))


def capture_dsh_sources(paths, previous, *, root):
    """Freeze compressed bytes, rejecting symlinked files or ancestor directories."""
    complete = all(value is not None and fingerprint(path) == value for path, value in previous.items())
    logs = []
    for path in sorted(paths, key=str):
        relative = path.relative_to(root)
        safe = not root.is_symlink() and not any((root/parent).is_symlink() for parent in
                                                (relative, *relative.parents))
        before = fingerprint(path) if safe else None
        try:
            data = path.read_bytes() if before is not None else None
        except OSError:
            data = None
        if data is None or fingerprint(path) != before:
            complete = False
        logs.append({'name': relative.as_posix(), 'data': None if data is None else base64.b64encode(data).decode('ascii')})
    return canonical_json({'version': 1, 'capture_complete': complete, 'logs': logs}).encode('utf-8')


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _tokens(raw):
    raw = raw if type(raw) is dict else {}
    parts = [_count(raw.get('inputTokens')), _count(raw.get('cacheReadTokens', 0)),
             _count(raw.get('cacheWriteTokens', 0))]
    return (None if None in parts else sum(parts), _count(raw.get('outputTokens')))


def _fold(header, events):
    """Fold only this file's own suffix; a later lifecycle marker cannot erase work."""
    seed = header.get('seedLength', 0)
    require('parentSession' not in header or 'seedLength' in header, 'DSH fork lacks its seed boundary')
    samples, turn_ids, step_ids, compactions, retries = [], set(), set(), {}, set()
    turn, step, current = None, None, None
    complete = True
    for event in events[seed:]:
        kind, data = event['type'], event['data']
        if kind == 'turn/start':
            value = _count(data.get('turn'))
            require(turn is None and value is not None and value not in turn_ids, 'invalid DSH turn start')
            turn = value
            turn_ids.add(turn)
        elif kind == 'step/start':
            value = _count(data.get('step'))
            require(turn is not None and type(data.get('turn')) is int and data['turn'] == turn and step is None and value is not None
                    and (turn, value) not in step_ids, 'invalid DSH step start')
            step = value
            step_ids.add((turn, step))
            current = {'usage': None, 'message': False, 'finish': None}
        elif kind in ('assistant/chunk', 'assistant/message', 'step/end'):
            require(current is not None and type(data.get('turn')) is int and type(data.get('step')) is int
                    and data['turn'] == turn and data['step'] == step,
                    'DSH event has no matching open step')
            if kind == 'assistant/chunk':
                require(not current['message'], 'DSH chunk follows finalized message')
                chunk = data['chunk']
                if chunk['type'] == 'usage':
                    current['usage'] = chunk.get('usage')
                elif chunk['type'] == 'finish':
                    current['finish'] = chunk['reason']['kind']
                    complete = complete and current['finish'] in ('stop', 'tool-calls', 'max-tokens')
                else:
                    require(chunk['type'] in CHUNKS, 'unsupported DSH chunk')
            elif kind == 'assistant/message':
                require(not current['message'], 'duplicate DSH finalized message')
                current['message'] = True
                if 'usage' in data:
                    current['usage'] = data['usage']
            else:
                complete = complete and current['message'] and current['finish'] in ('stop', 'tool-calls', 'max-tokens')
                samples.append(('steps', _tokens(current['usage'])))
                step, current = None, None
        elif kind == 'turn/end':
            require(turn is not None and type(data.get('turn')) is int and data['turn'] == turn and step is None,
                    'invalid DSH turn end')
            complete = complete and data['reason']['kind'] in ('completed', 'max-tokens')
            turn = None
        elif kind == 'compaction/start':
            identity = data['compactionId']
            require(isinstance(identity, str) and identity and identity not in compactions, 'invalid DSH compaction start')
            compactions[identity] = {'summary': False, 'closed': False}
        elif kind in ('compaction/summary', 'compaction/end'):
            compaction = compactions[data['compactionId']]
            require(not compaction['closed'], 'DSH compaction already closed')
            if kind == 'compaction/summary':
                require(not compaction['summary'], 'duplicate DSH summary')
                compaction['summary'] = True
                samples.append(('summaries', _tokens(data.get('usage'))))
            else:
                compaction['closed'] = True
                if 'error' in data or not compaction['summary']:
                    samples.append(('unreported', (None, None)))
        elif kind in UNREPORTED:
            # Failed attempts and title/search requests have no complete receipt.
            if kind == 'llm/retry':
                identity = data['retryId']
                require(isinstance(identity, str) and identity and identity not in retries, 'invalid DSH retry identity')
                retries.add(identity)
            samples.append(('unreported', (None, None)))
        elif kind == 'llm/retry-started':
            require(data['retryId'] in retries, 'DSH retry has no preceding failure record')
        else:
            require(kind in IGNORED and 'usage' not in data, 'unsupported DSH event')
    complete = complete and turn is None and step is None and all(c['closed'] for c in compactions.values())
    return samples, complete, seed


def dsh_fields(source, *, transport_complete):
    require(source is None or type(source) is bytes, 'DSH usage source must be bytes')
    fields = {'source_hash': None if source is None else 'sha256:'+hashlib.sha256(source).hexdigest(),
        'source_bytes': None if source is None else len(source), 'transport_complete': transport_complete,
        'stream_status': 'unavailable', 'session_ids': [], 'logs': 0, 'seed_events': 0,
        'samples': dict.fromkeys(KINDS, 0), 'unknown_samples': {'input_tokens': 0, 'output_tokens': 0},
        'known_tokens': {'input_tokens': 0, 'output_tokens': 0}, 'input_tokens': None, 'output_tokens': None}
    if source is None:
        return fields
    samples, names = [], set()
    try:
        bundle = json.loads(source.decode('utf-8'))
        require(type(bundle) is dict and set(bundle) == {'version', 'capture_complete', 'logs'}
                and type(bundle['version']) is int and bundle['version'] == 1
                and type(bundle['capture_complete']) is bool and type(bundle['logs']) is list, 'invalid DSH source bundle')
        complete = bundle['capture_complete']
        for log in bundle['logs']:
            require(type(log) is dict and set(log) == {'name', 'data'} and type(log['name']) is str,
                    'invalid DSH source log')
            path = PurePosixPath(log['name'])
            require(not path.is_absolute() and '..' not in path.parts and '\\' not in log['name']
                    and path.as_posix() == log['name'] and path.name == 'session.jsonl.zstd'
                    and log['name'] not in names, 'invalid DSH source path')
            names.add(log['name'])
            fields['logs'] += 1
            if log['data'] is None:
                complete = False
                continue
            require(type(log['data']) is str, 'invalid DSH source encoding')
            header, events = decode_log(decompress_complete(base64.b64decode(log['data'], validate=True)))
            require(header['id'] not in fields['session_ids'], 'duplicate DSH session identity')
            fields['session_ids'].append(header['id'])
            values, finished, seed = _fold(header, events)
            complete = complete and finished
            fields['seed_events'] += seed
            samples.extend(values)
        for kind, tokens in samples:
            fields['samples'][kind] += 1
            for key, value in zip(('input_tokens', 'output_tokens'), tokens):
                fields['unknown_samples'][key] += value is None
                fields['known_tokens'][key] += value or 0
        fields['stream_status'] = 'reported' if complete and samples and fields['logs'] else 'incomplete'
        if transport_complete and fields['stream_status'] == 'reported':
            for key in ('input_tokens', 'output_tokens'):
                if not fields['unknown_samples'][key]:
                    fields[key] = fields['known_tokens'][key]
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        fields.update(stream_status='invalid', input_tokens=None, output_tokens=None)
    return fields


def dsh_reported(fields, *, wall_seconds):
    require(type(fields) is dict and set(fields) == {'source_hash', 'source_bytes', 'transport_complete',
        'stream_status', 'session_ids', 'logs', 'seed_events', 'samples', 'unknown_samples', 'known_tokens',
        'input_tokens', 'output_tokens'}, 'invalid DSH usage fields')
    require(type(fields['transport_complete']) is bool and fields['stream_status'] in
        ('unavailable', 'incomplete', 'reported', 'invalid'), 'invalid DSH usage status')
    require(type(fields['session_ids']) is list and all(isinstance(value, str) and value.strip()
        for value in fields['session_ids']) and len(fields['session_ids']) == len(set(fields['session_ids'])),
        'invalid DSH session identities')
    require(type(fields['logs']) is int and fields['logs'] >= len(fields['session_ids'])
            and _count(fields['seed_events']) is not None, 'invalid DSH log counts')
    require(type(fields['samples']) is dict and set(fields['samples']) == set(KINDS)
            and all(_count(value) is not None for value in fields['samples'].values()), 'invalid DSH sample counts')
    count = sum(fields['samples'].values())
    for key in ('known_tokens', 'unknown_samples'):
        require(type(fields[key]) is dict and set(fields[key]) == {'input_tokens', 'output_tokens'}
                and all(_count(value) is not None for value in fields[key].values()), 'invalid DSH token breakdown')
    require(all(value <= count for value in fields['unknown_samples'].values()), 'invalid DSH unknown counts')
    if fields['stream_status'] == 'unavailable':
        require(fields['source_hash'] is None and fields['source_bytes'] is None and not fields['logs']
                and not fields['session_ids'] and not count, 'unavailable DSH usage has a source')
    else:
        require(isinstance(fields['source_hash'], str) and re.fullmatch(r'sha256:[0-9a-f]{64}', fields['source_hash'])
                and _count(fields['source_bytes']) is not None, 'invalid DSH usage source identity')
    if fields['stream_status'] == 'reported':
        require(fields['logs'] == len(fields['session_ids']) and count > 0, 'DSH usage has no complete sessions')
    for key in ('input_tokens', 'output_tokens'):
        expected = fields['known_tokens'][key] if fields['transport_complete'] and fields['stream_status'] == 'reported' \
            and not fields['unknown_samples'][key] else None
        require(fields[key] == expected, 'DSH usage total differs from its coverage')
    return Usage(input_tokens=fields['input_tokens'], output_tokens=fields['output_tokens'], wall_seconds=wall_seconds)


def verify_dsh_source(evidence, source):
    expected = dsh_fields(source, transport_complete=evidence['fields']['transport_complete'])
    require(expected == evidence['fields'], 'DSH usage source differs from normalized evidence')
