"""Upstream conformance, occurrence provenance and recovery across aggregation."""

from dataclasses import replace
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import ArtifactRef, BudgetLimits, OperationStatus
from proteus.core.serialization import content_hash, json_value
from proteus.examples.combee_aggregation import Options, prepare, register, response
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE, plugin
from test_v1_ledger import run_ledger


@pytest.fixture(autouse=True)
def optional_gepa(request):
    family = getattr(getattr(request.node, "callspec", None), "params", {}).get("family")
    if (
        family == "gepa"
        and importlib.util.find_spec("gepa") is None
        and not os.environ.get("PROTEUS_REQUIRE_GEPA")
    ):
        pytest.skip("optional pinned GEPA; required in CI")


def setup(root, family, *, responses=None, default_response=None, **options):
    recipe = load_recipe(prepare(root, family=family))
    registry = builtin_registry()
    register(registry)
    return registry, replace(
        recipe,
        method=plugin("example.combee_aggregation", **Options(family=family, **options).to_dict()),
        actors={
            "learner": replace(
                recipe.actors["learner"],
                runtime=plugin(
                    "example.combee_fixture",
                    family=family,
                    responses=responses or {},
                    default_response=default_response,
                ),
            )
        },
    )


def run(root, registry, recipe, *, resume=False):
    result = run_recipe(
        recipe,
        registry=registry,
        lock=registry.make_lock(recipe, [SOURCE]),
        root=root / "run",
        artifact_source=root / "artifact-store" if recipe.subject else None,
        resume=resume,
    )
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    records = json.loads(sorted((root / "run").glob("execution-*.json"))[-1].read_text())
    return result, records


def values(records, kind):
    return [row["value"] for row in records["values"] if row["ref"]["kind"] == kind]


@pytest.mark.parametrize(
    "samples,factor,groups", [(1, 2, None), (3, 1, None), (4, 2, None), (9, 2, None), (7, 3, 4)]
)
def test_ace_complete_phase2_matches_actual_upstream(tmp_path, samples, factor, groups):
    source = os.environ.get("PROTEUS_ACE_SOURCE")
    if not source:
        pytest.skip("actual pinned ACE source required in CI")
    from combee_reference import ace

    expected, text, next_id, rng = ace(
        source, samples, lambda prompt: response("ace", prompt), factor=factor, num_groups=groups
    )
    root = tmp_path / "study"
    registry, recipe = setup(
        root, "ace", samples=samples, duplication_factor=factor, num_groups=groups
    )
    _, records = run(root, registry, recipe)
    assert [row["request"]["prompt"] for row in records["actor_calls"]] == expected
    artifact = ArtifactRef.from_dict(values(records, "proteus.artifact_ref@1")[-1])
    final = ArtifactStore(root / "run/artifacts", registry).get(artifact)
    assert (final.text, final.next_id) == (text, next_id)
    plan = values(records, "proteus.combee.plan@1")[0]
    assert plan["rng_after"] == json_value(rng)
    assert len(plan["records"]) == samples and sum(map(len, plan["groups"])) == samples * factor


@pytest.mark.parametrize(
    "samples,factor,batch,components,repeat",
    [
        (0, 2, True, ("instruction",), 1),
        (2, 2, True, ("instruction",), 1),
        (4, 1, True, ("instruction",), 1),
        (9, 2, True, ("instruction",), 2),
        (9, 3, False, ("instruction",), 1),
        (4, 2, True, ("b", "a", "b"), 1),
    ],
)
def test_gepa_actual_upstream_prompts_outputs_waves_and_rng(
    tmp_path, samples, factor, batch, components, repeat
):
    source = os.environ.get("PROTEUS_COMBEE_GEPA_SOURCE")
    if not source:
        pytest.skip("actual pinned GEPA ComBEE source required in CI")
    from combee_reference import gepa

    expected = gepa(
        source,
        samples=samples,
        factor=factor,
        batch=batch,
        components=list(components),
        repeat=repeat,
    )
    root = tmp_path / "study"
    registry, recipe = setup(
        root,
        "gepa",
        samples=samples,
        duplication_factor=factor,
        batch_reflection=batch,
        components=components,
        repeat=repeat,
    )
    _, records = run(root, registry, recipe)
    actual = values(records, "proteus.combee.gepa_result@1")
    for value, wanted in zip(actual, expected["outputs"], strict=True):
        for key in ("new_texts", "prompts", "raw_lm_outputs", "metadata"):
            assert value[key] == wanted[key]
    by_id = {row["ref"]["id"]: row["value"] for row in records["values"]}
    waves = values(records, "proteus.combee.wave@1")
    assert [[by_id[ref["id"]]["text"] for ref in wave["prompts"]] for wave in waves] == expected[
        "calls"
    ]
    plans = values(records, "proteus.combee.plan@1")
    if plans:
        assert plans[-1]["rng_after"] == expected["rng"]


@pytest.mark.parametrize("family", ["ace", "gepa"])
@pytest.mark.parametrize("survivors", [0, 1, 2])
def test_invalid_map_outputs_and_single_survivor_reducer_rules(tmp_path, family, survivors):
    root = tmp_path / "baseline"
    registry, recipe = setup(root, family, samples=4)
    _, records = run(root, registry, recipe)
    map_calls = records["actor_calls"][:2]
    responses = {
        content_hash(call["request"]["prompt"]): response(family, call["request"]["prompt"])
        for call in map_calls[:survivors]
    }
    root = tmp_path / "filtered"
    registry, recipe = setup(
        root, family, samples=4, responses=responses, default_response="<think>unfinished reasoning"
    )
    _, filtered = run(root, registry, recipe)
    count = 2 + int(survivors > 0 if family == "ace" else survivors > 1)
    assert len(filtered["actor_calls"]) == count
    result = values(filtered, f"proteus.combee.{family}_result@1")[0]
    if family == "gepa":
        assert bool(result["new_texts"]) == (survivors == 1)
    else:
        assert not result["delta"]["records"]
    source = os.environ.get(
        "PROTEUS_ACE_SOURCE" if family == "ace" else "PROTEUS_COMBEE_GEPA_SOURCE"
    )
    if source:
        from combee_reference import ace, gepa

        if family == "ace":
            prompts, _, _, _ = ace(
                source,
                4,
                lambda prompt: responses.get(content_hash(prompt), "<think>unfinished reasoning"),
            )
        else:
            expected = gepa(
                source,
                samples=4,
                responses=responses,
                default_response="<think>unfinished reasoning",
            )
            prompts = [prompt for wave in expected["calls"] for prompt in wave]
            assert result["metadata"] == expected["outputs"][0]["metadata"]
        assert [call["request"]["prompt"] for call in filtered["actor_calls"]] == prompts


@pytest.mark.parametrize("family,count", [("ace", 4), ("gepa", 2)])
def test_equal_prompt_occurrences_are_not_a_response_cache(tmp_path, family, count):
    root = tmp_path / "study"
    registry, recipe = setup(root, family, samples=1, duplication_factor=1, repeat=2)
    result, records = run(root, registry, recipe)
    assert len(records["actor_calls"]) == count
    assert (
        records["actor_calls"][0]["request"]["prompt"]
        == records["actor_calls"][count // 2]["request"]["prompt"]
    )
    assert len({call["id"] for call in records["actor_calls"]}) == count
    assert run(root, registry, recipe, resume=True)[0] == result
    assert (
        sum(
            len(path.read_text().splitlines())
            for path in (root / "run").rglob("fixture-invocations.txt")
        )
        == count
    )


@pytest.mark.parametrize("family", ["ace", "gepa"])
def test_known_infrastructure_failure_stops_before_reducer(tmp_path, monkeypatch, family):
    from proteus.examples.combee_aggregation import FixtureActor

    original = FixtureActor.execute

    def failed(self, *args, **kwargs):
        return replace(
            original(self, *args, **kwargs),
            status=OperationStatus.FAILED,
            diagnostic="fixture infrastructure failure",
        )

    monkeypatch.setattr(FixtureActor, "execute", failed)
    root = tmp_path / "study"
    registry, recipe = setup(root, family, samples=4)
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(
        recipe,
        registry=registry,
        lock=lock,
        root=root / "run",
        artifact_source=root / "artifact-store" if recipe.subject else None,
    )
    assert result.status == OperationStatus.FAILED
    records = json.loads((root / "run/execution-0001.json").read_text())
    assert len(records["actor_calls"]) == 2
    assert not values(records, f"proteus.combee.{family}_result@1")
    again = run_recipe(recipe, registry=registry, lock=lock, root=root / "run", resume=True)
    assert again == result
    assert (
        sum(
            len(path.read_text().splitlines())
            for path in (root / "run").rglob("fixture-invocations.txt")
        )
        == 2
    )


@pytest.mark.parametrize("family", ["ace", "gepa"])
def test_http_map_overlaps_then_reducer_uses_settled_inputs(tmp_path, monkeypatch, family):
    root = tmp_path / "study"
    registry, recipe = setup(root, family, samples=4)
    _, reference = run(root, registry, recipe)
    map_prompts = [call["request"]["prompt"] for call in reference["actor_calls"][:2]]
    barrier = threading.Barrier(2)
    guard = threading.Lock()
    active, peak, map_finished, count = 0, 0, 0, 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal active, peak, map_finished, count
            prompt = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["messages"][
                -1
            ]["content"]
            with guard:
                active += 1
                count += 1
                peak = max(peak, active)
            if prompt in map_prompts:
                barrier.wait(timeout=5)
                with guard:
                    map_finished += 1
            else:
                assert map_finished == 2
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "model": "fixture",
                        "choices": [{"message": {"content": response(family, prompt)}}],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                    }
                ).encode()
            )
            with guard:
                active -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("COMBEE_FIXTURE_KEY", "fixture-secret")
    root = tmp_path / "http"
    registry, recipe = setup(root, family, samples=4)
    actor = replace(
        recipe.actors["learner"],
        runtime=plugin(
            "proteus.chat",
            key_env="COMBEE_FIXTURE_KEY",
            base_url=f"http://127.0.0.1:{server.server_port}",
            timeout_seconds=10,
        ),
        model="fixture",
        budget=BudgetLimits(max_model_calls=3, max_concurrency=2),
    )
    recipe = replace(
        recipe, actors={"learner": actor}, budget=BudgetLimits(max_model_calls=3, max_concurrency=2)
    )
    try:
        result, records = run(root, registry, recipe)
        assert run(root, registry, recipe, resume=True)[0] == result
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert count == 3 and peak == 2
    assert sum(call["usage"]["input_tokens"] for call in records["actor_calls"]) == 21
    ledger = run_ledger(root / "run")
    assert ledger.balance()["charged"]["model_calls"] == 3
    assert ledger.balance()["unknown"]["cost_usd"] == 3
    assert "fixture-secret" not in json.dumps(records)


WORKER = r"""
import os,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_combee_aggregation import setup,run
from proteus.core.recovery import Journal
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.examples.combee_aggregation import register
root=Path(sys.argv[1]); family,boundary,mode=sys.argv[2:]
if mode=='run':
    registry,recipe=setup(root,family,samples=4,max_workers=1,repeat=2)
    (root/'resolved.json').write_text(recipe.to_json())
    original=Journal.call
    def called(self,kind,*args,**kwargs):
        result=original(self,kind,*args,**kwargs)
        if kind==boundary: os._exit(86)
        return result
    Journal.call=called
else:
    registry=builtin_registry();register(registry);recipe=load_recipe(root/'resolved.json')
result,_=run(root,registry,recipe,resume=mode=='resume')
print(result.to_json())
"""


@pytest.mark.parametrize("family", ["ace", "gepa"])
@pytest.mark.parametrize(
    "boundary", ["aggregation.plan", "stage.map", "stage", "aggregation.result"]
)
def test_aggregation_hard_exit_does_not_reshuffle_or_repeat_calls(tmp_path, family, boundary):
    root = tmp_path / "crash"
    first = subprocess.run(
        [sys.executable, "-c", WORKER, str(root), family, boundary, "run"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert first.returncode == 86, first.stderr
    resumed = subprocess.run(
        [sys.executable, "-c", WORKER, str(root), family, boundary, "resume"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert resumed.returncode == 0, resumed.stderr
    fresh = tmp_path / "fresh"
    registry, recipe = setup(fresh, family, samples=4, max_workers=1, repeat=2)
    _, expected = run(fresh, registry, recipe)
    actual = json.loads(sorted((root / "run").glob("execution-*.json"))[-1].read_text())
    assert [call["request"]["prompt"] for call in actual["actor_calls"]] == [
        call["request"]["prompt"] for call in expected["actor_calls"]
    ]
    assert values(actual, "proteus.combee.plan@1") == values(expected, "proteus.combee.plan@1")
    assert sum(
        len(path.read_text().splitlines())
        for path in (root / "run").rglob("fixture-invocations.txt")
    ) == len(expected["actor_calls"])
