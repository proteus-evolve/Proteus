"""Private Formula regrading and coverage audit for serial and batched arms."""

from collections import Counter

from proteus.core.contracts import require
from proteus.core.serialization import content_hash, json_value
from proteus.evaluation.audit import RunAudit
from proteus.evaluation.records import EvaluationReceipt, Observation
from proteus.evaluation.tasks import TaskSelection, make_batch
from proteus.examples import ace_formula_audit
from proteus.methods.ace.batch import BatchLearning, CombeeOptions, CombeeSummary, SampleLearning
from proteus.methods.ace.playbook import REVISION, Playbook, PlaybookCodec, PlaybookDelta
from proteus.methods.ace.roles import AceTask, Generation, GenerationInput, Reflection
from proteus.operators.combee.ace import AceAggregation, AceRecord, parse_adds
from proteus.operators.combee.batching import initialize, observe, validate_measurements


def run_report(root, entry, recipe, lock, dataset, registry):
    if entry.variant == "ace":
        require(recipe.method.plugin.name == "proteus.ace", "sequential arm method differs")
        return ace_formula_audit.run_report(root, entry, recipe, lock, dataset, registry)
    require(recipe.method.plugin.name == "proteus.combee_ace", "batched arm method differs")
    options = CombeeOptions.from_dict(recipe.method.options)
    require((options.aggregation_mode == "single_pass") == (entry.variant == "naive_parallel")
            and (options.dynamic_batch is not None) == (entry.variant == "combee_dynamic"),
            "study variant differs from its configured method")
    audit = RunAudit(root, recipe, lock, registry)
    require(audit.manifest.run_id == entry.name, "study run identity differs")
    values, outputs = audit.values, audit.result.outputs
    require(set(outputs) == {"summary", "candidate", "final_candidate"}, "Combee result outputs differ")
    summary = values.get(outputs["summary"])
    require(type(summary) is CombeeSummary and summary.mode == entry.mode and summary.revision == REVISION,
            "Combee summary identity differs")
    selected, final = values.get(outputs["candidate"]), values.get(outputs["final_candidate"])
    require(selected == audit.candidates[summary.selected_id] and final == audit.candidates[summary.final_id],
            "Combee result candidates differ")
    require(summary.initial_id == "initial" and audit.candidates["initial"].artifact == recipe.subject.initial_artifact
            and audit.store.get(recipe.subject.initial_artifact) == Playbook.from_text(),
            "Formula baseline must use an empty initial playbook")
    offline = entry.mode == "offline"
    test_id = "test" if offline else "stream"
    require(set(summary.initial_tests) == {test_id} and set(summary.final_tests) == ({test_id} if offline else set()),
            "Combee test receipts differ")
    seen, receipts = [], []

    def cohort(receipt, evaluation_id, candidate_id, *, selection=None, reflection="(empty)"):
        result = audit.fixed_cohort(receipt, evaluation_id, dataset, candidate_id, selection=selection,
            inputs={"generation": GenerationInput(reflection=reflection, json_mode=options.json_mode)})
        seen.extend(result["rollout_ids"])
        receipts.append(receipt)
        return result

    def update(before_id, after_id, delta):
        before, after = audit.candidates[before_id], audit.candidates[after_id]
        require(delta.base_hash == before.artifact.content_hash, "playbook delta base differs")
        if not delta.records:
            require(after == before, "empty update changed the candidate")
        else:
            require(after.parent_ids == (before.id,) and audit.store.get(after.artifact)
                    == PlaybookCodec().apply(audit.store.get(before.artifact), delta),
                    "playbook update differs from its recorded evidence")

    baseline = cohort(values.get(summary.initial_tests[test_id]), test_id, "initial")
    learning = next(spec for spec in recipe.evaluations if spec.id == options.train)
    pool = make_batch(dataset, learning.tasks)
    tasks, count = pool.tasks, len(pool.tasks)
    require(len(summary.samples) == count * options.num_epochs, "Combee sample coverage differs")
    require((not summary.windows) if offline else not summary.validations, "Combee mode receipts differ")
    controller = initialize(options.dynamic_batch, pool_hash=content_hash(pool), dataset_size=count,
        bound=options.online_eval_frequency if not offline else None) if options.dynamic_batch else None
    current, best, best_accuracy, cursor = "initial", "initial", 0.0, 0
    validations, windows, sample_refs, batch_sizes = [], [], [], []
    reflection_count, curation_count = 0, 0

    def reflection_tags(reflection):
        records = json_value(reflection.bullet_tags)
        return tuple(records) if isinstance(records, list) else ()

    for batch_ref in summary.batches:
        batch = values.get(batch_ref)
        require(type(batch) is BatchLearning and not batch.rolled_back and not batch.diagnostic,
                "study contains an incomplete or rolled-back batch")
        epoch, offset = divmod(cursor, count)
        size = controller.size if controller else options.batch_size
        end = min(count, offset + size)
        if not offline:
            window = cursor // options.online_eval_frequency
            window_end = min(count, (window + 1) * options.online_eval_frequency)
            if len(windows) == window:
                require(window < len(summary.windows), "Combee online window missing")
                windows.append(cohort(values.get(summary.windows[window]), test_id, current,
                    selection=TaskSelection(task_ids=tuple(task.id for task in tasks[offset:window_end]))))
            end = min(end, window_end)
        expected = tasks[offset:end]
        require((batch.epoch, batch.start, batch.before_id, batch.task_ids) ==
                (epoch + 1, offset + 1, current, tuple(task.id for task in expected)), "Combee batch coverage/order differs")
        mapped = values.get(batch.workflow_map)
        require(mapped.succeeded and mapped.keys == batch.task_ids
                and tuple(row.outputs["learning"] for row in mapped.results) == batch.samples,
                "Combee batch differs from its workflow map")
        samples, tags = [], []
        for index, (task, sample_ref) in enumerate(zip(expected, batch.samples)):
            sample = values.get(sample_ref)
            require(type(sample) is SampleLearning and (sample.task_id, sample.epoch, sample.step, sample.before_id)
                    == (task.id, epoch + 1, offset + index + 1, current), "Combee sample binding differs")
            predictions = [values.get(ref) for ref in sample.predictions]
            reflections = [values.get(ref) for ref in sample.reflections]
            require(predictions and all(type(row) is Observation for row in predictions)
                    and all(type(row) is Reflection for row in reflections), "sample evidence types differ")
            scores = []
            local_id = current
            sample_tags = []
            for position, prediction in enumerate(predictions):
                if position:
                    previous = reflections[position - 1]
                    before = audit.candidates[local_id]
                    update(local_id, prediction.rollout.candidate_id, PlaybookDelta(
                        base_hash=before.artifact.content_hash, kind="counters", records=reflection_tags(previous)))
                    local_id = prediction.rollout.candidate_id
                require(prediction.rollout == audit.rollouts[prediction.rollout.id]
                        and prediction.rollout.candidate_id == local_id, "sample prediction candidate differs")
                path = audit.root / "evaluations" / content_hash(prediction.rollout.operation_id).split(":")[1] / "receipt.json"
                receipt = EvaluationReceipt.from_json(path.read_text())
                result = cohort(receipt, options.train, local_id, selection=TaskSelection(task_ids=(task.id,)),
                    reflection="(empty)" if position == 0 else reflections[position - 1].response)
                reference = next(row.reference for row in dataset.records
                                 if (row.task.split, row.task.id) == (task.split, task.id))
                grade = registry.create(learning.evaluator, "evaluator").grade(task, prediction.rollout, reference)
                require(prediction.row == receipt.batches[0].rows[0] and prediction.feedback == grade.feedback,
                        "sample feedback differs from the pinned metric")
                scores.append(result["accuracy"])
            if scores[0] == 1:
                require(len(predictions) == len(reflections) == 1, "correct sample must reflect once without regeneration")
            else:
                rounds = len(predictions) - 1
                require(rounds == len(reflections) and rounds <= options.max_num_rounds
                        and all(score == 0 for score in scores[:-1])
                        and (rounds == options.max_num_rounds or scores[-1] == 1), "sample regeneration protocol differs")
            for reflection in reflections:
                sample_tags.extend(reflection_tags(reflection))
            require(sample.local_id == local_id and sample.bullet_tags == tuple(sample_tags)
                    and sample.reflection == (reflections[-1].response if reflections else "(empty)")
                    and sample.pre_train_answer == Generation.from_dict(predictions[0].rollout.output).answer
                    and sample.final_answer == Generation.from_dict(predictions[-1].rollout.output).answer
                    and sample.correct == bool(scores[-1]), "sample summary differs from predictions/reflections")
            reflection_count += len(reflections)
            tags.extend(sample_tags)
            samples.append(sample)
        update(current, batch.counter_id, PlaybookDelta(base_hash=audit.candidates[current].artifact.content_hash,
                                                       kind="counters", records=tuple(tags)))
        aggregation = values.get(batch.aggregation)
        require(type(aggregation) is AceAggregation and aggregation.base == audit.candidates[batch.counter_id].artifact
                and aggregation.mode == options.aggregation_mode, "aggregation identity differs")
        plan = values.get(aggregation.plan)
        require(plan.options == options.grouping and len(plan.records) == len(samples), "aggregation source count differs")
        for task, sample, source_ref in zip(expected, samples, plan.records):
            require(values.get(source_ref) == AceRecord(sample_number=sample.step, reflection=sample.reflection,
                    context=AceTask.from_dict(task.input).context), "aggregation reflection provenance differs")
        waves = [values.get(ref) for ref in aggregation.waves]
        require(bool(waves), "aggregation wave missing")
        require(len(aggregation.proposals) == len(plan.groups)
                and aggregation.proposals == tuple(parse_adds(values.get(ref).text) for ref in waves[0].responses),
                "aggregation proposals differ from actual responses")
        needs_reducer = options.aggregation_mode == "combee" and any(item.operations for item in aggregation.proposals)
        require(len(waves) == 1 + int(needs_reducer), "aggregation wave coverage differs")
        require(not needs_reducer or len(waves[1].responses) == 1, "aggregation reducer response count differs")
        require(aggregation.reducer == (parse_adds(values.get(waves[1].responses[0]).text) if needs_reducer else None),
                "aggregation reducer differs from actual response")
        final_proposal = aggregation.proposals[0] if options.aggregation_mode == "single_pass" else aggregation.reducer
        require(aggregation.delta.records == (final_proposal.operations if final_proposal else ()), "aggregation delta differs")
        update(batch.counter_id, batch.proposed_id, aggregation.delta)
        require(batch.after_id == batch.proposed_id and batch.post_evaluation is not None, "post-curation candidate differs")
        cohort(values.get(batch.post_evaluation), options.train, batch.after_id,
               selection=TaskSelection(task_ids=batch.task_ids))
        curation_count += sum(len(wave.responses) for wave in waves)
        sample_refs.extend(batch.samples)
        batch_sizes.append(len(expected))
        cursor += len(expected)
        current = batch.after_id
        if controller:
            require(batch.timing is not None, "dynamic batch timing missing")
            controller = observe(options.dynamic_batch, controller, actual_size=len(expected),
                                 measurement=batch.timing, elapsed=values.get(batch.timing))
        else:
            require(batch.timing is None, "fixed batch unexpectedly carries dynamic timing")
        if offline and end % options.eval_steps == 0:
            require(len(validations) < len(summary.validations), "Combee validation missing")
            checked = cohort(values.get(summary.validations[len(validations)]), options.validation, current)
            validations.append(checked)
            if checked["accuracy"] > best_accuracy:
                best, best_accuracy = current, checked["accuracy"]

    require(cursor == count * options.num_epochs and tuple(sample_refs) == summary.samples, "Combee learning coverage differs")
    require(len(validations) == len(summary.validations) and len(windows) == len(summary.windows), "extra scoring receipts")
    require(summary.final_id == current and summary.selected_id == (best if offline else current)
            and summary.best_validation_accuracy == (best_accuracy if offline else None), "Combee final selection differs")
    require(summary.batch_controller == controller and summary.batch_size == options.batch_size, "Combee controller summary differs")
    if controller:
        validate_measurements(controller, values.get)
    final_test = cohort(values.get(summary.final_tests[test_id]), test_id, best) if offline else None
    require(len(seen) == len(set(seen)) and set(seen) == set(audit.rollouts), "Combee rollout coverage differs")
    audit.assert_cohort_coverage(receipts)
    roles = Counter(call["role"] for call in audit.records["actor_calls"])
    require(roles == Counter(generator=len(seen), reflector=reflection_count, curator=curation_count), "Combee role accounting differs")
    accuracy = final_test["accuracy"] if offline else sum(window["accuracy"] * window["tasks"] for window in windows) / baseline["tasks"]
    require(offline or sum(window["tasks"] for window in windows) == baseline["tasks"], "online denominator differs")
    return {"metric": "held-out test" if offline else "pre-learning online windows",
        "baseline_accuracy": baseline["accuracy"], "accuracy": accuracy, "accuracy_delta": accuracy - baseline["accuracy"],
        "baseline": baseline, "final_test": final_test, "windows": windows, "validations": validations,
        "samples": len(summary.samples), "batch_sizes": batch_sizes,
        "batch_controller": controller.to_dict() if controller else None,
        "selected_id": selected.id, "selected_artifact": selected.artifact.content_hash,
        "final_id": final.id, "final_artifact": final.artifact.content_hash,
        "manifest_hash": audit.manifest.manifest_hash,
        "role_calls": {role: roles[role] for role in ("generator", "reflector", "curator")}, **audit.managed_totals()}
