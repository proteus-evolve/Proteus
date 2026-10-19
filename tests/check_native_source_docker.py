"""Real pinned source images and coding tools, with a deterministic local provider.

Requires Linux Docker host networking. This exercises builds, native episodes and
activation, not model quality or live-provider billing. No real credential is read.
"""
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

from proteus.core import NEUTRAL, GoalConfig
from proteus.core.episode import RunConfig, completed_episodes, run
from proteus.core.native_inputs import read_native_inputs
from proteus.core.native_activation import capture_activation, source_tree_hash
from proteus.core.serialization import content_hash
from proteus.sandbox import DockerSandbox, SandboxConfig


PINS = {'pi': '914cf1472e715297caa30db4b9535d534a9eb718',
        'dsh': '99f6f02fecdb7dff40c3fbc9470f5907c29f74ca'}
ENTRY = {'pi': 'packages/coding-agent/src/cli.ts', 'dsh': 'apps/cli/src/bin.ts'}
MARKER = 'PROTEUS_SOURCE_ACTIVATION_WITNESS'


def marked(text):
    """Leave a possible shebang first and change observable compiled behavior."""
    assert MARKER not in text
    first, _, rest = text.partition('\n')
    prefix = first+'\n' if text.startswith('#!') else ''
    return prefix+'console.error("'+MARKER+'");\n'+(rest if prefix else text)


class Provider:
    def __init__(self, source, target):
        self.source, self.target = source, target
        self.requests, self.errors = [], []
        self.initial = self.modified = None

    def response(self, body):
        assert body['model'] == 'fixture' and body['stream'] is True
        tools = {item['function']['name']: item['function'] for item in body.get('tools', [])}
        ordinal = len(self.requests)
        assert ordinal < 4, 'unexpected retry/auxiliary provider request'
        self.requests.append({'ordinal': ordinal, 'body_hash': content_hash(body),
                              'tools': sorted(tools), 'roles': [m['role'] for m in body['messages']]})
        if ordinal < 2:
            name = ('read', 'write')[ordinal]
            properties = tools[name]['parameters']['properties']
            path_key = 'file_path' if 'file_path' in properties else 'path'
            assert path_key in properties
            args = {path_key: self.target}
            if ordinal == 0:
                self.initial = self.source.read_text()
                self.modified = marked(self.initial)
            else:
                assert body['messages'][-1]['role'] == 'tool', 'write must follow a real read result'
                witness = next(line for line in self.initial.splitlines() if len(line.strip()) > 8)
                assert json.dumps(witness, ensure_ascii=False)[1:-1] in json.dumps(
                    body['messages'][-1]['content'], ensure_ascii=False), 'read result lacks the actual source content'
                self.requests[-1]['read_witness_sha256'] = hashlib.sha256(witness.encode()).hexdigest()
                args['content'] = self.modified
            return {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'call_fixture_'+str(ordinal),
                'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}, 'tool_calls'
        if ordinal == 2:
            assert self.source.read_text() == self.modified, 'real write tool did not update the candidate'
        return {'role': 'assistant', 'content': 'SOURCE-FIXTURE-COMPLETE'}, 'stop'

    def start(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                try:
                    assert self.path == '/v1/chat/completions'
                    assert self.headers.get('Authorization') == 'Bearer fixture'
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    delta, finish = owner.response(body)
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    base = {'id': 'chatcmpl-source-fixture', 'object': 'chat.completion.chunk',
                            'created': int(time.time()), 'model': 'fixture'}
                    usage = {'prompt_tokens': 13, 'completion_tokens': 7, 'total_tokens': 20,
                             'prompt_cache_hit_tokens': 5, 'prompt_cache_miss_tokens': 8,
                             'prompt_tokens_details': {'cached_tokens': 5}}
                    for part in [
                        {'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}], 'usage': usage},
                        {'choices': [], 'usage': usage},
                    ]:
                        self.wfile.write(('data: '+json.dumps({**base, **part})+'\n\n').encode())
                    self.wfile.write(b'data: [DONE]\n\n')
                    self.wfile.flush()
                except Exception as exc:
                    owner.errors.append(repr(exc))
                    self.send_error(500)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


class RecordedDocker(DockerSandbox):
    """Record the production sandbox result; never replace commands or transports."""
    def __init__(self, config, root):
        super().__init__(config)
        self.root, self.calls = root, []

    def run(self, run_root, command, **kwargs):
        result = super().run(run_root, command, **kwargs)
        row = {'command': command, 'returncode': result.returncode,
               'stdout': result.stdout, 'stderr': result.stderr,
               'mounts': kwargs.get('mounts', ())}
        self.calls.append(row)
        (self.root/'docker-calls.json').write_text(json.dumps(self.calls, indent=2)+'\n')
        return result


def require_compiler_rejection(calls):
    rejected = [call for call in calls if call['command'] == ['--version']
                and any('/broken/' in mount[0] for mount in call['mounts'])]
    assert len(rejected) == 1, 'expected one broken-source build probe'
    call = rejected[0]
    assert call['returncode'] == 97 and 'self-edited source does not build' in call['stderr'], \
        'broken candidate must fail the source compiler, not Docker or an unrelated startup step'


def adapter_for(backend, image, root, url):
    common = dict(image=image, network='host', user=f'{os.getuid()}:{os.getgid()}',
                  mem_limit='6g', cpus='2', env_passthrough=('DEEPSEEK_API_KEY',))
    if backend == 'pi':
        from proteus.adapters.pi import PiHarness
        config = root/'fixture-config'
        config.mkdir()
        (config/'models.json').write_text(json.dumps({'providers': {'proteus_fixture': {
            'baseUrl': url, 'api': 'openai-completions', 'apiKey': 'fixture',
            'models': [{'id': 'fixture', 'name': 'Local fixture', 'reasoning': False,
                        'input': ['text'], 'contextWindow': 128000, 'maxTokens': 2000}]}}}))
        (config/'settings.json').write_text(json.dumps({'retry': {'enabled': False},
            'compaction': {'enabled': False}}))
        sandbox = RecordedDocker(SandboxConfig(**common,
            env={'PI_CODING_AGENT_DIR': '/proteus-fixture', 'PI_OFFLINE': '1', 'PI_TELEMETRY': '0'},
            extra_args=('--mount', f'type=bind,src={config},dst=/proteus-fixture,readonly')), root)
        return PiHarness(image=image, provider='proteus_fixture', model='fixture', key='fixture',
                         sandbox=sandbox, phase_timeout_s=600), sandbox
    from proteus.adapters.dsh import DshHarness
    state = root/'run/.dsh-state'
    state.mkdir(parents=True)
    (state/'settings.yaml').write_text(json.dumps({
        'agent-default-model': {'provider': 'deepseek-official', 'model': 'fixture'},
        'llm-deepseek': {'baseURL': url, 'apiKeyEnv': 'DEEPSEEK_API_KEY', 'thinking': 'disabled',
                       'reasoningEffort': 'off', 'maxTokens': 2000, 'streamIdleTimeoutMs': 10000}}))
    (state/'cordis.patch.yml').write_text('[{"id":"session-title-llm","disabled":true}]\n')
    sandbox = RecordedDocker(SandboxConfig(**common, env={
        'DSH_PERMISSION_MODE': 'workspace-write', 'DSH_TELEMETRY_MODE': 'DISABLED'}), root)
    # model=None preserves the explicitly prepared native settings file.
    return DshHarness(image=image, key='fixture', sandbox=sandbox, phase_timeout_s=600), sandbox


def verify(root, *, expected_head=None):
    """Re-read saved snapshots and raw startup receipts; no Docker or provider calls."""
    from proteus.core import snapshot
    root = Path(root)
    evidence = json.loads((root/'evidence.json').read_text())
    assert evidence['evidence_hash'] == content_hash({k: v for k, v in evidence.items() if k != 'evidence_hash'})
    if expected_head is not None:
        assert evidence['proteus_head'] == expected_head
    backend = evidence['backend']
    assert evidence['source_revision'] == PINS[backend]
    metadata = json.loads((root/'image.json').read_text())[0]
    assert metadata['Id'] == evidence['image_id']
    assert metadata['Config']['Labels']['org.opencontainers.image.revision'] == PINS[backend]
    rows = read_native_inputs(root/'run')
    assert len(rows) == 2 and [r['input']['episode'] for r in rows] == [1, 2]
    calls = json.loads((root/'docker-calls.json').read_text())
    phases = [call for call in calls if call['command'][0] == '--proteus-expect-source']
    assert len(phases) == 2
    activations = []
    with tempfile.TemporaryDirectory(prefix='proteus-source-evidence-') as directory:
        for row, call in zip(rows, phases):
            assert row['completion'] == 'returned' and row['outcome']['returncode'] == call['returncode'] == 0
            assert call['command'] == row['input']['payload']
            assert any(mount[1:] == ['/workspace', 'ro'] for mount in call['mounts'])
            activation = capture_activation(call['stdout'], call['command'], backend)
            assert activation == row['outcome']['activation']
            previous = row['input']['episode']-1
            commit = snapshot.commit_for_episode(root/'run/harness', previous)
            assert commit is not None
            tree = Path(directory)/str(previous)
            snapshot.materialize(root/'run/harness', commit, tree)
            assert activation['source_sha256'] == source_tree_hash(tree/'src')
            entry = (tree/'src'/ENTRY[backend]).read_bytes()
            assert hashlib.sha256(entry).hexdigest() == evidence[
                'initial_entry_sha256' if previous == 0 else 'modified_entry_sha256']
            activations.append(activation)
    assert activations == evidence['activation']
    assert [a['mode'] for a in activations] == ['pristine', 'cache']
    assert activations[0]['cli_sha256'] != activations[1]['cli_sha256']
    assert MARKER not in phases[0]['stderr'].splitlines() and MARKER in phases[1]['stderr'].splitlines()
    provider = json.loads((root/'provider.json').read_text())
    assert not provider['errors'] and provider['requests'] == evidence['requests']
    assert len(provider['requests']) == 4
    initial = (root/'initial-entry.ts').read_text()
    witness = next(line for line in initial.splitlines() if len(line.strip()) > 8)
    assert hashlib.sha256(initial.encode()).hexdigest() == evidence['initial_entry_sha256']
    assert provider['requests'][1]['read_witness_sha256'] == hashlib.sha256(witness.encode()).hexdigest()
    require_compiler_rejection(calls)
    return evidence


def check(backend, image, root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    assert not (root/'run/harness').exists(), 'use a fresh output directory'
    metadata = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True))[0]
    assert metadata['Config']['Labels']['org.opencontainers.image.revision'] == PINS[backend]
    (root/'image.json').write_text(json.dumps([metadata], indent=2)+'\n')
    provider = Provider(root/'run/harness/src'/ENTRY[backend], '/workspace/candidate/src/'+ENTRY[backend])
    url = provider.start()
    try:
        adapter, sandbox = adapter_for(backend, image, root, url)
        cfg = RunConfig(name=backend+'-source-fixture', adapter=adapter, disposition=NEUTRAL,
            goal=GoalConfig.of(text='Use the requested read/write tools to edit the candidate source.'),
            root=root/'run', model='fixture', episodes=2, phases=('act',), max_turns=12,
            phase_prompts={'act': 'Read the candidate CLI entrypoint and apply the requested source edit.'})
        result = run(cfg)
        assert not provider.errors, provider.errors
        assert result.episodes_complete == 2 and not result.error, asdict(result)
        assert len(provider.requests) == 4, provider.requests
        rows = [row for row in read_native_inputs(cfg.root) if row['input']['scope'] == 'harness_cli']
        assert len(rows) == 2 and all(row['completion'] == 'returned' for row in rows)
        activation = [row['outcome']['activation'] for row in rows]
        assert activation[0]['mode'] == 'pristine' and activation[1]['mode'] == 'cache', activation
        assert activation[0]['source_sha256'] != activation[1]['source_sha256']
        assert activation[0]['cli_sha256'] != activation[1]['cli_sha256']
        assert activation[1]['source_sha256'] == source_tree_hash(cfg.root/'harness/src')
        phases = [call for call in sandbox.calls if call['command'][0] == '--proteus-expect-source']
        assert len(phases) == 2 and all(call['returncode'] == 0 for call in phases)
        assert MARKER not in phases[0]['stderr'].splitlines() and MARKER in phases[1]['stderr'].splitlines()
        assert all(any(mount[1:] == ('/workspace', 'ro') for mount in call['mounts']) for call in phases)
        assert all(item['accepted'] for item in result.eval_history)
        before = read_native_inputs(cfg.root)
        resumed = run(cfg, start=completed_episodes(cfg), resume=True)
        assert resumed.episodes_complete == 2 and not resumed.error
        assert len(provider.requests) == 4 and read_native_inputs(cfg.root) == before
        # A broken candidate must fail the actual compiler without changing accepted source.
        broken = root/'broken/harness'
        shutil.copytree(cfg.root/'harness', broken, ignore=shutil.ignore_patterns('.git'))
        (broken/'src'/ENTRY[backend]).write_text('this is deliberately invalid TypeScript !!!\n')
        error = adapter.validate_candidate(broken)
        assert error and 'does not boot' in error, error
        require_compiler_rejection(sandbox.calls)
        assert source_tree_hash(cfg.root/'harness/src') == activation[1]['source_sha256']
        assert len(provider.requests) == 4
        evidence = {'schema_version': 1, 'backend': backend, 'source_revision': PINS[backend],
            'image_id': metadata['Id'], 'image_architecture': metadata['Architecture'],
            'proteus_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
            'requests': provider.requests, 'activation': activation,
            'initial_entry_sha256': hashlib.sha256(provider.initial.encode()).hexdigest(),
            'modified_entry_sha256': hashlib.sha256(provider.modified.encode()).hexdigest(),
            'broken_source_rejection': error, 'unchanged_completed_resume': True,
            'scope': 'actual pinned source build, Docker, native tool read/write, episode validation and next-episode activation; deterministic provider, no model-quality claim'}
        evidence['evidence_hash'] = content_hash(evidence)
        (root/'initial-entry.ts').write_text(provider.initial)
        (root/'modified-entry.ts').write_text(provider.modified)
        (root/'evidence.json').write_text(json.dumps(evidence, indent=2)+'\n')
        print(json.dumps(evidence, indent=2))
    finally:
        provider.close()
        (root/'provider.json').write_text(json.dumps({'requests': provider.requests, 'errors': provider.errors}, indent=2)+'\n')
    verify(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=tuple(PINS), required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    check(args.backend, args.image, args.out)
