"""A model-free worker fixture that learns a factor from another worker's note."""

import base64
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request


def request(path, body=None):
    deadline = time.monotonic()+45
    while time.monotonic()<deadline:
        try:
            wire = urllib.request.Request(os.environ['PROTEUS_EVALUATION_URL']+path,
                data=None if body is None else json.dumps(body).encode(),
                headers={'Authorization': 'Bearer '+os.environ['PROTEUS_EVALUATION_TOKEN'],
                         'Content-Type': 'application/json'})
            with urllib.request.urlopen(wire, timeout=1) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 503:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead):
            pass
        time.sleep(.05)
    raise TimeoutError('worker transport did not recover')


def file(path, data):
    return {'path': path, 'base64': base64.b64encode(data).decode(), 'executable': False}


def publish(kind, key, files, sources=()):
    body = {'action': 'publish', 'operation_id': key, 'kind': kind, 'key': key, 'files': files, 'sources': list(sources)}
    result = request('/v1/knowledge/writes', body)
    assert result['outcome']['status'] == 'published'
    return result


def main():
    launch = json.loads(sys.stdin.readline())
    worker = launch['identity']['worker_id']
    sources = []
    if worker == 'a':
        note = publish('notes', 'strategy', [file('factor.txt', b'2')])
        Path('publication.json').write_text(json.dumps(note))
        factor = 1
    else:
        # Every observation has a new operation ID. Retrying one observation
        # reuses its captured snapshot, even if another worker has since written.
        deadline, index = time.monotonic()+35, 0
        factor = None
        while time.monotonic()<deadline and factor is None:
            captured = request('/v1/knowledge/snapshots', {'operation_id': 'observe-'+str(index)})
            index += 1
            for ref in captured['snapshot']['revisions']:
                record = request('/v1/knowledge/revisions/'+ref['content_hash'].split(':')[1])
                write = record['revision']['write']
                if (write['kind'], write['key']) == ('notes', 'strategy'):
                    factor = int(base64.b64decode(record['files'][0]['base64']))
                    sources.append(ref)
                    Path('strategy.json').write_text(json.dumps({'captured': captured, 'record': record}))
                    break
            if factor is None:
                time.sleep(.025)
        assert factor is not None
    code = ("import json, sys\nr=json.load(sys.stdin)\n"
            f"print(json.dumps({{'output':r['task']['input']*{factor},'state':r['state']}}))\n").encode()
    ticket = request('/v1/submissions', {'operation_id': 'proposal', 'evaluation_id': 'validation',
                                        'files': [file('main.py', code)]})
    Path('ticket.json').write_text(json.dumps(ticket))
    Path('main.py').write_text("raise RuntimeError('later workspace edit must not be evaluated')\n")
    deadline = time.monotonic()+45
    while time.monotonic()<deadline:
        result = request('/v1/submissions/'+ticket['ticket_id'])
        if result['status'] in ('completed', 'failed'):
            assert result['status'] == 'completed'
            Path('response.json').write_text(json.dumps(result))
            if worker == 'b':
                sources.append(result['result']['shared_attempt'])
                skill = publish('skills', 'double-input', [file('SKILL.md', b'Multiply the task input by the shared factor.\n')], sources)
                Path('publication.json').write_text(json.dumps(skill))
            print(json.dumps({'event':'result', 'session_id':'knowledge-'+worker, 'resumed_from':None,
                              'usage':{'model_calls':0, 'input_tokens':0, 'output_tokens':0, 'cost_usd':0}}), flush=True)
            return
        time.sleep(.025)
    raise TimeoutError('formal evaluation did not finish')


if __name__ == '__main__':
    main()
