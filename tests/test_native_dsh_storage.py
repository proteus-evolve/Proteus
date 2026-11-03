"""Accounting needs complete compressed frames and lossless packed event ranges."""
import copy
import json

import pytest

from proteus.core.native_dsh_storage import decode_log, decompress_complete, unpack_record
from proteus.core.serialization import ContractError


def compress(data):
    try:
        from compression import zstd
        return zstd.compress(data)
    except ImportError:
        zstandard = pytest.importorskip('zstandard')
        return zstandard.ZstdCompressor(write_content_size=False).compress(data)


def row(tag='text-chunks'):
    data = {'turn': 1, 'step': 2, 'index': 0, 'dt': [4, -2]}
    if tag == 'tool-call-chunks':
        data.update(id='call', name='read', args=['{"', 'path":', '"x"}'])
    else:
        data['texts'] = ['one', ' ', 'two']
    return {'type': tag, 'seq0': 0, 'time0': 100, 'data': data}


def log(records):
    header = {'type': 'session', 'version': 0, 'id': 'session', 'createdAt': 100, 'delegationDepth': 0}
    return ''.join(json.dumps(value)+'\n' for value in [header, *records]).encode()


def test_concatenated_frames_require_complete_tail_even_when_jsonl_is_already_complete():
    first, second = compress(b'first\n'), compress(b'second\n')
    assert decompress_complete(first+second) == b'first\nsecond\n'
    for data in (first+second[:-1], first+b'garbage', first[:3], b''):
        with pytest.raises(ContractError):
            decompress_complete(data)


@pytest.mark.parametrize('tag', ['text-chunks', 'reasoning-chunks', 'tool-call-chunks'])
def test_packed_rows_have_identical_expanded_sequences_and_timestamps(tag):
    packed = row(tag)
    values = unpack_record(packed)
    assert [entry['seq'] for entry in values] == [0, 1, 2]
    assert [entry['time'] for entry in values] == [100, 104, 102]
    _, direct = decode_log(log(values))
    _, expanded = decode_log(log([packed]))
    assert direct == expanded
    expected = {'text-chunks':'text-delta', 'reasoning-chunks':'reasoning-delta', 'tool-call-chunks':'tool-call-delta'}
    assert all(event['data']['chunk']['type'] == expected[tag] for event in expanded)
    assert decode_log(decompress_complete(compress(log([packed]))))[1] == direct


@pytest.mark.parametrize('mutation', ['extra_key', 'empty', 'gap_count', 'bool_gap', 'seq_overflow', 'time_overflow', 'bad_member'])
def test_invalid_packed_storage_cannot_hide_missing_events(mutation):
    value = copy.deepcopy(row())
    if mutation == 'extra_key':
        value['usage'] = {'input': 100}
    elif mutation == 'empty':
        value['data']['texts'] = []
    elif mutation == 'gap_count':
        value['data']['dt'] = [4]
    elif mutation == 'bool_gap':
        value['data']['dt'] = [True, -2]
    elif mutation == 'seq_overflow':
        value['seq0'] = 2**53-2
    elif mutation == 'time_overflow':
        value['time0'] = 2**53-2
    else:
        value['data']['texts'][0] = None
    with pytest.raises(ContractError):
        unpack_record(value)


@pytest.mark.parametrize('mutation', ['partial_line', 'missing_event', 'repeated_event', 'unsupported_version', 'missing_depth', 'seed_outside'])
def test_logs_require_supported_headers_and_contiguous_expanded_coverage(mutation):
    records = [json.loads(line) for line in log(unpack_record(row())).splitlines()]
    if mutation == 'missing_event':
        records.pop(2)
    elif mutation == 'repeated_event':
        records.append(records[-1])
    elif mutation == 'unsupported_version':
        records[0]['version'] = 1
    elif mutation == 'missing_depth':
        records[0].pop('delegationDepth')
    elif mutation == 'seed_outside':
        records[0]['seedLength'] = 4
    source = ''.join(json.dumps(value)+'\n' for value in records).encode()
    if mutation == 'partial_line':
        source = source[:-1]
    with pytest.raises(ContractError):
        decode_log(source)
