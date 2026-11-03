from dataclasses import replace
from pathlib import Path
import json
import shutil
import sys
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_v1_parallel_evaluation import setup, run
from test_v1_evaluation import register, plugin
from proteus.core.contracts import BudgetLimits, RuntimeIdentity, Usage
from proteus.core.methods import ActorResult
from proteus.core.recovery import RecoveryRequired
from proteus.evaluation.parallel import TrialMap
from proteus.recipes.config import ActorSpec
from proteus.recipes.export import report_run

root = Path(__file__).parent / 'grader-recovery-probe'
registry, _, _, recipe = setup(root, workers=2, cap=2, timing='after_prediction')
class Judge:
    model_call_bound = 1
    def execute(self, spec, request, workspace):
        return ActorResult(text='{"score": 1}', runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
            usage=Usage(model_calls=1, input_tokens=0, output_tokens=0, cost_usd=0))
register(registry, 'fixture.review_judge', 'actor', Judge,
    capabilities=('actor.execute', 'actor.parallel', 'session.fresh', 'calls.bounded'))
recipe = replace(recipe, actors={'judge': ActorSpec(runtime=plugin('fixture.review_judge'),
    budget=BudgetLimits(max_concurrency=2))},
    evaluations=(replace(recipe.evaluations[0], evaluator=plugin('proteus.model_judge'), actor='judge'),))
original = TrialMap.join
def interrupted(self, records):
    raise RecoveryRequired('injected controller interruption after all branch results, before join')
TrialMap.join = interrupted
first, _ = run(root, registry, recipe)
TrialMap.join = original
path = next((root / 'run').glob('evaluations/*/private/grader-actors/*/input.json'))
raw = json.loads(path.read_text())
raw['value']['role'] = 'corrupted-role'
path.chmod(0o644)
path.write_text(json.dumps(raw))
second, _ = run(root, registry, recipe, resume=True)
audit = None
try:
    report_run(root / 'run')
except Exception as exc:
    audit = f'{type(exc).__name__}: {exc}'
result = {'first': {'status': first.status.value, 'diagnostic': first.diagnostic},
    'resumed_after_corrupt_private_grader_evidence': {'status': second.status.value, 'diagnostic': second.diagnostic},
    'report_rejection': audit}
(Path(__file__).parent / 'grader-recovery-results.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
shutil.rmtree(root)
