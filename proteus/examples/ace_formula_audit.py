"""Private, read-only audit of the Formula recipe's complete execution protocol."""

from collections import Counter

from proteus.core.contracts import require
from proteus.core.serialization import content_hash
from proteus.evaluation.audit import RunAudit
from proteus.evaluation.records import EvaluationReceipt, Observation
from proteus.evaluation.tasks import TaskSelection, make_batch
from proteus.methods.ace.method import AceOptions, AceSummary, SampleResult
from proteus.methods.ace.playbook import REVISION, Playbook
from proteus.methods.ace.roles import Curation, GenerationInput, Reflection


def run_report(root, entry, recipe, lock, dataset, registry):
    audit = RunAudit(root, recipe, lock, registry)
    require(audit.manifest.run_id == entry.name, "study run identity differs")
    values, outputs = audit.values, audit.result.outputs
    require(set(outputs) == {"summary", "candidate", "final_candidate"}, "ACE result outputs differ")
    summary = values.get(outputs["summary"])
    require(type(summary) is AceSummary and summary.mode == entry.mode and summary.revision == REVISION,
            "ACE summary identity differs")
    options = AceOptions.from_dict(recipe.method.options)
    selected, final = values.get(outputs["candidate"]), values.get(outputs["final_candidate"])
    require(selected == audit.candidates[summary.selected_id] and final == audit.candidates[summary.final_id],
            "ACE result candidates differ")
    require(summary.initial_id == "initial" and audit.candidates["initial"].artifact == recipe.subject.initial_artifact
            and audit.store.get(recipe.subject.initial_artifact) == Playbook.from_text(),
            "Formula baseline must use the empty initial playbook")
    offline = entry.mode == "offline"
    test_id = "test" if offline else "stream"
    require(set(summary.initial_tests) == {test_id} and
            set(summary.final_tests) == ({test_id} if offline else set()), "ACE test receipts differ")
    ordered, receipts = [], []

    def cohort(receipt, evaluation_id, candidate_id, *, selection=None, reflection="(empty)"):
        result = audit.fixed_cohort(receipt, evaluation_id, dataset, candidate_id, selection=selection,
            inputs={"generation": GenerationInput(reflection=reflection, json_mode=options.json_mode)})
        ordered.extend(result["rollout_ids"])
        receipts.append(receipt)
        return result

    baseline = cohort(values.get(summary.initial_tests[test_id]), test_id, "initial")
    learning = next(item for item in recipe.evaluations if item.id == options.train)
    tasks = make_batch(dataset, learning.tasks).tasks
    require(len(summary.samples) == len(tasks) * options.num_epochs, "ACE sample coverage differs")
    require((not summary.windows) if offline else not summary.validations, "ACE mode receipts differ")
    current, best, best_accuracy = "initial", "initial", 0.0
    validation_index, window_index, reflection_count, curation_count = 0, 0, 0, 0
    validations, windows = [], []

    for index, ref in enumerate(summary.samples):
        epoch, offset = divmod(index, len(tasks))
        task = tasks[offset]
        sample = values.get(ref)
        require(type(sample) is SampleResult and (sample.task_id, sample.epoch, sample.step, sample.before_id)
                == (task.id, epoch + 1, offset + 1, current), "ACE sample order/candidate differs")
        if not offline and index % options.online_eval_frequency == 0:
            require(window_index < len(summary.windows), "ACE online window missing")
            selection = TaskSelection(task_ids=tuple(item.id for item in tasks[index:index + options.online_eval_frequency]))
            windows.append(cohort(values.get(summary.windows[window_index]), test_id, current, selection=selection))
            window_index += 1

        predictions = [values.get(item) for item in sample.predictions]
        reflections = [values.get(item) for item in sample.reflections]
        require(len(predictions) >= 2 and all(type(item) is Observation for item in predictions)
                and all(type(item) is Reflection for item in reflections), "ACE sample evidence differs")
        require(predictions[0].rollout.candidate_id == current
                and predictions[-1].rollout.candidate_id == sample.after_id, "ACE before/after prediction differs")
        scores = []
        for position, observation in enumerate(predictions):
            rollout = observation.rollout
            require(rollout == audit.rollouts[rollout.id], "ACE sample prediction differs from exported rollout")
            path = audit.root / "evaluations" / content_hash(rollout.operation_id).split(":")[1] / "receipt.json"
            receipt = EvaluationReceipt.from_json(path.read_text())
            reflection = "(empty)" if position in (0, len(predictions) - 1) else reflections[position - 1].response
            checked = cohort(receipt, options.train, rollout.candidate_id,
                selection=TaskSelection(task_ids=(task.id,)), reflection=reflection)
            require(observation.row == receipt.batches[0].rows[0], "ACE observed score differs from its receipt")
            reference = next(item.reference for item in dataset.records if item.task.id == task.id
                             and item.task.split == task.split)
            grade = registry.create(learning.evaluator, "evaluator").grade(task, rollout, reference)
            require(observation.feedback == grade.feedback, "ACE observed feedback differs from the pinned metric")
            scores.append(checked["accuracy"])
        if scores[0] == 1:
            require(len(predictions) == 2 and len(reflections) == 1, "ACE correct-sample reflection protocol differs")
        else:
            rounds = len(predictions) - 2
            require(rounds == len(reflections) and rounds <= options.max_num_rounds
                    and all(score == 0 for score in scores[1:-2])
                    and (rounds == options.max_num_rounds or scores[-2] == 1),
                    "ACE regeneration/stopping protocol differs")
        reflection_count += len(reflections)
        require((sample.curation is not None) == ((offset + 1) % options.curator_frequency == 0),
                "ACE curator frequency differs")
        if sample.curation is not None:
            require(type(values.get(sample.curation)) is Curation, "ACE curation evidence differs")
            curation_count += 1
        current = sample.after_id

        if offline and (offset + 1) % options.eval_steps == 0:
            require(validation_index < len(summary.validations), "ACE validation missing")
            checked = cohort(values.get(summary.validations[validation_index]), options.validation, current)
            validations.append(checked)
            validation_index += 1
            if checked["accuracy"] > best_accuracy:
                best, best_accuracy = current, checked["accuracy"]

    require(validation_index == len(summary.validations) and window_index == len(summary.windows),
            "ACE has extra validation/window receipts")
    require(summary.final_id == current and summary.selected_id == (best if offline else current)
            and summary.best_validation_accuracy == (best_accuracy if offline else None),
            "ACE final/best-validation selection differs")
    final_test = cohort(values.get(summary.final_tests[test_id]), test_id, best) if offline else None
    require(ordered == list(audit.rollouts) and len(set(ordered)) == len(ordered),
            "ACE rollout coverage or prediction-before-learning order differs")
    audit.assert_cohort_coverage(receipts)
    roles = Counter(call["role"] for call in audit.records["actor_calls"])
    require(roles == Counter(generator=len(ordered), reflector=reflection_count, curator=curation_count),
            "ACE role-call accounting differs")
    accuracy = final_test["accuracy"] if offline else sum(
        window["accuracy"] * window["tasks"] for window in windows) / baseline["tasks"]
    require(offline or sum(window["tasks"] for window in windows) == baseline["tasks"],
            "ACE online window denominator differs")
    return {"metric": "held-out test" if offline else "pre-learning online windows",
        "baseline_accuracy": baseline["accuracy"], "accuracy": accuracy,
        "accuracy_delta": accuracy - baseline["accuracy"], "baseline": baseline, "final_test": final_test,
        "windows": windows, "validations": validations, "samples": len(summary.samples),
        "selected_id": selected.id, "selected_artifact": selected.artifact.content_hash,
        "final_id": final.id, "final_artifact": final.artifact.content_hash,
        "manifest_hash": audit.manifest.manifest_hash,
        "role_calls": {role: roles[role] for role in ("generator", "reflector", "curator")}, **audit.managed_totals()}
