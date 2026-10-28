"""CORAL v4_1: unchanged worker logic, newly pinned explicit Python output contract."""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import urllib.request
import circle_released_feedback_v4_1 as feedback_module
from circle_released_feedback_v4_1 import summarize_gateway, summarize_knowledge, render_prompt

ROOT = Path(__file__).resolve().parent
stopping = False


def stop(*args):
    global stopping
    stopping = True


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def persist(path, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode() + b'\n'
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('immutable worker record changed: ' + path.name)
        return
    temporary = path.with_suffix(path.suffix + '.pending')
    with temporary.open('xb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    os.link(temporary, path); temporary.unlink()


def gateway(path, body=None):
    request = urllib.request.Request(os.environ['PROTEUS_EVALUATION_URL'] + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + os.environ['PROTEUS_EVALUATION_TOKEN'],
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def encoded(name, data):
    return {'path': name, 'base64': base64.b64encode(data).decode(), 'executable': False}


def wait_for_legacy(api,arm):
    token=(ROOT/'private-bridge/scoped-token.txt').read_text().strip()
    while not stopping:
        request=urllib.request.Request(api+'/admission/'+arm,headers={'Authorization':'Bearer '+token})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(request,timeout=10) as response:admission=json.load(response)
                break
            except OSError as exc:
                if getattr(exc,'code',503) not in (408,429,500,502,503,504) or attempt==4:raise
                time.sleep(min(2**attempt,8))
        if admission.get('allowed') is True:
            return
        time.sleep(1)


def incremental_usage(root, prior_responses, unknown):
    # A restarted generation shares workspace history, not a fresh API budget.
    responses = [json.loads(p.read_text()) for p in root.glob('model-*-response.json') if p not in prior_responses]
    usages = [x.get('usage') or {} for x in responses]
    return {'model_calls': None if unknown else len(responses), 'cost_usd': None,
        'input_tokens': sum(x['prompt_tokens'] for x in usages)
            if not unknown and all('prompt_tokens' in x for x in usages) else None,
        'output_tokens': sum(x['completion_tokens'] for x in usages)
            if not unknown and all('completion_tokens' in x for x in usages) else None}


def main():
    launch = json.loads(sys.stdin.readline())
    worker = launch['identity']['worker_id']
    root, maximum, api, arm = Path.cwd(), int(sys.argv[1]), sys.argv[2], sys.argv[3]
    candidate = root/'candidate'
    imported = Path(feedback_module.__file__).resolve()
    if imported != (ROOT/'circle_released_feedback_v4_1.py').resolve():
        raise RuntimeError('Unexpected feedback helper import origin')
    imported_hash = 'sha256:' + hashlib.sha256(imported.read_bytes()).hexdigest()
    if len(sys.argv) <= 5 or sys.argv[5] != imported_hash:
        raise RuntimeError('Feedback import bytes differ from frozen command files')
    persist(root/'feedback-import-origin.json', {'module_path': str(imported), 'sha256': imported_hash,
        'only_authorized_gateway_evidence': True})
    prior_responses = set(root.glob('model-*-response.json'))
    unknown = False
    try:
        if (float(sys.argv[4]) if len(sys.argv)>4 else 0) < 650:
            raise RuntimeError('600s bounded provider requests require at least650s shutdown grace before any real dispatch')
        for index in range(maximum):
            attempt = root/f'attempt-{index:03d}.json'
            if attempt.exists():
                continue
            if stopping:
                break
            response_path = root/f'model-{index:03d}-response.json'
            intent_path = root/f'model-{index:03d}-intent.json'
            if intent_path.exists() and not response_path.exists():
                unknown = True
                raise RuntimeError('prior dispatched model response is uncertain; do not repeat the call')
            if not response_path.exists():
                observed = gateway('/v1/knowledge/snapshots', {'operation_id': f'{worker}-observe-{index}'})
                revisions = observed['snapshot']['revisions']
                if len(revisions) > 80:
                    raise RuntimeError('Unexpected shared knowledge size; refuse silent loss of measured feedback')
                candidates_shared = []
                for position, ref in enumerate(revisions):
                    key = ref['content_hash'].split(':')[1]
                    cache, first = root/f'knowledge-{key}.json', root/f'knowledge-firstseen-{key}.json'
                    if not cache.exists():
                        persist(cache, gateway('/v1/knowledge/revisions/' + key))
                    if not first.exists():
                        persist(first, {'first_observed_proposal': index+1, 'source': ref})
                    record = json.loads(cache.read_text())
                    item = summarize_knowledge(record, identity=launch['identity'])
                    seen = json.loads(first.read_text())['first_observed_proposal']
                    item['first_observed_proposal'] = seen
                    candidates_shared.append((seen, item['kind']=='measured_shared_attempt', position, item))
                # Snapshot hashes are not chronological. Cache authorized exports
                # once, keep the same8context slots, and prioritize newly observed
                # formal attempts over unverified notes. Observation order is not
                # misrepresented as the sender's wall-clock chronology.
                shared = [x[3] for x in sorted(candidates_shared, key=lambda x:x[:3], reverse=True)[:8]]
                history = [json.loads(p.read_text())['formal_summary']
                    for p in sorted(root.glob('attempt-*.json'), reverse=True)[:5]]
                if any(item.get('schema_version') != 3 for item in history):
                    raise RuntimeError('Legacy lossy feedback cannot warm-start this new profile')
                prompt, rendering = render_prompt(orientation=launch['prompt'], worker=worker,
                    proposal=index+1, maximum=maximum,
                    source=(candidate/'initial_program.py').read_text(),
                    recent=history, shared=shared)
                rendered_shared = json.loads(prompt.split('\nAUTHORIZED_SHARED_KNOWLEDGE_JSON\n',1)[1].split('\nCURRENT_SOURCE\n',1)[0])
                revisions = [item['source'] for item in rendered_shared]
                rendering['shared_context_order'] = 'Most recently first-observed; formal attempts prioritized; no claimed sender chronology'
                rendering['rendered_shared_source_refs'] = revisions
                body = {'model': 'deepseek-flash', 'temperature': 0, 'max_tokens': 8192,
                    'thinking': {'type': 'disabled'}, 'messages': [{'role': 'user', 'content': prompt}]}
                wait_for_legacy(api,arm)
                if stopping:
                    break
                persist(root/f'model-{index:03d}-request.json', body)
                persist(root/f'model-{index:03d}-rendering.json', rendering)
                persist(intent_path, {'feedback_transport': rendering, 'body_hash': hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                    'worker': worker, 'index': index, 'shared_revisions': revisions,
                    'dispatch_epoch': time.time(), 'same_operation_retry': False})
                request = urllib.request.Request(api+'/'+arm+'/v1/chat/completions',
                    data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
                        'Authorization':'Bearer '+(ROOT/'private-bridge/scoped-token.txt').read_text().strip(),
                        'X-Proteus-Request-ID':hashlib.sha256((arm+'|'+str(root.resolve())+'|'+worker+'|'+str(index)).encode()).hexdigest()})
                try:
                    with urllib.request.urlopen(request, timeout=600) as response:
                        answer = json.load(response)
                    persist(response_path, answer)
                except BaseException:
                    unknown = True
                    raise
            if stopping:
                # Seal an in-flight response first, but do not publish or submit
                # new work after the controller's pause. Resume reuses these bytes.
                break
            answer = json.loads(response_path.read_text())
            text = answer['choices'][0]['message']['content'].strip()
            if text.startswith('```'):
                text = text.split('\n', 1)[1].rsplit('```', 1)[0]
            value = json.loads(text)
            if not isinstance(value.get('code'), str) or not isinstance(value.get('note'), str):
                raise ValueError('model must supply code and note')
            # Only bytes are installed here; generated code executes inside formal Harbor.
            (candidate/'initial_program.py').write_text(value['code'])
            refs = json.loads(intent_path.read_text())['shared_revisions']
            published = gateway('/v1/knowledge/writes', {'operation_id': f'{worker}-note-{index}',
                'action': 'publish', 'kind': 'notes', 'key': f'{worker}-proposal-{index}',
                'files': [encoded('finding.md', value['note'].encode())], 'sources': refs})
            ticket_path = root/f'ticket-{index:03d}.json'
            if not ticket_path.exists():
                ticket = gateway('/v1/submissions', {'operation_id': f'{worker}-proposal-{index}',
                    'evaluation_id': 'packing',
                    'files': [encoded(p.name, p.read_bytes()) for p in sorted(candidate.iterdir()) if p.is_file()]})
                persist(ticket_path, ticket)
            ticket = json.loads(ticket_path.read_text())
            while not stopping:
                result = gateway('/v1/submissions/'+ticket['ticket_id'])
                if result['status'] in ('completed', 'failed'):
                    break
                time.sleep(.5)
            if stopping:
                break
            persist(root/f'formal-{index:03d}.json', result)
            summary = summarize_gateway(result, ticket_id=ticket['ticket_id'], identity=launch['identity'])
            persist(root/f'measured-{index:03d}.json', summary)
            persist(attempt, {'worker': worker, 'index': index, 'response_id': answer.get('id'),
                'model': answer.get('model'), 'usage': answer.get('usage'),
                'source_hash': hashlib.sha256(value['code'].encode()).hexdigest(),
                'shared_revisions_in_model_context': refs, 'published_note': published,
                'formal_summary': summary, 'formal_receipt': str(root/f'formal-{index:03d}.json')})
        while not stopping:
            time.sleep(.25)
    finally:
        usage = incremental_usage(root, prior_responses, unknown)
        print(json.dumps({'event': 'result', 'usage': usage}), flush=True)


if __name__ == '__main__':
    main()
