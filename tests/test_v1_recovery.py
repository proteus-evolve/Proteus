"""Hard process-exit recovery across candidate, role, prediction and method commits."""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from proteus.core.contracts import OperationStatus
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.recovery import Journal, recovery_status
from proteus.core.serialization import Contract, ContractError, canonical_json, content_hash
from proteus.core.contracts import PluginRef
from proteus.evaluation.runners import PythonSubject
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import setup, SOURCE, plugin
from test_v1_ledger import run_ledger


WORKER = r'''
import json, os, sys
from dataclasses import replace
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_v1_evaluation import setup, SOURCE
from test_v1_plugins import stage_recipe
from proteus.core.contracts import BudgetLimits
from proteus.core.ledger import Ledger
from proteus.core.recovery import Journal
from proteus.artifacts.store import ArtifactStore
from proteus.evaluation.runners import PythonSubject
from proteus.evaluation.service import EvaluationSession
from proteus.recipes.runner import run_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.builtins import EchoActor

root, boundary, resume, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]=='resume', sys.argv[4]
root.mkdir(parents=True, exist_ok=True)
marker = root / 'external-calls'
if mode == 'evaluation':
    source = "from pathlib import Path\nwith Path(" + repr(str(marker)) + ").open('a') as f: f.write('call'+chr(10))\n"
    source += "import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({'output':42,'state':r['state']}))\n"
    registry, store, _, recipe = setup(root, code=source)
    recipe = replace(recipe, budget=BudgetLimits(max_evaluations=2))
    artifact_source = store.root
else:
    registry, recipe, artifact_source = builtin_registry(), stage_recipe(), None
    execute = EchoActor.execute
    def actor(self, *args, **kwargs):
        with marker.open('a') as f: f.write('call\n')
        result = execute(self, *args, **kwargs)
        if boundary == 'actor.uncertain' and not resume: os._exit(86)
        return result
    EchoActor.execute = actor
if not resume:
    register_candidate = ArtifactStore.register_candidate
    def registered(self, *args, **kwargs):
        result = register_candidate(self, *args, **kwargs)
        if boundary == 'candidate.registered': os._exit(86)
        return result
    ArtifactStore.register_candidate = registered
    save = EvaluationSession._save
    def saved(self, name, value):
        result = save(self, name, value)
        if boundary == 'prediction.published' and name.startswith('predictions/'): os._exit(86)
        if boundary == 'grade.published' and name.startswith('private/grades/'): os._exit(86)
        return result
    EvaluationSession._save = saved
    original = Journal.call
    def call(self, kind, *args, **kwargs):
        result = original(self, kind, *args, **kwargs)
        if boundary == kind: os._exit(86)
        return result
    Journal.call = call
    settle = Ledger.settle
    def settled(self, entry_id, **kwargs):
        result = settle(self, entry_id, **kwargs)
        if boundary == self.entry(entry_id)['identity']['metadata']['kind'] + '.settled': os._exit(86)
        return result
    Ledger.settle = settled
    subject = PythonSubject.run
    def predict(self, context):
        result = subject(self, context)
        if boundary == 'subject.uncertain': os._exit(86)
        return result
    PythonSubject.run = predict
lock=registry.make_lock(recipe, [SOURCE])
(root/'recipe.json').write_text(recipe.to_json())
(root/'lock.json').write_text(lock.to_json())
result=run_recipe(recipe, registry=registry, lock=lock, root=root/'run', resume=resume, artifact_source=artifact_source)
print(result.to_json())
'''


def invoke(root, boundary, *, resume=False, mode="evaluation"):
    return subprocess.run([sys.executable, "-c", WORKER, str(root), boundary,
                           "resume" if resume else "run", mode], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("boundary", ["candidate.registered", "candidate", "evaluation.open", "subject.settled",
    "prediction.published", "evaluation.step", "grader.settled", "grade.published", "evaluation.finish", "method.result"])
def test_hard_exit_reuses_committed_work_and_preserves_cohort_budget(tmp_path, boundary):
    interrupted = invoke(tmp_path, boundary)
    assert interrupted.returncode == 86, interrupted.stderr
    resumed = invoke(tmp_path, boundary, resume=True)
    assert resumed.returncode == 0, resumed.stderr
    result = MethodResult.from_json(resumed.stdout)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert (tmp_path / "external-calls").read_text().splitlines() == ["call", "call"]
    book = run_ledger(tmp_path / "run")
    assert book.balance()["charged"]["evaluations"] == 2 and book.balance()["active_slots"] == 0
    assert len(book.entries()) == 5
    before = book.events()
    again = invoke(tmp_path, boundary, resume=True)
    assert again.returncode == 0, again.stderr
    assert MethodResult.from_json(again.stdout) == result
    assert book.events() == before
    assert len((tmp_path / "external-calls").read_text().splitlines()) == 2
    exported = json.loads(sorted((tmp_path / "run").glob("execution-*.json"))[-1].read_text())
    assert len(exported["candidates"]) == 1 and len(exported["rollouts"]) == 2 and len(exported["evaluations"]) == 1


@pytest.mark.parametrize("boundary", ["actor.settled", "stage", "method.result"])
def test_stage_recovery_reuses_actor_result(tmp_path, boundary):
    interrupted = invoke(tmp_path, boundary, mode="stage")
    assert interrupted.returncode == 86, interrupted.stderr
    resumed = invoke(tmp_path, boundary, resume=True, mode="stage")
    assert resumed.returncode == 0, resumed.stderr
    result = MethodResult.from_json(resumed.stdout)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert (tmp_path / "external-calls").read_text().splitlines() == ["call"]
    book = run_ledger(tmp_path / "run")
    assert len(book.entries()) == 1 and book.balance()["active_slots"] == 0


@pytest.mark.parametrize("mode,boundary", [("evaluation", "subject.uncertain"), ("stage", "actor.uncertain")])
def test_uncertain_external_work_is_not_reissued(tmp_path, mode, boundary):
    interrupted = invoke(tmp_path, boundary, mode=mode)
    assert interrupted.returncode == 86, interrupted.stderr
    book = run_ledger(tmp_path / "run")
    before = book.events()
    for _ in range(2):
        resumed = invoke(tmp_path, boundary, resume=True, mode=mode)
        assert resumed.returncode == 0, resumed.stderr
        result = MethodResult.from_json(resumed.stdout)
        assert result.status == OperationStatus.FAILED and "RecoveryRequired" in result.diagnostic
        assert book.events() == before
    assert book.balance()["active_slots"] == 1
    assert (tmp_path / "external-calls").read_text().splitlines() == ["call"]
    report = recovery_status(tmp_path / "run")
    assert report["pending"] is not None and report["unsettled"]
    assert "prompt" not in json.dumps(report) and "PRIVATE" not in json.dumps(report)


@dataclass(frozen=True, kw_only=True)
class StreamState(Contract):
    candidate_id: str
    session_id: str
    draws: tuple[int, ...] = ()


class StreamMethod:
    def run(self, services, restored_state):
        if restored_state is None:
            candidate = services.initial_candidate()
            session = services.open_evaluation("score", operation_id="training")
            state = StreamState(candidate_id=candidate.id, session_id=session.operation_id)
        else:
            state = StreamState.from_dict(restored_state.state.payload)
            candidate = next(item for item in services.candidates if item.id == state.candidate_id)
            session = services.evaluation_session(state.session_id)
            assert restored_state.random_state["method"] == {"custom_seed": 7}
            assert content_hash(restored_state.random_state["framework"]) == content_hash(services.rng.getstate())
            assert services.values.get(restored_state.archive) == candidate
            assert restored_state.sampling_cursor == session.position
            assert [request.operation_id for request in restored_state.pending_operations] == ["training"]
        archive = services.values.publish("proteus.candidate@1", candidate)
        while session.position < len(session.batch.tasks):
            state = replace(state, draws=(*state.draws, services.rng.randrange(10**9)))
            session.step(candidate)
            checkpoint = services.checkpoint(state, archive=archive, sampling_cursor=session.position,
                                             random_state={"custom_seed": 7})
        session.finish()
        return MethodResult(state=checkpoint)


def stream_fixture(root, *, hidden):
    registry, store, _, recipe = setup(root, sequence=True, hidden=hidden,
        references=("PRIVATE_STREAM_GOLD", "PRIVATE_STREAM_GOLD"))
    registry.register(Plugin(ref=PluginRef(name="fixture.stream", version="1"), kind="method", options=EmptyOptions,
        factory=lambda options: StreamMethod(), source="proteus", implementation_hash=content_hash("stream-fixture"),
        capabilities=("run.resume", "run.journal"), state_schema=StreamState))
    return registry, store, replace(recipe, method=plugin("fixture.stream"))


@pytest.mark.parametrize("hidden", [False, True])
def test_online_checkpoint_restores_state_rng_archive_and_feedback_privacy(tmp_path, monkeypatch, hidden):
    registry, store, recipe = stream_fixture(tmp_path, hidden=hidden)
    lock = registry.make_lock(recipe, [SOURCE])
    calls = []
    original_run = PythonSubject.run
    def subject(self, context):
        calls.append(context.task.id)
        return original_run(self, context)
    monkeypatch.setattr(PythonSubject, "run", subject)
    class Crash(BaseException):
        pass
    checkpoint = Journal.checkpoint
    def interrupt(self, *args, **kwargs):
        result = checkpoint(self, *args, **kwargs)
        if result.sampling_cursor == 1:
            raise Crash()
        return result
    root = tmp_path / "run"
    monkeypatch.setattr(Journal, "checkpoint", interrupt)
    with pytest.raises(Crash):
        run_recipe(recipe, registry=registry, lock=lock, root=root, artifact_source=store.root)
    monkeypatch.setattr(Journal, "checkpoint", checkpoint)
    resumed = run_recipe(recipe, registry=registry, lock=lock, root=root, resume=True)
    assert resumed.status == OperationStatus.COMPLETED, resumed.diagnostic
    assert calls == ["0", "1"]
    records = json.loads((root / "execution-0002.json").read_text())
    assert [rollout["output"] for rollout in records["rollouts"]] == [1, 2]
    if hidden:
        assert "PRIVATE_STREAM_GOLD" not in json.dumps(records)
        assert not records["evaluations"]
    control = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / "control", artifact_source=store.root)
    assert resumed.state.state.payload["draws"] == control.state.state.payload["draws"]
    assert resumed.state.random_state == control.state.random_state


def test_corrupt_journal_and_changed_configuration_fail_before_replay(tmp_path):
    assert invoke(tmp_path, "evaluation.step").returncode == 86
    path = sorted((tmp_path / "run/private/recovery/operations").glob("*.result.json"))[-1]
    data = json.loads(path.read_text())
    data["value"]["snapshot"]["run_id"] = "another run"
    path.chmod(0o644)
    path.write_text(json.dumps(data))
    resumed = invoke(tmp_path, "evaluation.step", resume=True)
    assert resumed.returncode != 0 and "integrity mismatch" in resumed.stderr
    assert len((tmp_path / "external-calls").read_text().splitlines()) == 1


def test_checkpoint_state_version_is_validated_before_restoring(tmp_path):
    assert invoke(tmp_path, "evaluation.step").returncode == 86
    directory = tmp_path / "run/private/recovery/operations"
    for request in sorted(directory.glob("*.request.json")):
        if json.loads(request.read_text())["value"]["kind"] == "checkpoint":
            path = request.with_name(request.name.replace(".request.", ".result."))
            envelope = json.loads(path.read_text())
            envelope["value"]["output"]["state"]["payload_version"] = 9
            envelope["hash"] = content_hash(envelope["value"])
            path.chmod(0o644)
            path.write_text(canonical_json(envelope))
            break
    resumed = invoke(tmp_path, "evaluation.step", resume=True)
    assert resumed.returncode != 0 and "state version" in resumed.stderr


def test_changed_recipe_cannot_replenish_budget_on_resume(tmp_path):
    registry, store, _, recipe = setup(tmp_path)
    lock = registry.make_lock(recipe, [SOURCE])
    root = tmp_path / "run"
    run_recipe(recipe, registry=registry, lock=lock, root=root, artifact_source=store.root)
    changed = replace(recipe, budget=replace(recipe.budget, max_evaluations=100))
    with pytest.raises(ContractError):
        run_recipe(changed, registry=registry, lock=registry.make_lock(changed, [SOURCE]), root=root, resume=True)


def test_cli_recovery_reports_uncertainty_without_private_inputs(tmp_path, capsys):
    from proteus.cli import main
    assert invoke(tmp_path, "subject.uncertain").returncode == 86
    assert main(["recipe", "recovery", str(tmp_path / "recipe.json"), "--lock", str(tmp_path / "lock.json"),
                 "--out", str(tmp_path / "run")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["pending"]["kind"] == "evaluation.step"
    assert {entry["kind"] for entry in report["unsettled"]} == {"evaluation_cohort", "subject"}
    assert "input" not in json.dumps(report) and "prompt" not in json.dumps(report)


LIVE_WORKER = r'''
import os, subprocess, sys, time
from pathlib import Path
from proteus.core.ledger import Ledger
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
root=Path(sys.argv[1]); resume=sys.argv[2]=='resume'
boundary=sys.argv[3]
recipe,registry=load_recipe(root/'recipe.json'),builtin_registry()
revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
lock=registry.make_lock(recipe,[SourcePin(name='proteus',repository='https://github.com/proteus-evolve/Proteus.git',revision=revision)])
if not resume and boundary=='worker.running':
    from proteus.execution import harbor
    harbor.WORKER=root/'barrier-worker.py'
    original_popen=subprocess.Popen
    def launch(args,*pos,**kw):
        process=original_popen(args,*pos,**kw)
        if len(args)>3 and args[2]==str(harbor.WORKER) and args[3]=='run':
            deadline=time.monotonic()+30
            while not (root/'worker-ready').exists():
                if process.poll() is not None: raise RuntimeError('worker exited before barrier')
                if time.monotonic()>deadline: raise TimeoutError('worker barrier')
                time.sleep(.05)
            os._exit(86)
        return process
    subprocess.Popen=launch
elif not resume:
    original=Ledger.settle
    def settle(self, entry_id, **kwargs):
        result=original(self,entry_id,**kwargs)
        if self.entry(entry_id)['identity']['metadata']['kind']=='subject': os._exit(86)
        return result
    Ledger.settle=settle
result=run_recipe(recipe,registry=registry,lock=lock,root=root/'run',resume=resume,artifact_source=root/'artifact-store')
print(result.to_json())
'''


LIVE_BARRIER_WORKER = r'''
import asyncio,importlib.util,os,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('harbor_test_worker', WORKER_PATH)
worker=importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
sys.modules['__main__']=worker
run=worker.run
root=Path(__file__).resolve().parent
async def barrier(request,output):
    (root/'worker-ready').write_text(str(os.getpid()))
    deadline=asyncio.get_running_loop().time()+60
    while not (root/'worker-release').exists():
        if asyncio.get_running_loop().time()>deadline: raise TimeoutError('test release barrier')
        await asyncio.sleep(.05)
    await run(request,output)
worker.run=barrier
worker.main()
'''


@pytest.mark.skipif(not os.environ.get("PROTEUS_HARBOR_PYTHON"), reason="requires pinned Harbor runtime and live Docker")
@pytest.mark.parametrize("boundary", ["subject.settled", "worker.running"])
def test_live_harbor_recovery_preserves_build_and_verifier_without_new_trial(tmp_path, boundary):
    from proteus.examples.harbor_source_build import prepare
    from proteus.execution.harbor import WORKER as HARBOR_WORKER
    root = Path(os.environ.get("PROTEUS_HARBOR_STUDY", str(tmp_path))) / ("recovery-build-" + boundary)
    prepare(root, os.environ["PROTEUS_HARBOR_PYTHON"])
    (root / "barrier-worker.py").write_text("WORKER_PATH=" + repr(str(HARBOR_WORKER)) + "\n" + LIVE_BARRIER_WORKER)
    def invoke(mode):
        return subprocess.run([sys.executable, "-c", LIVE_WORKER, str(root), mode, boundary],
                              capture_output=True, text=True, timeout=600)
    first = invoke("run")
    assert first.returncode == 86, first.stderr
    book = run_ledger(root / "run")
    if boundary == "worker.running":
        before = book.events()
        try:
            pending = invoke("resume")
            assert pending.returncode == 0, pending.stderr
            result = MethodResult.from_json(pending.stdout)
            assert result.status == OperationStatus.FAILED and "still running" in result.diagnostic
            assert before == book.events() and book.balance()["active_slots"] == 1
            os.kill(int((root / "worker-ready").read_text()), 0)
        finally:
            (root / "worker-release").touch()
        deadline = time.monotonic() + 300
        while not list((root / "run/evaluations").glob("*/trials/*/private/worker-completed.json")):
            assert time.monotonic() < deadline, "orphan worker did not complete its original dispatch"
            time.sleep(.1)
    assert len(list((root / "run/evaluations").glob("*/trials/*/private/response.json"))) == 1
    second = invoke("resume")
    assert second.returncode == 0, second.stderr
    result = MethodResult.from_json(second.stdout)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    records = json.loads(sorted((root / "run").glob("execution-*.json"))[-1].read_text())
    assert [row["rows"][0]["score"] for row in records["evaluations"]] == [0, 1, 0, None]
    responses = list((root / "run/evaluations").glob("*/trials/*/private/response.json"))
    assert len(responses) == 3
    ids = set()
    for path in responses:
        response = json.loads(path.read_text())
        assert response["loaded_proof"]["image_id"] != response["verifier_proof"]["image_id"]
        assert json.loads((path.parent / "cleanup.json").read_text())["verified"]
        ids.add(response["loaded_proof"]["container_id"])
    assert len(ids) == 3
    book = run_ledger(root / "run")
    assert len(book.entries()) == 16 and book.balance()["active_slots"] == 0
    evaluation_ops = {request["operation_id"] for request in records["requests"]
                      if request["stage"]["name"] == "proteus.evaluation"}
    wall = sum(result["usage"]["wall_seconds"] for result in records["results"]
               if result["operation_id"] in evaluation_ops)
    assert wall == pytest.approx(book.balance()["charged"]["wall_seconds"])
    assert "PROTEUS_PRIVATE_VERIFIER_23" not in json.dumps(records)
    before = book.events()
    again = invoke("resume")
    assert again.returncode == 0 and MethodResult.from_json(again.stdout) == result
    assert book.events() == before
