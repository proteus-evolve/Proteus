"""Typed transient subject inputs bind evaluations without mutating candidates."""

import json
from dataclasses import dataclass, replace

import pytest

from proteus.core.contracts import OperationStatus, Outcome, PluginRef, RuntimeIdentity
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.serialization import Contract, ContractError, content_hash
from proteus.evaluation.records import SubjectResult, zero_usage
from proteus.evaluation.tasks import TaskSelection
from test_v1_evaluation import SOURCE, method, plugin, run, setup


@dataclass(frozen=True, kw_only=True)
class Hint(Contract):
    text: str


class Subject:
    def run(self, context):
        with pytest.raises(TypeError):
            context.inputs["hint"] = Hint(text="mutation")
        return SubjectResult(loaded_artifact_hash=context.lease.loaded_artifact_hash,
            runtime=RuntimeIdentity(runtime=context.subject.runner.plugin,
                environment_hash=content_hash(context.execution.environment)),
            output=context.inputs["hint"].text, state=context.state,
            status=Outcome.SUCCEEDED, usage=zero_usage())


def test_inputs_are_typed_immutable_and_part_of_evidence_identity(tmp_path):
    registry, store, _, recipe = setup(tmp_path)
    registry.register_data("fixture.hint@1", Hint)
    registry.register(Plugin(ref=PluginRef(name="fixture.input_subject", version="1"), kind="subject",
        options=EmptyOptions, factory=lambda options: Subject(), source="proteus", implementation_hash=content_hash("hint"),
        inputs={"hint": "fixture.hint@1"}, capabilities=("subject.model_free",)))
    recipe = replace(recipe, subject=replace(recipe.subject, runner=plugin("fixture.input_subject")))

    def study(services):
        ref = services.values.publish("fixture.hint@1", Hint(text="first"))
        for inputs in (None, {}, {"wrong": ref}, {"hint": "first"},
                       {"hint": replace(ref, kind="proteus.text@1")},
                       {"hint": replace(ref, content_hash=content_hash("tampered"))}):
            with pytest.raises(ContractError):
                services.open_evaluation("score", inputs=inputs)
        assert not services.root.joinpath("evaluations").exists()
        assert not services.ledger.entries()
        candidate = services.initial_candidate()
        for value in ("first", "second", "first"):
            refs = {"hint": services.values.publish("fixture.hint@1", Hint(text=value))}
            session = services.open_evaluation("score", selection=TaskSelection(task_ids=("0",)), inputs=refs)
            refs.clear()  # the service took an immutable copy
            session.step(candidate)
            session.finish()
        assert len(services.candidates) == 1
        return MethodResult()

    recipe = method(registry, recipe, study)
    lock = registry.make_lock(recipe, [SOURCE])
    assert registry.resolve(recipe).subject.input_types == {"hint": "fixture.hint@1"}
    changed = registry.snapshot()
    key = ("fixture.input_subject", "1")
    changed._plugins[key] = replace(changed._plugins[key], inputs={"hint": "proteus.text@1"})
    with pytest.raises(ContractError, match="differ from lock"):
        changed.check_lock(recipe, lock)
    result, records, root = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert [row["output"] for row in records["rollouts"]] == ["first", "second", "first"]
    by_id = {item["rollout"]["id"]: item for item in
             (json.loads(path.read_text()) for path in root.glob("evaluations/*/predictions/*.json"))}
    predictions = [by_id[row["id"]] for row in records["rollouts"]]
    for key in ("cache_key", "inputs"):
        assert predictions[0][key] != predictions[1][key] and predictions[0][key] == predictions[2][key]
    execution = [item["rollout"]["execution_hash"] for item in predictions]
    assert execution[0] != execution[1] and execution[0] == execution[2]
    protocols = [item["protocol_hash"] for item in records["evaluations"]]
    assert protocols[0] != protocols[1] and protocols[0] == protocols[2]
    with pytest.raises(ContractError, match="subject inputs differ"):
        registry.resolve(replace(recipe, subject=replace(recipe.subject, input_types={"hint": "proteus.text@1"})))


def test_undeclared_subject_input_is_rejected_before_dispatch(tmp_path):
    registry, store, _, recipe = setup(tmp_path)

    def study(services):
        from proteus.recipes.builtins import Text
        ref = services.values.publish("proteus.text@1", Text(text="hint"))
        with pytest.raises(ContractError, match="declared ports"):
            services.open_evaluation("score", inputs={"hint": ref})
        assert not services.ledger.entries()
        return MethodResult()

    result, _, _ = run(tmp_path, registry, store, method(registry, recipe, study))
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
