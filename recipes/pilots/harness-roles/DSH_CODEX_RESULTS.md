# DSH / Codex minimum-cycle acceptance

**Status: all eight requested cells independently accepted (2026-10-30 UTC).**
See [structured evidence](dsh-codex-results.json) for source/run/artifact identities.

This matrix tests [ACE](https://arxiv.org/abs/2510.04618v3)
([code](https://github.com/ace-agent/ace)),
[ComBEE](https://arxiv.org/abs/2604.04247v2) with ACE
([code](https://github.com/gepa-ai/gepa)),
[Meta-Harness](https://arxiv.org/abs/2603.28052v1)
([code](https://github.com/stanford-iris-lab/meta-harness)) and
[CORAL](https://arxiv.org/abs/2604.01658v3)
([code](https://github.com/Human-Agent-Society/CORAL)) through two real coding
harness integrations. The tasks are deliberately synthetic integration
fixtures. Passing establishes the specified execution, feedback and recovery
paths; it does not reproduce paper experiments or demonstrate benchmark gains.

## Harness roles and setup

| Method | Optimized artifact | Coding harness role | Managed model roles | Minimum cycle |
| --- | --- | --- | --- | --- |
| ACE | Typed playbook | Generator executes tasks under the current playbook | Reflector and curator | One training sample, reflection/curation, changed-playbook execution, validation and private test |
| ComBEE-ACE | Typed playbook | Two independent generator workspaces | Map proposals and reducer | One two-sample batch, two aggregation proposals, one reduction, changed-playbook execution and private test |
| Meta-Harness | Python memory program | Workspace proposer reads released history and writes programs | Candidate learning/prediction through the memory API | One proposal iteration, interface validation, generated-program execution, selection and private test of selected systems |
| CORAL | Python task program | One supervised worker invokes two fresh coding workspaces | No separate optimizer model | Two exact-file submissions with released formal feedback between them, evaluation and final selection/test |

The harness implementation stays fixed in these four profiles. The candidate is
not the harness infrastructure. Native source evolution and GEPA policy overlays
are separate profiles with their own earlier evidence.

DSH requests `deepseek-v4-flash`. Codex explicitly requests `gpt-5.4-mini`; managed
roles request `deepseek-v4-flash`. These runs therefore are not a controlled
comparison of models or harness quality. Requested/applied identifiers are not
substituted for an unavailable resolved provider model ID.

Upstream source pins:

- DSH: [`99f6f02fecdb7dff40c3fbc9470f5907c29f74ca`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
- Codex: [`a7b86b6201d35200280e3d24ca5ebccd8122d6ca`](https://github.com/openai/codex/tree/a7b86b6201d35200280e3d24ca5ebccd8122d6ca).
- GEPA dependency used by the common registry: `8b0ce6cd99a234f6b74daf37558a2ac0ce18f975`.

## DSH accepted results

| Method | Coverage | Recorded outcome |
| --- | --- | --- |
| ACE | 2 candidates, 6 rollouts; updated playbook appears in 3 later CLI prompts | Updated validation score 1; initial and final private scores both 0. The final candidate is selected. No initial validation score was measured. |
| ComBEE-ACE | 2 candidates, 9 rollouts; 2 map proposals, 1 reducer; updated playbook appears in 4 later CLI prompts | Updated validation score 1; private score 0 → 1. The final candidate is selected. |
| Meta-Harness | 2 baselines + 3 generated systems, 12 rollouts | Generated systems and baselines all score 1 on observed examples. Selection retains NoMemory/FewShotAll; only those baselines are private-tested, both 1. |
| CORAL | 3 candidates including H0, 7 rollouts, 2 real coding turns | Observed score 0 → 0 → 1; second submission selected, private score 1. No initial private score was measured. |

The ACE playbook adds general advice to question the lowercase fallback, without
settling on a precise rule. ComBEE's learned playbook explicitly says the observed
correct fallback was uppercase. Both texts reach later real CLI prompts. This
explains what changed in these particular runs; the tiny fixtures and different
recovery configurations do not establish a comparison of method quality.

The generated DSH Meta programs are `contrastive_boundary_memory`,
`rulebook_induction_memory` and `prototype_differential_memory`. All execute;
none has a measured private score in this run.

DSH acceptance independently regrades 34 saved prediction/reference rows and
checks 45 candidate component files against their recorded hashes. Every
accepted cell records zero new actor/worker dispatches on completed resume.
Native input/outcome hashes and fixed-runtime source witnesses are also checked.
ComBEE native dispatch timestamps show two overlapping CLI invocation intervals;
this verifies overlap, not provider-level concurrency, throughput or speedup.

ACE and CORAL come from run `40994189131`, source `68980e4`, artifact
`12952051426`. That overall workflow failed because ComBEE and Meta failed.
Their successful cells remain individually bound evidence, not a passed workflow.
ComBEE and Meta come from successful recovery run `40998453988`, source
`ea02530`, artifact `12953012593`. Different commits do not form a same-SHA
release gate.

## Codex accepted producer cells

ACE, ComBEE-ACE and CORAL are independently accepted from production-image run
[40993578545](https://github.com/proteus-evolve/Proteus/actions/runs/40993578545),
source `44380dad769e87c435d714b990f03e59e58dbd9c`, artifact `12956393661`.
That workflow failed overall because Meta-Harness failed; three passing cells do
not turn the producer into a passed workflow.

| Method | Coverage | Recorded outcome |
| --- | --- | --- |
| ACE | 2 candidates, 6 rollouts; changed playbook appears in 2 later native prompts | Updated validation 0; selection keeps the initial candidate. Initial and selected private scores are 0. The last candidate has no measured private score. |
| ComBEE-ACE | 2 candidates, 9 rollouts; two map proposals and one accepted reduction; changed playbook appears in 4 later prompts | Updated validation 1; private 0 → 1; final candidate selected. Two native dispatch intervals overlap. |
| CORAL | 3 candidates including H0, 7 rollouts, two fresh coding turns | Observed 0 → 0 → 1, second submission selected, private 1. No initial private score measured. |

The independent producer audit regrades all 22 saved rows, verifies 11 candidate
component files and all 17 native input/outcome receipts, including the byte hash
of every raw Codex JSONL usage source. All 17 native streams end completed, with
no native error/turn-failed events. They contain 28 completed command-execution
items and 2 file-change items. CORAL's second output explicitly cites the released
`2 → 4` and `3 → 6` references before implementing doubling. Completed resume adds
zero dispatches in each accepted cell.

Meta's original attempt evaluates the two baselines but the proposer exits 2
with CLI usage text, no generated programs, and an empty native model stream.
The diagnostic retains the prompt's tail; it does not retain the full stderr
prefix. The unchanged baseline evaluations do not constitute an accepted cycle.
The native parser probe in recovery run `41009956547` confirms the diagnosis:
without `--`, the exact compiled CLI rejects the YAML-prefixed argument with exit
2; with `--`, it reaches the deliberately invalid model-free configuration check.
Both probes run without credentials or network.

That recovery's real proposer completes and writes three systems, but all fail
the candidate loader: their submitted files are self-forwarding imports while
full implementations remain in an ignored `output/text_classification/` mirror.
Its mock tests exercised that mirror, not the submitted package. The attempt and
its six managed baseline rows remain failed evidence. The runtime adaptation
prompt now explicitly prioritizes the actual submission paths and directs the
proposer to check the assembled submitted package with the existing loader in a
fresh scratch process. No generated source is manually repaired or relabeled as
agent output. The subsequent source-bound cycle is documented below.

## Codex Meta accepted recovery

The final cycle is accepted from run
[41011231821](https://github.com/proteus-evolve/Proteus/actions/runs/41011231821),
source `678ddaef698d6cbabb14f04e9d64790b2e826de5`, artifact `12958251219`.
The recovery image is
`sha256:a51db0996d3350d54abca4668c347c0f03117fd5aba9c7492b1ed988175a5af7`;
its binary hashes match the original production build, and its pristine source
hash is `19e60813756ac7b9e940e61bf5b47b0b6ba83fb47e04302a35490161bf8607e4`.

- One real coding iteration produces `contrastive_support_memory`,
  `lesson_ledger_memory` and `confusion_graph_memory`.
- Each new program has one successful formal learning call whose returned state
  is retained, and two actual task rollouts (training and validation). All three
  have observed score 1. There are 5 candidates and 12 total rollouts.
- Selection keeps `fewshot_all` and `no_memory`, whose private scores are both 1.
  **No generated program is private-tested in this run.**
- The native coding stream completes, with 32 command-execution and 3 file-change
  items, and no native error/turn-failed events. It checks the actual assembled
  submission through the existing loader with a mock callable; those checks are
  distinct from the subsequent real managed evaluations.
- The independent audit regrades 12 rows and verifies 52 recorded candidate
  components. Completed resume adds zero dispatches. The live cycle takes about
  176 seconds; this recovery's image preparation takes 180 seconds.

Across the eight accepted cells, the independent audits regrade **68 rows**, check
**108 candidate-component records** (not necessarily distinct files), and verify
**36 native CLI dispatch receipts** (not a count of model calls). All eight
completed-resume checks add **zero** dispatches. Raw native logs, actual selected
candidates, failures, source identities and unknown billing remain preserved.
This is a compatibility matrix collected across repairs, not a controlled
comparison or one-SHA release gate.

## Corrections and retained failures

1. Generic fixed-harness activation previously dereferenced source symlinks,
   changing pinned source identity and potentially rebuilding on each invocation.
   The copy now preserves symlinks; every accepted activation is checked against
   the image's pristine source marker.
2. DSH groups multiple model steps inside one dialogue turn. Its response reader
   now returns the last complete visible assistant message, while trace previews
   retain their limits. Progress text no longer gets prepended to structured answers.
3. DSH's nested workspace-write sandbox was unavailable in CI Docker. The failed
   ComBEE attempt exhausted two tool budgets and retained incomplete responses.
   The pilot explicitly chooses its inner `danger-full-access` policy within the
   existing Docker boundary; the adapter default remains `workspace-write`, and
   no privileged-container flag is introduced.
4. Meta's YAML-prefixed prompt was parsed as a CLI option before proposer startup.
   Two `--` delimiters now preserve the literal task through DSH's launcher and
   headless parsers. Historical command receipts remain readable; resume flags
   remain rejected. The model-free cold-start test exercises a dash-prefixed task.
5. Formatting task version 2 explicitly requests a complete JSON object. The
   completeness checker accepts bare JSON or one complete JSON fence; empty,
   truncated and prose-mixed replies remain failures. The earlier bad ComBEE
   replies are not retrospectively accepted.

Recovery ComBEE and Meta have 82 recorded tool calls/results with zero recorded
tool errors. The earlier accepted ACE/CORAL cells did encounter unavailable-shell
errors before completing their cycles; their acceptance does not mean every tool
attempt succeeded. All cancelled/failed attempts and their unknown costs remain
in provenance. The failed Meta attempt includes six managed baseline calls even
though its coding proposer never started.

## Validation and limits

Source `ea02530`: related local suite 166 passed / 1 environment skip; Ruff,
workflow parsing and shell syntax passed. Full CI, Harbor and Native source gates
passed. Python CI outcomes: 3.10/3.11/3.13 each 1824 passed / 33 skipped;
3.12 1833 passed / 24 skipped; 3.14 1823 passed / 34 skipped. These are overlapping
version runs, not additive unique-test counts. A stale Codex mock that treated
trace reading as final-response reading was fixed; the original CI failures are
retained.

CORAL covers one bounded worker with fresh sessions and explicit carried files
and feedback. Persistent provider-session continuation, multi-worker cooperation
and heartbeat intervention are outside this pilot. ComBEE-GEPA is not tested.
Codex Native and Codex GEPA are not part of this eight-cell expansion. Existing
Pi/DSH Native and GEPA evidence remains separately source-bound.

API billing totals remain unknown, not zero. CI image build time and failed
attempts are additional to measured cell execution time. The original Codex
production build took 8,971 seconds (149m31s), independently of the live method
steps. The Meta recovery downloads that producer's artifact on its CI runner,
verifies retained binaries against accepted-cell SHA-256/size manifests, and
reconstructs a fixed-runtime image with exactly matching pinned source bytes.
Its new image identity and producer links are recorded; it is not a source-evolution
build environment. Recovery image preparation took 198 seconds (3m18s), including
artifact retrieval and validation. AWS was not started.
Codex successful archives compact repeated runtime binaries into size/SHA-256
manifests after the completed-resume check. Such archives are evidence bundles,
not immediately restartable working directories.

See [setup and commands](OTHER_METHODS.md) for the executable recipe, role
configuration, model configuration, task construction and bounded controls.

For an exact recorded setup, check out that cell's full `source_head` from the
structured evidence before running its recipe. DSH ACE/CORAL used the earlier
inner permission policy; ACE used formatting task version 1. Recovery ComBEE/Meta
used the explicit Docker-contained inner permission policy, and ComBEE used
formatting task version 2. Running
the latest helper is a new experiment with newly recorded identities, not a replay
of the old setup or a guarantee of the same stochastic model output.

The literal-prompt fix at `814eb98` passed 126 related local tests with one
environment skip. Its complete CI (`41007306807`), Harbor (`41007306765`) and
Native source (`41007306762`) passed: Python 3.10/3.11/3.13 each 1825 passed /
33 skipped; 3.12 1834/24; 3.14 1824/34. The later fixed-runtime recovery helper
has seven integrity/provenance regressions and its affected suite passed 48 tests.
Ruff, workflow YAML/shell parsing and diff checks passed. Runtime tree
`febc347a14aa6b23cc02581942d4fc6ad8dbc258` is unchanged between the delimiter
fix and recovery-helper commits; this does not equate their separate workflows.

The final Meta package-layout clarification changes the proposer prompt at
`678ddaef698d6cbabb14f04e9d64790b2e826de5` (runtime tree
`44c684f3834933da0c28aa9489af1d0e228e6a54`). It is distinct from the earlier
passed-CI runtime. Related method/pilot regressions passed 32 tests / 2 skips;
after final wording refinement, preflight/pilot checks passed 17 tests. Scoring,
loader behavior and cycle limits are unchanged. Full CI for this source must
be reported separately, and superseded cancelled runs are not passes.

Method papers and upstream repositories are collected in
[Method references](../../../docs/METHOD_REFERENCES.md).

At report assembly, CI `41011209507`, Harbor `41011209510` and Native source
`41011209534` for the final prompt source were still running. Their earlier
versions' passes do not substitute for these checks. The eight live-cell
acceptances above are complete; this report is not a release-readiness claim.
