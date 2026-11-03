"""Evidence equals real transport inputs and survives failures without replay claims."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from proteus.core.adapter import EpisodeSpec
from proteus.core.native_inputs import input_directory, read_native_inputs
from proteus.core.native_usage import summarize_native_inputs
from proteus.core.serialization import ContractError, content_hash


def spec(root):
    (root/'harness').mkdir(parents=True, exist_ok=True)
    return EpisodeSpec(root=root, episode=1, model='recorded-model', phases=('act',),
                       phase_prompts={'act': 'ACT-MARKER'}, max_turns=4, announce_budget=True)


def test_chat_evidence_matches_actual_http_body_and_updated_state(tmp_path):
    from proteus.adapters.llm import LLMHarness
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            pending = read_native_inputs(tmp_path)[-1]
            assert pending['completion'] == 'unknown'
            assert pending['input']['payload'] == body
            captured.append(body)
            text = '[{"tool":"write_note","name":"learned","text":"NEW-STATE"}]' if len(captured)==1 else '[]'
            data = json.dumps({'choices':[{'message':{'content':text}}],
                               'usage':{'prompt_tokens':11,'completion_tokens':3}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    adapter = LLMHarness(key='PRIVATE-TRANSPORT-KEY', base_url=f'http://127.0.0.1:{server.server_port}')
    adapter.seed(tmp_path/'harness')
    request = EpisodeSpec(root=tmp_path, episode=1, model='actual-request-model', phases=('observe','act'),
                         phase_prompts={'observe':'OBSERVE-MARKER','act':'ACT-MARKER'}, max_turns=10, announce_budget=True)
    try:
        result = adapter.run_episode(request)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert result.ok and result.counters['tokens_in']==22
    rows = read_native_inputs(tmp_path)
    assert len(rows)==2 and all(row['completion']=='returned' for row in rows)
    assert [row['input']['payload'] for row in rows]==captured
    assert 'NEW-STATE' not in captured[0]['messages'][1]['content']
    assert 'NEW-STATE' in captured[1]['messages'][1]['content']
    assert 'ACT-MARKER' in captured[1]['messages'][1]['content']
    assert 'PRIVATE-TRANSPORT-KEY' not in json.dumps(rows)
    assert rows[0]['input']['payload_hash']==content_hash(captured[0])
    assert not input_directory(tmp_path).is_relative_to(tmp_path)


@pytest.mark.parametrize('backend', ['pi','dsh','codex'])
@pytest.mark.parametrize('failure', [None,'timeout','exit'])
def test_container_boundary_exact_argv_and_failed_attempts(tmp_path, monkeypatch, backend, failure):
    from proteus.adapters.pi import PiHarness
    from proteus.adapters.dsh import DshHarness
    from proteus.adapters.codex import CodexHarness
    captures = []

    class Sandbox:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            pending = read_native_inputs(tmp_path)[-1]
            assert pending['completion']=='unknown'
            assert pending['input']['payload']==command
            captures.append(list(command))
            if failure=='timeout':
                raise subprocess.TimeoutExpired(command, timeout_s, output='PRIVATE-TRANSPORT-KEY')
            return subprocess.CompletedProcess(command, 23 if failure=='exit' else 0, '', '')

    request = spec(tmp_path)
    if backend=='codex':
        auth = tmp_path/'auth.json'
        auth.write_text('{}')
        adapter = CodexHarness(auth_file=auth, sandbox=Sandbox())
        (tmp_path/'harness/src').mkdir()
        publication = tmp_path/'publication'
        publication.mkdir()
        monkeypatch.setattr(adapter, '_ensure_validated', lambda *args: ('',publication))
    else:
        cls = PiHarness if backend=='pi' else DshHarness
        adapter = cls(key='PRIVATE-TRANSPORT-KEY', sandbox=Sandbox())
    result = adapter.run_episode(request)
    rows = read_native_inputs(tmp_path)
    assert len(captures)==len(rows)==1
    assert rows[0]['input']['scope']=='harness_cli' and rows[0]['input']['backend']==backend
    assert rows[0]['input']['payload']==captures[0]
    assert 'ACT-MARKER' in captures[0][-1] and captures[0][-1]!='ACT-MARKER'
    if failure=='timeout':
        assert not result.ok and rows[0]['completion']=='raised'
        assert rows[0]['outcome']['error_type']=='TimeoutExpired'
    else:
        assert result.ok == (failure is None)
        assert rows[0]['completion']=='returned'
        assert rows[0]['outcome']['returncode']==(23 if failure else 0)
    assert 'PRIVATE-TRANSPORT-KEY' not in json.dumps(rows)

    usage = summarize_native_inputs(rows)['reported']
    assert all(usage[key] is None for key in ('model_calls', 'input_tokens', 'output_tokens', 'cost_usd'))
    assert usage['wall_seconds'] >= 0


def test_hard_exit_keeps_unknown_input_and_retry_is_a_new_attempt(tmp_path):
    program = '''import os,sys
from pathlib import Path
from proteus.adapters.minimal import MinimalHarness
from proteus.core.adapter import EpisodeSpec
root=Path(sys.argv[1]);(root/'harness').mkdir(parents=True,exist_ok=True)
def policy(phase,prompt,episode,rng):
 if sys.argv[2]=='stop': os._exit(86)
 yield 'write_note','notes','saved'
adapter=MinimalHarness(policy=policy)
result=adapter.run_episode(EpisodeSpec(root=root,episode=1,model='mock',phases=('act',),phase_prompts={'act':'saved input'}))
assert result.ok
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    stopped = subprocess.run([sys.executable,'-c',program,str(tmp_path),'stop'], env=env, capture_output=True, timeout=30)
    assert stopped.returncode==86, stopped.stderr
    before = {path.name:path.read_bytes() for path in input_directory(tmp_path).glob('*.json')}
    assert len(before)==1 and read_native_inputs(tmp_path)[0]['completion']=='unknown'
    retried = subprocess.run([sys.executable,'-c',program,str(tmp_path),'retry'], env=env, capture_output=True, timeout=30)
    assert retried.returncode==0, retried.stderr
    rows = read_native_inputs(tmp_path)
    assert [row['completion'] for row in rows]==['unknown','returned']
    assert rows[0]['input']['id']!=rows[1]['input']['id']
    assert rows[0]['input']['payload_hash']==rows[1]['input']['payload_hash']
    assert (tmp_path/'harness/notes/saved.md').exists()
    assert all((input_directory(tmp_path)/name).read_bytes()==data for name,data in before.items())
    usage = summarize_native_inputs(rows)
    assert usage['boundary_count']==2 and usage['reported']['wall_seconds'] is None
    assert usage['unknown_entries']['wall_seconds']==1 and usage['known_subtotal']['wall_seconds']>=0
    assert usage['reported']['model_calls'] is None


def test_minimal_policy_stays_lazy_and_corruption_is_rejected(tmp_path):
    from proteus.adapters.minimal import MinimalHarness
    seen=[]
    def policy(phase,prompt,episode,rng):
        for index in range(20):
            seen.append(index)
            yield 'write_note','notes',str(index)
    result = MinimalHarness(policy=policy).run_episode(spec(tmp_path))
    assert result.turns==4 and seen==[0,1,2,3,4]
    row=read_native_inputs(tmp_path)[0]
    assert row['input']['scope']=='policy_prompt'
    path=input_directory(tmp_path)/(row['input']['id']+'.input.json')
    value=json.loads(path.read_text())
    value['payload']='corrupted'
    path.write_text(json.dumps(value))
    with pytest.raises(ContractError, match='hash mismatch'):
        read_native_inputs(tmp_path)


def test_aki_records_opaque_supervisor_without_claiming_internal_prompts(tmp_path, monkeypatch):
    from proteus.adapters.aki import AkiHarness
    adapter=AkiHarness()
    async def run_episode(config,episode):
        row=read_native_inputs(tmp_path)[0]
        assert row['completion']=='unknown' and row['input']['scope']=='opaque_supervisor'
    monkeypatch.setattr(adapter,'_api',lambda:(None,SimpleNamespace(run_episode=run_episode),None,None))
    monkeypatch.setattr(adapter,'_episode_outcome',lambda *args:({'status':'complete'},{'turns_used':1}))
    adapter._run_configs[tmp_path]=object()
    assert adapter.run_episode(spec(tmp_path)).ok
    row=read_native_inputs(tmp_path)[0]
    assert row['input']['model'] is None and row['input']['payload']['internal_prompts']=='unavailable'


def test_changed_outcome_is_rejected(tmp_path):
    from proteus.adapters.minimal import MinimalHarness
    assert MinimalHarness().run_episode(spec(tmp_path)).ok
    path=next(input_directory(tmp_path).glob('*.outcome.json'))
    outcome=json.loads(path.read_text())
    outcome['status']='raised'
    path.write_text(json.dumps(outcome))
    with pytest.raises(ContractError, match='outcome hash mismatch'):
        read_native_inputs(tmp_path)
