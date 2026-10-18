"""Strict DSH format-0 storage decoding for durable usage evidence.

Unlike live budget polling, accounting must reject an unfinished zstd frame.
Packed delta rows follow deepseek-harness@99f6f02fecdb7dff40c3fbc9470f5907c29f74ca.
"""

import json

from proteus.core.contracts import require


MAX_SAFE_INTEGER = 2**53-1
PACKED = {'text-chunks', 'reasoning-chunks', 'tool-call-chunks'}


def _integer(value, minimum=-MAX_SAFE_INTEGER):
    return type(value) is int and minimum <= value <= MAX_SAFE_INTEGER


def decompress_complete(source):
    """Decode concatenated frames, requiring an EOF for every physical frame."""
    require(type(source) is bytes and source, 'missing DSH compressed source')
    try:
        from compression import zstd
        factory = zstd.ZstdDecompressor
        error = zstd.ZstdError
    except ImportError:
        try:
            import zstandard
        except ImportError as exc:
            raise RuntimeError('DSH usage needs Python 3.14+ or zstandard>=0.21') from exc
        def factory():
            return zstandard.ZstdDecompressor().decompressobj()
        error = zstandard.ZstdError
    output, rest = [], source
    while rest:
        require(rest.startswith(b'\x28\xb5\x2f\xfd'), 'invalid DSH compressed frame magic')
        decoder = factory()
        try:
            block = decoder.decompress(rest)
        except error:
            require(False, 'invalid DSH compressed frame')
        require(decoder.eof, 'truncated DSH compressed frame')
        remaining = decoder.unused_data
        require(len(remaining) < len(rest), 'DSH frame consumed no bytes')
        output.append(block)
        rest = remaining
    return b''.join(output)


def unpack_record(record):
    """Expand the pinned storage vocabulary without altering ordinary events."""
    require(type(record) is dict and isinstance(record.get('type'), str), 'invalid DSH storage record')
    tag = record['type']
    if tag not in PACKED:
        return [record]
    require(set(record) == {'type', 'seq0', 'time0', 'data'}
            and _integer(record['seq0'], 0) and _integer(record['time0']), 'invalid DSH packed envelope')
    data = record['data']
    require(type(data) is dict, 'invalid DSH packed data')
    keys = {'turn', 'step', 'index', 'dt'}
    tool = tag == 'tool-call-chunks'
    if tool:
        keys |= {'id', 'args'}
        require(set(data) in (keys, keys | {'name'}) and isinstance(data['id'], str)
                and ('name' not in data or isinstance(data['name'], str)), 'invalid DSH packed tool')
    else:
        require(set(data) == keys | {'texts'}, 'invalid DSH packed text')
    require(all(type(data[key]) in (int, float) for key in ('turn', 'step', 'index')),
            'invalid DSH packed coordinates')
    members = data['args' if tool else 'texts']
    require(type(members) is list and members and all(type(value) is str for value in members),
            'invalid DSH packed members')
    require(type(data['dt']) is list and len(data['dt']) == len(members)-1
            and all(_integer(value) for value in data['dt']), 'invalid DSH packed time gaps')
    require(_integer(record['seq0']+len(members)-1, 0), 'DSH packed sequence overflow')
    events, time = [], record['time0']
    for index, member in enumerate(members):
        if index:
            time += data['dt'][index-1]
        require(_integer(time), 'DSH packed timestamp overflow')
        if tool:
            chunk = {'type': 'tool-call-delta', 'index': data['index'],
                     'id': data['id'], 'argumentsDelta': member}
            if 'name' in data:
                chunk['name'] = data['name']
        else:
            chunk = {'type': 'text-delta' if tag == 'text-chunks' else 'reasoning-delta',
                     'index': data['index'], 'text': member}
        events.append({'type': 'assistant/chunk', 'seq': record['seq0']+index, 'time': time,
                       'data': {'turn': data['turn'], 'step': data['step'], 'chunk': chunk}})
    return events


def decode_log(source):
    """Read complete JSONL bytes; validate format and expanded sequence coverage."""
    require(type(source) is bytes, 'DSH log must be bytes')
    lines = source.splitlines(keepends=True)
    require(lines and all(line.endswith(b'\n') and line.strip() for line in lines), 'truncated or empty DSH log')
    records = [json.loads(line.decode('utf-8'), parse_constant=lambda _: require(False, 'invalid DSH JSON number'))
               for line in lines]
    header = records[0]
    require(type(header) is dict and header.get('type') == 'session'
            and type(header.get('version')) is int and header['version'] == 0
            and isinstance(header.get('id'), str) and header['id'].strip()
            and _integer(header.get('createdAt'), 0) and _integer(header.get('delegationDepth'), 0),
            'invalid DSH session header')
    require('parentSession' not in header or isinstance(header['parentSession'], str)
            and header['parentSession'].strip(), 'invalid DSH parent identity')
    require('origin' not in header or header['origin'] == 'subagent', 'invalid DSH session origin')
    require(not set(header).intersection(('sandboxMode', 'approvalPolicy')), 'retired DSH session policy')
    seed = header.get('seedLength', 0)
    require(_integer(seed, 0), 'invalid DSH fork seed boundary')
    events = []
    for record in records[1:]:
        for event in unpack_record(record):
            require(_integer(event.get('seq'), 0) and event['seq'] == len(events)
                    and _integer(event.get('time')) and type(event.get('data')) is dict,
                    'invalid DSH event sequence or envelope')
            events.append(event)
    require(seed <= len(events), 'DSH fork seed extends beyond log')
    return header, events
