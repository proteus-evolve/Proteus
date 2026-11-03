"""A deterministic file-proposer fixture, not a Meta-Harness benchmark.

It reads multiple historical candidates from files, returns three independent
candidate trees, evaluates them, and resumes without rerunning the proposer.
Swap the fixture actor for an explicitly configured workspace coding runtime to
exercise the same service boundary with a model.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path

from proteus.core.contracts import OperationStatus, PluginRef, RuntimeIdentity, require
from proteus.core.methods import ActorResult, MethodResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.evaluation.records import zero_usage
from proteus.evidence.records import EvidenceAccess
from proteus.examples.candidate_evaluation import plugin, prepare as prepare_comparison
from proteus.methods.compare import CompareOptions
from proteus.recipes import load_recipe
from proteus.recipes.builtins import Text
from proteus.recipes.config import ActorSpec, StageSpec


PROMPT = '''Inspect input/ and the candidate code and released results under history/.
This deterministic demonstration requests three independent Python candidates
whose outputs are the task input times 2, 3 and 4. Write output/proposals.json as
{"candidates":[{"name":"double","directory":"double"}, ...]}. Put a main.py
in each declared directory. main.py reads the existing JSON subject protocol from
stdin and prints {"output": ...}. Keep all helper files within its directory.
'''


def write_fixture(task):
    """Used by offline and real-container tests; performs no inference."""
    task = Path(task)
    history = json.loads((task / 'history/view.json').read_text())
    code_paths = [entry['path'] for entry in history['entries'] if entry['kind'] == 'artifact']
    assert len(history['candidate_ids']) >= 2 and len(code_paths) >= 2
    sources = [(task / 'history' / name).read_text() for name in code_paths]
    assert len(set(sources)) >= 2
    assert (task / 'input/main.py').read_text() in sources
    assert all(entry['split'] != 'test' for entry in history['entries'])
    output = task / 'output'
    candidates = []
    for name, factor in (('double', 2), ('triple', 3), ('quadruple', 4)):
        (output / name).mkdir()
        (output / name / 'main.py').write_text(
            'import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({"output":r["task"]["input"]*'
            + str(factor) + '}))\n')
        candidates.append({'name': name, 'directory': name})
    (output / 'proposals.json').write_text(json.dumps({'candidates': candidates}))
    return ({'kind': 'file_read', 'path': 'input/main.py'},
            *({'kind': 'file_read', 'path': 'history/' + name} for name in code_paths))


class FixtureFileActor:
    model_call_bound = 0

    def execute(self, spec, request, workspace):
        require(request.workspace is not None, 'fixture requires workspace inputs')
        trace = write_fixture(workspace / 'task')
        return ActorResult(text='Three deterministic file proposals.', trace=trace,
            runtime=RuntimeIdentity(runtime=spec.runtime.plugin), usage=zero_usage())


class WorkspaceStudy:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        initial = services.initial_candidate()
        codec = services.artifacts._codec(initial.artifact.codec)
        population = [initial, *(services.fork_candidate(initial, candidate_id=variant.id,
            delta=codec.delta_schema.from_dict(variant.delta)) for variant in self.options.variants)]
        for candidate in population:
            for evaluation in self.options.evaluations:
                services.evaluate(candidate, evaluation)
        history = services.evidence_view('propose')
        stage = services.stage('propose', {
            'prompt': services.values.publish('proteus.text@1', Text(text=PROMPT)),
            'seed': services.values.publish('proteus.artifact_ref@1', initial.artifact), 'history': history})
        result = services.values.get(stage.outputs['response'])
        require(result.status == OperationStatus.COMPLETED and result.workspace_artifact is not None,
                'proposer did not return captured files')
        files = services.artifacts.files(result.workspace_artifact)
        proposals = json.loads(files['proposals.json'].data)['candidates']
        require(len(proposals) == 3, 'this fixture requests exactly three candidates')
        names = [row['name'] for row in proposals]
        require(len(set(names)) == len(names), 'duplicate proposal name')
        for row in proposals:
            directory = row['directory']
            require(directory == row['name'] and directory.isidentifier(), 'invalid fixture proposal directory')
            prefix = directory + '/'
            source = {name[len(prefix):]: item for name, item in files.items() if name.startswith(prefix)}
            require('main.py' in source, 'proposal has no entry point')
            artifact = services.artifacts.put(initial.artifact.codec, source)
            candidate = services.create_candidate(artifact, candidate_id='proposal-' + row['name'],
                                                  parents=tuple(population))
            services.evaluate(candidate, 'validation')
        return MethodResult(outputs={'history': history, 'proposals': stage.outputs['response']})


def register(registry):
    digest = implementation_hash({'coding_workspace.py': Path(__file__)})
    registry.register(Plugin(ref=PluginRef(name='example.workspace_study', version='1'), kind='method',
        options=CompareOptions, factory=WorkspaceStudy, implementation_hash=digest, source='proteus',
        capabilities=('run.journal', 'run.resume'), state_schema=EmptyOptions))
    registry.register(Plugin(ref=PluginRef(name='example.file_proposer', version='1'), kind='actor',
        options=EmptyOptions, factory=lambda options: FixtureFileActor(), implementation_hash=digest, source='proteus',
        capabilities=('session.fresh', 'actor.execute', 'actor.workspace', 'calls.bounded')))


def prepare(root: Path) -> Path:
    path = prepare_comparison(root)
    recipe = load_recipe(path)
    stage = StageSpec(id='propose', role='proposer', operator=plugin('proteus.workspace'),
        input_types={'prompt': 'proteus.text@1', 'seed': 'proteus.artifact_ref@1', 'history': 'proteus.evidence.view@1'},
        output_types={'response': 'proteus.actor_result@1'},
        read_scopes=('proteus.text@1', 'proteus.artifact_ref@1', 'proteus.evidence.view@1'),
        write_scopes=('proteus.actor_result@1',),
        evidence=EvidenceAccess(evaluation_ids=('validation',), splits=('validation',)))
    recipe = replace(recipe, name='offline-file-proposer-fixture',
        method=plugin('example.workspace_study', **dict(recipe.method.options)), stages=(stage,),
        actors={'proposer': ActorSpec(runtime=plugin('example.file_proposer'))},
        budget=replace(recipe.budget, max_evaluations=12))
    path.write_text(recipe.to_json() + '\n')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    print(prepare(parser.parse_args().out))


if __name__ == '__main__':
    main()
