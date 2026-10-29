"""Constructor/schema parity, paired randomization and explicit model ownership."""

from dataclasses import dataclass, field, replace
import json
import sqlite3

import pytest
from jsonschema import Draft202012Validator

from proteus.core.contracts import BudgetLimits
from proteus.core.ledger import Amounts, Ledger
from proteus.core.serialization import Contract, ContractError


@dataclass(frozen=True, kw_only=True)
class Bounds(Contract):
    scalar: float = field(default=0.5, metadata={'minimum': 0, 'maximum': 1})
    exclusive: float = field(default=0.5, metadata={'exclusiveMinimum': 0, 'exclusiveMaximum': 1})
    text: str = field(default='ok', metadata={'minLength': 1, 'maxLength': 2})
    values: tuple[int, ...] = field(default=(1,), metadata={'minItems': 1, 'maxItems': 2})


@pytest.mark.parametrize('field_name,value', [('scalar', -0.1), ('scalar', 1.1), ('exclusive', 0),
    ('exclusive', 1), ('text', ''), ('text', 'abc'), ('values', ()), ('values', (1, 2, 3))])
def test_schema_constructor_and_json_reader_agree(field_name, value):
    data = Bounds().to_dict()
    data[field_name] = list(value) if isinstance(value, tuple) else value
    assert list(Draft202012Validator(Bounds.json_schema()).iter_errors(data))
    with pytest.raises(ContractError):
        Bounds(**{field_name: value})
    with pytest.raises(ContractError):
        Bounds.from_json(json.dumps(data))


def test_combee_rejects_slope_above_its_schema_maximum():
    from proteus.operators.combee.batching import DynamicBatchOptions
    with pytest.raises(ContractError, match='maximum'):
        DynamicBatchOptions(max_batch_size=8, slope_threshold=2.0)
    assert DynamicBatchOptions(max_batch_size=8, slope_threshold=1.0).slope_threshold == 1


@pytest.mark.parametrize('mismatch', ['execution', 'tasks', None])
def test_controlled_pairs_validate_explicit_seed_mapping(tmp_path, mismatch):
    from proteus.examples.search_policy import prepare, register
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.runner import run_recipe
    from proteus.recipes.study import StudyCase, StudySpec, compare_runs
    from test_v1_plugins import SOURCE
    registry = builtin_registry()
    register(registry)
    cases = []
    for name in ('a', 'b'):
        path = tmp_path / name
        recipe = load_recipe(prepare(path / 'study'))
        recipe = replace(recipe, seed=0, execution=replace(recipe.execution,
            seed=999 if name == 'b' and mismatch == 'execution' else 11),
            evaluations=tuple(replace(item, tasks=replace(item.tasks,
                seed=999 if name == 'b' and mismatch == 'tasks' else 42)) for item in recipe.evaluations))
        result = run_recipe(recipe, registry=registry, lock=registry.make_lock(recipe, [SOURCE]),
            root=path / 'run', artifact_source=path / 'study/artifact-store')
        assert result.status.value == 'completed', result.diagnostic
        cases.append(StudyCase(condition=name, run=name + '/run', seed=0, evaluation_ids=('test',)))
    spec = StudySpec(title='Seed mapping', mode='controlled', seeds=(0,), cases=tuple(cases), baseline='a')
    if mismatch:
        with pytest.raises(ContractError, match='seed mappings'):
            compare_runs(spec, base=tmp_path)
    else:
        report = compare_runs(spec, base=tmp_path, include_private=True)
        assert len(report['paired_deltas']) == 1
        assert all(row['randomization']['execution_seed'] == 11 for row in report['rows'])
        assert all(set(row['randomization']['evaluation_seeds'].values()) == {42} for row in report['rows'])


@pytest.mark.parametrize('workspace', [False, True])
@pytest.mark.parametrize('policy', ['legacy', 'framework', 'runtime'])
def test_dsh_model_ownership_is_explicit_and_preserves_legacy(monkeypatch, workspace, policy):
    from proteus.execution.actors import HarnessActor, WorkspaceHarnessActor, HarnessOptions
    from proteus.recipes.config import ActorSpec
    from test_v1_evaluation import plugin
    cls = WorkspaceHarnessActor if workspace else HarnessActor
    options = HarnessOptions(image='test@sha256:' + 'a' * 64, network='none',
                             phase_timeout_seconds=10, model_selection=policy)
    actor = cls('dsh', options)
    spec = ActorSpec(runtime=plugin('proteus.workspace.dsh' if workspace else 'proteus.harness.dsh'),
                     model=None if policy == 'runtime' else 'requested-model')
    monkeypatch.setattr('proteus.execution.actors.harness_class', lambda name: lambda **kwargs: kwargs)
    result = actor.adapter(spec)
    framework = policy == 'framework' or (policy == 'legacy' and workspace)
    assert result.get('model') == ('requested-model' if framework else None)
    selection = actor.model_selection(spec)
    assert selection['owner'] == ('framework' if framework else 'runtime')
    assert selection['resolved_model'] is None
    if policy == 'runtime':
        with pytest.raises(ContractError, match='model=null'):
            actor.adapter(replace(spec, model='ignored-setting'))


def test_removed_unused_environment_field_fails_instead_of_being_ignored(tmp_path):
    from test_native_inputs import spec
    with pytest.raises(TypeError, match='extra_env'):
        replace(spec(tmp_path), extra_env={'SETTING': 'unused'})


def test_cached_ledger_balances_match_full_audit_and_migrate_v1(tmp_path):
    identity = dict(run_id='audit', config_hash='test', lock_hash=None)
    book = Ledger(tmp_path / 'book.sqlite3', **identity)
    limits = {'run': BudgetLimits(max_model_calls=100, max_concurrency=4),
              'role:agent': BudgetLimits(max_model_calls=100, max_concurrency=4)}
    for i in range(30):
        key = str(i)
        book.reserve(key, scopes=limits, reservation=Amounts(model_calls=2, cost_usd=0.1),
                     metadata={'operation_id': key}, request={'prompt': 'long' * 1024})
        if i % 3:
            book.start(key)
            book.settle(key, actual=Amounts(model_calls=1 if i % 2 else None,
                        cost_usd=0.05 if i % 2 else None), status='failed' if i % 2 else 'completed')
        else:
            book.settle(key, actual=Amounts.zero(), status='cancelled')
        with book._connection() as db:
            for scope in limits:
                assert book._balance(db, scope) == book._scan_balance(db, scope)
    assert book.has_operation('15') and not book.has_operation('missing')
    before = book.balance()
    # Recreate the previous on-disk schema from genuine entries and settlements.
    with sqlite3.connect(book.path) as db:
        db.execute('DROP TABLE balances')
        db.execute('DROP INDEX entries_operation')
        db.execute('ALTER TABLE entries DROP COLUMN operation_id')
        db.execute('PRAGMA user_version=1')
    reopened = Ledger(book.path, **identity)
    assert reopened.balance() == before and reopened.has_operation('15')
