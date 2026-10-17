"""Experimental integrity checks for the 1.0 development contracts."""

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from proteus.core.contracts import (
    ArtifactComponent, ArtifactRef, BudgetLimits, Candidate, DataRef, EvaluationBatch,
    EvaluationRow, MethodState, OperationStatus, Outcome, PluginRef, PluginState, Rollout,
    RuntimeIdentity, StageRequest, StageResult, StateRegistry, Usage, validate_records,
)
from proteus.core.serialization import Contract, ContractError, content_hash, read_json
from proteus.recipes import RunManifest, load_recipe
from proteus.recipes.config import EvaluationSpec, PluginSpec, StageSpec
from proteus.recipes.manifest import DependencyLock, DependencyPin, PluginLock, SourcePin


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "0" * 64
OTHER = "sha256:" + "1" * 64
PLUGIN = PluginRef(name="fixture", version="1")


def recipe():
    return load_recipe(ROOT / "recipes/examples/p01-contract.toml")


def lock_for(config):
    refs = {(ref.name, ref.version): ref for ref in config.plugin_refs()}
    return DependencyLock(
        python_version="3.12.0", platform="fixture", sources=(SourcePin(
            name="proteus", repository="https://example.invalid/proteus", revision="0" * 40),),
        plugins=tuple(PluginLock(plugin=ref, source="proteus", implementation_hash=DIGEST)
                      for ref in refs.values()),
    )


def records():
    request = StageRequest(operation_id="op", attempt_id="try-1", run_id="run", stage=PLUGIN,
                           role="fixture")
    candidate = Candidate(id="c1", run_id="run", created_by="op", created_attempt_id="try-1",
                          artifact=ArtifactRef(content_hash=DIGEST, codec=PLUGIN))
    rollout = Rollout(id="r1", run_id="run", operation_id="op", attempt_id="try-1",
                      candidate_id="c1", artifact_hash=DIGEST, task_id="task-a", task_version="v1",
                      split="train", execution_hash=DIGEST, runtime=RuntimeIdentity(runtime=PLUGIN),
                      status=Outcome.SUCCEEDED, output={"answer": [42]})
    batch = EvaluationBatch(
        id="eval", run_id="run", operation_id="op", attempt_id="try-1", candidate_id="c1",
        artifact_hash=DIGEST, evaluator=PLUGIN, protocol_hash=DIGEST, expected_rollout_ids=("r1",),
        rows=(EvaluationRow(rollout_id="r1", task_id="task-a", status=Outcome.SUCCEEDED, score=1),),
    )
    result = StageResult(operation_id="op", attempt_id="try-1", run_id="run",
                         status=OperationStatus.COMPLETED)
    return dict(run_id="run", requests=(request,), candidates=(candidate,), rollouts=(rollout,),
                evaluations=(batch,), results=(result,))


def test_public_records_round_trip_and_are_deeply_immutable():
    graph = records()
    validate_records(**graph)
    config = recipe()
    manifest = RunManifest.create(run_id="run", recipe=config, lock=lock_for(config))
    state = MethodState(run_id="run", method=PLUGIN, config_hash=config.config_hash,
                        state=PluginState(namespace="fixture", payload_version=1,
                                          payload={"nested": [1, {"hello": "世界"}]}),
                        pending_operations=graph["requests"])
    objects = [config, manifest, state, *(row for key, rows in graph.items()
                                        if key != "run_id" for row in rows)]
    for obj in objects:
        assert type(obj).from_json(obj.to_json()) == obj
        assert json.loads(obj.to_json()) == obj.to_dict()
        schema = type(obj).json_schema()
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(obj.to_dict())
    with pytest.raises(TypeError):
        state.state.payload["nested"][1]["hello"] = "changed"
    detached = state.to_dict()
    detached["state"]["payload"]["nested"][1]["hello"] = "changed"
    assert state.state.payload["nested"][1]["hello"] == "世界"
    assert Usage().cost_usd is None
    assert BudgetLimits(max_model_calls=0).max_model_calls == 0


@pytest.mark.parametrize("data", [
    {"model_calls": True}, {"model_calls": "2"}, {"model_calls": 1.5}, {"cost_usd": -1},
    {"cost_usd": float("nan")}, {"cost_usd": float("inf")}, {"cost_usd": 10**1000},
    {"schema_version": 2}, {"schema_version": True}, {"schema_version": 1.0}, {"unknown": 0},
])
def test_strict_values_and_versions(data):
    with pytest.raises(ContractError):
        Usage.from_dict(data)


@pytest.mark.parametrize("data", [
    '{"model_calls":1,"model_calls":2}', '{"cost_usd":NaN}', '{"cost_usd":Infinity}',
    '{"cost_usd":1e999}', '[]', 'null',
])
def test_json_reader_rejects_ambiguous_or_invalid_records(data):
    with pytest.raises(ContractError):
        Usage.from_json(data)


@pytest.mark.parametrize("path", ["../code.py", "/absolute", "a/./b", "a//b", "a\\b", "C:/code",
                                  "nul\x00file"])
def test_artifact_component_paths(path):
    with pytest.raises(ContractError):
        ArtifactComponent(path=path, content_hash=DIGEST, size_bytes=1)


def test_artifact_manifest_conflicts():
    component = ArtifactComponent(path="code", content_hash=DIGEST, size_bytes=1)
    with pytest.raises(ContractError, match="duplicate"):
        ArtifactRef(content_hash=DIGEST, codec=PLUGIN, components=(component, component))
    with pytest.raises(ContractError, match="parent directory"):
        ArtifactRef(content_hash=DIGEST, codec=PLUGIN,
                    components=(component, replace(component, path="code/main.py")))


@pytest.mark.parametrize("status", [Outcome.INFRA_ERROR, Outcome.INVALID_CANDIDATE, Outcome.TIMED_OUT,
                                   Outcome.SCORE_MISSING, Outcome.CANCELLED])
def test_unavailable_scores_are_not_zero(status):
    with pytest.raises(ContractError):
        EvaluationRow(rollout_id="r1", task_id="a", status=status, score=0)
    with pytest.raises(ContractError):
        EvaluationRow(rollout_id="r1", task_id="a", status=status, metrics={"accuracy": 0})
    row = EvaluationRow(rollout_id="r1", task_id="a", status=status)
    assert row.score is None


def test_batch_alignment_and_deterministic_complete_mean():
    batch = records()["evaluations"][0]
    zero = EvaluationRow(rollout_id="r2", task_id="task-b", status=Outcome.TASK_FAILED, score=0)
    both = replace(batch, expected_rollout_ids=("r1", "r2"), rows=(*batch.rows, zero))
    assert both.mean_score() == 0.5
    missing = replace(zero, status=Outcome.SCORE_MISSING, score=None)
    assert replace(both, rows=(*batch.rows, missing)).mean_score() is None
    for rows in (batch.rows, (zero, batch.rows[0]), (zero, zero)):
        with pytest.raises(ContractError):
            replace(both, rows=rows)
    with pytest.raises(ContractError, match="unknown"):
        EvaluationBatch.from_dict({**batch.to_dict(), "mean_score": 123})


@pytest.mark.parametrize("change,match", [
    (lambda g: {**g, "run_id": "other"}, "different run"),
    (lambda g: {**g, "candidates": ()}, "unknown candidate"),
    (lambda g: {**g, "requests": ()}, "matching request"),
    (lambda g: {**g, "rollouts": ()}, "unknown rollout"),
    (lambda g: {**g, "rollouts": (replace(g["rollouts"][0], artifact_hash=OTHER),)}, "artifact"),
    (lambda g: {**g, "rollouts": (replace(g["rollouts"][0], task_id="other"),)}, "candidate/task"),
    (lambda g: {**g, "rollouts": (replace(g["rollouts"][0], attempt_id="other"),)}, "attempt"),
    (lambda g: {**g, "rollouts": (replace(g["rollouts"][0], status=Outcome.INFRA_ERROR),)},
     "valid score"),
    (lambda g: {**g, "candidates": (replace(g["candidates"][0], parent_ids=("missing",)),)}, "parent"),
    (lambda g: {**g, "candidates": g["candidates"] * 2}, "duplicate candidate"),
])
def test_reference_graph_rejects_mixed_evidence(change, match):
    with pytest.raises(ContractError, match=match):
        validate_records(**change(records()))


def test_lineage_cycles_and_long_valid_lineage():
    graph = records()
    root = graph["candidates"][0]
    child = replace(root, id="c2", parent_ids=("c1",))
    validate_records(**{**graph, "candidates": (root, child)})
    with pytest.raises(ContractError, match="cycle"):
        validate_records(**{**graph, "candidates": (replace(root, parent_ids=("c2",)), child)})
    chain = [root]
    for i in range(2, 1500):
        chain.append(replace(root, id=f"c{i}", parent_ids=(f"c{i-1}",)))
    validate_records(**{**graph, "candidates": tuple(chain)})


def test_retry_identity_and_duplicate_completion():
    graph = records()
    retry = replace(graph["requests"][0], attempt_id="try-2")
    validate_records(**{**graph, "requests": (*graph["requests"], retry)})
    with pytest.raises(ContractError, match="retry changed"):
        validate_records(**{**graph, "requests": (*graph["requests"], replace(retry, role="other"))})
    with pytest.raises(ContractError, match="completed operation"):
        validate_records(**{**graph, "requests": (*graph["requests"], retry),
                            "results": (*graph["results"], replace(graph["results"][0],
                                                                   attempt_id="try-2"))})


def test_plugin_state_requires_registered_version_and_explicit_migration():
    @dataclass(frozen=True, kw_only=True)
    class V1(Contract):
        count: int

    @dataclass(frozen=True, kw_only=True)
    class V2(Contract):
        total: int

    registry = StateRegistry()
    state = PluginState(namespace="fixture", payload_version=1, payload={"count": 3})
    with pytest.raises(ContractError, match="unsupported"):
        registry.restore(state)
    registry.register("fixture", 1, V1)
    registry.register("fixture", 2, V2)
    assert registry.restore(state).count == 3
    with pytest.raises(ContractError, match="no migration"):
        registry.migrate(state, 2)
    registry.register_migration("fixture", 1, lambda old: V2(total=old.count))
    migrated = registry.migrate(state, 2)
    assert migrated.payload_version == 2 and registry.restore(migrated).total == 3
    assert state.payload_version == 1
    with pytest.raises(ContractError, match="already registered"):
        registry.register("fixture", 1, V1)
    with pytest.raises(ContractError):
        registry.restore(replace(state, payload={"count": True}))


def test_resolved_config_hash_defaults_overrides_and_strict_fields(tmp_path):
    config = recipe()
    path = tmp_path / "recipe.json"
    data = config.to_dict()
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_recipe(path) == config
    assert content_hash(dict(reversed(list(data.items())))) == config.config_hash
    assert load_recipe(path, overrides={"budget": {"max_model_calls": 5}}).budget.max_model_calls == 5
    assert load_recipe(path).budget.max_model_calls == 0
    for update in ({"budegt": {}}, {"budget": {"max_modle_calls": 1}},
                   {"actors": {"optimizer": {"unknown": True}}}, {"schema_version": 2}):
        with pytest.raises(ContractError):
            load_recipe(path, overrides=update)
    with pytest.raises(ContractError):
        replace(config, subject=replace(config.subject, actor="missing"))
    stage = StageSpec(id="reflect", operator=config.method, role="missing")
    with pytest.raises(ContractError):
        replace(config, stages=(stage,))


def test_protocol_and_capability_declarations():
    config = recipe()
    with pytest.raises(ContractError):
        replace(config.execution, state_policy="sequence")
    with pytest.raises(ContractError):
        replace(config.execution.environment, image_digest="ubuntu:latest")
    with pytest.raises(ContractError):
        EvaluationSpec(id="test", evaluator=config.method, tasks=config.tasks, purpose="test")
    with pytest.raises(ContractError):
        EvaluationSpec(id="test", evaluator=config.method, tasks=config.tasks, visibility="observed")
    spec = replace(config.method, requires=("batch",))
    config = replace(config, method=spec)
    capabilities = {(ref.name, ref.version): () for ref in config.plugin_refs()}
    with pytest.raises(ContractError, match="lacks capabilities"):
        config.check_capabilities(capabilities)
    capabilities[(spec.plugin.name, spec.plugin.version)] = ("batch",)
    config.check_capabilities(capabilities)
    config = replace(config, actors={"optimizer": replace(config.actors["optimizer"], session="native")})
    with pytest.raises(ContractError, match="session.resume"):
        config.check_capabilities(capabilities)


def test_manifest_integrity_resume_and_no_overwrite(tmp_path):
    config = recipe()
    lock = lock_for(config)
    manifest = RunManifest.create(run_id="run", recipe=config, lock=lock)
    path = manifest.write(tmp_path / "manifest.json")
    assert RunManifest.from_json(path.read_text()) == manifest
    assert manifest.write(path) == path
    manifest.assert_resume_compatible(config, lock)
    changed = replace(config, seed=config.seed + 1)
    with pytest.raises(ContractError, match="recipe"):
        manifest.assert_resume_compatible(changed, lock)
    with pytest.raises(ContractError, match="dependency"):
        manifest.assert_resume_compatible(config, replace(lock, python_version="3.13.0"))
    with pytest.raises(ContractError, match="overwrite"):
        RunManifest.create(run_id="other", recipe=config, lock=lock).write(path)
    assert RunManifest.from_json(path.read_text()) == manifest
    assert not list(tmp_path.glob(".manifest-*"))
    for section, field, value in (("recipe", "seed", 999), ("lock", "python_version", "3.14.0")):
        data = manifest.to_dict()
        data[section][field] = value
        with pytest.raises(ContractError, match="hash"):
            RunManifest.from_dict(data)
    with pytest.raises(ContractError, match="timezone"):
        replace(manifest, created_at="2026-10-17T00:00:00")


def test_dependency_locks_require_complete_exact_references():
    config = recipe()
    lock = lock_for(config)
    with pytest.raises(ContractError, match="not locked"):
        RunManifest.create(run_id="run", recipe=config, lock=replace(lock, plugins=lock.plugins[:-1]))
    with pytest.raises(ContractError):
        replace(lock.sources[0], revision="main")
    with pytest.raises(ContractError):
        DependencyPin(name="pytest", version=">=7")
    with pytest.raises(ContractError, match="duplicate package"):
        replace(lock, packages=(DependencyPin(name="My_Package", version="1.0"),
                                DependencyPin(name="my-package", version="1.1")))


def test_schema_generation_and_structural_constraints():
    schema = RunManifest.json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["enum"] == [1]
    assert schema["$defs"]["RecipeConfig"]["additionalProperties"] is False
    assert schema["$defs"]["BudgetLimits"]["properties"]["max_concurrency"]["minimum"] == 1
    assert read_json(json.dumps(schema)) == schema
    assert DataRef.json_schema()["properties"]["content_hash"]["pattern"].startswith("^sha256:")


def test_plugin_options_are_json_and_copied():
    options = {"nested": {"values": [1, 2]}}
    spec = PluginSpec(plugin=PLUGIN, options=options)
    options["nested"]["values"].append(3)
    assert spec.options["nested"]["values"] == (1, 2)
    for options in ({1: "value"}, {"path": Path("a")}, {"bad": float("nan")}):
        with pytest.raises(ContractError):
            PluginSpec(plugin=PLUGIN, options=options)


def test_overflow_rejected_by_json_reader_itself():
    with pytest.raises(ContractError):
        read_json('{"value":1e999}')


def test_unresolved_model_and_unsupported_evidence_claim():
    config = recipe()
    with pytest.raises(ContractError, match="resolve model"):
        replace(config.actors["optimizer"], model="${MODEL}")
    with pytest.raises(ContractError, match="evidence"):
        replace(config.reference, status="benchmark_validated")


def test_manifest_concurrent_publication_is_never_partially_overwritten(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    config = recipe()
    lock = lock_for(config)
    manifests = [RunManifest.create(run_id=f"run-{i}", recipe=config, lock=lock) for i in range(8)]
    path = tmp_path / "manifest.json"

    def publish(manifest):
        try:
            manifest.write(path)
            return manifest.run_id
        except ContractError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [run for run in pool.map(publish, manifests) if run is not None]
    assert len(winners) == 1
    saved = RunManifest.from_json(path.read_text())
    assert saved.run_id == winners[0]
    assert saved in manifests
    assert not list(tmp_path.glob(".manifest-*"))
