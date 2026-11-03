"""Offline subprocess integration used to demonstrate configurable harness roles.

This small program is a mechanism fixture, not a third-party agent or model. It
executes only on the explicitly trusted host environment. No provider is called.
"""

from dataclasses import dataclass
import inspect
import json
from pathlib import Path
import subprocess
import sys

from proteus.adapters.minimal import MinimalHarness
from proteus.core.adapter import EpisodeResult
from proteus.core.native_inputs import native_input
from proteus.core.native_usage import usage_evidence
from proteus.core.plugins import implementation_hash
from proteus.core.serialization import content_hash
from proteus.examples.coding_workspace import write_fixture
from proteus.execution.actors import HarnessOptions
from proteus.execution.harnesses import HarnessIntegration, register_harness


PROGRAM = '''import json, sys
from pathlib import Path
MARK=1
''' + inspect.getsource(write_fixture) + '''
r=json.load(sys.stdin)
harness=Path(r['harness'])
if r['mode']=='reflection':
    print('```\\nfactor=2\\n```')
elif r['mode']=='coding':
    write_fixture(Path(r['task']))
elif r['mode']=='task':
    (harness/'notes/prediction.md').write_text(str(MARK*2))
elif r['phase']=='act':
    code=Path(__file__).read_text()
    if r['break_first'] and r['episode']==1:
        code='invalid Python ('
    else:
        code=code.replace('MARK=1','MARK=2')
    (harness/'controller.py').write_text(code)
'''


@dataclass(frozen=True, kw_only=True)
class ProcessHarnessOptions(HarnessOptions):
    break_first: bool = False


class ProcessHarness(MinimalHarness):
    staged_activation = supports_custom_phases = True

    def __init__(self, name, role, options):
        super().__init__()
        self.name, self.role, self.options = name, role, options

    def seed(self, harness, rng_seed=0):
        harness.mkdir(parents=True, exist_ok=True)
        (harness / 'controller.py').write_text(PROGRAM)
        self.prepare_candidate(harness)

    def validate_candidate(self, harness):
        try:
            compile((harness / 'controller.py').read_text(), 'controller.py', 'exec')
        except (SyntaxError, OSError) as exc:
            return f'{type(exc).__name__}: {exc}'
        return ''

    def run_episode(self, spec):
        source = spec.active_root / 'controller.py'
        harness, task = spec.root / 'harness', spec.root / 'task'
        mode = ('coding' if self.role == 'workspace' else 'evolve' if self.role == 'evolution'
                else 'task' if any('Task input:' in p for p in spec.phase_prompts.values()) else 'reflection')
        trace = spec.root / 'traces' / f'ep{spec.episode:03d}.jsonl'
        trace.parent.mkdir(exist_ok=True)
        for phase in spec.phases:
            payload = {'mode': mode, 'phase': phase, 'episode': spec.episode,
                'harness': str(harness), 'task': str(task), 'break_first': self.options.break_first,
                'source_hash': content_hash(source.read_text())}
            with native_input(spec, backend=self.name, phase=phase, scope='policy_prompt', payload=payload) as receipt:
                proc = subprocess.run([sys.executable, '-I', str(source)], input=json.dumps(payload),
                    text=True, capture_output=True, timeout=10, env={'PYTHONDONTWRITEBYTECODE': '1'})
                receipt['usage'] = usage_evidence('local_mock')
            with trace.open('a') as stream:
                stream.write(json.dumps({'turn': 1, 'phase': phase, 'text': proc.stdout.strip()}) + '\n')
            if proc.returncode:
                return EpisodeResult(episode=spec.episode, ok=False, error=proc.stderr)
        return EpisodeResult(episode=spec.episode, ok=True)


def register(registry, name='example.process_harness'):
    root = Path(__file__).resolve().parents[1]
    return register_harness(registry, HarnessIntegration(name=name,
        factory=lambda role, spec, options: ProcessHarness(name, role, options),
        options={role: ProcessHarnessOptions for role in ('actor', 'workspace', 'evolution')},
        capabilities=('harness.custom_phases', 'harness.workspace', 'harness.candidate_tree',
                      'harness.staged_activation', 'harness.model_free'),
        implementation_hash=implementation_hash({str(p.relative_to(root)): p for p in root.rglob('*.py')}),
        source='proteus', continuity='fresh', model_call_bound=0))
