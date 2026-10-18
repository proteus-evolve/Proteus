"""Formula domain semantics, real HTTP transport, complete reports and recovery."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proteus.benchmarks import ace_formula_data
from proteus.core.serialization import ContractError
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.examples.ace_formula import Settings, execute, load, prepare, report
from proteus.examples.ace_playbook import response
from proteus.examples.candidate_evaluation import plugin
from proteus.recipes.config import ActorSpec
from test_v1_evaluation import SOURCE


@pytest.fixture
def endpoint(monkeypatch):
    calls, queued = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(payload)
            role = payload["model"].removeprefix("fixture-")
            code, text = queued.pop(0) if queued else (200, response(role, payload["messages"][0]["content"]))
            body = json.dumps({"model": payload["model"], "choices": [{"message": {"content": text}}],
                               "usage": {"prompt_tokens": 5, "completion_tokens": 3}}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("FORMULA_FIXTURE_KEY", "fixture-only")
    yield f"http://127.0.0.1:{server.server_port}/v1", calls, queued
    server.shutdown()
    server.server_close()
    thread.join()


def setup(root, endpoint, *, seeds=(0,), modes=("offline", "online"), **options):
    dataset = TaskDataset(version="formula-http-fixture-1", records=tuple(
        TaskRecord(task=Task(id=f"{split}-{index}", version="1", split=split,
            input={"question": f"Formula fixture {split}-{index}", "context": ""}),
            reference="1.000000000000000001" if split == "test" else "1")
        for split in ("train", "validation", "test") for index in range(3)))
    actors = {role: ActorSpec(runtime=plugin("proteus.chat", base_url=endpoint[0], key_env="FORMULA_FIXTURE_KEY"),
                             model=f"fixture-{role}", model_settings={"temperature": 0, "max_tokens": 4096,
                                 **({"response_format": {"type": "json_object"}} if options.get("json_mode") else {})})
              for role in ("generator", "reflector", "curator")}
    settings = Settings(seeds=seeds, modes=modes, **actors,
                        **{"eval_steps": 2, "online_eval_frequency": 2, **options})
    prepare(root, dataset, settings, sources=[SOURCE])
    return dataset, settings


def test_formula_study_both_protocols_multiple_replicates_and_call_free_resume(tmp_path, endpoint):
    root = tmp_path / "study"
    setup(root, endpoint, seeds=(0, 7))
    result = execute(root)
    assert result["dataset_profile"] == "custom/fixture; not Formula reproduction"
    assert [(item["seed"], item["mode"]) for item in result["runs"]] == [(0, "offline"), (0, "online"), (7, "offline"), (7, "online")]
    assert result["aggregate"]["offline"]["accuracy_delta"] == {"n": 2, "mean": 1, "sample_sd": 0}
    assert result["aggregate"]["online"]["accuracy_delta"] == {"n": 2, "mean": 1 / 3, "sample_sd": 0}
    for item in result["runs"]:
        assert item["baseline_accuracy"] == 0 and item["samples"] == 3
        calls, trials = (22, 16) if item["mode"] == "offline" else (19, 13)
        assert item["ledger"]["charged"]["evaluations"] == trials
        assert item["ledger"]["charged"]["model_calls"] == calls
        assert item["ledger"]["charged"]["cost_usd"] is None
        assert item["tokens"] == {"input_tokens": 5 * calls, "output_tokens": 3 * calls}
        if item["mode"] == "offline":
            assert item["accuracy"] == 1 and len(item["validations"]) == 1
            assert item["selected_id"] != item["final_id"]
        else:
            assert [window["tasks"] for window in item["windows"]] == [2, 1]
            assert [window["accuracy"] for window in item["windows"]] == [0, 1]
    before = len(endpoint[1])
    assert before == 82
    assert execute(root, resume=True) == result and report(root) == result
    assert len(endpoint[1]) == before
    with pytest.raises(ContractError, match="use resume"):
        execute(root)
    for payload in endpoint[1][:22]:
        assert "1.000000000000000001" not in json.dumps(payload)
    for payload in endpoint[1]:
        if payload["model"] != "fixture-reflector":
            assert "1.000000000000000001" not in json.dumps(payload)


@pytest.mark.parametrize("mode", ["offline", "online"])
def test_formula_full_role_prompts_match_actual_upstream(tmp_path, endpoint, mode):
    source = os.environ.get("PROTEUS_ACE_SOURCE")
    if not source:
        pytest.skip("pinned ACE source required in CI")
    from ace_reference_method import load_engine
    from check_ace_formula_reference import load_processor
    root = tmp_path / "study"
    dataset, settings = setup(root, endpoint, modes=(mode,))
    result = execute(root)
    processor, _ = load_processor(source)
    engine, expected = load_engine(source, response)
    pools = {split: [{**record.task.input, "target": record.reference} for record in dataset.records
                     if record.task.split == split] for split in ("train", "validation", "test")}
    upstream = engine.run(mode=mode, train_samples=pools["train"], val_samples=pools["validation"],
        test_samples=pools["test"], data_processor=processor,
        config={**settings.to_dict(), "playbook_token_budget": settings.token_budget,
                "test_workers": 1, "save_dir": str(tmp_path / "reference")})
    assert [(payload["model"].removeprefix("fixture-"), payload["messages"][0]["content"])
            for payload in endpoint[1]] == expected
    assert upstream["initial_test_results"]["accuracy"] == result["runs"][0]["baseline_accuracy"]


@pytest.mark.parametrize("damage", ["dataset", "provenance", "config", "test-batch", "test-score", "prediction", "inputs", "order", "extra-cohort"])
def test_formula_report_refuses_drift_incomplete_scores_and_changed_evidence(tmp_path, endpoint, damage):
    root = tmp_path / "study"
    setup(root, endpoint, modes=("online",))
    execute(root)
    if damage in ("dataset", "provenance", "config"):
        target = root / {"dataset": "tasks.json", "provenance": "provenance.json", "config": "recipes/seed-0-online.json"}[damage]
        value = json.loads(target.read_text())
        if damage == "dataset":
            value["records"][0]["reference"] = "2"
        elif damage == "config":
            value["seed"] = 99
        else:
            value["dataset"] = "changed"
    elif damage == "order":
        target = root / "runs/seed-0-online/execution-0001.json"
        value = json.loads(target.read_text())
        value["rollouts"][0], value["rollouts"][3] = value["rollouts"][3], value["rollouts"][0]
    else:
        protocol = next(path for path in (root / "runs/seed-0-online/evaluations").glob("*/protocol.json")
                        if json.loads(path.read_text())["evaluation"]["id"] == "stream")
        if damage == "extra-cohort":
            shutil.copytree(protocol.parent, protocol.parent.parent / "unreported-cohort")
            with pytest.raises(ContractError, match="unreported evaluation cohorts"):
                report(root)
            return
        target = protocol if damage == "inputs" else protocol.parent / (
            "predictions/000000.json" if damage == "prediction" else "private/batches/000000.json")
        if damage == "test-batch":
            target.unlink()
            with pytest.raises(ContractError):
                report(root)
            return
        value = json.loads(target.read_text())
        if damage == "inputs":
            value["inputs"] = {}
        elif damage == "prediction":
            value["rollout"]["output"]["answer"] = "changed"
        else:
            value["rows"][0].update(score=1 - value["rows"][0]["score"], status="succeeded")
    target.chmod(0o600)
    target.write_text(json.dumps(value))
    with pytest.raises(ContractError):
        report(root)


def test_formula_transport_failure_stops_before_learning_or_other_runs(tmp_path, endpoint):
    root = tmp_path / "study"
    setup(root, endpoint)
    endpoint[2].append((503, "fixture failure"))
    with pytest.raises(ContractError, match="prediction failed"):
        execute(root)
    assert len(endpoint[1]) == 1
    assert not (root / "runs/seed-0-online").exists()
    assert all(payload["model"] == "fixture-generator" for payload in endpoint[1])


def test_formula_full_pinned_data_and_reference_semantics(tmp_path):
    source = os.environ.get("PROTEUS_ACE_SOURCE")
    if not source:
        pytest.skip("pinned ACE source required in CI")
    from check_ace_formula_reference import check
    dataset = check(source, tmp_path / "data")
    assert len(dataset.records) == 1000
    actor = ActorSpec(runtime=plugin("proteus.chat", base_url="http://127.0.0.1:9/v1",
                                    key_env="UNUSED_FORMULA_PREPARATION_KEY"), model="no-inference")
    settings = Settings(generator=actor, reflector=actor, curator=actor)
    prepare(tmp_path / "full-study", dataset, settings, sources=[SOURCE],
            provenance=json.loads((tmp_path / "data/provenance.json").read_text()))
    _, _, _, runs = load(tmp_path / "full-study")
    assert len(runs) == 6
    for entry, recipe, _ in runs:
        assert (recipe.budget.max_evaluations, recipe.budget.max_model_calls) == (
            (4400, 6400) if entry.mode == "offline" else (1400, 2200))
    cache = tmp_path / "bad-cache"
    cache.mkdir()
    (cache / ace_formula_data.SOURCES[0]["file"]).write_text("corrupted")
    with pytest.raises(ContractError, match="checksum"):
        ace_formula_data.fetch(ace_formula_data.SOURCES[0], cache)


def test_formula_settings_reject_unrecorded_extra_prompt(tmp_path, endpoint):
    _, settings = setup(tmp_path / "study", endpoint)
    with pytest.raises(ContractError, match="without tools or extra static prompts"):
        replace(settings, generator=replace(settings.generator, tools=("shell",)))
    _, _, _, runs = load(tmp_path / "study")
    assert runs[1][1].evaluations[0].tasks.split == "test"
    assert runs[1][1].evaluations[0].tasks == runs[1][1].evaluations[1].tasks


def test_formula_cache_resolves_symlink_ancestors_before_durable_publication(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    checkout = tmp_path / "source"
    source_file = checkout / "eval/finance/data/fixture.jsonl"
    source_file.parent.mkdir(parents=True)
    raw = b'{"context":"fixture","target":"1"}\n'
    source_file.write_bytes(raw)
    path = ace_formula_data.fetch({"file": "fixture.jsonl", "sha256": hashlib.sha256(raw).hexdigest()},
                                 linked / "new-cache", checkout=checkout)
    assert path == actual / "new-cache/fixture.jsonl" and path.read_bytes() == raw


@pytest.mark.parametrize("mode,options", [
    ("offline", {"num_epochs": 2, "curator_frequency": 2}),
    ("offline", {"eval_steps": 100, "max_num_rounds": 0}),
    ("online", {"max_num_rounds": 0, "curator_frequency": 2}),
    ("online", {"no_ground_truth": True, "json_mode": True, "online_eval_frequency": 10}),
])
def test_formula_report_covers_nondefault_reference_protocols(tmp_path, endpoint, mode, options):
    root = tmp_path / "study"
    setup(root, endpoint, modes=(mode,), **options)
    result = execute(root)["runs"][0]
    assert result["samples"] == 3 * options.get("num_epochs", 1)
    if options.get("eval_steps") == 100:
        assert result["validations"] == [] and result["selected_id"] == "initial" and result["accuracy"] == 0
    if options.get("no_ground_truth"):
        assert all("1.000000000000000001" not in json.dumps(payload) for payload in endpoint[1])


def test_formula_hard_exit_after_online_window_resumes_without_rescoring(tmp_path, endpoint):
    root = tmp_path / "study"
    setup(root, endpoint, modes=("online",))
    script = '''
import os, sys
from proteus.core.methods import RunServices
from proteus.examples.ace_formula import execute
original = RunServices.checkpoint
def checkpoint(self, payload, *args, **kwargs):
    result = original(self, payload, *args, **kwargs)
    if getattr(payload, "windows", ()) and not getattr(payload, "samples", ()):
        os._exit(73)
    return result
RunServices.checkpoint = checkpoint
execute(sys.argv[1])
'''
    stopped = subprocess.run([sys.executable, "-c", script, str(root)], capture_output=True, text=True)
    assert stopped.returncode == 73, stopped.stderr
    assert len(endpoint[1]) == 5  # initial whole-test baseline + first window, both before learning
    result = execute(root, resume=True)
    assert result["runs"][0]["accuracy"] == 1 / 3
    assert len(endpoint[1]) == 19
