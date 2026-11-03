---
name: proteus-contribute
description: Extend or contribute to the proteus-evolve/Proteus self-improvement framework. Use when adding or changing methods, stages, search policies, evaluator plugins, subject runners, harness integrations, recovery/accounting behavior, visualization or public APIs, or preparing a Proteus code contribution. This is an optional contributor workflow, not a mandatory repository policy. Use proteus-use instead for configuring or running existing experiments without framework development.
---

# Contribute to Proteus

Implement the requested change at the appropriate extension boundary and leave a
reviewable diff with relevant validation. This skill is an optional aid; contributors
can work directly from the repository documentation without installing it. Follow
the user's scope and any applicable repository instructions. Do not add skill usage
as a CI or contribution requirement.

## Inspect the target before editing

Locate the Proteus checkout and record its branch, commit, dirty files and active
runs. Preserve unrelated work and immutable experiment evidence. Documentation paths
below are relative to that **checkout root**, not this installed skill folder.
Use source and docs matching the target version; do not copy assumptions from a
newer skill into an older checkout without checking its contracts.

Read `CONTRIBUTING.md`, `docs/V1_OVERVIEW.md`, `docs/V1_PUBLIC_API.md` and the specific
contract being changed. Prefer a small external plugin when the public API can
express the request. Use `proteus.api.v1` for external plugin imports. Change core
only when the requested behavior requires it; avoid a special case for each
method/harness pair.

## Choose the extension boundary

| Request | Entry point and contract |
| --- | --- |
| New optimization loop | Method plugin; owns control flow, method state, checkpoints and service calls |
| Reusable proposal or self-evaluation step | Typed stage plugin; declare input/output ports and how the method consumes them |
| Parent/final selection or archive policy | Policy plugin with the required search capability and immutable eligible evidence |
| Execute a candidate on a task | Subject plugin; consume the exact candidate, produce a bound `SubjectResult` |
| Score a prediction | Evaluator plugin; private reference/verifier channel, declared scoring and failure semantics |
| Integrate another agent runtime | `HarnessIntegration` plus adapter/factory, role options, capabilities and continuity; see `docs/V1_HARNESS_ROLES.md` |
| Native adapter or local benchmark | Existing `HarnessAdapter` / `BenchTask` contracts in `docs/ADAPTERS.md` and `docs/BENCHMARKS.md` |
| Visualization | `docs/VISUALIZE.md` and `proteus/visualize/`; preserve read-only viewing and evidence boundaries |

A candidate is the optimized artifact. An actor/harness executes a role. A subject
runs the candidate; a method calls stages and evaluation services to improve it.
Native is one method profile. Do not impose Native phase order on all methods or
put the entire search loop inside a subject runner.

For method, stage, evaluator, subject or policy plugins, start from the existing
scaffold. For example, with Proteus installed, create a new directory outside its
source tree:

```bash
PROTEUS_PLUGIN="$(mktemp -d)/my-review"
proteus recipe scaffold stage --name my_review --out "$PROTEUS_PLUGIN"
cd "$PROTEUS_PLUGIN"
python prepare.py
PYTHONPATH=. python -m proteus.cli recipe check study/recipe.json --plugin my_review:register
python check.py
```

The generated check is an offline fixture. For real experiments, follow its README
to lock the actual Proteus and plugin repositories. Include helper modules in the
plugin's implementation hash and register precise option/state schemas and
capabilities. Do not weaken a contract check just to make an unsupported role fit.

## Preserve these framework invariants

- **Candidate identity:** materialize the specified immutable artifact. Respect
  declared mutable paths; do not reseed over evolved files. For staged activation,
  execute the last-valid source, edit a separate candidate, validate it, and activate
  it in the next episode. Retain rejected edits for repair.
- **Role separation:** bind optimizer, subject and model-grader independently. Check
  capabilities and candidate representation before dispatch. The same integration
  in two roles still needs independent instances/state where the contract requires it.
- **Evaluation boundary:** keep hidden task references, verifier traces and private
  scores out of optimizer observations, selection views and public exports. Record
  explicit splits, feedback timing and eligibility. Self-assessment is typed evidence,
  not automatically a formal score or a substitute for external evaluation.
- **Managed operations:** route calls and evaluations through the relevant framework
  services. Preserve reservations, settled results, failures and unknown usage.
  An infrastructure failure is not a task-score zero; unknown cost is not free.
- **Recovery:** checkpoint serializable method state, cursors and RNG state at safe
  boundaries. Reuse committed work. An external action without a durable result
  stays uncertain; reconciliation must establish the original outcome without a
  fresh call. Declare replay support only when repeated control flow is safe.
- **Source locks:** new code/options require a new lock and run. Never edit code
  during a recovery acceptance test or relabel an old run as evidence for new code.
  Preserve frozen recipes, original failures and source-bound acceptance records.
- **Public compatibility:** follow `docs/V1_COMPATIBILITY.md` for versioned API/schema
  changes. Keep the existing Native CLI working. Adding a plugin does not imply a
  new public API or support for every method/harness combination.

## Validate the changed behavior

Use a virtual environment and the repository's dev dependencies. Run the generated
plugin checks for an extension and focused existing tests for the affected contract.
Add a behavioral regression for a bug or new behavior: exercise a meaningful input
or failure, verify the outcome, and make it fail before the fix where feasible.
Do not add tests that only mirror wording or an implementation detail.

Choose checks by the behavior changed:

| Changed behavior | Useful evidence |
| --- | --- |
| Candidate execution or source activation | Two different candidates produce corresponding behavior; accepted source is used in the next episode; rejected source retains the previous valid runtime |
| Evaluator or self-evaluation stage | Known positive/negative fixtures, correct output shape and visibility; downstream consumption of self-evidence where claimed |
| Search or composition | Same eligible evidence, recorded decisions/lineage, and observable use of the new policy/stage |
| Recovery, concurrency or accounting | Process interruption at the affected boundary, repeated resume, no duplicate dispatch/settlement, unknown outcome retained |
| Public API or package data | External import/scaffold smoke from an installed wheel/sdist outside the checkout |
| Viewer | Relevant JavaScript checks and a local render using real fixture artifacts |

Use the current CI workflow as the source of truth for broader checks. Typical
repository commands are `ruff check .`, `pytest tests/ -q` and
`python tests/run_offline.py`; optional dependencies and integration gates are
listed in `.github/workflows/` and `docs/RELEASING.md`. Record skipped prerequisites
as skipped. Docs-only changes need link/example checks, not a paid model run.
Run provider/Docker/large experiments only within the user's existing authorization;
do not start historical studies or release jobs merely because they exist.

## Deliver the contribution

Explain the trigger/problem, changed behavior, extension point, relevant tests and
support limits. Update the matching user-facing doc/example and credit original
papers and software when implementing a method. Keep offline fixtures, live pilots
and benchmark results distinct. Mention changes to schemas, compatibility or source
identity. If a PR is requested, follow the repository template and make its final
scope and evidence clear. Commit, push, merge and release according to the user's
authorization; using this skill itself grants no publishing authority.

## Read on demand

All paths are relative to the matching Proteus checkout root.

| Need | Read |
| --- | --- |
| API and independent extensions | `docs/V1_PUBLIC_API.md`, `docs/V1_PLUGIN_SCAFFOLDS.md`, `proteus/api/v1.py` |
| Method ownership and composition | `docs/V1_OVERVIEW.md`, the chosen `docs/V1_*METHOD.md` or method-specific guide |
| Harness roles and Native adapters | `docs/V1_HARNESS_ROLES.md`, `docs/ADAPTERS.md`, `proteus/core/adapter.py` |
| Evaluators and policies | `docs/BENCHMARKS.md`, `docs/V1_SEARCH.md` |
| Recovery, identity and compatibility | `docs/V1_RECOVERY.md`, `docs/V1_COMPATIBILITY.md` |
| Evidence, releases and attribution | `docs/RELEASING.md`, `docs/METHOD_REFERENCES.md`, `references.bib` |
