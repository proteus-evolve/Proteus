"""Combee inside the full GEPA lifecycle, transport metadata and durable recovery."""

from dataclasses import replace
import importlib.util
import json
import os
import random
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import ArtifactRef, BudgetLimits, OperationStatus
from proteus.core.serialization import ContractError
from proteus.evaluation.tasks import TaskBatch
from proteus.examples.combee_gepa import prepare, register
from proteus.methods.combee_gepa import CombeeGepaOptions
from proteus.methods.gepa.method import Pool, evaluation_bound
from proteus.methods.gepa.reference import load_reference
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import load_recipe
from test_v1_evaluation import plugin
from test_v1_gepa_method import run, values, invocation_count
from test_v1_ledger import run_ledger


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("gepa") is None and not os.environ.get("PROTEUS_REQUIRE_GEPA"),
    reason="optional pinned GEPA; required in CI",
)


def setup(
    root,
    *,
    threshold=22,
    minibatch=9,
    proposal="Return 1.",
    factor=2,
    batch=True,
    acceptance="strict_improvement",
):
    recipe = load_recipe(prepare(root))
    registry = builtin_registry()
    register(registry)
    options = replace(
        CombeeGepaOptions.from_dict(recipe.method.options),
        max_metric_calls=threshold,
        minibatch_size=minibatch,
        duplication_factor=factor,
        batch_reflection=batch,
        acceptance=acceptance,
    )
    recipe = replace(
        recipe,
        method=plugin("proteus.combee_gepa", **options.to_dict()),
        actors={
            **recipe.actors,
            "reflection": replace(
                recipe.actors["reflection"],
                runtime=plugin("example.combee_gepa_offline", role="reflection", proposal=proposal),
            ),
        },
        budget=replace(recipe.budget, max_evaluations=evaluation_bound(options, 2) + 2),
    )
    return registry, recipe


@pytest.mark.parametrize(
    "threshold,minibatch,proposal,factor,batch,acceptance",
    [
        (2, 9, "Return 1.", 2, True, "strict_improvement"),
        (22, 9, "Return 1.", 2, True, "strict_improvement"),
        (22, 9, "Return 0.", 1, True, "strict_improvement"),
        (42, 9, "Return 0.", 3, False, "improvement_or_equal"),
        (10, 3, "Return 1.", 2, True, "strict_improvement"),
    ],
)
def test_full_search_matches_unwrapped_engine_and_combee(
    tmp_path, threshold, minibatch, proposal, factor, batch, acceptance
):
    root = tmp_path / "study"
    registry, recipe = setup(
        root,
        threshold=threshold,
        minibatch=minibatch,
        proposal=proposal,
        factor=factor,
        batch=batch,
        acceptance=acceptance,
    )
    result, records = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    summary = values(records, "proteus.gepa.summary@1")[0]
    train = TaskBatch.from_dict(result.state.state.payload["train"])
    validation = TaskBatch.from_dict(result.state.state.payload["validation"])
    expected_calls, prompts, outputs = [], [], []

    class Adapter:
        propose_new_texts = None

        def evaluate(self, tasks, candidate, capture_traces=False):
            from gepa.core.adapter import EvaluationBatch

            output = "1" if candidate["system.txt"] == "Return 1." else "0"
            score = float(output == "1")
            expected_calls.append(
                (dict(candidate), [task.id for task in tasks], [score] * len(tasks))
            )
            traces = [
                {
                    "Inputs": dict(task.input),
                    "Generated Outputs": output,
                    "Feedback": {
                        "score": score,
                        "feedback": [{"correct": bool(score), "reference": "1"}],
                    },
                    "Trace": [
                        {
                            "schema_version": 1,
                            "events": [{"kind": "offline-fixture", "role": "subject"}],
                        }
                    ],
                }
                for task in tasks
            ]
            return EvaluationBatch(
                outputs=[output] * len(tasks),
                scores=[score] * len(tasks),
                trajectories=traces if capture_traces else None,
                num_metric_calls=len(tasks),
            )

        def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
            return {key: eval_batch.trajectories for key in components_to_update}

    class LM:
        def __call__(self, prompt):
            prompts.append(prompt)
            return "```\n" + proposal + "\n```"

        def batch_complete(self, messages):
            return [self(message[0]["content"]) for message in messages]

    from proteus.operators.combee._gepa.combee import ComBEEReflectionLM

    class ReferenceReflection(ComBEEReflectionLM):
        def reflect_many(self, jobs):
            results = super().reflect_many(jobs)
            outputs.extend(result for result, _ in results)
            return results

    # The complete, independently invoked engine owns selection, evaluation,
    # acceptance and component iteration. Both paths share one seeded RNG across
    # parent selection, sampling and Combee grouping, not separate private streams.
    from gepa.strategies.candidate_selector import ParetoCandidateSelector
    from gepa.strategies.batch_sampler import EpochShuffledBatchSampler

    rng = random.Random(recipe.seed)
    reference = ReferenceReflection(
        LM(), rng=rng, duplication_factor=factor, batch_reflection=batch
    )
    expected = load_reference().optimize(
        seed_candidate={"system.txt": "Return 0."},
        trainset=Pool(train),
        valset=Pool(validation),
        adapter=Adapter(),
        reflection_strategy=reference,
        candidate_selection_strategy=ParetoCandidateSelector(rng),
        batch_sampler=EpochShuffledBatchSampler(minibatch, rng),
        reflection_minibatch_size=None,
        max_metric_calls=threshold,
        acceptance_criterion=acceptance,
        track_best_outputs=False,
        display_progress_bar=False,
        seed=recipe.seed,
    )
    store = ArtifactStore(root / "run/artifacts", registry)
    candidates = {item["id"]: item for item in records["candidates"]}
    texts = {
        key: dict(store.get(ArtifactRef.from_dict(item["artifact"])).components)
        for key, item in candidates.items()
    }
    assert [
        (
            texts[item["candidate_id"]],
            [row["task_id"] for row in item["rows"]],
            [row["score"] for row in item["rows"]],
        )
        for item in records["evaluations"]
    ] == expected_calls
    assert [
        call["request"]["prompt"] for call in records["actor_calls"] if call["role"] == "reflection"
    ] == prompts
    assert [texts[key] for key in summary["accepted_ids"]] == expected.candidates
    assert [candidates[key]["parent_ids"] for key in summary["accepted_ids"]] == [
        [summary["accepted_ids"][parent] for parent in parents if parent is not None]
        for parents in expected.parents
    ]
    actual = values(records, "proteus.combee.gepa_result@1")
    assert [
        {key: item[key] for key in ("new_texts", "prompts", "raw_lm_outputs", "metadata")}
        for item in actual
    ] == [
        {key: getattr(item, key) for key in ("new_texts", "prompts", "raw_lm_outputs", "metadata")}
        for item in outputs
    ]
    assert texts[summary["upstream_best_id"]] == expected.best_candidate
    assert summary["metric_calls"] == sum(len(item[1]) for item in expected_calls)
    assert summary["reflection_calls"] == len(prompts)
    assert invocation_count(root / "run") == summary["metric_calls"] + len(prompts) + 2
    assert (
        run_ledger(root / "run").balance()["charged"]["evaluations"] == summary["metric_calls"] + 2
    )
    assert all(
        row["split"] != "test"
        for item in values(records, "proteus.archive@1")
        for evaluation in item["evaluations"]
        for row in evaluation["cohort"]["tasks"]
    )
    before = run_ledger(root / "run").events()
    assert run(root, registry, recipe, resume=True)[0] == result
    assert run_ledger(root / "run").events() == before


@pytest.mark.parametrize(
    "answer,finish_reason,accepted",
    [
        ("Return 1.", "stop", True),
        ("Return 1.", None, True),
        ("Return 1.", "length", False),
        ("Return 1.", "max_tokens", False),
        ("```\nReturn 1.\n```", "length", True),
        ("<think>unfinished reasoning", "stop", False),
    ],
)
def test_transport_finish_reason_controls_proposals_and_survives_resume(
    tmp_path, monkeypatch, answer, finish_reason, accepted
):
    source = os.environ.get("PROTEUS_COMBEE_GEPA_SOURCE")
    if source:
        from combee_reference import gepa

        expected = gepa(source, default_response=answer, finish_reason=finish_reason)
        assert bool(expected["outputs"][0]["new_texts"]) == accepted
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            payload = json.dumps(
                {
                    "model": "fixture",
                    "choices": [{"message": {"content": answer}, "finish_reason": finish_reason}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("COMBEE_GEPA_TEST_KEY", "fixture-secret")
    root = tmp_path / "study"
    registry, recipe = setup(root, threshold=11)
    recipe = replace(
        recipe,
        actors={
            **recipe.actors,
            "reflection": replace(
                recipe.actors["reflection"],
                runtime=plugin(
                    "proteus.chat",
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    key_env="COMBEE_GEPA_TEST_KEY",
                ),
                model="fixture",
                budget=BudgetLimits(max_model_calls=4, max_concurrency=3),
            ),
        },
        budget=replace(recipe.budget, max_model_calls=4),
    )
    try:
        result, records = run(root, registry, recipe)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        summary = values(records, "proteus.gepa.summary@1")[0]
        assert len(summary["accepted_ids"]) == (2 if accepted else 1)
        assert summary["reflection_calls"] == len(requests) == (4 if accepted else 3)
        calls = [call for call in records["actor_calls"] if call["role"] == "reflection"]
        assert all(call["result"]["finish_reason"] == finish_reason for call in calls)
        assert sum(call["usage"]["input_tokens"] for call in calls) == len(requests) * 3
        assert run_ledger(root / "run").balance()["charged"]["model_calls"] == len(requests)
        before = len(requests)
        assert run(root, registry, recipe, resume=True)[0] == result
        assert len(requests) == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


WORKER = r"""
import os,sys,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_combee_gepa import setup
from test_v1_gepa_method import run,values,invocation_count
from proteus.recipes.config import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.examples.combee_gepa import register
from proteus.core.recovery import Journal
root=Path(sys.argv[1]);boundary,mode=sys.argv[2:]
if mode=='run':
    registry,recipe=setup(root,threshold=42,proposal='Return 0.',acceptance='improvement_or_equal')
    (root/'resolved.json').write_text(recipe.to_json())
    original=Journal.call
    seen={}
    def called(self,kind,*args,**kwargs):
        result=original(self,kind,*args,**kwargs)
        seen[kind]=seen.get(kind,0)+1
        if boundary==kind+':'+str(seen[kind]):os._exit(86)
        return result
    Journal.call=called
else:
    registry=builtin_registry();register(registry);recipe=load_recipe(root/'resolved.json')
result,records=run(root,registry,recipe,resume=mode=='resume')
print(json.dumps({'status':result.status.value,'diagnostic':result.diagnostic,
    'summary':values(records,'proteus.gepa.summary@1'),
    'prompts':[row['request']['prompt'] for row in records['actor_calls'] if row['role']=='reflection'],
    'plans':values(records,'proteus.combee.plan@1'),'calls':invocation_count(root/'run')}))
"""


@pytest.mark.parametrize(
    "boundary",
    [
        "aggregation.plan:1",
        "stage.map:1",
        "stage:1",
        "aggregation.result:1",
        "candidate:2",
        "checkpoint:2",
        "aggregation.plan:2",
        "method.result:1",
    ],
)
def test_full_method_hard_exit_preserves_groups_and_call_counts(tmp_path, boundary):
    def invoke(root, mode, selected=boundary):
        return subprocess.run(
            [sys.executable, "-c", WORKER, str(root), selected, mode],
            capture_output=True,
            text=True,
            timeout=60,
        )

    root = tmp_path / "crash"
    stopped = invoke(root, "run")
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    resumed = invoke(root, "resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    actual = json.loads(resumed.stdout)
    assert actual["status"] == "completed", actual
    clean = invoke(tmp_path / "clean", "run", "never")
    assert clean.returncode == 0, clean.stdout + clean.stderr
    expected = json.loads(clean.stdout)
    # IDs include the run identity; compare algorithm outputs and source/RNG plans.
    assert actual["prompts"] == expected["prompts"]
    assert actual["plans"] == expected["plans"]
    assert actual["calls"] == expected["calls"]
    for key in ("metric_calls", "reflection_calls", "iterations"):
        assert actual["summary"][0][key] == expected["summary"][0][key]
    ledger = run_ledger(root / "run")
    assert ledger.balance()["active_slots"] == 0
    before = ledger.events()
    again = invoke(root, "resume")
    assert again.returncode == 0 and json.loads(again.stdout) == actual
    assert ledger.events() == before


def test_preflight_rejects_reference_claim_and_nonparallel_actor(tmp_path):
    registry, recipe = setup(tmp_path / "study")
    from proteus.examples.gepa_prompt import register as register_serial_fixture

    register_serial_fixture(registry)
    for changed in (
        replace(recipe, reference=replace(recipe.reference, profile="reference")),
        replace(recipe, reference=replace(recipe.reference, deviations=())),
        replace(
            recipe,
            actors={
                **recipe.actors,
                "reflection": replace(
                    recipe.actors["reflection"],
                    runtime=plugin("example.gepa_offline", role="reflection"),
                ),
            },
        ),
    ):
        with pytest.raises(ContractError):
            registry.resolve(changed)


def test_failed_map_stops_engine_without_reduction_or_unrecorded_retry(tmp_path):
    from proteus.core.contracts import RuntimeIdentity
    from proteus.core.methods import ActorResult
    from proteus.evaluation.records import zero_usage
    from test_v1_evaluation import register as add_plugin

    class Failed:
        model_call_bound = 0

        def execute(self, spec, request, workspace):
            with (workspace / "fixture-invocations.txt").open("a") as stream:
                stream.write("failed-reflection\n")
            return ActorResult(
                text="",
                runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
                usage=zero_usage(),
                status=OperationStatus.FAILED,
                diagnostic="fixture unavailable",
            )

    root = tmp_path / "study"
    registry, recipe = setup(root)
    add_plugin(
        registry,
        "fixture.failed_combee",
        "actor",
        Failed,
        capabilities=("session.fresh", "actor.execute", "calls.bounded", "actor.parallel"),
    )
    recipe = replace(
        recipe,
        actors={
            **recipe.actors,
            "reflection": replace(
                recipe.actors["reflection"], runtime=plugin("fixture.failed_combee")
            ),
        },
    )
    result, records = run(root, registry, recipe)
    assert result.status == OperationStatus.FAILED, result
    assert "aggregation wave failed" in result.diagnostic
    assert len(records["candidates"]) == 1
    assert len(records["rollouts"]) == 11
    assert len([call for call in records["actor_calls"] if call["role"] == "reflection"]) == 3
    assert not values(records, "proteus.combee.gepa_result@1")
    assert invocation_count(root / "run") == 14
    before = run_ledger(root / "run").events()
    again, _ = run(root, registry, recipe, resume=True)
    assert again.status == OperationStatus.FAILED and again.diagnostic == result.diagnostic
    assert invocation_count(root / "run") == 14
    assert run_ledger(root / "run").events() == before
