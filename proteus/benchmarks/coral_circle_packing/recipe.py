"""Prepare a baseline or one/two-worker CORAL study on the original packing task."""

import argparse
import base64
import json
from pathlib import Path
import shlex

from proteus.artifacts.codecs import FileContent
from proteus.artifacts.store import ArtifactStore
from proteus.benchmarks.coral_circle_packing import ROOT, REVISION, reference
from proteus.core.contracts import BudgetLimits, FeedbackVisibility, PluginRef, require
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.serialization import content_hash
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.evidence.records import EvidenceAccess
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.harbor_evaluation import IMAGE
from proteus.execution.builds import SourceBuildSpec
from proteus.execution.harbor import identify_runtime
from proteus.methods.coral.method import register as register_method
from proteus.methods.coral.policy import register as register_policy
from proteus.recipes.builtins import Text, builtin_registry
from proteus.recipes.config import (
    ActorSpec, EnvironmentSpec, EvaluationSpec, EventPolicySpec, ExecutionSpec, GatewaySpec, KnowledgeSpec,
    PluginSpec, RecipeConfig, ReferenceSpec, StageSpec, SubjectSpec, TaskSuiteSpec, WorkerSpec,
)

INSTRUCTION = '''Pack 26 circles into a unit square to maximize the sum of radii.
Edit initial_program.py, preserving run() -> (centers, radii, sum_radii).
centers has shape (26, 2), radii shape (26,). Every radius is nonnegative;
each circle stays within the square and no two circles overlap.
The formal score is the verified sum of radii / 2.635977, with 1e-6 tolerance.
NumPy 2.2.6 and SciPy 1.15.3 are installed for evaluation. The timeout is 600s.
The claimed sum is ignored; an independent private verifier checks geometry.
'''

RESEARCH = '''You are a persistent research worker optimizing the circle-packing task.
Your editable seed is in candidate/. Read candidate/RESEARCH.md and inspect the
original initial_program.py. Explore multiple approaches; use formal evaluation
to compare your results, and consult shared notes and skills from other workers.
Keep main.py compatible with the declared geometry export protocol.

Use the provided stdlib gateway helper from the workspace root:
  python candidate/research.py submit --operation UNIQUE_ID --directory candidate
  python candidate/research.py poll TICKET_ID
  python candidate/research.py snapshot --operation UNIQUE_OBSERVATION_ID
  python candidate/research.py read REVISION_HASH
  python candidate/research.py publish --operation UNIQUE_ID --kind notes --key IDEA --file note.md
For skills use --kind skills; --sources FILE accepts a JSON array of exact
revision/evidence references. Cite the note or shared_attempt from a formal
result when publishing a derived skill. To update a key, pass --expected FILE
containing its current revision reference. A conflict requires reading current
state and making a new, explicit write. Retry transport failures using the same
operation ID and bytes. Use a new ID for new content or a new observation.
Only the authenticated gateway's formal scores count. Do not report a
candidate's claimed score as a verified result. Continue research until stopped
by the controller; respond to reflect, consolidate and pivot interventions.
'''


class PackingEvidence:
    def run(self, context, inputs):
        view = inputs['history']
        batches = [json.loads(context.read_evidence(view, item.path))
                   for item in view.entries if item.kind == 'evaluation']
        return {'report': Text(text=json.dumps({'candidates': view.candidate_ids, 'evaluations': batches}))}


def register(registry):
    register_method(registry)
    register_policy(registry)
    registry.register(Plugin(ref=PluginRef(name='coral.packing_evidence', version='1'), kind='stage',
        options=EmptyOptions, factory=lambda options: PackingEvidence(),
        implementation_hash=implementation_hash({'recipe.py': Path(__file__)}), source='proteus',
        capabilities=('stage.replay',), inputs={'history': 'proteus.evidence.view@1'},
        outputs={'report': 'proteus.text@1'}))


def build_spec():
    dependencies = reference()['dependencies']
    requirements = ''.join(f"{item['url']} --hash=sha256:{item['sha256']}\n" for item in dependencies)
    encoded = base64.b64encode(requirements.encode()).decode()
    script = ('import base64; from pathlib import Path; '
              f'Path("/tmp/proteus-packing.lock").write_bytes(base64.b64decode({encoded!r}))')
    return SourceBuildSpec(commands=(
        'python3 -I -c '+shlex.quote(script),
        'python3 -I -m pip install --no-deps --no-compile --require-hashes --only-binary=:all: '
        '--target /opt/proteus/build/.proteus-deps -r /tmp/proteus-packing.lock',),
        network='default', platform='linux/amd64', timeout_seconds=300)


def prepare(root, python, *, worker_runtime=None, workers=1, submissions=20, seed=0, runtime=None):
    root = Path(root).resolve()
    require(workers in (1, 2) and submissions >= 1, 'choose one/two workers and a positive submission limit')
    require(not root.exists() or not any(root.iterdir()), 'packing study output already contains data')
    require(worker_runtime is None or type(worker_runtime) is PluginSpec, 'worker runtime must be a typed plugin spec')
    record = reference()
    identity = runtime or identify_runtime(python)
    registry = builtin_registry()
    register(registry)
    root.mkdir(parents=True, exist_ok=True)
    task = root/'task'
    (task/'tests').mkdir(parents=True)
    (task/'instruction.md').write_text(INSTRUCTION)
    (task/'tests/verify.py').write_bytes((ROOT/'verify.py').read_bytes())
    (task/'tests/test.sh').write_text('#!/bin/sh\nset -eu\npython3 -I /tests/verify.py\n')
    (task/'task.toml').write_text(f'''schema_version = "1.4"
[environment]
docker_image = "{IMAGE}"
network_mode = "public"
cpus = 1
memory_mb = 2048
build_timeout_sec = 300
[agent]
timeout_sec = 600
[verifier]
environment_mode = "separate"
timeout_sec = 30
''')
    store = ArtifactStore(root/'artifact-store', registry)
    task_ref = store.capture_tree(task)
    initial = store.put(PluginRef(name='proteus.file_tree', version='1'), {
        'initial_program.py': FileContent((ROOT/'seed.reference.py').read_bytes()),
        'main.py': FileContent((ROOT/'candidate.py').read_bytes()),
        'research.py': FileContent((ROOT/'research.py').read_bytes()),
        'RESEARCH.md': FileContent((INSTRUCTION+'\n'+RESEARCH).encode())})
    dataset = TaskDataset(version='coral-circle-packing-'+REVISION[:7], records=tuple(
        TaskRecord(task=Task(id=name, version='1', split=split,
            input={'path': str(task), 'tree_hash': task_ref.content_hash}), reference=None)
        for name, split in [('packing', 'optimization'), ('confirmation', 'test')]))
    data = root/'tasks.json'
    data.write_text(dataset.to_json()+'\n')

    def evaluation(name, split):
        hidden = split == 'test'
        return EvaluationSpec(id=name, evaluator=plugin('proteus.harbor_reward'),
            tasks=TaskSuiteSpec(suite=plugin('proteus.json_tasks', path=str(data)), dataset_version=dataset.version,
                dataset_hash=content_hash(dataset), split=split, task_ids=(name,), seed=seed),
            purpose='test' if hidden else 'optimization', selection_eligible=not hidden,
            visibility=FeedbackVisibility.HIDDEN if hidden else FeedbackVisibility.OBSERVED,
            feedback_timing='never' if hidden else 'after_batch')

    build = build_spec()
    common = dict(reference=ReferenceSpec(profile='adapted', status='experimental',
        code=record['repository'], revision=REVISION, deviations=(
            'Independent JSON geometry verifier, explicit RNG seed and pinned dependency wheels; see NOTICE.',
            'Single shared space; serialized formal results and bounded submissions/polls/restarts.',
            'Explicit experimental 5/10/5 heartbeat cadence and prompts adapted to the mediated gateway.',
            'Final confirmation repeats the optimization problem with a fresh trial seed; it is not a held-out task.',
            'No real model result is implied by preparing this recipe.')),
        subject=SubjectSpec(runner=plugin('proteus.harbor_subject'), initial_artifact=initial),
        execution=ExecutionSpec(seed=seed, timeout_seconds=600, environment=EnvironmentSpec(
            backend=plugin('proteus.harbor', python=str(Path(python).absolute()), runtime_hash=identity['runtime_hash'],
                source_build=build.to_dict(), bridge_timeout_seconds=1000), provider='docker', image_digest=IMAGE,
            network='unrestricted', resources={'cpus': 1, 'memory_mb': 2048}, build_hash=content_hash(build))),
        evaluations=(evaluation('packing', 'optimization'), evaluation('confirmation', 'test')))
    if worker_runtime is None:
        recipe = RecipeConfig(name='coral-circle-packing-baseline', actors={},
            method=plugin('proteus.compare', evaluations=['packing', 'confirmation'], variants=[]),
            budget=BudgetLimits(max_evaluations=2, max_concurrency=1), **common)
    else:
        names = ('a', 'b')[:workers]
        access = EvidenceAccess(evaluation_ids=('packing',), splits=('optimization',))
        # ActorSpec anchors the role budget. The evidence stage is model-free;
        # persistent research execution uses the declared WorkerSpec runtime.
        recipe = RecipeConfig(name=f'coral-circle-packing-{workers}-workers',
            actors={'researcher': ActorSpec(runtime=plugin('proteus.echo'), budget=BudgetLimits(max_concurrency=workers))},
            stages=(StageSpec(id='feedback', role='researcher', operator=plugin('coral.packing_evidence'), evidence=access,
                input_types={'history': 'proteus.evidence.view@1'}, output_types={'report': 'proteus.text@1'},
                read_scopes=('proteus.evidence.view@1',), write_scopes=('proteus.text@1',)),),
            method=plugin('coral.single_island', gateway='formal', event_policy='heartbeat', bootstrap_prompt=RESEARCH,
                max_submissions=submissions, max_polls=7200, poll_seconds=.5, stop_timeout_seconds=120,
                final_evaluation='confirmation'),
            workers=tuple(WorkerSpec(id=name, role='researcher', runtime=worker_runtime, gateway='formal') for name in names),
            gateways=(GatewaySpec(id='formal', members={name: 'feedback' for name in names}, evaluations=('packing',),
                knowledge_space='research', knowledge_access='read_write', max_evaluations_per_worker=submissions,
                bind_host='0.0.0.0' if worker_runtime.plugin.name == 'proteus.worker.harbor' else '127.0.0.1'),),
            knowledge=(KnowledgeSpec(id='research', roles=('researcher',), evidence=access),),
            event_policies=(EventPolicySpec(id='heartbeat', gateway='formal', pause_timeout_seconds=120,
                operator=plugin('coral.heartbeat', rules=[
                    {'name': 'reflect', 'trigger': 'evaluations', 'every': 5, 'prompt': 'Reflect on formal results and share useful findings in notes.'},
                    {'name': 'consolidate', 'trigger': 'evaluations', 'every': 10, 'scope': 'global', 'prompt': 'Consolidate supported findings into sourced reusable skills.'},
                    {'name': 'pivot', 'trigger': 'plateau', 'every': 5, 'prompt': 'Inspect the plateau and try a different approach, using shared findings.'}])),),
            search={'final': plugin('proteus.scalar_best')},
            budget=BudgetLimits(max_evaluations=submissions+2, max_concurrency=workers+1), **common)
    recipe = registry.resolve(recipe)
    (root/'reference.json').write_text(json.dumps(record, indent=2)+'\n')
    path = root/'recipe.json'
    path.write_text(recipe.to_json()+'\n')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--python', required=True, help='Pinned external Harbor Python')
    parser.add_argument('--worker-spec', type=Path, help='PluginSpec JSON; omit for the unmodified seed baseline')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=1)
    parser.add_argument('--submissions', type=int, default=20)
    parser.add_argument('--seed', type=int, default=0)
    args = parser.parse_args()
    runtime = PluginSpec.from_json(args.worker_spec.read_text()) if args.worker_spec else None
    print(prepare(args.out, args.python, worker_runtime=runtime, workers=args.workers,
                  submissions=args.submissions, seed=args.seed))


if __name__ == '__main__':
    main()
