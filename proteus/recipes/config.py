"""Strict resolved configuration, with method-specific options in plugin namespaces."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from proteus.core.contracts import (
    DIGEST, TEXT, UNIQUE, ArtifactRef, BudgetLimits, FeedbackVisibility,
    PluginRef, SessionPolicy, StatePolicy, require, unique,
)
from proteus.core.serialization import Contract, ContractError, content_hash, json_value, read_json


@dataclass(frozen=True, kw_only=True)
class PluginSpec(Contract):
    plugin: PluginRef
    options: Mapping[str, Any] = field(default_factory=dict)
    requires: tuple[str, ...] = field(default=(), metadata=UNIQUE)

    def _validate(self):
        require(all(item.strip() for item in self.requires), "capabilities must be nonempty")


@dataclass(frozen=True, kw_only=True)
class ActorSpec(Contract):
    runtime: PluginSpec
    model: str | None = field(default=None, metadata=TEXT)
    model_settings: Mapping[str, Any] = field(default_factory=dict)
    prompt_hash: str | None = field(default=None, metadata=DIGEST)
    tools: tuple[str, ...] = field(default=(), metadata=UNIQUE)
    session: SessionPolicy = SessionPolicy.FRESH
    budget: BudgetLimits = field(default_factory=BudgetLimits)

    def _validate(self):
        require(self.model is None or "${" not in self.model,
                "resolve model variables before loading the recipe")


@dataclass(frozen=True, kw_only=True)
class StageSpec(Contract):
    id: str = field(metadata=TEXT)
    operator: PluginSpec
    role: str = field(metadata=TEXT)
    input_types: Mapping[str, str] = field(default_factory=dict)
    output_types: Mapping[str, str] = field(default_factory=dict)
    tools: tuple[str, ...] = field(default=(), metadata=UNIQUE)
    read_scopes: tuple[str, ...] = field(default=(), metadata=UNIQUE)
    write_scopes: tuple[str, ...] = field(default=(), metadata=UNIQUE)
    failure_policy: Literal["stop", "record", "retry"] = "stop"
    max_attempts: int = field(default=1, metadata={"minimum": 1})
    budget: BudgetLimits = field(default_factory=BudgetLimits)

    def _validate(self):
        require(self.failure_policy == "retry" or self.max_attempts == 1,
                "multiple attempts require retry failure_policy")


@dataclass(frozen=True, kw_only=True)
class SubjectSpec(Contract):
    runner: PluginSpec
    initial_artifact: ArtifactRef
    actor: str | None = field(default=None, metadata=TEXT)
    mutable_scopes: tuple[str, ...] = field(default=(), metadata=UNIQUE)


@dataclass(frozen=True, kw_only=True)
class TaskSuiteSpec(Contract):
    suite: PluginSpec
    dataset_version: str = field(metadata=TEXT)
    split: str = field(metadata=TEXT)
    task_ids: tuple[str, ...] = field(metadata={**UNIQUE, "minItems": 1})
    order: Literal["declared", "shuffle"] = "declared"
    seed: int = 0

    def _validate(self):
        require(all(item.strip() for item in self.task_ids), "task IDs must be nonempty")


@dataclass(frozen=True, kw_only=True)
class EnvironmentSpec(Contract):
    backend: PluginSpec
    provider: str = field(metadata=TEXT)
    image_digest: str | None = field(default=None, metadata=TEXT)
    build_hash: str | None = field(default=None, metadata=DIGEST)
    resources: Mapping[str, Any] = field(default_factory=dict)
    network: Literal["none", "restricted", "unrestricted"] = "none"
    allowed_hosts: tuple[str, ...] = field(default=(), metadata=UNIQUE)
    read_scopes: tuple[str, ...] = field(default=(), metadata=UNIQUE)
    write_scopes: tuple[str, ...] = field(default=(), metadata=UNIQUE)
    lifecycle: Literal["trial", "sequence", "worker"] = "trial"

    def _validate(self):
        require(self.network == "restricted" or not self.allowed_hosts,
                "allowed_hosts requires restricted network")
        if self.image_digest is not None:
            import re
            require(re.fullmatch(r"(?:[^\s@]+@)?sha256:[0-9a-f]{64}", self.image_digest) is not None,
                    "image_digest must identify immutable image content, not a tag")


@dataclass(frozen=True, kw_only=True)
class ExecutionSpec(Contract):
    environment: EnvironmentSpec
    state_policy: StatePolicy = StatePolicy.INDEPENDENT
    initial_state_hash: str | None = field(default=None, metadata=DIGEST)
    seed: int = 0
    timeout_seconds: float | None = field(default=None, metadata={"exclusiveMinimum": 0})

    def _validate(self):
        expected = {StatePolicy.INDEPENDENT: "trial", StatePolicy.SEQUENCE: "sequence",
                    StatePolicy.WORKER: "worker"}[self.state_policy]
        require(self.environment.lifecycle == expected,
                "environment lifecycle must match the declared state policy")


@dataclass(frozen=True, kw_only=True)
class EvaluationSpec(Contract):
    id: str = field(metadata=TEXT)
    evaluator: PluginSpec
    tasks: TaskSuiteSpec
    purpose: Literal["optimization", "validation", "test"] = "optimization"
    visibility: FeedbackVisibility = FeedbackVisibility.HIDDEN
    feedback_timing: Literal["never", "after_prediction", "after_batch"] = "never"
    selection_eligible: bool = True

    def _validate(self):
        require((self.visibility == FeedbackVisibility.HIDDEN) == (self.feedback_timing == "never"),
                "hidden feedback uses never; observed feedback needs an explicit timing")
        if self.purpose == "test":
            require(not self.selection_eligible and self.visibility == FeedbackVisibility.HIDDEN,
                    "test results cannot be used for optimization feedback or candidate selection")


@dataclass(frozen=True, kw_only=True)
class ReferenceSpec(Contract):
    profile: Literal["native", "reference", "adapted", "extended"]
    status: Literal["experimental", "mechanism_validated", "benchmark_validated"] = "experimental"
    paper: str | None = field(default=None, metadata=TEXT)
    code: str | None = field(default=None, metadata=TEXT)
    revision: str | None = field(default=None, metadata={"pattern": r"^[0-9a-f]{40}([0-9a-f]{24})?$"})
    deviations: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    def _validate(self):
        require((self.code is None) == (self.revision is None),
                "reference code and immutable revision must be provided together")
        require(self.profile == "native" or self.paper is not None or self.code is not None,
                "external method profiles need a paper or code reference")
        require(self.status == "experimental" or bool(self.evidence),
                "validated recipe status requires evidence references")


@dataclass(frozen=True, kw_only=True)
class RecipeConfig(Contract):
    name: str = field(metadata=TEXT)
    method: PluginSpec
    actors: Mapping[str, ActorSpec]
    reference: ReferenceSpec
    subject: SubjectSpec | None = None
    tasks: TaskSuiteSpec | None = None
    execution: ExecutionSpec | None = None
    stages: tuple[StageSpec, ...] = ()
    evaluations: tuple[EvaluationSpec, ...] = ()
    search: Mapping[str, PluginSpec] = field(default_factory=dict)
    budget: BudgetLimits = field(default_factory=BudgetLimits)
    seed: int = 0

    def _validate(self):
        require(all(role.strip() for role in self.actors), "actor roles must be nonempty")
        require(self.subject is None or self.subject.actor is None or self.subject.actor in self.actors,
                "subject references an unknown actor role")
        require(all(stage.role in self.actors for stage in self.stages),
                "stage references an unknown actor role")
        unique((stage.id for stage in self.stages), "stage ID")
        unique((spec.id for spec in self.evaluations), "evaluation ID")

    @property
    def config_hash(self) -> str:
        return content_hash(self)

    def plugin_specs(self) -> tuple[PluginSpec, ...]:
        optional = []
        if self.subject is not None:
            optional.append(self.subject.runner)
        if self.tasks is not None:
            optional.append(self.tasks.suite)
        if self.execution is not None:
            optional.append(self.execution.environment.backend)
        return (self.method, *optional, *(actor.runtime for actor in self.actors.values()),
                *(stage.operator for stage in self.stages), *self.search.values(),
                *(spec.evaluator for spec in self.evaluations),
                *(spec.tasks.suite for spec in self.evaluations))

    def plugin_refs(self) -> tuple[PluginRef, ...]:
        refs = [spec.plugin for spec in self.plugin_specs()]
        if self.subject is not None:
            refs.append(self.subject.initial_artifact.codec)
            if self.subject.initial_artifact.runtime is not None:
                refs.append(self.subject.initial_artifact.runtime)
        return tuple(refs)

    def check_capabilities(self, capabilities: Mapping[tuple[str, str], Sequence[str]]) -> None:
        """Check declarations supplied by a plugin registry; do not import plugins here."""
        for spec in self.plugin_specs():
            key = (spec.plugin.name, spec.plugin.version)
            require(key in capabilities, f"plugin capabilities unavailable: {key}")
            missing = set(spec.requires) - set(capabilities[key])
            require(not missing, f"{key} lacks capabilities: {sorted(missing)}")
        for actor in self.actors.values():
            key = (actor.runtime.plugin.name, actor.runtime.plugin.version)
            if actor.session == SessionPolicy.NATIVE:
                require("session.resume" in capabilities[key], f"{key} lacks session.resume")


def load_recipe(path: str | Path, *, overrides: Mapping[str, Any] | None = None) -> RecipeConfig:
    """Read JSON/TOML and recursively overlay objects; arrays replace as a whole.

    There is no implicit environment interpolation or import-time plugin execution.
    Strings such as model identifiers are caller-resolved before the manifest is frozen.
    Plugin options stay opaque until the plugin's own schema is registered in P02.
    """
    path = Path(path)
    if path.suffix == ".json":
        data = read_json(path.read_text(encoding="utf-8"))
    elif path.suffix == ".toml":
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ContractError(f"invalid TOML: {exc}") from exc
    else:
        raise ContractError("recipe file must be .json or .toml")

    def overlay(base, update):
        result = copy.deepcopy(base)
        for key, value in update.items():
            if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
                result[key] = overlay(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    require(isinstance(data, Mapping), "recipe must be an object")
    if overrides is not None:
        require(isinstance(overrides, Mapping), "overrides must be an object")
        data = overlay(data, json_value(overrides))
    return RecipeConfig.from_dict(data)
