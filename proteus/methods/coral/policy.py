"""Execute the pinned heartbeat implementation with an explicit lifecycle profile."""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from proteus.core.contracts import PluginRef, require
from proteus.core.plugins import Plugin, implementation_hash
from proteus.events.policies import CadenceOptions, CadenceState, progress
from proteus.events.records import EventAction, Transition
from proteus.methods.coral._reference_heartbeat import HeartbeatAction, HeartbeatRunner


@dataclass(frozen=True, kw_only=True)
class CoralOptions(CadenceOptions):
    profile: Literal["reference_manager", "persistent_cooldown"] = "reference_manager"

    def _validate(self):
        super()._validate()
        require(all(rule.trigger != "time" for rule in self.rules), "reference CORAL heartbeat has no time trigger")


class CoralPolicy:
    def __init__(self, options):
        self.options = options

    def initial_state(self):
        return CadenceState()

    def advance(self, observation, state):
        state = progress(state, observation)
        if observation.kind != "evaluation":
            return Transition(state)
        worker = state.workers[observation.worker_id]
        runner = HeartbeatRunner([HeartbeatAction(name=rule.name, every=int(rule.every), prompt=rule.prompt,
            is_global=rule.scope == "global", trigger="plateau" if rule.trigger == "plateau" else "interval",
            options={"epsilon": rule.epsilon} if rule.trigger == "plateau" else {}) for rule in self.options.rules])
        # The pinned manager creates a fresh runner on every check. Keeping this
        # class-level cooldown across checks is a separate, explicitly named profile.
        from proteus.core.serialization import content_hash
        keys = {rule.name: content_hash([observation.worker_id, rule.name]) for rule in self.options.rules}
        if self.options.profile == "persistent_cooldown":
            runner._plateau_fired_at = {name: int(state.fired[key]) for name, key in keys.items() if key in state.fired}
        actions = runner.check(local_eval_count=worker.evaluations, global_eval_count=state.evaluations,
                               score_history=worker.scores, minimize=self.options.minimize)
        fired = {key: value for key, value in state.fired.items() if key not in keys.values()}
        if self.options.profile == "persistent_cooldown":
            fired.update({keys[name]: value for name, value in runner._plateau_fired_at.items()})
        return Transition(replace(state, fired=fired), tuple(EventAction(name=action.name,
            worker_id=observation.worker_id, prompt=action.prompt) for action in actions))


def register(registry):
    root = Path(__file__).parent
    files = {path.name: path for path in root.iterdir() if path.suffix in (".py", ".md", ".txt")}
    registry.register(Plugin(ref=PluginRef(name="coral.heartbeat", version="1"), kind="event_policy",
        options=CoralOptions, factory=CoralPolicy, implementation_hash=implementation_hash(files), source="proteus",
        capabilities=("event.pure_transition", "coral.reference_heartbeat"), state_schema=CadenceState))
