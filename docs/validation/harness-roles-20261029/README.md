# Harness-role engineering evidence, 2026-10-29

The source-bound [acceptance record](acceptance.json) identifies the tested code and
package artifacts. This is mechanism validation for the additional role bridges.
It is independent of PR #106 and is not research or release acceptance.

| Current code check | Outcome |
| --- | --- |
| Affected role, workspace, evaluation, export, budget and Native source/activation suites | 245 passed, 1 environment skip |
| Installed wheel, outside the checkout: roles, methods, private graders, recovery, backward shapes and accounting | 81 passed, 1 Docker skip |
| Offline / JavaScript / Ruff | 145 passed / 8 passed / clean |
| wheel + sdist build, twine and exact package Python bytes | passed; all 261 Python files match |
| Installed wheel and sdist standalone Native example | 3 episodes, 4 task evaluations, zero model calls; completed resume dispatches zero work |
| Real saved comparison and coding-workspace reports generated under `325ab5e` | old/new report objects and hashes identical |

These suites overlap; their counts are not a sum of independent cases. The current
source check is the affected suite, not a new full-sweep claim. The earlier full
sweep of a pre-hardening snapshot had **1716 passed / 92 skipped / 1 failed**. Its
only failure was an overly narrow error-message regex: the finite call cap was
correctly rejected before dispatch. The assertion was corrected and the current
suite passes. Raw failures, skipped checks and earlier packaging-fixture setup
failures are retained with the evidence; they were not changed into passes.

The final routing follow-up explicitly points staged source predictions into the
writable `/workspace/candidate/` tree. It was rechecked with 245 affected source
cases and 81 installed-wheel cases passing; earlier snapshot artifacts are retained
separately. These are assertions over adapter contracts and a source subprocess
fixture, not a replacement for the open production-container checks.

The tests demonstrate:

- One real LLM adaptation executes changed candidates in the true pinned GEPA loop
  and in Native evolution, using a deterministic local HTTP provider and the same
  external exact-match evaluator.
- GEPA's reflection role switches between the LLM adaptation and a separate
  subprocess adaptation without method edits. The subprocess integration also
  edits another system as a coding-workspace actor.
- A failed source candidate remains pending for repair by the last-valid runtime;
  later phases and the next episode execute the correct active files. Candidate
  identity, usage and viability bind to immutable artifacts and native receipts.
- Settled work is reused after a process hard exit. Unknown outcomes retain their
  reservation and cannot be dispatched again. Corrupt subject/private-grader
  receipts block resume and export.
- Native inline feedback is limited to its declared evidence view, including when
  feedback kinds are excluded or the inline byte cap is zero. Hidden references
  and model-grader prompts/outcomes remain private.

The fixture scores are not benchmark improvements. Pi/DSH/Codex production
containers have **not** been accepted through the new candidate/evolution bridges
in this local environment; Docker is unavailable. Source-directory hooks and
receipt parsing are narrower evidence. Existing release/native-source success on
another commit cannot close this gap. The legacy Native API remains unchanged;
the explicit new profile does not port all legacy research controls.

Original logs, JUnit files, package artifacts and saved old-reader records are
retained locally under
`/Users/myyyth/Documents/Proteus-v1-experiments/harness-role-contracts-20261029`.
They contain synthetic fixture work, not new paid research runs. CI status belongs
to the PR's exact head and must be read there; this document does not declare
unstarted or pending jobs successful.
