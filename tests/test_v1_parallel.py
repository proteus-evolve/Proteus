"""Actual overlap, bounded spend, deterministic joins and hard-exit branch recovery."""

import json
import os
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import threading
import time

import pytest

from proteus.core.contracts import BudgetLimits, OperationStatus, PluginRef, RuntimeIdentity, Usage
from proteus.core.methods import ActorResult, MethodResult
from proteus.core.parallel import StageJob, StageMap, StageMapResult
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.recovery import _load, recovery_status
from proteus.core.serialization import Contract, content_hash
from proteus.recipes.builtins import Text, builtin_registry
from proteus.recipes.config import ActorSpec, PluginSpec
from proteus.recipes.runner import run_recipe
from test_v1_plugins import SOURCE, execution, stage_recipe
from test_v1_ledger import run_ledger


@dataclass(frozen=True, kw_only=True)
class Options(Contract):
    prompts: tuple[str, ...] = ("0", "1", "2", "3")
    workers: int = 2
    policy: str = "collect"
    alternate_roles: bool = False
    reduce: bool = False


class Method:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        options = self.options
        jobs = tuple(
            StageJob(
                key=str(i),
                stage="second" if options.alternate_roles and i % 2 else "propose",
                inputs={"prompt": services.values.publish("proteus.text@1", Text(text=prompt))},
            )
            for i, prompt in enumerate(options.prompts)
        )
        result = services.map_stages(
            jobs, operation_id="batch", max_workers=options.workers, failure_policy=options.policy
        )
        outputs = {"map": services.values.publish("proteus.stage_map@1", result)}
        if options.reduce and result.succeeded:
            text = ",".join(
                services.values.get(row.outputs["response"]).text for row in result.results
            )
            result = services.stage(
                "propose", {"prompt": services.values.publish("proteus.text@1", Text(text=text))}
            )
            outputs["reduced"] = result.outputs["response"]
        return MethodResult(outputs=outputs)


class Actor:
    model_call_bound = 1

    def execute(self, spec, request, workspace):
        # Distinct filesystem effects make no-repeat claims observable across
        # interpreter crashes, not just in-memory fixture counters.
        with (workspace.parent.parent / "external-calls").open("a") as f:
            f.write(request.prompt + "\n")
        if request.prompt == "uncertain":
            os._exit(87)
        if request.prompt == "exit":
            raise SystemExit("fixture transport terminated")
        if request.prompt == "fail":
            return ActorResult(
                text="failed",
                status=OperationStatus.FAILED,
                runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
                usage=Usage(model_calls=1),
            )
        time.sleep(0.06 if request.prompt == "0" else 0.01)
        return ActorResult(
            text=request.prompt,
            runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
            usage=Usage(model_calls=1, cost_usd=0, input_tokens=2, output_tokens=1),
        )


def setup(*, options=None, limit=2, role_limit=2, model_calls=20, retries=1):
    registry = builtin_registry()
    registry.register(
        Plugin(
            ref=PluginRef(name="fixture.map", version="1"),
            kind="method",
            options=Options,
            factory=Method,
            source="proteus",
            implementation_hash=content_hash("parallel-method"),
            capabilities=("run.journal", "run.resume"),
            state_schema=EmptyOptions,
        )
    )
    registry.register(
        Plugin(
            ref=PluginRef(name="fixture.parallel_actor", version="1"),
            kind="actor",
            options=EmptyOptions,
            factory=lambda _: Actor(),
            source="proteus",
            implementation_hash=content_hash("parallel-actor"),
            capabilities=("actor.execute", "actor.parallel", "calls.bounded", "session.fresh"),
        )
    )
    actor = ActorSpec(
        runtime=PluginSpec(plugin=PluginRef(name="fixture.parallel_actor", version="1")),
        budget=BudgetLimits(max_concurrency=role_limit),
    )
    base = stage_recipe()
    stage = replace(
        base.stages[0], failure_policy="retry" if retries > 1 else "record", max_attempts=retries
    )
    recipe = replace(
        base,
        method=PluginSpec(
            plugin=PluginRef(name="fixture.map", version="1"),
            options=(options or Options()).to_dict(),
        ),
        actors={"optimizer": actor, "other": actor},
        stages=(stage, replace(stage, id="second", role="other")),
        budget=BudgetLimits(max_concurrency=limit, max_model_calls=model_calls),
    )
    return registry, recipe


def run(root, registry, recipe, resume=False):
    return run_recipe(
        recipe,
        registry=registry,
        lock=registry.make_lock(recipe, [SOURCE]),
        root=root,
        resume=resume,
    )


def mapped(root, attempt=1):
    records = execution(root, attempt)
    value = next(
        row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.stage_map@1"
    )
    return StageMapResult.from_dict(value), records


def overlap(events, *, role=None):
    active, maximum = set(), 0
    for event in events:
        if event["kind"] == "reserved":
            if role is None or event["payload"]["metadata"]["role"] == role:
                active.add(event["entry"])
                maximum = max(maximum, len(active))
        elif event["kind"] == "settled":
            active.discard(event["entry"])
    return maximum


@pytest.mark.parametrize(
    "limit,role_limit,alternate,expected",
    [(2, 2, False, 2), (2, 1, False, 1), (2, 1, True, 2), (1, 4, True, 1)],
)
def test_role_and_run_caps_ordered_join_and_reduction(
    tmp_path, limit, role_limit, alternate, expected
):
    registry, recipe = setup(
        options=Options(alternate_roles=alternate, reduce=True), limit=limit, role_limit=role_limit
    )
    root = tmp_path / "run"
    result = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    joined, records = mapped(root)
    assert joined.succeeded and joined.keys == ("0", "1", "2", "3")
    assert joined.wall_seconds > 0 and not joined.resumed
    assert [row["request"]["prompt"] for row in records["actor_calls"]] == [
        "0",
        "1",
        "2",
        "3",
        "0,1,2,3",
    ]
    ledger = run_ledger(root)
    assert overlap(ledger.events()) == expected
    for role in ("optimizer", "other"):
        assert overlap(ledger.events(), role=role) <= role_limit
    assert ledger.balance()["charged"]["model_calls"] == 5
    assert ledger.balance()["active_slots"] == 0
    before = ledger.events()
    assert run(root, registry, recipe, resume=True) == result
    assert ledger.events() == before
    assert len((root / "external-calls").read_text().splitlines()) == 5
    assert execution(root, 2)["actor_calls"] == records["actor_calls"]


@pytest.mark.parametrize("policy", ["collect", "fail_fast"])
def test_failure_policy_drains_started_branches_without_fake_cancellation(tmp_path, policy):
    registry, recipe = setup(options=Options(prompts=("fail", "0", "2", "3"), policy=policy))
    root = tmp_path / "run"
    result = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    joined, records = mapped(root)
    assert not joined.succeeded and joined.results[0].status == OperationStatus.FAILED
    expected = 4 if policy == "collect" else 2
    assert len(records["actor_calls"]) == expected
    assert joined.cancelled == (() if policy == "collect" else ("2", "3"))
    assert joined.results[1].status == OperationStatus.COMPLETED
    assert run_ledger(root).balance()["charged"]["model_calls"] == expected
    assert run_ledger(root).balance()["active_slots"] == 0


def test_concurrent_shared_budget_never_overspends_and_retry_cost_is_retained(tmp_path):
    registry, recipe = setup(
        options=Options(prompts=("fail", "0", "2", "3")), model_calls=3, retries=2
    )
    root = tmp_path / "run"
    result = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    joined, records = mapped(root)
    assert not joined.succeeded
    assert run_ledger(root).balance()["charged"]["model_calls"] == 3
    assert (
        len(records["actor_calls"]) == len((root / "external-calls").read_text().splitlines()) == 3
    )
    assert any("budget exhausted" in row["diagnostic"] for row in records["results"])
    assert run_ledger(root).balance()["active_slots"] == 0


def test_http_actor_requests_really_overlap_and_report_known_tokens(tmp_path, monkeypatch):
    active, peak, calls = 0, 0, []
    guard = threading.Lock()
    rendezvous = threading.Barrier(2)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal active, peak
            prompt = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["messages"][
                -1
            ]["content"]
            with guard:
                active += 1
                peak = max(peak, active)
                calls.append(prompt)
            rendezvous.wait(timeout=5)
            with guard:
                active -= 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "model": "offline-http-fixture",
                        "choices": [{"message": {"content": prompt}}],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                    }
                ).encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PARALLEL_FIXTURE_KEY", "fixture-secret")
    registry, recipe = setup()
    actor = ActorSpec(
        model="fixture",
        budget=BudgetLimits(max_concurrency=2),
        runtime=PluginSpec(
            plugin=PluginRef(name="proteus.chat", version="1"),
            options={
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "key_env": "PARALLEL_FIXTURE_KEY",
                "timeout_seconds": 10,
            },
        ),
    )
    recipe = replace(recipe, actors={"optimizer": actor, "other": actor})
    root = tmp_path / "run"
    try:
        result = run(root, registry, recipe)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    joined, records = mapped(root)
    assert joined.succeeded and peak == 2 and sorted(calls) == ["0", "1", "2", "3"]
    assert sum(row["usage"]["input_tokens"] for row in records["actor_calls"]) == 28
    assert run_ledger(root).balance()["charged"]["model_calls"] == 4
    assert "fixture-secret" not in json.dumps(records)


def test_settled_system_exit_preserves_unknown_spend_without_claiming_uncertain_replay(tmp_path):
    registry, recipe = setup(options=Options(prompts=("exit", "0")))
    root = tmp_path / "run"
    result = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    joined, records = mapped(root)
    assert joined.results[0].status == OperationStatus.FAILED
    assert "SystemExit" in joined.results[0].diagnostic
    assert joined.results[0].usage.model_calls is None
    book = run_ledger(root)
    assert book.balance()["unknown"]["model_calls"] == 1
    assert book.balance()["charged"]["model_calls"] == 2 and book.balance()["active_slots"] == 0
    before = book.events()
    assert run(root, registry, recipe, resume=True) == result
    assert book.events() == before


WORKER = r"""
import os,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_parallel import setup,run,Options,Actor
from proteus.core.ledger import Ledger
from proteus.core.recovery import Journal
import proteus.core.parallel as parallel
root,boundary,resume=Path(sys.argv[1]),sys.argv[2],sys.argv[3]=='resume'
registry,recipe=setup(options=Options(prompts=('0','uncertain','2') if boundary=='uncertain' else ('0','1','2')), limit=1)
if boundary in ('parallel.settled','parallel.uncertain'):
    registry,recipe=setup(options=Options(prompts=('slow','fast'),reduce=True),limit=2)
    execute=Actor.execute
    def executing(self,spec,request,workspace):
        result=execute(self,spec,request,workspace)
        if request.prompt=='slow' and not resume:
            import time
            deadline=time.monotonic()+10
            while not list((root/'private/maps').glob('*/*/result.json')):
                if time.monotonic()>deadline: raise RuntimeError('sibling did not commit')
                time.sleep(.005)
            if boundary=='parallel.uncertain': os._exit(87)
        return result
    Actor.execute=executing
if not resume:
    settle=Ledger.settle
    def settled(self,*args,**kwargs):
        result=settle(self,*args,**kwargs)
        if boundary=='actor.settled': os._exit(86)
        if boundary=='parallel.settled' and kwargs['result']['request']['prompt']=='slow': os._exit(86)
        return result
    Ledger.settle=settled
    write=parallel._write
    def written(path,value):
        result=write(path,value)
        if boundary=='branch' and path.name=='result.json': os._exit(86)
        if boundary=='join' and path.name=='join.json': os._exit(86)
        if boundary=='request' and path.name=='request.json': os._exit(86)
        return result
    parallel._write=written
    call=Journal.call
    def called(self,kind,*args,**kwargs):
        result=call(self,kind,*args,**kwargs)
        if kind==boundary: os._exit(86)
        return result
    Journal.call=called
print(run(root,registry,recipe,resume).to_json())
"""


def invoke(root, boundary, resume=False):
    return subprocess.run(
        [sys.executable, "-c", WORKER, str(root), boundary, "resume" if resume else "run"],
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "boundary", ["request", "actor.settled", "branch", "join", "stage.map", "method.result"]
)
def test_hard_exit_preserves_completed_branches_and_reuses_actor_receipts(tmp_path, boundary):
    root = tmp_path / "run"
    first = invoke(root, boundary)
    assert first.returncode == 86, first.stderr
    second = invoke(root, boundary, True)
    assert second.returncode == 0, second.stderr
    result = MethodResult.from_json(second.stdout)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert (root / "external-calls").read_text().splitlines() == ["0", "1", "2"]
    joined, records = mapped(root)
    assert joined.succeeded
    assert joined.resumed == (boundary in ("request", "actor.settled", "branch"))
    assert (joined.wall_seconds is None) == joined.resumed
    ledger = run_ledger(root)
    assert ledger.balance()["charged"]["model_calls"] == 3 and ledger.balance()["active_slots"] == 0
    before = ledger.events()
    third = invoke(root, boundary, True)
    assert third.returncode == 0 and MethodResult.from_json(third.stdout) == result, third.stderr
    assert ledger.events() == before


def test_uncertain_parallel_branch_blocks_new_work_and_preserves_completed_sibling(tmp_path):
    root = tmp_path / "run"
    first = invoke(root, "uncertain")
    assert first.returncode == 87, first.stderr
    ledger = run_ledger(root)
    before = ledger.events()
    for _ in range(2):
        resumed = invoke(root, "uncertain", True)
        assert resumed.returncode == 0, resumed.stderr
        result = MethodResult.from_json(resumed.stdout)
        assert result.status == OperationStatus.FAILED and "RecoveryRequired" in result.diagnostic
        assert ledger.events() == before
    assert (root / "external-calls").read_text().splitlines() == ["0", "uncertain"]
    assert ledger.balance()["active_slots"] == 1
    status = recovery_status(root)
    assert status["pending"]["kind"] == "stage.map" and status["unsettled"]
    assert [row["state"] for row in status["pending"]["branches"]] == [
        "completed",
        "submitted",
        "not_submitted",
    ]
    receipts = list((root / "private/maps").glob("*/*/result.json"))
    assert len(receipts) == 1 and _load(receipts[0])["result"]["status"] == "completed"


@pytest.mark.parametrize("boundary", ["parallel.settled", "parallel.uncertain"])
def test_hard_exit_with_two_simultaneously_running_branches(tmp_path, boundary):
    root = tmp_path / "run"
    first = invoke(root, boundary)
    assert first.returncode == (86 if boundary == "parallel.settled" else 87), first.stderr
    book = run_ledger(root)
    assert overlap(book.events()) == 2
    receipts = list((root / "private/maps").glob("*/*/result.json"))
    assert len(receipts) == 1
    before = book.events()
    resumed = invoke(root, boundary, True)
    assert resumed.returncode == 0, resumed.stderr
    result = MethodResult.from_json(resumed.stdout)
    if boundary == "parallel.settled":
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        assert sorted((root / "external-calls").read_text().splitlines()) == [
            "fast",
            "slow",
            "slow,fast",
        ]
        assert book.balance()["charged"]["model_calls"] == 3 and book.balance()["active_slots"] == 0
    else:
        assert result.status == OperationStatus.FAILED and "RecoveryRequired" in result.diagnostic
        assert sorted((root / "external-calls").read_text().splitlines()) == ["fast", "slow"]
        assert book.events() == before and book.balance()["active_slots"] == 1


@pytest.mark.parametrize("change", ["capability", "ports", "duplicate", "workers"])
def test_map_preflight_rejects_invalid_jobs_before_actor_calls(tmp_path, change, monkeypatch):
    registry, recipe = setup()
    original = StageMap.run

    def altered(self, jobs, **kwargs):
        if change == "ports":
            jobs = (replace(jobs[0], inputs={}), *jobs[1:])
        if change == "duplicate":
            jobs = (jobs[0], jobs[0])
        if change == "workers":
            kwargs["max_workers"] = 0
        return original(self, jobs, **kwargs)

    monkeypatch.setattr(StageMap, "run", altered)
    if change == "capability":
        key = ("fixture.parallel_actor", "1")
        registry._plugins[key] = replace(
            registry._plugins[key], capabilities=("actor.execute", "calls.bounded", "session.fresh")
        )
    root = tmp_path / "run"
    result = run(root, registry, recipe)
    assert result.status == OperationStatus.FAILED
    assert not (root / "external-calls").exists()
    assert execution(root)["actor_calls"] == []
