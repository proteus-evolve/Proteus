"""Reconciliation accepts only bound, completed external work; never redispatches."""

import json
import subprocess
import sys
from dataclasses import replace

import pytest

from proteus.core.contracts import Candidate, Outcome
from proteus.core.recovery import RecoveryRequired
from proteus.core.serialization import ContractError, canonical_json, content_hash
from proteus.evaluation.runners import SubjectRecoveryContext
from proteus.evaluation.tasks import StateValue, TaskDataset
from proteus.execution.harbor import HarborSubject, HarborSubjectOptions, completed_worker
from proteus.execution import harbor_worker
from proteus.execution.builds import BuildReceipt
from proteus.evaluation.records import zero_usage
from proteus.recipes.builtins import builtin_registry
from test_v1_harbor import fixture_recipe
from test_v1_ledger import run_ledger
from proteus.core.methods import MethodResult
from proteus.core.contracts import OperationStatus


def bundle(tmp_path):
    recipe, task_path = fixture_recipe(tmp_path)
    recipe = builtin_registry().resolve(recipe)
    task = TaskDataset.from_json((task_path.parent / "tasks.json").read_text()).records[0].task
    candidate = Candidate(id="initial", run_id="run", artifact=recipe.subject.initial_artifact,
                          created_by="initial", created_attempt_id="attempt-1")
    root = tmp_path / "trial"
    private = root / "private"
    private.mkdir(parents=True)
    (private / "worker.lock").touch()
    options = recipe.execution.environment.backend.options
    context = SubjectRecoveryContext(candidate=candidate, task=task, state=StateValue(), history=(), seed=7,
                                    execution=recipe.execution, subject=recipe.subject, root=root)
    request = {"runtime_hash": options["runtime_hash"], "task_dir": str(private / "task"),
        "trials_dir": str(private / "trials"), "candidate_dir": str(root / "candidate"),
        "trial_name": "proteus-" + "a" * 32, "candidate_id": candidate.id, "artifact": candidate.artifact,
        "task_hash": content_hash(task), "task": task, "state": context.state, "seed": 7,
        "entrypoint": "main.py", "arguments": [], "model": None, "credential_env": [],
        "timeout_seconds": recipe.execution.timeout_seconds, "setup_timeout_seconds": options["setup_timeout_seconds"],
        "image_digest": recipe.execution.environment.image_digest, "agent_image": recipe.execution.environment.image_digest,
        "build_receipt": None, "resources": recipe.execution.environment.resources}
    timing = {"started_at": "2026-10-17T00:00:00Z", "finished_at": "2026-10-17T00:00:03Z"}
    response = {"request_hash": content_hash(request), "runtime": {"runtime_hash": options["runtime_hash"]},
        "result": {"config": {"trial_name": request["trial_name"]}, "id": "trial-uuid", "task_checksum": "checksum",
                   "verifier_environment_mode": "separate", "agent_execution": timing, "verifier": timing},
        "loaded_proof": {"loaded_artifact_hash": candidate.artifact.content_hash, "candidate_id": candidate.id,
                         "task_hash": content_hash(task), "requested_image": request["agent_image"],
                         "build_receipt_hash": None, "nano_cpus": 10**9, "memory_bytes": 512 * 1024**2},
        "verifier_proof": {"container_id": "verifier"},
        "execution": {"return_code": 0, "stdout": '{"output": 42}'}}
    cleanup = {"verified": True, "resources": [], "errors": []}
    receipt = {"version": 1, "request_hash": content_hash(request), "response_hash": content_hash(response),
               "cleanup_hash": content_hash(cleanup), "return_code": 0, "elapsed_seconds": 9.5}
    for name, value in (("request.json", request), ("response.json", response),
                        ("worker-dispatch.json", {"request_hash": content_hash(request)}),
                        ("worker-cleanup.json", cleanup), ("worker-completed.json", receipt)):
        (private / name).write_text(canonical_json(value))
    return context, request, private


def test_worker_receipt_cannot_adopt_another_request(tmp_path):
    _, request, private = bundle(tmp_path)
    for name in ("worker-dispatch.json", "worker-completed.json"):
        path = private / name
        original = path.read_text()
        data = json.loads(original)
        data["request_hash"] = content_hash("another dispatch")
        path.write_text(canonical_json(data))
        with pytest.raises(ContractError, match="different request"):
            completed_worker(private, request)
        path.write_text(original)


def test_worker_dispatch_guard_prevents_restart_even_without_completion(tmp_path):
    request = {"trial_name": "proteus-" + "a" * 32}
    source, output = tmp_path / "request.json", tmp_path / "response.json"
    source.write_text(canonical_json(request))
    script = r'''
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('worker',sys.argv[1])
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
async def run(request,output):
    marker=output.parent/'calls'
    with marker.open('a') as f: f.write('call\n')
    worker.write_json(output,{'request_hash':worker.digest(request)})
worker.run=run
worker.cleanup_owned_resources=lambda trial: {'verified':True,'resources':[],'errors':[]}
sys.argv=['worker','run',sys.argv[2],sys.argv[3]]
worker.main()
'''
    def launch():
        return subprocess.run([sys.executable, "-c", script, str(harbor_worker.__file__), str(source), str(output)],
                              capture_output=True, text=True, timeout=10)
    first = launch()
    assert first.returncode == 0, first.stderr
    assert completed_worker(tmp_path, request)[2]["return_code"] == 0
    assert launch().returncode != 0
    (tmp_path / "worker-completed.json").unlink()
    assert launch().returncode != 0
    assert (tmp_path / "calls").read_text().splitlines() == ["call"]


def test_completed_worker_result_reuses_original_usage_and_private_evidence(tmp_path):
    context, _, private = bundle(tmp_path)
    subject = HarborSubject(HarborSubjectOptions())
    result = subject.reconcile(context)
    assert result.status == Outcome.SUCCEEDED and result.output == 42
    assert result.usage.wall_seconds == 3 and result.backend_evaluation.usage.wall_seconds == 3
    assert result.backend_evaluation.payload["cleanup"]["verified"]
    assert subject.reconcile(context) == result
    assert json.loads((private / "bridge-usage.json").read_text())["elapsed_seconds"] == 9.5


@pytest.mark.parametrize("serialized", [False, True])
def test_source_built_result_decodes_live_and_recovered_receipts(tmp_path, serialized):
    context, request, private = bundle(tmp_path)
    build = BuildReceipt(id="build-1", artifact_hash=context.candidate.artifact.content_hash,
        build_hash=content_hash("build"), base_image=request["image_digest"],
        dockerfile_hash=content_hash("Dockerfile"), image_id=content_hash("built image"),
        status="completed", usage=zero_usage())
    request.update(build_receipt=build, agent_image=build.image_id)
    if serialized:
        request = json.loads(canonical_json(request))
    response = json.loads((private / "response.json").read_text())
    response["request_hash"] = content_hash(request)
    response["loaded_proof"].update(requested_image=build.image_id, image_id=build.image_id,
                                    build_receipt_hash=content_hash(build))
    _, cleanup, usage = completed_worker(private, json.loads((private / "request.json").read_text()))
    subject = HarborSubject(HarborSubjectOptions())
    result = subject._result(context, request, response, cleanup, usage)
    assert result.status == Outcome.SUCCEEDED and result.output == 42
    assert result.usage.wall_seconds == 3 and result.backend_evaluation.usage.wall_seconds == 3
    response["loaded_proof"]["image_id"] = content_hash("another image")
    with pytest.raises(ContractError, match="did not execute the built image"):
        subject._result(context, request, response, cleanup, usage)


@pytest.mark.parametrize("name", ["response.json", "worker-cleanup.json"])
def test_changed_worker_evidence_is_rejected(tmp_path, name):
    context, _, private = bundle(tmp_path)
    data = json.loads((private / name).read_text())
    data["changed"] = True
    (private / name).write_text(json.dumps(data))
    with pytest.raises(ContractError, match="evidence changed"):
        HarborSubject(HarborSubjectOptions()).reconcile(context)
    assert not (private / "cleanup.json").exists()


@pytest.mark.parametrize("changed", ["candidate", "task", "seed", "state", "environment", "root"])
def test_reconciliation_rejects_another_dispatch_identity(tmp_path, changed):
    context, _, _ = bundle(tmp_path)
    if changed == "candidate":
        context = replace(context, candidate=replace(context.candidate, id="other"))
    elif changed == "task":
        context = replace(context, task=replace(context.task, id="other"))
    elif changed == "seed":
        context = replace(context, seed=8)
    elif changed == "state":
        context = replace(context, state=StateValue(payload={"learned": True}))
    elif changed == "environment":
        context = replace(context, execution=replace(context.execution,
            environment=replace(context.execution.environment, resources={"cpus": 2, "memory_mb": 512})))
    else:
        context = replace(context, root=tmp_path / "other")
    with pytest.raises((ContractError, FileNotFoundError)):
        HarborSubject(HarborSubjectOptions()).reconcile(context)


def test_live_worker_lease_blocks_even_a_present_completion_receipt(tmp_path):
    _, request, private = bundle(tmp_path)
    code = "import fcntl,sys; f=open(sys.argv[1],'r+'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()"
    worker = subprocess.Popen([sys.executable, "-c", code, str(private / "worker.lock")],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert worker.stdout.readline().strip() == "ready"
        assert worker.poll() is None
        with pytest.raises(RecoveryRequired, match="still running"):
            completed_worker(private, request)
        assert worker.poll() is None
    finally:
        worker.communicate("", timeout=5)
    assert completed_worker(private, request)[2]["return_code"] == 0


def test_missing_receipt_or_symlink_cannot_claim_completion(tmp_path):
    _, request, private = bundle(tmp_path)
    receipt = private / "worker-completed.json"
    original = receipt.read_bytes()
    receipt.unlink()
    with pytest.raises(FileNotFoundError):
        completed_worker(private, request)
    elsewhere = tmp_path / "receipt.json"
    elsewhere.write_bytes(original)
    receipt.symlink_to(elsewhere)
    with pytest.raises((ContractError, OSError)):
        completed_worker(private, request)


def test_completed_failed_worker_without_response_is_not_redispatched(tmp_path):
    context, request, private = bundle(tmp_path)
    (private / "response.json").unlink()
    path = private / "worker-completed.json"
    receipt = json.loads(path.read_text())
    receipt.update(response_hash=content_hash(None), return_code=1)
    path.write_text(canonical_json(receipt))
    result = HarborSubject(HarborSubjectOptions()).reconcile(context)
    assert result.status == Outcome.INFRA_ERROR and result.usage.wall_seconds == 9.5
    assert result.usage.model_calls is None and result.backend_evaluation is None
    assert completed_worker(private, request)[0] is None


DRIVER = r'''
import os,sys
from pathlib import Path
from dataclasses import replace
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_evaluation import setup,SOURCE,plugin
from proteus.core.contracts import PluginRef
from proteus.core.ledger import Ledger
from proteus.evaluation.runners import PythonSubject
from proteus.evaluation.records import SubjectResult
from proteus.recipes.runner import run_recipe
root=Path(sys.argv[1]); resume=sys.argv[2]!='run'
marker=root/'calls'
code="from pathlib import Path\np=Path("+repr(str(marker))+");p.open('a').write('call\\n')\n"
code+="import json,sys\nr=json.load(sys.stdin);print(json.dumps({'output':42}))\n"
registry,store,_,recipe=setup(root,code=code)
class Recoverable(PythonSubject):
    def run(self,context):
        result=super().run(context)
        (context.lease.local_root.parent/'receipt.json').write_text(result.to_json())
        if not resume and context.task.id=='0': os._exit(86)
        return result
    def reconcile(self,context):
        return SubjectResult.from_json((context.root/'receipt.json').read_text())
definition=registry.get(recipe.subject.runner.plugin)
ref=PluginRef(name='fixture.reconciler',version='1')
registry.register(replace(definition,ref=ref,factory=Recoverable,capabilities=(*definition.capabilities,'subject.reconcile')))
recipe=replace(recipe,subject=replace(recipe.subject,runner=plugin(ref.name)))
lock=registry.make_lock(recipe,[SOURCE])
if sys.argv[2]=='crash-reconcile':
    settle=Ledger.settle
    def interrupted(self,entry_id,**kwargs):
        if self.entry(entry_id)['identity']['metadata']['kind']=='subject': os._exit(87)
        return settle(self,entry_id,**kwargs)
    Ledger.settle=interrupted
result=run_recipe(recipe,registry=registry,lock=lock,root=root/'run',resume=resume,artifact_source=store.root)
print(result.to_json())
'''


def dispatch(root, mode):
    return subprocess.run([sys.executable, "-c", DRIVER, str(root), mode], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("interrupt_reconciliation", [False, True])
def test_service_reconciles_receipt_after_process_exit_without_extra_work(tmp_path, interrupt_reconciliation):
    first = dispatch(tmp_path, "run")
    assert first.returncode == 86, first.stderr
    book = run_ledger(tmp_path / "run")
    if interrupt_reconciliation:
        second = dispatch(tmp_path, "crash-reconcile")
        assert second.returncode == 87, second.stderr
        assert book.balance()["active_slots"] == 1
    recovered = dispatch(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    result = MethodResult.from_json(recovered.stdout)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert (tmp_path / "calls").read_text().count("call") == 2
    assert len(book.entries()) == 5 and book.balance()["active_slots"] == 0
    assert len(list((tmp_path / "run/evaluations").glob("*/private/reconciled/*.json"))) == 1
    before = book.events()
    assert dispatch(tmp_path, "resume").returncode == 0
    assert book.events() == before and (tmp_path / "calls").read_text().count("call") == 2


@pytest.mark.parametrize("fault", ["missing", "artifact", "runtime"])
def test_bad_reconciliation_evidence_keeps_reservations_and_allows_later_repair(tmp_path, fault):
    first = dispatch(tmp_path, "run")
    assert first.returncode == 86, first.stderr
    receipt = next((tmp_path / "run/evaluations").glob("*/trials/*/receipt.json"))
    original = receipt.read_text()
    if fault == "missing":
        receipt.unlink()
    else:
        data = json.loads(original)
        if fault == "artifact":
            data["loaded_artifact_hash"] = content_hash("different")
        else:
            data["runtime"]["environment_hash"] = content_hash("different")
        receipt.write_text(json.dumps(data))
    book = run_ledger(tmp_path / "run")
    before = book.events()
    for _ in range(2):
        resumed = dispatch(tmp_path, "resume")
        assert resumed.returncode == 0, resumed.stderr
        result = MethodResult.from_json(resumed.stdout)
        assert result.status == OperationStatus.FAILED and "RecoveryRequired" in result.diagnostic
        assert book.events() == before and book.balance()["active_slots"] == 1
    assert (tmp_path / "calls").read_text().count("call") == 1
    receipt.write_text(original)
    repaired = dispatch(tmp_path, "resume")
    assert repaired.returncode == 0, repaired.stderr
    assert MethodResult.from_json(repaired.stdout).status == OperationStatus.COMPLETED
    assert (tmp_path / "calls").read_text().count("call") == 2
