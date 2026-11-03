"""ACE full-loop conformance, feedback boundaries, and durable role recovery."""

import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import ArtifactRef, BudgetLimits, OperationStatus
from proteus.core.serialization import ContractError
from proteus.examples.ace_playbook import prepare, register as register_fixture, response
from proteus.methods.ace.method import AceOptions, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.runner import run_recipe
from test_v1_ledger import run_ledger
from test_v1_evaluation import SOURCE, plugin


def setup(root, *, mode="offline", behavior="learn", invalid_curation=False, **options):
    path = prepare(root, mode=mode)
    registry = builtin_registry()
    register(registry)
    register_fixture(registry)
    recipe = load_recipe(path)
    method = replace(AceOptions.from_dict(recipe.method.options), **options)
    recipe = replace(recipe, method=plugin("proteus.ace", **method.to_dict()),
        actors={role: replace(actor, runtime=plugin("example.ace_offline", role=role,
            behavior=behavior, invalid_curation=invalid_curation)) for role, actor in recipe.actors.items()},
        budget=BudgetLimits(max_evaluations=120, max_model_calls=160))
    return registry, recipe


def run(root, registry, recipe, **kwargs):
    result = run_recipe(recipe, registry=registry, lock=registry.make_lock(recipe, [SOURCE]),
        root=root / "run", artifact_source=root / "artifact-store", **kwargs)
    records = json.loads(sorted((root / "run").glob("execution-*.json"))[-1].read_text())
    return result, records


def values(records, kind):
    return [item["value"] for item in records["values"] if item["ref"]["kind"] == kind]


@pytest.mark.parametrize("mode,behavior,options,invalid", [
    ("offline", "learn", {}, False), ("online", "learn", {}, False),
    ("offline", "correct", {"max_num_rounds": 0}, False),
    ("offline", "wrong", {"max_num_rounds": 2}, False),
    ("online", "wrong", {"max_num_rounds": 0, "curator_frequency": 2}, False),
    ("offline", "learn", {"num_epochs": 2, "curator_frequency": 2, "json_mode": True}, False),
    ("offline", "learn", {"eval_steps": 100, "no_ground_truth": True}, False),
    ("online", "learn", {"json_mode": True, "no_ground_truth": True}, True),
])
def test_full_loop_matches_actual_upstream(tmp_path, mode, behavior, options, invalid):
    source = os.environ.get("PROTEUS_ACE_SOURCE")
    if not source:
        pytest.skip("pinned ACE source required in CI")
    root = tmp_path / "study"
    registry, recipe = setup(root, mode=mode, behavior=behavior, invalid_curation=invalid, **options)
    result, records = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    from ace_reference_method import Processor, load_engine
    engine, expected_calls = load_engine(source, lambda role, prompt: response(role, prompt,
        behavior=behavior, invalid_curation=invalid))
    dataset = json.loads((root / "tasks.json").read_text())
    samples = {split: [{**row["task"]["input"], "target": row["reference"]} for row in dataset["records"]
                       if row["task"]["split"] == split] for split in ("train", "validation", "test")}
    method = AceOptions.from_dict(recipe.method.options)
    upstream_result = engine.run(mode=mode, train_samples=samples["train"], val_samples=samples["validation"],
        test_samples=samples["test"] if mode == "offline" else samples["train"], data_processor=Processor(),
        config={**method.to_dict(), "playbook_token_budget": method.token_budget,
                "test_workers": 1, "save_dir": str(tmp_path / "reference")})
    actual_calls = [(call["role"], call["request"]["prompt"]) for call in records["actor_calls"]]
    assert actual_calls == expected_calls
    summary = values(records, "proteus.ace.summary@1")[0]
    store = ArtifactStore(root / "run/artifacts", registry)
    candidates = {candidate["id"]: store.get(ArtifactRef.from_dict(candidate["artifact"])) for candidate in records["candidates"]}
    assert candidates[summary["final_id"]].text == engine.playbook
    assert candidates[summary["final_id"]].next_id == engine.next_global_id
    assert candidates[summary["selected_id"]].text == (engine.best_playbook if mode == "offline" else engine.playbook)
    if mode == "offline":
        assert summary["best_validation_accuracy"] == upstream_result["training_results"]["best_validation_accuracy"]
        assert len(summary["validations"]) == method.num_epochs * (3 // method.eval_steps)
        assert all("hidden-reference" not in prompt for _, prompt in actual_calls)
    else:
        assert len(summary["windows"]) == 2
        refs = {item["ref"]["id"]: item["value"] for item in records["values"]}
        assert all(not refs[ref["id"]]["batches"] for ref in summary["windows"])
        assert all(len(set(refs[ref["id"]]["candidate_ids"])) == 1 for ref in summary["windows"])
    assert sum(len(path.read_text().splitlines()) for path in (root / "run").rglob("fixture-invocations.txt")) == len(expected_calls)
    again, _ = run(root, registry, recipe, resume=True)
    assert again == result
    assert sum(len(path.read_text().splitlines()) for path in (root / "run").rglob("fixture-invocations.txt")) == len(expected_calls)


def test_preflight_rejects_unconsumed_protocols_and_false_reference_claim(tmp_path):
    registry, recipe = setup(tmp_path / "study")
    options = AceOptions.from_dict(recipe.method.options)
    for changed in (
        replace(recipe, reference=replace(recipe.reference, profile="reference")),
        replace(recipe, reference=replace(recipe.reference, deviations=())),
        replace(recipe, method=plugin("proteus.ace", **replace(options, validation=None).to_dict())),
        replace(recipe, method=plugin("proteus.ace", **replace(options, stream="test").to_dict())),
        replace(recipe, method=plugin("proteus.ace", **replace(options, train="unknown").to_dict())),
        replace(recipe, stages=recipe.stages[:1]),
        replace(recipe, evaluations=tuple(replace(item, tasks=recipe.evaluations[0].tasks)
            if item.id == "validation" else item for item in recipe.evaluations)),
    ):
        with pytest.raises(ContractError):
            registry.resolve(changed)
    registry, recipe = setup(tmp_path / "online", mode="online")
    options = AceOptions.from_dict(recipe.method.options)
    with pytest.raises(ContractError):
        registry.resolve(replace(recipe, method=plugin("proteus.ace", **replace(options, num_epochs=2).to_dict())))


@pytest.mark.parametrize("content", [float("nan"), float("inf"), {"invalid": "content"}])
def test_invalid_curator_content_keeps_raw_response_and_serializable_rejection(content):
    from proteus.core.serialization import content_hash
    from proteus.methods.ace.playbook import Playbook
    from proteus.methods.ace.roles import Curation, CurationInput, CurateStage
    raw = json.dumps({"reasoning": "fixture", "operations": [{"type": "ADD", "section": "OTHERS", "content": content}]})
    calls = []

    def actor(prompt):
        calls.append(prompt)
        return SimpleNamespace(text=raw, status=OperationStatus.COMPLETED, diagnostic="")

    request = CurationInput(playbook=Playbook.from_text(), artifact_hash=content_hash("fixture"),
        reflection="fixture", question_context="", step=1, total_samples=1)
    output = CurateStage().run(SimpleNamespace(actor=actor), {"request": request})["curation"]
    assert not output.accepted and output.response == raw and output.diagnostic
    assert Curation.from_json(output.to_json()) == output and len(calls) == 1


@pytest.mark.parametrize("failure", [None, "reflector", "generator"])
def test_managed_http_roles_count_calls_and_stop_on_infrastructure_failure(tmp_path, monkeypatch, failure):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            role = payload["model"]
            if role == failure:
                self.send_response(503)
                self.end_headers()
                return
            answer = response(role, payload["messages"][0]["content"])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"model": role, "choices": [{"message": {"content": answer}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3}}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PROTEUS_ACE_TEST_KEY", "fixture-key")
    root = tmp_path / "study"
    registry, recipe = setup(root, json_mode=True)
    recipe = replace(recipe, actors={role: replace(actor, model=role, model_settings={
        "temperature": 0.0, "max_completion_tokens": 4096, "response_format": {"type": "json_object"}},
        runtime=plugin("proteus.chat", base_url=f"http://127.0.0.1:{server.server_port}/v1", key_env="PROTEUS_ACE_TEST_KEY"))
        for role, actor in recipe.actors.items()})
    try:
        result, records = run(root, registry, recipe)
        if failure:
            assert result.status == OperationStatus.FAILED
            assert len([item for item in requests if item["model"] == failure]) == 1
            assert not values(records, "proteus.ace.summary@1")
        else:
            assert result.status == OperationStatus.COMPLETED, result.diagnostic
            assert len(requests) == 22
            assert all(len(item["messages"]) == 1 and item["messages"][0]["role"] == "user" for item in requests)
            assert sum(call["result"]["usage"]["model_calls"] for call in records["actor_calls"]) == 22
            assert run_ledger(root / "run").balance()["charged"]["model_calls"] == 22
            assert run(root, registry, recipe, resume=True)[0] == result
            assert len(requests) == 22
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


DRIVER = r'''
import json,os,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_ace_method import setup,run,values
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.examples.ace_playbook import register as register_fixture
from proteus.methods.ace.method import register
from proteus.core.recovery import Journal
from proteus.core.methods import RunServices
import proteus.core.recovery as recovery
root=Path(sys.argv[1]);mode=sys.argv[2];boundary=sys.argv[3];resume=sys.argv[4]=='resume'
if not (root/'recipe-final.json').exists():
    registry,recipe=setup(root,mode=mode)
    (root/'recipe-final.json').write_text(recipe.to_json())
else:
    registry=builtin_registry();register(registry);register_fixture(registry)
    recipe=load_recipe(root/'recipe-final.json')
if not resume:
    seen={}
    def die(kind):
        seen[kind]=seen.get(kind,0)+1
        if boundary==kind+':'+str(seen[kind]):os._exit(86)
    original=Journal.call
    def call(self,kind,*args,**kwargs):
        result=original(self,kind,*args,**kwargs);die(kind);return result
    Journal.call=call
    original_actor=RunServices._actor
    def actor(self,spec,*args,**kwargs):
        result=original_actor(self,spec,*args,**kwargs)
        if spec.role in ('reflector','curator'):die('actor.'+spec.role)
        return result
    RunServices._actor=actor
    original_write=recovery._write
    def write(path,value):
        original_write(path,value)
        if path.name.endswith('.request.json') and value.get('kind')=='checkpoint':die('checkpoint.request')
    recovery._write=write
result,records=run(root,registry,recipe,resume=resume)
print(json.dumps({'status':result.status.value,'diagnostic':result.diagnostic,
    'summary':values(records,'proteus.ace.summary@1'), 'candidates':records['candidates'],
    'calls':sum(len(path.read_text().splitlines()) for path in (root/'run').rglob('fixture-invocations.txt'))}))
'''


@pytest.mark.parametrize("mode,boundary", [
    ("offline", "evaluation.step:1"), ("offline", "actor.reflector:1"),
    ("offline", "actor.curator:1"), ("offline", "candidate:3"),
    ("offline", "evaluation.step:5"), ("offline", "checkpoint.request:3"),
    ("offline", "checkpoint:3"), ("offline", "method.result:1"),
    ("online", "checkpoint:2"), ("online", "checkpoint:5"),
])
def test_hard_exit_preserves_loop_and_does_not_repeat_roles(tmp_path, mode, boundary):
    root = tmp_path / "study"

    def invoke(resume):
        return subprocess.run([sys.executable, "-c", DRIVER, str(root), mode, boundary,
            "resume" if resume else "run"], capture_output=True, text=True, timeout=50)

    stopped = invoke(False)
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    continued = invoke(True)
    assert continued.returncode == 0, continued.stdout + continued.stderr
    result = json.loads(continued.stdout)
    assert result["status"] == "completed", result
    assert result["calls"] == (22 if mode == "offline" else 19)
    assert len(result["candidates"]) == 7
    ledger = run_ledger(root / "run")
    assert ledger.balance()["charged"]["evaluations"] == (16 if mode == "offline" else 13)
    assert ledger.balance()["active_slots"] == 0
    events = ledger.events()
    again = invoke(True)
    assert again.returncode == 0, again.stdout + again.stderr
    assert json.loads(again.stdout) == result and ledger.events() == events
