"""Installed generic harness accounting through a real local HTTP transport."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading

from proteus.core.contracts import BudgetLimits, OperationStatus, PluginRef
from proteus.core.native_inputs import input_directory
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import PluginSpec
from proteus.recipes.export import export_run, report_run
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def check():
    calls = []
    replies = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            reply = replies.pop(0) if replies else {'choices': [{'message': {'content': '[]'}}],
                               'usage': {'prompt_tokens': 13, 'completion_tokens': 7}}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    key_name = 'PROTEUS_PACKAGE_ACCOUNTING_FIXTURE'
    previous = os.environ.get(key_name)
    os.environ[key_name] = 'local-fixture-only'
    try:
        with tempfile.TemporaryDirectory(prefix='proteus-harness-accounting-') as directory:
            root = Path(directory)/'run'
            recipe = load_recipe(Path(__file__).with_name('stages.toml'))
            actor = replace(recipe.actors['optimizer'], model='fixture', runtime=PluginSpec(
                plugin=PluginRef(name='proteus.harness.llm', version='1'),
                options={'base_url': f'http://127.0.0.1:{server.server_port}', 'key_env': key_name}))
            recipe = replace(recipe, budget=BudgetLimits(), actors={'optimizer': actor})
            registry = builtin_registry()
            lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/fixture', revision='0'*40)])
            first = run_recipe(recipe, registry=registry, lock=lock, root=root)
            assert first.status == OperationStatus.COMPLETED, first.diagnostic
            for path in (root/'actors').glob('actor-*'):
                shutil.rmtree(path)
            assert run_recipe(recipe, registry=registry, lock=lock, root=root, resume=True) == first
            assert len(calls) == 1
            public, private = report_run(root), report_run(root, include_private=True)
            assert public['accounting']['actual']['model_calls'] == 1
            assert public['accounting']['actor_tokens']['reported'] == {'input_tokens': 13., 'output_tokens': 7.}
            assert public['accounting']['actual']['cost_usd'] is None
            assert len(public['accounting']['native_receipts']) == 1
            assert not any('.proteus-records' in path for path in public['evidence'])
            assert sum('.proteus-records' in path for path in private['evidence']) == 2
            assert public['native_accounting']['coverage'] == 'unavailable'
            export_run(root, Path(directory)/'export')
            costs = (Path(directory)/'export/role-stage-costs.csv').read_text()
            assert 'input_tokens' in costs and '13.0' in costs
            call = json.loads((root/'execution-0002.json').read_text())['actor_calls'][0]
            next(input_directory(root/'actors'/call['id']).glob('*.outcome.json')).unlink()
            try:
                report_run(root)
            except ValueError:
                pass
            else:
                raise AssertionError('missing actor receipt was accepted')
            print('installed harness actor: real local HTTP usage, ledger/token export, private receipt validation and unchanged resume passed')
            # The shared chat runtime must account for a rejected response too.
            chat = replace(actor, runtime=replace(actor.runtime,
                plugin=PluginRef(name='proteus.chat', version='1')))
            recipe = replace(recipe, actors={'optimizer': chat}, budget=BudgetLimits(max_model_calls=2),
                stages=(replace(recipe.stages[0], failure_policy='retry', max_attempts=2),))
            lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/fixture', revision='0'*40)])
            replies.extend([{'choices': [], 'usage': {'prompt_tokens': 13, 'completion_tokens': 7}},
                {'choices': [{'message': {'content': 'answer'}}],
                 'usage': {'prompt_tokens': 5, 'completion_tokens': 2}}])
            chat_root = Path(directory)/'chat-run'
            completed = run_recipe(recipe, registry=registry, lock=lock, root=chat_root)
            assert completed.status == OperationStatus.COMPLETED, completed.diagnostic
            assert not replies and len(calls) == 3
            report = report_run(chat_root)
            assert report['accounting']['actual']['model_calls'] == 2
            assert report['accounting']['actual']['cost_usd'] is None
            assert report['accounting']['actor_tokens']['reported'] == {'input_tokens': 18., 'output_tokens': 9.}
            records = json.loads((chat_root/'execution-0001.json').read_text())['actor_calls']
            assert len(records) == 2 and records[0]['result']['status'] == 'failed'
            assert records[0]['usage']['input_tokens'] == 13
            assert run_recipe(recipe, registry=registry, lock=lock, root=chat_root, resume=True) == completed
            assert len(calls) == 3
            export_run(chat_root, Path(directory)/'chat-export')
            costs = (Path(directory)/'chat-export/role-stage-costs.csv').read_text()
            assert '18.0' in costs and '9.0' in costs
            print('installed chat actor: failed response usage, retry totals, export and unchanged resume passed')
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        if previous is None:
            os.environ.pop(key_name, None)
        else:
            os.environ[key_name] = previous


if __name__ == '__main__':
    check()
