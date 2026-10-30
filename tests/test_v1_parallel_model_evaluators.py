"""Private model graders obey parallel admission and verify evidence on replay.

Actors and HTTP replies below are local fixtures, never provider calls.
"""

from dataclasses import replace
import json
import threading
import time

import pytest

from proteus.core.contracts import BudgetLimits, OperationStatus, PluginRef, RuntimeIdentity, Usage
from proteus.core.methods import ActorResult
from proteus.core.native_inputs import input_directory
from proteus.core.recovery import RecoveryRequired
from proteus.core.serialization import ContractError, content_hash
from proteus.evaluation.parallel import TrialMap
from proteus.recipes.config import ActorSpec
from proteus.recipes.export import report_run
from test_native_usage import endpoint
from test_v1_evaluation import plugin, register
from test_v1_ledger import run_ledger
from test_v1_model_evaluators import response
from test_v1_parallel_evaluation import run, setup


def configured(root, *, role_cap=2, run_cap=2, parallel=True, timing="after_prediction",
               hidden=False, expected_overlap=False):
    registry, _, _, recipe = setup(root, workers=3, cap=run_cap, timing=timing, hidden=hidden)
    state = {"active": 0, "peak": 0, "calls": 0}
    mutex = threading.Lock()
    barrier = threading.Barrier(2) if expected_overlap else None

    class Judge:
        model_call_bound = 1

        def execute(self, spec, request, workspace):
            with mutex:
                state["active"] += 1
                state["calls"] += 1
                number = state["calls"]
                state["peak"] = max(state["peak"], state["active"])
            try:
                if barrier and number <= 2:
                    barrier.wait(timeout=10)
                else:
                    time.sleep(.03)
                return ActorResult(text='{"score":1}', runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
                    usage=Usage(model_calls=1, input_tokens=0, output_tokens=0, cost_usd=0))
            finally:
                with mutex:
                    state["active"] -= 1

    capabilities = ("actor.execute", "session.fresh", "calls.bounded")
    register(registry, "fixture.parallel_judge", "actor", Judge,
             capabilities=capabilities + (("actor.parallel",) if parallel else ()))
    recipe = replace(recipe, actors={"judge": ActorSpec(runtime=plugin("fixture.parallel_judge"),
        budget=BudgetLimits(max_concurrency=role_cap))}, evaluations=(replace(recipe.evaluations[0],
            evaluator=plugin("proteus.model_judge"), actor="judge"),))
    return registry, recipe, state


@pytest.mark.parametrize("run_cap,role_cap,peak", [(2, 1, 1), (1, 2, 1), (2, 2, 2)])
def test_parallel_judge_obeys_run_and_role_limits_and_resumes_without_calls(tmp_path, run_cap, role_cap, peak):
    registry, recipe, state = configured(tmp_path, run_cap=run_cap, role_cap=role_cap,
                                         expected_overlap=peak == 2)
    result, records = run(tmp_path, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert state == {"active": 0, "peak": peak, "calls": 3}
    assert [row["task_id"] for row in records["rollouts"]] == ["0", "1", "2"]
    book = run_ledger(tmp_path / "run")
    assert book.balance()["active_slots"] == 0
    graders = [entry for entry in book.entries() if entry["identity"]["metadata"]["kind"] == "grader"]
    assert len(graders) == 3
    assert all(entry["identity"]["scopes"] == ["role:judge", "run"] for entry in graders)
    assert sum(entry["settlement"]["actual"]["model_calls"] for entry in graders) == 3
    events = book.events()
    assert run(tmp_path, registry, recipe, resume=True)[0] == result
    assert state["calls"] == 3 and book.events() == events
    report_run(tmp_path / "run")


@pytest.mark.parametrize("role_cap", [1, 2])
def test_parallel_judge_without_capability_is_rejected_before_dispatch(tmp_path, role_cap):
    registry, recipe, state = configured(tmp_path, role_cap=role_cap, parallel=False)
    with pytest.raises(ContractError, match="parallel grading.*actor.parallel"):
        registry.resolve(recipe)
    assert state["calls"] == 0
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("timing,hidden", [("after_batch", False), ("never", True)])
def test_serial_finalization_accepts_nonparallel_judge(tmp_path, timing, hidden):
    registry, recipe, state = configured(tmp_path, role_cap=1, parallel=False, timing=timing, hidden=hidden)
    registry.resolve(recipe)
    result, _ = run(tmp_path, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert state == {"active": 0, "peak": 1, "calls": 3}
    assert run(tmp_path, registry, recipe, resume=True)[0] == result
    assert state["calls"] == 3
    report_run(tmp_path / "run")


@pytest.mark.parametrize("damage", [None, "input", "outcome", "missing_input", "missing_outcome",
                                    "native_usage", "missing_native"])
def test_parallel_join_recovery_verifies_private_grader_evidence_without_redispatch(tmp_path, monkeypatch, damage):
    calls = []

    def replies():
        for _ in range(3):
            calls.append(1)
            yield response('{"score":1}')

    registry, _, _, recipe = setup(tmp_path, workers=2, cap=2, timing="after_prediction")
    # Explicit local test integration; do not broaden the bundled LLM harness's
    # supported capabilities just to obtain real native receipts in this test.
    harness = registry.get(plugin("proteus.harness.llm").plugin)
    registry.register(replace(harness, ref=PluginRef(name="fixture.parallel_harness_judge", version="1"),
        capabilities=(*harness.capabilities, "actor.parallel"),
        implementation_hash=content_hash("local parallel grading receipt fixture")))
    monkeypatch.setenv("PROTEUS_PRIVATE_GRADER_KEY", "local-fixture")
    with endpoint(replies()) as url:
        recipe = replace(recipe, actors={"judge": ActorSpec(model="fixture-judge",
            runtime=plugin("fixture.parallel_harness_judge", base_url=url, key_env="PROTEUS_PRIVATE_GRADER_KEY"),
            budget=BudgetLimits(max_concurrency=2))}, evaluations=(replace(recipe.evaluations[0],
                evaluator=plugin("proteus.model_judge"), actor="judge"),))
        join = TrialMap.join

        def interrupt(self, records):
            raise RecoveryRequired("injected interruption before joining completed grades")

        monkeypatch.setattr(TrialMap, "join", interrupt)
        first, _ = run(tmp_path, registry, recipe)
        monkeypatch.setattr(TrialMap, "join", join)
    assert first.status == OperationStatus.FAILED and "injected interruption" in first.diagnostic
    assert len(calls) == 3
    assert len(list((tmp_path / "run/private/evaluation-maps").glob("*/*/result.json"))) == 3
    book = run_ledger(tmp_path / "run")
    events = book.events()
    invocations = (tmp_path / "invocations.txt").read_bytes()
    grader_root = next((tmp_path / "run").glob("evaluations/*/private/grader-actors/*"))
    if damage is not None:
        if "native" in damage:
            path = next(input_directory(grader_root / "work").glob("*.outcome.json"))
        else:
            path = grader_root / ("input.json" if "input" in damage else "outcome.json")
        original_evidence = path.read_bytes()
        if damage.startswith("missing"):
            path.unlink()
        else:
            raw = json.loads(path.read_text())
            if damage == "input":
                raw["value"]["role"] = "wrong-role"
            elif damage == "outcome":
                raw["value"]["input_hash"] = "sha256:" + "0" * 64
            else:
                raw["usage"]["reported"]["output_tokens"] = 999
            path.chmod(0o644)
            path.write_text(json.dumps(raw))
    # The local endpoint is closed: neither valid nor invalid replay may dispatch.
    for attempt in range(2):
        result, _ = run(tmp_path, registry, recipe, resume=True)
        if damage is None:
            assert result.status == OperationStatus.COMPLETED, result.diagnostic
            report_run(tmp_path / "run")
        else:
            assert result.status == OperationStatus.FAILED, result
            assert "private grading evidence must be reconciled" in result.diagnostic
            assert not list((tmp_path / "run/private/evaluation-maps").glob("*/join.json"))
        assert len(calls) == 3
        assert (tmp_path / "invocations.txt").read_bytes() == invocations
        # A successful join settles its cohort once, while rejected evidence must
        # not change any reservation/settlement or retry the original calls.
        if damage is None and attempt == 0:
            events = book.events()
        assert book.events() == events
    if damage is not None:
        # Explicitly restore the original bytes, never synthesize a new score.
        path.write_bytes(original_evidence)
        result, _ = run(tmp_path, registry, recipe, resume=True)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        assert len(calls) == 3 and (tmp_path / "invocations.txt").read_bytes() == invocations
        report_run(tmp_path / "run")
        events = book.events()
        assert run(tmp_path, registry, recipe, resume=True)[0] == result
        assert book.events() == events
