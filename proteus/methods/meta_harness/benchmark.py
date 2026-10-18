"""Managed offline/online memory learning and validation for one candidate."""

from dataclasses import replace

from proteus.core.contracts import Outcome, require
from proteus.evaluation.tasks import TaskSelection
from proteus.execution.code_records import CodeCall, CodeState
from proteus.methods.meta_harness.records import BenchmarkResult


def metadata(services, rollout):
    rows = [
        event["metadata"]
        for ref in rollout.trace
        for event in services.values.get(ref).events
        if event.get("kind") == "prediction_metadata"
    ]
    require(
        len(rows) == 1 and set(rows[0]) == {"upstream_metadata", "prompt_info"},
        "Meta-Harness prediction metadata is missing or malformed",
    )
    return rows[0]


def evaluate(services, candidate, evaluation_id, memory, *, selection=None):
    session = services.open_evaluation(
        evaluation_id,
        selection=selection,
        inputs={"memory": services.values.publish("proteus.code.state@1", memory)},
    )
    observations = [session.step(candidate) for _ in session.batch.tasks]
    receipt = session.finish()
    ref = services.values.publish("proteus.eval.receipt@1", receipt)
    if any(o.rollout.status == Outcome.INFRA_ERROR for o in observations):
        raise RuntimeError("candidate evaluation infrastructure failed; no score can be inferred")
    valid = all(
        o.rollout.status == Outcome.SUCCEEDED and type(o.rollout.output) is str
        for o in observations
    )
    if valid:
        memory = CodeState.from_dict(
            services.values.get(observations[-1].rollout.state_after).payload["code_memory"]
        )
    return ref, receipt, observations, memory, valid


def benchmark(services, candidate, system, profile, options):
    memory = CodeState(artifact_hash=candidate.artifact.content_hash)
    result = BenchmarkResult(
        system=system,
        candidate_id=candidate.id,
        dataset=profile.name,
        seed=profile.seed,
        memory=memory,
    )
    training, learning, validations = [], [], []
    train = services.task_pool(profile.train)
    supervision = services.supervised_batch(profile.train) if options.mode == "offline" else None
    best_accuracy, best_memory, best_epoch = -1.0, None, 0
    for epoch in range(options.num_epochs):
        for offset in range(0, len(train.tasks), options.batch_size):
            tasks = train.tasks[offset : offset + options.batch_size]
            rows = []
            if supervision is not None:
                for task, target in zip(
                    tasks, supervision.references[offset : offset + len(tasks)]
                ):
                    rows.append(
                        {
                            "input": task.input,
                            "prediction": target["target"],
                            "ground_truth": target["target"],
                            "was_correct": True,
                            **{
                                k: v
                                for k, v in target["extras"].items()
                                if k not in ("input", "prediction", "ground_truth", "was_correct")
                            },
                        }
                    )
            else:
                ref, receipt, observations, memory, valid = evaluate(
                    services,
                    candidate,
                    profile.train,
                    memory,
                    selection=TaskSelection(task_ids=tuple(task.id for task in tasks)),
                )
                training.append(ref)
                if not valid:
                    return replace(
                        result,
                        memory=memory,
                        training=tuple(training),
                        learning=tuple(learning),
                        diagnostic="training prediction failed",
                    )
                for task, observation in zip(tasks, observations):
                    feedback = observation.feedback.value
                    require(
                        observation.row.score in (0, 1), "training feedback requires a binary score"
                    )
                    row = {
                        "input": task.input,
                        "prediction": observation.rollout.output,
                        "ground_truth": feedback["ground_truth"],
                        "was_correct": bool(observation.row.score),
                        "metadata": metadata(services, observation.rollout)["upstream_metadata"],
                    }
                    if feedback["metrics"]:
                        row["metrics"] = feedback["metrics"]
                    row.update({k: v for k, v in feedback["extras"].items() if k not in row})
                    rows.append(row)
            learned = services.invoke_candidate(
                candidate,
                CodeCall(method="learn_from_batch", state=memory, args=(rows,), seed=profile.seed),
            )
            learning.append(services.values.publish("proteus.code.result@1", learned))
            if learned.status == Outcome.INFRA_ERROR:
                raise RuntimeError(learned.diagnostic)
            if learned.status != Outcome.SUCCEEDED:
                return replace(
                    result,
                    memory=memory,
                    learning=tuple(learning),
                    training=tuple(training),
                    diagnostic=learned.diagnostic,
                )
            memory = learned.state
        if options.mode == "offline":
            ref, receipt, _, memory, valid = evaluate(
                services, candidate, profile.validation, memory
            )
            validations.append(ref)
            accuracy = receipt.batches[0].mean_score() if receipt.batches else None
            if not valid or accuracy is None:
                return replace(
                    result,
                    memory=memory,
                    epoch_validations=tuple(validations),
                    learning=tuple(learning),
                    diagnostic="epoch validation failed",
                )
            if accuracy > best_accuracy:
                best_accuracy, best_memory, best_epoch = accuracy, memory, epoch
    if best_memory is not None and best_epoch < options.num_epochs - 1:
        memory = best_memory
    # Upstream saves learned memory before its final validation pass. Hidden
    # tests later load this exact state; validation-time mutations do not leak in.
    saved = memory
    ref, receipt, observations, _, valid = evaluate(services, candidate, profile.validation, memory)
    rows = receipt.batches[0].rows if receipt.batches else ()
    if not valid or any(row.score is None for row in rows) or len(rows) != len(observations):
        return replace(
            result,
            memory=saved,
            validation=ref,
            epoch_validations=tuple(validations),
            learning=tuple(learning),
            training=tuple(training),
            diagnostic="final validation failed",
        )
    tasks = services.task_pool(profile.validation).tasks
    lengths = []
    for task, observation in zip(tasks, observations):
        info = metadata(services, observation.rollout)["prompt_info"]
        length = info.get("prompt_len") or 0
        require(type(length) is int and length >= 0, "invalid last prompt character count")
        lengths.append(max(0, length - len(task.input)) if length else 0)
    return replace(
        result,
        complete=True,
        memory=saved,
        correct=sum(int(row.score) for row in rows),
        total=len(rows),
        context_chars=int(sum(lengths) / len(lengths)),
        validation=ref,
        epoch_validations=tuple(validations),
        learning=tuple(learning),
        training=tuple(training),
    )
