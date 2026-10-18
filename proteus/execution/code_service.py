"""Journaled candidate operations (e.g. memory learning), separate from evaluation."""

import time
from dataclasses import replace

from proteus.core.contracts import (
    BudgetLimits, OperationStatus, Outcome, StageRequest, StageResult, ValidationStatus, require,
)
from proteus.core.ledger import Amounts
from proteus.core.methods import supported_budget
from proteus.core.recovery import RecoveryRequired
from proteus.core.serialization import content_hash
from proteus.evaluation.records import sum_usage, zero_usage
from proteus.evaluation.runners import InvalidCandidate
from proteus.execution.code_records import CodeCall, CodeOptions, CodeResult, CodeState
from proteus.recipes.config import StageSpec


class CodeService:
    def __init__(self, services):
        self.services = services

    def call(self, candidate, call: CodeCall, *, operation_id=None):
        services = self.services
        subject = services.recipe.subject
        require(subject is not None and 'subject.code_protocol' in services.registry.get(subject.runner.plugin).capabilities,
                'candidate invocation requires the managed code subject protocol')
        require(type(call) is CodeCall and call.state.artifact_hash == candidate.artifact.content_hash,
                'code call state belongs to another candidate version')
        require(candidate in services.candidates and candidate.run_id == services.run_id
                and services.artifacts.candidate(services.run_id, candidate.id) == candidate,
                'code invocation requires a registered candidate')
        runtime = services.recipe.actors[subject.actor].runtime.plugin
        require(candidate.artifact.runtime in (None, runtime)
                and candidate.artifact.environment_hash in (None, content_hash(services.recipe.execution.environment)),
                'candidate is bound to another runtime or environment')
        options = CodeOptions.from_dict(subject.runner.options)
        require(call.method in options.methods, 'candidate operation is not declared')
        operation_id = operation_id or f'op-{len(services._operations) + 1:06d}'
        return services._record('code.call', {'candidate': candidate, 'call': call, 'operation_id': operation_id},
            lambda: self._execute(candidate, call, options, operation_id), decode=CodeResult.from_dict)

    def _execute(self, candidate, call, options, operation_id):
        services, recipe = self.services, self.services.recipe
        services._check_operation(operation_id)
        supported_budget(recipe.budget, allow_evaluations=True)
        services._operations.add(operation_id)
        inputs = {'candidate': services.values.publish('proteus.candidate@1', candidate),
                  'call': services.values.publish('proteus.code.call@1', call)}
        stage = StageSpec(id='candidate_code', operator=recipe.subject.runner, role=recipe.subject.actor)
        request = StageRequest(run_id=services.run_id, operation_id=operation_id, attempt_id='attempt-1',
            stage=recipe.subject.runner.plugin, role=stage.role, inputs=inputs,
            read_scopes=('proteus.candidate@1', 'proteus.code.call@1'), write_scopes=('proteus.code.result@1',),
            budget=BudgetLimits())
        services.requests.append(request)
        entry_id = 'code-' + content_hash(operation_id).split(':')[1]
        entry = services.ledger.reserve(entry_id, scopes={'run': recipe.budget}, reservation=Amounts.zero(), slots=0,
            metadata={'kind': 'code_invocation', 'operation_id': operation_id, 'attempt_id': request.attempt_id,
                      'stage': stage.id, 'role': stage.role, 'candidate_id': candidate.id},
            request={'candidate': candidate, 'call': call, 'execution': recipe.execution, 'options': options},
            reuse=services._journal is not None and services._journal.recovering)
        if entry['state'] != 'reserved':
            settlement = entry['settlement']
            if settlement is None or not (settlement['result'] or {}).get('code_result'):
                raise RecoveryRequired(f'code invocation {operation_id} has no settled result; reconcile before continuing')
            result = CodeResult.from_dict(settlement['result']['code_result'])
            require(result.candidate_id == candidate.id and result.operation_id == operation_id
                    and result.call_hash == content_hash(call) and result.state.artifact_hash == candidate.artifact.content_hash,
                    'settled code result differs from its request')
            services.restore_trial_usage(operation_id)
        else:
            services.ledger.start(entry_id)
            started = time.monotonic()
            first_actor = len(services.actor_calls)
            first_build = len(services.builds.records)
            result = CodeResult(candidate_id=candidate.id, operation_id=operation_id, call_hash=content_hash(call),
                state=call.state, status=Outcome.INVALID_CANDIDATE, usage=zero_usage(), diagnostic='candidate is marked invalid')
            try:
                if candidate.validation != ValidationStatus.INVALID:
                    environment = services.registry.create(recipe.execution.environment.backend, 'environment')
                    def build(artifact, spec, base_image):
                        return services.builds.execute(artifact, spec, base_image, operation_id=operation_id,
                            attempt_id=request.attempt_id, trial_id=operation_id)
                    env_options = {'build': build} if 'environment.source_build' in services.registry.get(
                        recipe.execution.environment.backend.plugin).capabilities else {}
                    with environment.open(candidate.artifact, services.artifacts,
                            services.root / 'code' / content_hash(operation_id).split(':')[1], recipe.execution,
                            **env_options) as lease:
                        require(lease.loaded_artifact_hash == candidate.artifact.content_hash and callable(lease.code_executor),
                                'code environment loaded another candidate or lacks its executor')
                        def actor(prompt, *, timeout_seconds):
                            return services._actor(stage, request, prompt, None, None, isolate=True,
                                                   trial_id=operation_id, timeout_seconds=timeout_seconds)
                        reply = lease.code_executor(options, call, actor)
                    result = replace(result, status=reply.status, output=reply.output, trace=reply.trace,
                        state=CodeState(artifact_hash=candidate.artifact.content_hash, initialized=True, value=reply.state)
                            if reply.status == Outcome.SUCCEEDED else call.state, diagnostic=reply.diagnostic)
            except RecoveryRequired:
                raise  # Keep the started reservation; an uncertain external call must not be repeated.
            except InvalidCandidate as exc:
                result = replace(result, status=Outcome.INVALID_CANDIDATE, diagnostic=str(exc))
            except Exception as exc:
                result = replace(result, status=Outcome.INFRA_ERROR, diagnostic=f'{type(exc).__name__}: {exc}')
            # BaseException/hard exit deliberately leaves a started operation requiring reconciliation.
            build_usages = [record.usage for record in services.builds.records[first_build:]]
            usage = sum_usage((services._usage(first_actor), *build_usages))
            elapsed = time.monotonic() - started
            result = replace(result, usage=replace(usage, wall_seconds=elapsed))
            actor_seconds = sum(record.get('wall_seconds', 0) for record in services.actor_calls[first_actor:])
            build_seconds = sum(usage.wall_seconds or 0 for usage in build_usages)
            services.ledger.settle(entry_id, actual=Amounts(model_calls=0, cost_usd=0,
                wall_seconds=max(0, elapsed - actor_seconds - build_seconds)), status='completed' if result.status == Outcome.SUCCEEDED else 'failed',
                result={'code_result': result, 'delegates_model_usage_to_actor': True})
        ref = services.values.publish('proteus.code.result@1', result)
        services.results.append(StageResult(run_id=services.run_id, operation_id=operation_id, attempt_id=request.attempt_id,
            status=OperationStatus.COMPLETED if result.status == Outcome.SUCCEEDED else OperationStatus.FAILED,
            outputs={'result': ref}, usage=result.usage, diagnostic=result.diagnostic))
        return result
