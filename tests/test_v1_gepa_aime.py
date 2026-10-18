"""Domain semantics, real HTTP transport, full-cohort reporting and recovery."""

import importlib.util
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proteus.benchmarks import aime_data, aime_format
from proteus.core.serialization import ContractError, content_hash
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.gepa_aime import Settings, execute, load, prepare, report
from proteus.recipes.config import ActorSpec
from test_v1_evaluation import SOURCE


HAS_GEPA = importlib.util.find_spec("gepa") is not None or os.environ.get("PROTEUS_REQUIRE_GEPA")
requires_gepa = pytest.mark.skipif(not HAS_GEPA, reason="optional pinned GEPA; required in CI")


@pytest.fixture
def endpoint(monkeypatch):
    calls, responses = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(payload)
            if responses:
                code, answer = responses.pop(0)
            else:
                code = 200
                answer = "```Return correct answer.```" if payload["model"] == "fixture-reflector" else (
                    "[[ ## reasoning ## ]]\nFixture calculation.\n[[ ## answer ## ]]\n"
                    + ("42" if "Return correct answer." in payload["messages"][0]["content"] else "0")
                    + "\n[[ ## completed ## ]]")
            body = json.dumps({"model": payload["model"], "choices": [{"message": {"content": answer}}],
                               "usage": {"prompt_tokens": 5, "completion_tokens": 3}}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AIME_FIXTURE_KEY", "fixture-only")
    yield f"http://127.0.0.1:{server.server_port}/v1", calls, responses
    server.shutdown()
    server.server_close()
    thread.join()


def setup(root, endpoint, seeds=(0,), threshold=10):
    url, _, _ = endpoint
    dataset = TaskDataset(version="aime-http-fixture-1", records=tuple(
        TaskRecord(task=Task(id=f"{split}-{i}", version="1", split=split, input={"problem": f"Fixture {split} question {i}"}),
            reference={"answer": "42", "solution": "private training solution" if split != "test" else "private test solution"})
        for split in ("train", "validation", "test") for i in range(2)))
    settings = Settings(seeds=seeds, max_metric_calls=threshold,
        solver=ActorSpec(runtime=plugin("proteus.aime_solver", base_url=url, key_env="AIME_FIXTURE_KEY"),
                        model="fixture-solver", model_settings={"temperature": 1, "max_tokens": 32000}),
        reflection=ActorSpec(runtime=plugin("proteus.chat", base_url=url, key_env="AIME_FIXTURE_KEY"), model="fixture-reflector"))
    prepare(root, dataset, settings, sources=[SOURCE])
    return dataset, settings


@requires_gepa
def test_study_executes_both_arms_reports_all_seeds_and_resumes_without_calls(tmp_path, endpoint):
    root = tmp_path / "study"
    setup(root, endpoint, seeds=(0, 7, 19))
    result = execute(root)
    assert result["dataset_profile"] == "custom/fixture; not AIME reproduction"
    assert [row["seed"] for row in result["seeds"]] == [0, 7, 19]
    assert result["aggregate"]["accuracy_delta"] == {"n": 3, "mean": 1, "sample_sd": 0}
    assert len(endpoint[1]) == 45  # per seed: baseline 2, optimization 10, test 2, reflection 1
    for row in result["seeds"]:
        assert row["baseline"]["accuracy"] == 0 and row["optimized"]["accuracy"] == 1
        for arm, trials, calls in (("baseline", 2, 2), ("optimized", 12, 13)):
            assert row[arm]["ledger"]["charged"]["evaluations"] == trials
            assert row[arm]["ledger"]["charged"]["model_calls"] == calls
            assert row[arm]["ledger"]["charged"]["cost_usd"] is None
            assert row[arm]["tokens"] == {"input_tokens": 5 * calls, "output_tokens": 3 * calls}
    for call in endpoint[1]:
        text = json.dumps(call)
        assert "private test solution" not in text and "fixture-only" not in text
        if call["model"] == "fixture-solver":
            assert "private training solution" not in text
    before = len(endpoint[1])
    assert execute(root, resume=True) == result and report(root) == result
    assert len(endpoint[1]) == before
    with pytest.raises(ContractError, match="use resume"):
        execute(root)


@requires_gepa
@pytest.mark.parametrize("threshold", [10, 18])
def test_domain_reflection_matches_actual_optimize_anything(tmp_path, endpoint, threshold):
    from gepa.optimize_anything import GEPAConfig, EngineConfig, ReflectionConfig, optimize_anything
    from types import SimpleNamespace
    root = tmp_path / "study"
    dataset, _ = setup(root, endpoint, threshold=threshold)
    execute(root)
    expected_prompts = []
    def evaluate(candidate, example):
        answer = "42" if candidate == "Return correct answer." else "0"
        score, feedback = aime_format.math_metric(SimpleNamespace(**example.reference), SimpleNamespace(answer=answer))
        return score, {"score": score, "input": example.task.input["problem"], "output": answer,
                       "reasoning": "Fixture calculation.", "execution_feedback": feedback}
    def reflect(prompt):
        expected_prompts.append(prompt)
        return "```Return correct answer.```"
    from proteus.examples.gepa_aime import INITIAL_PROMPT
    upstream = optimize_anything(seed_candidate=INITIAL_PROMPT, evaluator=evaluate,
        dataset=[r for r in dataset.records if r.task.split == "train"],
        valset=[r for r in dataset.records if r.task.split == "validation"],
        config=GEPAConfig(engine=EngineConfig(max_metric_calls=threshold, parallel=False, cache_evaluation=False,
            seed=0, display_progress_bar=False), reflection=ReflectionConfig(reflection_lm=reflect)))
    assert upstream.best_candidate == "Return correct answer."
    actual = [call["messages"][0]["content"] for call in endpoint[1] if call["model"] == "fixture-reflector"]
    assert actual == expected_prompts  # includes actual hybrid frontier/default optimize_anything formatting
    assert len(endpoint[1]) == upstream.total_metric_calls + 4 + len(expected_prompts)


@requires_gepa
@pytest.mark.parametrize("damage", ["dataset", "config", "provenance", "test-batch", "test-score", "receipt", "subject-inputs", "extra-cohort"])
def test_report_refuses_drift_and_incomplete_test_evidence(tmp_path, endpoint, damage):
    root = tmp_path / "study"
    setup(root, endpoint)
    execute(root)
    if damage in ("dataset", "config", "provenance"):
        target = root / {"dataset": "tasks.json", "config": "recipes/seed-0-optimized.json", "provenance": "provenance.json"}[damage]
        data = json.loads(target.read_text())
        if damage == "dataset":
            data["records"][0]["task"]["input"]["problem"] += "changed"
        elif damage == "config":
            data["seed"] = 42
        else:
            data["dataset"] = "changed"
        target.chmod(0o600)
        target.write_text(json.dumps(data))
    else:
        protocol = next(path for path in (root / "runs/seed-0-optimized/evaluations").glob("*/protocol.json")
                        if json.loads(path.read_text())["evaluation"]["id"] == "test")
        if damage == "extra-cohort":
            shutil.copytree(protocol.parent, protocol.parent.parent / "unreported-cohort")
            with pytest.raises(ContractError, match="unreported evaluation cohorts"):
                report(root)
            return
        target = protocol if damage == "subject-inputs" else protocol.parent / (
            "receipt.json" if damage == "receipt" else "private/batches/000000.json")
        if damage == "test-batch":
            target.unlink()
        else:
            data = json.loads(target.read_text())
            if damage == "receipt":
                data["candidate_ids"][0] = "initial"
            elif damage == "subject-inputs":
                data["inputs"] = {"undeclared": "changed"}
            else:
                data["rows"][0].update(score=0, status="task_failed")
            target.chmod(0o600)
            target.write_text(json.dumps(data))
    with pytest.raises((ContractError, FileNotFoundError)):
        report(root)


@requires_gepa
def test_http_failure_stops_before_other_arms_and_malformed_answer_is_scored(tmp_path, endpoint):
    root = tmp_path / "failed"
    setup(root, endpoint)
    endpoint[2].append((503, "unavailable"))
    with pytest.raises(ContractError, match="stopped"):
        execute(root)
    count = len(endpoint[1])
    assert not (root / "runs/seed-0-optimized").exists()
    with pytest.raises(ContractError):
        execute(root, resume=True)
    assert len(endpoint[1]) == count
    with pytest.raises(ContractError):
        report(root)
    root = tmp_path / "malformed"
    setup(root, endpoint)
    endpoint[2].append((200, "not structured"))
    result = execute(root)
    assert result["seeds"][0]["baseline"]["test_scores"][0]["score"] == 0


@requires_gepa
def test_recipe_rejects_native_profile_wrong_subject_and_invalid_dataset_stage(tmp_path, endpoint):
    root = tmp_path / "study"
    setup(root, endpoint)
    _, _, reg, runs = load(root)
    recipe = runs[1][1]
    with pytest.raises(ContractError, match="adapted or extended"):
        reg.resolve(replace(recipe, reference=replace(recipe.reference, profile="reference")))
    with pytest.raises(ContractError, match="solver input"):
        reg.resolve(replace(recipe, actors={**recipe.actors, "solver": replace(recipe.actors["solver"],
            runtime=plugin("proteus.chat", base_url=endpoint[0], key_env="AIME_FIXTURE_KEY"))}))
    with pytest.raises(ContractError):
        reg.resolve(replace(recipe, stages=tuple(replace(s, operator=plugin("proteus.prompt"))
                    if s.id == "side-info" else s for s in recipe.stages)))


def test_data_source_hash_and_split_rules(tmp_path):
    training = [{"id": i, "problem": f"train problem {i}", "answer": str(i), "solution": f"solution {i}"} for i in range(90)]
    testing = [{"problem_idx": i, "problem": f"test problem {i}", "answer": i} for i in range(30)]
    dataset = aime_data.from_rows(training, testing)
    import random
    ids = list(range(90))
    random.Random(0).shuffle(ids)
    assert [r.task.id for r in dataset.records[:45]] == ["AI-MO/aimo-validation-aime:" + str(i) for i in ids[:45]]
    assert [r.task.id for r in dataset.records[90:]] == ["MathArena/aime_2025:" + str(i) for i in range(30)]
    assert all(set(r.task.input) == {"problem"} for r in dataset.records)
    assert content_hash(dataset) != aime_data.DATASET_HASH
    with pytest.raises(ContractError, match="90"):
        aime_data.from_rows(training[:-1], testing)
    with pytest.raises(ContractError, match="duplicate"):
        aime_data.from_rows([training[0]] * 90, testing)
    path = tmp_path / aime_data.SOURCES[0]["file"]
    path.write_bytes(b"corrupt")
    with pytest.raises(ContractError, match="checksum"):
        aime_data.fetch(aime_data.SOURCES[0], tmp_path)
    assert path.read_bytes() == b"corrupt"


def test_aime_cache_resolves_symlink_ancestors_before_durable_publication(tmp_path, monkeypatch):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    raw = b"fixture source bytes"
    monkeypatch.setattr(aime_data.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(raw))
    source = {"file": "fixture.parquet", "repository": "fixture/source", "revision": "0" * 40,
              "sha256": hashlib.sha256(raw).hexdigest()}
    path = aime_data.fetch(source, linked / "new-cache")
    assert path == actual / "new-cache/fixture.parquet" and path.read_bytes() == raw


@requires_gepa
def test_hard_exit_after_domain_stage_replays_exact_reflection_without_rebilling(tmp_path, endpoint):
    root = tmp_path / "study"
    setup(root, endpoint)
    child = subprocess.run([sys.executable, "-c", """
import os, sys
from proteus.core.methods import RunServices
from proteus.examples.gepa_aime import execute
original = RunServices.stage
def stage(self, name, *args, **kwargs):
    result = original(self, name, *args, **kwargs)
    if name == 'side-info':
        os._exit(73)
    return result
RunServices.stage = stage
execute(sys.argv[1])
""", str(root)], capture_output=True, text=True, timeout=60)
    assert child.returncode == 73, child.stderr
    before = len(endpoint[1])
    assert before == 7  # baseline 2, initial validation 2, parent minibatch 3
    result = execute(root, resume=True)
    assert result["seeds"][0]["optimized"]["accuracy"] == 1
    assert len(endpoint[1]) == 15
    resumed_prompts = [c["messages"] for c in endpoint[1] if c["model"] == "fixture-reflector"]
    fresh = tmp_path / "fresh"
    setup(fresh, endpoint)
    execute(fresh)
    assert [c["messages"] for c in endpoint[1] if c["model"] == "fixture-reflector"] == resumed_prompts * 2
