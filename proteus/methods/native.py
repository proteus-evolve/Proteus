"""Compatibility recipe that delegates to the existing native episode transaction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Literal

from proteus.core.budget import PHASES, make_budget_plan, validate_phases
from proteus.core.contracts import OperationStatus, require
from proteus.core.methods import MethodResult, supported_budget
from proteus.core.serialization import Contract, content_hash


@dataclass(frozen=True, kw_only=True)
class NativeOptions(Contract):
    actor: str = "agent"
    episodes: int = field(default=2, metadata={"minimum": 1})
    max_turns: int = field(default=100, metadata={"minimum": 0})
    min_turns_per_phase: int = field(default=0, metadata={"minimum": 0})
    phase_turns: Mapping[str, int] = field(default_factory=dict)
    hard_max_turns: int = field(default=0, metadata={"minimum": 0})
    checkpoint_turns: int = field(default=0, metadata={"minimum": 0})
    announce_budget: bool = False
    phases: tuple[str, ...] = PHASES
    phase_prompts: Mapping[str, str] = field(default_factory=dict)
    goal: str = "none"
    arm: str = "neutral"
    evaluators: tuple[str, ...] = ()
    selection: Literal["none", "accept_reject", "rank"] = "none"
    eval_every: tuple[str, ...] = ()
    eval_at: tuple[str, ...] = ()
    eval_initial: tuple[str, ...] = ()
    eval_missing_on_error: tuple[str, ...] = ()
    selection_exclude: tuple[str, ...] = ()

    def _validate(self):
        validate_phases(self.phases)
        make_budget_plan(max_turns=self.max_turns, min_turns_per_phase=self.min_turns_per_phase,
            phase_turns=self.phase_turns, hard_max_turns=self.hard_max_turns,
            checkpoint_turns=self.checkpoint_turns, phases=self.phases)
        require(not self.checkpoint_turns or self.announce_budget, "checkpoint turns require announce_budget")


@dataclass(frozen=True, kw_only=True)
class NativeSummary(Contract):
    name: str
    episodes_complete: int
    root: str
    error: str
    eval_history: tuple[Mapping[str, Any], ...]
    counters: Mapping[str, Any]
    model_selection: Mapping[str, Any] = field(default_factory=dict)


def validate_native(recipe, options):
    require(set(recipe.actors) == {options.actor}, "native recipe requires exactly its bound actor")
    require(not recipe.stages and recipe.subject is None and recipe.tasks is None
            and recipe.execution is None and not recipe.evaluations and not recipe.search,
            "native recipe uses its legacy episode/workspace/evaluator protocol")
    supported_budget(recipe.budget)
    actor = recipe.actors[options.actor]
    supported_budget(actor.budget)
    from proteus.core.native_budget import validate
    validate(recipe, options)
    require(actor.runtime.plugin.name.startswith("proteus.native."), "native recipe needs a native harness binding")
    require(actor.prompt_hash is None, "native recipe prompts are declared in native phase_prompts")
    from proteus.execution.actors import harness_class
    name = actor.runtime.plugin.name.removeprefix("proteus.native.")
    cls = harness_class(name)
    mode = {"none": "fresh", "framework": "handoff", "native": "native"}[
        getattr(cls, "continuity_mode", "native")]
    require(actor.session.value == mode, "native actor session must match the adapter's continuity policy")
    require(not options.checkpoint_turns or mode != "fresh", "checkpoint turns need adapter continuity")
    if options.phases != PHASES or options.phase_prompts:
        require(getattr(cls, "supports_custom_phases", False), "adapter does not support custom phases")


class NativeMethod:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        from proteus.cli import _arm, _evaluator, _evaluator_controls, _goal
        from proteus.core.episode import RunConfig, completed_episodes, run
        options = self.options
        actor = services.recipe.actors[options.actor]
        runtime = services.registry.create(actor.runtime, "actor")
        def factory():
            return runtime.adapter(actor)
        parsed = [_evaluator(spec, factory) for spec in options.evaluators]
        controls = SimpleNamespace(**{key: getattr(options, key) for key in (
            "eval_every", "eval_at", "eval_initial", "eval_missing_on_error", "selection_exclude")})
        evaluators = _evaluator_controls(tuple(spec for spec, _ in parsed), controls)
        tasks = [task for _, task in parsed if task is not None]
        require(len(tasks) <= 1, "native recipe supports one persistent task workspace")
        cfg = RunConfig(name=services.recipe.name, adapter=factory(), disposition=_arm(options.arm),
            goal=_goal(options.goal, evaluators, selection=options.selection),
            root=services.root / "workspace", model=actor.model or "", seed=services.recipe.seed,
            task=tasks[0] if tasks else None,
            **{key: getattr(options, key) for key in ("episodes", "max_turns", "min_turns_per_phase",
                "phase_turns", "hard_max_turns", "checkpoint_turns", "announce_budget", "phases", "phase_prompts")})
        # This observer records every episode's final core-rendered prompts without
        # rebuilding them or changing the native transaction/feedback boundaries.
        from proteus.core.episode_protocol import DEFAULT_EPISODE_PROTOCOL_VERSION
        def record_prompts(episode, prompts):
            services.native_prompts.append({"episode": episode,
                "protocol_version": DEFAULT_EPISODE_PROTOCOL_VERSION,
                "prompts": dict(prompts), "hash": content_hash(prompts)})

        cfg = replace(cfg, on_episode_prompts=record_prompts)
        start = completed_episodes(cfg) if services.resume else 0
        from proteus.core.native_inputs import read_native_inputs
        from proteus.core.native_budget import budget_scope
        if services.resume and any(row['outcome'] is None for row in read_native_inputs(cfg.root)):
            from proteus.core.recovery import RecoveryRequired
            raise RecoveryRequired('native dispatch outcome is unknown; reconcile before resuming')
        try:
            with budget_scope(services, cfg.adapter, evaluators):
                result = run(cfg, start=start, resume=services.resume)
        finally:
            services.native_inputs = read_native_inputs(cfg.root)
        selection = (runtime.model_selection(actor) if hasattr(runtime, "model_selection")
                     else {"owner": "runtime", "requested_model": actor.model, "resolved_model": None})
        summary = NativeSummary.from_dict({**asdict(result), "model_selection": selection})
        ref = services.values.publish("proteus.native_summary@1", summary)
        return MethodResult(status=OperationStatus.FAILED if result.error or result.episodes_complete < options.episodes
                            else OperationStatus.COMPLETED, outputs={"native": ref}, diagnostic=result.error)
