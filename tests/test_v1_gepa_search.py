"""Compare against installed, source-verified GEPA; no mock upstream algorithm."""

import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from proteus.core.contracts import DataRef, PluginRef
from proteus.core.search import Objective, ScoreVector, SelectionView
from proteus.core.serialization import ContractError, content_hash
from proteus.evaluation.tasks import StateValue, Task, TaskBatch
from proteus.methods.gepa.reference import SOURCE_HASH, load_reference, verify_source
from proteus.methods.gepa.sampling import EpochSampler, SamplerState
from proteus.methods.gepa.search import InstanceFrontier, register
from test_v1_evaluation import plugin
from test_v1_ledger import run_ledger
from test_v1_search import scored_fixture


pytestmark = pytest.mark.skipif(importlib.util.find_spec("gepa") is None and not os.environ.get("PROTEUS_REQUIRE_GEPA"),
                               reason="optional pinned GEPA; required in upstream-conformance CI")


@pytest.fixture(autouse=True, scope="module")
def reference():
    return load_reference()


def view_of(matrix):
    ids = tuple(f"program-{i}" for i in range(len(matrix)))
    return SelectionView(archive=DataRef(kind="proteus.archive@1", id="fixture", content_hash=content_hash(matrix)),
        evaluation_id="validation", candidate_ids=ids, dimensions=tuple(f"score:{i}:task-{i}" for i in range(len(matrix[0]))),
        vectors=tuple(ScoreVector(candidate_id=ids[i], batch_id=f"batch-{i}", scores=tuple(row))
                      for i, row in enumerate(matrix)))


def upstream_state(matrix):
    from gepa.core.state import GEPAState, ValsetEvaluation
    def batch(scores):
        return ValsetEvaluation(outputs_by_val_id={}, scores_by_val_id=dict(enumerate(scores)))
    state = GEPAState({"system.txt": "same content"}, batch(matrix[0]))
    for row in matrix[1:]:
        state.update_state_with_new_program([0], {"system.txt": "same content"}, batch(row), None, 0)
    return state


@pytest.mark.parametrize("programs", [1, 3, 17, 70])
def test_frontier_and_seeded_parent_sequence_equal_real_gepa_state(programs):
    from gepa.gepa_utils import remove_dominated_programs
    from gepa.strategies.candidate_selector import ParetoCandidateSelector
    policy = InstanceFrontier()
    generator = random.Random(173)
    for _ in range(12):
        # Ties, negative scores, nonlexical IDs, and >64 indices exercise set/order semantics.
        matrix = [[generator.choice((-2., -.5, 0., .2, .6, 1.)) for _ in range(9)] for _ in range(programs)]
        state = upstream_state(matrix)
        view = view_of(matrix)
        fronts, scores = policy.inputs(view)
        assert fronts == state.get_pareto_front_mapping()
        assert scores == state.per_program_tracked_scores
        pruned = remove_dominated_programs(state.get_pareto_front_mapping(), state.per_program_tracked_scores)
        kept = {index for front in pruned.values() for index in front}
        untouched = random.Random(9)
        before = untouched.getstate()
        assert policy.retain(view, untouched).selected == tuple(view.candidate_ids[i] for i in range(programs) if i in kept)
        assert untouched.getstate() == before
        for seed in (0, 1, 831):
            ours, theirs = random.Random(seed), random.Random(seed)
            selector = ParetoCandidateSelector(theirs)
            actual = [policy.select_parent(view, ours).selected[0] for _ in range(25)]
            expected = [view.candidate_ids[selector.select_candidate_idx(state)] for _ in range(25)]
            assert actual == expected and ours.getstate() == theirs.getstate()


def test_gepa_coverage_differs_from_generic_pareto_and_keeps_index_identity(tmp_path):
    services, _ = scored_fixture(tmp_path)
    register(services.registry)
    services.recipe = replace(services.recipe, search={**services.recipe.search,
        "gepa": plugin("proteus.gepa_frontier"), "gepa_parent": plugin("proteus.gepa_parent")})
    # Reverse two equal-content candidates: GEPA prunes the earlier equal witness,
    # whereas generic Pareto retains both and scalar_best picks the balanced program.
    population = ("tie", "specialist", "balanced", "dominated", "initial")
    archive = services.archive_snapshot()
    ref = services.selection_view(archive, "score", candidate_ids=population)
    assert services.retain_candidates("gepa", ref).result.selected == ("specialist", "initial")
    generic = services.retain_candidates("archive", ref)
    assert set(generic.result.selected) == {"tie", "specialist", "balanced", "initial"}
    assert services.select_final("final", ref).result.selected == ("balanced",)
    book_before = services.ledger.events()
    for _ in range(20):
        decision = services.select_parents("gepa_parent", ref)
        assert decision.result.selected[0] in ("specialist", "initial")
        assert "weight=0" in decision.result.reasons["balanced"]
        assert "redundant" in decision.result.reasons["tie"]
        assert set(decision.result.reasons) == {item.id for item in services.candidates}
    assert services.ledger.events() == book_before
    assert len(services.candidates) == 8
    assert services.candidates[0].artifact == services.values.get(archive).candidate("tie").artifact
    with pytest.raises(ContractError, match="does not support this role"):
        services.select_final("gepa", ref)
    for bad in (services.selection_view(archive, "score"),
                services.selection_view(archive, "score", candidate_ids=("initial", "failed")),
                services.selection_view(archive, "score", candidate_ids=("initial",), objectives=(Objective(metric="score"),))):
        before = services.rng.getstate()
        with pytest.raises(ContractError):
            services.select_parents("gepa_parent", bad)
        assert services.rng.getstate() == before


def pool_of(size):
    # Deliberately not sorted: upstream shuffle/padding use declared order.
    return TaskBatch(suite=PluginRef(name="fixture", version="1"), dataset_version="1",
        dataset_hash=content_hash(size), split="train", order="declared", seed=0, initial_state=StateValue(),
        tasks=tuple(Task(id=f"task-{size-i}", version="1", split="train", input=i) for i in range(size)))


@pytest.mark.parametrize("size,batch_size", [(1, 1), (2, 3), (5, 2), (7, 4)])
def test_minibatches_shared_rng_and_json_restore_match_upstream(size, batch_size):
    from gepa.strategies.batch_sampler import EpochShuffledBatchSampler
    from gepa.strategies.candidate_selector import ParetoCandidateSelector
    pool = pool_of(size)
    ours, theirs = random.Random(16), random.Random(16)
    sampler = EpochSampler(pool, batch_size, ours)
    original = EpochShuffledBatchSampler(batch_size, theirs)
    state = upstream_state([[1, 1, 0], [0, 0, 1], [.8, .8, .8]])
    view = view_of([[1, 1, 0], [0, 0, 1], [.8, .8, .8]])
    policy, parent = InstanceFrontier(), ParetoCandidateSelector(theirs)
    iterations = (0, 0, 0, 1, 2, 2, *range(3, 30))
    for index, iteration in enumerate(iterations):
        chosen = policy.select_parent(view, ours).selected[0]
        assert chosen == view.candidate_ids[parent.select_candidate_idx(state)]
        assert sampler.select(iteration).task_ids == tuple(original.next_minibatch_ids(sampler, SimpleNamespace(i=iteration)))
        assert ours.getstate() == theirs.getstate()
        saved = SamplerState.from_json(sampler.snapshot().to_json())
        # New object and wrong seed each time; continuation must come from the full state.
        ours = random.Random(999 + index)
        sampler = EpochSampler(pool, batch_size, ours)
        sampler.restore(saved)
        assert sampler.snapshot() == saved
        assert ours.getstate() == theirs.getstate()
        assert list(sampler.sampler.id_freqs.items()) == list(original.id_freqs.items())


def test_sampler_rejects_incompatible_state_before_mutation():
    sampler = EpochSampler(pool_of(2), 3, random.Random(4))
    initial = SamplerState.from_json(sampler.snapshot().to_json())
    sampler.restore(initial)
    assert len(sampler.select(0).task_ids) == 3
    saved = sampler.snapshot()
    for invalid in (replace(saved, pool_hash=content_hash("other")), replace(saved, minibatch_size=4),
                    replace(saved, shuffled_ids=("unknown",) * 3), replace(saved, last_trainset_size=4),
                    replace(saved, frequencies=()), replace(saved, epoch=-1)):
        with pytest.raises(ContractError):
            sampler.restore(invalid)
        assert sampler.snapshot() == saved
    for iteration in (-1, True, 0.5):
        with pytest.raises(ContractError):
            sampler.select(iteration)
    sampler.select(2)
    with pytest.raises(ContractError, match="backwards"):
        sampler.select(1)
    for invalid_rng in ((3, (), None), (), None):
        with pytest.raises(ContractError, match="RNG"):
            replace(saved, rng_state=invalid_rng)


def test_wrong_optional_source_is_rejected_before_import(tmp_path, monkeypatch, reference):
    root = Path(reference.__file__).parent
    assert verify_source(root) == SOURCE_HASH
    copied = tmp_path / "gepa"
    shutil.copytree(root, copied, ignore=shutil.ignore_patterns("__pycache__"))
    source = copied / "gepa_utils.py"
    source.write_bytes(source.read_bytes() + b"\n# different reference\n")
    with pytest.raises(ContractError, match="source differs"):
        verify_source(copied)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ContractError, match="runtime is missing"):
        load_reference()


DRIVER = r'''
import os,sys
from pathlib import Path
from dataclasses import dataclass,replace
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_evaluation import setup,SOURCE,plugin
from proteus.core.contracts import BudgetLimits,DataRef,PluginRef
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions,Plugin
from proteus.core.recovery import Journal
from proteus.core.serialization import Contract,content_hash
from proteus.evaluation.tasks import TaskBatch
from proteus.methods.gepa.search import register
from proteus.methods.gepa.sampling import EpochSampler,SamplerState
from proteus.recipes.runner import run_recipe
root=Path(sys.argv[1]); boundary=sys.argv[2]; mode=sys.argv[3]
root.mkdir(exist_ok=True)
code="from pathlib import Path\nwith Path("+repr(str(root/'calls'))+").open('a') as f:f.write('call\\n')\n"
code+="import json,sys\nr=json.load(sys.stdin);print(json.dumps({'output':1}))\n"
registry,store,_,recipe=setup(root,code=code)
register(registry)
@dataclass(frozen=True,kw_only=True)
class State(Contract):
    pool:TaskBatch
    view:DataRef
    sampler:SamplerState
    iteration:int
class Study:
    def run(self,services,restored_state):
        if restored_state:
            payload=State.from_dict(restored_state.state.payload)
            pool=payload.pool;view=payload.view;start=payload.iteration
            sampler=EpochSampler(pool,3,services.rng);sampler.restore(payload.sampler)
        else:
            pool=services.task_pool('score')
            first=services.initial_candidate();second=services.fork_candidate(first)
            for candidate in (first,second):services.evaluate(candidate,'score')
            ids=(second.id,first.id) if mode=='changed' else (first.id,second.id)
            view=services.selection_view(services.archive_snapshot(),'score',candidate_ids=ids)
            sampler=EpochSampler(pool,3,services.rng);start=0
        checkpoint=restored_state
        for i in range(start,4):
            chosen=services.select_parents('parent',view).result.selected[0]
            selection=sampler.select(i)
            candidate=next(item for item in services.candidates if item.id==chosen)
            services.evaluate(candidate,'score',selection=selection)
            checkpoint=services.checkpoint(State(pool=pool,view=view,sampler=sampler.snapshot(),iteration=i+1))
        return MethodResult(state=checkpoint)
registry.register(Plugin(ref=PluginRef(name='fixture.gepa_search',version='1'),kind='method',options=EmptyOptions,
    factory=lambda options:Study(),implementation_hash=content_hash('fixture.gepa_search'),source='proteus',
    capabilities=('run.resume','run.journal'),state_schema=State))
recipe=replace(recipe,method=plugin('fixture.gepa_search'),search={'parent':plugin('proteus.gepa_parent')},
    budget=BudgetLimits(max_evaluations=16))
if mode=='run':
    original=Journal.call
    def call(self,kind,*args,**kwargs):
        result=original(self,kind,*args,**kwargs)
        if kind==boundary:os._exit(86)
        return result
    Journal.call=call
lock=registry.make_lock(recipe,[SOURCE])
result=run_recipe(recipe,registry=registry,lock=lock,root=root/'run',artifact_source=store.root,resume=mode!='run')
print(result.to_json())
'''


def invoke(root, boundary, mode):
    return subprocess.run([sys.executable, "-c", DRIVER, str(root), boundary, mode],
                          capture_output=True, text=True, timeout=40)


@pytest.mark.parametrize("boundary", ["archive.view", "search.parent", "checkpoint"])
def test_process_recovery_keeps_parent_and_sampler_state_without_extra_evaluation(tmp_path, boundary):
    failed = invoke(tmp_path, boundary, "run")
    assert failed.returncode == 86, failed.stderr
    resumed = invoke(tmp_path, boundary, "resume")
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["status"] == "completed", resumed.stdout
    assert len((tmp_path / "calls").read_text().splitlines()) == 16
    records = json.loads(sorted((tmp_path / "run").glob("execution-*.json"))[-1].read_text())
    decisions = [item["value"] for item in records["values"] if item["ref"]["kind"] == "proteus.search.decision@1"]
    assert len(decisions) == 4
    assert all(item["result"]["selected"] == ["candidate-000002"] for item in decisions)
    # Same shared-RNG ordering as the real upstream parent and sampler.
    from gepa.strategies.batch_sampler import EpochShuffledBatchSampler
    from gepa.strategies.candidate_selector import ParetoCandidateSelector
    from gepa.core.data_loader import ListDataLoader
    rng = random.Random(0)
    original = EpochShuffledBatchSampler(3, rng)
    parent = ParetoCandidateSelector(rng)
    state = upstream_state([[1, 1], [1, 1]])
    expected = []
    for i in range(4):
        parent.select_candidate_idx(state)
        expected.extend(str(x) for x in original.next_minibatch_ids(ListDataLoader([0,1]),SimpleNamespace(i=i)))
    assert [item["task_id"] for item in records["rollouts"]][4:] == expected
    book = run_ledger(tmp_path / "run")
    before = book.events()
    assert book.balance()["charged"]["evaluations"] == 16 and book.balance()["active_slots"] == 0
    again=invoke(tmp_path,boundary,"resume")
    assert again.returncode == 0, again.stderr
    assert book.events() == before and len((tmp_path/"calls").read_text().splitlines()) == 16


def test_changed_population_is_not_a_continuation(tmp_path):
    assert invoke(tmp_path,"archive.view","run").returncode == 86
    before=run_ledger(tmp_path/"run").events()
    changed=invoke(tmp_path,"archive.view","changed")
    assert changed.returncode != 0 and "inputs changed" in changed.stderr
    assert run_ledger(tmp_path/"run").events() == before
    assert invoke(tmp_path,"archive.view","resume").returncode == 0
