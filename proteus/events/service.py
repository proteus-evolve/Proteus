"""Journaled pure transitions, immutable observations and recoverable interventions."""

import time
import uuid

from proteus.core.contracts import DataRef, EvaluationBatch, FeedbackVisibility, require
from proteus.core.recovery import RecoveryRequired, _load, _write
from proteus.core.serialization import content_hash
from proteus.events.records import (
    ClockSample, EventAction, EventDecision, EventObservation, InterventionDispatch, InterventionPlan, Transition,
)
from proteus.evidence.store import EvidenceStore


DECISION = "proteus.event.decision@1"
PLAN = "proteus.event.plan@1"
DISPATCH = "proteus.event.dispatch@1"


def intervention_prompt(plan):
    actions = "\n\n".join(f"[{action.name}]\n{action.prompt}" for action in plan.actions)
    return plan.context + "\n\n" + actions if plan.context else actions


class EventService:
    def __init__(self, services, policy_id):
        self.services = services
        self.spec = next((item for item in services.recipe.event_policies if item.id == policy_id), None)
        require(self.spec is not None, "unknown declared event policy")
        self.definition = services.registry.get(self.spec.operator.plugin, "event_policy")
        self.policy = services.registry.create(self.spec.operator, "event_policy")
        self.gateway = services.gateway(self.spec.gateway)
        self.identity_hash = content_hash({"gateway": self.gateway.identity, "policy": self.spec})
        self.root = services.root / "private/events" / content_hash(policy_id).split(":")[1]
        if not hasattr(services, "_event_invocation"):
            services._event_invocation = uuid.uuid4().hex

    def _path(self, category, key):
        return self.root / category / (content_hash(key).split(":")[1] + ".json")

    def _decisions(self):
        return sorted(((ref, value) for ref, value in self.services.values._values.values()
                       if ref.kind == DECISION and value.policy_id == self.spec.id), key=lambda pair: pair[1].sequence)

    def _score(self, result_ref):
        require(result_ref.kind == "proteus.gateway.result@1", "event needs a formal gateway result")
        result = self.services.values.get(result_ref)
        require(result.ticket.identity_hash == content_hash(self.gateway.identity), "event result belongs to another gateway")
        require(self.gateway.store.poll(result.ticket.worker_id, result.ticket.id()).result == result,
                "event result differs from the formal gateway receipt")
        if result.status == "failed":
            return result.ticket.worker_id, None, False
        store = EvidenceStore(self.services.root / "evidence")
        view = store.verify(result.evidence)
        stage = next(item for item in self.services.recipe.stages
                     if item.id == self.gateway.spec.members[result.ticket.worker_id])
        identity = self.gateway.identity
        require((view.run_id, view.config_hash, view.lock_hash, view.stage_id, view.role, view.access,
                 view.candidate_ids) == (identity.run_id, identity.config_hash, identity.lock_hash,
                 stage.id, stage.role, stage.evidence, (result.candidate.id,)), "event evidence exceeds its policy")
        operation = "gateway:" + result.ticket.id() + ":evaluation"
        entries = [entry for entry in view.entries if entry.kind == "evaluation"
                   and entry.operation_id == operation and entry.evaluation_id == result.ticket.evaluation_id
                   and entry.candidate_id == result.candidate.id]
        require(len(entries) == 1, "event needs exactly one released formal evaluation")
        batch = EvaluationBatch.from_json(store.read(result.evidence, entries[0].path).decode())
        require(batch.run_id == identity.run_id and batch.operation_id == operation
                and batch.candidate_id == result.candidate.id and batch.artifact_hash == result.ticket.artifact.content_hash
                and batch.visibility == FeedbackVisibility.OBSERVED, "event evaluation binding differs")
        return result.ticket.worker_id, batch.mean_score(), True

    def observe(self, operation_id, result):
        worker_id, score, sample = self._score(result)
        return self._advance(operation_id, worker_id, "evaluation", result=result, score=score, sample=sample)

    def tick(self, operation_id, worker_id):
        return self._advance(operation_id, worker_id, "tick")

    def _advance(self, operation_id, worker_id, kind, *, result=None, score=None, sample=False):
        require(type(operation_id) is str and bool(operation_id.strip()), "event operation must be named")
        require(worker_id in self.gateway.spec.members, "event worker is outside the declared gateway")
        request = {"identity_hash": self.identity_hash, "operation_id": operation_id, "worker_id": worker_id,
                   "kind": kind, "result": result}
        # Every formal result is observed once per policy, even under a new operation ID.
        source = content_hash([self.identity_hash, result if result is not None else [operation_id, worker_id]])

        def advance():
            _write(self._path("operations", operation_id), request)
            decisions = self._decisions()
            old = next((ref for ref, item in decisions if item.observation.source_key == source), None)
            if old is not None:
                return old
            path = self._path("observations", source)
            if path.exists():
                observation = EventObservation.from_dict(_load(path))
            else:
                observation = EventObservation(source_key=source, worker_id=worker_id, kind=kind, result=result,
                    score=score, plateau_sample=sample, clock=ClockSample(invocation=self.services._event_invocation,
                    monotonic_seconds=time.monotonic()))
                _write(path, observation)
            require((observation.source_key, observation.worker_id, observation.kind, observation.result,
                observation.score, observation.plateau_sample) == (source, worker_id, kind, result, score, sample),
                "saved event observation differs from its formal input")
            previous = decisions[-1] if decisions else None
            state = self.definition.state_schema.from_dict(previous[1].payload) if previous else self.policy.initial_state()
            require(type(state) is self.definition.state_schema, "event policy initial state has the wrong schema")
            transition = self._transition(observation, state)
            decision = EventDecision(policy_id=self.spec.id, identity_hash=self.identity_hash,
                plugin=self.definition.ref, state_version=self.definition.state_version,
                sequence=len(decisions) + 1, previous=previous[0] if previous else None,
                state_before_hash=content_hash(state), observation=observation,
                payload=transition.state.to_dict(), actions=transition.actions)
            return self.services.values.publish(DECISION, decision)

        return self.services._record("event." + kind, request, advance, decode=DataRef.from_dict)

    def _transition(self, observation, state):
        transition = self.policy.advance(observation, state)
        require(type(transition) is Transition and type(transition.state) is self.definition.state_schema,
                "event policy must return a transition with its declared state schema")
        require(type(transition.actions) is tuple and all(type(item) is EventAction for item in transition.actions),
                "event transition actions must be a tuple of typed EventAction records")
        require(all(item.worker_id in self.gateway.spec.members for item in transition.actions),
                "event action targets an undeclared worker")
        return transition

    def _decision(self, ref):
        require(ref.kind == DECISION, "expected an event decision")
        value = self.services.values.get(ref)
        require(value.policy_id == self.spec.id and value.identity_hash == self.identity_hash,
                "event decision belongs to another policy")
        return value

    def apply(self, decision_ref, *, context=""):
        require(type(context) is str, "intervention context must be text")
        decision = self._decision(decision_ref)
        outputs = []
        for worker_id in dict.fromkeys(item.worker_id for item in decision.actions):
            key = [decision_ref, worker_id]

            def existing():
                for ref, value in self.services.values._values.values():
                    if ref.kind == DISPATCH:
                        plan = self.services.values.get(value.plan)
                        if plan.decision == decision_ref and plan.worker_id == worker_id:
                            require(plan.context == context, "intervention context changed after dispatch")
                            return ref
                return None

            # This read is journaled too: replay must not skip the original inner
            # plan/control/start operations because a later dispatch file exists.
            prior = self.services._record("event.applied", {"key": key, "context": context}, existing,
                decode=lambda value: DataRef.from_dict(value) if value is not None else None,
                encode=lambda value: value.to_dict() if value is not None else None)
            if prior is not None:
                outputs.append(prior)
                continue
            worker = self.services.worker(worker_id)
            path = self._path("plans", key)
            actions = tuple(item for item in decision.actions if item.worker_id == worker_id)

            def make_plan():
                if path.exists():
                    plan = InterventionPlan.from_dict(_load(path))
                else:
                    previous = worker.controller.head()
                    require(previous is not None, "intervention requires an existing worker generation")
                    plan = InterventionPlan(decision=decision_ref, worker_id=worker_id, previous=previous,
                        actions=actions, resume_policy=self.spec.resume_policy, fresh_orientation=self.spec.fresh_orientation,
                        context=context)
                    _write(path, plan)
                require((plan.decision, plan.worker_id, plan.actions, plan.resume_policy, plan.fresh_orientation, plan.context)
                        == (decision_ref, worker_id, actions, self.spec.resume_policy, self.spec.fresh_orientation, context),
                        "saved intervention plan differs")
                worker.controller._launch(plan.previous)
                return self.services.values.publish(PLAN, plan)

            plan_ref = self.services._record("event.plan", {"key": key, "context": context}, make_plan, decode=DataRef.from_dict)
            plan = self.services.values.get(plan_ref)
            worker.control(plan.previous, "pause")
            deadline = time.monotonic() + self.spec.pause_timeout_seconds
            while worker.poll(plan.previous).completion is None:
                if time.monotonic() >= deadline:
                    raise RecoveryRequired("event intervention awaits a confirmed terminal worker receipt")
                time.sleep(0.025)
            prompt = intervention_prompt(plan)
            fresh = plan.resume_policy == "fresh"
            resumed = worker.start("event:" + plan_ref.content_hash, prompt, previous=plan.previous,
                fresh_orientation=plan.fresh_orientation,
                fallback_reason="event policy explicitly requests a fresh generation" if fresh else None)

            def dispatched():
                value = InterventionDispatch(plan=plan_ref, worker=resumed)
                _write(self._path("dispatches", plan_ref), value)
                return self.services.values.publish(DISPATCH, value)

            outputs.append(self.services._record("event.dispatch", {"plan": plan_ref, "worker": resumed},
                                                dispatched, decode=DataRef.from_dict))
        return tuple(outputs)


def validate_restored(services):
    values = list(services.values._values.values())
    decisions = [(ref, value) for ref, value in values if ref.kind == DECISION]
    if not decisions:
        require(not any(ref.kind in (PLAN, DISPATCH) for ref, _ in values), "intervention lacks an event decision")
        return
    policies = {key: EventService(services, key) for key in {value.policy_id for _, value in decisions}}
    for service in policies.values():
        previous, state, sources = None, service.policy.initial_state(), set()
        for sequence, (ref, value) in enumerate(service._decisions(), 1):
            require(value.identity_hash == service.identity_hash and value.plugin == service.definition.ref
                    and value.state_version == service.definition.state_version and value.sequence == sequence
                    and value.previous == previous and value.state_before_hash == content_hash(state),
                    "restored event chain or policy state differs")
            observation = value.observation
            require(observation.source_key not in sources and observation.worker_id in service.gateway.spec.members,
                    "restored event repeats a source or observes another worker")
            sources.add(observation.source_key)
            require(EventObservation.from_dict(_load(service._path("observations", observation.source_key))) == observation,
                    "restored event clock/input differs from its receipt")
            if observation.result is not None:
                require(service._score(observation.result) == (observation.worker_id, observation.score,
                        observation.plateau_sample), "restored event score differs from released evidence")
            transition = service._transition(observation, state)
            require(content_hash(transition.state) == content_hash(value.payload) and transition.actions == value.actions,
                    "restored event transition differs from its declared pure policy")
            previous, state = ref, transition.state
    for ref, value in values:
        if ref.kind == PLAN:
            decision = services.values.get(value.decision)
            service = policies[decision.policy_id]
            require(InterventionPlan.from_dict(_load(service._path("plans", [value.decision, value.worker_id]))) == value
                    and value.actions == tuple(item for item in decision.actions if item.worker_id == value.worker_id)
                    and value.resume_policy == service.spec.resume_policy
                    and value.fresh_orientation == service.spec.fresh_orientation, "restored intervention plan differs")
            services.worker(value.worker_id).controller._launch(value.previous)
        elif ref.kind == DISPATCH:
            plan = services.values.get(value.plan)
            decision = services.values.get(plan.decision)
            service = policies[decision.policy_id]
            require(InterventionDispatch.from_dict(_load(service._path("dispatches", value.plan))) == value,
                    "restored event dispatch differs from its receipt")
            launch, _ = services.worker(plan.worker_id).controller._launch(value.worker)
            prompt = intervention_prompt(plan)
            if plan.fresh_orientation is not None:
                prompt = plan.fresh_orientation + "\n\n" + prompt
            require(launch.previous == plan.previous and launch.operation_id == "event:" + value.plan.content_hash
                    and launch.prompt == prompt and launch.continuity == (
                        "native" if plan.resume_policy == "native" else "fresh_fallback"),
                    "event dispatch resumed a different generation or prompt")


def require_terminal(services):
    plans = {ref for ref, _ in services.values._values.values() if ref.kind == PLAN}
    dispatched = {value.plan for ref, value in services.values._values.values() if ref.kind == DISPATCH}
    if plans - dispatched:
        raise RecoveryRequired("method cannot complete with an unfinished worker intervention")
