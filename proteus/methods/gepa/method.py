"""Pinned GEPA optimization with managed calls, explicit lineage and durable replay."""

from __future__ import annotations

import functools
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from proteus.artifacts.codecs import TextDelta
from proteus.core.contracts import FeedbackVisibility, OperationStatus, PluginRef, StatePolicy, TEXT, require
from proteus.core.methods import MethodResult
from proteus.core.serialization import Contract, content_hash, json_value
from proteus.evaluation.tasks import TaskSelection
from proteus.methods.gepa.reference import REPOSITORY, REVISION, load_reference
from proteus.methods.gepa.sampling import EpochSampler
from proteus.methods.gepa.state import (
    BatchData, BridgeState, GepaState, GepaSummary, ProposalDecision, restore_engine, seal,
)
from proteus.recipes.builtins import Text


@dataclass(frozen=True, kw_only=True)
class GepaOptions(Contract):
    train: str = field(metadata=TEXT)
    validation: str = field(metadata=TEXT)
    reflection_stage: str = field(metadata=TEXT)
    max_metric_calls: int = field(metadata={"minimum": 1})
    minibatch_size: int = field(default=3, metadata={"minimum": 1})
    parent_policy: str = "parent"
    archive_policy: str = "frontier"
    final_policy: str = "final"
    skip_perfect_score: bool = True
    perfect_score: float = 1.0
    acceptance: Literal["strict_improvement", "improvement_or_equal"] = "strict_improvement"
    test_evaluations: tuple[str, ...] = field(default=(), metadata={"uniqueItems": True})


def evaluation_bound(options, validation_size):
    # Upstream checks its metric-call threshold BETWEEN iterations. A final
    # single-mutation iteration can use parent + child minibatches + validation.
    return validation_size if options.max_metric_calls <= validation_size else (
        options.max_metric_calls - 1 + 2 * options.minibatch_size + validation_size)


def validate_gepa(recipe, options, registry):
    require(recipe.subject is not None and recipe.execution is not None, "GEPA requires subject/execution")
    require(recipe.subject.initial_artifact.codec == PluginRef(name="proteus.text_components", version="1"),
            "GEPA reference method requires text components")
    require(bool(recipe.subject.initial_artifact.components), "GEPA requires at least one mutable text component")
    require(recipe.execution.state_policy == StatePolicy.INDEPENDENT, "GEPA reference tasks must be independent")
    require(recipe.tasks is None, "GEPA uses its declared evaluation pools")
    evaluations = {item.id: item for item in recipe.evaluations}
    ids = (options.train, options.validation, *options.test_evaluations)
    require(len(ids) == len(set(ids)) and set(ids) == set(evaluations), "GEPA evaluation IDs differ or go unused")
    require(not {"summary", "candidate", "archive"}.intersection(options.test_evaluations),
            "GEPA test evaluation IDs collide with reserved method outputs")
    train, validation = evaluations[options.train], evaluations[options.validation]
    require(train.purpose == "optimization" and validation.purpose == "validation", "GEPA needs train/validation purposes")
    require(all(item.visibility == FeedbackVisibility.OBSERVED and item.feedback_timing == "after_batch"
                for item in (train, validation)) and validation.selection_eligible,
            "GEPA needs observed after-batch train/validation feedback and eligible validation")
    pools = [evaluations[key].tasks for key in ids]
    for index, pool in enumerate(pools):
        require(all(pool.dataset_hash != other.dataset_hash or pool.split != other.split
                    or not set(pool.task_ids).intersection(other.task_ids) for other in pools[index + 1:]),
                "GEPA train/validation/test pools overlap")
    require(all(evaluations[key].purpose == "test" for key in options.test_evaluations), "final evaluations must be hidden tests")
    bound = evaluation_bound(options, len(validation.tasks.task_ids)) + sum(
        len(evaluations[key].tasks.task_ids) for key in options.test_evaluations)
    require(recipe.budget.max_evaluations is None or recipe.budget.max_evaluations >= bound,
            f"GEPA threshold is checked between iterations; evaluation hard cap must be at least {bound}")
    require(len(recipe.stages) == 1 and recipe.stages[0].id == options.reflection_stage,
            "GEPA consumes one declared reflection stage")
    stage = recipe.stages[0]
    require(dict(stage.input_types) == {"prompt": "proteus.text@1"}
            and dict(stage.output_types) == {"response": "proteus.actor_result@1"}, "GEPA reflection stage port mismatch")
    slots = {options.parent_policy: "parent", options.archive_policy: "archive", options.final_policy: "final"}
    require(len(slots) == 3 and set(recipe.search) == set(slots), "GEPA search slots must be distinct and consumed")
    for key, role in slots.items():
        require("search." + role in registry.get(recipe.search[key].plugin, "policy").capabilities,
                "GEPA policy capability mismatch")
    if recipe.subject.runner.plugin.name == "proteus.prompt_subject":
        component = recipe.subject.runner.options.get("component", "system.txt")
        require(tuple(item.path for item in recipe.subject.initial_artifact.components) == (component,),
                "the builtin prompt subject must consume the only mutable GEPA component")
    require(recipe.reference.code == REPOSITORY and recipe.reference.revision == REVISION,
            "GEPA method requires its pinned reference identity")
    require(recipe.reference.profile != "native", "GEPA requires a reference, adapted or extended profile")
    if recipe.reference.profile == "reference":
        expected = {options.parent_policy: "proteus.gepa_parent", options.archive_policy: "proteus.gepa_frontier",
                    options.final_policy: "proteus.gepa_best"}
        require(all(recipe.search[key].plugin == PluginRef(name=name, version="1") for key, name in expected.items())
                and stage.operator.plugin == PluginRef(name="proteus.prompt", version="1")
                and stage.max_attempts == 1, "changed GEPA policies/stages require an adapted or extended profile")


class BridgeAbort(BaseException):
    """Carry fatal service errors through upstream observational catch boundaries."""
    def __init__(self, error):
        self.error = error


def authoritative(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            if args and isinstance(args[0], Bridge) and args[0].fatal is None:
                args[0].fatal = exc
            raise BridgeAbort(exc) from exc
    return wrapped


class Pool:
    def __init__(self, batch):
        self.batch = batch
        self.tasks = {task.id: task for task in batch.tasks}

    def all_ids(self):
        return list(self.tasks)

    def fetch(self, ids):
        require(set(ids) <= self.tasks.keys(), "upstream requested an unknown task")
        return [self.tasks[key] for key in ids]

    def __len__(self):
        return len(self.tasks)


class Bridge:
    propose_new_texts = None  # Use the actual upstream stateless renderer/parser.

    def __init__(self, services, options, train, validation, initial, work, restored=None):
        self.services, self.options = services, options
        self.train, self.validation = Pool(train), Pool(validation)
        self.initial, self.work = initial, work
        self.accepted = [initial.id]
        self.decisions = []
        self.seed_data, self.validation_protocol = None, None
        self.sampler = EpochSampler(train, options.minibatch_size, services.rng)
        self.evaluations = self.reflection_calls = 0
        self.phase, self.bootstrap = "idle", True
        self.pending = self.pending_object = self.parent = None
        self.state = None
        self.fatal = None
        self.train_ids = None
        self.restored = restored
        self.suppress_checkpoint = restored is not None
        self.checkpoint = None
        if restored:
            self._restore(restored.bridge)

    def candidate(self, candidate_id):
        return next(item for item in self.services.candidates if item.id == candidate_id)

    @authoritative
    def log(self, message):
        with (self.services.root / "private/gepa/engine.log").open("a", encoding="utf-8") as stream:
            stream.write(str(message) + "\n")

    def texts(self, candidate_id):
        return dict(self.services.artifacts.get(self.candidate(candidate_id).artifact).components)

    def _restore(self, state):
        self.accepted, self.decisions = list(state.accepted_ids), list(state.decisions)
        self.seed_data, self.validation_protocol = state.seed, state.validation_protocol
        self.evaluations, self.reflection_calls = state.evaluations, state.reflection_calls
        self.sampler.restore(state.sampler)

    def _bind(self, state):
        require(len(state.program_candidates) == len(self.accepted), "accepted GEPA program mapping differs")
        require(len(state.parent_program_for_candidate) == len(self.accepted)
                and len(state.prog_candidate_val_subscores) == len(self.accepted), "GEPA lineage/score population differs")
        for index, candidate_id in enumerate(self.accepted):
            require(state.program_candidates[index] == self.texts(candidate_id), "GEPA program/artifact identity differs")
            parents = tuple(self.accepted[parent] for parent in state.parent_program_for_candidate[index] if parent is not None)
            require(parents == self.candidate(candidate_id).parent_ids, "GEPA program/candidate lineage differs")
            batches = [batch for batch in self.services.evaluations if batch.candidate_id == candidate_id
                       and batch.protocol_hash == self.validation_protocol]
            require(bool(batches) and {row.task_id: row.score for row in batches[-1].rows}
                    == state.prog_candidate_val_subscores[index], "GEPA validation scores differ from committed evidence")
        require(state.total_num_evals == self.evaluations, "GEPA metric-call count differs from executed trials")
        self.state = state

    @authoritative
    def evaluate(self, batch, candidate, capture_traces=False):
        validation = batch == self.validation.fetch(self.validation.all_ids())
        if self.bootstrap:
            require(validation and candidate == self.texts(self.initial.id), "unexpected GEPA seed bootstrap")
            self.bootstrap = False
            if self.restored:
                # The reference re-evaluates seed BEFORE loading its state. Return
                # only this original, checkpoint-bound result, without a new call.
                self.services.values.get(self.seed_data.receipt)
                return self._upstream_batch(self.seed_data)
            actual, evaluation_id = self.initial, self.options.validation
        elif validation:
            require(self.phase == "candidate_decision" and candidate is self.pending_object,
                    "unexpected GEPA validation candidate/phase")
            actual, evaluation_id = self.pending, self.options.validation
            self.phase = "acceptance"
        else:
            require(self.train_ids is not None and batch == self.train.fetch(self.train_ids),
                    "GEPA minibatch differs from sampled tasks")
            if self.phase == "parent_train":
                require(candidate is self.state.program_candidates[self.accepted.index(self.parent.id)],
                        "GEPA parent object differs from selected program")
                actual = self.parent
                self.phase = "reflection"
            else:
                require(self.phase == "child_train" and candidate == self.texts(self.pending.id)
                        and candidate is not self.state.program_candidates[self.accepted.index(self.parent.id)],
                        "GEPA proposed candidate differs from recorded delta")
                actual = self.pending
                self.pending_object = candidate
                self.phase = "candidate_decision"
            evaluation_id = self.options.train
        receipt = self.services.evaluate(actual, evaluation_id, selection=TaskSelection(task_ids=tuple(task.id for task in batch)))
        require(len(receipt.batches) == 1 and len(receipt.batches[0].rows) == len(batch), "GEPA needs complete public evaluation")
        evaluated = receipt.batches[0]
        require(all(row.score is not None for row in evaluated.rows), "GEPA cannot impute missing/infrastructure scores")
        by_id = {item.id: item for item in self.services.rollouts}
        rollouts = [by_id[key] for key in receipt.rollout_ids]
        trajectories = []
        for task, rollout, row in zip(batch, rollouts, evaluated.rows):
            trajectories.append({"Inputs": json_value(task.input), "Generated Outputs": json_value(rollout.output),
                "Feedback": {"score": row.score, "feedback": [json_value(self.services.values.get(ref).value) for ref in row.feedback]},
                "Trace": [json_value(self.services.values.get(ref)) for ref in rollout.trace]})
        data = BatchData(receipt=self.services.values.publish("proteus.eval.receipt@1", receipt),
            outputs=tuple(item.output for item in rollouts), scores=tuple(row.score for row in evaluated.rows),
            trajectories=tuple(trajectories) if capture_traces else None)
        self.evaluations += len(batch)
        if self.seed_data is None:
            self.seed_data, self.validation_protocol = data, evaluated.protocol_hash
        elif validation:
            require(evaluated.protocol_hash == self.validation_protocol, "GEPA validation protocol changed")
        return self._upstream_batch(data)

    @staticmethod
    def _upstream_batch(data):
        from gepa.core.adapter import EvaluationBatch
        return EvaluationBatch(outputs=json_value(data.outputs), scores=list(data.scores),
            trajectories=json_value(data.trajectories), num_metric_calls=len(data.scores))

    @authoritative
    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        require(self.phase == "reflection" and candidate is self.state.program_candidates[self.accepted.index(self.parent.id)],
                "GEPA reflection parent differs")
        return {name: eval_batch.trajectories for name in components_to_update}

    @authoritative
    def reflect(self, prompt):
        require(type(prompt) is str, "GEPA managed reflection currently requires text prompts")
        ref = self.services.values.publish("proteus.text@1", Text(text=prompt))
        result = self.services.stage(self.options.reflection_stage, {"prompt": ref})
        require(result.status == OperationStatus.COMPLETED, "GEPA reflection stage failed")
        response = self.services.values.get(result.outputs["response"])
        require(response.status == OperationStatus.COMPLETED, "GEPA reflection actor failed")
        self.reflection_calls += 1
        return response.text

    def view(self):
        return self.services.selection_view(self.services.archive_snapshot(), self.options.validation,
            protocol_hash=self.validation_protocol, candidate_ids=tuple(self.accepted))

    @authoritative
    def select_candidate_idx(self, state):
        self._bind(state)
        require(self.phase == "idle", "GEPA started another parent before completing a proposal")
        view = self.view()
        retained = self.services.retain_candidates(self.options.archive_policy, view).result.selected
        require(bool(retained), "GEPA archive policy left no parents")
        search = self.services.recipe.search
        reference_pair = (search[self.options.archive_policy].plugin == PluginRef(name="proteus.gepa_frontier", version="1")
                          and search[self.options.parent_policy].plugin == PluginRef(name="proteus.gepa_parent", version="1"))
        if not reference_pair:
            # Adapted pairs select within the retained population, preserving its
            # accepted-program order. The reference pair must retain full indices:
            # the upstream sampler performs the same pruning internally, and
            # renumbering its integer sets can change tie/RNG iteration behavior.
            view = self.services.selection_view(self.services.archive_snapshot(), self.options.validation,
                protocol_hash=self.validation_protocol, candidate_ids=tuple(key for key in self.accepted if key in retained))
        chosen = self.services.select_parents(self.options.parent_policy, view).result.selected
        require(len(chosen) == 1 and chosen[0] in retained, "GEPA requires one retained parent")
        self.parent = self.candidate(chosen[0])
        self.phase = "parent_train"
        return self.accepted.index(chosen[0])

    @authoritative
    def next_minibatch_ids(self, loader, state):
        require(loader is self.train, "GEPA changed its training loader")
        self.train_ids = self.sampler.select(state.i).task_ids
        return list(self.train_ids)

    @authoritative
    def on_proposal_end(self, event):
        require(self.phase == "reflection" and self.pending is None, "GEPA proposal phase differs")
        updates = event["new_instructions"]
        require(bool(updates) and set(updates) <= self.texts(self.parent.id).keys(), "GEPA proposed unknown/empty components")
        self.pending = self.services.fork_candidate(self.parent, delta=TextDelta(
            base_hash=self.parent.artifact.content_hash, put=updates))
        self.phase = "child_train"

    def _decision(self, disposition, reason):
        decision = ProposalDecision(candidate_id=self.pending.id, parent_id=self.parent.id,
            iteration=self.state.i, disposition=disposition, reason=reason)
        self.services.values.publish("proteus.gepa.proposal@1", decision)
        self.decisions.append(decision)
        self.pending = self.pending_object = None
        self.phase = "idle"

    @authoritative
    def on_candidate_accepted(self, event):
        require(self.phase == "acceptance" and event["new_candidate_idx"] == len(self.accepted)
                and list(event["parent_ids"]) == [self.accepted.index(self.parent.id)], "GEPA acceptance mapping differs")
        self.accepted.append(self.pending.id)
        self._decision("accepted", self.options.acceptance)

    @authoritative
    def on_candidate_rejected(self, event):
        require(self.phase == "candidate_decision" and self.pending is not None, "GEPA rejection mapping differs")
        self._decision("rejected", event["reason"])

    @authoritative
    def on_iteration_start(self, event):
        self._bind(event["state"])

    @authoritative
    def on_iteration_end(self, event):
        if self.fatal is not None:
            return  # Preserve the original error while upstream unwinds its finally block.
        require(self.pending is None, "GEPA iteration lost an unresolved proposal")
        self._bind(event["state"])
        self.phase = "idle"  # includes skip-perfect-score/no-proposal iterations

    @authoritative
    def on_optimization_end(self, event):
        self._bind(event["final_state"])

    @authoritative
    def get_adapter_state(self):
        require(self.phase == "idle" and self.seed_data is not None, "GEPA checkpoint is not at an iteration boundary")
        return BridgeState(accepted_ids=tuple(self.accepted), seed=self.seed_data, validation_protocol=self.validation_protocol,
            sampler=self.sampler.snapshot(), decisions=tuple(self.decisions), evaluations=self.evaluations,
            reflection_calls=self.reflection_calls).to_dict()

    @authoritative
    def set_adapter_state(self, payload):
        if not payload:
            require(self.restored is None, "missing persisted GEPA adapter state")
            return
        state = BridgeState.from_dict(payload)
        require(self.restored is not None and state == self.restored.bridge, "GEPA engine/bridge checkpoints differ")
        self._restore(state)

    @authoritative
    def on_state_saved(self, event):
        if self.suppress_checkpoint:
            self.suppress_checkpoint = False
            require(BridgeState.from_dict(self.get_adapter_state()) == self.restored.bridge,
                    "GEPA resume changed the committed checkpoint")
            return
        state = GepaState(train=self.train.batch, validation=self.validation.batch,
            bridge=BridgeState.from_dict(self.get_adapter_state()), engine_hash=seal(self.services, self.work))
        self.checkpoint = self.services.checkpoint(state, archive=self.services.archive_snapshot())


class GepaMethod:
    def __init__(self, options):
        self.options = options
        self.gepa = load_reference()

    def run(self, services, restored_state):
        restored = GepaState.from_dict(restored_state.state.payload) if restored_state else None
        if restored and restored.phase == "finished":
            return MethodResult(outputs=restored.outputs, state=restored_state)
        if restored:
            initial = next(item for item in services.candidates if item.id == restored.bridge.accepted_ids[0])
            train, validation = restored.train, restored.validation
        else:
            initial = services.initial_candidate()
            train, validation = services.task_pool(self.options.train), services.task_pool(self.options.validation)
        temporary_root = services.root / "private/gepa"
        temporary_root.mkdir(parents=True, exist_ok=True)
        # A crash may leave a stale work directory. Only the manifest-bound sealed
        # checkpoint is ever copied into this fresh engine directory.
        with tempfile.TemporaryDirectory(prefix="engine-", dir=temporary_root) as directory:
            work = Path(directory)
            if restored:
                restore_engine(services, work, restored.engine_hash)
            bridge = Bridge(services, self.options, train, validation, initial, work, restored)
            try:
                result = self.gepa.optimize(seed_candidate=bridge.texts(initial.id), trainset=bridge.train,
                    valset=bridge.validation, adapter=bridge, reflection_lm=bridge.reflect,
                    candidate_selection_strategy=bridge, batch_sampler=bridge, reflection_minibatch_size=None,
                    max_metric_calls=self.options.max_metric_calls, skip_perfect_score=self.options.skip_perfect_score,
                    perfect_score=self.options.perfect_score, acceptance_criterion=self.options.acceptance,
                    use_merge=False, cache_evaluation=False, track_best_outputs=False, seed=services.recipe.seed,
                    display_progress_bar=False, raise_on_exception=True, run_dir=str(work), callbacks=[bridge], logger=bridge)
                require(tuple(result.candidates) == tuple(bridge.texts(key) for key in bridge.accepted),
                        "GEPA final candidates differ from accepted artifacts")
                view = bridge.view()
                selection = services.select_final(self.options.final_policy, view).result.selected
                require(len(selection) == 1, "GEPA final policy must choose one candidate")
                best = bridge.accepted[result.best_idx]
                if services.recipe.reference.profile == "reference":
                    require(selection == (best,), "GEPA reference final selection differs")
                summary = GepaSummary(revision=REVISION, accepted_ids=tuple(bridge.accepted), upstream_best_id=best,
                    selected_ids=selection, decisions=tuple(bridge.decisions), metric_calls=bridge.evaluations,
                    reflection_calls=bridge.reflection_calls, iterations=bridge.state.i + 1,
                    stop_threshold=self.options.max_metric_calls, evaluation_upper_bound=evaluation_bound(self.options, len(validation.tasks)))
                selected = bridge.candidate(selection[0])
                outputs = {"summary": services.values.publish("proteus.gepa.summary@1", summary),
                    "candidate": services.values.publish("proteus.candidate@1", selected)}
                for evaluation_id in self.options.test_evaluations:
                    receipt = services.evaluate(selected, evaluation_id)
                    outputs[evaluation_id] = services.values.publish("proteus.eval.receipt@1", receipt)
                outputs["archive"] = services.archive_snapshot()
                checkpoint = services.checkpoint(GepaState(train=train, validation=validation,
                    bridge=BridgeState.from_dict(bridge.get_adapter_state()), phase="finished", outputs=outputs),
                    archive=outputs["archive"])
                return MethodResult(outputs=outputs, state=checkpoint)
            except BridgeAbort as exc:
                raise bridge.fatal or exc.error


def register(registry):
    from proteus.core.plugins import Plugin, implementation_hash
    from proteus.methods.gepa.search import register as register_search
    register_search(registry)
    registry.register_data("proteus.gepa.proposal@1", ProposalDecision)
    registry.register_data("proteus.gepa.summary@1", GepaSummary)
    root = Path(__file__).resolve().parent
    registry.register(Plugin(ref=PluginRef(name="proteus.gepa", version="1"), kind="method", options=GepaOptions,
        factory=GepaMethod, implementation_hash=content_hash({"bridge": implementation_hash(
            {path.name: path for path in root.glob("*.py")}), "revision": REVISION}), source="proteus",
        capabilities=("run.resume", "run.journal"), state_schema=GepaState,
        validate=lambda recipe, options: validate_gepa(recipe, options, registry)))
