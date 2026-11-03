"""Prepare a two-condition, multi-seed offline search-policy study."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from proteus.core.contracts import require
from proteus.examples.search_policy import prepare as prepare_policy
from proteus.recipes import load_recipe
from proteus.recipes.study import StudyCase, StudySpec


def prepare(root, *, seeds=(0, 1)):
    root = Path(root).resolve()
    require(not root.exists() or not any(root.iterdir()), 'study destination already contains data')
    require(bool(seeds) and len(set(seeds)) == len(seeds) and all(type(seed) is int for seed in seeds),
            'study seeds must be distinct integers')
    cases, experiments = [], []
    common = [sys.executable, '-m', 'proteus.cli', 'recipe']
    hooks = ['--plugin', 'proteus.examples.search_policy:register']
    for seed in seeds:
        for condition, policy in [('mean', 'proteus.scalar_best'), ('worst', 'example.maximin')]:
            directory = root/condition/str(seed)
            path = prepare_policy(directory, policy=policy)
            recipe = load_recipe(path)
            recipe = replace(recipe, seed=seed, execution=replace(recipe.execution, seed=seed),
                evaluations=tuple(replace(item, tasks=replace(item.tasks, seed=seed)) for item in recipe.evaluations))
            path.write_text(recipe.to_json()+'\n')
            run, lock = directory/'run', directory/'lock.json'
            cases.append(StudyCase(condition=condition, run=str(run.relative_to(root)), seed=seed, evaluation_ids=('test',)))
            experiments.append({'condition': condition, 'seed': seed, 'recipe': str(path), 'config_hash': recipe.config_hash,
                'commands': {'check': [*common, 'check', str(path), *hooks],
                    'lock': [*common, 'lock', str(path), *hooks, '--source', 'proteus=<PROTEUS_GIT_ROOT>', '--out', str(lock)],
                    'run': [*common, 'run', str(path), *hooks, '--lock', str(lock),
                            '--artifact-store', str(directory/'artifact-store'), '--out', str(run)],
                    'resume': [*common, 'resume', str(path), *hooks, '--lock', str(lock), '--out', str(run)]}})
    spec = StudySpec(title='Offline final-selector ablation', mode='controlled', seeds=tuple(seeds), cases=tuple(cases),
                     varied_fields=('/search/final',), baseline='mean')
    (root/'study.json').write_text(spec.to_json()+'\n')
    (root/'experiments.json').write_text(json.dumps({'experiments': experiments,
        'notice': 'Deterministic API demonstration, not a model-quality result; repeated seeds exercise aggregation.',
        'summarize': [*common, 'summarize', str(root/'study.json'), '--include-private', '--out', str(root/'comparison.json')]}, indent=2)+'\n')
    return root/'study.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--seeds', type=int, nargs='+', default=[0, 1])
    args = parser.parse_args()
    print(prepare(args.out, seeds=args.seeds))


if __name__ == '__main__':
    main()
