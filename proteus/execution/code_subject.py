"""Prediction adapter for a managed candidate-code protocol."""

import os
from dataclasses import replace

from proteus.core.contracts import Outcome, RuntimeIdentity, StatePolicy, Usage, require
from proteus.core.serialization import content_hash
from proteus.evaluation.records import SubjectResult, Trace, sum_usage
from proteus.evaluation.tasks import StateValue
from proteus.execution.code_records import CodeCall, CodeState
from proteus.execution.code_rpc import invoke


def validate_code(recipe, options):
    subject = recipe.subject
    require(os.name == 'posix', 'candidate-code host execution requires POSIX')
    require(subject.actor is not None, 'candidate code requires a managed model role')
    require(recipe.actors[subject.actor].session.value == 'fresh', 'candidate code carries explicit state and needs fresh model calls')
    require(subject.initial_artifact.codec.name == 'proteus.file_tree', 'candidate code requires a file-tree artifact')
    require(subject.input_types == {'memory': 'proteus.code.state@1'}, 'code prediction needs an explicit memory input')
    require(not subject.mutable_scopes, 'code memory is explicit serialized state, not inherited files')
    require(recipe.execution is not None and recipe.execution.environment.backend.plugin.name == 'proteus.local_json',
            'this candidate-code provider requires the explicit trusted host environment')
    require(recipe.execution.timeout_seconds is not None, 'candidate code requires a finite invocation timeout')
    require(recipe.execution.state_policy in (StatePolicy.INDEPENDENT, StatePolicy.SEQUENCE),
            'candidate code currently uses explicit per-call memory, not persistent workers')


class CodeSubject:
    def __init__(self, options):
        self.options = options

    def run(self, context):
        memory = context.inputs['memory']
        if context.state.payload:
            require(set(context.state.payload) == {'code_memory'}, 'unknown state in code prediction')
            memory = CodeState.from_dict(context.state.payload['code_memory'])
        require(type(memory) is CodeState and memory.artifact_hash == context.candidate.artifact.content_hash,
                'memory belongs to another candidate code version')
        require(context.actor_runtime is not None and context.lease.local_root is not None,
                'code prediction requires a managed actor and local execution lease')
        usages = []

        def actor(prompt, *, timeout_seconds):
            result = context.actor(prompt, timeout_seconds=timeout_seconds)
            usages.append(result.usage)
            return result

        call = CodeCall(method=self.options.prediction_method, state=memory, args=(context.task.input,), seed=context.seed)
        reply = invoke(context.lease.local_root, self.options, call, actor, timeout_seconds=context.execution.timeout_seconds)
        output, trace, status, diagnostic = reply.output, reply.trace, reply.status, reply.diagnostic
        if status == Outcome.SUCCEEDED and self.options.prediction_output == 'answer_metadata':
            if not isinstance(output, list) or len(output) != 2 or not isinstance(output[1], dict):
                status, diagnostic, output = Outcome.INVALID_CANDIDATE, 'prediction must return (answer, metadata)', None
            else:
                output, metadata = output
                trace = Trace(events=(*trace.events, {'kind': 'prediction_metadata', 'metadata': metadata}))
        state = CodeState(artifact_hash=memory.artifact_hash, initialized=True, value=reply.state)
        return SubjectResult(loaded_artifact_hash=context.lease.loaded_artifact_hash,
            runtime=RuntimeIdentity(runtime=context.actor_runtime, settings={'code_protocol': self.options.to_dict()},
                                    environment_hash=content_hash(context.execution.environment)),
            status=status, output=output, state=StateValue(payload={'code_memory': state.to_dict()})
                if status == Outcome.SUCCEEDED else context.state, trace=trace,
            usage=Usage() if status == Outcome.INFRA_ERROR else replace(sum_usage(usages), wall_seconds=0), diagnostic=diagnostic)
