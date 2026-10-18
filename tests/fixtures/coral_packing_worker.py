"""Deterministic protocol fixture, not a model or a circle-packing optimizer."""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evaluation', default='packing')
    parser.add_argument('--cooperate', action='store_true')
    parser.add_argument('--sentinel')
    parser.add_argument('--submissions-a', type=int, default=2)
    args = parser.parse_args()
    if args.sentinel:
        assert os.getuid() != 0 and not Path(args.sentinel).exists()
        assert not Path('/var/run/docker.sock').exists()
    launch = json.loads(sys.stdin.readline())
    worker = launch['identity']['worker_id']
    interrupted = False

    def stop(*_):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, stop)
    spec = importlib.util.spec_from_file_location('research_gateway', 'candidate/research.py')
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    session = 'packing-fixture-'+worker
    if launch['session_id'] is not None:
        assert Path('session.txt').read_text() == launch['session_id'] == session
    Path('session.txt').write_text(session)
    with Path('launches.jsonl').open('a') as stream:
        stream.write(json.dumps(launch)+'\n')
    print(json.dumps({'event':'session','session_id':session,'resumed_from':launch['session_id']}),flush=True)
    candidate = Path('candidate/initial_program.py')
    assert candidate.is_file()
    if not Path('initial-source.py').exists():
        Path('initial-source.py').write_bytes(candidate.read_bytes())

    def publish(kind, key, data, sources=()):
        local=Path(key+'.json')
        local.write_text(json.dumps(data))
        result=gateway.request('/v1/knowledge/writes',{'action':'publish','operation_id':key,'kind':kind,
            'key':key,'files':[gateway.file(local,'SKILL.md' if kind=='skills' else 'finding.json')],
            'sources':list(sources)})
        assert result['outcome']['status']=='published'
        return result['outcome']['revision']

    def find(key):
        deadline, index = time.monotonic()+300, 0
        while time.monotonic()<deadline:
            snapshot=gateway.request('/v1/knowledge/snapshots',{'operation_id':'observe-'+key+'-'+str(index)})
            index+=1
            for ref in snapshot['snapshot']['revisions']:
                record=gateway.request('/v1/knowledge/revisions/'+ref['content_hash'].split(':')[1])
                write=record['revision']['write']
                if (write['kind'],write['key'])==('notes',key):
                    return ref, record
            time.sleep(.1)
        raise TimeoutError('cooperating worker did not publish '+key)

    done=len(list(Path('.').glob('result-*.json')))
    if worker=='b' and done==1 and launch['continuity']!='fresh':
        publish('notes','b-resumed',{'native_generation_ready':True})
    if done<(args.submissions_a if worker=='a' else 1):
        sources=[]
        geometry={'centers':[[(i%6+.5)/6,(i//6+.5)/6] for i in range(26)],'radii':[1/12]*26}
        if worker=='a':
            publish('notes','grid',geometry)
            if done>=1 and args.cooperate:
                find('b-resumed')
        else:
            ref, record=find('grid')
            sources.append(ref)
            Path('learned.json').write_text(json.dumps(record))
            geometry=json.loads(base64.b64decode(record['files'][0]['base64']))
        if worker=='b' or done>=1:
            candidate.write_text(f"def run(): return {geometry['centers']!r}, {geometry['radii']!r}, 999999\n")
        ticket=gateway.request('/v1/submissions',{'operation_id':'proposal-'+str(done),
            'evaluation_id':args.evaluation,'files':gateway.tree(Path('candidate'))})
        Path(f'ticket-{done}.json').write_text(json.dumps(ticket))
        deadline=time.monotonic()+600
        while time.monotonic()<deadline:
            result=gateway.request('/v1/submissions/'+ticket['ticket_id'])
            if result['status'] in ('completed','failed'):
                Path(f'result-{done}.json').write_text(json.dumps(result))
                assert result['status']=='completed', result
                if worker=='b':
                    sources.append(result['result']['shared_attempt'])
                    publish('skills','verified-grid',{'geometry':geometry},sources)
                break
            time.sleep(.1)
        else:
            raise TimeoutError('formal packing trial did not complete')
    while not interrupted:
        time.sleep(.05)
    print(json.dumps({'event':'result','session_id':session,'resumed_from':launch['session_id'],
        'usage':{'model_calls':0,'input_tokens':0,'output_tokens':0,'cost_usd':0}}),flush=True)


if __name__=='__main__':
    main()
