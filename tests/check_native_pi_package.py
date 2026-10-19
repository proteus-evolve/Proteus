"""Installed Pi adapter/session accounting with a deterministic transport fixture."""
import json
from pathlib import Path
import subprocess
import tempfile
import uuid

from proteus.adapters.pi import PiHarness
from proteus.core.adapter import EpisodeSpec
from proteus.core.native_inputs import input_directory, read_native_inputs
from proteus.core.native_usage import summarize_native_inputs


def usage(incoming, outgoing, read=0, write=0):
    return {'input': incoming, 'output': outgoing, 'cacheRead': read, 'cacheWrite': write,
            'cacheWrite1h': write, 'reasoning': 1, 'totalTokens': incoming+outgoing+read+write,
            'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'total': 99}}


def entries(identity=None):
    values = [{'type': 'session', 'version': 3, 'id': identity or str(uuid.uuid4()),
               'timestamp': '2026-01-01T00:00:00Z', 'cwd': '/workspace'}]
    for index, entry in enumerate([
        {'type': 'message', 'message': {'role': 'assistant', 'provider': 'fixture', 'model': 'fixture',
            'content': [], 'usage': usage(11, 7, 5, 2), 'stopReason': 'toolUse'}},
        {'type': 'message', 'message': {'role': 'toolResult', 'content': [], 'usage': usage(3, 2)}},
        {'type': 'compaction', 'summary': 'PRIVATE-SUMMARY', 'usage': usage(4, 3, 1)},
        {'type': 'branch_summary', 'summary': 'PRIVATE-SUMMARY', 'usage': usage(2, 1, 0, 1)},
        {'type': 'message', 'message': {'role': 'assistant', 'provider': 'fixture', 'model': 'fixture',
            'content': [{'type': 'text', 'text': 'PRIVATE-ANSWER'}], 'usage': usage(1, 1), 'stopReason': 'stop'}},
    ], 1):
        values.append({**entry, 'id': str(index), 'parentId': str(index-1) if index > 1 else None,
                       'timestamp': '2026-01-01T00:00:00Z'})
    return values


def raw(values):
    return ''.join(json.dumps(value)+'\n' for value in values).encode()


def check():
    with tempfile.TemporaryDirectory(prefix='proteus-native-pi-') as directory:
        root = Path(directory)
        (root/'harness').mkdir()
        sources = []

        class Sandbox:
            def run(self, run_root, command, env, timeout_s, mounts=(), stop_check=None):
                values = entries()
                source = raw(values)
                (run_root/'.pi-state'/(values[0]['id']+'.jsonl')).write_bytes(source)
                sources.append(source)
                return subprocess.CompletedProcess(command, 0, '', '')

        adapter = PiHarness(key='local-fixture-only', sandbox=Sandbox())
        request = EpisodeSpec(root=root, episode=1, model='fixture', phases=('act',), phase_prompts={'act':'fixture'})
        assert adapter.run_episode(request).ok and adapter.run_episode(request).ok
        records = read_native_inputs(root)
        assert len(records) == 2
        totals = summarize_native_inputs(records)
        assert totals['reported']['input_tokens'] == 60 and totals['reported']['output_tokens'] == 28
        assert totals['reported']['model_calls'] is None and totals['reported']['cost_usd'] is None
        assert all(row['outcome']['usage']['fields']['samples'] ==
                   {'assistant':2, 'toolResult':1, 'compaction':1, 'branch_summary':1} for row in records)
        assert len(list(input_directory(root).glob('*.usage-source'))) == 2
        assert 'PRIVATE-ANSWER' not in json.dumps(records) and 'PRIVATE-SUMMARY' not in json.dumps(totals)
        print('Installed Pi adapter: two fresh v3 logs, cache-inclusive tokens, four usage categories, immutable sources and unknown costs passed')


if __name__ == '__main__':
    check()
