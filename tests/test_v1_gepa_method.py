"""Full pinned upstream loop, real service records and hard-process-exit recovery."""

import importlib.util
import json
import os
import random
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proteus.artifacts.codecs import TextComponents
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import BudgetLimits, OperationStatus, Outcome, PluginRef, RuntimeIdentity
from proteus.core.methods import ActorResult
from proteus.core.search import PolicyResult
from proteus.core.serialization import ContractError, content_hash
from proteus.evaluation.records import Grade, SubjectResult, zero_usage
from proteus.evaluation.tasks import TaskBatch
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.gepa_prompt import prepare, register as register_fixture
from proteus.methods.gepa.method import Bridge, GepaOptions, Pool, evaluation_bound, register
from proteus.methods.gepa.reference import load_reference
from proteus.methods.gepa.state import pack, unpack
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import load_recipe
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE, register as add_plugin
from test_v1_ledger import run_ledger


pytestmark = pytest.mark.skipif(importlib.util.find_spec("gepa") is None and not os.environ.get("PROTEUS_REQUIRE_GEPA"),
                               reason="optional pinned GEPA; required in CI")


def setup(root, *, threshold=10, proposal="Return 1.", acceptance="strict_improvement"):
    path = prepare(root)
    registry = builtin_registry()
    register_fixture(registry)
    register(registry)
    recipe = load_recipe(path)
    options = GepaOptions.from_dict(recipe.method.options)
    options = replace(options, max_metric_calls=threshold, acceptance=acceptance)
    recipe = replace(recipe, method=plugin("proteus.gepa", **options.to_dict()), actors={**recipe.actors,
        "reflection": replace(recipe.actors["reflection"], runtime=plugin("example.gepa_offline", role="reflection", proposal=proposal))},
        budget=BudgetLimits(max_evaluations=evaluation_bound(options, 2) + 2, max_model_calls=32))
    return registry, recipe


def run(root, registry, recipe, **kwargs):
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root / "run", artifact_source=root / "artifact-store", **kwargs)
    records = json.loads(sorted((root / "run").glob("execution-*.json"))[-1].read_text())
    return result, records


def values(records, kind):
    return [item["value"] for item in records["values"] if item["ref"]["kind"] == kind]


def invocation_count(root):
    return sum(len(path.read_text().splitlines()) for path in root.rglob("fixture-invocations.txt"))


@pytest.mark.parametrize("threshold,proposal,acceptance", [
    (2, "Return 1.", "strict_improvement"), (3, "Return 1.", "strict_improvement"),
    (10, "Return 1.", "strict_improvement"), (18, "Return 1.", "strict_improvement"),
    (10, "Return 0.", "strict_improvement"), (18, "Return 0.", "improvement_or_equal"),
])
def test_complete_loop_matches_unwrapped_upstream(tmp_path, threshold, proposal, acceptance):
    registry, recipe = setup(tmp_path / "study", threshold=threshold, proposal=proposal, acceptance=acceptance)
    result, records = run(tmp_path / "study", registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    summary = values(records, "proteus.gepa.summary@1")[0]
    train = TaskBatch.from_dict(result.state.state.payload["train"])
    validation = TaskBatch.from_dict(result.state.state.payload["validation"])
    expected_calls, prompts = [], []
    # A separate adapter and the unmodified default strategies exercise the
    # algorithm independently of Proteus's phase/identity/checkpoint bridge.
    class Adapter:
        propose_new_texts = None
        def evaluate(self, batch, candidate, capture_traces=False):
            from gepa.core.adapter import EvaluationBatch
            output = "1" if candidate["system.txt"] == "Return 1." else "0"
            score = float(output == "1")
            expected_calls.append((dict(candidate), [task.id for task in batch], [score] * len(batch)))
            trajectories = [{"Inputs": dict(task.input), "Generated Outputs": output,
                "Feedback": {"score": score, "feedback": [{"correct": bool(score), "reference": "1"}]},
                "Trace": [{"schema_version": 1, "events": [{"kind": "offline-fixture", "role": "subject"}]}]} for task in batch]
            return EvaluationBatch(outputs=[output] * len(batch), scores=[score] * len(batch),
                trajectories=trajectories if capture_traces else None, num_metric_calls=len(batch))
        def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
            return {key: eval_batch.trajectories for key in components_to_update}
    def reflect(prompt):
        prompts.append(prompt)
        return "```\n" + proposal + "\n```"
    gepa = load_reference()
    expected = gepa.optimize(seed_candidate={"system.txt": "Return 0."}, trainset=Pool(train), valset=Pool(validation),
        adapter=Adapter(), reflection_lm=reflect, reflection_minibatch_size=3, max_metric_calls=threshold,
        acceptance_criterion=acceptance, track_best_outputs=False, display_progress_bar=False, seed=recipe.seed)
    store = ArtifactStore(tmp_path / "study/run/artifacts", registry)
    candidates = {item["id"]: item for item in records["candidates"]}
    from proteus.core.contracts import ArtifactRef
    texts = {key: dict(store.get(ArtifactRef.from_dict(item["artifact"])).components) for key, item in candidates.items()}
    actual_calls = []
    for evaluation in records["evaluations"]:
        actual_calls.append((texts[evaluation["candidate_id"]], [row["task_id"] for row in evaluation["rows"]],
                             [row["score"] for row in evaluation["rows"]]))
    assert actual_calls == expected_calls
    assert [call["request"]["prompt"] for call in records["actor_calls"] if call["role"] == "reflection"] == prompts
    assert [texts[key] for key in summary["accepted_ids"]] == expected.candidates
    assert [candidates[key]["parent_ids"] for key in summary["accepted_ids"]] == [
        [summary["accepted_ids"][parent] for parent in parents if parent is not None] for parents in expected.parents]
    assert [{row["task_id"]: row["score"] for row in batch["rows"]} for batch in records["evaluations"]
            if batch["candidate_id"] in summary["accepted_ids"]
            and batch["protocol_hash"] == result.state.state.payload["bridge"]["validation_protocol"]] == expected.val_subscores
    assert texts[summary["upstream_best_id"]] == expected.best_candidate
    assert summary["metric_calls"] == sum(len(item[1]) for item in expected_calls)
    assert summary["reflection_calls"] == len(prompts)
    assert invocation_count(tmp_path / "study/run") == summary["metric_calls"] + len(prompts) + 2
    assert run_ledger(tmp_path / "study/run").balance()["charged"]["evaluations"] == summary["metric_calls"] + 2
    assert all(row["split"] != "test" for item in values(records, "proteus.archive@1")
               for evaluation in item["evaluations"] for row in evaluation["cohort"]["tasks"])
    if proposal == "Return 0." and threshold > 2:
        assert len(set(item["artifact"]["content_hash"] for item in candidates.values())) == 1
        assert len(candidates) > 1  # identical artifacts are distinct proposals/lineage
        assert all(item["disposition"] == ("accepted" if acceptance == "improvement_or_equal" else "rejected")
                   for item in summary["decisions"])
    before = run_ledger(tmp_path / "study/run").events()
    again, _ = run(tmp_path / "study", registry, recipe, resume=True)
    assert again == result and run_ledger(tmp_path / "study/run").events() == before


def test_multiple_components_follow_upstream_round_robin(tmp_path):
    registry, recipe = setup(tmp_path / "study", threshold=18, acceptance="improvement_or_equal")
    store = ArtifactStore(tmp_path / "study/artifact-store", registry)
    initial = store.put(PluginRef(name="proteus.text_components", version="1"),
                        TextComponents(components={"a.txt": "Return 0.", "b.txt": "Return 0."}))
    class Subject:
        def run(self, context):
            content = [path.read_text() for path in sorted(context.lease.local_root.glob("*.txt"))]
            return SubjectResult(loaded_artifact_hash=context.candidate.artifact.content_hash,
                runtime=RuntimeIdentity(runtime=context.subject.runner.plugin,
                    environment_hash=content_hash(context.execution.environment)), status=Outcome.SUCCEEDED,
                output="1" if content == ["Return 1."] * 2 else "0", usage=zero_usage())
    add_plugin(registry, "fixture.components", "subject", Subject, capabilities=("subject.model_free",))
    recipe = replace(recipe, subject=replace(recipe.subject, runner=plugin("fixture.components"), actor=None, initial_artifact=initial))
    result, records = run(tmp_path / "study", registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    summary = values(records, "proteus.gepa.summary@1")[0]
    assert len(summary["accepted_ids"]) == 3
    final_store = ArtifactStore(tmp_path / "study/run/artifacts", registry)
    from proteus.core.contracts import ArtifactRef
    texts = [dict(final_store.get(ArtifactRef.from_dict(item["artifact"])).components) for item in records["candidates"]]
    assert texts == [{"a.txt": "Return 0.", "b.txt": "Return 0."},
                     {"a.txt": "Return 1.", "b.txt": "Return 0."},
                     {"a.txt": "Return 1.", "b.txt": "Return 1."}]


def test_missing_scores_and_reflection_failures_stop_without_imputation_or_retry(tmp_path):
    registry, recipe = setup(tmp_path / "missing")
    class Missing:
        def grade(self, *args):
            return Grade(status=Outcome.INFRA_ERROR, score=None, usage=zero_usage())
    add_plugin(registry, "fixture.missing", "evaluator", Missing, capabilities=("evaluator.deterministic",))
    evaluations = tuple(replace(item, evaluator=plugin("fixture.missing")) if item.id == "validation" else item
                        for item in recipe.evaluations)
    result, records = run(tmp_path / "missing", registry, replace(recipe, evaluations=evaluations))
    assert result.status == OperationStatus.FAILED and "cannot impute" in result.diagnostic
    assert len(records["rollouts"]) == 2 and not values(records, "proteus.gepa.proposal@1")
    assert all(row["score"] is None for row in records["evaluations"][0]["rows"])
    registry, recipe = setup(tmp_path / "reflection")
    class Failed:
        model_call_bound = 0
        def execute(self, spec, request, workspace):
            with (workspace / "fixture-invocations.txt").open("a") as stream:
                stream.write("failed\n")
            return ActorResult(text="", runtime=RuntimeIdentity(runtime=spec.runtime.plugin), usage=zero_usage(),
                status=OperationStatus.FAILED, diagnostic="fixture reflection unavailable")
    add_plugin(registry, "fixture.failed", "actor", Failed, capabilities=("session.fresh", "actor.execute", "calls.bounded"))
    recipe = replace(recipe, actors={**recipe.actors, "reflection": replace(recipe.actors["reflection"], runtime=plugin("fixture.failed"))})
    result, records = run(tmp_path / "reflection", registry, recipe)
    assert result.status == OperationStatus.FAILED and "fixture reflection unavailable" in result.diagnostic
    assert len(records["rollouts"]) == 5 and invocation_count(tmp_path / "reflection/run") == 6
    assert len([item for item in records["actor_calls"] if item["role"] == "reflection"]) == 1


def test_reference_configuration_rejects_unconsumed_or_adapted_inputs(tmp_path):
    registry, recipe = setup(tmp_path / "study")
    empty = ArtifactStore(tmp_path / "study/artifact-store", registry).put(
        PluginRef(name="proteus.text_components", version="1"), TextComponents(components={}))
    for changed in (replace(recipe, budget=BudgetLimits(max_evaluations=10)),
                    replace(recipe, search={**recipe.search, "parent": plugin("proteus.uniform_parent")}),
                    replace(recipe, reference=replace(recipe.reference, profile="native")),
                    replace(recipe, subject=replace(recipe.subject, initial_artifact=empty)),
                    replace(recipe, stages=()),
                    replace(recipe, evaluations=tuple(replace(item, tasks=recipe.evaluations[0].tasks)
                        if item.id == "validation" else item for item in recipe.evaluations)),
                    replace(recipe, evaluations=tuple(replace(item, tasks=recipe.evaluations[0].tasks)
                        if item.id == "test" else item for item in recipe.evaluations))):
        with pytest.raises(ContractError):
            registry.resolve(changed)
    assert not (tmp_path / "study/run").exists()


@pytest.mark.parametrize("name", ["summary", "candidate", "archive"])
def test_test_evaluation_cannot_overwrite_method_output(tmp_path, name):
    registry, recipe = setup(tmp_path / "study")
    options = replace(GepaOptions.from_dict(recipe.method.options), test_evaluations=(name,))
    recipe = replace(recipe, method=plugin("proteus.gepa", **options.to_dict()),
        evaluations=tuple(replace(item, id=name) if item.id == "test" else item for item in recipe.evaluations))
    with pytest.raises(ContractError, match="reserved method outputs"):
        registry.resolve(recipe)


@pytest.mark.parametrize("keep_latest", [False, True])
def test_adapted_archive_policy_controls_the_parent_population(tmp_path, keep_latest):
    registry, recipe = setup(tmp_path / "study", threshold=18, proposal="Return 0.", acceptance="improvement_or_equal")
    class Retention:
        def retain(self, view, rng):
            selected = view.vectors[-1 if keep_latest else 0].candidate_id
            return PolicyResult(selected=(selected,), reasons={item.candidate_id: "fixture retention" for item in view.vectors})
    add_plugin(registry, "fixture.retention", "policy", Retention, capabilities=("search.archive",))
    recipe = replace(recipe, reference=replace(recipe.reference, profile="extended", deviations=("custom retention and parent",)),
        search={**recipe.search, "frontier": plugin("fixture.retention"), "parent": plugin("proteus.uniform_parent")})
    result, records = run(tmp_path / "study", registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    accepted = values(records, "proteus.gepa.summary@1")[0]["accepted_ids"]
    assert len(accepted) == 3
    candidates = {item["id"]: item for item in records["candidates"]}
    assert candidates[accepted[2]]["parent_ids"] == [accepted[1 if keep_latest else 0]]
    decisions = values(records, "proteus.search.decision@1")
    retained = [item["result"]["selected"] for item in decisions if item["role"] == "archive"]
    parents = [item["result"]["selected"] for item in decisions if item["role"] == "parent"]
    assert retained == parents
    before = invocation_count(tmp_path / "study/run")
    again, _ = run(tmp_path / "study", registry, recipe, resume=True)
    assert again == result and invocation_count(tmp_path / "study/run") == before


@pytest.mark.parametrize("field", ["lineage", "score"])
def test_upstream_state_must_match_committed_lineage_and_scores(tmp_path, monkeypatch, field):
    registry, recipe = setup(tmp_path / "study")
    original = Bridge.on_iteration_start
    def corrupt(self, event):
        state = event["state"]
        if field == "lineage":
            state.parent_program_for_candidate[0] = [0]
        else:
            state.prog_candidate_val_subscores[0]["validation-0"] = 17.0
        return original(self, event)
    monkeypatch.setattr(Bridge, "on_iteration_start", corrupt)
    result, records = run(tmp_path / "study", registry, recipe)
    assert result.status == OperationStatus.FAILED and field in result.diagnostic
    assert len(records["rollouts"]) == 2 and invocation_count(tmp_path / "study/run") == 2


def test_checkpoint_tree_codec_preserves_order_types_and_rejects_objects():
    original = {"b": [(1, 2), {0, 3, 11}], "a": {3: "three", 1: "one"}, "rng": random.Random(3).getstate()}
    assert unpack(json.loads(json.dumps(pack(original)))) == original
    assert list(unpack(pack(original))) == ["b", "a", "rng"]
    with pytest.raises(ValueError):
        pack(object())
    with pytest.raises(ContractError):
        unpack(["pickle", "untrusted"])


def test_chat_transport_roles_usage_and_resume_share_the_same_method(tmp_path, monkeypatch):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            prompt = body["messages"][-1]["content"]
            answer = "```\nReturn 1.\n```" if body["model"] == "fixture-reflector" else (
                "1" if prompt.startswith("Return 1.\n\n") else "0")
            payload = json.dumps({"model": body["model"], "choices": [{"message": {"content": answer}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PROTEUS_GEPA_TEST_KEY", "fixture-key")
    try:
        registry, recipe = setup(tmp_path / "study")
        actors = {role: replace(spec, runtime=plugin("proteus.chat", base_url=f"http://127.0.0.1:{server.server_port}",
            key_env="PROTEUS_GEPA_TEST_KEY"), model="fixture-reflector" if role == "reflection" else "fixture-subject")
                  for role, spec in recipe.actors.items()}
        recipe = replace(recipe, actors=actors, reference=replace(recipe.reference,
            deviations=("Local HTTP provider fixture; no real model or benchmark result.",)))
        result, records = run(tmp_path / "study", registry, recipe)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        assert len(requests) == 13
        assert sum(item["model"] == "fixture-reflector" for item in requests) == 1
        charged = run_ledger(tmp_path / "study/run").balance()["charged"]
        assert charged["model_calls"] == 13 and charged["evaluations"] == 12
        assert all(item["result"]["usage"]["input_tokens"] == 2 for item in records["actor_calls"])
        assert run(tmp_path / "study", registry, recipe, resume=True)[0] == result
        assert len(requests) == 13
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


DRIVER = r'''
import os,sys,json
from pathlib import Path
from dataclasses import replace
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_gepa_method import setup,run,values,invocation_count
from proteus.recipes.config import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.examples.gepa_prompt import register as register_fixture
from proteus.methods.gepa.method import register,Bridge
from proteus.core.recovery import Journal
import proteus.core.recovery as recovery
import proteus.methods.gepa.method as method
from gepa.core.state import GEPAState
root=Path(sys.argv[1]);boundary=sys.argv[2];mode=sys.argv[3]
if not (root/'recipe-final.json').exists():
    registry,recipe=setup(root,threshold=18)
    (root/'recipe-final.json').write_text(recipe.to_json())
else:
    registry=builtin_registry();register_fixture(registry);register(registry)
    recipe=load_recipe(root/'recipe-final.json')
if mode=='run':
    seen={}
    def die(kind):
        seen[kind]=seen.get(kind,0)+1
        if boundary==kind+':'+str(seen[kind]):os._exit(86)
    original=Journal.call
    def call(self,kind,*args,**kwargs):
        result=original(self,kind,*args,**kwargs);die(kind);return result
    Journal.call=call
    original_write=recovery._write
    def write(path,value):
        original_write(path,value)
        if path.name.endswith('.request.json') and value.get('kind')=='checkpoint':die('checkpoint.request')
    recovery._write=write
    original_seal=method.seal
    def seal(*args):
        result=original_seal(*args);die('sealed');return result
    method.seal=seal
    original_save=GEPAState.save
    def save(self,*args,**kwargs):
        result=original_save(self,*args,**kwargs);die('engine.save');return result
    GEPAState.save=save
result,records=run(root,registry,recipe,resume=mode!='run')
print(json.dumps({'status':result.status.value,'diagnostic':result.diagnostic,
    'summary':values(records,'proteus.gepa.summary@1'),'rollouts':[(x['candidate_id'],x['task_id'],x['output']) for x in records['rollouts']],
    'calls':invocation_count(root/'run')}))
'''


def invoke(root, boundary, mode):
    return subprocess.run([sys.executable, "-c", DRIVER, str(root), boundary, mode], capture_output=True, text=True, timeout=50)


@pytest.mark.parametrize("boundary", ["candidate:1", "evaluation.step:1", "engine.save:1", "sealed:1",
    "checkpoint.request:1", "checkpoint:1", "search.parent:1", "stage:1", "candidate:2",
    "evaluation.step:6", "checkpoint:2", "checkpoint.request:2", "checkpoint:5", "method.result:1"])
def test_hard_exit_replays_full_engine_without_duplicate_calls(tmp_path, boundary):
    root = tmp_path / "study"
    stopped = invoke(root, boundary, "run")
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    continued = invoke(root, boundary, "resume")
    assert continued.returncode == 0, continued.stdout + continued.stderr
    output = json.loads(continued.stdout)
    assert output["status"] == "completed", output
    assert output["calls"] == 22  # 19 optimized + 2 hidden test trials + 1 reflection
    assert len(output["rollouts"]) == 21
    assert output["summary"][0]["metric_calls"] == 19 and output["summary"][0]["reflection_calls"] == 1
    book = run_ledger(root / "run")
    assert book.balance()["charged"]["evaluations"] == 21 and book.balance()["active_slots"] == 0
    before = book.events()
    again = invoke(root, boundary, "resume")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout) == output and book.events() == before


def test_modified_engine_checkpoint_fails_before_any_new_invocations(tmp_path):
    root = tmp_path / "study"
    assert invoke(root, "checkpoint:1", "run").returncode == 86
    calls = invocation_count(root / "run")
    path = next((root / "run/private/gepa/checkpoints").glob("*.json"))
    path.chmod(0o644)
    data = json.loads(path.read_text())
    data["identity"]["revision"] = "0" * 40
    path.write_text(json.dumps(data))
    result = invoke(root, "checkpoint:1", "resume")
    assert result.returncode == 0, result.stderr
    assert "integrity" in json.loads(result.stdout)["diagnostic"]
    assert invocation_count(root / "run") == calls
