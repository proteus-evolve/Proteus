"""Private, attributable actor execution for external model-backed evaluators."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import math

from proteus.artifacts.store import _read, publish_bytes
from proteus.core.contracts import RuntimeIdentity, require
from proteus.core.methods import ActorFailed, ActorRequest, ActorResult
from proteus.core.recovery import uncertain_execution
from proteus.core.serialization import Contract, canonical_json, content_hash, read_json
from proteus.evaluation.records import Feedback, Grade, zero_usage
from proteus.core.contracts import Outcome, Usage


class GradingContext:
    """One private actor dispatch owned by the grader's ledger reservation.

    Nothing is published to the public actor/value stores. Unknown execution is
    left unsettled by the evaluation service; creating another context cannot
    bypass its durable reservation. Plugins must route all model work here.
    """

    def __init__(self, spec, runtime, root, *, role, binding, system=''):
        self.spec, self.runtime, self.root, self.system = spec, runtime, root, system
        self.role, self.binding = role, binding
        self.started = False
        self.result = None
        self.input_hash = self.outcome_hash = None

    def actor(self, prompt, *, system='', timeout_seconds=None):
        require(not self.started, 'model evaluator supports one actor dispatch per grade')
        request = ActorRequest(prompt=prompt, system='\n\n'.join(value for value in (self.system, system) if value),
                               timeout_seconds=timeout_seconds)
        self.root.mkdir(parents=True, exist_ok=False)
        value = {'actor': self.spec, 'role': self.role, 'binding': self.binding, 'request': request}
        self.input_hash = content_hash(value)
        publish_bytes(self.root / 'input.json', canonical_json({'hash': self.input_hash, 'value': value}).encode())
        workspace = self.root / 'work'
        workspace.mkdir()
        self.started = True
        try:
            result = self.runtime.execute(self.spec, request, workspace)
        except ActorFailed as exc:
            self._finish(exc.result)
            raise
        except BaseException as exc:
            if not uncertain_execution(exc):
                value = {'input_hash': self.input_hash, 'error_type': type(exc).__name__}
                self.outcome_hash = content_hash(value)
                publish_bytes(self.root / 'outcome.json',
                    canonical_json({'hash': self.outcome_hash, 'value': value}).encode())
            raise
        self._finish(result)
        require(result.status.value == 'completed', result.diagnostic or 'grading actor failed')
        return result

    def _finish(self, result):
        require(type(result) is ActorResult, 'grading runtime must return ActorResult')
        self.result = result
        value = {'input_hash': self.input_hash, 'result': result}
        self.outcome_hash = content_hash(value)
        publish_bytes(self.root / 'outcome.json',
            canonical_json({'hash': self.outcome_hash, 'value': value}).encode())

    def usage(self):
        return self.result.usage if self.result is not None else Usage() if self.started else zero_usage()

    def identity(self):
        if not self.started:
            return None
        identity = self.result.runtime if self.result is not None else RuntimeIdentity(runtime=self.spec.runtime.plugin,
            settings={'requested_model': self.spec.model})
        return replace(identity, settings={**identity.settings, 'private_grading': {
            'input_hash': self.input_hash, 'outcome_hash': self.outcome_hash}})


def grading_binding(evaluation_id, rollout):
    return {'evaluation_id': evaluation_id, 'rollout_id': rollout.id, 'task_id': rollout.task_id,
            'candidate_id': rollout.candidate_id, 'artifact_hash': rollout.artifact_hash}


def verify_grade(root, grade, actor, *, role, binding):
    """Verify private actor provenance without executing any plugin/provider."""
    if grade.runtime is None:
        require(grade.usage.model_calls == 0, 'model grade has usage without an actor receipt')
        return
    marker = grade.runtime.settings.get('private_grading')
    require(isinstance(marker, Mapping) and set(marker) == {'input_hash', 'outcome_hash'},
            'model grade has no private actor provenance')
    require(all(not parent.is_symlink() for parent in (root, root.parent, root.parent.parent)),
            'private grader evidence directory cannot be a symlink')
    records = []
    for filename, key in (('input.json', 'input_hash'), ('outcome.json', 'outcome_hash')):
        record = read_json(_read(root / filename).decode('utf-8'))
        require(set(record) == {'hash', 'value'} and content_hash(record['value']) == record['hash'] == marker[key],
                'private grading evidence identity differs')
        records.append(record['value'])
    request, outcome = records
    require(request['actor'] == actor.to_dict() and request['role'] == role and request['binding'] == binding,
            'private grader role/candidate/task binding differs')
    ActorRequest.from_dict(request['request'])
    require(outcome['input_hash'] == marker['input_hash'], 'private grader response belongs to another request')
    if 'result' in outcome:
        result = ActorResult.from_dict(outcome['result'])
        expected = replace(result.runtime, settings={**result.runtime.settings, 'private_grading': marker})
        require(grade.runtime == expected, 'model grade runtime differs from its actual actor result')
        require(all(getattr(grade.usage, key) == getattr(result.usage, key)
                    for key in ('model_calls', 'input_tokens', 'output_tokens', 'cost_usd')),
                'model grade usage differs from its actual actor result')
    else:
        require(set(outcome) == {'input_hash', 'error_type'} and grade.status == Outcome.INFRA_ERROR
                and grade.score is None, 'failed grading transport cannot report a successful score')
        require(all(getattr(grade.usage, key) is None
                    for key in ('model_calls', 'input_tokens', 'output_tokens', 'cost_usd')),
                'unreported grading transport usage must stay unknown')
    require(grade.runtime.runtime == actor.runtime.plugin, 'model grade uses a different runtime role')


@dataclass(frozen=True, kw_only=True)
class ModelJudgeOptions(Contract):
    rubric: str = 'Score whether the prediction correctly answers the task, using the reference.'
    min_score: float = 0
    max_score: float = 1
    max_feedback_chars: int = field(default=4000, metadata={'minimum': 1})

    def _validate(self):
        require(bool(self.rubric.strip()), 'model judge needs a rubric')
        require(self.min_score <= self.max_score, 'model judge score bounds are reversed')


class ModelJudge:
    def __init__(self, options):
        self.options = options

    def grade(self, task, rollout, reference, *, context):
        options = self.options
        prompt = (options.rubric + '\nReturn exactly one JSON object with numeric score and optional string feedback. '
                  f'Score must be between {options.min_score} and {options.max_score}.\n' +
                  canonical_json({'task': task, 'prediction': rollout.output, 'reference': reference}))
        reply = context.actor(prompt)
        answer = read_json(reply.text)
        require(type(answer) is dict and 'score' in answer and set(answer) <= {'score', 'feedback'},
                'model judge response does not match score/feedback schema')
        score = answer['score']
        require(type(score) in (int, float) and math.isfinite(score)
                and options.min_score <= score <= options.max_score, 'model judge score is outside its declared bounds')
        feedback = answer.get('feedback', '')
        require(type(feedback) is str and len(feedback) <= options.max_feedback_chars,
                'model judge feedback is invalid or exceeds its declared limit')
        return Grade(status=Outcome.SUCCEEDED, score=score, feedback=Feedback(value=feedback))
