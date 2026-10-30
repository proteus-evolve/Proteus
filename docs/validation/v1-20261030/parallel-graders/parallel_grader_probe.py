from dataclasses import replace
from pathlib import Path
import json
import shutil
import sys
import threading
import time

sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_v1_parallel_evaluation import setup, run
from test_v1_evaluation import register, plugin
from proteus.core.contracts import BudgetLimits, RuntimeIdentity, Usage
from proteus.core.methods import ActorResult
from proteus.recipes.config import ActorSpec

root = Path(__file__).parent / 'grader-probes'
root.mkdir(exist_ok=True)
results = []
for role_cap, parallel in [(1, True), (2, False)]:
    work = root / f'cap-{role_cap}-parallel-{parallel}'
    registry, _, _, recipe = setup(work, workers=2, cap=2, timing='after_prediction')
    state = {'active': 0, 'peak': 0, 'calls': 0}
    mutex = threading.Lock()
    class Judge:
        model_call_bound = 1
        def execute(self, spec, request, workspace):
            with mutex:
                state['active'] += 1
                state['calls'] += 1
                state['peak'] = max(state['peak'], state['active'])
            time.sleep(0.5)
            with mutex:
                state['active'] -= 1
            return ActorResult(text='{"score": 1}', runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
                usage=Usage(model_calls=1, input_tokens=0, output_tokens=0, cost_usd=0))
    caps = ('actor.execute', 'session.fresh', 'calls.bounded') + (('actor.parallel',) if parallel else ())
    register(registry, 'fixture.review_judge', 'actor', Judge, capabilities=caps)
    recipe = replace(recipe,
        actors={'judge': ActorSpec(runtime=plugin('fixture.review_judge'),
            budget=BudgetLimits(max_concurrency=role_cap))},
        evaluations=(replace(recipe.evaluations[0], evaluator=plugin('proteus.model_judge'), actor='judge'),))
    registry.resolve(recipe)
    result, records = run(work, registry, recipe)
    results.append({'role_cap': role_cap, 'declares_parallel': parallel, 'preflight_accepted': True,
        'status': result.status.value, 'diagnostic': result.diagnostic, **state})
    shutil.rmtree(work)
(root / 'results.json').write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))
