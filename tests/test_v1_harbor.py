"""Harbor boundary checks, plus an opt-in real Docker release gate."""

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from proteus.artifacts.codecs import FileContent
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import OperationStatus, PluginRef
from proteus.core.serialization import ContractError, canonical_json, content_hash
from proteus.examples.harbor_evaluation import prepare
from proteus.execution import harbor_worker
from proteus.execution.harbor import _task_config, _verifier_usage, identify_runtime
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
from test_v1_ledger import run_ledger


def fixture_recipe(tmp_path):
    path = prepare(tmp_path / "study", sys.executable, runtime={"runtime_hash": content_hash("fixture")})
    return load_recipe(path), path.parent / "task"


def test_harbor_recipe_resolves_without_importing_optional_runtime(tmp_path):
    recipe, task = fixture_recipe(tmp_path)
    resolved = builtin_registry().resolve(recipe)
    assert resolved.execution.environment.provider == "docker"
    assert _task_config(task, recipe.execution)["verifier"]["environment_mode"] == "separate"
    assert "harbor" not in sys.modules


@pytest.mark.parametrize("before,after,error", [
    ('environment_mode = "separate"', 'environment_mode = "shared"', "isolate"),
    ('network_mode = "public"', 'network_mode = "no-network"', "network"),
    ('[agent]', '[agent]\nnetwork_mode = "no-network"', "phase network"),
    ('[verifier]', '[verifier]\nnetwork_mode = "allowlist"\nallowed_hosts = ["example.org"]', "phase network"),
    ('cpus = 1', 'cpus = 2', "resources"),
    ('[environment]', '[environment]\nallow_internet = true', "deprecated"),
    ('[environment]', '[environment]\ngpus = 1', "resources"),
    ('[environment]', '[environment]\nos = "windows"', "Linux"),
    ('[agent]', '[agent]\nuser = "nobody"', "user override"),
    ('[environment]', '[environment]\nenv = { SECRET = "${SECRET}" }', "interpolation"),
])
def test_task_policy_cannot_silently_override_execution(tmp_path, before, after, error):
    recipe, task = fixture_recipe(tmp_path)
    path = task / "task.toml"
    path.write_text(path.read_text().replace(before, after))
    with pytest.raises(ContractError, match=error):
        _task_config(task, recipe.execution)


def test_custom_compose_is_rejected_before_container_dispatch(tmp_path):
    recipe, task = fixture_recipe(tmp_path)
    (task / "environment").mkdir()
    (task / "environment/docker-compose.yaml").write_text("services: {}\n")
    with pytest.raises(ContractError, match="Compose"):
        _task_config(task, recipe.execution)


@pytest.mark.parametrize("name", ["Dockerfile", "docker-compose.yaml"])
def test_verifier_build_cannot_replace_pinned_image(tmp_path, name):
    recipe, task = fixture_recipe(tmp_path)
    (task / "tests" / name).write_text("unbound verifier build")
    with pytest.raises(ContractError, match="verifier build"):
        _task_config(task, recipe.execution)


def test_container_witness_checks_bytes_deletions_and_executable_bits(tmp_path):
    store = ArtifactStore(tmp_path / "store", builtin_registry())
    artifact = store.put(PluginRef(name="proteus.file_tree", version="1"), {
        "main.py": FileContent(b"print(42)\n", executable=True)})
    candidate = store.materialize(artifact, tmp_path / "candidate")
    code = harbor_worker.VERIFY_TREE.replace("Path('/opt/proteus/candidate')", "Path(" + repr(str(candidate)) + ")")
    encoded = base64.b64encode(canonical_json(artifact).encode()).decode()
    def check():
        return subprocess.run([sys.executable, "-c", code, encoded], capture_output=True, text=True)
    result = check()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["loaded_artifact_hash"] == artifact.content_hash
    (candidate / "stale.py").write_text("pass")
    assert check().returncode != 0
    (candidate / "stale.py").unlink()
    (candidate / "main.py").chmod(0o644)
    assert check().returncode != 0


def test_cleanup_is_limited_to_exact_trial_labels_and_verifies_removal(monkeypatch):
    name = "proteus-" + "a" * 32
    calls, removed = [], set()
    def docker(*args, timeout=30):
        calls.append(args)
        if "--filter" in args:
            label = args[-1]
            assert label in {"label=com.docker.compose.project=" + name + suffix
                             for suffix in ("__env", "__verifier__trial")}
            if args[0] == "ps" and label not in removed:
                return label
            return ""
        assert args[:3] == ("rm", "--force", "--volumes")
        removed.update(args[3:])
        return ""
    monkeypatch.setattr(harbor_worker, "docker", docker)
    result = harbor_worker.cleanup_owned_resources(name)
    assert result["verified"] and len(removed) == 2
    assert not any("prune" in call for call in calls)
    with pytest.raises(ValueError, match="owned trial"):
        harbor_worker.cleanup_owned_resources("another-user-project")


def test_cleanup_daemon_failure_is_never_reported_as_success(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 1)
    monkeypatch.setattr(harbor_worker, "docker", fail)
    result = harbor_worker.cleanup_owned_resources("proteus-" + "b" * 32)
    assert not result["verified"] and len(result["errors"]) == 6


def test_docker_timeout_terminates_wrapper_children_holding_pipes(tmp_path, monkeypatch):
    wrapper = tmp_path / "docker"
    wrapper.write_text(f"#!{sys.executable}\nimport subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        harbor_worker.docker("info", timeout=0.2)
    assert time.monotonic() - started < 3


def test_verifier_not_started_is_zero_but_executed_opaque_verifier_is_unknown():
    assert _verifier_usage({}).model_calls == 0
    usage = _verifier_usage({"verifier": {"started_at": "2026-10-17T00:00:00Z",
                                        "finished_at": "2026-10-17T00:00:03Z"}})
    assert usage.wall_seconds == 3 and usage.model_calls is None and usage.cost_usd is None


@pytest.mark.skipif(not os.environ.get("PROTEUS_HARBOR_PYTHON"), reason="requires pinned Harbor runtime and live Docker")
def test_live_harbor_candidates_private_verifier_timeout_and_cleanup(tmp_path):
    python = os.environ["PROTEUS_HARBOR_PYTHON"]
    root = Path(os.environ.get("PROTEUS_HARBOR_STUDY", str(tmp_path / "live-study")))
    path = prepare(root, python, include_timeout=True)
    recipe, registry = load_recipe(path), builtin_registry()
    # Pin the checkout actually under test; the runtime and task tree have their own hashes.
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    lock = registry.make_lock(recipe, [SourcePin(name="proteus",
        repository="https://github.com/proteus-evolve/Proteus.git", revision=revision)])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root / "run", artifact_source=root / "artifact-store")
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    records = json.loads((root / "run/execution-0001.json").read_text())
    rollouts = records["rollouts"]
    assert [r["status"] for r in rollouts] == ["succeeded", "succeeded", "succeeded", "timed_out"], rollouts
    assert [r["output"] for r in rollouts[:3]] == [
        {"answer": 41, "obsolete": True}, {"answer": 42, "obsolete": False}, {"answer": 41, "obsolete": True}]
    assert [b["rows"][0]["score"] for b in records["evaluations"]] == [0, 1, 0, None]
    assert "PROTEUS_PRIVATE_VERIFIER_23" not in json.dumps(records)
    trials = list((root / "run/evaluations").glob("*/trials/*/private"))
    assert len(trials) == 4
    proofs = []
    for trial in trials:
        assert json.loads((trial / "cleanup.json").read_text())["verified"]
        response = json.loads((trial / "response.json").read_text())
        assert response["runtime"]["runtime_hash"] == identify_runtime(python)["runtime_hash"]
        proof = response["loaded_proof"]
        assert proof["image_id"].startswith("sha256:")
        assert proof["mount_destinations"] == ["/logs/agent", "/logs/artifacts"]
        assert proof["nano_cpus"] == 10**9 and proof["memory_bytes"] == 512 * 1024**2
        proofs.append(proof)
    assert len({p["container_id"] for p in proofs}) == 4
    assert len({p["loaded_artifact_hash"] for p in proofs}) == 3
    book = run_ledger(root / "run")
    assert book.balance()["charged"]["evaluations"] == 4
    assert book.balance()["active_slots"] == 0 and len(book.entries()) == 12
    assert book.balance()["charged"]["model_calls"] is None
    assert all(e["state"] in ("completed", "failed") for e in book.entries())
