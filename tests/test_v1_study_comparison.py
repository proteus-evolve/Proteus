"""Controlled ablations, seed coverage and explicit reference-profile summaries."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from proteus.core.serialization import ContractError
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.search_policy import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
from proteus.recipes.study import StudyCase, StudySpec, compare_runs


def study(root, *, extra_budget=False, metric_change=False, source_change=False):
    registry = builtin_registry()
    register(registry)
    cases = []
    for seed in (0, 1):
        for condition, policy in [('mean', 'proteus.scalar_best'), ('worst', 'example.maximin')]:
            target = root/condition/str(seed)
            recipe = load_recipe(prepare(target/'study', policy=policy))
            recipe = replace(recipe, seed=seed, execution=replace(recipe.execution, seed=seed))
            if condition == 'worst' and extra_budget:
                recipe = replace(recipe, budget=replace(recipe.budget, max_evaluations=8))
            if condition == 'worst' and metric_change:
                recipe = replace(recipe, evaluations=tuple(replace(item, evaluator=plugin('proteus.exact_match'))
                    if item.id == 'test' else item for item in recipe.evaluations))
            recipe = registry.resolve(recipe)
            lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/study-fixture', revision=('1' if source_change and seed == 1 else '0')*40)])
            result = run_recipe(recipe, registry=registry, lock=lock, root=target/'run', artifact_source=target/'study/artifact-store')
            assert result.status.value == 'completed', result.diagnostic
            cases.append(StudyCase(condition=condition, run=str((target/'run').relative_to(root)), seed=seed, evaluation_ids=('test',)))
    return StudySpec(title='Final policy ablation', mode='controlled', seeds=(0,1), cases=tuple(cases),
                     varied_fields=('/search/final',), baseline='mean')


def test_controlled_final_policy_ablation_keeps_paired_seeds_and_unknown_costs(tmp_path):
    spec = study(tmp_path)
    public = compare_runs(spec, base=tmp_path)
    assert all(row['quality'] is None for row in public['rows'])
    report = compare_runs(spec, base=tmp_path, include_private=True)
    summary = {row['condition']: row for row in report['summaries']}
    assert summary['mean']['metrics']['quality'] == {'n':2,'known':2,'mean':.8,'sample_sd':0.0}
    assert summary['worst']['metrics']['quality'] == {'n':2,'known':2,'mean':.4,'sample_sd':0.0}
    assert summary['mean']['metrics']['cost_usd']['mean'] is None
    assert summary['mean']['metrics']['cost_usd']['known'] == 0
    assert report['paired_deltas'][0]['quality_delta']['mean'] == pytest.approx(-.4)
    assert {row['seed'] for row in report['paired_deltas'][0]['per_seed']} == {0,1}
    assert all(row['declared_variations'].keys() == {'/search/final'} for row in report['rows'])


def test_unmentioned_budget_change_is_rejected_but_can_be_declared(tmp_path):
    spec = study(tmp_path, extra_budget=True)
    with pytest.raises(ContractError, match='undeclared'):
        compare_runs(spec, base=tmp_path)
    report = compare_runs(replace(spec, varied_fields=(*spec.varied_fields, '/budget/max_evaluations')),
                          base=tmp_path, include_private=True)
    assert len(report['rows']) == 4


def test_different_metric_is_not_made_comparable_by_masking_configuration(tmp_path):
    spec = study(tmp_path, metric_change=True)
    with pytest.raises(ContractError, match='task/metric/state'):
        compare_runs(replace(spec, varied_fields=(*spec.varied_fields, '/evaluations')), base=tmp_path, include_private=True)
    profiles = compare_runs(replace(spec, mode='reference_profiles', baseline=None, varied_fields=()),
                            base=tmp_path, include_private=True)
    assert profiles['paired_deltas'] == []
    assert profiles['comparison_semantics'].startswith('descriptive reference profiles')


def test_missing_seed_duplicate_run_and_wrong_seed_are_rejected(tmp_path):
    spec = study(tmp_path)
    with pytest.raises(ContractError, match='every declared seed'):
        replace(spec, cases=spec.cases[:-1])
    same = (spec.cases[0], replace(spec.cases[0], condition='copied'))
    with pytest.raises(ContractError, match='duplicate run'):
        compare_runs(replace(spec, seeds=(0,), cases=same, baseline='mean'), base=tmp_path)
    wrong = (replace(spec.cases[0], seed=5),)
    with pytest.raises(ContractError, match='seed'):
        compare_runs(replace(spec, seeds=(5,), cases=wrong, baseline=None), base=tmp_path)


def test_selection_is_unambiguous_and_final_only(tmp_path):
    spec = study(tmp_path)
    changed = tuple(replace(case, evaluation_ids=('validation',), candidate_id='initial') for case in spec.cases)
    with pytest.raises(ContractError, match='final test'):
        compare_runs(replace(spec, cases=changed), base=tmp_path)
    with pytest.raises(ContractError, match='variation path'):
        compare_runs(replace(spec, varied_fields=('/missing',)), base=tmp_path)


def test_cli_resolves_runs_relative_to_study_and_preserves_reference_status(tmp_path):
    spec = study(tmp_path)
    source = tmp_path/'study.json'
    source.write_text(spec.to_json())
    output = tmp_path/'comparison.json'
    subprocess.run([sys.executable, '-m', 'proteus.cli', 'recipe', 'summarize', str(source),
                    '--out', str(output), '--include-private'], capture_output=True, check=True, cwd=Path.cwd())
    report = json.loads(output.read_text())
    assert all(row['reference']['status'] == 'experimental' for row in report['rows'])
    assert report['study_hash'] and report['comparison_hash']
    assert subprocess.run([sys.executable, '-m', 'proteus.cli', 'recipe', 'summarize', str(source),
                           '--out', str(output)], capture_output=True).returncode == 2


def test_reference_profile_cannot_pool_changed_implementations_across_seeds(tmp_path):
    spec = study(tmp_path, source_change=True)
    with pytest.raises(ContractError, match='lock changes across seeds'):
        compare_runs(replace(spec, mode='reference_profiles', baseline=None, varied_fields=()),
                     base=tmp_path, include_private=True)
