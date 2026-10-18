"""Combee-ACE's independent sample learning program over shared Proteus services.

The sample program owns temporary counter updates. Only its collected tags and
last reflection feed the owner's shared batch update; no sample publishes a
replacement shared playbook. The full batch controller composes this workflow
with the common Combee aggregation layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from proteus.core.contracts import (
    Candidate,
    DataRef,
    FeedbackVisibility,
    OperationStatus,
    Outcome,
    PluginRef,
    TEXT,
    require,
)
from proteus.core.plugins import Plugin, implementation_hash
from proteus.core.methods import MethodResult
from proteus.core.recovery import RecoveryRequired
from proteus.core.serialization import Contract, content_hash, json_value
from proteus.core.workflows import WorkflowJob
from proteus.evaluation.tasks import Task, TaskSelection
from proteus.methods.ace.playbook import CODEC, PlaybookDelta
from proteus.methods.ace.roles import AceTask, Generation, GenerationInput, ReflectionInput
from proteus.methods.ace.method import (
    AceState,
    AceSummary,
    LearningOptions,
    validate_learning_protocol,
)
from proteus.operators.combee.ace import AceRecord, AggregationMode, aggregate_ace
from proteus.operators.combee.grouping import Grouping
from proteus.operators.combee.batching import (
    BatchControllerState,
    DynamicBatchOptions,
    initialize as initialize_batching,
    observe as observe_batch,
    validate_state as validate_batching,
    validate_measurements,
)


@dataclass(frozen=True, kw_only=True)
class SampleOptions(Contract):
    train: str = field(metadata=TEXT)
    reflection_stage: str = field(metadata=TEXT)
    max_num_rounds: int = field(default=3, metadata={"minimum": 0})
    no_ground_truth: bool = False
    json_mode: bool = False


@dataclass(frozen=True, kw_only=True)
class SampleInput(Contract):
    candidate: Candidate
    task: Task
    epoch: int = field(metadata={"minimum": 1})
    step: int = field(metadata={"minimum": 1})


@dataclass(frozen=True, kw_only=True)
class SampleLearning(Contract):
    task_id: str
    epoch: int
    step: int
    before_id: str
    local_id: str
    predictions: tuple[DataRef, ...]
    reflections: tuple[DataRef, ...]
    bullet_tags: tuple[Any, ...]
    reflection: str
    pre_train_answer: str
    final_answer: str
    correct: bool


class LearnSample:
    def __init__(self, options):
        self.options = options

    def run(self, services, inputs):
        item, options = inputs["sample"], self.options
        task = AceTask.from_dict(item.task.input)
        current = item.candidate
        predictions, reflections, tags = [], [], []

        def generate(reflection="(empty)"):
            request = services.values.publish(
                "proteus.ace.generation_input@1",
                GenerationInput(reflection=reflection, json_mode=options.json_mode),
            )
            session = services.open_evaluation(
                options.train,
                selection=TaskSelection(task_ids=(item.task.id,)),
                inputs={"generation": request},
            )
            require(
                session.batch.tasks == (item.task,), "Combee sample differs from its pinned task"
            )
            observation = session.step(current)
            session.finish()
            require(
                observation.rollout.status == Outcome.SUCCEEDED
                and observation.row is not None
                and observation.row.status in (Outcome.SUCCEEDED, Outcome.TASK_FAILED)
                and observation.row.score in (0.0, 1.0),
                "Combee ACE training requires complete binary feedback; cannot impute",
            )
            feedback = observation.feedback.value
            require(
                isinstance(feedback, Mapping)
                and type(feedback.get("correct")) is bool
                and float(feedback["correct"]) == observation.row.score,
                "Combee ACE feedback must expose a consistent boolean correct field",
            )
            if not options.no_ground_truth:
                require(
                    type(feedback.get("ground_truth")) is str,
                    "Combee ACE reflection needs ground_truth",
                )
            predictions.append(services.values.publish("proteus.eval.observation@1", observation))
            return Generation.from_dict(observation.rollout.output), feedback

        def reflect(generation, feedback, *, update_local):
            nonlocal current
            playbook = services.artifacts.get(current.artifact)
            request = ReflectionInput(
                question=task.question,
                reasoning_trace=generation.response,
                predicted_answer=generation.answer,
                ground_truth=None if options.no_ground_truth else feedback["ground_truth"],
                environment_feedback="Predicted answer matches ground truth"
                if feedback["correct"]
                else "Predicted answer does not match ground truth",
                bullets_used=playbook.extract(json_value(generation.bullet_ids)),
                use_ground_truth=not options.no_ground_truth,
                json_mode=options.json_mode,
            )
            result = services.stage(
                options.reflection_stage,
                {"request": services.values.publish("proteus.ace.reflection_input@1", request)},
            )
            require(
                result.status == OperationStatus.COMPLETED,
                result.diagnostic or "Combee reflection failed",
            )
            reflections.append(result.outputs["reflection"])
            reflection = services.values.get(reflections[-1])
            records = json_value(reflection.bullet_tags)
            if isinstance(records, list) and records:
                tags.extend(records)
                if update_local:
                    current = services.fork_candidate(
                        current,
                        delta=PlaybookDelta(
                            base_hash=current.artifact.content_hash,
                            kind="counters",
                            records=records,
                        ),
                    )
            return reflection.response

        generation, feedback = generate()
        initial_answer = generation.answer
        reflection = "(empty)"
        if feedback["correct"]:
            # Correct samples contribute tags once without a local counter edit
            # or redundant generation before the shared curation phase.
            reflection = reflect(generation, feedback, update_local=False)
        else:
            for _ in range(options.max_num_rounds):
                reflection = reflect(generation, feedback, update_local=True)
                generation, feedback = generate(reflection)
                if feedback["correct"]:
                    break
        return {
            "learning": SampleLearning(
                task_id=item.task.id,
                epoch=item.epoch,
                step=item.step,
                before_id=item.candidate.id,
                local_id=current.id,
                predictions=tuple(predictions),
                reflections=tuple(reflections),
                bullet_tags=tuple(tags),
                reflection=reflection,
                pre_train_answer=initial_answer,
                final_answer=generation.answer,
                correct=feedback["correct"],
            )
        }


def validate_sample(recipe, options):
    require(
        recipe.subject is not None and recipe.subject.initial_artifact.codec == CODEC,
        "Combee ACE sample requires a playbook subject",
    )
    require(
        dict(recipe.subject.input_types) == {"generation": "proteus.ace.generation_input@1"},
        "Combee ACE sample requires typed generation inputs",
    )
    pool = next((item for item in recipe.evaluations if item.id == options.train), None)
    require(
        pool is not None
        and pool.purpose == "optimization"
        and pool.visibility == FeedbackVisibility.OBSERVED
        and pool.feedback_timing == "after_prediction",
        "Combee ACE sample requires observed per-prediction training feedback",
    )
    stage = next((item for item in recipe.stages if item.id == options.reflection_stage), None)
    require(
        stage is not None
        and dict(stage.input_types) == {"request": "proteus.ace.reflection_input@1"}
        and dict(stage.output_types) == {"reflection": "proteus.ace.reflection@1"}
        and stage.max_attempts == 1
        and stage.failure_policy == "stop",
        "Combee ACE reflector requires its typed ports and strict transport policy",
    )


def register_sample(registry):
    registry.register_data("proteus.combee.ace_sample_input@1", SampleInput)
    registry.register_data("proteus.combee.ace_sample_learning@1", SampleLearning)
    root = Path(__file__).parent
    registry.register(
        Plugin(
            ref=PluginRef(name="proteus.combee_ace_sample", version="1"),
            kind="workflow",
            options=SampleOptions,
            factory=LearnSample,
            source="proteus",
            implementation_hash=implementation_hash(
                {path.name: path for path in root.glob("*.py")}
            ),
            capabilities=("workflow.parallel", "workflow.replay"),
            inputs={"sample": "proteus.combee.ace_sample_input@1"},
            outputs={"learning": "proteus.combee.ace_sample_learning@1"},
            validate=validate_sample,
        )
    )


@dataclass(frozen=True, kw_only=True)
class CombeeOptions(LearningOptions):
    sample_workflow: str = field(metadata=TEXT)
    reflection_stage: str = field(metadata=TEXT)
    aggregation_stage: str = field(metadata=TEXT)
    batch_size: int = field(default=4, metadata={"minimum": 1})
    max_workers: int = field(default=4, metadata={"minimum": 1})
    grouping: Grouping = field(default_factory=lambda: Grouping(profile="ace"))
    dynamic_batch: DynamicBatchOptions | None = None
    aggregation_mode: AggregationMode = "combee"


@dataclass(frozen=True, kw_only=True)
class BatchLearning(Contract):
    epoch: int
    start: int
    task_ids: tuple[str, ...]
    before_id: str
    counter_id: str
    proposed_id: str
    after_id: str
    samples: tuple[DataRef, ...]
    workflow_map: DataRef
    aggregation: DataRef
    post_evaluation: DataRef | None
    rolled_back: bool = False
    diagnostic: str = ""
    timing: DataRef | None = None


@dataclass(frozen=True, kw_only=True)
class CombeeState(AceState):
    batches: tuple[DataRef, ...] = ()
    status: OperationStatus = OperationStatus.COMPLETED
    diagnostic: str = ""
    batch_controller: BatchControllerState | None = None


@dataclass(frozen=True, kw_only=True)
class CombeeSummary(AceSummary):
    batches: tuple[DataRef, ...]
    batch_size: int
    batch_controller: BatchControllerState | None = None


def validate_combee(recipe, options):
    validate_learning_protocol(recipe, options)
    require(options.grouping.profile == "ace", "Combee ACE requires ACE grouping")
    require(options.aggregation_mode != "single_pass"
            or (options.grouping.duplication_factor == 1 and options.grouping.num_groups == 1),
            "single-pass aggregation requires one group with no duplication")
    require(
        {item.id for item in recipe.stages} == {options.reflection_stage, options.aggregation_stage}
        and options.reflection_stage != options.aggregation_stage,
        "Combee ACE stages differ or go unused",
    )
    validate_sample(
        recipe,
        SampleOptions(
            train=options.train,
            reflection_stage=options.reflection_stage,
            max_num_rounds=options.max_num_rounds,
            no_ground_truth=options.no_ground_truth,
            json_mode=options.json_mode,
        ),
    )
    require(
        len(recipe.workflows) == 1 and recipe.workflows[0].id == options.sample_workflow,
        "Combee ACE consumes one sample workflow",
    )
    workflow = recipe.workflows[0]
    require(
        dict(workflow.input_types) == {"sample": "proteus.combee.ace_sample_input@1"}
        and dict(workflow.output_types) == {"learning": "proteus.combee.ace_sample_learning@1"}
        and workflow.stages == (options.reflection_stage,)
        and workflow.evaluations == (options.train,),
        "Combee ACE sample workflow has different ports or service scopes",
    )
    if workflow.operator.plugin.name == "proteus.combee_ace_sample":
        require(
            SampleOptions.from_dict(workflow.operator.options)
            == SampleOptions(
                train=options.train,
                reflection_stage=options.reflection_stage,
                max_num_rounds=options.max_num_rounds,
                no_ground_truth=options.no_ground_truth,
                json_mode=options.json_mode,
            ),
            "Combee ACE sample options differ from the method protocol",
        )
    else:
        require(
            recipe.reference.profile == "extended",
            "a replacement sample algorithm requires an extended profile",
        )
    stage = next(item for item in recipe.stages if item.id == options.aggregation_stage)
    require(
        dict(stage.input_types) == {"prompt": "proteus.text@1"}
        and dict(stage.output_types) == {"response": "proteus.actor_result@1"}
        and stage.max_attempts == 1
        and stage.failure_policy == "stop",
        "Combee ACE aggregation needs a strict managed text stage",
    )


class CombeeAceMethod:
    def __init__(self, options):
        self.options = options

    def evaluate(self, services, candidate, evaluation, selection=None):
        generation = services.values.publish(
            "proteus.ace.generation_input@1", GenerationInput(json_mode=self.options.json_mode)
        )
        receipt = services.evaluate(
            candidate, evaluation, selection=selection, inputs={"generation": generation}
        )
        predicted = {row.id: row for row in services.rollouts if row.id in receipt.rollout_ids}
        require(
            len(predicted) == len(receipt.rollout_ids)
            and all(row.status == Outcome.SUCCEEDED for row in predicted.values()),
            "Combee ACE prediction failed; cannot impute a score",
        )
        return receipt

    def train_batch(self, services, before, tasks, epoch, start, total):
        options = self.options
        operation = f"combee-epoch-{epoch}-batch-{start}"
        publish = services.values.publish
        timing = services.begin_timing(operation) if options.dynamic_batch else None
        jobs = tuple(
            WorkflowJob(
                key=task.id,
                workflow=options.sample_workflow,
                inputs={
                    "sample": publish(
                        "proteus.combee.ace_sample_input@1",
                        SampleInput(candidate=before, task=task, epoch=epoch, step=start + index),
                    )
                },
            )
            for index, task in enumerate(tasks)
        )
        mapped = services.map_workflows(
            jobs, operation_id=operation + "/samples", max_workers=options.max_workers
        )
        map_ref = publish("proteus.workflow_map@1", mapped)
        require(
            mapped.succeeded,
            "Combee ACE sample learning failed; incomplete reflections cannot be aggregated",
        )
        sample_refs = tuple(row.outputs["learning"] for row in mapped.results)
        samples = [services.values.get(ref) for ref in sample_refs]
        require(
            all(
                sample.task_id == task.id
                and sample.before_id == before.id
                and sample.epoch == epoch
                and sample.step == start + index
                for index, (sample, task) in enumerate(zip(samples, tasks))
            ),
            "Combee sample result binding differs",
        )
        # Preserve the upstream single combined counter update. Repeated bullet
        # IDs use the last original tag; augmentation never duplicates counters.
        tags = tuple(tag for sample in samples for tag in sample.bullet_tags)
        counter = (
            services.fork_candidate(
                before,
                delta=PlaybookDelta(
                    base_hash=before.artifact.content_hash, kind="counters", records=tags
                ),
            )
            if tags
            else before
        )
        records = tuple(
            publish(
                "proteus.combee.ace_record@1",
                AceRecord(
                    sample_number=start + index,
                    reflection=sample.reflection,
                    context=AceTask.from_dict(task.input).context,
                ),
            )
            for index, (sample, task) in enumerate(zip(samples, tasks))
        )
        aggregation = aggregate_ace(
            services,
            counter.artifact,
            records,
            stage=options.aggregation_stage,
            operation_id=operation + "/curation",
            step=start + len(tasks) - 1,
            total_samples=total,
            max_workers=options.max_workers,
            grouping=options.grouping,
            token_budget=options.token_budget,
            use_ground_truth=not options.no_ground_truth,
            mode=options.aggregation_mode,
        )
        delta = services.values.get(aggregation).delta
        proposed = services.fork_candidate(counter, delta=delta) if delta.records else counter
        post, diagnostic = None, ""
        try:
            receipt = self.evaluate(
                services,
                proposed,
                options.train,
                TaskSelection(task_ids=tuple(task.id for task in tasks)),
            )
            post = publish("proteus.eval.receipt@1", receipt)
            rows = [row for batch in receipt.batches for row in batch.rows]
            require(
                len(rows) == len(tasks)
                and all(
                    row.score in (0.0, 1.0)
                    and row.status in (Outcome.SUCCEEDED, Outcome.TASK_FAILED)
                    for row in rows
                ),
                "Combee ACE post-curation requires complete binary scores",
            )
        except RecoveryRequired:
            raise
        except Exception as exc:
            diagnostic = f"{type(exc).__name__}: {exc}"
        # Known phase-3 failure restores both playbook text and next global ID
        # by selecting the immutable pre-batch candidate. It then stops, instead
        # of inserting the upstream API-error sentinel as a task prediction.
        current = before if diagnostic else proposed
        batch = BatchLearning(
            epoch=epoch,
            start=start,
            task_ids=tuple(task.id for task in tasks),
            before_id=before.id,
            counter_id=counter.id,
            proposed_id=proposed.id,
            after_id=current.id,
            samples=sample_refs,
            workflow_map=map_ref,
            aggregation=aggregation,
            post_evaluation=post,
            rolled_back=bool(diagnostic),
            diagnostic=diagnostic,
            timing=services.finish_timing(timing) if timing is not None else None,
        )
        return current, batch

    def run(self, services, restored_state):
        options = self.options
        state = CombeeState.from_dict(restored_state.state.payload) if restored_state else None
        if state and state.phase == "finished":
            return MethodResult(
                status=state.status,
                diagnostic=state.diagnostic,
                outputs=state.outputs,
                state=restored_state,
            )
        publish = services.values.publish

        def candidate(key):
            return next(item for item in services.candidates if item.id == key)

        def evaluate(selected, key, selection=None):
            return self.evaluate(services, selected, key, selection)

        if state is None:
            initial = services.initial_candidate()
            train = services.task_pool(options.train)
            for task in train.tasks:
                AceTask.from_dict(task.input)
            initial_tests = {
                key: publish("proteus.eval.receipt@1", evaluate(initial, key))
                for key in options.test_evaluations
                if options.mode == "offline"
            }
            if options.mode == "online":
                initial_tests[options.stream] = publish(
                    "proteus.eval.receipt@1", evaluate(initial, options.stream)
                )
            state = CombeeState(
                train=train,
                initial_id=initial.id,
                current_id=initial.id,
                best_id=initial.id,
                initial_tests=initial_tests,
                batch_controller=initialize_batching(options.dynamic_batch,
                    pool_hash=content_hash(train), dataset_size=len(train.tasks),
                    bound=options.online_eval_frequency if options.mode == "online" else None)
                    if options.dynamic_batch else None,
            )
            services.checkpoint(state)
        count = len(state.train.tasks)
        require((state.batch_controller is not None) == (options.dynamic_batch is not None),
                "Combee batching policy differs from the checkpoint")
        if state.batch_controller is not None:
            validate_batching(options.dynamic_batch, state.batch_controller,
                pool_hash=content_hash(state.train), dataset_size=count,
                bound=options.online_eval_frequency if options.mode == "online" else None)
            validate_measurements(state.batch_controller, services.values.get)
        while state.cursor < count * options.num_epochs:
            current = candidate(state.current_id)
            epoch, offset = divmod(state.cursor, count)
            size = state.batch_controller.size if state.batch_controller else options.batch_size
            end = min(count, offset + size)
            if options.mode == "online":
                window = state.cursor // options.online_eval_frequency
                window_end = min(count, (window + 1) * options.online_eval_frequency)
                if len(state.windows) == window:
                    selection = TaskSelection(
                        task_ids=tuple(task.id for task in state.train.tasks[offset:window_end])
                    )
                    receipt = evaluate(current, options.stream, selection)
                    state = replace(
                        state, windows=(*state.windows, publish("proteus.eval.receipt@1", receipt))
                    )
                    services.checkpoint(state)
                end = min(end, window_end)
            tasks = state.train.tasks[offset:end]
            current, batch = self.train_batch(
                services, current, tasks, epoch + 1, offset + 1, count
            )
            batch_ref = publish("proteus.combee.ace_batch@1", batch)
            state = replace(
                state,
                current_id=current.id,
                cursor=state.cursor + len(tasks),
                samples=(*state.samples, *batch.samples),
                batches=(*state.batches, batch_ref),
            )
            if batch.rolled_back:
                outputs = {
                    "failed_batch": batch_ref,
                    "candidate": publish("proteus.candidate@1", current),
                }
                state = replace(
                    state,
                    phase="finished",
                    status=OperationStatus.FAILED,
                    diagnostic=batch.diagnostic,
                    outputs=outputs,
                )
                checkpoint = services.checkpoint(state)
                return MethodResult(
                    status=state.status,
                    diagnostic=state.diagnostic,
                    outputs=outputs,
                    state=checkpoint,
                )
            if state.batch_controller is not None:
                state = replace(state, batch_controller=observe_batch(options.dynamic_batch,
                    state.batch_controller, actual_size=len(tasks), measurement=batch.timing,
                    elapsed=services.values.get(batch.timing)))
            if options.mode == "offline" and end % options.eval_steps == 0:
                receipt = evaluate(current, options.validation)
                rows = [row for evaluated in receipt.batches for row in evaluated.rows]
                require(
                    rows
                    and len(rows) == len(receipt.rollout_ids)
                    and all(
                        row.score in (0.0, 1.0)
                        and row.status in (Outcome.SUCCEEDED, Outcome.TASK_FAILED)
                        for row in rows
                    ),
                    "Combee ACE validation requires complete binary scores",
                )
                accuracy = sum(row.score for row in rows) / len(rows)
                if accuracy > state.best_accuracy:
                    state = replace(state, best_accuracy=accuracy, best_id=current.id)
                state = replace(
                    state,
                    validations=(*state.validations, publish("proteus.eval.receipt@1", receipt)),
                )
            services.checkpoint(state)
        selected = candidate(state.best_id if options.mode == "offline" else state.current_id)
        final_tests = {
            key: publish("proteus.eval.receipt@1", evaluate(selected, key))
            for key in options.test_evaluations
        }
        summary = CombeeSummary(
            mode=options.mode,
            initial_id=state.initial_id,
            selected_id=selected.id,
            final_id=state.current_id,
            best_validation_accuracy=state.best_accuracy if options.mode == "offline" else None,
            samples=state.samples,
            validations=state.validations,
            initial_tests=state.initial_tests,
            windows=state.windows,
            final_tests=final_tests,
            batches=state.batches,
            batch_size=options.batch_size,
            batch_controller=state.batch_controller,
        )
        outputs = {
            "summary": publish("proteus.combee.ace_summary@1", summary),
            "candidate": publish("proteus.candidate@1", selected),
            "final_candidate": publish("proteus.candidate@1", candidate(state.current_id)),
        }
        checkpoint = services.checkpoint(replace(state, phase="finished", outputs=outputs))
        return MethodResult(outputs=outputs, state=checkpoint)


def register(registry):
    from proteus.methods.ace.method import register as register_ace
    from proteus.operators.combee import register as register_combee

    register_ace(registry)
    register_combee(registry)
    register_sample(registry)
    registry.register_data("proteus.combee.ace_batch@1", BatchLearning)
    registry.register_data("proteus.combee.ace_summary@1", CombeeSummary)
    root = Path(__file__).parent
    registry.register(
        Plugin(
            ref=PluginRef(name="proteus.combee_ace", version="1"),
            kind="method",
            options=CombeeOptions,
            factory=CombeeAceMethod,
            source="proteus",
            implementation_hash=implementation_hash(
                {path.name: path for path in root.glob("*.py")}
            ),
            capabilities=("run.journal", "run.resume"),
            state_schema=CombeeState,
            validate=validate_combee,
        )
    )
