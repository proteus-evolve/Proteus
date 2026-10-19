"""Repair a rejected source candidate across real controller processes and Docker CLIs."""
from dataclasses import asdict
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from check_native_source_docker import ENTRY, MARKER, PINS, Provider, adapter_for, marked
from proteus.core import GoalConfig, NEUTRAL, snapshot
from proteus.core.episode import RunConfig, completed_episodes, eval_history_path, pending_candidate_path, run
from proteus.core.native_activation import capture_activation, source_tree_hash
from proteus.core.native_inputs import read_native_inputs
from proteus.core.serialization import content_hash


BROKEN = 'this is deliberately invalid TypeScript !!!\n'


class RepairProvider(Provider):
    def response(self, body):
        assert body['model'] == 'fixture' and body['stream'] is True
        ordinal = len(self.requests)
        assert ordinal < 7, 'unexpected retry or repeated completed episode'
        tools = {item['function']['name']: item['function'] for item in body.get('tools', [])}
        self.requests.append({'ordinal': ordinal, 'body_hash': content_hash(body),
                              'tools': sorted(tools), 'roles': [m['role'] for m in body['messages']]})
        if ordinal == 0:
            self.initial = self.source.read_text()
            self.modified = marked(self.initial)
        if ordinal in (0, 1, 3, 4):
            expected = self.initial if ordinal < 3 else BROKEN
            assert self.source.read_text() == expected, 'repair did not receive the exact failed candidate'
            name = 'read' if ordinal in (0, 3) else 'write'
            properties = tools[name]['parameters']['properties']
            path_key = 'file_path' if 'file_path' in properties else 'path'
            assert path_key in properties
            args = {path_key: self.target}
            if name == 'write':
                reads = [m for m in body['messages'] if m.get('role') == 'tool'
                         and m.get('tool_call_id') == f'call_repair_{ordinal-1}']
                assert len(reads) == 1, 'write requires the matching actual read result'
                witness = next(line for line in expected.splitlines() if len(line.strip()) > 8)
                assert json.dumps(witness, ensure_ascii=False)[1:-1] in json.dumps(
                    reads[0]['content'], ensure_ascii=False), 'read did not expose the expected candidate'
                self.requests[-1]['read_witness_sha256'] = hashlib.sha256(witness.encode()).hexdigest()
                args['content'] = BROKEN if ordinal == 1 else self.modified
            return {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': f'call_repair_{ordinal}',
                'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}, 'tool_calls'
        assert self.source.read_text() == (BROKEN if ordinal == 2 else self.modified)
        return {'role': 'assistant', 'content': 'REPAIR-FIXTURE-COMPLETE'}, 'stop'


def controller(backend, image, root, url, mode):
    """Each invocation reconstructs the controller and adapter from saved files."""
    adapter, _ = adapter_for(backend, image, root, url, resume=mode != 'first')
    cfg = RunConfig(name=backend+'-source-repair', adapter=adapter, disposition=NEUTRAL,
        goal=GoalConfig.of(text='Read and edit the candidate source using the requested tools.'),
        root=root/'run', model='fixture', episodes=1 if mode == 'first' else 3,
        phases=('act',), max_turns=12,
        phase_prompts={'act': 'Read the candidate CLI entrypoint and apply the requested source edit.'})
    start = 0 if mode == 'first' else completed_episodes(cfg)
    assert start == {'first': 0, 'repair': 1, 'complete': 3}[mode]
    result = run(cfg, start=start, resume=mode != 'first')
    rows = read_native_inputs(cfg.root)
    pending = pending_candidate_path(cfg.root)
    record = {'pid': os.getpid(), 'start': start, 'result': asdict(result),
        'pending': json.loads(pending.read_text()) if pending.exists() else None,
        'native_record_hashes': [content_hash({'input': row['input'], 'outcome': row['outcome']}) for row in rows]}
    (root/f'controller-{mode}.json').write_text(json.dumps(record, indent=2)+'\n')
    assert result.episodes_complete == cfg.episodes and not result.error, asdict(result)


def source_at(root, commit, backend):
    assert re.fullmatch(r'[0-9a-f]{40}', commit)
    with tempfile.TemporaryDirectory(prefix='proteus-repair-snapshot-') as directory:
        tree = Path(directory)/'tree'
        snapshot.materialize(root/'run/harness', commit, tree)
        return source_tree_hash(tree/'src'), (tree/'src'/ENTRY[backend]).read_text()


def require_rejected_candidate(calls):
    failed = [call for call in calls if call['returncode'] != 0]
    assert len(failed) == 1, 'expected exactly one failed source validation'
    call = failed[0]
    assert call['command'] == ['--version'] and call['returncode'] == 97
    assert 'self-edited source does not build' in call['stderr'], 'failure was not the source compiler'
    assert any(mount[0].endswith('/run/harness') and mount[1] == '/workspace'
               for mount in call['mounts']), 'compiler failure belongs to another candidate'


def controller_reports(root):
    """Require three successful controller processes, including an unchanged completed resume."""
    reports = {mode: json.loads((root/f'controller-{mode}.json').read_text())
               for mode in ('first', 'repair', 'complete')}
    processes = json.loads((root/'controller-processes.json').read_text())
    assert [p['mode'] for p in processes] == ['first', 'repair', 'complete']
    assert len({p['pid'] for p in processes}) == 3
    assert all(p['returncode'] == 0 and p['pid'] == reports[p['mode']]['pid'] for p in processes)
    assert [reports[m]['start'] for m in reports] == [0, 1, 3]
    assert [report['result']['episodes_complete'] for report in reports.values()] == [1, 3, 3]
    assert all(not report['result']['error'] for report in reports.values())
    return tuple(reports.values())


def verify(root, *, expected_head=None):
    """Inspect saved native receipts, rejected/accepted snapshots and raw process outputs."""
    root = Path(root)
    evidence = json.loads((root/'evidence.json').read_text())
    assert evidence['evidence_hash'] == content_hash({k: v for k, v in evidence.items() if k != 'evidence_hash'})
    if expected_head is not None:
        assert evidence['proteus_head'] == expected_head
    backend = evidence['backend']
    metadata = json.loads((root/'image.json').read_text())[0]
    assert evidence['source_revision'] == metadata['Config']['Labels']['org.opencontainers.image.revision'] == PINS[backend]
    assert metadata['Id'] == evidence['image_id']
    first, repaired, complete = controller_reports(root)
    history = json.loads(eval_history_path(root/'run').read_text())
    assert history == repaired['result']['eval_history'] == complete['result']['eval_history']
    assert first['result']['eval_history'] == history[:1]
    assert [row['episode'] for row in history] == [1, 2, 3]
    assert [row['accepted'] for row in history] == [False, True, True]
    assert [row['failure_kind'] for row in history] == ['viability', '', '']
    pending = first['pending']
    assert pending == {'version': 1, 'commit': history[0]['candidate_commit'],
                       'resume_episode': 2, 'reason': 'viability', 'error': history[0]['error'][:2000]}
    assert repaired['pending'] is complete['pending'] is None
    assert not pending_candidate_path(root/'run').exists()
    assert not (root/'run/.proteus-state/controller-notice.md').exists()
    initial = (root/'initial-entry.ts').read_text()
    modified = marked(initial)
    assert evidence['initial_entry_sha256'] == hashlib.sha256(initial.encode()).hexdigest()
    assert evidence['modified_entry_sha256'] == hashlib.sha256(modified.encode()).hexdigest()
    checkpoints = [snapshot.commit_for_episode(root/'run/harness', ep) for ep in range(4)]
    states = [source_at(root, commit, backend) for commit in checkpoints]
    assert [text for _, text in states] == [initial, initial, modified, modified]
    assert states[0][0] == states[1][0] != states[2][0] == states[3][0]
    # Uploaded working files can lose executable bits. Hash materialized Git
    # snapshots above; their source modes survive artifact ZIP transport.
    failed_hash, failed_text = source_at(root, pending['commit'], backend)
    assert failed_text == BROKEN and failed_hash != states[0][0]
    assert pending['commit'] not in checkpoints
    rows = read_native_inputs(root/'run')
    assert len(rows) == 3 and [row['input']['episode'] for row in rows] == [1, 2, 3]
    hashes = [content_hash({'input': row['input'], 'outcome': row['outcome']}) for row in rows]
    assert first['native_record_hashes'] == hashes[:1]
    assert repaired['native_record_hashes'] == complete['native_record_hashes'] == hashes
    assert history[0]['error'][:200] in '\n'.join(rows[1]['input']['payload'])
    calls = json.loads((root/'docker-calls.json').read_text())
    require_rejected_candidate(calls)
    phases = [call for call in calls if call['command'][0] == '--proteus-expect-source']
    assert len(phases) == 3
    activations = []
    for index, (row, call) in enumerate(zip(rows, phases)):
        assert row['completion'] == 'returned' and row['outcome']['returncode'] == call['returncode'] == 0
        assert call['command'] == row['input']['payload']
        assert any(mount[1:] == ['/workspace', 'ro'] for mount in call['mounts'])
        assert any(mount[0].endswith('/run/harness') and mount[1:] == ['/workspace/candidate']
                   for mount in call['mounts'])
        activation = capture_activation(call['stdout'], call['command'], backend)
        assert activation == row['outcome']['activation']
        assert activation['source_sha256'] == states[index][0]
        assert (MARKER in call['stderr'].splitlines()) == (index == 2)
        activations.append(activation)
    assert [a['mode'] for a in activations] == ['pristine', 'pristine', 'cache']
    assert activations[0]['cli_sha256'] == activations[1]['cli_sha256'] != activations[2]['cli_sha256']
    assert activations == evidence['activation']
    provider = json.loads((root/'provider.json').read_text())
    assert not provider['errors'] and provider['requests'] == evidence['requests']
    assert len(provider['requests']) == 7
    assert [request['ordinal'] for request in provider['requests']] == list(range(7))
    for ordinal, text in ((1, initial), (4, BROKEN)):
        witness = next(line for line in text.splitlines() if len(line.strip()) > 8)
        assert provider['requests'][ordinal]['read_witness_sha256'] == hashlib.sha256(witness.encode()).hexdigest()
    return evidence


def check(backend, image, root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    assert not (root/'run').exists(), 'use a fresh repair output directory'
    metadata = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True))[0]
    assert metadata['Config']['Labels']['org.opencontainers.image.revision'] == PINS[backend]
    (root/'image.json').write_text(json.dumps([metadata], indent=2)+'\n')
    provider = RepairProvider(root/'run/harness/src'/ENTRY[backend], '/workspace/candidate/src/'+ENTRY[backend])
    url = provider.start()
    processes = []
    try:
        for mode in ('first', 'repair', 'complete'):
            command = [sys.executable, str(Path(__file__).resolve()), '--backend', backend,
                       '--image', image, '--out', str(root), '--controller', mode, '--url', url]
            with (root/f'controller-{mode}.log').open('w') as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                try:
                    code = process.wait(timeout=1800)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
            processes.append({'mode': mode, 'pid': process.pid, 'returncode': code})
            (root/'controller-processes.json').write_text(json.dumps(processes, indent=2)+'\n')
            assert code == 0, f'{mode} controller failed; inspect controller-{mode}.log'
            assert not provider.errors, provider.errors
            assert len(provider.requests) == {'first': 3, 'repair': 7, 'complete': 7}[mode]
        rows = read_native_inputs(root/'run')
        assert source_tree_hash(root/'run/harness/src') == rows[-1]['outcome']['activation']['source_sha256']
        evidence = {'schema_version': 1, 'backend': backend, 'source_revision': PINS[backend],
            'image_id': metadata['Id'], 'proteus_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
            'initial_entry_sha256': hashlib.sha256(provider.initial.encode()).hexdigest(),
            'modified_entry_sha256': hashlib.sha256(provider.modified.encode()).hexdigest(),
            'requests': provider.requests, 'activation': [row['outcome']['activation'] for row in rows],
            'scope': 'Actual failed source candidate, separate-process repair/resume and next-episode activation; deterministic provider, no model-quality claim'}
        evidence['evidence_hash'] = content_hash(evidence)
        (root/'initial-entry.ts').write_text(provider.initial)
        (root/'evidence.json').write_text(json.dumps(evidence, indent=2)+'\n')
    finally:
        provider.close()
        (root/'provider.json').write_text(json.dumps({'requests': provider.requests, 'errors': provider.errors}, indent=2)+'\n')
    verified = verify(root)
    print(json.dumps({'backend': backend, 'evidence_hash': verified['evidence_hash'], 'requests': 7,
                      'controller_processes': 3, 'failed_then_repaired': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=tuple(PINS), required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--controller', choices=('first', 'repair', 'complete'))
    parser.add_argument('--url')
    args = parser.parse_args()
    if args.controller:
        assert args.url and args.url.startswith('http://127.0.0.1:')
        controller(args.backend, args.image, Path(args.out), args.url, args.controller)
    else:
        check(args.backend, args.image, args.out)
