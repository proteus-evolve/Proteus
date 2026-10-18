"""Formal cohorts retain ordering, visibility and usage under concurrent trials."""

from dataclasses import replace
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proteus.core.contracts import BudgetLimits, OperationStatus, Outcome, RuntimeIdentity
from proteus.core.methods import MethodResult
from proteus.core.serialization import ContractError, content_hash
from proteus.evaluation.audit import RunAudit
from proteus.evaluation.records import SubjectResult, TrialRecord
from proteus.evaluation.tasks import TaskSelection
from proteus.recipes.config import ActorSpec
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE, setup as base_setup, register as add_plugin, plugin
from test_v1_ledger import run_ledger


def setup(root, *, hidden=False, timing="after_batch", workers=2, cap=2):
    root.mkdir(parents=True, exist_ok=True)
    code = "import json,sys\nfrom pathlib import Path\nr=json.load(sys.stdin)\n"
    code += f"with Path({str(root / 'invocations.txt')!r}).open('a') as stream: stream.write(str(r['task']['id'])+'\\n')\n"
    code += "print(json.dumps({'output':1,'trace':{'events':[{'seed':r['seed'],'history':r['history']}]}}))\n"
    registry, store, dataset, recipe = base_setup(
        root, code=code, references=(1, 1, 1), hidden=hidden, timing=timing
    )
    recipe = replace(
        recipe,
        execution=replace(recipe.execution, max_workers=workers),
        budget=BudgetLimits(max_concurrency=cap, max_evaluations=3),
    )
    return registry, store, dataset, recipe


def run(root, registry, recipe, *, resume=False):
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(
        recipe,
        registry=registry,
        lock=lock,
        root=root / "run",
        artifact_source=None if resume else root / "input-artifacts",
        resume=resume,
    )
    records = json.loads(sorted((root / "run").glob("execution-*.json"))[-1].read_text())
    return result, records


@pytest.mark.parametrize(
    "hidden,timing", [(False, "after_batch"), (False, "after_prediction"), (True, "never")]
)
def test_complete_cohort_order_private_grades_and_no_repeat_resume(tmp_path, hidden, timing):
    root = tmp_path / "study"
    registry, _, dataset, recipe = setup(root, hidden=hidden, timing=timing)
    result, records = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert [row["task_id"] for row in records["rollouts"]] == ["0", "1", "2"]
    assert len(records["evaluations"]) == (0 if hidden else 1)
    assert records["reserved_evaluations"] == 3
    assert not hidden or not any(
        row["ref"]["kind"] == "proteus.eval.feedback@1" for row in records["values"]
    )
    maps = [row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.eval.map@1"]
    assert len(maps) == 1 and len(maps[0]["rollout_ids"]) == 3 and maps[0]["wall_seconds"] > 0
    resolved = registry.resolve(recipe)
    audit = RunAudit(root / "run", resolved, registry.make_lock(resolved, [SOURCE]), registry)
    receipt = next(
        audit.values.get(ref)
        for ref in result.outputs.values()
        if ref.kind == "proteus.eval.receipt@1"
    )
    assert audit.fixed_cohort(receipt, "score", dataset, "initial")["accuracy"] == 1
    before = run_ledger(root / "run").events()
    assert run(root, registry, recipe, resume=True)[0] == result
    assert len((root / "invocations.txt").read_text().splitlines()) == 3
    assert run_ledger(root / "run").events() == before


def test_parallel_selection_keeps_original_occurrence_seeds_and_one_reservation(tmp_path):
    runs = []
    selection = TaskSelection(task_ids=("2", "0", "2"))
    for width in (1, 3):
        root = tmp_path / str(width)
        registry, _, _, recipe = setup(root, workers=width, cap=3)

        class Study:
            def run(self, services, restored):
                candidate = services.initial_candidate()
                receipt = services.evaluate(candidate, "score", selection=selection)
                return MethodResult(
                    outputs={"score": services.values.publish("proteus.eval.receipt@1", receipt)}
                )

        from proteus.core.plugins import EmptyOptions, Plugin
        from proteus.core.contracts import PluginRef

        registry.register(
            Plugin(
                ref=PluginRef(name="fixture.selected", version="1"),
                kind="method",
                options=EmptyOptions,
                factory=lambda options: Study(),
                implementation_hash=content_hash("selected"),
                source="proteus",
                capabilities=("run.journal", "run.resume"),
                state_schema=EmptyOptions,
            )
        )
        recipe = replace(recipe, method=plugin("fixture.selected"))
        result, records = run(root, registry, recipe)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        trials = [
            TrialRecord.from_json(path.read_text())
            for path in sorted((root / "run").glob("evaluations/*/predictions/*.json"))
        ]
        runs.append(
            [(trial.rollout.task_id, trial.task_seed, trial.history_hash) for trial in trials]
        )
        assert len({trial.rollout.id for trial in trials}) == 3
        assert (
            len(
                [
                    entry
                    for entry in run_ledger(root / "run").entries()
                    if entry["identity"]["metadata"]["kind"] == "evaluation_cohort"
                ]
            )
            == 1
        )
        assert run_ledger(root / "run").balance()["charged"]["evaluations"] == 3
    assert runs[0] == runs[1]
    assert runs[1][0][1] != runs[1][2][1]


@pytest.mark.parametrize("role_cap,expected_peak", [(1, 1), (2, 2)])
def test_http_overlap_multiple_calls_per_trial_and_budget_scopes(
    tmp_path, monkeypatch, role_cap, expected_peak
):
    root = tmp_path / "study"
    registry, _, _, recipe = setup(root, workers=4, cap=2, timing="after_prediction")
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
                    "choices": [{"message": {"content": "1"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            with lock:
                active -= 1
            self.wfile.write(payload)

    class Twice:
        def run(self, context):
            first = context.actor("first:" + context.task.id)
            context.actor("second:" + context.task.id)
            return SubjectResult(
                loaded_artifact_hash=context.candidate.artifact.content_hash,
                runtime=RuntimeIdentity(
                    runtime=context.actor_runtime,
                    environment_hash=content_hash(context.execution.environment),
                ),
                status=Outcome.SUCCEEDED,
                output=1,
                usage=replace(first.usage, model_calls=2),
            )

    add_plugin(
        registry,
        "fixture.twice",
        "subject",
        Twice,
        capabilities=("subject.parallel", "subject.managed_actor"),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PARALLEL_EVAL_KEY", "fixture-secret")
    recipe = replace(
        recipe,
        subject=replace(recipe.subject, runner=plugin("fixture.twice"), actor="subject"),
        actors={
            "subject": ActorSpec(
                runtime=plugin(
                    "proteus.chat",
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    key_env="PARALLEL_EVAL_KEY",
                ),
                model="fixture",
                budget=BudgetLimits(max_concurrency=role_cap, max_model_calls=6),
            )
        },
        budget=replace(recipe.budget, max_model_calls=6),
    )
    try:
        result, records = run(root, registry, recipe)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        assert len(requests) == 6 and peak == expected_peak
        assert len({call["id"] for call in records["actor_calls"]}) == 6
        ledger = run_ledger(root / "run")
        calls = [
            entry for entry in ledger.entries() if entry["identity"]["metadata"]["kind"] == "actor"
        ]
        assert sorted(entry["identity"]["metadata"]["ordinal"] for entry in calls) == [
            1,
            1,
            1,
            2,
            2,
            2,
        ]
        assert len({entry["identity"]["metadata"]["trial_id"] for entry in calls}) == 3
        assert (
            ledger.balance()["charged"]["model_calls"] == 6
            and ledger.balance()["active_slots"] == 0
        )
        assert sum(call["usage"]["input_tokens"] for call in records["actor_calls"]) == 12
        assert ledger.balance()["unknown"]["cost_usd"] == 6
        assert run(root, registry, recipe, resume=True)[0] == result and len(requests) == 6
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


WORKER = r"""
import os,sys,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_parallel_evaluation import setup,run
from proteus.recipes.config import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.core.recovery import Journal
from proteus.evaluation.service import EvaluationSession
import proteus.evaluation.parallel as parallel
root=Path(sys.argv[1]);boundary,mode=sys.argv[2:]
if mode=='run':
    registry,_,_,recipe=setup(root,workers=2,cap=1,hidden=True)
    (root/'recipe.json').write_text(recipe.to_json())
    if boundary=='uncertain':
        from proteus.evaluation.runners import PythonSubject
        def uncertain(self,context):
            with (root/'invocations.txt').open('a') as stream:stream.write('uncertain\n')
            from proteus.core.recovery import _load
            plan=next((root/'run/private/evaluation-maps').glob('*/plan.json'))
            parallel._write(plan.parent/'000001/request.json',parallel.TrialMap.request(_load(plan),1))
            os._exit(86)
        PythonSubject.run=uncertain
    original=parallel._write
    def write(path,value):
        original(path,value)
        if path.name==boundary:os._exit(86)
    parallel._write=write
    original_save=EvaluationSession._save
    def save(self,name,value):
        original_save(self,name,value)
        if boundary=='prediction' and name=='predictions/000000.json':os._exit(86)
    EvaluationSession._save=save
    original_call=Journal.call
    def call(self,kind,*args,**kwargs):
        result=original_call(self,kind,*args,**kwargs)
        if kind==boundary:os._exit(86)
        return result
    Journal.call=call
else:
    registry=builtin_registry();recipe=load_recipe(root/'recipe.json')
result,records=run(root,registry,recipe,resume=mode=='resume')
print(json.dumps({'status':result.status.value,'diagnostic':result.diagnostic,'calls':len((root/'invocations.txt').read_text().splitlines()),
    'tasks':[row['task_id'] for row in records['rollouts']]}))
"""


@pytest.mark.parametrize(
    "boundary",
    [
        "plan.json",
        "request.json",
        "prediction",
        "result.json",
        "join.json",
        "evaluation.map",
        "evaluation.finish",
    ],
)
def test_hard_exit_recovers_trials_without_reexecution(tmp_path, boundary):
    root = tmp_path / "study"

    def invoke(mode):
        return subprocess.run(
            [sys.executable, "-c", WORKER, str(root), boundary, mode],
            text=True,
            capture_output=True,
            timeout=30,
        )

    first = invoke("run")
    assert first.returncode == 86, first.stdout + first.stderr
    resumed = invoke("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    output = json.loads(resumed.stdout)
    assert output == {"status": "completed", "diagnostic": "", "calls": 3, "tasks": ["0", "1", "2"]}
    before = run_ledger(root / "run").events()
    again = invoke("resume")
    assert again.returncode == 0 and json.loads(again.stdout) == output
    assert run_ledger(root / "run").events() == before


def test_preflight_rejects_state_and_missing_capabilities(tmp_path):
    from proteus.core.contracts import StatePolicy

    registry, _, _, recipe = setup(tmp_path / "study")
    with pytest.raises(ContractError, match="independent"):
        replace(recipe.execution, state_policy=StatePolicy.SEQUENCE)

    class Unsupported:
        pass

    add_plugin(registry, "fixture.no_parallel", "subject", Unsupported)
    with pytest.raises(ContractError, match="parallel"):
        registry.resolve(
            replace(recipe, subject=replace(recipe.subject, runner=plugin("fixture.no_parallel")))
        )


def test_unavailable_task_remains_in_the_full_parallel_cohort(tmp_path):
    from proteus.artifacts.codecs import FileContent
    from proteus.core.contracts import PluginRef

    root = tmp_path / "study"
    registry, store, _, recipe = setup(root)
    artifact = store.put(
        PluginRef(name="proteus.file_tree", version="1"),
        {
            "main.py": FileContent(
                b"import json,sys\nr=json.load(sys.stdin)\nprint('invalid response' if r['task']['id']=='1' else json.dumps({'output':1}))\n"
            )
        },
    )
    recipe = replace(recipe, subject=replace(recipe.subject, initial_artifact=artifact))
    result, records = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert len(records["rollouts"]) == 3 and records["reserved_evaluations"] == 3
    assert [row["score"] for row in records["evaluations"][0]["rows"]] == [1, None, 1]
    assert run_ledger(root / "run").balance()["active_slots"] == 0


def test_unknown_trial_stops_recovery_before_new_dispatch(tmp_path):
    from proteus.core.recovery import recovery_status

    root = tmp_path / "study"

    def invoke(mode):
        return subprocess.run(
            [sys.executable, "-c", WORKER, str(root), "uncertain", mode],
            text=True,
            capture_output=True,
            timeout=30,
        )

    stopped = invoke("run")
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    for _ in range(2):
        resumed = invoke("resume")
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        output = json.loads(resumed.stdout)
        assert output["status"] == "failed" and "RecoveryRequired" in output["diagnostic"]
        assert output["calls"] == 1 and output["tasks"] == []
    report = recovery_status(root / "run")
    assert report["pending"]["kind"] == "evaluation.map"
    assert [item["state"] for item in report["pending"]["branches"]] == [
        "submitted",
        "submitted",
        "not_submitted",
    ]
    assert {item["kind"] for item in report["unsettled"]} == {"evaluation_cohort", "subject"}
    assert run_ledger(root / "run").balance()["charged"]["evaluations"] == 3


def test_forged_prediction_cannot_override_settled_subject_result(tmp_path):
    from proteus.core.recovery import _load

    root = tmp_path / "study"
    stopped = subprocess.run(
        [sys.executable, "-c", WORKER, str(root), "result.json", "run"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    receipt = next((root / "run/private/evaluation-maps").glob("*/000000/result.json"))
    data = _load(receipt)
    data["observation"]["rollout"]["output"] = 999
    data["trial"]["rollout"]["output"] = 999
    receipt.chmod(0o644)
    receipt.write_text(json.dumps({"hash": content_hash(data), "value": data}))
    prediction = next((root / "run/evaluations").glob("*/predictions/000000.json"))
    prediction.chmod(0o644)
    prediction.write_text(json.dumps(data["trial"]))
    resumed = subprocess.run(
        [sys.executable, "-c", WORKER, str(root), "result.json", "resume"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    output = json.loads(resumed.stdout)
    assert output["status"] == "failed" and "settled subject" in output["diagnostic"]
    assert output["calls"] == 1


@pytest.mark.skipif(
    not os.environ.get("PROTEUS_HARBOR_PYTHON"),
    reason="requires pinned Harbor runtime and live Docker",
)
def test_live_harbor_parallel_trials_build_cache_and_independent_verifiers(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from proteus.artifacts.store import ArtifactStore
    from proteus.evaluation.tasks import TaskDataset, TaskRecord
    from proteus.execution.builds import SourceBuildSpec, BuildReceipt
    from proteus.examples import harbor_evaluation
    from proteus.methods.compare import CompareOptions
    from proteus.recipes.config import load_recipe
    from proteus.recipes.builtins import builtin_registry

    root = Path(os.environ.get("PROTEUS_HARBOR_STUDY", str(tmp_path))) / "parallel-evaluation"
    barrier = threading.Barrier(2)
    arrivals = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            arrivals.append(self.path)
            barrier.wait(timeout=90)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Both real candidate containers rendezvous at a fixture-only HTTP barrier
    # on their host gateway. This proves overlap without depending on timing.
    rendezvous = f"""\nimport socket, time, urllib.request
started = time.time()
route = next(line.split()[2] for line in Path('/proc/net/route').read_text().splitlines()[1:] if line.split()[1] == '00000000')
gateway = socket.inet_ntoa(int(route, 16).to_bytes(4, 'little'))
with urllib.request.build_opener(urllib.request.ProxyHandler({{}})).open('http://' + gateway + ':{server.server_port}/ready', timeout=100) as response:
    assert response.read() == b'ok'
ended = time.time()
"""
    source = harbor_evaluation.SOURCE.replace(
        "request = json.load(sys.stdin)", "request = json.load(sys.stdin)" + rendezvous
    )
    source = source.replace(
        "'obsolete': Path('obsolete.txt').exists()",
        "'obsolete': Path('obsolete.txt').exists(), 'started': started, 'ended': ended",
    )
    monkeypatch.setattr(harbor_evaluation, "SOURCE", source)
    try:
        recipe = load_recipe(
            harbor_evaluation.prepare(root / "study", os.environ["PROTEUS_HARBOR_PYTHON"])
        )
        registry = builtin_registry()
        store = ArtifactStore(root / "study/artifact-store", registry)
        task = root / "study/task"
        config = task / "task.toml"
        config.write_text(config.read_text().replace("timeout_sec = 3\n", "timeout_sec = 110\n"))
        task_tree = store.capture_tree(task)
        path = root / "study/tasks.json"
        original = TaskDataset.from_json(path.read_text()).records[0]
        data = TaskDataset(
            version="parallel-harbor-proof-v1",
            records=tuple(
                TaskRecord(
                    task=replace(
                        original.task,
                        id=f"answer-{i}",
                        input={"path": str(task), "tree_hash": task_tree.content_hash},
                    ),
                    reference=None,
                )
                for i in range(2)
            ),
        )
        path.write_text(data.to_json() + "\n")
        evaluation = recipe.evaluations[0]
        build = SourceBuildSpec(commands=("python -m py_compile main.py value.py",))
        env = recipe.execution.environment
        env = replace(
            env,
            backend=plugin(
                "proteus.harbor", **dict(env.backend.options), source_build=build.to_dict()
            ),
            build_hash=content_hash(build),
        )
        recipe = registry.resolve(
            replace(
                recipe,
                method=plugin(
                    "proteus.compare", **CompareOptions(evaluations=("reward",)).to_dict()
                ),
                execution=replace(
                    recipe.execution, environment=env, max_workers=2, timeout_seconds=110
                ),
                evaluations=(
                    replace(
                        evaluation,
                        tasks=replace(
                            evaluation.tasks,
                            dataset_version=data.version,
                            dataset_hash=content_hash(data),
                            task_ids=("answer-0", "answer-1"),
                        ),
                    ),
                ),
                budget=BudgetLimits(max_evaluations=2, max_concurrency=2),
            )
        )
        lock = registry.make_lock(recipe, [SOURCE])
        (root / "recipe.json").write_text(recipe.to_json())
        (root / "lock.json").write_text(lock.to_json())
        result = run_recipe(
            recipe, registry=registry, lock=lock, root=root / "run", artifact_source=store.root
        )
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        records = json.loads((root / "run/execution-0001.json").read_text())
        assert arrivals == ["/ready", "/ready"]
        outputs = [row["output"] for row in records["rollouts"]]
        assert all(row["answer"] == 41 and row["obsolete"] for row in outputs)
        assert max(row["started"] for row in outputs) < min(row["ended"] for row in outputs)
        assert [row["score"] for row in records["evaluations"][0]["rows"]] == [0, 0]
        receipts = [
            BuildReceipt.from_json(path.read_text())
            for path in (root / "run/private/builds/attempts").glob("*/receipt.json")
        ]
        assert len(receipts) == 2 and sum(item.cache_hit for item in receipts) == 1
        assert len({item.image_id for item in receipts}) == 1
        proofs = []
        for path in (root / "run/evaluations").glob("*/trials/*/private/response.json"):
            response = json.loads(path.read_text())
            agent, verifier = response["loaded_proof"], response["verifier_proof"]
            assert (
                agent["container_id"] != verifier["container_id"]
                and agent["image_id"] != verifier["image_id"]
            )
            assert agent["build_receipt_hash"] in {content_hash(item) for item in receipts}
            assert json.loads(path.with_name("cleanup.json").read_text())["verified"]
            proofs.append(agent)
        assert len(proofs) == 2 and len({item["container_id"] for item in proofs}) == 2
        assert "PROTEUS_PRIVATE_VERIFIER_23" not in json.dumps(records)
        book = run_ledger(root / "run")
        assert book.balance()["charged"]["evaluations"] == 2 and book.balance()["active_slots"] == 0
        before = book.events()
        assert (
            run_recipe(recipe, registry=registry, lock=lock, root=root / "run", resume=True)
            == result
        )
        assert book.events() == before and len(arrivals) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(
    not os.environ.get("PROTEUS_HARBOR_PYTHON"),
    reason="requires pinned Harbor runtime and live Docker",
)
def test_live_harbor_parallel_managed_code_keeps_input_memory_and_usage(tmp_path):
    import os
    from pathlib import Path
    from proteus.examples.harbor_code_memory import prepare
    from proteus.examples.code_memory import register
    from proteus.recipes.config import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from test_v1_harbor_code import evidence

    root = Path(os.environ.get("PROTEUS_HARBOR_STUDY", str(tmp_path))) / "parallel-managed-code"
    recipe = load_recipe(prepare(root / "study", os.environ["PROTEUS_HARBOR_PYTHON"]))
    registry = builtin_registry()
    register(registry)
    recipe = registry.resolve(
        replace(
            recipe,
            execution=replace(recipe.execution, max_workers=2),
            actors={
                key: replace(actor, budget=replace(actor.budget, max_concurrency=2))
                for key, actor in recipe.actors.items()
            },
            budget=replace(recipe.budget, max_concurrency=2),
        )
    )
    lock = registry.make_lock(recipe, [SOURCE])
    (root / "recipe.json").write_text(recipe.to_json())
    (root / "lock.json").write_text(lock.to_json())
    result = run_recipe(
        recipe,
        registry=registry,
        lock=lock,
        root=root / "run",
        artifact_source=root / "study/artifact-store",
    )
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    records = json.loads((root / "run/execution-0001.json").read_text())
    assert [row["output"] for row in records["rollouts"]] == [2, 3, 4, 6, 14]
    assert (
        len(records["actor_calls"]) == 7 and len({row["id"] for row in records["actor_calls"]}) == 7
    )
    assert len(evidence(root / "run", 6)) == 6
    book = run_ledger(root / "run")
    before = book.events()
    assert (
        book.balance()["charged"]["model_calls"] == 0
        and book.balance()["charged"]["evaluations"] == 5
    )
    assert book.balance()["active_slots"] == 0
    assert (
        run_recipe(recipe, registry=registry, lock=lock, root=root / "run", resume=True) == result
    )
    assert book.events() == before
