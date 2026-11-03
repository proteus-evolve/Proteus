"""Exercise an explicitly supplied real Codex CLI against a local Responses stub.

No external model credentials or inference services are used. The exact CLI
binary is pinned in the worker declaration. Not part of ordinary pytest: call
this manually when validating a supported CLI version.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from proteus.core.contracts import BudgetLimits
from proteus.core.ledger import Ledger
from proteus.core.serialization import content_hash
from proteus.workers import WorkerController, WorkerIdentity
from proteus.workers.codex import codex_worker


def check(executable):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            assert self.path == '/v1/responses', self.path
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(request)
            number = len(requests)
            message = {'id': f'msg_local_{number}', 'type': 'message', 'role': 'assistant',
                       'status': 'completed', 'content': [{'type': 'output_text',
                        'text': f'LOCAL_STUB_RESPONSE_{number}', 'annotations': []}]}
            response = {'id': f'resp_local_{number}', 'object': 'response', 'created_at': int(time.time()),
                        'model': 'local-protocol-fixture', 'status': 'completed', 'output': [message],
                        'usage': {'input_tokens': 13, 'output_tokens': 7, 'total_tokens': 20,
                                  'input_tokens_details': {'cached_tokens': 5},
                                  'output_tokens_details': {'reasoning_tokens': 0}}}
            events = [
                {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                {'type': 'response.output_item.added', 'output_index': 0,
                 'item': {**message, 'status': 'in_progress', 'content': []}},
                {'type': 'response.content_part.added', 'item_id': message['id'], 'output_index': 0,
                 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}},
                {'type': 'response.output_text.delta', 'item_id': message['id'], 'output_index': 0,
                 'content_index': 0, 'delta': f'LOCAL_STUB_RESPONSE_{number}'},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': message},
                {'type': 'response.completed', 'response': response},
            ]
            body = ''.join('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n'
                           for event in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    previous_key = os.environ.get('PROTEUS_CODEX_FIXTURE_KEY')
    os.environ['PROTEUS_CODEX_FIXTURE_KEY'] = 'local-stub-only'
    try:
        with tempfile.TemporaryDirectory(prefix='proteus-codex-cli-') as temporary:
            root = Path(temporary)
            options = codex_worker((str(executable),), model='local-protocol-fixture',
                command_files={str(executable): 'sha256:'+hashlib.sha256(executable.read_bytes()).hexdigest()},
                api_base_url=f'http://127.0.0.1:{server.server_port}/v1', api_key_env='PROTEUS_CODEX_FIXTURE_KEY',
                credential_env=('PROTEUS_CODEX_FIXTURE_KEY',), timeout_seconds=40, shutdown_grace_seconds=1)
            identity = WorkerIdentity(run_id='local-cli', config_hash=content_hash('fixture'),
                                      lock_hash=content_hash('lock'), worker_id='a', role='researcher')
            ledger = Ledger(root/'private/ledger.sqlite3', run_id=identity.run_id,
                            config_hash=identity.config_hash, lock_hash=identity.lock_hash)
            worker = WorkerController(root, identity=identity, options=options, ledger=ledger,
                                      scopes={'run': BudgetLimits(max_concurrency=1)})

            def finish(ref):
                deadline = time.monotonic()+50
                while time.monotonic() < deadline:
                    result = worker.poll(ref)
                    if result.completion is not None:
                        if result.state != 'completed':
                            for path in root.rglob('stderr.log'):
                                print(path.read_text()[-6000:])
                        assert result.state == 'completed', result
                        return result.completion
                    time.sleep(.05)
                worker.control(ref, 'stop')
                raise AssertionError('real CLI did not complete')

            first = worker.start('first', 'LOCAL_FIRST_PROMPT: reply briefly without tools.')
            completed = finish(first)
            second = worker.start('second', 'LOCAL_SECOND_PROMPT: continue briefly without tools.', previous=first)
            resumed = finish(second)
            assert resumed.session_id == resumed.resumed_from == completed.session_id
            assert len(requests) == 2, len(requests)
            assert 'LOCAL_STUB_RESPONSE_1' in json.dumps(requests[1]['input'])
            assert 'LOCAL_FIRST_PROMPT' in json.dumps(requests[1]['input'])
            for path in root.rglob('stdout.log'):
                for line in path.read_text().splitlines():
                    event = json.loads(line)
                    if event.get('record', {}).get('type') == 'turn.completed':
                        print('CLI terminal receipt:', json.dumps(event['record']))
            assert all(item.usage.input_tokens == 13 and item.usage.output_tokens == 7
                       and item.usage.model_calls is None for item in (completed, resumed)), (completed.usage, resumed.usage)
            assert ledger.balance()['active_slots'] == 0
            print('Real Codex CLI: local API, native thread/history continuation and token receipts passed.')
            from proteus.adapters.codex import CodexHarness
            from proteus.core.adapter import EpisodeSpec
            from proteus.core.native_inputs import input_directory, read_native_inputs
            from proteus.core.native_usage import summarize_native_inputs

            class LocalNativeSandbox:
                def run(self, run_root, command, env, timeout_s, mounts=(), stop_check=None):
                    # Test transport only: replace container mount paths with this
                    # temporary workspace, retain the native fresh exec arguments.
                    args = list(command[2:])
                    args[args.index('-C')+1] = str(run_root/'harness')
                    configs = {'model_provider': '"local-fixture"',
                        'model_providers.local-fixture.name': '"Local fixture"',
                        'model_providers.local-fixture.base_url': json.dumps(f'http://127.0.0.1:{server.server_port}/v1'),
                        'model_providers.local-fixture.wire_api': '"responses"',
                        'model_providers.local-fixture.env_key': '"PROTEUS_CODEX_FIXTURE_KEY"',
                        'agents.enabled': 'false'}
                    flags = [part for key, value in configs.items() for part in ('-c', key+'='+value)]
                    environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR') if key in os.environ}
                    (run_root/'cli-home').mkdir(exist_ok=True)
                    environment.update(CODEX_HOME=str(run_root/'cli-home'), PROTEUS_CODEX_FIXTURE_KEY='local-stub-only')
                    return subprocess.run([str(executable), '--no-daemon', *args[:-1], *flags, args[-1]],
                        cwd=run_root, env=environment, capture_output=True, text=True, timeout=40)

            native = (root/'native').resolve()
            (native/'harness/src').mkdir(parents=True)
            auth = native/'auth.json'
            auth.write_text('{}')
            publication = native/'fixture-publication'
            publication.mkdir()
            adapter = CodexHarness(auth_file=auth, sandbox=LocalNativeSandbox())
            adapter._ensure_validated = lambda *args: ('', publication)
            result = adapter.run_episode(EpisodeSpec(root=native, episode=1, model='local-protocol-fixture',
                phases=('observe','act'), phase_prompts={'observe':'Reply briefly without tools.', 'act':'Reply briefly without tools.'}))
            assert result.ok, result.error
            records = read_native_inputs(native)
            totals = summarize_native_inputs(records)
            assert len(records) == 2 and len(requests) == 4
            assert totals['reported']['input_tokens'] == 26 and totals['reported']['output_tokens'] == 14
            assert totals['reported']['model_calls'] is None and totals['reported']['cost_usd'] is None
            assert len({row['outcome']['usage']['fields']['session_id'] for row in records}) == 2
            assert len(list(input_directory(native).glob('*.usage-source'))) == 2
            assert 'LOCAL_STUB_RESPONSE_3' not in json.dumps(requests[3]['input'])
            print('Real native Codex adapter: two fresh ephemeral sessions, four total local API requests, raw streams and 26/14 native tokens passed.')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        if previous_key is None:
            os.environ.pop('PROTEUS_CODEX_FIXTURE_KEY', None)
        else:
            os.environ['PROTEUS_CODEX_FIXTURE_KEY'] = previous_key


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    check(parser.parse_args().executable.absolute())
