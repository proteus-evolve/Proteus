"""Run real plugin/control-flow boundaries with offline actors and local HTTP fixtures."""

import json
import threading
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from proteus.core.contracts import BudgetLimits, OperationStatus, PluginRef, RuntimeIdentity, Usage
from proteus.core.methods import ActorResult, MethodResult, RunServices
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.serialization import Contract, ContractError, content_hash
from proteus.recipes import load_recipe
from proteus.recipes.builtins import Text, builtin_registry
from proteus.recipes.config import ActorSpec, PluginSpec, StageSpec
from proteus.recipes.manifest import DependencyPin, SourcePin
from proteus.recipes.runner import run_recipe


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "0" * 64
SOURCE = SourcePin(name="proteus", repository="https://example.invalid/fixture", revision="0" * 40)


def stage_recipe():
    return load_recipe(ROOT / "recipes/examples/stages.toml")


def native_recipe():
    return load_recipe(ROOT / "recipes/native/minimal.toml")


def run(config, registry, root, **kwargs):
    lock = registry.make_lock(config, [SOURCE])
    return run_recipe(config, registry=registry, lock=lock, root=root, **kwargs)


def execution(root, attempt=1):
    return json.loads((root / f"execution-{attempt:04d}.json").read_text())


def plugin(name, kind, factory, *, options=EmptyOptions, inputs=None, outputs=None, capabilities=()):
    return Plugin(ref=PluginRef(name=name, version="1"), kind=kind, options=options, factory=factory,
                  inputs=inputs or {}, outputs=outputs or {}, capabilities=capabilities,
                  implementation_hash=DIGEST, source="proteus")


def test_sequence_runs_typed_stage_without_native_prompt_injection(tmp_path):
    registry = builtin_registry()
    recipe = stage_recipe()
    resolved = registry.resolve(recipe)
    assert registry.resolve(resolved) == resolved
    result = run(recipe, registry, tmp_path / "run")
    assert result.status == OperationStatus.COMPLETED
    records = execution(tmp_path / "run")
    assert records["actor_calls"][0]["request"]["system"] == ""
    assert records["actor_calls"][0]["request"]["prompt"] == "Inspect this scoped candidate."
    assert records["native_prompts"] == []
    assert records["results"][0]["usage"]["model_calls"] == 0
    assert records["values"][-1]["ref"] == result.outputs["proposal"].to_dict()


@dataclass(frozen=True, kw_only=True)
class SelfAssessment(Contract):
    supported: bool
    reason: str


def test_third_party_self_evaluator_stage_without_core_edits(tmp_path):
    registry = builtin_registry()
    registry.register_data("study.self_assessment@1", SelfAssessment)

    class SelfEvaluate:
        def run(self, context, inputs):
            return {"evidence": SelfAssessment(supported=bool(inputs["answer"].text), reason="fixture")}

    registry.register(plugin("study.self_eval", "stage", lambda opts: SelfEvaluate(),
        inputs={"answer": "proteus.actor_result@1"}, outputs={"evidence": "study.self_assessment@1"}))
    recipe = stage_recipe()
    stage = StageSpec(id="self_evaluator", role="optimizer",
        operator=PluginSpec(plugin=PluginRef(name="study.self_eval", version="1")),
        read_scopes=("proteus.actor_result@1",), write_scopes=("study.self_assessment@1",))
    options = dict(recipe.method.options)
    options["steps"] = [*options["steps"], {"stage": "self_evaluator", "inputs": {"answer": "proposal"},
                                         "outputs": {"evidence": "self_evidence"}}]
    options["outputs"] = {"proposal": "proposal", "self_evidence": "self_evidence"}
    options["iterations"] = 3
    options["stop_when"] = {"slot": "self_evidence", "field": "supported", "equals": True}
    recipe = replace(recipe, stages=(*recipe.stages, stage), method=replace(recipe.method, options=options))
    result = run(recipe, registry, tmp_path / "run")
    assert result.status == OperationStatus.COMPLETED
    assert result.outputs["self_evidence"].kind == "study.self_assessment@1"
    assert len(execution(tmp_path / "run")["requests"]) == 2  # conditional stop, not three iterations
    assert recipe.evaluations == ()  # self evidence did not replace a formal evaluator


def test_custom_method_controls_multiple_stage_calls_and_outputs(tmp_path):
    registry = builtin_registry()

    class Explore:
        def run(self, services, restored_state):
            outputs = {}
            for index in range(2):
                value = services.values.publish("proteus.text@1", Text(text=f"candidate-{index}"))
                result = services.stage("propose", {"prompt": value})
                outputs[str(index)] = result.outputs["response"]
            return MethodResult(outputs=outputs)

    registry.register(plugin("study.explore", "method", lambda opts: Explore()))
    recipe = replace(stage_recipe(), method=PluginSpec(plugin=PluginRef(name="study.explore", version="1")))
    result = run(recipe, registry, tmp_path / "run")
    assert result.status == OperationStatus.COMPLETED and set(result.outputs) == {"0", "1"}
    assert len(execution(tmp_path / "run")["requests"]) == 2


@pytest.mark.parametrize("change", [
    lambda r: replace(r, method=replace(r.method, options={"unknown": True})),
    lambda r: replace(r, actors={"optimizer": replace(r.actors["optimizer"], session="handoff")}),
    lambda r: replace(r, stages=(replace(r.stages[0], read_scopes=()),)),
    lambda r: replace(r, stages=(replace(r.stages[0], output_types={"response": "unknown"}),)),
    lambda r: replace(r, stages=(replace(r.stages[0], tools=("shell",)),)),
    lambda r: replace(r, method=replace(r.method, plugin=PluginRef(name="proteus.sequence", version="999"))),
    lambda r: replace(r, budget=BudgetLimits(max_cost_usd=1)),
    lambda r: replace(r, method=replace(r.method, options={**r.method.options,
        "initial": {"question": {"kind": "proteus.text@1", "value": {"typo": "x"}}}})),
])
def test_bad_config_rejected_before_run_directory_or_actor_work(tmp_path, change):
    registry = builtin_registry()
    with pytest.raises(ContractError):
        run(change(stage_recipe()), registry, tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_plugin_kind_duplicate_and_implementation_lock(tmp_path):
    registry = builtin_registry()
    config = stage_recipe()
    lock = registry.make_lock(config, [SOURCE])
    with pytest.raises(ContractError, match="expected method"):
        registry.resolve(replace(config, method=config.actors["optimizer"].runtime))
    definition = registry.get(config.method.plugin)
    with pytest.raises(ContractError, match="already registered"):
        registry.register(definition)
    changed = replace(lock, plugins=(replace(lock.plugins[0], implementation_hash=DIGEST), *lock.plugins[1:]))
    with pytest.raises(ContractError, match="differ from lock"):
        run_recipe(config, registry=registry, lock=changed, root=tmp_path / "run")
    missing = replace(lock, packages=(DependencyPin(name="proteus-nonexistent-fixture", version="1.0"),))
    with pytest.raises(ContractError, match="dependency"):
        registry.check_lock(config, missing)


def test_role_prompt_requires_exact_content_and_is_recorded(tmp_path):
    registry = builtin_registry()
    recipe = stage_recipe()
    digest = content_hash("Research carefully.")
    recipe = replace(recipe, actors={"optimizer": replace(recipe.actors["optimizer"], prompt_hash=digest)})
    with pytest.raises(ContractError, match="prompt content"):
        run(recipe, registry, tmp_path / "missing")
    with pytest.raises(ContractError, match="differs"):
        run(recipe, registry, tmp_path / "changed", prompts={digest: "Other."})
    result = run(recipe, registry, tmp_path / "run", prompts={digest: "Research carefully."})
    assert result.status == OperationStatus.COMPLETED
    assert execution(tmp_path / "run")["actor_calls"][0]["request"]["system"] == "Research carefully."


@dataclass(frozen=True, kw_only=True)
class FailingOptions(Contract):
    fail_first: bool = True


def paid_actor_registry():
    registry = builtin_registry()

    class PaidActor:
        model_call_bound = 1

        def __init__(self, options):
            self.options, self.calls = options, 0

        def execute(self, spec, request, workspace):
            self.calls += 1
            if self.options.fail_first and self.calls == 1:
                raise RuntimeError("provider failed before reporting usage")
            return ActorResult(text=request.prompt, runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
                               usage=Usage(model_calls=1))

    registry.register(plugin("fixture.paid", "actor", PaidActor, options=FailingOptions,
                             capabilities=("actor.execute", "session.fresh", "calls.bounded")))
    recipe = stage_recipe()
    recipe = replace(recipe, budget=BudgetLimits(max_model_calls=2),
        actors={"optimizer": ActorSpec(runtime=PluginSpec(plugin=PluginRef(name="fixture.paid", version="1")))},
        stages=(replace(recipe.stages[0], failure_policy="retry", max_attempts=2),))
    return registry, recipe


def test_retry_keeps_unknown_usage_and_reservation(tmp_path):
    registry, recipe = paid_actor_registry()
    result = run(recipe, registry, tmp_path / "success")
    assert result.status == OperationStatus.COMPLETED
    rows = execution(tmp_path / "success")
    assert len(rows["actor_calls"]) == 2
    assert rows["results"][0]["usage"]["model_calls"] is None
    assert rows["results"][1]["usage"]["model_calls"] == 1
    result = run(replace(recipe, budget=BudgetLimits(max_model_calls=1)), registry, tmp_path / "capped")
    assert result.status == OperationStatus.FAILED
    assert "budget exhausted" in result.diagnostic
    assert len(execution(tmp_path / "capped")["actor_calls"]) == 1


def test_output_type_failure_and_record_policy(tmp_path):
    registry = builtin_registry()

    class BadStage:
        def run(self, context, inputs):
            return {"response": Text(text="wrong output schema")}

    registry.register(plugin("fixture.bad_stage", "stage", lambda opts: BadStage(),
        inputs={"prompt": "proteus.text@1"}, outputs={"response": "proteus.actor_result@1"}))
    recipe = stage_recipe()
    spec = replace(recipe.stages[0], failure_policy="record",
                   operator=PluginSpec(plugin=PluginRef(name="fixture.bad_stage", version="1")))
    result = run(replace(recipe, stages=(spec,)), registry, tmp_path / "run")
    assert result.status == OperationStatus.FAILED
    assert "output type mismatch" in result.diagnostic
    assert len(execution(tmp_path / "run")["values"]) == 1  # input only; no partial result


def test_operation_reuse_and_input_mismatch(tmp_path):
    registry = builtin_registry()
    recipe = registry.resolve(stage_recipe())
    services = RunServices(run_id="fixture", recipe=recipe, registry=registry, root=tmp_path)
    ref = services.values.publish("proteus.text@1", Text(text="hello"))
    with pytest.raises(ContractError, match="port mismatch"):
        services.stage("propose", {})
    services.stage("propose", {"prompt": ref}, operation_id="one")
    with pytest.raises(ContractError, match="already submitted"):
        services.stage("propose", {"prompt": ref}, operation_id="one")
    assert len(services.actor_calls) == 1


def test_two_roles_share_runtime_but_have_separate_settings_and_budget(tmp_path):
    registry, recipe = paid_actor_registry()
    actor = replace(recipe.actors["optimizer"], runtime=replace(recipe.actors["optimizer"].runtime,
                    options={"fail_first": False}), budget=BudgetLimits(max_model_calls=1))
    second = replace(recipe.stages[0], id="reflect", role="reflector")
    options = dict(recipe.method.options)
    options["steps"] = [*options["steps"], {"stage": "reflect", "inputs": {"prompt": "question"},
                                         "outputs": {"response": "reflection"}}]
    recipe = replace(recipe, actors={"optimizer": replace(actor, model="one"),
                                    "reflector": replace(actor, model="two")},
        stages=(*recipe.stages, second), method=replace(recipe.method, options=options))
    result = run(recipe, registry, tmp_path / "run")
    assert result.status == OperationStatus.COMPLETED
    assert [row["role"] for row in execution(tmp_path / "run")["actor_calls"]] == ["optimizer", "reflector"]


def test_chat_http_transport_and_usage(tmp_path, monkeypatch):
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"model": "resolved-model", "choices": [{"message": {"content": "answer"}}],
                                        "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("FIXTURE_CHAT_KEY", "private-fixture-value")
    registry = builtin_registry()
    actor = ActorSpec(model="requested-model", model_settings={"temperature": 0},
        runtime=PluginSpec(plugin=PluginRef(name="proteus.chat", version="1"), options={
            "base_url": f"http://127.0.0.1:{server.server_port}", "key_env": "FIXTURE_CHAT_KEY"}))
    recipe = replace(stage_recipe(), actors={"optimizer": actor}, budget=BudgetLimits(max_model_calls=1))
    try:
        result = run(recipe, registry, tmp_path / "run")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert result.status == OperationStatus.COMPLETED
    assert captured[0]["model"] == "requested-model" and captured[0]["temperature"] == 0
    records = execution(tmp_path / "run")
    actor_result = records["actor_calls"][0]["result"]
    assert actor_result["runtime"]["model"] == "resolved-model"
    assert actor_result["usage"]["input_tokens"] == 5
    assert "private-fixture-value" not in json.dumps(records)


def test_native_recipe_matches_legacy_execution_and_resume(tmp_path):
    from proteus.core.episode import RunConfig, run as run_legacy
    from proteus.adapters.minimal import MinimalHarness
    from proteus.core.disposition import review
    from proteus.core.goal import EvaluatorSpec, GoalConfig, Visibility
    from proteus.core.evaluators import surface_units

    registry = builtin_registry()
    recipe = native_recipe()
    root = tmp_path / "recipe"
    result = run(recipe, registry, root)
    assert result.status == OperationStatus.COMPLETED
    records = execution(root)
    native = next(row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.native_summary@1")
    adapter = MinimalHarness()
    notes = next(surface for surface in adapter.surfaces() if surface.name == "notes")
    legacy = run_legacy(RunConfig(name=recipe.name, adapter=adapter, disposition=review("notes"),
        goal=GoalConfig.of(evaluators=(EvaluatorSpec(name="units:notes", run=surface_units(notes),
                                                   kind="measurement", visibility=Visibility.OBSERVE),)),
        root=tmp_path / "legacy" / "workspace", model="", episodes=2, max_turns=20))
    assert native["episodes_complete"] == legacy.episodes_complete == 2
    assert native["counters"] == legacy.counters
    assert [row["results"] for row in native["eval_history"]] == [row["results"] for row in legacy.eval_history]
    assert len(records["native_inputs"]) == 8
    assert all(row["completion"] == "returned" for row in records["native_inputs"])
    assert len(records["native_prompts"]) == 2
    assert "Proteus epistemic protocol" in records["native_prompts"][0]["prompts"]["act"]
    assert "Feedback on your last episode" in records["native_prompts"][1]["prompts"]["act"]
    before = (root / "execution-0001.json").read_bytes()
    resumed = run(recipe, registry, root, resume=True)
    assert resumed.status == OperationStatus.COMPLETED
    assert execution(root, 2)["native_prompts"] == []
    assert execution(root, 2)["native_inputs"] == records["native_inputs"]
    assert (root / "execution-0001.json").read_bytes() == before
    with pytest.raises(ContractError, match="resolved recipe"):
        run(replace(recipe, seed=1), registry, root, resume=True)


def test_native_failed_episode_resumes_exact_durable_checkpoint(tmp_path, monkeypatch):
    from proteus.adapters.minimal import MinimalHarness
    from proteus.core.adapter import EpisodeResult
    original = MinimalHarness.run_episode
    failed = []

    def fail_second(self, spec):
        if spec.episode == 2 and not failed:
            failed.append(True)
            return EpisodeResult(episode=2, ok=False, error="injected provider outage")
        return original(self, spec)

    monkeypatch.setattr(MinimalHarness, "run_episode", fail_second)
    registry = builtin_registry()
    root = tmp_path / "run"
    assert run(native_recipe(), registry, root).status == OperationStatus.FAILED
    assert run(native_recipe(), registry, root, resume=True).status == OperationStatus.COMPLETED
    assert [row["episode"] for row in execution(root, 2)["native_prompts"]] == [2]


def test_resume_needs_manifest_and_native_session_mismatch_rejected(tmp_path):
    registry = builtin_registry()
    with pytest.raises(ContractError, match="existing manifest"):
        run(stage_recipe(), registry, tmp_path / "stage", resume=True)
    recipe = native_recipe()
    with pytest.raises(ContractError):
        registry.resolve(replace(recipe, actors={"agent": replace(recipe.actors["agent"], session="native")}))


def test_prompt_observer_cannot_mutate_native_prompts(tmp_path):
    from proteus.adapters.minimal import MinimalHarness
    from proteus.core.disposition import NEUTRAL
    from proteus.core.episode import RunConfig, run as run_legacy
    from proteus.core.goal import GoalConfig

    observed = []

    def observer(episode, prompts):
        with pytest.raises(TypeError):
            prompts["act"] = "altered"
        observed.append(dict(prompts))

    result = run_legacy(RunConfig(name="observer", adapter=MinimalHarness(), disposition=NEUTRAL,
        goal=GoalConfig.no_goal(), root=tmp_path / "run", model="", episodes=1, on_episode_prompts=observer))
    assert result.episodes_complete == 1 and len(observed) == 1


def test_coding_harness_actor_has_fresh_workspace_and_frozen_active_tree(tmp_path, monkeypatch):
    from proteus.adapters.codex import CodexHarness
    from proteus.core.adapter import ActionEvent, EpisodeResult

    seen = []

    def seed(self, root, rng_seed=0):
        (root / "source.txt").write_text("initial")

    def episode(self, spec):
        assert spec.phases == ("act",)
        assert spec.active_root != spec.root / "harness"
        assert (spec.active_root / "source.txt").read_text() == "initial"
        assert (spec.root / "harness" / "source.txt").read_text() == "initial"
        assert "epistemic protocol" not in spec.phase_prompts["act"]
        (spec.root / "harness" / "source.txt").write_text("changed")
        seen.append(spec.root)
        return EpisodeResult(episode=1, ok=True, turns=1)

    monkeypatch.setattr(CodexHarness, "seed", seed)
    monkeypatch.setattr(CodexHarness, "run_episode", episode)
    monkeypatch.setattr(CodexHarness, "read_trace", lambda self, root, ep: [
        ActionEvent(turn=1, phase="act", tool="file_change", text="changed source")])
    registry = builtin_registry()
    recipe = stage_recipe()
    actor = ActorSpec(model="fixture-model", runtime=PluginSpec(
        plugin=PluginRef(name="proteus.harness.codex", version="1")))
    recipe = replace(recipe, actors={"optimizer": actor}, budget=BudgetLimits(),
                     method=replace(recipe.method, options={**recipe.method.options, "iterations": 2}))
    result = run(recipe, registry, tmp_path / "run")
    assert result.status == OperationStatus.COMPLETED
    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(row["result"]["usage"]["model_calls"] is None for row in execution(tmp_path / "run")["actor_calls"])


def test_native_staged_repair_preserves_last_valid_runtime(tmp_path, monkeypatch):
    from proteus.adapters.codex import CodexHarness
    from proteus.core.adapter import EpisodeResult

    seen = []

    def seed(self, root, rng_seed=0):
        root.mkdir(parents=True, exist_ok=True)
        (root / "source.txt").write_text("initial")
        (root / "AGENTS.md").write_text("Fixture coding agent")

    def episode(self, spec):
        candidate = spec.root / "harness" / "source.txt"
        seen.append(((spec.active_root / "source.txt").read_text(), candidate.read_text()))
        candidate.write_text("invalid" if spec.episode == 1 else "repaired")
        return EpisodeResult(episode=spec.episode, ok=True, turns=1)

    def validate(self, root):
        return "invalid source" if (root / "source.txt").read_text() == "invalid" else ""

    monkeypatch.setattr(CodexHarness, "seed", seed)
    monkeypatch.setattr(CodexHarness, "run_episode", episode)
    monkeypatch.setattr(CodexHarness, "validate_candidate", validate)
    monkeypatch.setattr(CodexHarness, "read_trace", lambda self, root, ep: [])
    registry = builtin_registry()
    recipe = native_recipe()
    recipe = replace(recipe, actors={"agent": ActorSpec(model="fixture-model", session="handoff",
        runtime=PluginSpec(plugin=PluginRef(name="proteus.native.codex", version="1")))},
        method=replace(recipe.method, options={"episodes": 2, "max_turns": 20}))
    result = run(recipe, registry, tmp_path / "run")
    assert result.status == OperationStatus.COMPLETED
    assert seen == [("initial", "initial"), ("initial", "invalid")]
    assert (tmp_path / "run/workspace/harness/source.txt").read_text() == "repaired"


def test_cli_check_lock_run_resume_and_source_pin(tmp_path, capsys):
    from proteus.cli import main

    config = str(ROOT / "recipes/native/minimal.toml")
    lock = str(tmp_path / "native.lock.json")
    root = str(tmp_path / "native")
    assert main(["recipe", "check", config]) == 0
    assert main(["recipe", "lock", config, "--source", f"proteus={ROOT}", "--out", lock]) == 0
    assert main(["recipe", "run", config, "--lock", lock, "--out", root]) == 0
    assert main(["recipe", "resume", config, "--lock", lock, "--out", root]) == 0
    assert json.loads(Path(lock).read_text())["sources"][0]["revision"]
    capsys.readouterr()


def test_registry_is_frozen_for_running_method(tmp_path):
    registry = builtin_registry()

    class RegisterDuringRun:
        def run(self, services, restored_state):
            services.registry.register_data("late", Text)
            return MethodResult()

    registry.register(plugin("fixture.mutator", "method", lambda opts: RegisterDuringRun()))
    recipe = replace(stage_recipe(), method=PluginSpec(plugin=PluginRef(name="fixture.mutator", version="1")))
    result = run(recipe, registry, tmp_path / "run")
    assert result.status == OperationStatus.FAILED and "registry is frozen" in result.diagnostic
    registry.register_data("next-experiment", Text)  # the user's registry is not frozen


def test_explicit_method_state_schema_on_restore(tmp_path):
    from proteus.core.contracts import MethodState, PluginState

    @dataclass(frozen=True, kw_only=True)
    class Counter(Contract):
        count: int

    class CountMethod:
        def run(self, services, restored_state):
            old = Counter.from_dict(restored_state.state.payload).count if restored_state else 0
            return MethodResult(state=MethodState(run_id=services.run_id, method=services.recipe.method.plugin,
                config_hash=services.recipe.config_hash, state=PluginState(namespace="fixture.count",
                    payload_version=1, payload=Counter(count=old + 1).to_dict())))

    registry = builtin_registry()
    definition = plugin("fixture.count", "method", lambda options: CountMethod(), capabilities=("run.resume",))
    registry.register(replace(definition, state_schema=Counter))
    recipe = replace(stage_recipe(), method=PluginSpec(plugin=definition.ref))
    root = tmp_path / "run"
    assert run(recipe, registry, root).state.state.payload["count"] == 1
    assert run(recipe, registry, root, resume=True).state.state.payload["count"] == 2
    result_file = root / "result.json"
    data = json.loads(result_file.read_text())
    data["state"]["state"]["payload_version"] = 9
    result_file.write_text(json.dumps(data))
    with pytest.raises(ContractError, match="state version"):
        run(recipe, registry, root, resume=True)


def test_installed_package_duplicates_are_canonicalized_or_rejected(monkeypatch):
    import importlib.metadata
    from types import SimpleNamespace
    from proteus.recipes.cli import installed_packages

    one = SimpleNamespace(metadata={"Name": "Some_Package"}, version="1.0")
    two = SimpleNamespace(metadata={"Name": "some-package"}, version="1.0")
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: [one, two])
    assert installed_packages() == (DependencyPin(name="some-package", version="1.0"),)
    two.version = "2.0"
    with pytest.raises(ContractError, match="conflicting"):
        installed_packages()


def test_serial_services_reject_cross_thread_dispatch_before_actor_work(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    registry = builtin_registry()
    services = RunServices(run_id="fixture", recipe=registry.resolve(stage_recipe()),
                           registry=registry, root=tmp_path)
    ref = services.values.publish("proteus.text@1", Text(text="prompt"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(services.stage, "propose", {"prompt": ref})
        with pytest.raises(ContractError, match="cross-thread"):
            future.result()
    assert services.actor_calls == [] and services.requests == []
