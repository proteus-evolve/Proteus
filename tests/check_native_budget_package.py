"""Installed native call limits use a local HTTP fixture, never a paid provider."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading

from proteus.core.contracts import BudgetLimits, PluginRef
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import PluginSpec
from proteus.recipes.export import export_run, report_run
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def check():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers['Content-Length'])))
            data = json.dumps({'choices': [{'message': {'content': '[]'}}],
                               'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    old = os.environ.get('PROTEUS_NATIVE_BUDGET_TEST_KEY')
    os.environ['PROTEUS_NATIVE_BUDGET_TEST_KEY'] = 'private-fixture-key'
    try:
        with tempfile.TemporaryDirectory(prefix='proteus-native-budget-package-') as directory:
            root = Path(directory)/'run'
            recipe = load_recipe(Path(__file__).with_name('native.toml'))
            actor = replace(recipe.actors['agent'], model='fixture', budget=BudgetLimits(max_model_calls=2),
                runtime=PluginSpec(plugin=PluginRef(name='proteus.native.llm', version='1'),
                    options={'base_url': f'http://127.0.0.1:{server.server_port}',
                             'key_env': 'PROTEUS_NATIVE_BUDGET_TEST_KEY'}))
            recipe = replace(recipe, actors={'agent': actor}, budget=BudgetLimits(max_model_calls=3),
                method=replace(recipe.method, options={**dict(recipe.method.options),
                    'episodes': 3, 'phases': ['act'], 'evaluators': ['tool-calls']}))
            registry = builtin_registry()
            lock = registry.make_lock(recipe, [SourcePin(name='proteus',
                repository='https://example.invalid/fixture', revision='0'*40)])
            for resume in (False, True):
                result = run_recipe(recipe, registry=registry, lock=lock, root=root, resume=resume)
                assert result.status.value == 'failed' and 'budget exhausted' in result.diagnostic
                assert len(calls) == 2
                report = report_run(root)
                assert report['accounting']['budget_charged']['model_calls'] == 2
                usage = report['native_accounting']
                assert usage['budget_enforced'] and usage['budget_dimensions'] == ['model_calls']
                assert usage['reported']['model_calls'] == 2 and usage['reported']['input_tokens'] == 14
                assert usage['reported']['cost_usd'] is None
                assert len(usage['ledger_bindings']) == usage['boundary_count']
                assert 'private-fixture-key' not in json.dumps(report)
            export_run(root, Path(directory)/'export')
            assert (Path(directory)/'export/native-usage.csv').is_file()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        if old is None:
            os.environ.pop('PROTEUS_NATIVE_BUDGET_TEST_KEY')
        else:
            os.environ['PROTEUS_NATIVE_BUDGET_TEST_KEY'] = old
    print('installed native budgets: real local HTTP, actor/run caps, free evaluators, no-refill resume and export passed')


if __name__ == '__main__':
    check()
