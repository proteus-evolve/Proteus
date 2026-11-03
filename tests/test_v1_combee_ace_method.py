"""Complete Combee-ACE sample programs against actual pinned source control flow."""

import __future__
import ast
from collections import Counter
from dataclasses import replace
import hashlib
import json
import os
import random
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MethodType

import pytest

from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import ArtifactRef, BudgetLimits, OperationStatus, PluginRef
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.serialization import content_hash
from proteus.core.workflows import WorkflowJob
from proteus.examples.ace_playbook import response
from proteus.methods.ace.batch import SampleInput, SampleOptions, register_sample
from proteus.methods.ace.playbook import CODEC, Playbook
from proteus.recipes.config import WorkflowSpec
from test_v1_ace_method import setup as ace_setup, run
from test_v1_evaluation import plugin
from test_v1_ledger import run_ledger


PLAYBOOK = "## OTHERS\n[misc-00001] helpful=0 harmful=0 :: Original rule."


class SampleStudy:
    def run(self, services, restored_state):
        initial = services.initial_candidate()
        jobs = tuple(
            WorkflowJob(
                key=task.id,
                workflow="sample",
                inputs={
                    "sample": services.values.publish(
                        "proteus.combee.ace_sample_input@1",
                        SampleInput(candidate=initial, task=task, epoch=1, step=index + 1),
                    )
                },
            )
            for index, task in enumerate(services.task_pool("train").tasks)
        )
        mapped = services.map_workflows(jobs, operation_id="samples", max_workers=3)
        return MethodResult(
            status=OperationStatus.COMPLETED if mapped.succeeded else OperationStatus.FAILED,
            outputs={"samples": services.values.publish("proteus.workflow_map@1", mapped)},
            diagnostic=";".join(row.diagnostic for row in mapped.results if row.diagnostic),
        )


def setup(root, *, behavior="learn", rounds=2, no_ground_truth=False, json_mode=False):
    registry, recipe = ace_setup(root, behavior=behavior)
    register_sample(registry)
    registry.register(
        Plugin(
            ref=PluginRef(name="fixture.combee_samples", version="1"),
            kind="method",
            options=EmptyOptions,
            factory=lambda options: SampleStudy(),
            source="proteus",
            implementation_hash=content_hash("sample-study"),
            capabilities=("run.journal", "run.resume"),
            state_schema=EmptyOptions,
        )
    )
    initial = ArtifactStore(root / "artifact-store", registry).put(
        CODEC, Playbook.from_text(PLAYBOOK)
    )
    options = SampleOptions(
        train="train",
        reflection_stage="reflect",
        max_num_rounds=rounds,
        no_ground_truth=no_ground_truth,
        json_mode=json_mode,
    )
    recipe = replace(
        recipe,
        method=plugin("fixture.combee_samples"),
        subject=replace(recipe.subject, initial_artifact=initial),
        workflows=(
            WorkflowSpec(
                id="sample",
                operator=plugin("proteus.combee_ace_sample", **options.to_dict()),
                stages=("reflect",),
                evaluations=("train",),
            ),
        ),
        actors={
            name: replace(actor, budget=BudgetLimits(max_concurrency=2))
            for name, actor in recipe.actors.items()
        },
        budget=BudgetLimits(max_evaluations=40, max_concurrency=3),
    )
    return registry, recipe, options


def actual_sample(source, responder):
    from ace_reference_method import load_engine

    engine, calls = load_engine(source, responder)
    raw = (Path(source) / "ace/ace_batch.py").read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest()
        == "18e4bf974ff17383997ba28b715d0a99e83b5ea2e5d49a2fb91b4b000c0874fd"
    )
    cls = next(
        node
        for node in ast.parse(raw).body
        if isinstance(node, ast.ClassDef) and node.name == "ACEBatch"
    )
    method = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "_generate_and_reflect_single_sample"
    )
    namespace = dict(engine._train_single_sample.__globals__)
    exec(
        compile(
            ast.Module(body=[method], type_ignores=[]),
            str(source) + "/ace/ace_batch.py",
            "exec",
            flags=__future__.annotations.compiler_flag,
        ),
        namespace,
    )
    engine.sample = MethodType(namespace[method.name], engine)
    return engine, calls


@pytest.mark.parametrize(
    "behavior,rounds,no_ground_truth,json_mode",
    [
        ("learn", 2, False, False),
        ("correct", 0, False, True),
        ("wrong", 0, False, False),
        ("wrong", 2, False, False),
        ("learn", 3, True, True),
    ],
)
def test_sample_program_matches_actual_upstream_and_keeps_counters_local(
    tmp_path, behavior, rounds, no_ground_truth, json_mode
):
    source = os.environ.get("PROTEUS_ACE_SOURCE")
    if not source:
        pytest.skip("actual pinned ACE source required in CI")
    from ace_reference_method import Processor

    root = tmp_path / "study"
    registry, recipe, options = setup(
        root, behavior=behavior, rounds=rounds, no_ground_truth=no_ground_truth, json_mode=json_mode
    )
    result, records = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    engine, calls = actual_sample(
        source, lambda role, prompt: response(role, prompt, behavior=behavior)
    )
    dataset = json.loads((root / "tasks.json").read_text())
    samples = [row for row in dataset["records"] if row["task"]["split"] == "train"]
    expected = []
    for index, row in enumerate(samples):
        expected.append(
            engine.sample(
                {**row["task"]["input"], "target": row["reference"]},
                Processor(),
                PLAYBOOK,
                f"train_e_1_s_{index + 1}",
                1,
                index + 1,
                None,
                None,
                {
                    "max_num_rounds": rounds,
                    "use_json_mode": json_mode,
                    "no_ground_truth": no_ground_truth,
                },
            )
        )
    assert [(call["role"], call["request"]["prompt"]) for call in records["actor_calls"]] == calls
    mapped = next(
        row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.workflow_map@1"
    )
    values = {row["ref"]["id"]: row["value"] for row in records["values"]}
    learning = [values[row["outputs"]["learning"]["id"]] for row in mapped["results"]]
    store = ArtifactStore(root / "run/artifacts", registry)
    candidates = {
        row["id"]: store.get(ArtifactRef.from_dict(row["artifact"]))
        for row in records["candidates"]
    }
    assert candidates["initial"].text == PLAYBOOK and candidates["initial"].next_id == 2
    for actual, reference in zip(learning, expected):
        assert actual["pre_train_answer"] == reference["pre_train_answer"]
        assert actual["final_answer"] == reference["final_answer"]
        assert actual["correct"] == reference["is_correct"]
        assert actual["bullet_tags"] == reference["all_bullet_tags"]
        assert actual["reflection"] == reference["reflection_content"]
        local = candidates[actual["local_id"]]
        assert local.next_id == 2
        expected_count = 0 if behavior == "correct" else len(actual["reflections"])
        # Upstream's tag lookup keeps the last tag for each bullet within one
        # update: the fixture's helpful tag supersedes its earlier harmful tag.
        assert local.bullets[0].helpful == expected_count and local.bullets[0].harmful == 0
        assert len(actual["predictions"]) == (1 if behavior == "correct" else expected_count + 1)
        if behavior == "correct":
            assert actual["local_id"] == "initial" and len(actual["reflections"]) == 1
    book = run_ledger(root / "run")
    before = book.events()
    assert book.balance()["charged"]["evaluations"] == sum(
        len(item["predictions"]) for item in learning
    )
    assert book.balance()["active_slots"] == 0
    assert run(root, registry, recipe, resume=True)[0] == result
    assert book.events() == before
    assert sum(
        len(path.read_text().splitlines())
        for path in (root / "run").rglob("fixture-invocations.txt")
    ) == len(calls)


@pytest.mark.parametrize("mode,workers", [("offline", 1), ("offline", 3), ("online", 3)])
def test_complete_combee_fixture_preserves_three_phases_and_window_boundaries(
    tmp_path, mode, workers
):
    from proteus.examples.combee_ace import prepare
    from proteus.examples.ace_playbook import register as register_fixture
    from proteus.methods.ace.batch import register
    from proteus.recipes.config import load_recipe
    from proteus.recipes.builtins import builtin_registry

    root = tmp_path / "study"
    recipe = load_recipe(prepare(root, mode=mode, workers=workers, batch_size=2))
    registry = builtin_registry()
    register(registry)
    register_fixture(registry)
    result, records = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    values = {row["ref"]["id"]: row["value"] for row in records["values"]}
    summary = next(
        row["value"]
        for row in records["values"]
        if row["ref"]["kind"] == "proteus.combee.ace_summary@1"
    )
    batches = [values[ref["id"]] for ref in summary["batches"]]
    assert [batch["task_ids"] for batch in batches] == [["train-0", "train-1"], ["train-2"]]
    assert [batch["start"] for batch in batches] == [1, 3]
    assert all(not batch["rolled_back"] for batch in batches)
    for batch in batches:
        samples = [values[ref["id"]] for ref in batch["samples"]]
        assert all(sample["before_id"] == batch["before_id"] for sample in samples)
        receipt = values[batch["post_evaluation"]["id"]]
        assert receipt["candidate_ids"] == [batch["after_id"]] * len(batch["task_ids"])
        assert all(
            row["score"] == 1 for evaluated in receipt["batches"] for row in evaluated["rows"]
        )
    if mode == "online":
        assert len(summary["windows"]) == 2
        for batch, ref in zip(batches, summary["windows"]):
            receipt = values[ref["id"]]
            assert not receipt["batches"] and receipt["candidate_ids"] == [
                batch["before_id"]
            ] * len(batch["task_ids"])
    else:
        assert len(summary["validations"]) == 1
        assert summary["selected_id"] == batches[0]["after_id"]
    ledger = run_ledger(root / "run")
    before = ledger.events()
    assert ledger.balance()["active_slots"] == 0
    assert run(root, registry, recipe, resume=True)[0] == result
    assert ledger.events() == before


def actual_batch_engine(source, responder, seed):
    """Unchanged full ACEBatch AST and original roles; only transport/logging replaced."""
    from ace_reference_method import load_engine

    serial, calls = load_engine(source, responder)
    namespace = serial._train_single_sample.__globals__
    raw = (Path(source) / "ace/ace_batch.py").read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest()
        == "18e4bf974ff17383997ba28b715d0a99e83b5ea2e5d49a2fb91b4b000c0874fd"
    )
    cls = next(
        node
        for node in ast.parse(raw).body
        if isinstance(node, ast.ClassDef) and node.name == "ACEBatch"
    )
    names = {
        "run",
        "_extract_config_params",
        "_setup_paths",
        "_run_test",
        "_generate_and_reflect_single_sample",
        "_train_batch",
        "_offline_train",
        "_online_train_and_test",
    }
    cls.body = [
        node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert len(cls.body) == len(names)
    curator = next(
        node
        for node in ast.parse((Path(source) / "ace/core/curator.py").read_text()).body
        if isinstance(node, ast.ClassDef) and node.name == "Curator"
    )
    additions = [
        node
        for node in curator.body
        if isinstance(node, ast.FunctionDef)
        and node.name in ("propose_operations", "aggregate_operations", "_add_operations_only")
    ]
    assert len(additions) == 3
    namespace.update(
        random=random.Random(seed),
        is_api_error=lambda error: False,
        INCORRECT_DUE_TO_API_ERROR="api-error",
    )
    exec(
        compile(
            ast.Module(body=[cls, *additions], type_ignores=[]),
            str(source) + "/ace/ace_batch.py",
            "exec",
            flags=__future__.annotations.compiler_flag,
        ),
        namespace,
    )
    engine = namespace["ACEBatch"]()
    engine.__dict__.update(serial.__dict__)
    for node in additions:
        setattr(engine.curator, node.name, MethodType(namespace[node.name], engine.curator))
    return engine, calls


@pytest.mark.parametrize(
    "mode,behavior,changes",
    [
        ("offline", "learn", {}),
        ("online", "learn", {"batch_size": 3}),
        ("offline", "correct", {"max_num_rounds": 0, "num_epochs": 2}),
        ("offline", "wrong", {"max_num_rounds": 2, "eval_steps": 3}),
        ("online", "wrong", {"max_num_rounds": 0, "online_eval_frequency": 1}),
        ("offline", "learn", {"no_ground_truth": True, "json_mode": True, "batch_size": 3}),
    ],
)
def test_complete_combee_method_matches_actual_reference_controller(
    tmp_path, mode, behavior, changes
):
    source = os.environ.get("PROTEUS_ACE_SOURCE")
    if not source:
        pytest.skip("actual pinned ACE source required in CI")
    from ace_reference_method import Processor
    from proteus.examples.combee_ace import prepare
    from proteus.examples.ace_playbook import register as fixture
    from proteus.methods.ace.batch import register, CombeeOptions
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.config import load_recipe

    root = tmp_path / "study"
    recipe = load_recipe(prepare(root, mode=mode, workers=3))
    registry = builtin_registry()
    register(registry)
    fixture(registry)
    options = replace(CombeeOptions.from_dict(recipe.method.options), **changes)
    sample = SampleOptions(
        train=options.train,
        reflection_stage=options.reflection_stage,
        max_num_rounds=options.max_num_rounds,
        no_ground_truth=options.no_ground_truth,
        json_mode=options.json_mode,
    )
    initial = ArtifactStore(root / "artifact-store", registry).put(
        CODEC, Playbook.from_text(PLAYBOOK)
    )
    recipe = replace(
        recipe,
        method=plugin("proteus.combee_ace", **options.to_dict()),
        subject=replace(recipe.subject, initial_artifact=initial),
        workflows=(
            replace(
                recipe.workflows[0],
                operator=plugin("proteus.combee_ace_sample", **sample.to_dict()),
            ),
        ),
        actors={
            role: replace(
                actor, runtime=plugin("example.ace_offline", role=role, behavior=behavior)
            )
            for role, actor in recipe.actors.items()
        },
    )
    result, records = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    engine, calls = actual_batch_engine(
        source, lambda role, prompt: response(role, prompt, behavior=behavior), recipe.seed
    )
    engine.playbook = engine.best_playbook = PLAYBOOK
    engine.next_global_id = 2
    data = json.loads((root / "tasks.json").read_text())
    samples = {
        split: [
            {**row["task"]["input"], "target": row["reference"]}
            for row in data["records"]
            if row["task"]["split"] == split
        ]
        for split in ("train", "validation", "test")
    }
    upstream = engine.run(
        mode=mode,
        train_samples=samples["train"],
        val_samples=samples["validation"],
        test_samples=samples["test"] if mode == "offline" else samples["train"],
        data_processor=Processor(),
        config={
            **options.to_dict(),
            "playbook_token_budget": options.token_budget,
            "augmented_shuffling_factor": options.grouping.duplication_factor,
            "curator_num_groups": options.grouping.num_groups,
            "test_workers": 3,
            "save_dir": str(tmp_path / "reference"),
        },
    )
    actual = [(call["role"], call["request"]["prompt"]) for call in records["actor_calls"]]
    # Thread completion can reorder independent samples. Preserve the complete
    # prompt multiset plus sequential generator/reflector order for each task.
    assert Counter(actual) == Counter(calls)
    for row in data["records"]:
        question = row["task"]["input"]["question"]
        assert [call for call in actual if call[0] != "curator" and question in call[1]] == [
            call for call in calls if call[0] != "curator" and question in call[1]
        ]
    summary = next(
        row["value"]
        for row in records["values"]
        if row["ref"]["kind"] == "proteus.combee.ace_summary@1"
    )
    store = ArtifactStore(root / "run/artifacts", registry)
    candidates = {
        row["id"]: store.get(ArtifactRef.from_dict(row["artifact"]))
        for row in records["candidates"]
    }
    assert (candidates[summary["final_id"]].text, candidates[summary["final_id"]].next_id) == (
        engine.playbook,
        engine.next_global_id,
    )
    assert candidates[summary["selected_id"]].text == (
        engine.best_playbook if mode == "offline" else engine.playbook
    )
    if mode == "offline":
        assert (
            summary["best_validation_accuracy"]
            == upstream["training_results"]["best_validation_accuracy"]
        )
    assert all("hidden-reference" not in prompt for _, prompt in actual)
    ledger = run_ledger(root / "run")
    before = ledger.events()
    assert run(root, registry, recipe, resume=True)[0] == result
    assert ledger.events() == before


def test_post_curation_infrastructure_failure_rolls_back_playbook_and_global_id(tmp_path):
    from proteus.examples.combee_ace import prepare
    from proteus.examples.ace_playbook import register as fixture, OfflineActor
    from proteus.methods.ace.batch import register
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.config import load_recipe
    from proteus.core.methods import ActorResult
    from proteus.core.contracts import RuntimeIdentity
    from proteus.evaluation.records import zero_usage

    root = tmp_path / "study"
    recipe = load_recipe(prepare(root, workers=3))
    registry = builtin_registry()
    register(registry)
    fixture(registry)

    class Failing(OfflineActor):
        def execute(self, spec, request, workspace):
            if self.options.role == "generator" and "fixture learned" in request.prompt:
                with (workspace / "fixture-invocations.txt").open("a") as stream:
                    stream.write("failed generator\n")
                return ActorResult(
                    status=OperationStatus.FAILED,
                    text="",
                    diagnostic="fixture provider unavailable",
                    runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
                    usage=zero_usage(),
                )
            return super().execute(spec, request, workspace)

    ref = recipe.actors["generator"].runtime.plugin
    registry._plugins[(ref.name, ref.version)] = replace(registry.get(ref), factory=Failing)
    result, records = run(root, registry, recipe)
    assert result.status == OperationStatus.FAILED and "cannot impute" in result.diagnostic
    values = {row["ref"]["id"]: row["value"] for row in records["values"]}
    batch = values[result.outputs["failed_batch"].id]
    assert batch["rolled_back"] and batch["before_id"] == batch["after_id"] == "initial"
    assert batch["proposed_id"] != "initial"
    store = ArtifactStore(root / "run/artifacts", registry)
    candidates = {
        row["id"]: store.get(ArtifactRef.from_dict(row["artifact"]))
        for row in records["candidates"]
    }
    assert (
        candidates[batch["before_id"]].next_id == 1
        and candidates[batch["proposed_id"]].next_id == 2
    )
    assert "fixture learned" not in candidates[batch["after_id"]].text
    failed = [
        row
        for evaluated in records["evaluations"]
        for row in evaluated["rows"]
        if row["status"] == "infra_error"
    ]
    assert len(failed) == 2 and all(row["score"] is None for row in failed)
    ledger = run_ledger(root / "run")
    before = ledger.events()
    assert ledger.balance()["active_slots"] == 0
    assert run(root, registry, recipe, resume=True)[0] == result
    assert ledger.events() == before


BATCH_WORKER = r"""
import os,sys,json
from pathlib import Path
from proteus.examples.combee_ace import prepare
from proteus.examples.ace_playbook import register as fixture
from proteus.methods.ace.batch import register
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import load_recipe
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
from proteus.core.recovery import Journal
root,boundary,mode=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
path=prepare(root/'study',workers=1) if mode=='run' else root/'study/recipe.json'
recipe=load_recipe(path);registry=builtin_registry();register(registry);fixture(registry)
lock=registry.make_lock(recipe,[SourcePin(name='proteus',repository='https://example.invalid/fixture',revision='0'*40)])
if mode=='run':
    original=Journal.call
    target,_,occurrence=boundary.partition('@')
    remaining=int(occurrence or 1)
    def call(self,kind,*args,**kwargs):
        global remaining
        result=original(self,kind,*args,**kwargs)
        if kind==target and self.root==root/'run/private/recovery':
            remaining-=1
            if remaining==0:os._exit(86)
        return result
    Journal.call=call
result=run_recipe(recipe,registry=registry,lock=lock,root=root/'run',artifact_source=root/'study/artifact-store',resume=mode=='resume')
records=json.loads(sorted((root/'run').glob('execution-*.json'))[-1].read_text())
print(json.dumps({'status':result.status.value,'diagnostic':result.diagnostic,'calls':len(records['actor_calls']),
    'rollouts':len(records['rollouts'])}))
"""


@pytest.mark.parametrize(
    "boundary",
    [
        "workflow.map",
        "aggregation.plan",
        "stage.map",
        "stage",
        "aggregation.result",
        "evaluation.finish",
        "evaluation.finish@2",
        "checkpoint",
        "checkpoint@2",
        "method.result",
    ],
)
def test_combee_batch_recovery_does_not_repeat_completed_learning(tmp_path, boundary):
    def invoke(mode):
        return subprocess.run(
            [sys.executable, "-c", BATCH_WORKER, str(tmp_path), boundary, mode],
            text=True,
            capture_output=True,
            timeout=60,
        )

    stopped = invoke("run")
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    resumed = invoke("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    output = json.loads(resumed.stdout)
    assert output["status"] == "completed", output
    ledger = run_ledger(tmp_path / "run")
    before = ledger.events()
    assert ledger.balance()["active_slots"] == 0
    assert (
        sum(
            len(path.read_text().splitlines())
            for path in (tmp_path / "run").rglob("fixture-invocations.txt")
        )
        == output["calls"]
    )
    again = invoke("resume")
    assert again.returncode == 0 and again.stdout == resumed.stdout
    assert ledger.events() == before


@pytest.mark.parametrize("limit", [5, 80])
def test_combee_http_calls_share_budget_and_resume_without_provider_replay(tmp_path, monkeypatch, limit):
    from proteus.examples.combee_ace import prepare
    from proteus.examples.ace_playbook import register as fixture
    from proteus.methods.ace.batch import register
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.config import load_recipe

    requests, active, peak = [], Counter(), Counter()
    mutex, barrier = threading.Lock(), threading.Barrier(2)
    first_training = set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            role, prompt = payload["model"], payload["messages"][0]["content"]
            with mutex:
                requests.append(payload)
                active[role] += 1
                active["total"] += 1
                for key, count in active.items():
                    peak[key] = max(peak[key], count)
                training = next((i for i in (0, 1) if f"Solve fixture train-{i}." in prompt), None)
                meet = role == "generator" and training is not None and training not in first_training
                if meet:
                    first_training.add(training)
            try:
                if meet:
                    barrier.wait(timeout=10)
                answer = response(role, prompt)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"model": role, "choices": [{"message": {"content": answer}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3}}).encode())
            finally:
                with mutex:
                    active[role] -= 1
                    active["total"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PROTEUS_COMBEE_TEST_KEY", "fixture-key")
    root = tmp_path / "study"
    recipe = load_recipe(prepare(root, workers=3))
    registry = builtin_registry()
    register(registry)
    fixture(registry)
    recipe = replace(recipe, actors={role: replace(actor, model=role,
        runtime=plugin("proteus.chat", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                       key_env="PROTEUS_COMBEE_TEST_KEY"),
        budget=BudgetLimits(max_concurrency=2 if role == "generator" else 1))
        for role, actor in recipe.actors.items()},
        budget=BudgetLimits(max_model_calls=limit, max_evaluations=120, max_concurrency=3))
    try:
        result, records = run(root, registry, recipe)
        expected = OperationStatus.COMPLETED if limit == 80 else OperationStatus.FAILED
        assert result.status == expected, result.diagnostic
        assert peak["generator"] == 2 and peak["total"] <= 3
        assert peak["reflector"] <= 1 and peak["curator"] <= 1
        assert len(requests) <= limit and first_training == {0, 1}
        ledger = run_ledger(root / "run")
        balance = ledger.balance()
        assert balance["active_slots"] == 0
        assert balance["charged"]["model_calls"] == len(requests)
        calls = [row["settlement"]["result"] for row in ledger.entries()
                 if row["identity"]["metadata"]["kind"] == "actor"]
        assert sum(row["usage"]["input_tokens"] for row in calls) == 5 * len(requests)
        assert sum(row["usage"]["output_tokens"] for row in calls) == 3 * len(requests)
        assert all("hidden-reference" not in item["messages"][0]["content"] for item in requests)
        if limit == 5:
            assert len(requests) == 5 and all(item["model"] == "generator" for item in requests)
            assert not any(row["ref"]["kind"] == "proteus.combee.ace_batch@1" for row in records["values"])
        else:
            assert {item["model"] for item in requests} == {"generator", "reflector", "curator"}
        before, count = ledger.events(), len(requests)
        assert run(root, registry, recipe, resume=True)[0] == result
        assert ledger.events() == before and len(requests) == count
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_combee_preflight_rejects_protocol_mismatch_and_unconsumed_stages(tmp_path):
    from proteus.core.serialization import ContractError
    from proteus.examples.combee_ace import prepare
    from proteus.examples.ace_playbook import register as fixture
    from proteus.methods.ace.batch import register
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.config import load_recipe

    recipe = load_recipe(prepare(tmp_path / "study"))
    registry = builtin_registry()
    register(registry)
    fixture(registry)
    workflow = recipe.workflows[0]
    mismatched = replace(SampleOptions.from_dict(workflow.operator.options), max_num_rounds=0)
    for changed in (
        replace(recipe, reference=replace(recipe.reference, profile="reference")),
        replace(recipe, workflows=(replace(workflow,
            operator=plugin("proteus.combee_ace_sample", **mismatched.to_dict())),)),
        replace(recipe, workflows=(replace(workflow, evaluations=("test",)),)),
        replace(recipe, stages=recipe.stages[:1]),
        replace(recipe, stages=(*recipe.stages, replace(recipe.stages[0], id="unused"))),
    ):
        with pytest.raises(ContractError):
            registry.resolve(changed)
    assert not (tmp_path / "study/run").exists()
