"""Installed worker bridge and durable cumulative usage outside the checkout."""

import hashlib
from pathlib import Path
import sys
import tempfile
import time

from proteus.core.contracts import BudgetLimits
from proteus.core.ledger import Ledger
from proteus.core.serialization import content_hash
from proteus.workers import WorkerController, WorkerIdentity
from proteus.workers.codex import codex_worker


PROVIDER = '''
import json, os, sys, uuid
from pathlib import Path
args = sys.argv[1:]
assert '--no-daemon' in args
assert 'agents.enabled=false' in args
request = sys.stdin.read()
state = Path(os.environ['CODEX_HOME'])/'fixture.json'
if 'resume' in args:
    value = json.loads(state.read_text())
    assert args[args.index('resume')+1] == value['session']
else:
    value = {'session': str(uuid.uuid4()), 'count': 0}
value['count'] += 1
state.write_text(json.dumps(value))
for event in [{'type':'thread.started','thread_id':value['session']},
              {'type':'turn.started'},
              {'type':'turn.completed','usage':{'input_tokens':17*value['count'],
                                                'output_tokens':5*value['count']}}]:
    print(json.dumps(event),flush=True)
'''


def check():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        script = root/'provider.py'
        script.write_text(PROVIDER)
        command = (sys.executable, str(script))
        options = codex_worker(command, model='offline-fixture', credential_env=(),
            command_files={path: 'sha256:'+hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in command})
        identity = WorkerIdentity(run_id='installed', config_hash=content_hash('fixture'),
                                  worker_id='researcher', lock_hash=content_hash('lock'), role='researcher')
        ledger = Ledger(root/'private/ledger.sqlite3', run_id=identity.run_id,
                        config_hash=identity.config_hash, lock_hash=identity.lock_hash)
        worker = WorkerController(root, identity=identity, options=options, ledger=ledger,
                                  scopes={'run': BudgetLimits(max_concurrency=1)})
        previous, session, generations = None, None, []
        for index in range(3):
            ref = worker.start(str(index), 'fixture', previous=previous)
            deadline = time.monotonic()+15
            while worker.poll(ref).completion is None and time.monotonic()<deadline:
                time.sleep(.025)
            result = worker.poll(ref)
            assert result.state == 'completed', result
            completion = result.completion
            assert completion.usage.input_tokens == 17 and completion.usage.output_tokens == 5
            assert completion.usage.model_calls is None and completion.usage.cost_usd is None
            assert completion.session_usage.input_tokens == 17*(index+1)
            if previous is not None:
                assert completion.resumed_from == session
            previous, session = ref, completion.session_id
            generations.append(ref)
        assert sum(entry['settlement']['result']['usage']['input_tokens']
                   for entry in ledger.entries()) == 51
        assert ledger.balance()['active_slots'] == 0
        before = ledger.events()
        assert worker.start('2', 'fixture', previous=generations[-2]) == previous
        assert ledger.events() == before
    print('Installed Codex worker: three native generations, cumulative-to-incremental tokens and replay passed.')


if __name__ == '__main__':
    check()
