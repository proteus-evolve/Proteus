"""Explicit plugin registration, option schemas, typed ports and implementation locks.

Importing a recipe never imports its plugins. Hosts explicitly register trusted Python
code (or call a requested module's registration function); plugins are not a sandbox.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import platform
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

from proteus.core.contracts import PluginRef, require
from proteus.core.serialization import Contract, content_hash
from proteus.recipes.config import PluginSpec, RecipeConfig
from proteus.recipes.manifest import DependencyLock, PluginLock, SourcePin


@dataclass(frozen=True, kw_only=True)
class EmptyOptions(Contract):
    pass


def implementation_hash(files: Mapping[str, Path]) -> str:
    """Hash named source files, including helper modules supplied by the plugin author."""
    require(bool(files), "plugin needs implementation files")
    return content_hash({name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                         for name, path in files.items()})


@dataclass(frozen=True)
class Plugin:
    ref: PluginRef
    kind: str
    options: type[Contract]
    factory: Callable
    implementation_hash: str
    source: str
    capabilities: tuple[str, ...] = ()
    inputs: Mapping[str, str] = field(default_factory=dict)
    outputs: Mapping[str, str] = field(default_factory=dict)
    validate: Callable | None = None
    state_schema: type[Contract] | None = None
    state_version: int = 1

    def __post_init__(self):
        require(self.kind in {"method", "stage", "actor", "subject", "task_suite", "environment",
                              "codec", "evaluator", "policy"}, "unknown plugin kind")
        require(isinstance(self.options, type) and issubclass(self.options, Contract),
                "plugin options must be a Contract class")
        require(callable(self.factory), "plugin factory must be callable")
        require(type(self.state_version) is int and self.state_version > 0, "invalid plugin state version")
        if self.state_schema is not None:
            require(self.kind == "method" and isinstance(self.state_schema, type)
                    and issubclass(self.state_schema, Contract), "only methods declare a state schema")
        validated = PluginLock(plugin=self.ref, implementation_hash=self.implementation_hash, source=self.source,
                               capabilities=self.capabilities)
        object.__setattr__(self, "capabilities", validated.capabilities)
        require(self.kind == "stage" or not (self.inputs or self.outputs),
                "only stages declare data ports")
        for ports in (self.inputs, self.outputs):
            require(all(type(name) is str and name.strip() and type(kind) is str and kind.strip()
                        for name, kind in ports.items()), "stage ports and data kinds must be named")
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))


class Registry:
    def __init__(self):
        self._plugins = {}
        self._data = {}
        self._frozen = False

    def snapshot(self):
        registry = Registry()
        registry._plugins, registry._data = dict(self._plugins), dict(self._data)
        registry._frozen = True
        return registry

    def register_data(self, kind: str, schema: type[Contract]) -> None:
        require(not self._frozen, "run registry is frozen")
        require(isinstance(kind, str) and bool(kind.strip()), "data kind must be named")
        require(kind not in self._data, f"data kind already registered: {kind}")
        require(isinstance(schema, type) and issubclass(schema, Contract), "data schema must be a Contract")
        self._data[kind] = schema

    def data_schema(self, kind: str) -> type[Contract]:
        require(kind in self._data, f"unregistered data kind: {kind}")
        return self._data[kind]

    def register(self, plugin: Plugin) -> None:
        require(not self._frozen, "run registry is frozen")
        key = (plugin.ref.name, plugin.ref.version)
        require(key not in self._plugins, f"plugin already registered: {key}")
        for kind in (*plugin.inputs.values(), *plugin.outputs.values()):
            self.data_schema(kind)
        self._plugins[key] = plugin

    def get(self, ref: PluginRef, kind: str | None = None) -> Plugin:
        key = (ref.name, ref.version)
        require(key in self._plugins, f"unregistered plugin: {ref.name}@{ref.version}")
        plugin = self._plugins[key]
        require(kind is None or plugin.kind == kind, f"{ref.name} is {plugin.kind}, expected {kind}")
        return plugin

    def create(self, spec: PluginSpec, kind: str):
        plugin = self.get(spec.plugin, kind)
        return plugin.factory(plugin.options.from_dict(spec.options))

    def load(self, entry: str) -> None:
        """Explicit trusted ``package.module:register`` hook; never an implicit scan."""
        module, separator, function = entry.partition(":")
        require(bool(separator and module and function), "plugin entry must be module:function")
        register = getattr(importlib.import_module(module), function)
        require(callable(register), "registration hook must be callable")
        register(self)

    def resolve(self, recipe: RecipeConfig) -> RecipeConfig:
        """Validate all declarations before construction/execution; expand option defaults."""
        from proteus.core.methods import supported_budget
        for budget in (recipe.budget, *(actor.budget for actor in recipe.actors.values()),
                       *(stage.budget for stage in recipe.stages)):
            supported_budget(budget)
        def resolve(spec, kind):
            plugin = self.get(spec.plugin, kind)
            return replace(spec, options=plugin.options.from_dict(spec.options).to_dict())

        stages = []
        for stage in recipe.stages:
            plugin = self.get(stage.operator.plugin, "stage")
            actor_plugin = self.get(recipe.actors[stage.role].runtime.plugin, "actor")
            require("actor.execute" in actor_plugin.capabilities,
                    "stage actors must support generic execution, not only legacy episodes")
            if any(budget.max_model_calls is not None for budget in
                   (recipe.budget, recipe.actors[stage.role].budget, stage.budget)):
                require("calls.bounded" in actor_plugin.capabilities,
                        "finite model-call caps require a runtime with a defensible call bound")
            require(not stage.input_types or dict(stage.input_types) == dict(plugin.inputs),
                    f"{stage.id}: declared inputs differ from the registered stage")
            require(not stage.output_types or dict(stage.output_types) == dict(plugin.outputs),
                    f"{stage.id}: declared outputs differ from the registered stage")
            require(set(stage.tools) <= set(recipe.actors[stage.role].tools),
                    f"{stage.id}: stage tools exceed its actor's tools")
            require(set(plugin.inputs.values()) <= set(stage.read_scopes),
                    f"{stage.id}: input kinds must be declared in read_scopes")
            require(set(plugin.outputs.values()) <= set(stage.write_scopes),
                    f"{stage.id}: output kinds must be declared in write_scopes")
            stages.append(replace(stage, operator=resolve(stage.operator, "stage"),
                                  input_types=plugin.inputs, output_types=plugin.outputs))
        subject = recipe.subject
        if subject is not None:
            self.get(subject.initial_artifact.codec, "codec")
            if subject.initial_artifact.runtime is not None:
                self.get(subject.initial_artifact.runtime)
            subject = replace(subject, runner=resolve(subject.runner, "subject"))
        tasks = recipe.tasks
        if tasks is not None:
            tasks = replace(tasks, suite=resolve(tasks.suite, "task_suite"))
        execution = recipe.execution
        if execution is not None:
            execution = replace(execution, environment=replace(execution.environment,
                backend=resolve(execution.environment.backend, "environment")))
        resolved = replace(recipe, method=resolve(recipe.method, "method"), stages=tuple(stages),
            actors={name: replace(actor, runtime=resolve(actor.runtime, "actor"))
                    for name, actor in recipe.actors.items()}, subject=subject, tasks=tasks,
            execution=execution, search={name: resolve(spec, "policy") for name, spec in recipe.search.items()},
            evaluations=tuple(replace(spec, evaluator=resolve(spec.evaluator, "evaluator"),
                tasks=replace(spec.tasks, suite=resolve(spec.tasks.suite, "task_suite")))
                for spec in recipe.evaluations))
        capabilities = {key: plugin.capabilities for key, plugin in self._plugins.items()}
        resolved.check_capabilities(capabilities)
        for actor in resolved.actors.values():
            plugin = self.get(actor.runtime.plugin, "actor")
            require(f"session.{actor.session.value}" in plugin.capabilities,
                    f"{plugin.ref.name} does not support {actor.session.value} sessions")
            if plugin.validate is not None:
                plugin.validate(actor, plugin.options.from_dict(actor.runtime.options))
        method = self.get(resolved.method.plugin, "method")
        if method.validate is not None:
            method.validate(resolved, method.options.from_dict(resolved.method.options))
        return resolved

    def make_lock(self, recipe: RecipeConfig, sources: Sequence[SourcePin], *, packages=()) -> DependencyLock:
        recipe = self.resolve(recipe)
        refs = {(ref.name, ref.version): ref for ref in recipe.plugin_refs()}
        locks = []
        for key, ref in sorted(refs.items()):
            plugin = self.get(ref)
            # Include option/data schemas in identity, not just the implementation files.
            digest = content_hash({"implementation": plugin.implementation_hash,
                "options": plugin.options.json_schema(), "kind": plugin.kind,
                "inputs": dict(plugin.inputs), "outputs": dict(plugin.outputs),
                "state_schema": plugin.state_schema.json_schema() if plugin.state_schema else None,
                "state_version": plugin.state_version,
                "data_schemas": {kind: schema.json_schema() for kind, schema in sorted(self._data.items())}})
            locks.append(PluginLock(plugin=ref, implementation_hash=digest, source=plugin.source,
                                    capabilities=plugin.capabilities))
        return DependencyLock(python_version=platform.python_version(), platform=sys.platform,
                              sources=tuple(sources), plugins=tuple(locks), packages=tuple(packages))

    def check_lock(self, recipe: RecipeConfig, lock: DependencyLock) -> None:
        actual = self.make_lock(recipe, lock.sources, packages=lock.packages)
        require(actual == lock, "registered plugins, capabilities or host runtime differ from lock")
        for package in lock.packages:
            try:
                installed = importlib.metadata.version(package.name)
            except importlib.metadata.PackageNotFoundError:
                installed = None
            require(installed == package.version, f"installed dependency differs from lock: {package.name}")

    def describe(self) -> list[dict]:
        return [{"plugin": plugin.ref.to_dict(), "kind": plugin.kind,
                 "capabilities": list(plugin.capabilities), "options": plugin.options.json_schema(),
                 "inputs": dict(plugin.inputs), "outputs": dict(plugin.outputs)}
                for _, plugin in sorted(self._plugins.items())]
