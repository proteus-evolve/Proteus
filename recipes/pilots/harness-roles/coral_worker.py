"""Bounded live CORAL worker: two fresh coding turns with released feedback.

The worker service owns the process and its usage. This bridge does not claim
native provider-session continuation. Unknown calls/submissions are never retried.
"""

import argparse
import base64
import json
import os
from pathlib import Path
import signal
import sys
import time
import urllib.request

from proteus.api.v1 import ArtifactStore, FileContent, PluginRef, builtin_registry, require
from proteus.core.methods import ActorRequest
from proteus.evidence.records import EvidenceAccess, EvidenceView
from proteus.execution.workspace_records import WorkspaceInput
from proteus.recipes.config import ActorSpec


def call(path, body=None):
    request = urllib.request.Request(os.environ['PROTEUS_EVALUATION_URL'] + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + os.environ['PROTEUS_EVALUATION_TOKEN'],
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--actor', type=Path, required=True)
    args = parser.parse_args()
    launch = json.loads(sys.stdin.readline())
    require(launch['continuity'] == 'fresh', 'this bounded pilot never repeats uncertain worker generations')
    interrupted = False

    def stop(*_):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, stop)
    actor = ActorSpec.from_json(args.actor.read_text())
    registry = builtin_registry()
    runtime = registry.create(actor.runtime, 'actor')
    store = ArtifactStore(Path('pilot-artifacts').resolve(), registry)
    results = []
    feedback = None
    for index in range(2):
        require(not interrupted, 'controller stopped the worker before its next coding turn')
        workspace = Path(f'pilot-turn-{index}').resolve()
        for part in ('input', 'output', 'history', 'scratch'):
            (workspace / 'task' / part).mkdir(parents=True)
        source = Path('candidate/main.py').read_bytes()
        files = {'main.py': FileContent(source)}
        if feedback is not None:
            files['released-feedback.json'] = FileContent(json.dumps(feedback).encode())
        seed = store.put(PluginRef(name='proteus.file_tree', version='1'), files)
        for name, file in files.items():
            (workspace / 'task/input' / name).write_bytes(file.data)
        view = EvidenceView(run_id=launch['identity']['run_id'], config_hash=launch['identity']['config_hash'],
            lock_hash=launch['identity']['lock_hash'], stage_id='worker-turn', role=launch['identity']['role'],
            access=EvidenceAccess(evaluation_ids=(), splits=(), kinds=('artifact',)), candidate_ids=(), entries=())
        save(workspace / 'task/history/view.json', view.to_dict())
        prompt = ('Read /workspace/task/input/main.py. Preserve the JSON stdin/stdout protocol. '
            'Write a complete Python program to /workspace/task/output/main.py. '
            + ('For this initial mechanism probe, preserve its numerical behavior but refactor the program '
               'to use a named solve function. No target transformation is provided yet.' if index == 0 else
               'Read released-feedback.json, infer the integer transformation from that actual formal feedback, '
               'and improve the program. Explain the feedback you used in /workspace/task/output/feedback-used.md.')
            + ' Use real read/write/bash tools. Keep the program self-contained with stdlib only. '
              'Do not read credentials or files outside the declared task directories. Finish after writing the output.')
        request = ActorRequest(prompt=prompt, workspace=WorkspaceInput(seed=seed, history=view.ref()))
        save(Path(f'pilot-actor-request-{index}.json'), request.to_dict())
        result = runtime.execute(actor, request, workspace)
        save(Path(f'pilot-actor-result-{index}.json'), result.to_dict())
        results.append(result)
        require(result.status.value == 'completed' and result.runtime.settings.get('native_usage'),
                'coding turn failed or lacks real harness receipts')
        output = workspace / 'task/output/main.py'
        require(output.is_file() and not output.is_symlink() and output.stat().st_size < 100000,
                'coding worker did not produce a bounded source file')
        code = output.read_bytes()
        require(code != source, 'coding turn did not change candidate source')
        if index == 1:
            require((workspace / 'task/output/feedback-used.md').is_file(), 'feedback-consumption witness missing')
        Path('candidate/main.py').write_bytes(code)
        ticket = call('/v1/submissions', {'operation_id': f'pilot-proposal-{index}',
            'evaluation_id': 'validation', 'files': [{'path': 'main.py',
                'base64': base64.b64encode(code).decode(), 'executable': False}]})
        save(Path(f'pilot-ticket-{index}.json'), ticket)
        deadline = time.monotonic() + 120
        while True:
            feedback = call('/v1/submissions/' + ticket['ticket_id'])
            if feedback['status'] in ('completed', 'failed'):
                save(Path(f'pilot-feedback-{index}.json'), feedback)
                require(feedback['status'] == 'completed', 'formal evaluation failed')
                break
            require(time.monotonic() < deadline, 'formal outcome remains uncertain')
            time.sleep(.1)
    while not interrupted:
        time.sleep(.1)
    usage = {key: sum(values) if all(v is not None for v in values) else None
             for key in ('model_calls', 'input_tokens', 'output_tokens', 'cost_usd')
             for values in [[getattr(r.usage, key) for r in results]]}
    print(json.dumps({'event': 'result', 'usage': usage}), flush=True)


if __name__ == '__main__':
    main()
