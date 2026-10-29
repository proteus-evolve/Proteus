"""Durability, concurrent cap enforcement and real service-boundary accounting."""

import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest

from proteus.core.contracts import BudgetLimits, OperationStatus, RuntimeIdentity, Usage
from proteus.core.ledger import Amounts, Ledger
from proteus.core.methods import ActorResult, RunServices, StageFailed
from proteus.core.serialization import ContractError, content_hash
from proteus.recipes.builtins import Text, builtin_registry
from proteus.recipes.config import ActorSpec, PluginSpec
from proteus.recipes.manifest import RunManifest
from test_v1_plugins import execution, paid_actor_registry, plugin, run, stage_recipe
from test_v1_evaluation import setup as evaluation_setup, run as evaluation_run


IDENTITY = dict(run_id="study", config_hash=content_hash("recipe"), lock_hash=content_hash("lock"))


def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.sqlite3", **IDENTITY)


def reserve(book, entry_id="one", *, calls=1, limits=None, scopes=None, slots=1, **kwargs):
    return book.reserve(entry_id, scopes=scopes or {"run": limits or BudgetLimits(max_model_calls=2)},
        reservation=Amounts(model_calls=calls), slots=slots,
        metadata={"operation_id": entry_id, "attempt_id": "attempt-1", "role": "optimizer"},
        request={"prompt": "research"}, **kwargs)


def run_ledger(root):
    manifest = RunManifest.from_json((root / "manifest.json").read_text())
    return Ledger(root / "private/ledger.sqlite3", run_id=manifest.run_id,
                  config_hash=manifest.recipe.config_hash, lock_hash=manifest.lock_hash)


def test_atomic_multiscope_admission_leaves_no_partial_reservation(tmp_path):
    book = ledger(tmp_path)
    with pytest.raises(ContractError, match="budget exhausted"):
        reserve(book, scopes={"run": BudgetLimits(max_model_calls=5),
                             "role:reflector": BudgetLimits(max_model_calls=0)})
    assert book.events() == [] and book.entries() == []
    # A rejected transaction does not freeze its newly introduced scope limits.
    reserve(book, scopes={"run": BudgetLimits(max_model_calls=1)})
    assert book.balance()["charged"]["model_calls"] == 1


def test_refund_and_concurrency_release_are_atomic_but_spend_is_not_reset(tmp_path):
    book = ledger(tmp_path)
    reserve(book, calls=2)
    book.start("one")
    with pytest.raises(ContractError, match="concurrency"):
        reserve(book, "two", calls=0)
    book.settle("one", actual=Amounts(model_calls=1), status="completed", result={"text": "answer"})
    reserve(book, "two")
    book.start("two")
    book.settle("two", actual=Amounts(model_calls=1), status="completed")
    with pytest.raises(ContractError, match="budget exhausted"):
        reserve(ledger(tmp_path), "three")
    assert book.balance()["active_slots"] == 0
    assert book.balance()["charged"]["model_calls"] == 2


def test_unknown_usage_retains_bound_and_unknown_cost_is_not_zero(tmp_path):
    book = ledger(tmp_path)
    reserve(book)
    book.start("one")
    book.settle("one", actual=Amounts(), status="failed", result={"error": "transport lost"})
    reserve(book, "two")
    book.start("two")
    book.settle("two", actual=Amounts(model_calls=1, cost_usd=0.25), status="completed")
    balance = ledger(tmp_path).balance()
    assert balance["charged"]["model_calls"] == 2
    assert balance["unknown"]["model_calls"] == 1
    assert balance["charged"]["cost_usd"] is None
    assert book.entry("one")["settlement"]["actual"]["model_calls"] is None


def test_unknown_unbounded_reservation_stays_unknown(tmp_path):
    book = ledger(tmp_path)
    reserve(book, calls=None, limits=BudgetLimits())
    assert book.balance()["charged"]["model_calls"] is None
    book.start("one")
    book.settle("one", actual=Amounts(), status="failed")
    assert book.balance()["charged"]["model_calls"] is None


def test_decimal_money_caps_and_unknown_reservation_rejection(tmp_path):
    book = ledger(tmp_path)
    kwargs = dict(scopes={"run": BudgetLimits(max_cost_usd=0.3)}, metadata={}, request={})
    for key, cost in (("a", 0.1), ("b", 0.2)):
        book.reserve(key, reservation=Amounts(cost_usd=cost), **kwargs)
        book.start(key)
        book.settle(key, actual=Amounts(cost_usd=cost), status="completed")
    assert book.balance()["charged"]["cost_usd"] == 0.3
    with pytest.raises(ContractError, match="budget exhausted"):
        book.reserve("c", reservation=Amounts(cost_usd=0.001), **kwargs)
    with pytest.raises(ContractError, match="finite cost_usd"):
        book.reserve("d", reservation=Amounts(), **kwargs)


def test_overrun_records_real_charge_and_violation_before_next_admission(tmp_path):
    book = ledger(tmp_path)
    reserve(book)
    book.start("one")
    violations = book.settle("one", actual=Amounts(model_calls=3), status="failed",
                             result={"usage": Usage(model_calls=3).to_dict()})
    assert violations == ("model_calls",)
    assert ledger(tmp_path).balance()["charged"]["model_calls"] == 3
    assert book.events()[-1]["payload"]["violations"] == ["model_calls"]
    with pytest.raises(ContractError, match="budget exhausted"):
        reserve(book, "two")


@pytest.mark.parametrize("field,value", [("run_id", "another"),
    ("config_hash", content_hash("changed")), ("lock_hash", None)])
def test_reopen_cannot_change_experiment_identity(tmp_path, field, value):
    book = ledger(tmp_path)
    reserve(book)
    with pytest.raises(ContractError, match="identity differs"):
        Ledger(book.path, **{**IDENTITY, field: value})
    assert ledger(tmp_path).balance()["charged"]["model_calls"] == 1


def test_schema_version_and_unrecognized_database_are_not_reset(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=42")
    with pytest.raises(ContractError, match="unsupported ledger schema"):
        ledger(tmp_path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 42
        db.execute("PRAGMA user_version=0")
        db.execute("CREATE TABLE unknown(data TEXT)")
    with pytest.raises(ContractError, match="unrecognized ledger"):
        ledger(tmp_path)


def test_identity_idempotence_never_grants_permission_to_repeat_dispatch(tmp_path):
    book = ledger(tmp_path)
    reserve(book)
    assert reserve(book) == book.entry("one")
    assert len(book.events()) == 1
    with pytest.raises(ContractError, match="already submitted"):
        reserve(book, reuse=False)
    with pytest.raises(ContractError, match="operation identity changed"):
        reserve(book, calls=0)
    with pytest.raises(ContractError, match="budget scope changed"):
        reserve(book, limits=BudgetLimits(max_model_calls=10))
    book.start("one")
    with pytest.raises(ContractError, match="already started"):
        book.start("one")
    with pytest.raises(ContractError, match="cannot be cancelled"):
        book.settle("one", actual=Amounts.zero(), status="cancelled")
    book.settle("one", actual=Amounts(model_calls=1), status="completed", result={"answer": 4})
    book.settle("one", actual=Amounts(model_calls=1), status="completed", result={"answer": 4})
    assert len(book.events()) == 3
    assert len(book.events(after=2)) == 1
    with pytest.raises(ContractError, match="settlement changed"):
        book.settle("one", actual=Amounts(model_calls=0), status="completed", result={"answer": 4})


def test_predispatch_cancellation_releases_reservation(tmp_path):
    book = ledger(tmp_path)
    reserve(book)
    book.settle("one", actual=Amounts.zero(), status="cancelled")
    assert book.balance()["charged"]["model_calls"] == 0
    reserve(book, "two", calls=2)


@pytest.mark.parametrize("dimension", ["calls", "slots"])
def test_separate_processes_cannot_oversubscribe_shared_limits(tmp_path, dimension):
    book = ledger(tmp_path)
    script = """
import sys
from proteus.core.ledger import Ledger, Amounts
from proteus.core.contracts import BudgetLimits
from proteus.core.serialization import content_hash, ContractError
book = Ledger(sys.argv[1], run_id='study', config_hash=content_hash('recipe'), lock_hash=content_hash('lock'))
try:
    book.reserve(sys.argv[2], scopes={'run': BudgetLimits(max_model_calls=2 if sys.argv[3]=='calls' else None,
        max_concurrency=20 if sys.argv[3]=='calls' else 2)}, reservation=Amounts(model_calls=1),
        metadata={'worker_id': sys.argv[2]}, request={})
    book.start(sys.argv[2])
    print('admitted')
except ContractError:
    print('rejected')
"""
    processes = [subprocess.Popen([sys.executable, "-c", script, str(book.path), str(i), dimension],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(6)]
    outputs = []
    try:
        for process in processes:
            out, err = process.communicate(timeout=30)
            assert process.returncode == 0, err
            outputs.append(out.strip())
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert outputs.count("admitted") == 2 and outputs.count("rejected") == 4
    assert book.balance()["active_slots"] == 2
    assert book.balance()["charged"]["model_calls"] == 2
    assert len(book.events()) == 4


def test_process_crash_rolls_back_uncommitted_cap_change(tmp_path):
    book = ledger(tmp_path)
    reserve(book)
    script = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute('BEGIN IMMEDIATE')
db.execute("UPDATE scopes SET limits_json='corrupt'")
os._exit(23)
"""
    result = subprocess.run([sys.executable, "-c", script, str(book.path)], timeout=20)
    assert result.returncode == 23
    assert reserve(ledger(tmp_path)) == book.entry("one")
    assert book.balance()["charged"]["model_calls"] == 1


def test_retry_reservations_survive_reopen_and_keep_attempt_identity(tmp_path):
    registry, recipe = paid_actor_registry()
    root = tmp_path / "run"
    result = run(recipe, registry, root)
    assert result.status == OperationStatus.COMPLETED
    book = run_ledger(root)
    entries = book.entries()
    assert len(entries) == 2
    assert {e["identity"]["metadata"]["attempt_id"] for e in entries} == {"attempt-1", "attempt-2"}
    assert book.balance()["charged"]["model_calls"] == 2
    assert book.balance()["unknown"]["model_calls"] == 1
    assert sorted(e["state"] for e in entries) == ["completed", "failed"]
    assert execution(root)["ledger"]["coverage"] == ["managed_actor_calls", "evaluation_cohort_reservations",
                                                   "subject_calls", "grader_calls", "backend_verifiers", "source_builds",
                                                   "candidate_code_calls"]


def test_reopened_services_do_not_refill_budget_or_replay_an_actor(tmp_path):
    registry, recipe = paid_actor_registry()
    actor = replace(recipe.actors["optimizer"], runtime=replace(recipe.actors["optimizer"].runtime,
                    options={"fail_first": False}))
    recipe = registry.resolve(replace(recipe, actors={"optimizer": actor}, budget=BudgetLimits(max_model_calls=2)))
    def services():
        return RunServices(run_id="study", recipe=recipe, registry=registry, root=tmp_path)
    first = services()
    ref = first.values.publish("proteus.text@1", Text(text="hello"))
    first.stage("propose", {"prompt": ref}, operation_id="first")
    second = services()
    ref = second.values.publish("proteus.text@1", Text(text="hello"))
    with pytest.raises(ContractError, match="already submitted"):
        second.stage("propose", {"prompt": ref}, operation_id="first")
    assert second.actor_calls == []
    second.stage("propose", {"prompt": ref}, operation_id="second")
    with pytest.raises(StageFailed, match="budget exhausted"):
        second.stage("propose", {"prompt": ref}, operation_id="third")
    assert len(second.actor_calls) == 1
    assert second.ledger.balance()["charged"]["model_calls"] == 2


@pytest.mark.parametrize("behavior", ["overrun", "wrong_runtime", "exit"])
def test_invalid_runtime_results_and_system_exit_do_not_erase_spend(tmp_path, behavior):
    registry = builtin_registry()
    class Actor:
        model_call_bound = 1
        def execute(self, spec, request, workspace):
            if behavior == "exit":
                raise SystemExit("interrupted transport")
            identity = RuntimeIdentity(runtime=spec.runtime.plugin if behavior == "overrun"
                else stage_recipe().actors["optimizer"].runtime.plugin)
            return ActorResult(text="answer", runtime=identity, usage=Usage(model_calls=3))
    definition = plugin("fixture.usage", "actor", lambda options: Actor(),
                         capabilities=("actor.execute", "session.fresh", "calls.bounded"))
    registry.register(definition)
    recipe = replace(stage_recipe(), actors={"optimizer": ActorSpec(runtime=PluginSpec(plugin=definition.ref))},
                     budget=BudgetLimits(max_model_calls=1))
    root = tmp_path / "run"
    result = run(recipe, registry, root)
    assert result.status == OperationStatus.FAILED
    book = run_ledger(root)
    entry, = book.entries()
    if behavior == "exit":
        assert entry["state"] == "started" and entry["settlement"] is None
        assert book.balance()["charged"]["model_calls"] == 1
        assert book.balance()["active_slots"] == 1
    else:
        assert entry["state"] == "failed"
        assert entry["settlement"]["actual"]["model_calls"] == 3
        assert book.balance()["charged"]["model_calls"] == 3
        assert entry["settlement"]["violations"] == ["model_calls"]
        assert execution(root)["results"][0]["usage"]["model_calls"] == 3


def test_real_actor_crash_leaves_durable_request_and_uncertain_dispatch(tmp_path):
    root = tmp_path / "run"
    script = """
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from dataclasses import replace
from test_v1_plugins import plugin, stage_recipe, run
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import ActorSpec, PluginSpec
from proteus.core.contracts import BudgetLimits
class Actor:
    model_call_bound = 1
    def execute(self, spec, request, workspace):
        (workspace / 'external-effect').write_text('dispatched')
        os._exit(19)
registry = builtin_registry()
definition = plugin('fixture.crash', 'actor', lambda options: Actor(),
    capabilities=('actor.execute', 'session.fresh', 'calls.bounded'))
registry.register(definition)
recipe = replace(stage_recipe(), actors={'optimizer': ActorSpec(runtime=PluginSpec(plugin=definition.ref))},
    budget=BudgetLimits(max_model_calls=1))
run(recipe, registry, Path(sys.argv[1]))
"""
    result = subprocess.run([sys.executable, "-c", script, str(root)], timeout=30, capture_output=True, text=True)
    assert result.returncode == 19, result.stderr
    assert not (root / "execution-0001.json").exists()
    assert len(list(root.glob("actors/*/external-effect"))) == 1
    book = run_ledger(root)
    entry, = book.entries()
    assert entry["state"] == "started" and entry["settlement"] is None
    assert entry["identity"]["request"]["request"]["prompt"] == "Inspect this scoped candidate."
    assert book.balance()["active_slots"] == 1
    assert book.balance()["charged"]["model_calls"] == 1
    with pytest.raises(ContractError, match="already started"):
        book.start(entry["id"])
    with pytest.raises(ContractError, match="concurrency budget exhausted"):
        reserve(book, "retry", limits=BudgetLimits(max_model_calls=1))


def test_evaluation_cohort_reservation_survives_service_recreation(tmp_path):
    registry, store, _, recipe = evaluation_setup(tmp_path)
    recipe = replace(recipe, budget=BudgetLimits(max_evaluations=2))
    result, records, root = evaluation_run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED
    book = run_ledger(root)
    entry, = [e for e in book.entries() if e["identity"]["metadata"]["kind"] == "evaluation_cohort"]
    assert entry["state"] == "completed"
    assert entry["identity"]["metadata"]["kind"] == "evaluation_cohort"
    assert book.balance()["charged"]["evaluations"] == records["reserved_evaluations"] == 2
    manifest = RunManifest.from_json((root / "manifest.json").read_text())
    services = RunServices(run_id=manifest.run_id, recipe=registry.resolve(recipe), registry=registry,
                           root=root, lock_hash=manifest.lock_hash)
    assert services._evaluation_service.reserved == 2
    with pytest.raises(ContractError, match="evaluation budget exhausted"):
        services.open_evaluation("score", operation_id="new-evaluation")
    assert services.rollouts == [] and len(book.entries()) == 5


def test_native_path_does_not_claim_zero_cost_ledger_coverage(tmp_path):
    from test_v1_plugins import native_recipe
    root = tmp_path / "run"
    result = run(native_recipe(), builtin_registry(), root)
    assert result.status == OperationStatus.COMPLETED
    assert execution(root)["ledger"] is None
    assert not (root / "private/ledger.sqlite3").exists()


def test_failed_workspace_creation_cancels_only_undispatched_call(tmp_path):
    registry = builtin_registry()
    recipe = registry.resolve(stage_recipe())
    services = RunServices(run_id="study", recipe=recipe, registry=registry, root=tmp_path)
    key = ["fixed", "attempt-1", "optimizer", 1]
    workspace = tmp_path / "actors" / ("actor-" + content_hash(key).split(":")[1])
    workspace.mkdir(parents=True)
    (workspace / "preserve").write_text("existing data")
    ref = services.values.publish("proteus.text@1", Text(text="request"))
    with pytest.raises(StageFailed, match="FileExistsError"):
        services.stage("propose", {"prompt": ref}, operation_id="fixed")
    entry, = services.ledger.entries()
    assert entry["state"] == "cancelled"
    assert entry["settlement"]["actual"] == Amounts.zero().to_dict()
    assert services.results[0].usage.model_calls == 0
    assert (workspace / "preserve").read_text() == "existing data"


def test_aborted_evaluation_retains_its_entire_admitted_cohort(tmp_path):
    from test_v1_evaluation import method
    from proteus.core.methods import MethodResult
    registry, store, _, recipe = evaluation_setup(tmp_path)
    def study(services):
        candidate = services.initial_candidate()
        session = services.open_evaluation("score")
        session.step(candidate)
        return MethodResult()  # Runner must abort the unfinished cohort.
    result, _, root = evaluation_run(tmp_path, registry, store, method(registry, recipe, study))
    assert result.status == OperationStatus.FAILED
    book = run_ledger(root)
    entry, = [e for e in book.entries() if e["identity"]["metadata"]["kind"] == "evaluation_cohort"]
    assert entry["state"] == "failed"
    assert entry["settlement"]["result"]["completed_predictions"] == 1
    assert book.balance()["charged"]["evaluations"] == 2
