"""A third-party adaptation is reused across roles without method registration."""

from dataclasses import replace
from pathlib import Path

import pytest

from proteus.adapters.minimal import MinimalHarness
from proteus.api.v1 import HarnessIntegration, harness_support_matrix, register_harness
from proteus.core.contracts import BudgetLimits, OperationStatus
from proteus.core.plugins import implementation_hash
from proteus.core.serialization import ContractError
from proteus.execution.actors import AdapterOptions, HarnessOptions
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import PluginSpec
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
from test_v1_plugins import SOURCE, native_recipe, stage_recipe


class ExtensionHarness(MinimalHarness):
    name = 'extension'
    dispatches = 0

    def run_episode(self, spec):
        type(self).dispatches += 1
        return super().run_episode(spec)


EXTENSION = SourcePin(name='extension', repository='https://example.invalid/offline-harness-fixture',
                      revision='1' * 40)


def integration(factory):
    return HarnessIntegration(name='extension', factory=factory,
        options={'actor': HarnessOptions, 'native': AdapterOptions},
        capabilities=('harness.custom_phases', 'harness.model_free'),
        implementation_hash=implementation_hash({'fixture.py': Path(__file__)}), source='extension',
        continuity='fresh', model_call_bound=0)


def test_extension_registered_once_runs_as_actor_and_native_with_separate_state(tmp_path):
    calls = []

    def factory(role, spec, options):
        adapter = ExtensionHarness()
        calls.append((role, adapter))
        return adapter

    registry = builtin_registry()
    bindings = register_harness(registry, integration(factory))
    recipe = native_recipe()
    recipe = replace(recipe, actors={'agent': replace(recipe.actors['agent'],
        runtime=PluginSpec(plugin=bindings['native']))})
    lock = registry.make_lock(recipe, [SOURCE, EXTENSION])
    native = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / 'native')
    assert native.status == OperationStatus.COMPLETED, native.diagnostic
    native_calls = ExtensionHarness.dispatches
    assert all(role == 'native' for role, _ in calls)
    assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / 'native', resume=True) == native
    assert ExtensionHarness.dispatches == native_calls  # Construction is permitted; dispatch is not.

    stage = stage_recipe()
    stage = replace(stage, budget=BudgetLimits(), actors={'optimizer': replace(stage.actors['optimizer'],
        model=None, budget=BudgetLimits(), runtime=PluginSpec(plugin=bindings['actor']))})
    stage_lock = registry.make_lock(stage, [SOURCE, EXTENSION])
    acted = run_recipe(stage, registry=registry, lock=stage_lock, root=tmp_path / 'actor')
    assert acted.status == OperationStatus.COMPLETED, acted.diagnostic
    assert calls[-1][0] == 'actor'
    assert calls[-1][1] is not calls[0][1]
    assert (tmp_path / 'native/workspace/harness/notes').is_dir()
    assert list((tmp_path / 'actor/actors').glob('*/harness/notes'))
    matrix = harness_support_matrix(registry)
    assert set(matrix['extension']) == {'actor', 'native'}
    assert matrix['extension']['actor']['plugin'] == bindings['actor'].to_dict()


def test_existing_builtin_names_use_same_registration_flow_and_explicit_roles():
    registry = builtin_registry()
    matrix = harness_support_matrix(registry)
    assert set(matrix) == {'minimal', 'llm', 'pi', 'dsh', 'codex'}
    assert set(matrix['minimal']) == {'actor', 'native'}
    assert set(matrix['pi']) == {'actor', 'native', 'workspace', 'subject', 'evolution'}
    assert matrix['pi']['workspace']['plugin']['name'] == 'proteus.workspace.pi'
    assert matrix['dsh']['native']['plugin']['name'] == 'proteus.native.dsh'


def test_dsh_inner_permission_policy_is_explicit_and_scoped_to_dsh():
    from proteus.execution.actors import HarnessActor, validate_harness
    from proteus.core.contracts import PluginRef
    from proteus.recipes.config import ActorSpec
    spec = ActorSpec(model='fixture', runtime=PluginSpec(plugin=PluginRef(name='proteus.harness.dsh', version='1')))
    assert HarnessActor('dsh', HarnessOptions()).adapter(spec).permission_mode == 'workspace-write'
    options = HarnessOptions(permission_mode='danger-full-access')
    assert HarnessActor('dsh', options).adapter(spec).permission_mode == 'danger-full-access'
    for name in ('pi', 'codex', 'llm'):
        with pytest.raises(ContractError, match='permission_mode is a DSH'):
            validate_harness(name, spec, options)


def test_parallel_capability_is_explicit_and_does_not_include_continuous_roles(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from proteus.core.methods import ActorRequest
    barrier = Barrier(2)
    instances = []

    class ConcurrentHarness(ExtensionHarness):
        def run_episode(self, spec):
            (spec.root / 'harness/owner').write_text(spec.phase_prompts['act'])
            barrier.wait(timeout=10)
            assert (spec.root / 'harness/owner').read_text() == spec.phase_prompts['act']
            return super().run_episode(spec)

    def factory(*args):
        instance = ConcurrentHarness()
        instances.append(instance)
        return instance

    registry = builtin_registry()
    definition = integration(factory)
    bindings = register_harness(registry, replace(definition,
        capabilities=(*definition.capabilities, 'harness.parallel')))
    assert 'actor.parallel' in registry.get(bindings['actor']).capabilities
    assert 'actor.parallel' not in registry.get(bindings['native']).capabilities
    spec = replace(stage_recipe().actors['optimizer'], model=None, budget=BudgetLimits(),
                   runtime=PluginSpec(plugin=bindings['actor']))
    runtime = registry.create(spec.runtime, 'actor')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(runtime.execute, spec, ActorRequest(prompt=str(i)), tmp_path / str(i)) for i in range(2)]
        assert all(f.result().status == OperationStatus.COMPLETED for f in futures)
    assert len(instances) == 2 and instances[0] is not instances[1]
    for name in ('pi', 'dsh', 'codex'):
        matrix = harness_support_matrix(registry)[name]
        assert 'actor.parallel' in matrix['actor']['capabilities']
        assert 'actor.parallel' in matrix['workspace']['capabilities']
        assert 'actor.parallel' not in matrix['evolution']['capabilities']


def test_unsupported_workspace_role_is_rejected_without_constructing_adapter():
    def never(*args):
        pytest.fail('unsupported role must not construct an adapter')

    definition = integration(never)
    with pytest.raises(ContractError, match='does not support harness role workspace'):
        definition.check('workspace', stage_recipe().actors['optimizer'], HarnessOptions())
    with pytest.raises(ContractError, match='harness.workspace capability'):
        replace(definition, options={**definition.options, 'workspace': HarnessOptions})


def test_registry_snapshot_and_lock_retain_external_integration_identity():
    registry = builtin_registry()
    refs = register_harness(registry, integration(lambda *args: ExtensionHarness()))
    recipe = native_recipe()
    recipe = replace(recipe, actors={'agent': replace(recipe.actors['agent'],
        runtime=PluginSpec(plugin=refs['native']))})
    lock = registry.make_lock(recipe, [SOURCE, EXTENSION])
    registry.snapshot().check_lock(registry.resolve(recipe), lock)
    wrong = builtin_registry()
    register_harness(wrong, replace(integration(lambda *args: ExtensionHarness()),
        implementation_hash='sha256:' + '2' * 64))
    with pytest.raises(ContractError, match='differ from lock'):
        wrong.check_lock(wrong.resolve(recipe), lock)


@pytest.mark.parametrize('role', ('workspace', 'evolution'))
def test_workspace_protocol_cannot_be_bound_to_a_plain_prompt_stage(role):
    from proteus.examples.harness_roles import register
    registry = builtin_registry()
    refs = register(registry)
    recipe = stage_recipe()
    recipe = replace(recipe, budget=BudgetLimits(), actors={'optimizer': replace(recipe.actors['optimizer'],
        model=None, budget=BudgetLimits(), runtime=PluginSpec(plugin=refs[role]))})
    with pytest.raises(ContractError, match='requires a harness actor binding'):
        registry.resolve(recipe)
