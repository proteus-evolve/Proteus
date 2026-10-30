"""Structured consumers must receive full final replies, not trace previews."""

import json

import pytest

from proteus.adapters.pi import PiHarness
from proteus.adapters.dsh import DshHarness
from proteus.adapters.codex import CodexHarness
from proteus.core.adapter import EpisodeResult
from proteus.core.methods import ActorRequest
from proteus.execution.actors import HarnessActor, HarnessOptions
from proteus.recipes.config import ActorSpec, PluginSpec
from proteus.core.contracts import PluginRef


def test_fresh_staged_actor_preserves_source_symlinks_and_pristine_hash(tmp_path):
    class LinkedSeed:
        supports_custom_phases = staged_activation = True
        continuity_mode = 'framework'

        def seed(self, root, seed):
            source = root / 'src'
            (source / 'real').mkdir(parents=True)
            (source / 'real/tool.py').write_text('pinned source')
            (source / 'tool.py').symlink_to('real/tool.py')
            (source / 'alias').symlink_to('real', target_is_directory=True)

        def run_episode(self, spec):
            source = spec.root / 'harness/src'
            active = spec.active_root / 'src'
            assert CodexHarness._source_hash(active) == CodexHarness._source_hash(source), 'source identity changed during activation copy'
            assert (active / 'tool.py').is_symlink() and (active / 'alias').is_symlink()
            return EpisodeResult(episode=1, ok=True, turns=0)

        def read_trace(self, *args):
            return ()

        def read_response(self, *args):
            return 'ok'

    runtime = HarnessActor('codex', HarnessOptions())
    runtime.adapter = lambda spec: LinkedSeed()
    actor = ActorSpec(model='fixture', runtime=PluginSpec(plugin=PluginRef(name='proteus.harness.codex', version='1')))
    result = runtime.execute(actor, ActorRequest(prompt='Use the fixed runtime.'), tmp_path)
    assert result.text == 'ok', result.diagnostic


@pytest.mark.parametrize('backend', ('pi', 'dsh', 'codex'))
def test_native_final_response_preserves_long_json_without_progress_or_reasoning(tmp_path, monkeypatch, backend):
    answer = json.dumps({'reasoning': 'visible explanation ' * 100, 'final_answer': 'ALPHA'})
    progress = 'Checking the task now.'
    if backend == 'pi':
        adapter = PiHarness()
        directory = tmp_path / '.pi-state'
        name = 'session.jsonl'
        events = [dict(type='message', message=dict(role='assistant', content=[{'type': 'text', 'text': t}]))
                  for t in (progress, answer)]
        events[-1]['message']['content'].insert(0, {'type': 'thinking', 'thinking': 'private scratchpad'})
    elif backend == 'dsh':
        adapter = DshHarness()
        directory = tmp_path / '.dsh-state/session'
        name = 'session.jsonl.zstd'
        # A native DSH turn spans several model steps, including progress + final.
        events = [dict(type='assistant/message', data={'turn': 1, 'step': i, 'message': {'content': [
            {'type': 'text', 'text': t}, {'type': 'reasoning', 'text': 'private scratchpad'}]}})
            for i, t in enumerate((progress, answer), 1)]
        monkeypatch.setattr('proteus.adapters.dsh._zstd_partial', lambda data: data)
    else:
        adapter = CodexHarness()
        directory = tmp_path / '.codex-state/sessions'
        name = 'session.jsonl'
        events = [dict(type='item.completed', item={'type': kind, 'text': t}) for kind, t in (
            ('agent_message', progress), ('reasoning', 'private scratchpad'), ('agent_message', answer))]
    directory.mkdir(parents=True)
    (directory / name).write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    (tmp_path / 'traces').mkdir()
    mapping = {'act': ['session']} if backend == 'dsh' else {'act': name if backend == 'codex' else [name]}
    (tmp_path / 'traces/ep001.json').write_text(json.dumps(mapping))
    assert adapter.read_response(tmp_path, 1) == answer
    assert json.loads(adapter.read_response(tmp_path, 1))['final_answer'] == 'ALPHA'
    assert len(adapter.read_trace(tmp_path, 1)[-1].text) < len(answer)

    # Exercise the generic actor boundary, not just the native reader helper.
    adapter.seed = lambda root, seed: root.mkdir(exist_ok=True)
    adapter.run_episode = lambda spec: EpisodeResult(episode=1, ok=True, turns=1)
    adapter.staged_activation = False
    runtime = HarnessActor(backend, HarnessOptions())
    runtime.adapter = lambda spec: adapter
    actor = ActorSpec(model='fixture', runtime=PluginSpec(plugin=PluginRef(name=f'proteus.harness.{backend}', version='1')))
    result = runtime.execute(actor, ActorRequest(prompt='Return the structured answer.'), tmp_path)
    assert result.text == answer and json.loads(result.text)['final_answer'] == 'ALPHA'
    assert len(result.trace[-1]['text']) < len(result.text)


def test_final_response_does_not_return_trace_only_codex_reasoning_or_errors(tmp_path):
    directory = tmp_path / '.codex-state/sessions'
    directory.mkdir(parents=True)
    (tmp_path / 'traces').mkdir()
    (tmp_path / 'traces/ep001.json').write_text(json.dumps({'act': 'session.jsonl'}))
    (directory / 'session.jsonl').write_text(json.dumps({'type': 'item.completed',
        'item': {'type': 'reasoning', 'text': 'not a task answer'}}) + '\n' + json.dumps({
        'type': 'turn.failed', 'error': {'message': 'failed provider'}}) + '\n')
    assert CodexHarness().read_response(tmp_path, 1) == ''
