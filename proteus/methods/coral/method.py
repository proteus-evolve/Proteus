"""Single shared-space CORAL controller over reusable worker, knowledge and event services."""

from dataclasses import replace
from pathlib import Path
import time

from proteus.core.contracts import DataRef, FeedbackVisibility, PluginRef, require
from proteus.core.methods import MethodResult
from proteus.core.plugins import Plugin, implementation_hash
from proteus.core.recovery import RecoveryRequired
from proteus.core.search import Objective
from proteus.methods.coral.records import CoralMethodOptions, CoralPoll, CoralState, CoralSummary


def validate(recipe, options):
    gateway = next((item for item in recipe.gateways if item.id == options.gateway), None)
    policy = next((item for item in recipe.event_policies if item.id == options.event_policy), None)
    require(gateway is not None and policy is not None and policy.gateway == gateway.id,
            "CORAL needs a matching gateway and event policy")
    require(gateway.knowledge_space is not None and gateway.knowledge_access == "read_write",
            "CORAL workers need shared notes/skills read and write access")
    require(set(gateway.members) == {item.id for item in recipe.workers},
            "CORAL manages exactly the declared gateway workers")
    require(set(options.worker_prompts) <= set(gateway.members), "unknown worker prompt override")
    require(len(gateway.evaluations) == 1, "CORAL scalar search needs one comparable formal evaluation protocol")
    evaluation = next(item for item in recipe.evaluations if item.id == gateway.evaluations[0])
    require(evaluation.selection_eligible, "CORAL formal evaluation must permit final selection")
    require(options.final_selector in recipe.search, "CORAL requires a declared final selector")
    if options.final_evaluation is not None:
        final = next((item for item in recipe.evaluations if item.id == options.final_evaluation), None)
        require(final is not None and final.purpose == "test" and final.visibility == FeedbackVisibility.HIDDEN,
                "CORAL final evaluation must be a separate hidden test")
    require(recipe.subject is not None and not recipe.subject.input_types,
            "CORAL autonomous file submissions currently require an input-free subject protocol")
    if "minimize" in policy.operator.options:
        require(policy.operator.options["minimize"] == options.minimize, "CORAL search and heartbeat directions differ")


def feedback(services, decision_ref):
    if decision_ref is None:
        return "No formal submission by this worker has completed yet."
    decision = services.values.get(decision_ref)
    result = services.values.get(decision.observation.result)
    score = decision.observation.score
    return ("## Formal evaluation result\n"
            f"Worker: {decision.observation.worker_id}\n"
            f"Submission: {result.ticket.id()}\n"
            f"Score: {score if score is not None else 'unavailable'}\n"
            f"Status: {result.status}\n"
            "Read this submission's released gateway feedback and shared attempt for details. "
            "Private test results are not available to the research workers.")


class CoralMethod:
    def __init__(self, options):
        self.options = options

    def _poll(self, services, state):
        def capture():
            observed = {key: services.worker(key).poll(ref) for key, ref in state.workers.items()}
            for value in observed.values():
                if value.completion is not None:
                    services.values.publish("proteus.worker.completion@1", value.completion)
            return services.values.publish("coral.poll@1", CoralPoll(workers=observed))
        ref = services._record("coral.poll", {"poll": state.polls, "workers": state.workers}, capture, decode=DataRef.from_dict)
        return services.values.get(ref)

    def _apply(self, services, state, decision_ref, *, context=""):
        refs = services.events(self.options.event_policy).apply(decision_ref, context=context)
        workers = dict(state.workers)
        for ref in refs:
            worker = services.values.get(ref).worker
            workers[worker.worker_id] = worker
        return replace(state, workers=workers)

    def run(self, services, restored_state):
        options = self.options
        state = CoralState.from_dict(restored_state.state.payload) if restored_state is not None else CoralState()
        gateway, events = services.gateway(options.gateway), services.events(options.event_policy)
        evaluation_id = gateway.spec.evaluations[0]
        with gateway.serve():
            if state.phase == "initial":
                initial = services.initial_candidate()
                services.evaluate(initial, evaluation_id, operation_id="coral:baseline")
                workers = {}
                for name in gateway.spec.members:
                    worker = services.worker(name)
                    worker.seed("coral:seed", initial.artifact)
                    prompt = options.worker_prompts.get(name, options.bootstrap_prompt)
                    workers[name] = worker.start("coral:initial", prompt)
                state = replace(state, phase="search", workers=workers)
                for name in workers:
                    tick = events.tick("coral:initial:" + name, name)
                    state = self._apply(services, state, tick)
                services.checkpoint(state)

            while state.phase == "search":
                ticket = gateway.claim(f"coral:claim:{state.polls}", timeout_seconds=options.poll_seconds)
                if ticket is not None:
                    result = gateway.process(ticket)
                    result_ref = services.values.publish("proteus.gateway.result@1", result)
                    decision_ref = events.observe("coral:result:" + ticket.id(), result_ref)
                    state = replace(state, results=state.results + (result_ref,), decisions=state.decisions + (decision_ref,),
                        latest={**state.latest, ticket.worker_id: decision_ref})
                    # Like the reference manager, auto-stop precedes an intervention
                    # on the final allowed result. Its observation is still recorded.
                    if len(state.results) >= options.max_submissions:
                        state = replace(state, phase="closing", reason="submission_limit")
                    else:
                        state = self._apply(services, state, decision_ref, context=feedback(services, decision_ref))
                state = replace(state, polls=state.polls + 1)
                if state.phase == "search" and state.polls >= options.max_polls:
                    state = replace(state, phase="closing", reason="poll_limit")
                if state.phase == "search":
                    for name in state.workers:
                        tick = events.tick(f"coral:tick:{state.polls}:{name}", name)
                        state = self._apply(services, state, tick, context=feedback(services, state.latest.get(name)))
                    observed = self._poll(services, state)
                    for name, value in observed.workers.items():
                        if value.state == "uncertain":
                            raise RecoveryRequired("CORAL worker has no confirmed completion; its budget stays reserved")
                        if value.completion is None:
                            continue
                        count = state.restarts.get(name, 0)
                        if count >= options.max_restarts_per_worker:
                            state = replace(state, phase="closing", reason="worker_failure")
                            break
                        policy = events.spec
                        fresh = policy.resume_policy == "fresh"
                        resumed = services.worker(name).start(f"coral:restart:{name}:{count}",
                            feedback(services, state.latest.get(name)) + "\n\nContinue independent research and share useful findings.",
                            previous=state.workers[name], fresh_orientation=policy.fresh_orientation,
                            fallback_reason="CORAL recipe explicitly requests a fresh generation" if fresh else None)
                        state = replace(state, workers={**state.workers, name: resumed},
                            restarts={**state.restarts, name: count + 1})
                services.checkpoint(state)

            if state.phase == "closing":
                closure = gateway.close("coral:close", state.reason)
                closure_ref = services.values.publish("proteus.gateway.closure@1", closure)
                cancelled = []
                for ticket in closure.pending:
                    # Controller dispatch is serial. Any claimed request must have
                    # been replayed/reconciled before reaching the closing phase.
                    if gateway.store.was_claimed(ticket):
                        raise RecoveryRequired("CORAL closing found an unreconciled claimed evaluation")
                    result = gateway.cancel(ticket, closure)
                    cancelled.append(services.values.publish("proteus.gateway.result@1", result))
                for name, ref in state.workers.items():
                    services.worker(name).control(ref, "stop")
                deadline = time.monotonic() + options.stop_timeout_seconds
                for name, ref in state.workers.items():
                    worker = services.worker(name)
                    while worker.poll(ref).completion is None:
                        if time.monotonic() >= deadline:
                            raise RecoveryRequired("CORAL shutdown awaits a confirmed worker completion")
                        time.sleep(0.025)
                    worker.result(ref)
                state = replace(state, phase="finalizing", closure=closure_ref, cancelled=tuple(cancelled))
                services.checkpoint(state)

            archive = services.archive_snapshot()
            view = services.selection_view(archive, evaluation_id,
                objectives=(Objective(metric="score", maximize=not options.minimize),))
            decision = services.select_final(options.final_selector, view, operation_id="coral:final-selection")
            selection = services.values.publish("proteus.search.decision@1", decision)
            chosen = decision.result.selected
            final_evaluations = []
            if options.final_evaluation is not None:
                for candidate_id in chosen:
                    candidate = services.artifacts.candidate(services.run_id, candidate_id)
                    receipt = services.evaluate(candidate, options.final_evaluation,
                        operation_id="coral:final-test:" + candidate_id)
                    final_evaluations.append(services.values.publish("proteus.eval.receipt@1", receipt))
            knowledge_service = services.knowledge(gateway.spec.knowledge_space)
            knowledge = knowledge_service.capture(role=knowledge_service.spec.roles[0],
                reader_id="coral:controller", operation_id="coral:final-knowledge")
            completed = {name: services.worker(name).result(ref) for name, ref in state.workers.items()}
            summary = CoralSummary(results=state.results, decisions=state.decisions, closure=state.closure,
                cancelled=state.cancelled, workers=completed, restarts=state.restarts, selection=selection,
                final_evaluations=tuple(final_evaluations), knowledge=knowledge, polls=state.polls, reason=state.reason)
            output = services.values.publish("coral.summary@1", summary)
        return MethodResult(outputs={"summary": output})


def register(registry):
    registry.register_data("coral.poll@1", CoralPoll)
    registry.register_data("coral.summary@1", CoralSummary)
    root = Path(__file__).parent
    registry.register(Plugin(ref=PluginRef(name="coral.single_island", version="1"), kind="method",
        options=CoralMethodOptions, factory=CoralMethod, implementation_hash=implementation_hash({
            path.name: path for path in root.iterdir() if path.suffix in (".py", ".md", ".txt")}), source="proteus",
        capabilities=("run.journal", "run.resume", "coral.single_island"), state_schema=CoralState, validate=validate))
