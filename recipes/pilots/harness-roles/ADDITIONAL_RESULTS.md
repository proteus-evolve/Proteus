# Additional RSI method / harness acceptance

All four additional **synthetic mechanism pilots** have accepted evidence with
real Pi execution. This verifies the specified roles and minimum cycles, not
benchmark improvement, arbitrary method × harness compatibility, or paper-level
reproduction. The runtime fixes and recipes are in PR #108; this report does not
imply that PR has merged.

| Method | Real Pi role and changed candidate | Coverage | Actual result |
| --- | --- | --- | --- |
| ACE | Generator executes the current typed playbook rendered in its prompt | One training sample, reflection/curation, 2 candidates, 6 task rollouts | Playbook changes and is used; validation remains 0. Initial candidate is selected; initial and final private score both 0. |
| Combee-ACE | Independent generator workspaces execute the current playbook | One two-sample batch; 2 map proposals and 1 reducer; 2 candidates, 9 rollouts | Aggregated playbook changes and is used; validation remains 0. Initial candidate is selected; initial and final private score both 0. |
| Meta-Harness | Coding proposer reads disclosed history and writes memory-system Python | One proposal iteration, 2 baselines + 3 generated systems, 12 rollouts | All generated systems pass interface checks and achieve 1 on observed examples, as do baselines. Final selection retains NoMemory/FewShotAll; only those baselines receive private tests, both 1. |
| CORAL | One supervised worker invokes two fresh Pi coding workspaces | 2 changed code submissions, 3 candidates including H0, 7 rollouts | Observed accuracy 0 → 0 → 1. Second submission is selected and scores 1 on the private case. No initial private score was measured. |

All accepted cells check that completed resume adds **zero new actor/worker
dispatch receipts**. Independently regrading all **34 recorded rows** from the
saved predictions and references reproduces every reported score. Candidate
artifact hashes, selected/last identities, batch rows, source commits and image
IDs are in [additional-results.json](additional-results.json).

## What the evidence establishes

ACE and Combee actually pass new playbook text to later real harness executions.
The generator's complete responses cite the new bullet IDs. The learned text in
this run mainly offered general advice about discovering the policy, while the
task still specified a lowercase fallback. The generator continued returning
lowercase. These are valid zero scores, not missing evaluations; they establish
data flow without demonstrating useful optimization.

Combee uses its actual two-sample workflow map, two accepted aggregation proposals
and accepted reducer. It is not a serial ACE run renamed Combee. This test does not
measure speedup, dynamic batching, online behavior, or the eight historical study
conditions. Combee-GEPA is not included in this additional matrix.

Meta-Harness produced `contrastive_label_memory`, `reflection_lesson_memory`, and
`adaptive_verification_memory`. Each was executed and evaluated on the synthetic
observed pool. The coding harness is infrastructure; the optimized artifact is a
memory program. The private score 1 belongs to the selected baselines, **not** to
the three unselected generated programs. No superiority over baselines is shown.

CORAL's first actor-produced program preserved the initial numerical behavior.
After receiving the real released references `2 → 4` and `3 → 6`, its next coding
turn changed the program to multiply by two. The final private execution returned
`7 → 14`. The worker's `feedback-used.md` explicitly identifies those released
records. This is one supervised worker with fresh sessions and explicit carried
files/feedback; persistent provider sessions, heartbeat intervention and
multi-worker cooperation remain unverified here.

ACE/Combee reflector and curator roles use managed DeepSeek chat, and Meta's
candidate programs call managed DeepSeek chat. Those are distinct from Pi's
generator/proposer/worker roles. All requested `deepseek-v4-flash`; metadata must
not be interpreted as proof that every role reports a resolved provider model ID.

## Source and failure provenance

- [First run 40980909790](https://github.com/proteus-evolve/Proteus/actions/runs/40980909790),
  source `3e8a0e69d1d0ea7a628266378812a481ce33e61b`: overall **failure**; artifact
  `12945595368`. CORAL is accepted from this run. ACE's apparent initial pass was
  withdrawn after audit found three truncated JSON replies. Combee also had three
  truncated replies and a checker looking for the wrong evidence kind. Meta's
  budget-capped proposer returned no files and its image lacked Python. Original
  failures, baseline-only outcomes and unknown costs remain retained.
- [Correction run 40982188649](https://github.com/proteus-evolve/Proteus/actions/runs/40982188649),
  source `ffa106e5a1614fe2c5d8c7c56c9d6de204bcf17d`: overall **success**; artifact
  `12945053185`. ACE, Combee-ACE and Meta-Harness are accepted from this run. CORAL
  was intentionally not rerun. These separate source bindings must not be combined
  into a claim that all cells or release gates passed on one SHA.

The core defect was using shortened measurement traces as structured actor replies.
Pi/DSH previews kept 500 characters; Codex previews kept 1000. The repair reads the
complete final visible assistant response separately, excluding progress,
reasoning-only and diagnostic entries. All 15 ACE/Combee replies in the correction
run parse as complete JSON; seven exceed the old 500-character limit. Short trace
previews retain their existing behavior.

The correction also fixes the Combee evidence lookup, requires complete parsed
responses, installs Python in a derived source-pinned authoring image, and gives
the single Meta proposal iteration 64 tool-budget units and 480 seconds. The live
steps now retain actual failure conclusions while continuing independent cells.

Local verification: initial related suite **83 passed / 12 skipped**, pilot
configuration/boundary checks **7 passed**, response repair and related suite
**66 passed / 1 skipped**, and adapter regressions **80 passed**. These are separate
overlapping suites, not additive unique-test counts. Ruff and workflow parsing
passed. Current PR CI/release-gate acceptance is separate from these cell results.

## Time, cost and reproduction

Accepted cell elapsed times include preparation and completed-resume checks:
ACE about 52 seconds, Combee about 57 seconds, Meta about 4 minutes, CORAL about
33 seconds. Workflow image builds and the failed first attempts are additional.
Provider billing totals are **unknown**, not zero; all earlier attempts remain
part of cost provenance. AWS was not started.

See [setup and commands](OTHER_METHODS.md). The workflow's `other-methods` option
runs all four cells; `other-recovery` runs ACE/Combee/Meta only. Both are paid,
explicit opt-in selections. DSH/Codex versions of these four cells remain
unverified. Prior Native/GEPA Pi/DSH results are recorded separately in
[RESULTS.md](RESULTS.md), with their own measured source identities.
