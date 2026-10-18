"""Discover bundled research entry points without importing optional method code."""

from dataclasses import dataclass
import importlib
import json
from pathlib import Path
import sys
from typing import Literal

from proteus.core.contracts import require
from proteus.core.serialization import Contract


@dataclass(frozen=True, kw_only=True)
class Entry(Contract):
    id: str
    family: str
    kind: Literal['example', 'benchmark']
    summary: str
    module: str
    plugins: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    prepare_arguments: tuple[str, ...] = ('--out', '<OUTPUT>')
    documentation: str
    result_status: Literal['offline_fixture', 'model_results_pending']

    def describe(self):
        return {**self.to_dict(), 'prepare_command':
                [sys.executable, '-m', self.module, *self.prepare_arguments]}


def example(id, family, summary, module, plugins=(), requirements=(), documentation='V1_PLUGINS.md'):
    return Entry(id=id, family=family, kind='example', summary=summary, module='proteus.examples.'+module,
        plugins=plugins, requirements=requirements, documentation='docs/'+documentation, result_status='offline_fixture')


STUDY_ARGUMENTS = ('prepare', '--out', '<OUTPUT>', '--data', '<PINNED_DATA>',
                   '--source', '<CLEAN_PROTEUS_CHECKOUT>', '--settings', '<ACTOR_AND_BUDGET_SETTINGS_JSON>')

ENTRIES = (
    example('candidate-comparison', 'framework', 'Two exact candidates with observed and private evaluations.',
            'candidate_evaluation', documentation='V1_CONTRACTS.md'),
    example('search-policy', 'framework', 'Same candidates and scores; compare mean and worst-task final selection.',
            'search_policy', ('proteus.examples.search_policy:register',), documentation='V1_RECIPE_CATALOG.md'),
    example('gepa', 'gepa', 'Pinned single-mutation/no-merge search with deterministic model fixtures.',
            'gepa_prompt', ('proteus.methods.gepa.method:register', 'proteus.examples.gepa_prompt:register'),
            ('Pinned optional GEPA installation; see V1_GEPA_METHOD.md.',), 'V1_GEPA_METHOD.md'),
    example('ace', 'ace', 'Playbook, generator, reflector and curator under an offline fixture.',
            'ace_playbook', ('proteus.methods.ace.method:register', 'proteus.examples.ace_playbook:register'),
            documentation='V1_ACE_METHOD.md'),
    example('meta-harness', 'meta-harness', 'Code proposals, memory learning and private tests on fixture data.',
            'meta_harness_fixture', ('proteus.methods.meta_harness.method:register',
                                    'proteus.examples.meta_harness_fixture:register'), documentation='V1_META_HARNESS.md'),
    example('combee-ace', 'combee', 'Parallel ACE sample learning and aggregation with fixed default batches.',
            'combee_ace', ('proteus.methods.ace.batch:register', 'proteus.examples.ace_playbook:register'),
            documentation='V1_COMBEE_ACE.md'),
    example('combee-gepa', 'combee', 'Combee reflection inside the pinned GEPA search loop.',
            'combee_gepa', ('proteus.examples.combee_gepa:register',),
            ('Pinned optional GEPA installation; see V1_COMBEE_GEPA.md.',), 'V1_COMBEE_GEPA.md'),
    example('coral', 'coral', 'Two persistent deterministic workers, shared knowledge and event interventions.',
            'coral_search', ('proteus.examples.coral_search:register',), documentation='V1_CORAL_METHOD.md'),
    Entry(id='gepa-aime', family='gepa', kind='benchmark', summary='AIME baseline/optimized multi-seed study.',
        module='proteus.examples.gepa_aime', prepare_arguments=STUDY_ARGUMENTS,
        requirements=('Pinned GEPA and complete 45/45/30 AIME data.', 'Solver/reflection actors and explicit budgets.'),
        documentation='docs/V1_GEPA_AIME.md', result_status='model_results_pending'),
    Entry(id='ace-formula', family='ace', kind='benchmark', summary='Formula offline/online ACE and baseline study.',
        module='proteus.examples.ace_formula', prepare_arguments=STUDY_ARGUMENTS,
        requirements=('Complete pinned Formula 500/300/200 data.', 'Generator/reflector/curator actors and budgets.'),
        documentation='docs/V1_ACE_FORMULA.md', result_status='model_results_pending'),
    Entry(id='meta-harness-mce', family='meta-harness', kind='benchmark',
        summary='Three original MCE datasets, code proposals, baselines and private tests.',
        module='proteus.examples.meta_harness',
        prepare_arguments=('--out', '<OUTPUT>', '--reference-source', '<PINNED_META_HARNESS_SOURCE>',
            '--model-config', '<MODEL_ACTOR_JSON>', '--proposer-config', '<PROPOSER_ACTOR_JSON>',
            '--harbor-python', '<HARBOR_PYTHON>'),
        plugins=('proteus.methods.meta_harness.method:register',),
        requirements=('Pinned upstream source/data and Harbor/Docker.', 'Managed model and workspace proposer actors.'),
        documentation='docs/V1_META_HARNESS.md', result_status='model_results_pending'),
    Entry(id='combee-formula', family='combee', kind='benchmark',
        summary='ACE, naive-parallel, fixed and dynamic Combee under common Formula conditions.',
        module='proteus.examples.combee_formula', prepare_arguments=STUDY_ARGUMENTS,
        requirements=('Complete pinned Formula data.', 'Shared actor settings and measured quality/cost/time.'),
        documentation='docs/V1_COMBEE_FORMULA.md', result_status='model_results_pending'),
    Entry(id='coral-circle-packing', family='coral', kind='benchmark',
        summary='Original 26-circle task; baseline and one/two-worker research configurations.',
        module='proteus.benchmarks.coral_circle_packing.recipe',
        prepare_arguments=('--out', '<OUTPUT>', '--python', '<HARBOR_PYTHON>'),
        plugins=('proteus.benchmarks.coral_circle_packing.recipe:register',),
        requirements=('Pinned Harbor/Docker and dependency wheels.',
            'For model research, add --worker-spec <WORKER_JSON> and --workers 1 or 2.',
            'Final confirmation is the same problem, not a held-out task.'),
        documentation='docs/V1_CORAL_CIRCLE_PACKING.md', result_status='model_results_pending'),
)


def describe(name=None, *, family=None, kind=None):
    entries = [entry for entry in ENTRIES if (name is None or entry.id == name)
               and (family is None or entry.family == family) and (kind is None or entry.kind == kind)]
    require(name is None or bool(entries), 'unknown recipe catalog entry or incompatible filter')
    return [entry.describe() for entry in entries]


def prepare_example(name, destination):
    entry = next((entry for entry in ENTRIES if entry.id == name), None)
    require(entry is not None, 'unknown recipe catalog entry')
    require(entry.kind == 'example', 'benchmark preparation requires explicit data/runtime/settings; use recipe catalog NAME')
    root = Path(destination).resolve()
    require(not root.exists() or not any(root.iterdir()), 'example destination already contains data')
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.config import load_recipe
    registry = builtin_registry()
    # Registration/preflight precedes preparation so missing optional methods do
    # not leave a misleading half-prepared experiment.
    for hook in entry.plugins:
        registry.load(hook)
    path = importlib.import_module(entry.module).prepare(root)
    recipe = registry.resolve(load_recipe(path))
    Path(path).write_text(recipe.to_json()+'\n')
    hooks = [argument for hook in entry.plugins for argument in ('--plugin', hook)]
    common = [sys.executable, '-m', 'proteus.cli', 'recipe']
    lock, run = root/'lock.json', root/'run'
    commands = {
        'check': [*common, 'check', str(path), *hooks],
        'lock': [*common, 'lock', str(path), *hooks, '--source', 'proteus=<PROTEUS_GIT_ROOT>', '--out', str(lock)],
        'run': [*common, 'run', str(path), *hooks, '--lock', str(lock), '--out', str(run)],
        'resume': [*common, 'resume', str(path), *hooks, '--lock', str(lock), '--out', str(run)],
    }
    if recipe.subject is not None:
        commands['run'] += ['--artifact-store', str(root/'artifact-store')]
    result = {'catalog_entry': entry.describe(), 'recipe': str(path), 'config_hash': recipe.config_hash,
              'commands': commands, 'notice': 'Offline fixture only; no model benchmark result is claimed.'}
    (root/'example.json').write_text(json.dumps(result, indent=2)+'\n')
    return result
