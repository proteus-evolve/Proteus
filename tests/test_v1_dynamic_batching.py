"""Controller curve, pinned-source conformance and unusable timing semantics."""

import __future__
import ast
from collections import Counter
from dataclasses import replace
import hashlib
import math
import os
import random
from pathlib import Path
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from proteus.core.contracts import DataRef
from proteus.core.serialization import ContractError, content_hash
from proteus.core.timing import TimingMeasurement
from proteus.operators.combee.batching import DynamicBatchOptions, initialize, observe


def ref(kind, value):
    digest = content_hash(value)
    return DataRef(kind=kind, id=content_hash([kind, digest]), content_hash=digest)


def record(options, state, seconds, *, actual_size=None):
    start = ref("proteus.timing.start@1", [len(state.observations), seconds])
    elapsed = TimingMeasurement(start=start, wall_seconds=seconds, interrupted=seconds is None)
    return observe(options, state, actual_size=actual_size or state.size,
                   measurement=ref("proteus.timing.measurement@1", elapsed), elapsed=elapsed)


@pytest.mark.parametrize("maximum,expected,exponent", [(6, 6, 0), (16, 8, 0), (16, 16, 0.5)])
def test_ordinary_iteration_delays_choose_a_bounded_plateau(maximum, expected, exponent):
    options = DynamicBatchOptions(max_batch_size=maximum, profile_batch_sizes=(1, 2, 4))
    state = initialize(options, pool_hash=content_hash("tasks"), dataset_size=17)
    for size in (1, 2, 4):
        assert state.size == size
        state = record(options, state, 10 * size ** exponent)
        state = type(state).from_json(state.to_json())
    assert state.size == expected and state.selected_size == expected
    assert [row.epoch_seconds for row in state.observations] == pytest.approx(
        [170 * size ** (exponent - 1) for size in (1, 2, 4)])


@pytest.mark.parametrize("delays", [(10, 20, 40), (10, 40, 160), (None, 1, None), (0, 0, 0)])
def test_flat_increasing_or_unusable_profiles_fall_back(delays):
    options = DynamicBatchOptions(max_batch_size=16, profile_batch_sizes=(1, 2, 4))
    state = initialize(options, pool_hash=content_hash("tasks"), dataset_size=20)
    for delay in delays:
        state = record(options, state, delay)
    assert state.selected_size == 1
    assert sum(row.epoch_seconds is not None for row in state.observations) == sum(
        delay is not None and delay > 0 for delay in delays)


def test_short_and_interrupted_batches_stay_recorded_without_false_throughput():
    options = DynamicBatchOptions(max_batch_size=8, profile_batch_sizes=(1, 2, 4))
    state = initialize(options, pool_hash=content_hash("tasks"), dataset_size=9)
    state = record(options, state, 5)
    state = record(options, state, None)
    state = record(options, state, 0.01, actual_size=1)
    assert state.selected_size == 1
    assert [row.epoch_seconds for row in state.observations] == [45, None, None]
    assert "interrupted" in state.observations[1].diagnostic
    assert "short" in state.observations[2].diagnostic


def test_online_window_caps_profile_sizes_without_extra_trials():
    options = DynamicBatchOptions(max_batch_size=100)
    state = initialize(options, pool_hash=content_hash("tasks"), dataset_size=30, bound=3)
    assert state.profile_sizes == (1, 2, 3) and state.bound == 3
    single = initialize(options, pool_hash=content_hash("one"), dataset_size=1)
    assert single.size == 1 and single.selected_size == 1 and single.observations == ()
    with pytest.raises(ContractError):
        initialize(options, pool_hash=content_hash("tasks"), dataset_size=30, bound=0)


def test_controller_rejects_options_drift_and_reused_measurements():
    options = DynamicBatchOptions(max_batch_size=4)
    state = initialize(options, pool_hash=content_hash("tasks"), dataset_size=8)
    state = record(options, state, 1)
    with pytest.raises(ContractError, match="options changed"):
        record(replace(options, max_batch_size=8), state, 2)
    duplicate = replace(state.observations[0], requested_size=2, actual_size=2)
    with pytest.raises(ContractError, match="reuse"):
        replace(state, observations=(*state.observations, duplicate))


def test_restored_profile_cannot_discard_good_measurements_or_invent_durations():
    from proteus.operators.combee.batching import validate_state, validate_measurements

    options = DynamicBatchOptions(max_batch_size=4)
    pool_hash = content_hash("tasks")
    state = record(options, initialize(options, pool_hash=pool_hash, dataset_size=8), 1)
    row = state.observations[0]
    ignored = replace(row, epoch_seconds=None, diagnostic="claimed interruption")
    with pytest.raises(ContractError, match="timing diagnostic"):
        validate_state(options, replace(state, observations=(ignored,)), pool_hash=pool_hash, dataset_size=8)
    invented = replace(row, wall_seconds=2, epoch_seconds=16)
    changed = replace(state, observations=(invented,))
    validate_state(options, changed, pool_hash=pool_hash, dataset_size=8)
    elapsed = TimingMeasurement(start=ref("proteus.timing.start@1", [0, 1]),
                                wall_seconds=1, interrupted=False)
    with pytest.raises(ContractError, match="recorded measurement"):
        validate_measurements(changed, lambda reference: elapsed)
    validate_measurements(state, lambda reference: elapsed)


def test_large_duration_overflow_is_excluded_and_survives_restore():
    from proteus.operators.combee.batching import validate_state

    options = DynamicBatchOptions(max_batch_size=4)
    pool_hash = content_hash("tasks")
    state = record(options, initialize(options, pool_hash=pool_hash, dataset_size=8), 1e308)
    assert state.observations[0].epoch_seconds is None
    assert state.observations[0].diagnostic == "epoch estimate is not finite"
    validate_state(options, state, pool_hash=pool_hash, dataset_size=8)


@pytest.mark.parametrize("exponent", [0, 0.25, 0.5, 0.9, 1, 1.5])
def test_plateau_matches_actual_pinned_dynamic_sampler(exponent):
    source = os.environ.get("PROTEUS_COMBEE_DYNAMIC_SOURCE")
    if not source:
        pytest.skip("pinned dynamic-sampler source required in CI")
    raw = Path(source).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "ece82e2558da06ae17a0aa2451cc357aceb605ca45e53a1982a66f5a744d6957"
    cls = next(node for node in ast.parse(raw).body if isinstance(node, ast.ClassDef)
               and node.name == "DynamicBatchSampler")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_plateau_size")
    namespace = {"math": math}
    exec(compile(ast.Module(body=[method], type_ignores=[]), source, "exec"), namespace)
    options = DynamicBatchOptions(max_batch_size=32, profile_batch_sizes=(1, 2, 4, 8))
    state = initialize(options, pool_hash=content_hash("tasks"), dataset_size=35)
    for size in (1, 2, 4, 8):
        state = record(options, state, 10 * size ** exponent)
    reference = SimpleNamespace(_profile_sizes=[1, 2, 4, 8],
        _epoch_times={row.requested_size: row.epoch_seconds for row in state.observations},
        max_batch_size=32, last_trainset_size=35, slope_threshold=0.016)
    assert state.selected_size == namespace["_plateau_size"](reference)


def dynamic_ace(root, mode="offline"):
    from proteus.evaluation.tasks import TaskDataset
    from proteus.examples.combee_ace import prepare
    from proteus.examples.ace_playbook import register as fixture
    from proteus.methods.ace.batch import CombeeOptions, register
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from test_v1_evaluation import plugin

    path = prepare(root, mode=mode, workers=3)
    recipe = load_recipe(path)
    dataset = TaskDataset.from_json((root / "tasks.json").read_text())
    train = next(row for row in dataset.records if row.task.split == "train")
    dataset = replace(dataset, records=(*dataset.records, *(replace(train, task=replace(train.task,
        id=f"train-{index}", input={"question": f"Solve fixture train-{index}.", "context": f"Context train-{index}."}))
        for index in range(3, 9))))
    (root / "tasks.json").write_text(dataset.to_json())
    method = replace(CombeeOptions.from_dict(recipe.method.options), online_eval_frequency=3,
                     dynamic_batch=DynamicBatchOptions(max_batch_size=8, profile_batch_sizes=(1, 2, 4)))
    recipe = replace(recipe, method=plugin("proteus.combee_ace", **method.to_dict()),
        evaluations=tuple(replace(spec, tasks=replace(spec.tasks, dataset_hash=content_hash(dataset),
            task_ids=tuple(row.task.id for row in dataset.records if row.task.split == spec.tasks.split)))
            for spec in recipe.evaluations))
    path.write_text(recipe.to_json())
    registry = builtin_registry()
    register(registry)
    fixture(registry)
    return registry, recipe


@pytest.mark.parametrize("mode", ["offline", "online"])
def test_dynamic_ace_profiles_ordinary_batches_and_preserves_task_coverage(tmp_path, mode):
    from test_v1_ace_method import run
    from test_v1_ledger import run_ledger

    root = tmp_path / "study"
    registry, recipe = dynamic_ace(root, mode)
    result, records = run(root, registry, recipe)
    assert result.status.value == "completed", result.diagnostic
    values = {row["ref"]["id"]: row["value"] for row in records["values"]}
    summary = next(row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.combee.ace_summary@1")
    batches = [values[ref["id"]] for ref in summary["batches"]]
    assert [task for batch in batches for task in batch["task_ids"]] == [f"train-{i}" for i in range(9)]
    controller = summary["batch_controller"]
    assert controller["profile_sizes"] == ([1, 2, 4] if mode == "offline" else [1, 2, 3])
    assert [len(batch["task_ids"]) for batch in batches[:3]] == controller["profile_sizes"]
    assert len(controller["observations"]) == 3 and controller["selected_size"] is not None
    assert len(summary["samples"]) == 9  # profiling uses these same learning samples
    for index, observation in enumerate(controller["observations"]):
        assert observation["measurement"] == batches[index]["timing"]
        assert observation["epoch_seconds"] == pytest.approx(observation["wall_seconds"] * 9 / observation["actual_size"])
    if mode == "online":
        assert len(summary["windows"]) == 3
        assert all(len({int(task.rsplit("-", 1)[1]) // 3 for task in batch["task_ids"]}) == 1 for batch in batches)
    ledger = run_ledger(root / "run")
    before = ledger.events()
    assert run(root, registry, recipe, resume=True)[0] == result
    assert ledger.events() == before and ledger.balance()["active_slots"] == 0


DYNAMIC_ACE_DRIVER = r'''
import json,os,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_dynamic_batching import dynamic_ace
from test_v1_ace_method import run
from proteus.recipes.builtins import builtin_registry
from proteus.recipes import load_recipe
from proteus.methods.ace.batch import register
from proteus.examples.ace_playbook import register as fixture
from proteus.core.recovery import Journal
root,boundary,mode=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
if mode=='run':
    registry,recipe=dynamic_ace(root)
    original=Journal.call
    def call(self,kind,*args,**kwargs):
        result=original(self,kind,*args,**kwargs)
        if kind==boundary:os._exit(86)
        return result
    Journal.call=call
else:
    registry=builtin_registry();register(registry);fixture(registry)
    recipe=load_recipe(root/'recipe.json')
result,records=run(root,registry,recipe,resume=mode=='resume')
summary=next(row['value'] for row in records['values'] if row['ref']['kind']=='proteus.combee.ace_summary@1')
print(json.dumps({'status':result.status.value,'controller':summary['batch_controller'],'calls':len(records['actor_calls'])}))
'''


@pytest.mark.parametrize("boundary", ["timing.begin", "timing.finish"])
def test_dynamic_ace_recovery_keeps_profile_evidence_without_extra_learning(tmp_path, boundary):
    def invoke(mode):
        return subprocess.run([sys.executable, "-c", DYNAMIC_ACE_DRIVER, str(tmp_path / "study"), boundary, mode],
                              capture_output=True, text=True, timeout=90)

    stopped = invoke("run")
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    resumed = invoke("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    result = json.loads(resumed.stdout)
    assert result["status"] == "completed"
    observations = result["controller"]["observations"]
    assert (observations[0]["epoch_seconds"] is None) == (boundary == "timing.begin")
    assert all(row["epoch_seconds"] is not None for row in observations[1:])
    again = invoke("resume")
    assert again.returncode == 0 and again.stdout == resumed.stdout
    assert sum(len(path.read_text().splitlines()) for path in (tmp_path / "study/run").rglob("fixture-invocations.txt")) == result["calls"]


@pytest.mark.parametrize("exponent", [0, 0.5, 1.5])
def test_variable_gepa_sampler_matches_actual_source_across_size_changes_and_restore(exponent):
    source = os.environ.get("PROTEUS_COMBEE_DYNAMIC_SOURCE")
    if not source:
        pytest.skip("pinned dynamic-sampler source required in CI")
    from proteus.evaluation.tasks import StateValue, TaskBatch, Task
    from proteus.core.contracts import PluginRef
    from proteus.methods.gepa.method import Pool
    from proteus.methods.gepa.dynamic_sampling import DynamicEpochSampler, DynamicSamplerState
    from gepa.strategies.batch_sampler import EpochShuffledBatchSampler
    from gepa.core.data_loader import DataId
    from gepa.core.adapter import DataInst

    raw = Path(source).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "ece82e2558da06ae17a0aa2451cc357aceb605ca45e53a1982a66f5a744d6957"
    cls = next(node for node in ast.parse(raw).body if isinstance(node, ast.ClassDef)
               and node.name == "DynamicBatchSampler")
    clock = SimpleNamespace(now=0)
    namespace = {"EpochShuffledBatchSampler": EpochShuffledBatchSampler, "random": random,
                 "math": math, "Counter": Counter, "monotonic": lambda: clock.now,
                 "DataId": DataId, "DataInst": DataInst}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), source, "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    pool = TaskBatch(suite=PluginRef(name="fixture.tasks", version="1"), dataset_version="1",
        dataset_hash=content_hash("tasks"), split="train", order="declared", seed=0, initial_state=StateValue(),
        tasks=tuple(Task(id=str(i), version="1", split="train", input={"n": i}) for i in range(17)))
    options = DynamicBatchOptions(max_batch_size=16, profile_batch_sizes=(1, 2, 4))
    sampler = DynamicEpochSampler(pool, options, random.Random(0))
    reference = namespace["DynamicBatchSampler"](max_batch_size=16, profile_batch_sizes=[1, 2, 4], rng=random.Random(0))
    loader = Pool(pool)
    for iteration in range(8):
        batch = sampler.select(iteration)
        expected = reference.next_minibatch_ids(loader, SimpleNamespace(i=iteration))
        assert list(batch.task_ids) == expected
        assert sampler.rng.getstate() == reference.rng.getstate()
        assert sampler.frequencies == reference.id_freqs
        delay = 10 * len(batch.task_ids) ** exponent
        clock.now += delay
        elapsed = TimingMeasurement(start=ref("proteus.timing.start@1", iteration),
                                    wall_seconds=delay, interrupted=False)
        sampler.finish(ref("proteus.timing.measurement@1", elapsed), elapsed)
        saved = DynamicSamplerState.from_json(sampler.snapshot().to_json())
        sampler = DynamicEpochSampler(pool, options, random.Random(99))
        sampler.restore(saved)
    assert sampler.controller.selected_size is not None


def dynamic_gepa(root):
    from proteus.examples.combee_gepa import prepare, register
    from proteus.methods.combee_gepa import CombeeGepaOptions
    from proteus.methods.gepa.method import evaluation_bound
    from proteus.core.contracts import BudgetLimits
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from test_v1_evaluation import plugin

    path = prepare(root)
    recipe = load_recipe(path)
    options = replace(CombeeGepaOptions.from_dict(recipe.method.options), max_metric_calls=35,
        skip_perfect_score=False, dynamic_batch=DynamicBatchOptions(max_batch_size=8, profile_batch_sizes=(1, 2, 4)))
    recipe = replace(recipe, method=plugin("proteus.combee_gepa", **options.to_dict()),
        budget=BudgetLimits(max_evaluations=evaluation_bound(options, 2) + 2, max_model_calls=0, max_concurrency=3),
        execution=replace(recipe.execution, max_workers=3),
        reference=replace(recipe.reference, deviations=("Dynamic Combee fixture; no model or benchmark result.",)))
    path.write_text(recipe.to_json())
    registry = builtin_registry()
    register(registry)
    return registry, recipe


def test_complete_gepa_loop_uses_dynamic_batches_without_unaccounted_profiling(tmp_path):
    from test_v1_ace_method import run
    from test_v1_ledger import run_ledger

    root = tmp_path / "study"
    registry, recipe = dynamic_gepa(root)
    result, records = run(root, registry, recipe)
    assert result.status.value == "completed", result.diagnostic
    summary = next(row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.gepa.summary@1")
    controller = summary["batch_controller"]
    assert [row["actual_size"] for row in controller["observations"]] == [1, 2, 4]
    assert controller["selected_size"] is not None
    assert len(records["rollouts"]) == summary["metric_calls"] + 2
    timings = [row for row in records["values"] if row["ref"]["kind"] == "proteus.timing.measurement@1"]
    assert len(timings) == summary["iterations"]
    ledger = run_ledger(root / "run")
    assert ledger.balance()["charged"]["evaluations"] == len(records["rollouts"])
    before = ledger.events()
    assert run(root, registry, recipe, resume=True)[0] == result
    assert ledger.events() == before


DYNAMIC_GEPA_DRIVER = DYNAMIC_ACE_DRIVER.replace("dynamic_ace", "dynamic_gepa").replace(
    "from proteus.methods.ace.batch import register\nfrom proteus.examples.ace_playbook import register as fixture",
    "from proteus.examples.combee_gepa import register\nfixture=lambda registry:None"
).replace("proteus.combee.ace_summary@1", "proteus.gepa.summary@1")


@pytest.mark.parametrize("boundary", ["timing.begin", "timing.finish"])
def test_dynamic_gepa_recovery_preserves_sampling_and_charged_work(tmp_path, boundary):
    from test_v1_ledger import run_ledger

    def invoke(mode):
        return subprocess.run([sys.executable, "-c", DYNAMIC_GEPA_DRIVER, str(tmp_path / "study"), boundary, mode],
                              capture_output=True, text=True, timeout=120)

    stopped = invoke("run")
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    resumed = invoke("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    result = json.loads(resumed.stdout)
    assert result["status"] == "completed"
    observations = result["controller"]["observations"]
    assert (observations[0]["epoch_seconds"] is None) == (boundary == "timing.begin")
    assert all(row["epoch_seconds"] is not None for row in observations[1:])
    ledger = run_ledger(tmp_path / "study/run")
    before = ledger.events()
    again = invoke("resume")
    assert again.returncode == 0 and again.stdout == resumed.stdout
    assert ledger.events() == before and ledger.balance()["active_slots"] == 0
    assert sum(len(path.read_text().splitlines()) for path in (tmp_path / "study/run").rglob("fixture-invocations.txt")) == result["calls"]
