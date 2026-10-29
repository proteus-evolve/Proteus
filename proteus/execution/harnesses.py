"""Source-bound harness integrations reused by configured execution roles.

Methods do not register harness pairs. An integration registers the roles its
adapter can actually implement, using the ordinary versioned plugin/lock system.
Candidate and evaluator bridges build on these same integration identities.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable

from proteus.core.contracts import PluginRef, require
from proteus.core.plugins import Plugin
from proteus.core.serialization import Contract
from proteus.execution.actors import HarnessActor


@dataclass(frozen=True)
class HarnessIntegration:
    name: str
    factory: Callable
    options: Mapping[str, type[Contract]]
    implementation_hash: str
    source: str
    capabilities: tuple[str, ...] = ()
    continuity: str = 'fresh'
    model_call_bound: int | None = None
    validate: Callable | None = None
    model_selection: Callable | None = None

    def __post_init__(self):
        PluginRef(name=self.name, version='1')
        require(callable(self.factory), 'harness integration needs an adapter factory')
        require(bool(self.options) and set(self.options) <= {'actor', 'workspace', 'native', 'evolution'},
                'unknown or empty harness integration roles')
        require(all(isinstance(schema, type) and issubclass(schema, Contract)
                    for schema in self.options.values()), 'harness role options must be Contract classes')
        require(self.continuity in ('fresh', 'handoff', 'native'), 'invalid harness continuity policy')
        require(self.model_call_bound is None or (type(self.model_call_bound) is int
                and self.model_call_bound >= 0), 'invalid harness model-call bound')
        require(len(set(self.capabilities)) == len(self.capabilities), 'duplicate harness capability')
        require('workspace' not in self.options or 'harness.workspace' in self.capabilities,
                'workspace role needs harness.workspace capability')
        require('evolution' not in self.options or 'harness.candidate_tree' in self.capabilities,
                'evolution role needs harness.candidate_tree capability')
        require('harness.custom_phases' in self.capabilities or not {'actor', 'workspace', 'evolution'} & set(self.options),
                'generic actor roles need harness.custom_phases capability')
        object.__setattr__(self, 'options', MappingProxyType(dict(self.options)))

    def check(self, role, spec, options):
        require(role in self.options, f'{self.name} does not support harness role {role}')
        require(type(options) is self.options[role], 'harness role option schema differs')
        if self.validate is not None:
            self.validate(role, spec, options)
        else:
            require(not spec.tools, 'harness integration does not enforce a restricted tool allowlist')
            require(not spec.model_settings, 'harness integration does not accept generic model_settings')
            require(spec.model is not None or 'harness.model_free' in self.capabilities,
                    'model-backed harness role needs an explicit model')
        # Legacy Native accounts at its own transport boundaries. Its validator
        # checks those bounds separately; a generic-call bound is not equivalent.
        if role != 'native' and spec.budget.max_model_calls is not None:
            bound = self.model_call_bound if role != 'evolution' or self.model_call_bound == 0 else None
            require(bound is not None, 'harness role lacks calls.bounded capability')


class IntegratedHarnessActor(HarnessActor):
    """The same adapter factory supplies ordinary, coding and Native roles."""

    def __init__(self, integration, role, options):
        super().__init__(integration.name, options)
        self.integration, self.role = integration, role
        self.supports_workspace = role in ('workspace', 'evolution')
        self.model_call_bound = (integration.model_call_bound
            if role != 'evolution' or integration.model_call_bound == 0 else None)

    def adapter(self, spec):
        self.integration.check(self.role, spec, self.options)
        adapter = self.integration.factory(self.role, spec, self.options)
        require(adapter.name == self.integration.name, 'adapter identity differs from harness integration')
        if self.role in ('actor', 'workspace', 'evolution'):
            require(getattr(adapter, 'supports_custom_phases', False),
                    'declared harness.custom_phases capability is not implemented')
        return adapter

    def execute(self, spec, request, workspace):
        if self.role == 'evolution':
            from proteus.execution.harness_evolution import execute_episode
            return execute_episode(self, spec, request, workspace)
        return super().execute(spec, request, workspace)

    def model_selection(self, spec):
        if self.integration.model_selection is not None:
            return self.integration.model_selection(self.role, spec, self.options)
        return {'owner': 'framework', 'requested_model': spec.model,
                'applied_model': spec.model, 'resolved_model': None}


def register_harness(registry, integration, *, bindings=None):
    """Register one integration; recipe plugin references choose independent roles.

    Factories and all helpers must be included in implementation_hash/source pins,
    exactly as for other plugins. No adapter instance or mutable workspace is shared.
    Optional bindings preserve existing public plugin names without method changes.
    """
    roles = set(integration.options)
    names = bindings or {role: f'{integration.name}.{role}' for role in roles}
    require(set(names) == roles, 'harness binding names differ from declared roles')
    require(len(set(names.values())) == len(names), 'harness roles need distinct plugin identities')
    registered = {}
    for role in sorted(roles):
        capabilities = [f'harness.integration.{integration.name}', f'harness.role.{role}',
                        *integration.capabilities]
        if role == 'native':
            capabilities.extend((f'session.{integration.continuity}', 'legacy.episode', 'trace.normalized'))
            if 'harness.staged_activation' in integration.capabilities:
                capabilities.append('candidate.staged_activation')
        else:
            capabilities.extend(('session.fresh', 'actor.execute', 'trace.normalized'))
            if role in ('workspace', 'evolution'):
                capabilities.append('actor.workspace')
            if integration.model_call_bound is not None and (role != 'evolution' or integration.model_call_bound == 0):
                capabilities.append('calls.bounded')
        ref = PluginRef(name=names[role], version='1')
        registry.register(Plugin(ref=ref, kind='actor', options=integration.options[role],
            factory=lambda options, role=role: IntegratedHarnessActor(integration, role, options),
            validate=lambda spec, options, role=role: integration.check(role, spec, options),
            implementation_hash=integration.implementation_hash, source=integration.source,
            capabilities=tuple(capabilities)))
        registered[role] = ref
    return MappingProxyType(registered)


def harness_support_matrix(registry):
    """Report registered capabilities, not assumed support for every method pair."""
    result = {}
    for entry in registry.describe():
        capabilities = entry['capabilities']
        names = [value.removeprefix('harness.integration.') for value in capabilities
                 if value.startswith('harness.integration.')]
        roles = [value.removeprefix('harness.role.') for value in capabilities
                 if value.startswith('harness.role.')]
        if not names:
            continue
        require(len(names) == len(roles) == 1, 'invalid registered harness role metadata')
        result.setdefault(names[0], {})[roles[0]] = {
            'plugin': entry['plugin'], 'capabilities': capabilities, 'options': entry['options']}
        if roles[0] == 'actor' and 'harness.candidate_tree' in capabilities:
            result[names[0]]['subject'] = {
                'plugin': {'name': 'proteus.harness_subject', 'version': '1'},
                'runtime_role': entry['plugin'],
                'capabilities': ['candidate.materialized', 'state.independent'],
                'scope': 'Explicit file-tree or pinned-base projection; per-trial adapter state is isolated.'}
    return result
