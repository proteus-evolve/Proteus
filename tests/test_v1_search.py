"""Real service boundaries for task selection, public history and pure search policies."""

import json
import random
import subprocess
import sys
from dataclasses import replace

import pytest

from proteus.artifacts.codecs import FileDelta, FileWrite
from proteus.core.contracts import BudgetLimits, OperationStatus, Outcome, ValidationStatus
from proteus.core.methods import MethodResult, RunServices
from proteus.core.search import Objective, PolicyResult
from proteus.core.serialization import ContractError, content_hash
from proteus.evaluation.records import Grade, zero_usage
from proteus.evaluation.tasks import TaskSelection, make_batch
from test_v1_evaluation import method, plugin, register, setup
from test_v1_ledger import run_ledger


def services_fixture(tmp_path, **kwargs):
    registry, store, dataset, recipe = setup(tmp_path, **kwargs)
    recipe = method(registry, recipe, lambda services: MethodResult())
    recipe = replace(recipe, search={"parent": plugin("proteus.uniform_parent"),
        "archive": plugin("proteus.pareto_front"), "final": plugin("proteus.scalar_best")})
    recipe = registry.resolve(recipe)
    services = RunServices(run_id="run", recipe=recipe, registry=registry, root=tmp_path / "run")
    services.artifacts.copy_from(store, recipe.subject.initial_artifact)
    return services, dataset


@pytest.mark.parametrize("sequence,outputs", [(False, [1, 1, 1]), (True, [1, 2, 3])])
def test_explicit_occurrences_preserve_order_identity_state_and_budget(tmp_path, sequence, outputs):
    services, dataset = services_fixture(tmp_path, sequence=sequence, references=("PRIVATE_REFERENCE", 2, 3))
    candidate = services.initial_candidate()
    pool = services.task_pool("score")
    assert pool.dataset_hash == content_hash(dataset) and pool.selection is None
    assert "PRIVATE_REFERENCE" not in pool.to_json()
    assert not (services.root / "private/ledger.sqlite3").exists()
    selection = TaskSelection(task_ids=("2", "0", "2"))
    receipt = services.evaluate(candidate, "score", selection=selection)
    assert [row.task_id for row in services.rollouts] == ["2", "0", "2"]
    assert [row.output for row in services.rollouts] == outputs
    assert len(set(receipt.rollout_ids)) == 3
    assert services.ledger.balance()["charged"]["evaluations"] == 3
    entries = [entry for entry in services.ledger.entries() if entry['identity']['metadata']['kind'] == 'subject']
    assert len({entry['identity']['request']['seed'] for entry in entries}) == 3
    protocol = json.loads(next((services.root / "evaluations").glob("*/protocol.json")).read_text())
    assert protocol['batch']['selection']['task_ids'] == ["2", "0", "2"]


def test_selection_cannot_expand_pool_and_fails_before_budget_reservation(tmp_path):
    services, dataset = services_fixture(tmp_path, references=(1, 2, 3))
    spec = services.recipe.evaluations[0]
    services.recipe = replace(services.recipe, evaluations=(replace(spec, tasks=replace(spec.tasks, task_ids=("0", "1"))),))
    candidate = services.initial_candidate()
    for ids in (("2",), ("missing",)):
        with pytest.raises(ContractError, match="outside the declared pool"):
            services.evaluate(candidate, "score", selection=TaskSelection(task_ids=ids))
    assert not (services.root / "private/ledger.sqlite3").exists()
    with pytest.raises(ContractError, match="TaskSelection"):
        services.open_evaluation("score", selection={"task_ids": ["0"]})
    with pytest.raises(ContractError):
        TaskSelection(task_ids=())
    shuffled = replace(spec.tasks, order="shuffle", seed=14)
    selected = make_batch(dataset, shuffled, TaskSelection(task_ids=("2", "0", "2")))
    assert [task.id for task in selected.tasks] == ["2", "0", "2"]


def test_each_repeated_occurrence_is_reserved_against_the_hard_cap(tmp_path):
    services, _ = services_fixture(tmp_path)
    services.recipe = replace(services.recipe, budget=BudgetLimits(max_evaluations=2))
    candidate = services.initial_candidate()
    with pytest.raises(ContractError, match="budget exhausted"):
        services.evaluate(candidate, "score", selection=TaskSelection(task_ids=("0", "0", "0")))
    assert services.ledger.entries() == [] and services.rollouts == []
    services.evaluate(candidate, "score", selection=TaskSelection(task_ids=("0", "0")))
    assert services.ledger.balance()["charged"]["evaluations"] == 2


def vector_source(scores):
    return "import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({'output':" + repr(scores) + "[r['task']['input']]}))\n"


def scored_fixture(tmp_path):
    services, _ = services_fixture(tmp_path, code=vector_source([1, 0]))
    class Score:
        def grade(self, task, rollout, reference):
            if not isinstance(rollout.output, (int, float)):
                return Grade(status=Outcome.INFRA_ERROR, score=None, usage=zero_usage())
            return Grade(status=Outcome.SUCCEEDED, score=rollout.output,
                         metrics={"cost": 5 - rollout.output}, usage=zero_usage())
    register(services.registry, "fixture.score", "evaluator", Score, capabilities=("evaluator.deterministic",))
    spec = services.recipe.evaluations[0]
    services.recipe = replace(services.recipe, evaluations=(replace(spec, evaluator=plugin("fixture.score")),))
    initial = services.initial_candidate()
    candidates = [initial]
    for name, scores in (("specialist", [0, 1]), ("balanced", [.6, .6]), ("dominated", [.1, .1]),
                         ("tie", [1, 0]), ("failed", ["unknown", "unknown"])):
        delta = FileDelta(base_hash=initial.artifact.content_hash,
                          put=(FileWrite.from_bytes("main.py", vector_source(scores).encode()),))
        candidates.append(services.fork_candidate(initial, candidate_id=name, delta=delta))
    for candidate in candidates:
        services.evaluate(candidate, "score")
    services.fork_candidate(initial, candidate_id="invalid", validation=ValidationStatus.INVALID)
    services.fork_candidate(initial, candidate_id="unevaluated")
    return services, candidates


def test_archive_retains_branches_failures_and_local_advantages_after_selection(tmp_path):
    services, candidates = scored_fixture(tmp_path)
    ref = services.archive_snapshot()
    archive = services.values.get(ref)
    assert archive.candidate("specialist").parent_ids == ("initial",)
    assert len(archive.children("initial")) == 7
    assert archive.evidence("failed")[0].batch.mean_score() is None
    view = services.selection_view(ref, "score")
    frontier = services.retain_candidates("archive", view)
    assert frontier.result.selected == ("initial", "specialist", "balanced", "tie")
    assert frontier.result.reasons["dominated"] == "dominated"
    assert "missing" in frontier.result.reasons["failed"]
    assert frontier.result.reasons["invalid"] == "invalid candidate"
    assert "not evaluated" in frontier.result.reasons["unevaluated"]
    assert services.select_final("archive", view).result.selected == frontier.result.selected
    services.recipe = replace(services.recipe, search={**services.recipe.search, "keep": plugin("proteus.keep_all")})
    assert services.retain_candidates("keep", view).result.selected == tuple(item.id for item in candidates[:-1])
    final = services.select_final("final", view)
    assert final.result.selected == ("balanced",)
    assert services.values.get(ref) == archive and len(services.candidates) == 8
    later = services.values.get(services.archive_snapshot())
    assert len(later.decisions) == 4 and later.candidates == archive.candidates
    assert len(services.ledger.entries()) == 5 * len(candidates)


def test_explicit_objectives_support_minimization_and_keep_comparison_evidence(tmp_path):
    services, _ = scored_fixture(tmp_path)
    archive = services.archive_snapshot()
    view = services.selection_view(archive, "score", objectives=(Objective(metric="cost", maximize=False),))
    assert services.select_final("final", view).result.selected == ("balanced",)
    multiple = services.selection_view(archive, "score", objectives=(Objective(metric="score"), Objective(metric="cost", maximize=False)))
    assert services.retain_candidates("archive", multiple).result.selected == ("balanced",)
    with pytest.raises(ContractError, match="one aggregate objective"):
        services.select_final("final", multiple)
    absent = services.selection_view(archive, "score", objectives=(Objective(metric="not_reported"),))
    assert services.values.get(absent).vectors == ()
    with pytest.raises(ContractError, match="no eligible"):
        services.select_final("final", absent)


@pytest.mark.parametrize("mode", ["hidden", "ineligible", "test"])
def test_hidden_or_ineligible_evaluations_never_enter_selection(tmp_path, mode):
    services, _ = services_fixture(tmp_path, hidden=mode != "ineligible")
    spec = services.recipe.evaluations[0]
    if mode == "test":
        spec = replace(spec, purpose="test", selection_eligible=False)
    elif mode == "ineligible":
        spec = replace(spec, selection_eligible=False)
    services.recipe = replace(services.recipe, evaluations=(spec,))
    services.evaluate(services.initial_candidate(), "score")
    ref = services.archive_snapshot()
    if mode != "ineligible":
        assert services.values.get(ref).evaluations == ()
    with pytest.raises(ContractError, match="not eligible"):
        services.selection_view(ref, "score")


def test_distinct_minibatches_require_an_explicit_protocol_and_stale_views_remain_stable(tmp_path):
    services, _ = services_fixture(tmp_path)
    first = services.initial_candidate()
    services.evaluate(first, "score", selection=TaskSelection(task_ids=("0",)))
    old_archive = services.archive_snapshot()
    old_view = services.selection_view(old_archive, "score")
    child = services.fork_candidate(first)
    services.evaluate(child, "score", selection=TaskSelection(task_ids=("1",)))
    new_archive = services.archive_snapshot()
    with pytest.raises(ContractError, match="multiple evaluation cohorts"):
        services.selection_view(new_archive, "score")
    selected = services.selection_view(new_archive, "score", protocol_hash=services.evaluations[0].protocol_hash)
    assert services.select_final("final", selected).result.selected == (first.id,)
    assert services.select_final("final", old_view).result.selected == (first.id,)


def test_policy_role_outputs_and_forged_scores_are_checked(tmp_path):
    services, _ = services_fixture(tmp_path)
    services.evaluate(services.initial_candidate(), "score")
    archive = services.archive_snapshot()
    view_ref = services.selection_view(archive, "score")
    with pytest.raises(ContractError, match="does not support this role"):
        services.select_parents("archive", view_ref)
    view = services.values.get(view_ref)
    vector = replace(view.vectors[0], scores=(99., 99.))
    fake = services.values.publish("proteus.search.view@1", replace(view, vectors=(vector,)))
    with pytest.raises(ContractError, match="differs from its eligible archive"):
        services.select_final("final", fake)
    snapshot = services.values.get(archive)
    evidence = snapshot.evaluations[0]
    rows = (replace(evidence.batch.rows[0], score=99.), *evidence.batch.rows[1:])
    altered = replace(snapshot, evaluations=(replace(evidence, batch=replace(evidence.batch, rows=rows)),))
    fake_archive = services.values.publish("proteus.archive@1", altered)
    with pytest.raises(ContractError, match="committed candidate/evaluation history"):
        services.selection_view(fake_archive, "score")
    class Wrong:
        def select_parent(self, view, rng):
            return PolicyResult(selected=("other",), reasons={"initial": "not chosen"})
    register(services.registry, "fixture.wrong", "policy", Wrong, capabilities=("search.parent",))
    services.recipe = replace(services.recipe, search={**services.recipe.search, "wrong": plugin("fixture.wrong")})
    with pytest.raises(ContractError, match="outside its eligible view"):
        services.select_parents("wrong", view_ref)
    assert services.results[-1].status == OperationStatus.FAILED


def test_online_partial_candidate_cohorts_are_not_ranked_as_full_evaluations(tmp_path):
    services, _ = services_fixture(tmp_path, sequence=True)
    first = services.initial_candidate()
    second = services.fork_candidate(first)
    session = services.open_evaluation("score")
    session.step(first)
    session.step(second)
    session.finish()
    view = services.values.get(services.selection_view(services.archive_snapshot(), "score"))
    assert view.vectors == ()
    assert set(view.excluded.values()) == {"candidate has only part of the cohort"}


def test_agent_directed_multiple_parents_need_no_sampling_and_scalar_ties_use_creation_order(tmp_path):
    services, _ = services_fixture(tmp_path)
    first = services.initial_candidate(candidate_id="z-first")
    second = services.fork_candidate(first, candidate_id="a-second")
    combined = services.create_candidate(first.artifact, candidate_id="combined", parents=(first, second))
    assert not any(request.stage.name.startswith("proteus.uniform") for request in services.requests)
    archive = services.values.get(services.archive_snapshot())
    assert archive.candidate("combined").parent_ids == (first.id, second.id)
    assert combined in archive.children(first.id) and combined in archive.children(second.id)
    for candidate in (first, second):
        services.evaluate(candidate, "score")
    view = services.selection_view(services.archive_snapshot(), "score")
    assert services.select_final("final", view).result.selected == (first.id,)


RECOVERY_DRIVER = r'''
import os,sys
from pathlib import Path
from dataclasses import replace
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_evaluation import setup,SOURCE,plugin
from proteus.core.contracts import BudgetLimits,PluginRef
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions,Plugin
from proteus.core.recovery import Journal
from proteus.core.serialization import content_hash
from proteus.evaluation.tasks import TaskSelection
from proteus.recipes.runner import run_recipe
root=Path(sys.argv[1]); boundary=sys.argv[2]; resume=sys.argv[3]!='run'
root.mkdir(exist_ok=True)
source="from pathlib import Path\nwith Path("+repr(str(root/'calls'))+").open('a') as f: f.write('call\\n')\n"
source+="import json,sys\nr=json.load(sys.stdin);print(json.dumps({'output':1}))\n"
registry,store,_,recipe=setup(root,code=source)
class Study:
    def run(self,services,restored_state):
        if restored_state: return MethodResult(state=restored_state)
        first=services.initial_candidate()
        second=services.fork_candidate(first)
        pool=services.task_pool('score')
        ids=('0','1') if sys.argv[3]=='changed' else ('1','0','1')
        for candidate in (first,second): services.evaluate(candidate,'score',selection=TaskSelection(task_ids=ids))
        archive=services.archive_snapshot()
        view=services.selection_view(archive,'score')
        services.select_parents('parent',view)
        services.select_parents('parent',view)
        checkpoint=services.checkpoint(EmptyOptions(),archive=services.archive_snapshot(),sampling_cursor=6)
        return MethodResult(state=checkpoint)
registry.register(Plugin(ref=PluginRef(name='fixture.search',version='1'),kind='method',options=EmptyOptions,
    factory=lambda options:Study(),implementation_hash=content_hash('fixture.search'),source='proteus',
    capabilities=('run.resume','run.journal'),state_schema=EmptyOptions))
recipe=replace(recipe,method=plugin('fixture.search'),search={'parent':plugin('proteus.uniform_parent')},budget=BudgetLimits(max_evaluations=6))
if not resume:
    original=Journal.call
    def call(self,kind,*args,**kwargs):
        result=original(self,kind,*args,**kwargs)
        if kind==boundary: os._exit(86)
        return result
    Journal.call=call
lock=registry.make_lock(recipe,[SOURCE])
result=run_recipe(recipe,registry=registry,lock=lock,root=root/'run',artifact_source=store.root,resume=resume)
print(result.to_json())
'''


def invoke(root, boundary, mode):
    return subprocess.run([sys.executable, '-c', RECOVERY_DRIVER, str(root), boundary, mode],
                          text=True, capture_output=True, timeout=30)


@pytest.mark.parametrize("boundary", ["evaluation.pool", "evaluation.open", "evaluation.step", "archive.snapshot", "archive.view", "search.parent", "checkpoint"])
def test_process_exit_restores_occurrences_archive_and_sampling_without_more_work(tmp_path, boundary):
    assert invoke(tmp_path, boundary, 'run').returncode == 86
    resumed = invoke(tmp_path, boundary, 'resume')
    assert resumed.returncode == 0, resumed.stderr
    result = MethodResult.from_json(resumed.stdout)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert (tmp_path / 'calls').read_text().splitlines() == ['call'] * 6
    records = json.loads(sorted((tmp_path / 'run').glob('execution-*.json'))[-1].read_text())
    assert [row['task_id'] for row in records['rollouts']] == ['1', '0', '1'] * 2
    decisions = [value['value'] for value in records['values'] if value['ref']['kind'] == 'proteus.search.decision@1']
    rng = random.Random(0)
    assert [item['result']['selected'][0] for item in decisions] == [rng.choice(['initial', 'candidate-000002']) for _ in range(2)]
    assert result.state.sampling_cursor == 6 and result.state.archive is not None
    book = run_ledger(tmp_path / 'run')
    assert book.balance()['charged']['evaluations'] == 6 and book.balance()['active_slots'] == 0
    before = book.events()
    assert invoke(tmp_path, boundary, 'resume').returncode == 0
    assert book.events() == before and (tmp_path / 'calls').read_text().splitlines() == ['call'] * 6


def test_changed_selection_cannot_be_presented_as_resume(tmp_path):
    assert invoke(tmp_path, 'evaluation.open', 'run').returncode == 86
    book = run_ledger(tmp_path / 'run')
    before = book.events()
    changed = invoke(tmp_path, 'evaluation.open', 'changed')
    assert changed.returncode != 0 and 'inputs changed' in changed.stderr
    assert book.events() == before and not (tmp_path / 'calls').exists()
    repaired = invoke(tmp_path, 'evaluation.open', 'resume')
    assert repaired.returncode == 0, repaired.stderr
    assert MethodResult.from_json(repaired.stdout).status == OperationStatus.COMPLETED
    assert (tmp_path / 'calls').read_text().splitlines() == ['call'] * 6
