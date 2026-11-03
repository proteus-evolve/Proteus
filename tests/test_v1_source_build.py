"""Source-build provenance, cache validity, failure accounting and live Docker gate."""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from proteus.artifacts.codecs import FileDelta, FileWrite
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import OperationStatus
from proteus.core.methods import RunServices
from proteus.core.serialization import ContractError, content_hash
from proteus.evaluation.runners import InvalidCandidate, LocalJsonEnvironment, PythonSubject, PythonSubjectOptions
from proteus.examples.harbor_evaluation import IMAGE
from proteus.examples.harbor_source_build import prepare
from proteus.execution import builds
from proteus.execution.builds import BuildReceipt, SourceBuildSpec, build_key
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import plugin, register, run, setup
from test_v1_ledger import run_ledger


BUILDER = {"buildkit": "fixture-v1", "server": "fixture"}


def services_fixture(tmp_path):
    path = prepare(tmp_path / "study", sys.executable, runtime={"runtime_hash": content_hash("fixture")})
    recipe, registry = load_recipe(path), builtin_registry()
    recipe = registry.resolve(recipe)
    services = RunServices(run_id="build-study", recipe=recipe, registry=registry, root=tmp_path / "run")
    services.artifacts.copy_from(ArtifactStore(path.parent / "artifact-store", registry), recipe.subject.initial_artifact)
    candidate = services.initial_candidate()
    return services, candidate, SourceBuildSpec.from_dict(recipe.execution.environment.backend.options["source_build"])


def fake_docker(monkeypatch):
    images, commands = {}, []
    monkeypatch.setattr(builds, "builder_identity", lambda: BUILDER)
    def dispatch(args, log, timeout):
        commands.append(args)
        context = Path(args[-1])
        source = context / "source"
        assert (source / "build.py").is_file()
        assert not (context / ".dockerignore").exists()
        image = content_hash([args, len(commands)])
        labels = dict(args[index + 1].split("=", 1) for index, value in enumerate(args) if value == "--label")
        images[image] = {"Id": image, "Config": {"Labels": labels}, "Os": "linux", "Architecture": "amd64"}
        Path(args[args.index("--iidfile") + 1]).write_text(image)
        log.write_text("fixture build log")
    def docker(*args):
        assert args[:2] == ("image", "inspect")
        if args[2] not in images:
            raise subprocess.CalledProcessError(1, args, stderr="Error: No such image")
        return json.dumps([images[args[2]]])
    monkeypatch.setattr(builds, "run_build", dispatch)
    monkeypatch.setattr(builds, "docker", docker)
    return images, commands


def execute(services, candidate, spec, number):
    return services.builds.execute(candidate.artifact, spec, IMAGE,
        operation_id="evaluation", attempt_id="attempt-1", trial_id=str(number))


def test_build_key_binds_source_changes_and_every_build_input(tmp_path):
    services, candidate, spec = services_fixture(tmp_path)
    key = build_key(candidate.artifact, spec, IMAGE, BUILDER)
    variants = [FileDelta(base_hash=candidate.artifact.content_hash, delete=("obsolete.txt",)),
        FileDelta(base_hash=candidate.artifact.content_hash, put=(FileWrite.from_bytes("data-original.json", b"42"),)),
        FileDelta(base_hash=candidate.artifact.content_hash, delete=("data-original.json",),
            put=(FileWrite.from_bytes("renamed.json", b'{"answer":41}'),))]
    for delta in variants:
        ref = services.artifacts.apply_delta(candidate.artifact, delta)
        assert build_key(ref, spec, IMAGE, BUILDER) != key
    for field, value in (("commands", ("python3 another.py",)), ("platform", "linux/arm64"),
                         ("network", "default"), ("reuse", False), ("timeout_seconds", 50),
                         ("builder_image", "other@sha256:" + "a" * 64)):
        assert build_key(candidate.artifact, replace(spec, **{field: value}), IMAGE, BUILDER) != key
    assert build_key(candidate.artifact, spec, "other@sha256:" + "b" * 64, BUILDER) != key
    assert build_key(candidate.artifact, spec, IMAGE, {"buildkit": "changed"}) != key


@pytest.mark.parametrize("build_hash", [None, content_hash("another build")])
def test_build_definition_must_match_locked_environment(tmp_path, build_hash):
    services, _, _ = services_fixture(tmp_path)
    recipe = replace(services.recipe, execution=replace(services.recipe.execution,
        environment=replace(services.recipe.execution.environment, build_hash=build_hash)))
    with pytest.raises(ContractError, match="build_hash"):
        services.registry.resolve(recipe)


def test_builder_identity_uses_supported_inspect_and_ignores_last_activity(monkeypatch):
    def docker(*args):
        if args[0] == "version":
            return json.dumps(dict(Version="28", ApiVersion="1.51", Os="linux", Arch="amd64"))
        if args == ("buildx", "version"):
            return "buildx v0.29"
        assert args == ("buildx", "inspect", "default")
        return "Name: default\nDriver: docker\nLast Activity: anything\nNodes:\nBuildKit version: v0.25\nPlatforms: linux/amd64, linux/386"
    monkeypatch.setattr(builds, "docker", docker)
    identity = builds.builder_identity()
    assert identity["buildkit"] == "v0.25" and "anything" not in json.dumps(identity)


def test_cache_reuses_only_inspected_images_and_rebuilds_missing_image(tmp_path, monkeypatch):
    services, candidate, spec = services_fixture(tmp_path)
    images, commands = fake_docker(monkeypatch)
    first = execute(services, candidate, spec, 1)
    second = execute(services, candidate, spec, 2)
    assert first.image_id == second.image_id and second.cache_hit and len(commands) == 1
    assert second.cached_from == content_hash(first)
    assert first.usage.model_calls is None and second.usage.model_calls == 0
    images.clear()
    third = execute(services, candidate, spec, 3)
    assert not third.cache_hit and third.image_id != first.image_id and len(commands) == 2
    assert services.ledger.balance()["active_slots"] == 0
    assert len(services.ledger.entries()) == 3
    assert all(e["state"] == "completed" for e in services.ledger.entries())
    assert len(list((services.root / "private/builds/cache").glob("*/*.json"))) == 2


def test_cache_image_provenance_mismatch_fails_closed(tmp_path, monkeypatch):
    services, candidate, spec = services_fixture(tmp_path)
    images, commands = fake_docker(monkeypatch)
    first = execute(services, candidate, spec, 1)
    images[first.image_id]["Config"]["Labels"]["org.proteus.source"] = content_hash("wrong")
    with pytest.raises(ContractError, match="provenance"):
        execute(services, candidate, spec, 2)
    assert len(commands) == 1 and services.builds.records[-1].status == "failed"


def test_timeout_kills_buildx_child_even_when_wrapper_exits(tmp_path):
    marker = tmp_path / "escaped-child"
    child = ("import signal,time; from pathlib import Path; signal.signal(signal.SIGINT,signal.SIG_IGN);"
             f"time.sleep(1); Path({str(marker)!r}).write_text('escaped')")
    wrapper = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        builds.run_build([sys.executable, "-c", wrapper], tmp_path / "build.log", 0.3)
    time.sleep(1)
    assert not marker.exists()


@pytest.mark.parametrize("message,error", [
    ('ERROR: process "/bin/sh -eu -c make" did not complete successfully: exit code: 2', InvalidCandidate),
    ("Cannot connect to the Docker daemon", RuntimeError),
])
def test_build_command_failure_is_distinct_from_infrastructure_failure(tmp_path, message, error):
    with pytest.raises(error):
        builds.run_build([sys.executable, "-c", f"print({message!r}); raise SystemExit(1)"], tmp_path / "build.log", 5)


def test_reuse_false_bypasses_receipts_and_layers(tmp_path, monkeypatch):
    services, candidate, spec = services_fixture(tmp_path)
    spec = replace(spec, reuse=False)
    services.recipe = replace(services.recipe, execution=replace(services.recipe.execution,
        environment=replace(services.recipe.execution.environment, build_hash=content_hash(spec))))
    _, commands = fake_docker(monkeypatch)
    first, second = execute(services, candidate, spec, 1), execute(services, candidate, spec, 2)
    assert not first.cache_hit and not second.cache_hit and len(commands) == 2
    assert all("--no-cache" in command for command in commands)


@pytest.mark.parametrize("failure", [InvalidCandidate("compile failed"), subprocess.TimeoutExpired("build", 1)])
def test_failed_build_is_charged_and_never_cached(tmp_path, monkeypatch, failure):
    services, candidate, spec = services_fixture(tmp_path)
    fake_docker(monkeypatch)
    def fail(*args):
        raise failure
    monkeypatch.setattr(builds, "run_build", fail)
    with pytest.raises(type(failure)):
        execute(services, candidate, spec, 1)
    assert not list((services.root / "private/builds/cache").glob("*/*.json"))
    receipt = services.builds.records[-1]
    assert receipt.status == "failed" and receipt.usage.model_calls is None and receipt.usage.wall_seconds > 0
    assert services.ledger.entries()[0]["state"] == "failed"
    assert BuildReceipt.from_json(next((services.root / "private/builds/attempts").glob("*/receipt.json")).read_text()) == receipt
    with pytest.raises(ContractError):
        execute(services, candidate, spec, 1)  # same identity never implicitly retries


@pytest.mark.parametrize("failed", [False, True])
def test_evaluation_counts_build_usage_once_in_success_and_failure(tmp_path, monkeypatch, failed):
    registry, store, _, recipe = setup(tmp_path)
    spec = SourceBuildSpec(commands=("python3 build.py",))
    class Environment:
        @contextmanager
        def open(self, artifact, store, root, execution, *, build):
            build(artifact, spec, IMAGE)
            with LocalJsonEnvironment().open(artifact, store, root, execution) as lease:
                yield lease
    register(registry, "fixture.build_environment", "environment", Environment,
             capabilities=("state.independent", "environment.source_build"))
    register(registry, "fixture.build_subject", "subject", lambda: PythonSubject(PythonSubjectOptions()))
    recipe = replace(recipe, subject=replace(recipe.subject, runner=plugin("fixture.build_subject")),
        execution=replace(recipe.execution, environment=replace(recipe.execution.environment,
        backend=plugin("fixture.build_environment"), image_digest=IMAGE, build_hash=content_hash(spec))))
    # Use the real build ledger/service and fake only the Docker boundary.
    fake_docker(monkeypatch)
    def dispatch(args, log, timeout):
        if failed:
            raise InvalidCandidate("injected compile failure")
        Path(args[args.index("--iidfile") + 1]).write_text(content_hash("built"))
    monkeypatch.setattr(builds, "run_build", dispatch)
    monkeypatch.setattr(builds, "verify_image", lambda *args: None)
    result, records, root = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED
    book = run_ledger(root)
    entries = [e for e in book.entries() if e["identity"]["metadata"]["kind"] == "source_build"]
    assert len(entries) == 2
    assert all(e["state"] == ("failed" if failed else "completed") for e in entries)
    assert records["results"][-1]["usage"]["wall_seconds"] == pytest.approx(book.balance()["charged"]["wall_seconds"])
    if failed:
        assert all(row["score"] is None and row["status"] == "invalid_candidate" for row in records["evaluations"][0]["rows"])


@pytest.mark.skipif(not os.environ.get("PROTEUS_HARBOR_PYTHON"), reason="requires pinned Harbor runtime and live Docker")
def test_live_harbor_source_build_cache_and_pristine_verifier(tmp_path):
    root = Path(os.environ.get("PROTEUS_HARBOR_STUDY", str(tmp_path))) / "source-build"
    path = prepare(root, os.environ["PROTEUS_HARBOR_PYTHON"])
    recipe, registry = load_recipe(path), builtin_registry()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    lock = registry.make_lock(recipe, [SourcePin(name="proteus", repository="https://github.com/proteus-evolve/Proteus.git", revision=revision)])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root / "run", artifact_source=root / "artifact-store")
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    records = json.loads((root / "run/execution-0001.json").read_text())
    assert [r["status"] for r in records["rollouts"]] == ["succeeded"] * 3 + ["invalid_candidate"], records["rollouts"]
    assert [r["output"] for r in records["rollouts"][:3]] == [
        {"answer": 41, "obsolete": True}, {"answer": 42, "obsolete": False}, {"answer": 41, "obsolete": True}]
    assert [b["rows"][0]["score"] for b in records["evaluations"]] == [0, 1, 0, None]
    receipts = [BuildReceipt.from_json(p.read_text()) for p in (root / "run/private/builds/attempts").glob("*/receipt.json")]
    assert len(receipts) == 4 and sum(r.cache_hit for r in receipts) == 1
    assert len({r.image_id for r in receipts if r.status == "completed"}) == 2
    trials = list((root / "run/evaluations").glob("*/trials/*/private"))
    assert len(trials) == 3  # broken build never dispatches a subject/verifier trial
    agent_images, verifier_images = set(), set()
    for trial in trials:
        assert json.loads((trial / "cleanup.json").read_text())["verified"]
        response = json.loads((trial / "response.json").read_text())
        agent, verifier = response["loaded_proof"], response["verifier_proof"]
        assert agent["image_id"] != verifier["image_id"]
        assert agent["container_id"] != verifier["container_id"]
        assert agent["build_receipt_hash"] in {content_hash(r) for r in receipts}
        agent_images.add(agent["image_id"])
        verifier_images.add(verifier["image_id"])
    assert len(agent_images) == 2 and len(verifier_images) == 1
    assert "PROTEUS_PRIVATE_VERIFIER_23" not in json.dumps(records)
    book = run_ledger(root / "run")
    assert len(book.entries()) == 16 and book.balance()["active_slots"] == 0
    assert all(e["state"] in ("completed", "failed") for e in book.entries())
    broken = records["rollouts"][-1]
    undispatched = [e for e in book.entries() if e["identity"]["metadata"].get("trial_id") == broken["id"]
                   and e["identity"]["metadata"]["kind"] in ("subject", "backend_verifier")]
    assert len(undispatched) == 2
    assert all(e["settlement"]["actual"]["model_calls"] == 0 for e in undispatched)
