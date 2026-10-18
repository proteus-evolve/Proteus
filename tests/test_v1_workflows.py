"""Workflow declarations must be validated and included in the experiment lock."""

from dataclasses import dataclass, replace
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proteus.core.contracts import PluginRef
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.serialization import ContractError, content_hash
from proteus.recipes.config import PluginSpec, WorkflowSpec
from test_v1_parallel import setup
from test_v1_plugins import SOURCE

from proteus.core.contracts import OperationStatus
from proteus.core.contracts import Candidate, BudgetLimits
from proteus.core.methods import MethodResult
from proteus.core.workflows import WorkflowJob
from proteus.recipes.builtins import Text
from proteus.recipes.runner import run_recipe
from test_v1_ledger import run_ledger
from proteus.core.serialization import Contract
from proteus.artifacts.codecs import FileContent, FileDelta, FileWrite
from proteus.evaluation.tasks import TaskSelection
from proteus.recipes.config import ActorSpec, StageSpec
from test_v1_evaluation import setup as evaluation_setup, plugin as spec_plugin
from test_v1_parallel import Actor
from proteus.core.contracts import Outcome, RuntimeIdentity
from proteus.evaluation.records import SubjectResult


def configured():
    registry, recipe = setup()
    definition = Plugin(
        ref=PluginRef(name="fixture.sample_workflow", version="1"),
        kind="workflow",
        options=EmptyOptions,
        factory=lambda options: None,
        implementation_hash=content_hash("sample-program"),
        source="proteus",
        capabilities=("workflow.parallel", "workflow.replay"),
        inputs={"request": "proteus.text@1"},
        outputs={"answer": "proteus.text@1"},
    )
    registry.register(definition)
    recipe = replace(
        recipe,
        workflows=(
            WorkflowSpec(
                id="sample",
                operator=PluginSpec(plugin=definition.ref),
                stages=("propose",),
            ),
        ),
    )
    return registry, recipe


def test_workflow_ports_options_and_implementation_are_locked():
    registry, recipe = configured()
    resolved = registry.resolve(recipe)
    assert dict(resolved.workflows[0].input_types) == {"request": "proteus.text@1"}
    assert dict(resolved.workflows[0].output_types) == {"answer": "proteus.text@1"}
    assert registry.resolve(resolved) == resolved
    assert type(recipe).from_json(resolved.to_json()) == resolved
    lock = registry.make_lock(resolved, [SOURCE])
    ref = resolved.workflows[0].operator.plugin
    assert ref in [entry.plugin for entry in lock.plugins]
    registry._plugins[(ref.name, ref.version)] = replace(
        registry.get(ref), implementation_hash=content_hash("different-sample-program")
    )
    with pytest.raises(ContractError, match="differ from lock"):
        registry.check_lock(resolved, lock)


@pytest.mark.parametrize("missing", ["workflow.parallel", "workflow.replay"])
def test_workflow_rejects_undeclared_execution_capabilities(missing):
    registry, recipe = configured()
    ref = recipe.workflows[0].operator.plugin
    definition = registry.get(ref)
    registry._plugins[(ref.name, ref.version)] = replace(
        definition, capabilities=tuple(c for c in definition.capabilities if c != missing)
    )
    with pytest.raises(ContractError, match="replay and independent"):
        registry.resolve(recipe)


@pytest.mark.parametrize(
    "component,missing",
    [
        ("stage", "stage.parallel"),
        ("stage", "stage.replay"),
        ("actor", "actor.parallel"),
    ],
)
def test_workflow_cannot_inherit_parallel_safety_from_its_own_declaration(component, missing):
    registry, recipe = configured()
    ref = (
        recipe.stages[0].operator.plugin
        if component == "stage"
        else recipe.actors["optimizer"].runtime.plugin
    )
    definition = registry.get(ref)
    registry._plugins[(ref.name, ref.version)] = replace(
        definition, capabilities=tuple(c for c in definition.capabilities if c != missing)
    )
    with pytest.raises(ContractError, match="independent"):
        registry.resolve(recipe)


@pytest.mark.parametrize("field", ["stages", "evaluations"])
def test_workflow_cannot_name_an_undeclared_service(field):
    _, recipe = configured()
    with pytest.raises(ContractError, match="workflow references an unknown"):
        replace(recipe, workflows=(replace(recipe.workflows[0], **{field: ("not-declared",)}),))


class SampleProgram:
    def run(self, services, inputs):
        prompt = services.values.publish("proteus.text@1", inputs["request"])
        first = services.stage("propose", {"prompt": prompt}, operation_id="first")
        prompt = services.values.publish(
            "proteus.text@1",
            Text(text=services.values.get(first.outputs["response"]).text + ":again"),
        )
        second = services.stage("propose", {"prompt": prompt}, operation_id="second")
        return {"answer": Text(text=services.values.get(second.outputs["response"]).text)}


class SampleMethod:
    def run(self, services, restored_state):
        jobs = tuple(
            WorkflowJob(
                key=str(i),
                workflow="sample",
                inputs={"request": services.values.publish("proteus.text@1", Text(text=str(i)))},
            )
            for i in range(3)
        )
        result = services.map_workflows(jobs, operation_id="samples", max_workers=2)
        return MethodResult(
            outputs={"map": services.values.publish("proteus.workflow_map@1", result)}
        )


def runnable():
    registry, recipe = configured()
    ref = recipe.workflows[0].operator.plugin
    registry._plugins[(ref.name, ref.version)] = replace(
        registry.get(ref), factory=lambda options: SampleProgram()
    )
    ref = recipe.method.plugin
    registry._plugins[(ref.name, ref.version)] = replace(
        registry.get(ref), options=EmptyOptions, factory=lambda options: SampleMethod()
    )
    return registry, replace(recipe, method=replace(recipe.method, options={}))


def execute(root, *, resume=False):
    registry, recipe = runnable()
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root, resume=resume)
    records = json.loads(sorted(root.glob("execution-*.json"))[-1].read_text())
    return result, records


def test_multistep_programs_join_without_call_identity_collisions(tmp_path):
    root = tmp_path / "run"
    result, records = execute(root)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    mapped = next(
        row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.workflow_map@1"
    )
    assert mapped["keys"] == ["0", "1", "2"]
    assert all(row["status"] == "completed" for row in mapped["results"]), mapped
    texts = {row["ref"]["id"]: row["value"] for row in records["values"]}
    assert [texts[row["outputs"]["answer"]["id"]]["text"] for row in mapped["results"]] == [
        "0:again",
        "1:again",
        "2:again",
    ]
    assert len(records["actor_calls"]) == len({row["id"] for row in records["actor_calls"]}) == 6
    ledger = run_ledger(root)
    assert ledger.balance()["charged"]["model_calls"] == 6 and ledger.balance()["active_slots"] == 0
    before = ledger.events()
    assert execute(root, resume=True)[0] == result
    assert ledger.events() == before
    assert len((root / "external-calls").read_text().splitlines()) == 6


WORKER = r"""
import os,sys,json
from pathlib import Path
from dataclasses import replace
sys.path.insert(0,str(Path.cwd()/'tests'))
import test_v1_workflows as fixture
from proteus.core.recovery import Journal
from proteus.core.ledger import Ledger
import proteus.core.workflows as flows
root,boundary,mode=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
original=fixture.runnable
def runnable():
    registry,recipe=original()
    return registry,replace(recipe,budget=replace(recipe.budget,max_concurrency=1))
fixture.runnable=runnable
if mode=='run':
    write=flows._write
    def changed(path,value):
        write(path,value)
        if path.name==boundary:os._exit(86)
    flows._write=changed
    call=Journal.call
    def changed_call(self,kind,*args,**kwargs):
        result=call(self,kind,*args,**kwargs)
        if kind==boundary:os._exit(86)
        return result
    Journal.call=changed_call
    settle=Ledger.settle
    def settled(self,entry_id,**kwargs):
        result=settle(self,entry_id,**kwargs)
        if boundary=='actor.settled' and self.entry(entry_id)['identity']['metadata']['kind']=='actor':os._exit(86)
        return result
    Ledger.settle=settled
    if boundary=='uncertain':
        def uncertain(self,services,inputs):
            services.stage('propose',{'prompt':services.values.publish('proteus.text@1',fixture.Text(text='uncertain'))})
        fixture.SampleProgram.run=uncertain
result,records=fixture.execute(root,resume=mode=='resume')
print(json.dumps({'status':result.status.value,'diagnostic':result.diagnostic,'calls':len(records['actor_calls'])}))
"""


@pytest.mark.parametrize(
    "boundary",
    [
        "plan.json",
        "request.json",
        "actor.settled",
        "stage",
        "method.result",
        "result.json",
        "join.json",
        "workflow.map",
    ],
)
def test_workflow_hard_exit_reuses_each_committed_call(tmp_path, boundary):
    root = tmp_path / "run"

    def invoke(mode):
        return subprocess.run(
            [sys.executable, "-c", WORKER, str(root), boundary, mode],
            capture_output=True,
            text=True,
            timeout=30,
        )

    stopped = invoke("run")
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    resumed = invoke("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert json.loads(resumed.stdout) == {"status": "completed", "diagnostic": "", "calls": 6}
    assert len((root / "external-calls").read_text().splitlines()) == 6
    before = run_ledger(root).events()
    again = invoke("resume")
    assert again.returncode == 0 and again.stdout == resumed.stdout
    assert run_ledger(root).events() == before


def test_uncertain_program_stops_other_branches_before_dispatch(tmp_path):
    from proteus.core.recovery import recovery_status

    root = tmp_path / "run"

    def invoke(mode):
        return subprocess.run(
            [sys.executable, "-c", WORKER, str(root), "uncertain", mode],
            capture_output=True,
            text=True,
            timeout=30,
        )

    stopped = invoke("run")
    assert stopped.returncode == 87, stopped.stdout + stopped.stderr
    for _ in range(2):
        resumed = invoke("resume")
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        result = json.loads(resumed.stdout)
        assert result["status"] == "failed" and "unsettled actor" in result["diagnostic"]
    assert (root / "external-calls").read_text().splitlines() == ["uncertain"]
    report = recovery_status(root)
    assert report["pending"]["kind"] == "workflow.map"
    assert [row["state"] for row in report["pending"]["branches"]] == [
        "submitted",
        "not_submitted",
        "not_submitted",
    ]
    assert run_ledger(root).balance()["active_slots"] == 1


FAIL_FAST_WORKER = WORKER.replace("max_concurrency=1", "max_concurrency=2").replace(
    "original=fixture.runnable",
    r"""
from proteus.core.workflows import WorkflowJob
from proteus.core.methods import MethodResult
from proteus.recipes.builtins import Text
import threading
original_program=fixture.SampleProgram.run
def program(self,services,inputs):
    if inputs['request'].text=='0':
        services.stage('propose',{'prompt':services.values.publish('proteus.text@1',Text(text='fail'))})
        raise ValueError('known failed sample')
    if mode=='run':threading.Event().wait(20)
    return original_program(self,services,inputs)
def method(self,services,restored_state):
    jobs=tuple(WorkflowJob(key=str(i),workflow='sample',inputs={
        'request':services.values.publish('proteus.text@1',Text(text=str(i)))}) for i in range(4))
    mapped=services.map_workflows(jobs,operation_id='samples',max_workers=2,failure_policy='fail_fast')
    return MethodResult(outputs={'map':services.values.publish('proteus.workflow_map@1',mapped)})
fixture.SampleProgram.run=program
fixture.SampleMethod.run=method
original=fixture.runnable
""",
)


def test_fail_fast_resume_drains_previously_submitted_program(tmp_path):
    root = tmp_path / "run"

    def invoke(mode):
        return subprocess.run(
            [sys.executable, "-c", FAIL_FAST_WORKER, str(root), "result.json", mode],
            text=True,
            capture_output=True,
            timeout=30,
        )

    stopped = invoke("run")
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    resumed = invoke("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert json.loads(resumed.stdout) == {"status": "completed", "diagnostic": "", "calls": 3}
    records = json.loads((root / "execution-0001.json").read_text())
    mapped = next(
        row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.workflow_map@1"
    )
    assert mapped["keys"] == ["0", "1"] and mapped["cancelled"] == ["2", "3"]
    assert [row["status"] for row in mapped["results"]] == ["failed", "completed"]
    assert mapped["resumed"] and mapped["wall_seconds"] is None
    assert (root / "external-calls").read_text().splitlines() == ["fail", "1", "1:again"]
    assert run_ledger(root).balance()["active_slots"] == 0


@dataclass(frozen=True, kw_only=True)
class SampleInput(Contract):
    candidate: Candidate
    task_id: str


class AdaptiveProgram:
    def run(self, services, inputs):
        item = inputs["sample"]
        candidate = item.candidate
        for _ in range(4):
            session = services.open_evaluation(
                "score", selection=TaskSelection(task_ids=(item.task_id,))
            )
            observation = session.step(candidate)
            session.finish()
            if observation.row.score == 1:
                return {"candidate": candidate}
            prompt = services.values.publish(
                "proteus.text@1", Text(text=str(observation.rollout.output + 1))
            )
            proposal = services.stage("reflect", {"prompt": prompt})
            text = services.values.get(proposal.outputs["response"]).text
            candidate = services.fork_candidate(
                candidate,
                delta=FileDelta(
                    base_hash=candidate.artifact.content_hash,
                    put=(FileWrite.from_bytes("value.txt", text.encode()),),
                ),
            )
        raise ValueError("sample failed to learn")


class AdaptiveMethod:
    def run(self, services, restored_state):
        initial = services.initial_candidate()
        jobs = tuple(
            WorkflowJob(
                key=str(i),
                workflow="sample",
                inputs={
                    "sample": services.values.publish(
                        "fixture.sample_input@1", SampleInput(candidate=initial, task_id=str(i))
                    )
                },
            )
            for i in range(3)
        )
        mapped = services.map_workflows(jobs, operation_id="adaptive", max_workers=3)
        return MethodResult(
            outputs={"map": services.values.publish("proteus.workflow_map@1", mapped)}
        )


def adaptive(root, *, role_cap=1):
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "predictions.txt"
    code = (
        "import json,sys\nfrom pathlib import Path\nr=json.load(sys.stdin)\n"
        f'with Path({str(marker)!r}).open("a") as stream: stream.write(r["task"]["id"]+"\\n")\n'
        'print(json.dumps({"output":int(Path("value.txt").read_text())}))\n'
    )
    registry, store, _, recipe = evaluation_setup(
        root, code=code, timing="after_prediction", references=(1, 2, 3)
    )
    artifact = store.put(
        PluginRef(name="proteus.file_tree", version="1"),
        {"main.py": FileContent(code.encode()), "value.txt": FileContent(b"0")},
    )
    registry.register_data("fixture.sample_input@1", SampleInput)
    for name, kind, factory, ports in (
        ("fixture.adaptive", "method", lambda options: AdaptiveMethod(), {}),
        (
            "fixture.sample",
            "workflow",
            lambda options: AdaptiveProgram(),
            {
                "inputs": {"sample": "fixture.sample_input@1"},
                "outputs": {"candidate": "proteus.candidate@1"},
            },
        ),
        ("fixture.reflector", "actor", lambda options: Actor(), {}),
    ):
        caps = {
            "method": ("run.journal", "run.resume"),
            "workflow": ("workflow.parallel", "workflow.replay"),
            "actor": ("actor.execute", "actor.parallel", "calls.bounded", "session.fresh"),
        }[kind]
        registry.register(
            Plugin(
                ref=PluginRef(name=name, version="1"),
                kind=kind,
                options=EmptyOptions,
                factory=factory,
                implementation_hash=content_hash(name),
                source="proteus",
                capabilities=caps,
                state_schema=EmptyOptions if kind == "method" else None,
                **ports,
            )
        )
    recipe = replace(
        recipe,
        method=spec_plugin("fixture.adaptive"),
        subject=replace(recipe.subject, initial_artifact=artifact),
        actors={
            "reflector": ActorSpec(
                runtime=spec_plugin("fixture.reflector"),
                budget=BudgetLimits(max_concurrency=role_cap),
            )
        },
        stages=(
            StageSpec(
                id="reflect",
                operator=spec_plugin("proteus.prompt"),
                role="reflector",
                read_scopes=("proteus.text@1",),
                write_scopes=("proteus.actor_result@1",),
            ),
        ),
        workflows=(
            WorkflowSpec(
                id="sample",
                operator=spec_plugin("fixture.sample"),
                stages=("reflect",),
                evaluations=("score",),
            ),
        ),
        budget=BudgetLimits(max_evaluations=9, max_concurrency=3),
    )
    return registry, store, recipe


class ManagedSubject:
    def run(self, context):
        response = context.actor((context.lease.local_root / "value.txt").read_text())
        return SubjectResult(
            loaded_artifact_hash=context.candidate.artifact.content_hash,
            runtime=RuntimeIdentity(
                runtime=context.actor_runtime,
                environment_hash=content_hash(context.execution.environment),
            ),
            status=Outcome.SUCCEEDED,
            output=int(response.text),
            state=context.state,
            usage=response.usage,
        )


def managed_adaptive(root):
    registry, store, recipe = adaptive(root)
    registry.register(
        Plugin(
            ref=PluginRef(name="fixture.managed", version="1"),
            kind="subject",
            options=EmptyOptions,
            factory=lambda options: ManagedSubject(),
            implementation_hash=content_hash("managed"),
            source="proteus",
            capabilities=("subject.parallel", "subject.managed_actor"),
        )
    )
    recipe = replace(
        recipe,
        subject=replace(recipe.subject, runner=spec_plugin("fixture.managed"), actor="generator"),
        actors={
            **recipe.actors,
            "generator": ActorSpec(
                runtime=spec_plugin("fixture.reflector"),
                budget=BudgetLimits(max_concurrency=2, max_model_calls=9),
            ),
        },
        budget=replace(recipe.budget, max_model_calls=15),
    )
    return registry, store, recipe


def test_adaptive_workflows_share_generator_and_reflector_budgets(tmp_path):
    registry, store, recipe = managed_adaptive(tmp_path / "study")
    root = tmp_path / "run"
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root, artifact_source=store.root)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    records = json.loads((root / "execution-0001.json").read_text())
    mapped = next(
        row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.workflow_map@1"
    )
    assert all(row["status"] == "completed" for row in mapped["results"]), mapped
    assert [row["output"] for row in records["rollouts"]] == [0, 1, 0, 1, 2, 0, 1, 2, 3]
    assert [row["role"] for row in records["actor_calls"]].count("generator") == 9
    assert [row["role"] for row in records["actor_calls"]].count("reflector") == 6
    ledger = run_ledger(root)
    assert ledger.balance()["charged"]["model_calls"] == 15
    assert ledger.balance()["charged"]["evaluations"] == 9
    assert ledger.balance()["active_slots"] == 0
    for entry in ledger.entries():
        metadata = entry["identity"]["metadata"]
        assert metadata["worker_id"].startswith("workflow-")
        assert metadata["operation_id"].startswith(metadata["worker_id"] + ":")
    # Verify every reservation, not merely the final zero balance.
    active = {}
    peaks = {}
    entries = {row["id"]: row for row in ledger.entries()}
    for event in ledger.events():
        entry = entries[event["entry"]]
        delta = entry["identity"]["slots"] * (
            1 if event["kind"] == "reserved" else -1 if event["kind"] == "settled" else 0
        )
        for scope in entry["identity"]["scopes"]:
            active[scope] = active.get(scope, 0) + delta
            peaks[scope] = max(peaks.get(scope, 0), active[scope])
    assert peaks["run"] <= 3 and peaks["role:generator"] <= 2 and peaks["role:reflector"] <= 1
    before = ledger.events()
    assert run_recipe(recipe, registry=registry, lock=lock, root=root, resume=True) == result
    assert ledger.events() == before
    assert len((root / "external-calls").read_text().splitlines()) == 15


@pytest.mark.parametrize("policy", ["collect", "fail_fast"])
def test_workflow_failure_drains_started_programs(tmp_path, policy, monkeypatch):
    from proteus.core.workflows import WorkflowMap

    registry, recipe = runnable()
    rendezvous = threading.Barrier(2)
    failed = threading.Event()
    read_result = WorkflowMap.read_result

    def read(self, path, job, *args):
        record = read_result(self, path, job, *args)
        if job.key == "0":
            failed.set()
        return record

    monkeypatch.setattr(WorkflowMap, "read_result", read)

    class Program:
        def run(self, services, inputs):
            value = inputs["request"].text
            if value in ("0", "1"):
                rendezvous.wait(timeout=5)
            if value == "0":
                services.stage(
                    "propose",
                    {"prompt": services.values.publish("proteus.text@1", Text(text="fail"))},
                )
                raise ValueError("sample rejected the failed stage")
            if value == "1":
                assert failed.wait(timeout=5)
            return SampleProgram().run(services, inputs)

    class Study:
        def run(self, services, restored_state):
            jobs = tuple(
                WorkflowJob(
                    key=str(i),
                    workflow="sample",
                    inputs={
                        "request": services.values.publish("proteus.text@1", Text(text=str(i)))
                    },
                )
                for i in range(4)
            )
            mapped = services.map_workflows(
                jobs, operation_id="failure", max_workers=2, failure_policy=policy
            )
            return MethodResult(
                outputs={"map": services.values.publish("proteus.workflow_map@1", mapped)}
            )

    for ref, factory in (
        (recipe.method.plugin, Study),
        (recipe.workflows[0].operator.plugin, Program),
    ):
        registry._plugins[(ref.name, ref.version)] = replace(
            registry.get(ref), factory=lambda options, factory=factory: factory()
        )
    root = tmp_path / "run"
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    records = json.loads((root / "execution-0001.json").read_text())
    mapped = next(
        row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.workflow_map@1"
    )
    assert mapped["results"][0]["status"] == "failed"
    assert mapped["results"][1]["status"] == "completed"
    assert mapped["cancelled"] == (["2", "3"] if policy == "fail_fast" else [])
    assert len(records["actor_calls"]) == (3 if policy == "fail_fast" else 7)
    ledger = run_ledger(root)
    before = ledger.events()
    assert ledger.balance()["active_slots"] == 0
    assert run_recipe(recipe, registry=registry, lock=lock, root=root, resume=True) == result
    assert ledger.events() == before


@pytest.mark.parametrize("role_cap", [1, 2])
def test_adaptive_programs_keep_candidate_updates_and_formal_scores_local(tmp_path, role_cap):
    registry, store, recipe = adaptive(tmp_path / "study", role_cap=role_cap)
    root = tmp_path / "run"
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root, artifact_source=store.root)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    records = json.loads((root / "execution-0001.json").read_text())
    mapped = next(
        row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.workflow_map@1"
    )
    assert all(row["status"] == "completed" for row in mapped["results"]), mapped
    assert [row["output"] for row in records["rollouts"]] == [0, 1, 0, 1, 2, 0, 1, 2, 3]
    assert [row["task_id"] for row in records["rollouts"]] == ["0"] * 2 + ["1"] * 3 + ["2"] * 4
    assert (
        len(records["candidates"]) == 7 and len({row["id"] for row in records["candidates"]}) == 7
    )
    assert (
        len(records["actor_calls"]) == 6 and len({row["id"] for row in records["actor_calls"]}) == 6
    )
    book = run_ledger(root)
    assert book.balance()["charged"]["evaluations"] == 9 and book.balance()["active_slots"] == 0
    assert (
        sum(
            entry["settlement"]["actual"]["model_calls"]
            for entry in book.entries()
            if entry["identity"]["metadata"]["kind"] == "actor"
        )
        == 6
    )
    before = book.events()
    assert run_recipe(recipe, registry=registry, lock=lock, root=root, resume=True) == result
    assert book.events() == before
    assert len((tmp_path / "study/predictions.txt").read_text().splitlines()) == 9
    assert len((root / "external-calls").read_text().splitlines()) == 6


@pytest.mark.parametrize("role_cap", [1, 2])
def test_http_multistep_workflows_wait_for_role_capacity_without_overdispatch(
    tmp_path, monkeypatch, role_cap
):
    registry, recipe = runnable()
    barrier = threading.Barrier(2) if role_cap == 2 else None
    lock, requests = threading.Lock(), []
    active = peak = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal active, peak
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                requests.append(body)
                number = len(requests)
                active += 1
                peak = max(peak, active)
            if barrier and number <= 2:
                barrier.wait(timeout=10)
            payload = json.dumps(
                {
                    "model": "fixture",
                    "choices": [
                        {
                            "message": {"content": body["messages"][-1]["content"]},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            with lock:
                active -= 1
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("WORKFLOW_HTTP_KEY", "fixture-only")
    recipe = replace(
        recipe,
        actors={
            **recipe.actors,
            "optimizer": ActorSpec(
                runtime=spec_plugin(
                    "proteus.chat",
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    key_env="WORKFLOW_HTTP_KEY",
                ),
                model="fixture",
                budget=BudgetLimits(max_concurrency=role_cap, max_model_calls=6),
            ),
        },
        budget=BudgetLimits(max_model_calls=6, max_concurrency=2),
    )
    try:
        root = tmp_path / "run"
        dependency_lock = registry.make_lock(recipe, [SOURCE])
        result = run_recipe(recipe, registry=registry, lock=dependency_lock, root=root)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        records = json.loads((root / "execution-0001.json").read_text())
        mapped = next(
            row["value"]
            for row in records["values"]
            if row["ref"]["kind"] == "proteus.workflow_map@1"
        )
        assert all(row["status"] == "completed" for row in mapped["results"]), mapped
        assert len(requests) == 6 and peak == role_cap
        book = run_ledger(root)
        assert book.balance()["charged"]["model_calls"] == 6 and book.balance()["active_slots"] == 0
        assert sum(row["usage"]["input_tokens"] for row in records["actor_calls"]) == 12
        assert book.balance()["unknown"]["cost_usd"] == 6
        assert (
            run_recipe(recipe, registry=registry, lock=dependency_lock, root=root, resume=True)
            == result
        )
        assert len(requests) == 6
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


ADAPTIVE_WORKER = r"""
import os,sys,json
from pathlib import Path
from dataclasses import replace
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_workflows import adaptive,SOURCE
from proteus.core.recovery import Journal
from proteus.core.ledger import Ledger
from proteus.evaluation.service import EvaluationSession
from proteus.recipes.runner import run_recipe
import proteus.core.workflows as flows
root,boundary,mode=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
registry,store,recipe=adaptive(root/'study')
recipe=replace(recipe,budget=replace(recipe.budget,max_concurrency=1))
lock=registry.make_lock(recipe,[SOURCE])
if mode=='run':
    reserve=Ledger.reserve
    def reserved(self,entry_id,**kwargs):
        result=reserve(self,entry_id,**kwargs)
        if boundary==kwargs['metadata']['kind']+'.reserved':os._exit(86)
        return result
    Ledger.reserve=reserved
    settle=Ledger.settle
    def settled(self,entry_id,**kwargs):
        result=settle(self,entry_id,**kwargs)
        if boundary==self.entry(entry_id)['identity']['metadata']['kind']+'.settled':os._exit(86)
        return result
    Ledger.settle=settled
    save=EvaluationSession._save
    def saved(self,name,value):
        save(self,name,value)
        if boundary=='prediction' and name.startswith('predictions/'):os._exit(86)
    EvaluationSession._save=saved
    call=Journal.call
    def called(self,kind,*args,**kwargs):
        result=call(self,kind,*args,**kwargs)
        # Candidate boundary should exercise a branch edit, not the initial artifact.
        if kind==boundary and '/workflows/' in str(self.root):os._exit(86)
        return result
    Journal.call=called
    write=flows._write
    def written(path,value):
        write(path,value)
        if path.name==boundary:os._exit(86)
    flows._write=written
result=run_recipe(recipe,registry=registry,lock=lock,root=root/'run',artifact_source=store.root,resume=mode=='resume')
records=json.loads(sorted((root/'run').glob('execution-*.json'))[-1].read_text())
print(json.dumps({'status':result.status.value,'diagnostic':result.diagnostic,
    'outputs':[row['output'] for row in records['rollouts']], 'calls':len(records['actor_calls'])}))
"""


@pytest.mark.parametrize(
    "boundary",
    [
        "evaluation_cohort.reserved",
        "subject.reserved",
        "subject.settled",
        "prediction",
        "grader.reserved",
        "grader.settled",
        "evaluation.step",
        "evaluation.finish",
        "actor.reserved",
        "actor.settled",
        "candidate",
        "result.json",
    ],
)
def test_adaptive_workflow_recovery_preserves_actual_evaluations_and_candidates(tmp_path, boundary):
    def invoke(mode):
        return subprocess.run(
            [sys.executable, "-c", ADAPTIVE_WORKER, str(tmp_path), boundary, mode],
            text=True,
            capture_output=True,
            timeout=40,
        )

    stopped = invoke("run")
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    resumed = invoke("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert json.loads(resumed.stdout) == {
        "status": "completed",
        "diagnostic": "",
        "outputs": [0, 1, 0, 1, 2, 0, 1, 2, 3],
        "calls": 6,
    }
    assert len((tmp_path / "study/predictions.txt").read_text().splitlines()) == 9
    assert len((tmp_path / "run/external-calls").read_text().splitlines()) == 6
    book = run_ledger(tmp_path / "run")
    before = book.events()
    assert book.balance()["active_slots"] == 0
    again = invoke("resume")
    assert again.returncode == 0 and again.stdout == resumed.stdout
    assert book.events() == before


@pytest.mark.parametrize("field", ["actor", "prediction", "public_score", "inherited_candidate"])
def test_workflow_rejects_resealed_branch_evidence_before_new_dispatch(tmp_path, field):
    from proteus.core.recovery import _load
    from proteus.core.serialization import canonical_json

    def invoke(mode):
        return subprocess.run(
            [sys.executable, "-c", ADAPTIVE_WORKER, str(tmp_path), "result.json", mode],
            text=True,
            capture_output=True,
            timeout=30,
        )

    stopped = invoke("run")
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    receipt = next((tmp_path / "run/private/workflows").glob("*/*/result.json"))
    record = _load(receipt)
    snapshot = record["snapshot"]
    if field == "actor":
        snapshot["actor_calls"][0]["usage"]["input_tokens"] = 999
    elif field == "prediction":
        snapshot["rollouts"][0]["output"] = 999
    elif field == "public_score":
        snapshot["evaluations"][0]["rows"][0]["score"] = 999
    else:
        snapshot["candidates"][0]["validation"] = "invalid"
    terminal = sorted((receipt.parent / "journal/operations").glob("*.result.json"))[-1]
    journal = _load(terminal)
    journal["snapshot"] = snapshot
    for path, value in ((receipt, record), (terminal, journal)):
        path.chmod(0o644)
        path.write_text(canonical_json({"hash": content_hash(value), "value": value}))
    before = run_ledger(tmp_path / "run").events()
    resumed = invoke("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert json.loads(resumed.stdout)["status"] == "failed"
    assert run_ledger(tmp_path / "run").events() == before
    assert len((tmp_path / "study/predictions.txt").read_text().splitlines()) == 2
    assert len((tmp_path / "run/external-calls").read_text().splitlines()) == 1


RECONCILE_WORKER = ADAPTIVE_WORKER.replace(
    "lock=registry.make_lock(recipe,[SOURCE])",
    r"""
from proteus.evaluation.runners import PythonSubject
from proteus.evaluation.records import SubjectResult
from proteus.core.contracts import PluginRef
from test_v1_evaluation import plugin
class Recoverable(PythonSubject):
    def run(self,context):
        result=super().run(context)
        (context.lease.local_root.parent/'receipt.json').write_text(result.to_json())
        if mode=='run':os._exit(86)
        return result
    def reconcile(self,context):
        return SubjectResult.from_json((context.root/'receipt.json').read_text())
definition=registry.get(recipe.subject.runner.plugin)
ref=PluginRef(name='fixture.reconciler',version='1')
registry.register(replace(definition,ref=ref,factory=Recoverable,
    capabilities=(*definition.capabilities,'subject.reconcile')))
recipe=replace(recipe,subject=replace(recipe.subject,runner=plugin(ref.name)))
lock=registry.make_lock(recipe,[SOURCE])
""",
)


def test_workflow_reconciles_old_subject_before_allowing_new_work(tmp_path):
    def invoke(mode):
        return subprocess.run(
            [sys.executable, "-c", RECONCILE_WORKER, str(tmp_path), "none", mode],
            text=True,
            capture_output=True,
            timeout=40,
        )

    stopped = invoke("run")
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    receipt = next((tmp_path / "run/evaluations").glob("*/trials/*/receipt.json"))
    saved = receipt.read_text()
    receipt.unlink()
    book = run_ledger(tmp_path / "run")
    before = book.events()
    for _ in range(2):
        waiting = invoke("resume")
        assert waiting.returncode == 0, waiting.stdout + waiting.stderr
        assert json.loads(waiting.stdout)["status"] == "failed"
        assert "RecoveryRequired" in json.loads(waiting.stdout)["diagnostic"]
        assert book.events() == before
        assert book.balance()["active_slots"] == 1
        assert len((tmp_path / "study/predictions.txt").read_text().splitlines()) == 1
        assert not (tmp_path / "run/external-calls").exists()
    receipt.write_text(saved)
    resumed = invoke("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert json.loads(resumed.stdout) == {
        "status": "completed",
        "diagnostic": "",
        "outputs": [0, 1, 0, 1, 2, 0, 1, 2, 3],
        "calls": 6,
    }
    assert len((tmp_path / "study/predictions.txt").read_text().splitlines()) == 9
    assert len((tmp_path / "run/external-calls").read_text().splitlines()) == 6
    assert book.balance()["active_slots"] == 0


def configure_harbor_workflows(registry, recipe):
    """Use the same real container task as the standalone parallel evaluation test."""

    class Refine:
        def run(self, services, inputs):
            item = inputs["sample"]
            first = services.evaluate(
                item.candidate, "reward", selection=TaskSelection(task_ids=(item.task_id,))
            )
            assert first.batches[0].mean_score() == 0
            response = services.stage(
                "propose", {"prompt": services.values.publish("proteus.text@1", Text(text="42"))}
            )
            text = services.values.get(response.outputs["response"]).text
            child = services.fork_candidate(
                item.candidate,
                delta=FileDelta(
                    base_hash=item.candidate.artifact.content_hash,
                    put=(FileWrite.from_bytes("value.py", f"ANSWER = {text}\n".encode()),),
                    delete=("obsolete.txt",),
                ),
            )
            final = services.evaluate(
                child, "reward", selection=TaskSelection(task_ids=(item.task_id,))
            )
            assert final.batches[0].mean_score() == 1
            return {"candidate": child}

    class Study:
        def run(self, services, restored_state):
            initial = services.initial_candidate()
            jobs = tuple(
                WorkflowJob(
                    key=task.id,
                    workflow="sample",
                    inputs={
                        "sample": services.values.publish(
                            "fixture.sample_input@1",
                            SampleInput(candidate=initial, task_id=task.id),
                        )
                    },
                )
                for task in services.task_pool("reward").tasks
            )
            mapped = services.map_workflows(jobs, operation_id="harbor-samples", max_workers=2)
            return MethodResult(
                status=OperationStatus.COMPLETED if mapped.succeeded else OperationStatus.FAILED,
                outputs={"map": services.values.publish("proteus.workflow_map@1", mapped)},
                diagnostic=";".join(row.diagnostic for row in mapped.results if row.diagnostic),
            )

    registry.register_data("fixture.sample_input@1", SampleInput)
    for name, kind, factory, ports in (
        ("fixture.harbor_workflows", "method", lambda options: Study(), {}),
        (
            "fixture.harbor_refine",
            "workflow",
            lambda options: Refine(),
            {
                "inputs": {"sample": "fixture.sample_input@1"},
                "outputs": {"candidate": "proteus.candidate@1"},
            },
        ),
    ):
        registry.register(
            Plugin(
                ref=PluginRef(name=name, version="1"),
                kind=kind,
                options=EmptyOptions,
                factory=factory,
                source="proteus",
                implementation_hash=content_hash(name),
                capabilities=("run.journal", "run.resume")
                if kind == "method"
                else ("workflow.parallel", "workflow.replay"),
                state_schema=EmptyOptions if kind == "method" else None,
                **ports,
            )
        )
    return registry.resolve(
        replace(
            recipe,
            method=spec_plugin("fixture.harbor_workflows"),
            actors={
                "optimizer": ActorSpec(
                    runtime=spec_plugin("proteus.echo"), budget=BudgetLimits(max_concurrency=2)
                )
            },
            stages=(
                StageSpec(
                    id="propose",
                    operator=spec_plugin("proteus.prompt"),
                    role="optimizer",
                    read_scopes=("proteus.text@1",),
                    write_scopes=("proteus.actor_result@1",),
                ),
            ),
            workflows=(
                WorkflowSpec(
                    id="sample",
                    operator=spec_plugin("fixture.harbor_refine"),
                    stages=("propose",),
                    evaluations=("reward",),
                ),
            ),
            budget=BudgetLimits(max_evaluations=4, max_concurrency=2),
        )
    )


def test_workflows_after_owner_evaluation_and_previous_join_restore_sessions(tmp_path):
    registry, store, recipe = managed_adaptive(tmp_path / "study")

    class Study:
        def run(self, services, restored_state):
            initial = services.initial_candidate()
            services.evaluate(initial, "score", selection=TaskSelection(task_ids=("0",)))
            maps = []
            for round_number in range(2):
                jobs = tuple(
                    WorkflowJob(
                        key=str(i),
                        workflow="sample",
                        inputs={
                            "sample": services.values.publish(
                                "fixture.sample_input@1",
                                SampleInput(candidate=initial, task_id=str(i)),
                            )
                        },
                    )
                    for i in range(3)
                )
                mapped = services.map_workflows(
                    jobs, operation_id=f"round-{round_number}", max_workers=3
                )
                assert mapped.succeeded
                maps.append(services.values.publish("proteus.workflow_map@1", mapped))
            services.evaluate(initial, "score", selection=TaskSelection(task_ids=("0",)))
            return MethodResult(outputs={str(i): ref for i, ref in enumerate(maps)})

    ref = recipe.method.plugin
    registry._plugins[(ref.name, ref.version)] = replace(
        registry.get(ref), factory=lambda options: Study()
    )
    recipe = replace(
        recipe,
        budget=BudgetLimits(max_evaluations=20, max_model_calls=32, max_concurrency=3),
        actors={
            **recipe.actors,
            "generator": replace(
                recipe.actors["generator"],
                budget=BudgetLimits(max_model_calls=20, max_concurrency=2),
            ),
        },
    )
    root = tmp_path / "run"
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root, artifact_source=store.root)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    records = json.loads((root / "execution-0001.json").read_text())
    assert len(records["rollouts"]) == 20 and len(records["actor_calls"]) == 32
    assert len(records["candidates"]) == 13
    ledger = run_ledger(root)
    before = ledger.events()
    assert ledger.balance()["active_slots"] == 0
    assert run_recipe(recipe, registry=registry, lock=lock, root=root, resume=True) == result
    assert ledger.events() == before
    assert len((root / "external-calls").read_text().splitlines()) == 32
