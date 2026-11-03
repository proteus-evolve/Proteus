import hashlib
import json

import pytest

from proteus.core import snapshot
from scripts.release_smoke_diagnostics import Collector, ENTRY_FILES, collect, edit_goal, sanitize


def _payloads(output):
    return b'\n'.join(p.read_bytes() for p in output.rglob('*') if p.is_file())


def _run(root, backend='dsh'):
    run = root / 'runs/run-one'
    entry = run / 'harness' / ENTRY_FILES[backend]
    entry.parent.mkdir(parents=True)
    entry.write_text('#!/usr/bin/env node\nstart();\n')
    snapshot.init(run / 'harness')
    entry.write_text('#!/usr/bin/env node\n// PROTEUS-NOTE\nstart();\n')
    snapshot.commit(run / 'harness', 'episode 1: source edit')
    snapshot.commit(run / 'harness', 'episode 2: unchanged')
    (root / 'manifest.json').write_text(json.dumps({'episodes': 2, 'goal': 'edit'}))
    # Export must not follow the arbitrary root supplied by a result record.
    (root / 'seeds.jsonl').write_text(json.dumps({'root': '/outside/credential-directory',
        'episodes_complete': 2, 'error': ''}) + '\n')
    return run, entry


@pytest.mark.parametrize('backend,target', [
    ('dsh', '/workspace/candidate/src/apps/cli/src/bin.ts'),
    ('pi', '/workspace/candidate/src/packages/coding-agent/src/cli.ts')])
def test_goal_addresses_writable_candidate_for_each_pinned_backend(backend, target):
    goal = edit_goal(backend)
    assert target in goal and 'read-only at /workspace' in goal
    other = ENTRY_FILES['pi' if backend == 'dsh' else 'dsh']
    assert '/workspace/candidate/' + other not in goal
    assert 'immediately after the shebang' in goal and 'next episode' in goal


def test_secrets_and_reasoning_are_removed_including_nested_tool_arguments():
    gateway = 'random-gateway-credential-value'
    known = 'opaque-provider-credential'
    value = {'Authorization': 'Bearer gateway-value', 'apiKey': 'unshaped-key',
        'nested': json.dumps({'gatewayToken': gateway, 'visible': known}),
        'message': {'content': [{'type': 'thinking', 'thinking': 'private-chain'},
                                {'type': 'text', 'text': 'visible decision'}]},
        'arguments': 'API_KEY=some-unshaped-value TOKEN=other-token',
        'usage': {'input_tokens': 9, 'reasoning_tokens': 7, 'cost_usd': None}}
    output = json.dumps(sanitize(value, (known,)))
    assert all(s not in output for s in (gateway, known, 'gateway-value', 'unshaped-key',
                                         'some-unshaped-value', 'other-token', 'private-chain'))
    assert 'visible decision' in output
    assert sanitize(value, (known,))['usage'] == {'input_tokens': 9, 'reasoning_tokens': 7, 'cost_usd': None}


@pytest.mark.parametrize('kind', ['text-chunks', 'reasoning-chunks', 'tool-call-chunks', 'request/header'])
def test_packed_deltas_and_transport_headers_are_not_exported(kind):
    record = {'type': kind, 'data': {'texts': ['private-part-1', 'private-part-2']}}
    assert sanitize(record) == {'diagnostic_omitted_event': kind}


@pytest.mark.parametrize('backend', ['dsh', 'pi'])
def test_capture_preserves_checkpoints_and_partial_native_outcomes_without_mutation(tmp_path, backend):
    root, output = tmp_path / 'sweep', tmp_path / 'diagnostics'
    run, _ = _run(root, backend)
    private = root / 'runs/.proteus-records/run-one'
    native = private / 'native-inputs'
    native.mkdir(parents=True)
    (native / 'attempt.input.json').write_text(json.dumps({'payload': 'inspect /workspace/src',
        'api_key': 'a-provider-key', 'gateway_token': 'random-gateway-token'}))
    # Missing outcome stays missing, not a fabricated successful/zero-cost receipt.
    (private / 'pending_candidate.json').write_text(json.dumps({'cost_usd': None, 'error': 'gate failed'}))
    (native / 'attempt.usage-source').write_bytes(b'opaque private bytes a-provider-key')
    settings = run / f'.{backend}-state/settings.yaml'
    settings.parent.mkdir()
    settings.write_text('apiKey: must-not-copy-settings')
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    index = collect([root], output, backend, secrets=('a-provider-key',))
    assert before == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    payload = _payloads(output).decode()
    assert 'must-not-copy-settings' not in payload and 'random-gateway-token' not in payload
    assert 'a-provider-key' not in payload and 'opaque private bytes' not in payload
    assert 'ep0_to_ep1_src_name_status' in payload and 'PROTEUS-NOTE' in payload
    assert 'cost_usd' in payload and 'null' in payload
    missing = next(r for r in index['records'] if 'attempt.outcome.json' in r['source'])
    assert missing['status'] == 'missing' and missing['completion'] == 'unknown'
    assert any(r['status'] == 'hash_only_opaque_usage_source_not_uploaded' for r in index['records'])


def test_jsonl_keeps_native_tool_paths_and_hashes_unparseable_lines(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    trace = source / 'session.jsonl'
    trace.write_text(json.dumps({'type': 'tool/call', 'data': {'name': 'write',
        'arguments': json.dumps({'file_path': '/workspace/src/main.ts'})}}) + '\n' +
        json.dumps({'type': 'assistant/chunk', 'data': {'delta': 'private-chain'}}) + '\n' +
        '{unfinished-secret\n')
    collector = Collector(tmp_path / 'out')
    collector.capture(trace, source, 'trace', kind='jsonl')
    record = collector.records[0]
    assert record['original_sha256'] == hashlib.sha256(trace.read_bytes()).hexdigest()
    assert record['invalid_lines'][0]['line'] == 3
    output = _payloads(collector.output).decode()
    assert '/workspace/src/main.ts' in output and 'private-chain' not in output
    assert 'unfinished-secret' not in output


def test_symlinked_sources_are_never_read_or_hashed(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    secret = tmp_path / 'credential.json'
    secret.write_text('outside-private-material')
    link = source / 'traces'
    link.symlink_to(tmp_path, target_is_directory=True)
    collector = Collector(tmp_path / 'out')
    collector.capture(link / 'credential.json', source, 'unsafe')
    assert collector.records == [{'source': 'unsafe', 'kind': 'json', 'status': 'refused_symlink'}]
    assert 'outside-private-material' not in _payloads(collector.output).decode()


def test_missing_sweep_and_invalid_metadata_still_produce_diagnostics(tmp_path):
    output = tmp_path / 'diagnostics'
    index = collect([tmp_path / 'missing'], output, 'dsh')
    assert (output / 'diagnostics.json').is_file()
    assert all(r['status'] == 'missing' for r in index['records'])
    assert index['provider_calls_by_collector'] == 0


def test_output_byte_limit_does_not_silently_truncate_raw_records(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'manifest.json').write_text(json.dumps({'value': 'x' * 100}))
    collector = Collector(tmp_path / 'out', max_bytes=10)
    collector.capture(source / 'manifest.json', source, 'manifest')
    assert collector.records[0]['status'] == 'omitted_output_byte_limit'
    assert collector.records[0]['original_sha256']
    assert list(collector.output.iterdir()) == []


def test_oversized_input_is_explicitly_missing_a_full_hash(tmp_path, monkeypatch):
    from scripts import release_smoke_diagnostics as module
    monkeypatch.setattr(module, 'MAX_FILE_BYTES', 16)
    source = tmp_path / 'source'
    source.mkdir()
    path = source / 'huge.json'
    path.write_bytes(b'x' * 100)
    collector = Collector(tmp_path / 'out')
    collector.capture(path, source, 'huge')
    assert collector.records[0]['status'] == 'omitted_input_byte_limit'
    assert collector.records[0]['original_sha256'] is None
    assert list(collector.output.iterdir()) == []


def test_output_inside_sweep_is_rejected_before_creating_anything(tmp_path):
    with pytest.raises(ValueError, match='outside'):
        collect([tmp_path], tmp_path / 'artifact', 'dsh')
    assert not (tmp_path / 'artifact').exists()


def test_native_dsh_compressed_trace_is_sanitized_without_copying_compressed_bytes(tmp_path):
    try:
        from compression import zstd
    except ImportError:
        zstd = pytest.importorskip('zstandard')
    source = tmp_path / 'source'
    source.mkdir()
    path = source / 'session.jsonl.zstd'
    raw = (json.dumps({'type': 'tool/call', 'data': {'name': 'write',
        'arguments': json.dumps({'file_path': '/workspace/candidate/src/apps/cli/src/bin.ts',
                                'gatewayToken': 'secret-gateway-value'})}}) + '\n').encode()
    path.write_bytes(zstd.compress(raw))
    collector = Collector(tmp_path / 'out')
    collector.capture(path, source, 'dsh-session', kind='dsh')
    assert collector.records[0]['status'] == 'retained_sanitized_derivative'
    payload = _payloads(collector.output)
    assert b'/workspace/candidate/' in payload and b'secret-gateway-value' not in payload
    assert path.read_bytes() not in payload


def test_multiple_dsh_frames_and_decompression_limit(tmp_path, monkeypatch):
    try:
        from compression import zstd
    except ImportError:
        zstd = pytest.importorskip('zstandard')
    from scripts import release_smoke_diagnostics as module
    source = tmp_path / 'source'
    source.mkdir()
    path = source / 'session.jsonl.zstd'
    first = b'{"type":"tool/call","data":{"name":"read"}}\n'
    second = b'{"type":"tool/call","data":{"name":"write"}}\n'
    path.write_bytes(zstd.compress(first) + zstd.compress(second))
    collector = Collector(tmp_path / 'full')
    collector.capture(path, source, 'frames', kind='dsh')
    assert b'"read"' in _payloads(collector.output) and b'"write"' in _payloads(collector.output)
    monkeypatch.setattr(module, 'MAX_DECODED_BYTES', 32)
    bounded = Collector(tmp_path / 'bounded')
    bounded.capture(path, source, 'frames', kind='dsh')
    assert bounded.records[0]['status'] == 'omitted_decompressed_byte_limit'
    assert list(bounded.output.iterdir()) == []


def test_invalid_line_samples_are_bounded_even_when_data_output_is_omitted(tmp_path):
    from scripts.release_smoke_diagnostics import MAX_INVALID_LINE_SAMPLES
    source = tmp_path / 'source'
    source.mkdir()
    path = source / 'bad.jsonl'
    raw = b'x\n' * 4096
    path.write_bytes(raw)
    collector = Collector(tmp_path / 'out', max_bytes=0)
    collector.capture(path, source, 'bad', kind='jsonl')
    record = collector.records[0]
    assert record['status'] == 'omitted_output_byte_limit'
    assert record['original_sha256'] == hashlib.sha256(raw).hexdigest()
    assert record['jsonl_line_count'] == record['invalid_line_count'] == 4096
    assert record['jsonl_valid_line_count'] == 0
    assert len(record['invalid_lines']) == MAX_INVALID_LINE_SAMPLES
    assert record['invalid_line_samples_omitted'] == 4096 - MAX_INVALID_LINE_SAMPLES
    assert record['invalid_line_samples_truncated'] is True
    assert record['invalid_lines'][0] == {'line': 1, 'sha256': hashlib.sha256(b'x').hexdigest()}


def test_compressed_dsh_invalid_line_amplification_has_full_counts_and_bounded_index(tmp_path):
    try:
        from compression import zstd
    except ImportError:
        zstd = pytest.importorskip('zstandard')
    from scripts.release_smoke_diagnostics import MAX_INDEX_BYTES, MAX_INVALID_LINE_SAMPLES
    root, output = tmp_path / 'sweep', tmp_path / 'diagnostics'
    path = root / 'runs/run-one/.dsh-state/sessions/session-one/session.jsonl.zstd'
    path.parent.mkdir(parents=True)
    count = 131072
    compressed = zstd.compress(b'x\n' * count)
    path.write_bytes(compressed)
    index = collect([root], output, 'dsh', max_bytes=0)
    record = next(r for r in index['records'] if r['kind'] == 'dsh')
    assert record['original_sha256'] == hashlib.sha256(compressed).hexdigest()
    assert record['jsonl_line_count'] == record['invalid_line_count'] == count
    assert record['jsonl_valid_line_count'] == 0
    assert len(record['invalid_lines']) == MAX_INVALID_LINE_SAMPLES
    assert record['invalid_line_samples_omitted'] == count - MAX_INVALID_LINE_SAMPLES
    assert record['invalid_line_samples_truncated'] is True
    assert record['status'] == 'omitted_output_byte_limit'
    assert index['invalid_line_summary']['count'] == count
    assert index['output_data_bytes'] == 0
    assert (output / 'diagnostics.json').stat().st_size <= MAX_INDEX_BYTES


def test_global_index_cap_preserves_hashes_missing_outcomes_counts_and_redaction(tmp_path):
    from scripts.release_smoke_diagnostics import MAX_INDEX_BYTES, MAX_OUTPUT_BYTES
    root, output = tmp_path / 'sweep', tmp_path / 'diagnostics'
    state = root / 'runs/run-one/.pi-state'
    state.mkdir(parents=True)
    raw = b'x\n' * 128 + b'{"apiKey":"unshaped-credential","cost_usd":null}\n'
    for number in range(80):
        (state / f'session-{number:03d}.jsonl').write_bytes(raw)
    native = root / 'runs/.proteus-records/run-one/native-inputs'
    native.mkdir(parents=True)
    (native / 'attempt.input.json').write_text('{"payload":"opaque-provider-value"}')
    opaque = native / 'attempt.usage-source'
    opaque.write_bytes(b'RAW-OPAQUE-CAPTURE-UNIQUE-DO-NOT-EXPORT')
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    index = collect([root], output, 'pi', secrets=('opaque-provider-value',))
    rows = [r for r in index['records'] if r.get('jsonl_line_count')]
    assert len(rows) == 80
    assert all(r['invalid_line_count'] == 128 and r['jsonl_valid_line_count'] == 1 for r in rows)
    assert all(r['original_sha256'] == hashlib.sha256(raw).hexdigest() for r in rows)
    assert all(r['invalid_lines'] == [] and r['invalid_line_samples_omitted'] == 128
               and r['invalid_line_samples_truncated'] for r in rows)
    assert index['index_degradation'] == 'invalid_line_samples_omitted_for_index_limit'
    assert index['invalid_line_summary'] == {'count': 80 * 128, 'samples_retained': 0,
                                             'samples_omitted': 80 * 128}
    missing = next(r for r in index['records'] if r['source'].endswith('attempt.outcome.json'))
    assert missing['status'] == 'missing' and missing['completion'] == 'unknown'
    row = next(r for r in index['records'] if r['source'].endswith('attempt.usage-source'))
    assert row['status'] == 'hash_only_opaque_usage_source_not_uploaded'
    assert row['original_sha256'] == hashlib.sha256(opaque.read_bytes()).hexdigest()
    assert index['cost_unknowns_are_not_zero'] is True
    exported = _payloads(output)
    assert b'opaque-provider-value' not in exported and b'unshaped-credential' not in exported
    assert b'RAW-OPAQUE-CAPTURE-UNIQUE-DO-NOT-EXPORT' not in exported and b'"cost_usd": null' in exported
    assert (output / 'diagnostics.json').stat().st_size <= MAX_INDEX_BYTES
    assert sum(p.stat().st_size for p in output.rglob('*') if p.is_file()) <= MAX_OUTPUT_BYTES + MAX_INDEX_BYTES
    assert before == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_index_projects_oversized_context_without_losing_source_identity_or_missing_status(tmp_path):
    from scripts.release_smoke_diagnostics import MAX_INDEX_BYTES
    root, output = tmp_path / 'sweep', tmp_path / 'diagnostics'
    root.mkdir()
    data = b'{"cost_usd":null}\n'
    (root / 'manifest.json').write_bytes(data)
    index = collect([root], output, 'pi', context={'debug': 'x' * (MAX_INDEX_BYTES * 2),
                                                'apiKey': 'synthetic-private-key'})
    assert index['index_degradation'] == 'bounded_metadata_projection'
    assert index['execution_context']['omitted_for_index_limit'] is True
    manifest = next(r for r in index['records'] if r['source'].endswith('manifest.json'))
    assert manifest['original_sha256'] == hashlib.sha256(data).hexdigest()
    assert manifest['source_label_sha256']
    assert next(r for r in index['records'] if r['source'].endswith('seeds.jsonl'))['status'] == 'missing'
    assert index['cost_unknowns_are_not_zero'] is True
    assert b'synthetic-private-key' not in _payloads(output)
    assert (output / 'diagnostics.json').stat().st_size <= MAX_INDEX_BYTES
