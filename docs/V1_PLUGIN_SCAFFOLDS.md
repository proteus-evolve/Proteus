# Editable researcher plugins

`proteus recipe scaffold` creates an independent Python plugin project. A method,
stage, evaluator or subject adapter can be edited and registered without changing
Proteus core code. The project includes preparation code, a runnable offline
recipe, a contract-check script and instructions for locking real source versions.

```sh
proteus recipe scaffold stage --name my_review --out my-review
cd my-review
python prepare.py
PYTHONPATH=. python -m proteus.cli recipe check study/recipe.json --plugin my_review:register
python check.py
```

The output directory must be new. Existing projects and names that shadow Python,
Proteus or the generated scripts are rejected before files are written. The same
command accepts `method`, `evaluator` and `subject` in place of `stage`.

| Template | Editable mechanism | Checks shipped with the project |
| --- | --- | --- |
| `method` | Evaluate the initial candidate through managed services; checkpoint after each cohort | Formal evaluation count, private test receipt, recovery without repeated work |
| `stage` | Return typed self-evaluation evidence from a proposal | Ports, output semantics, zero model usage, changed configuration and recovery |
| `evaluator` | Score numeric outputs against private references with a configured tolerance | Baseline/changed-candidate scores, private test visibility and recovery |
| `subject` | Read a factor from the exact materialized candidate and execute each numeric task | Candidate-dependent outputs, changed artifact, formal scoring and recovery |

`check.py` creates temporary outputs and uses explicitly labelled fixture source
pins. It makes no model calls. For an actual study, use the generated README's
`recipe lock` command with the Git repositories containing Proteus and the plugin.
The framework locks the plugin source hash, schemas and declared capabilities.
Changing plugin code requires a fresh lock and run; old runs keep their original
implementation. Include new helper files in the plugin's `implementation_hash`.

## Add self-evaluation through configuration

The stage project's recipe composes the existing prompt stage with `my_review.stage`.
It uses an echo actor for the offline example. Change the `assess` stage's
`operator.options.minimum_characters` from `1` to `1000` in `study/recipe.json`,
then check and lock the changed recipe for a new run. The typed assessment changes
without modifying the engine. The shipped check performs this configuration change
and verifies the resulting assessment.

This local length check is self-evaluation evidence, not a formal task score. A
research method can consume that evidence explicitly while keeping its independent
evaluator. The templates show where to add a mechanism; they do not imply that an
arbitrary new method can be expressed only by changing configuration.

## Validation and remaining researcher tools

Repository checks run the generated projects through their real command-line
entry, registry and execution services. A separate method check terminates the
controller after a committed evaluation cohort and resumes it without duplicating
the candidate, trials or evaluation charges. Wheel and sdist gates run all four
projects outside the checkout.

The examples execute trusted local code. New backend, concurrency or session
capabilities require corresponding tests. Recipe catalog discovery, a search-policy
extension tutorial, unified result export and complete 1.0 release acceptance
remain separate work; this increment does not complete I13–I15.
