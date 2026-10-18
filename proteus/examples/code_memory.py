"""Prepare a deterministic local code/memory protocol fixture; no live model inference."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from proteus.artifacts.codecs import FileContent
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import BudgetLimits, Outcome, PluginRef, require
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.execution.code_records import CodeCall, CodeState
from proteus.examples.candidate_evaluation import plugin, prepare as prepare_comparison
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import ActorSpec

SOURCE = '''class Memory:
    def __init__(self, model):
        self.model, self.factor = model, 1
    def learn_from_batch(self, rows):
        for row in rows:
            self.factor = int(self.model(str(row['ground_truth'] // row['input'])))
    def predict(self, value):
        return int(self.model(str(value * self.factor))), {'factor': self.factor}
    def get_state(self):
        return {'factor': self.factor}
    def set_state(self, state):
        self.factor = state['factor']
'''


class MemoryStudy:
    def run(self, services, restored_state):
        candidate = services.initial_candidate()
        state = CodeState(artifact_hash=candidate.artifact.content_hash)
        cold = services.values.publish('proteus.code.state@1', state)
        services.evaluate(candidate, 'validation', inputs={'memory': cold})
        learned = services.invoke_candidate(candidate, CodeCall(method='learn_from_batch', state=state,
            args=([{'input': 1, 'ground_truth': 2}, {'input': 5, 'ground_truth': 10}],)))
        require(learned.status == Outcome.SUCCEEDED, learned.diagnostic)
        warm = services.values.publish('proteus.code.state@1', learned.state)
        services.evaluate(candidate, 'validation', inputs={'memory': warm})
        services.evaluate(candidate, 'test', inputs={'memory': warm})
        return MethodResult(outputs={'memory': warm, 'learning': services.values.publish('proteus.code.result@1', learned)})


def validate(recipe, options):
    require(not recipe.stages and not recipe.search and recipe.tasks is None, 'code-memory fixture has no unused stages or policies')
    require(recipe.subject is not None and recipe.subject.runner.plugin.name == 'proteus.code_subject', 'fixture needs code subject')
    require({spec.id for spec in recipe.evaluations} == {'validation', 'test'}, 'fixture needs validation and test')


def register(registry):
    registry.register(Plugin(ref=PluginRef(name='example.code_memory', version='1'), kind='method', options=EmptyOptions,
        factory=lambda options: MemoryStudy(), implementation_hash=implementation_hash({'code_memory.py': Path(__file__)}),
        source='proteus', capabilities=('run.journal', 'run.resume'), state_schema=EmptyOptions, validate=validate))


def prepare(root: Path) -> Path:
    path = prepare_comparison(root)
    recipe = load_recipe(path)
    registry = builtin_registry()
    store = ArtifactStore(path.parent/'artifact-store', registry)
    artifact = store.put(PluginRef(name='proteus.file_tree', version='1'), {'candidate.py': FileContent(SOURCE.encode())})
    recipe = replace(recipe, name='offline-code-memory-fixture', method=plugin('example.code_memory'),
        subject=replace(recipe.subject, runner=plugin('proteus.code_subject'), initial_artifact=artifact,
                        actor='model', input_types={'memory': 'proteus.code.state@1'}),
        actors={'model': ActorSpec(runtime=plugin('proteus.echo'))}, budget=BudgetLimits(max_model_calls=0, max_evaluations=5))
    path.write_text(recipe.to_json()+'\n', encoding='utf-8')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    print(prepare(parser.parse_args().out))


if __name__ == '__main__':
    main()
