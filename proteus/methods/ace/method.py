"""ACE offline/online control flow over managed, journaled Proteus services.

The pinned algorithm's success path is preserved. Transport failures stop with
evidence instead of invoking upstream's unbounded retry/incorrect-answer fallback.
Recipes therefore explicitly declare an adapted (or extended) profile.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from proteus.core.contracts import DataRef, FeedbackVisibility, OperationStatus, Outcome, PluginRef, SessionPolicy, StatePolicy, TEXT, require
from proteus.core.methods import MethodResult
from proteus.core.plugins import Plugin, implementation_hash
from proteus.core.serialization import Contract, json_value
from proteus.evaluation.tasks import TaskBatch, TaskSelection
from proteus.methods.ace.playbook import CODEC, REPOSITORY, REVISION, PlaybookDelta
from proteus.methods.ace.roles import AceTask, CurationInput, Generation, GenerationInput, ReflectionInput


@dataclass(frozen=True, kw_only=True)
class AceOptions(Contract):
    train: str = field(metadata=TEXT)
    reflection_stage: str = field(metadata=TEXT)
    curation_stage: str = field(metadata=TEXT)
    mode: Literal["offline", "online"] = "offline"
    validation: str | None = field(default=None, metadata=TEXT)
    stream: str | None = field(default=None, metadata=TEXT)
    test_evaluations: tuple[str, ...] = field(default=(), metadata={"uniqueItems": True})
    num_epochs: int = field(default=1, metadata={"minimum": 1})
    max_num_rounds: int = field(default=3, metadata={"minimum": 0})
    curator_frequency: int = field(default=1, metadata={"minimum": 1})
    eval_steps: int = field(default=100, metadata={"minimum": 1})
    online_eval_frequency: int = field(default=100, metadata={"minimum": 1})
    token_budget: int = field(default=80000, metadata={"minimum": 1})
    no_ground_truth: bool = False
    json_mode: bool = False


@dataclass(frozen=True, kw_only=True)
class SampleResult(Contract):
    task_id: str
    epoch: int
    step: int
    before_id: str
    after_id: str
    predictions: tuple[DataRef, ...]
    reflections: tuple[DataRef, ...]
    curation: DataRef | None


@dataclass(frozen=True, kw_only=True)
class AceState(Contract):
    train: TaskBatch
    initial_id: str
    current_id: str
    best_id: str
    cursor: int = 0
    best_accuracy: float = 0.0
    initial_tests: Mapping[str, DataRef] = field(default_factory=dict)
    windows: tuple[DataRef, ...] = ()
    samples: tuple[DataRef, ...] = ()
    validations: tuple[DataRef, ...] = ()
    phase: Literal["training", "finished"] = "training"
    outputs: Mapping[str, DataRef] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class AceSummary(Contract):
    mode: Literal["offline", "online"]
    initial_id: str
    selected_id: str
    final_id: str
    best_validation_accuracy: float | None
    samples: tuple[DataRef, ...]
    validations: tuple[DataRef, ...]
    initial_tests: Mapping[str, DataRef]
    windows: tuple[DataRef, ...]
    final_tests: Mapping[str, DataRef]
    revision: str = REVISION


def validate_ace(recipe, options, registry):
    require(recipe.subject is not None and recipe.execution is not None, "ACE requires subject/execution")
    require(recipe.subject.initial_artifact.codec == CODEC and
            dict(recipe.subject.input_types) == {"generation": "proteus.ace.generation_input@1"},
            "ACE requires playbook artifacts and typed generation inputs")
    require(recipe.execution.state_policy == StatePolicy.INDEPENDENT,
            "ACE carries learning in the playbook, with independent subject trials")
    require(recipe.tasks is None and not recipe.search, "ACE consumes declared evaluation pools and no search policies")
    require(recipe.reference.code == REPOSITORY and recipe.reference.revision == REVISION,
            "ACE requires its pinned reference identity")
    require(recipe.reference.profile in ("adapted", "extended") and bool(recipe.reference.deviations),
            "ACE managed transport and strict artifacts require declared adapted/extended deviations")
    evaluations = {item.id: item for item in recipe.evaluations}
    ids = [options.train, *options.test_evaluations]
    if options.mode == "offline":
        require(options.validation is not None and options.stream is None,
                "offline ACE requires validation and no online stream")
        ids.append(options.validation)
    else:
        require(options.stream is not None and options.validation is None and not options.test_evaluations
                and options.num_epochs == 1, "online ACE requires one stream/epoch and no offline evaluations")
        ids.append(options.stream)
    require(len(ids) == len(set(ids)) and set(ids) == set(evaluations), "ACE evaluation IDs differ or go unused")
    train = evaluations[options.train]
    require(train.purpose == "optimization" and train.visibility == FeedbackVisibility.OBSERVED
            and train.feedback_timing == "after_prediction", "ACE needs observed per-prediction training feedback")
    require(all(item.tasks.order == "declared" for item in evaluations.values()), "ACE preserves declared task order")
    if options.mode == "offline":
        validation = evaluations[options.validation]
        require(validation.purpose == "validation" and validation.visibility == FeedbackVisibility.OBSERVED
                and validation.feedback_timing == "after_batch" and validation.selection_eligible,
                "ACE requires observed, selection-eligible validation")
        pools = [evaluations[key].tasks for key in ids]
        for index, pool in enumerate(pools):
            require(all(pool.dataset_hash != other.dataset_hash or pool.split != other.split
                        or not set(pool.task_ids).intersection(other.task_ids) for other in pools[index + 1:]),
                    "offline ACE train/validation/test pools overlap")
        require(all(evaluations[key].purpose == "test" for key in options.test_evaluations), "ACE tests must be hidden")
    else:
        stream = evaluations[options.stream]
        require(stream.purpose == "test" and stream.tasks == train.tasks,
                "online ACE hidden stream and observed learning pool must have identical ordered tasks")
        require(stream.evaluator == train.evaluator, "online scoring and learning must use the same evaluator")
    stages = {item.id: item for item in recipe.stages}
    require(options.reflection_stage != options.curation_stage and
            set(stages) == {options.reflection_stage, options.curation_stage}, "ACE stages differ or go unused")
    for key, request, output in ((options.reflection_stage, "reflection_input", "reflection"),
                                 (options.curation_stage, "curation_input", "curation")):
        stage = stages[key]
        require(dict(stage.input_types) == {"request": f"proteus.ace.{request}@1"} and
                dict(stage.output_types) == {output: f"proteus.ace.{output}@1"}, "ACE stage port mismatch")
        require(stage.max_attempts == 1 and stage.failure_policy == "stop", "ACE roles stop on transport failures")
    for actor in recipe.actors.values():
        require(actor.session == SessionPolicy.FRESH, "ACE roles require fresh sessions")
        if actor.runtime.plugin.name == "proteus.chat":
            require((actor.model_settings.get("response_format") == {"type": "json_object"}) == options.json_mode,
                    "ACE JSON parsing mode must match the chat response format")


class AceMethod:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        options = self.options
        state = AceState.from_dict(restored_state.state.payload) if restored_state else None
        if state and state.phase == "finished":
            return MethodResult(outputs=state.outputs, state=restored_state)

        def candidate(key):
            return next(item for item in services.candidates if item.id == key)

        def publish(kind, value):
            return services.values.publish(kind, value)

        def generation_inputs(reflection="(empty)"):
            return {"generation": publish("proteus.ace.generation_input@1",
                GenerationInput(reflection=reflection, json_mode=options.json_mode))}

        def evaluate(selected, evaluation_id, selection=None):
            session = services.open_evaluation(evaluation_id, selection=selection, inputs=generation_inputs())
            # Public prediction failures must never become baseline scores. Hidden
            # grader validity is checked separately by the private results report.
            for _ in session.batch.tasks:
                observation = session.step(selected)
                require(observation.rollout.status == Outcome.SUCCEEDED,
                        "ACE evaluation prediction failed; cannot impute a score")
            return session.finish()

        if state is None:
            initial = services.initial_candidate()
            train = services.task_pool(options.train)
            for task in train.tasks:
                AceTask.from_dict(task.input)
            initial_tests = {}
            for key in options.test_evaluations if options.mode == "offline" else (options.stream,):
                initial_tests[key] = publish("proteus.eval.receipt@1", evaluate(initial, key))
            state = AceState(train=train, initial_id=initial.id, current_id=initial.id, best_id=initial.id,
                             initial_tests=initial_tests)
            services.checkpoint(state)

        count = len(state.train.tasks)
        while state.cursor < count * options.num_epochs:
            current = candidate(state.current_id)
            if options.mode == "online" and state.cursor % options.online_eval_frequency == 0:
                window_index = state.cursor // options.online_eval_frequency
                if len(state.windows) == window_index:
                    tasks = state.train.tasks[state.cursor:state.cursor + options.online_eval_frequency]
                    receipt = evaluate(current, options.stream, TaskSelection(task_ids=tuple(task.id for task in tasks)))
                    state = replace(state, windows=(*state.windows, publish("proteus.eval.receipt@1", receipt)))
                    services.checkpoint(state)
            epoch, offset = divmod(state.cursor, count)
            task = state.train.tasks[offset]
            current, sample = self.train_sample(services, current, task, epoch + 1, offset + 1, count, generation_inputs)
            state = replace(state, current_id=current.id, cursor=state.cursor + 1,
                            samples=(*state.samples, publish("proteus.ace.sample@1", sample)))
            if options.mode == "offline" and (offset + 1) % options.eval_steps == 0:
                receipt = evaluate(current, options.validation)
                rows = [row for batch in receipt.batches for row in batch.rows]
                require(len(rows) == len(receipt.rollout_ids) and rows and
                        all(row.score in (0.0, 1.0) and row.status in (Outcome.SUCCEEDED, Outcome.TASK_FAILED)
                            for row in rows), "ACE validation requires complete binary scores; cannot impute")
                accuracy = sum(row.score for row in rows) / len(rows)
                if accuracy > state.best_accuracy:
                    state = replace(state, best_accuracy=accuracy, best_id=current.id)
                state = replace(state, validations=(*state.validations, publish("proteus.eval.receipt@1", receipt)))
            services.checkpoint(state)

        selected = candidate(state.best_id if options.mode == "offline" else state.current_id)
        final_tests = {key: publish("proteus.eval.receipt@1", evaluate(selected, key)) for key in options.test_evaluations}
        summary = AceSummary(mode=options.mode, initial_id=state.initial_id, selected_id=selected.id,
            final_id=state.current_id, best_validation_accuracy=state.best_accuracy if options.mode == "offline" else None,
            samples=state.samples, validations=state.validations, initial_tests=state.initial_tests,
            windows=state.windows, final_tests=final_tests)
        outputs = {"summary": publish("proteus.ace.summary@1", summary),
            "candidate": publish("proteus.candidate@1", selected),
            "final_candidate": publish("proteus.candidate@1", candidate(state.current_id))}
        checkpoint = services.checkpoint(replace(state, phase="finished", outputs=outputs))
        return MethodResult(outputs=outputs, state=checkpoint)

    def train_sample(self, services, current, task, epoch, step, total, generation_inputs):
        options = self.options
        item = AceTask.from_dict(task.input)
        before_id, predictions, reflections = current.id, [], []

        def generate(reflection="(empty)"):
            session = services.open_evaluation(options.train, selection=TaskSelection(task_ids=(task.id,)),
                                              inputs=generation_inputs(reflection))
            observation = session.step(current)
            session.finish()
            require(observation.rollout.status == Outcome.SUCCEEDED and observation.row is not None
                    and observation.row.status in (Outcome.SUCCEEDED, Outcome.TASK_FAILED)
                    and observation.row.score in (0.0, 1.0), "ACE training requires binary feedback; cannot impute")
            feedback = observation.feedback.value
            require(isinstance(feedback, Mapping) and type(feedback.get("correct")) is bool
                    and float(feedback["correct"]) == observation.row.score,
                    "ACE feedback must expose a consistent boolean correct field")
            if not options.no_ground_truth:
                require(type(feedback.get("ground_truth")) is str, "ACE reflection needs training ground_truth")
            predictions.append(services.values.publish("proteus.eval.observation@1", observation))
            return Generation.from_dict(observation.rollout.output), feedback

        def reflect(generation, feedback):
            nonlocal current
            playbook = services.artifacts.get(current.artifact)
            request = ReflectionInput(question=item.question, reasoning_trace=generation.response,
                predicted_answer=generation.answer,
                ground_truth=None if options.no_ground_truth else feedback["ground_truth"],
                environment_feedback="Predicted answer matches ground truth" if feedback["correct"]
                    else "Predicted answer does not match ground truth",
                bullets_used=playbook.extract(json_value(generation.bullet_ids)),
                use_ground_truth=not options.no_ground_truth, json_mode=options.json_mode)
            result = services.stage(options.reflection_stage,
                {"request": services.values.publish("proteus.ace.reflection_input@1", request)})
            require(result.status == OperationStatus.COMPLETED, result.diagnostic or "ACE reflection failed")
            reflections.append(result.outputs["reflection"])
            reflection = services.values.get(reflections[-1])
            tags = json_value(reflection.bullet_tags)
            # Upstream ignores non-list tags; retain the raw response as evidence.
            if isinstance(tags, list) and tags:
                current = services.fork_candidate(current, delta=PlaybookDelta(
                    base_hash=current.artifact.content_hash, kind="counters", records=tags))
            return reflection.response

        generation, feedback = generate()
        reflection_content = "(empty)"
        if feedback["correct"]:
            reflection_content = reflect(generation, feedback)
        else:
            for _ in range(options.max_num_rounds):
                reflection_content = reflect(generation, feedback)
                generation, feedback = generate(reflection_content)
                if feedback["correct"]:
                    break
        curation_ref = None
        if step % options.curator_frequency == 0:
            request = CurationInput(playbook=services.artifacts.get(current.artifact),
                artifact_hash=current.artifact.content_hash, reflection=reflection_content,
                question_context=item.context, step=step, total_samples=total, token_budget=options.token_budget,
                use_ground_truth=not options.no_ground_truth)
            result = services.stage(options.curation_stage,
                {"request": services.values.publish("proteus.ace.curation_input@1", request)})
            require(result.status == OperationStatus.COMPLETED, result.diagnostic or "ACE curation failed")
            curation_ref = result.outputs["curation"]
            curation = services.values.get(curation_ref)
            if curation.accepted:
                current = services.fork_candidate(current, delta=PlaybookDelta(
                    base_hash=current.artifact.content_hash, kind="curate", records=curation.operations))
        generate()
        return current, SampleResult(task_id=task.id, epoch=epoch, step=step, before_id=before_id,
            after_id=current.id, predictions=tuple(predictions), reflections=tuple(reflections), curation=curation_ref)


def register(registry):
    from proteus.methods.ace.playbook import register as register_playbook
    from proteus.methods.ace.roles import register as register_roles
    register_playbook(registry)
    register_roles(registry)
    registry.register_data("proteus.ace.sample@1", SampleResult)
    registry.register_data("proteus.ace.summary@1", AceSummary)
    root = Path(__file__).parent
    registry.register(Plugin(ref=PluginRef(name="proteus.ace", version="1"), kind="method", options=AceOptions,
        factory=AceMethod, source="proteus", implementation_hash=implementation_hash(
            {path.name: path for path in root.glob("*.py")}), capabilities=("run.resume", "run.journal"),
        state_schema=AceState, validate=lambda recipe, options: validate_ace(recipe, options, registry)))
