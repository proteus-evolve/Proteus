# Live harness-role minimum-cycle acceptance — 2026-10-29

All four configured cells passed their minimum-cycle acceptance. The completion
workflow also finished successfully. This confirms the tested combinations and
artifact projections, not unrestricted method/harness interchangeability.

This is an engineering mechanism pilot using real coding harnesses and a real
DeepSeek model. The uppercase formatting tasks are deliberately tiny; these scores
are not benchmark performance, matched paper reproduction, or research evidence.

## Source and execution

- PR [#106](https://github.com/proteus-evolve/Proteus/pull/106) and
  [#107](https://github.com/proteus-evolve/Proteus/pull/107) were merged into
  `dev/1.0.0`; the resulting merge is
  `ae18ffa97f7b8a7a7eb20a0c5162a2dcdf2e1af8`.
- First pilot source: `4cca31567f5dc01be589dbf90ac3e3fe7de7a877`,
  [run 40968776199](https://github.com/proteus-evolve/Proteus/actions/runs/40968776199).
  The overall run failed; its accepted Pi Native cell and original failures are
  recorded separately below.
- Completion source: `83b0ee80a82ac45455a84f2dbb8d9c91e03963af`,
  [run 40970464201](https://github.com/proteus-evolve/Proteus/actions/runs/40970464201).
  It runs Pi GEPA and both DSH cells, without repeating accepted Pi Native.
- Both pilot commits have the identical `proteus/` runtime tree
  `02d11ce08a5ec00e95c96216d14b6870cd5e6f70`. Their recipes/checkers differ as
  described below. This is a cell-level experiment matrix, not a claim that one
  failed workflow passed or that separate runs satisfy a release gate.
- Requested/applied target/evolution model: `deepseek-v4-flash`; native model-selection
  receipts are retained. The adapters' provider-resolved model identity remains unreported.
  Harness sources: Pi
  `914cf1472e715297caa30db4b9535d534a9eb718`, DSH
  `99f6f02fecdb7dff40c3fbc9470f5907c29f74ca`. Official GEPA is pinned to
  `8b0ce6cd99a234f6b74daf37558a2ac0ce18f975`.

Runs use GitHub Linux runners, production Docker source images and the existing
DeepSeek credential. AWS and historical benchmark studies were not restarted.
The preceding Docker source gates use their existing local providers; the cells
below separately exercise the new bridges with the live provider.

## Setup and acceptance

The common subject runner is `proteus.harness_subject`, configured with the actual
Pi or DSH adapter. Every trial materializes an immutable candidate into a fresh
workspace, executes real tools, reads the resulting prediction file, and grades it
with the external `proteus.exact_match` evaluator. No live model reply or tool
action is mocked.

The seed policy lowercases input. The observed training word is `aLpHa`; GEPA also
has observed validation word `bEtA`. The final private word is `oMeGa`. References
are uppercase. Private test references do not enter optimization feedback. There
is no initial private-test evaluation, so no baseline private-test gain is claimed.

- **Native:** the same harness integration is both evolution actor and target.
  One observe/propose/act/reflect episode has an explicit engineering goal: change
  the policy to uppercase and add an executable CLI marker. Acceptance requires a
  valid build, the marker in the saved source, a different compiled CLI in later
  target execution, accurate external grades, and a completed resume with no new
  dispatch receipts. This is deliberate self-modification, not discovery quality.
- **GEPA:** official GEPA mutates `notes/policy.md`, projected over a frozen real
  harness tree. Its reflection actor is managed DeepSeek chat; the task subject is
  actual Pi/DSH, not a thin chat task runner. One reflection generates one proposed
  policy. A valid rejected proposal is mechanism evidence, not an improvement.
  The completion recipe clarifies that the replacement policy must contain the
  rule itself; it does not provide the uppercase solution or private answers.
- **DSH seed projection:** eight pinned internal documentation/fixture aliases are
  copied into ordinary files/directories because the artifact codec rejects
  symlinks. Contents are preserved and recorded in `seed-projection.json`; alias
  identity is not. Unknown, broken or escaping links fail. Executable source is
  retained. This is an adapted seed, not a byte-identical upstream checkout.
  It also changes the source hash from the image's pristine tree. DSH's boot
  wrapper therefore rebuilds in fresh trial workspaces; build outputs are not
  shared across these independent trials. This recipe has avoidable repeated
  compilation overhead and must not be used as a throughput measurement.

## Results

| Harness / method | Accepted source | Cycles | Candidates | Target rollouts | Observed score | Final private score | Resume new dispatch receipts | Status |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | --- |
| Pi / Native | `4cca315` | 1 episode | 2 | 3 | train 0 → 1 | 1 | 0 | Passed |
| Pi / GEPA | `83b0ee8` | 1 reflection | 2 | 5 | train/validation 0 → 1 | 1 | 0 | Passed |
| DSH / Native | `83b0ee8` | 1 episode | 2 | 3 | train 0 → 1 | 1 | 0 | Passed |
| DSH / GEPA | `83b0ee8` | 1 reflection | 2 | 5 | train/validation 0 → 1 | 1 | 0 | Passed |

Pi Native's actual outputs were `alpha`, `ALPHA`, `OMEGA`. Its compiled CLI changed
from `840d1e8e689ed9e4937bcb00b9a810e02a8567d9afb10a47097f11ca93ea1521`
to `1855fa3c676e596d5b47e1bce956a542e78d3160831b3a6053657b8edb6dc462`.
The saved evolved TypeScript entry contains `PROTEUS_ROLE_PILOT_ACTIVATED`.
Pi GEPA's actual outputs were `beta`, `alpha`, `ALPHA`, `BETA`, `OMEGA`.
DSH produced the same output sequence for each respective method. Its Native CLI
changed from `c0226687bb20f45c603ec6fe50f3de16d1c3510c3a803304ec575ef9bc366c62`
to `b181874dc436f9a95b0c9fe0419df72bd54ddac94919899835969183f5cb0204`.

End-to-end cell times, including preparation and completed-resume acceptance:

| Cell | Start (UTC) | Finish (UTC) | Elapsed |
| --- | --- | --- | --- |
| Pi Native | 22:51:55 | 22:54:22 | 2m 27s |
| Pi GEPA | 23:06:07 | 23:06:50 | 43s |
| DSH Native | 23:12:03 | 23:26:56 | 14m 53s |
| DSH GEPA | 23:26:56 | 23:35:02 | 8m 6s |

These exclude workflow image construction, the preceding source gates and artifact
upload. They include more work than ledger-instrumented operation time, and are
not pure model latency. DSH's repeated compilation is a limitation of this recipe.
The follow-up workflow separates Native and GEPA into independent visible steps,
with an aggregate check requiring every selected cell to succeed. The paid runs
above used the original combined step; this observability change does not relabel
their source or rerun them.

Compact source-bound records, artifact IDs, candidate hashes, changed paths,
model-selection/usage receipts and independently checked predictions are saved in
[`results.json`](results.json). Complete raw data remains in the linked Actions
artifacts; the local audit fetched selected records rather than downloading the
entire 882 MB DSH archive.

## Original failures and corrections

1. First Pi GEPA completed two mutations: the first was correctly rejected with
   score 0; the second was accepted with observed and private score 1. The pilot
   checker incorrectly required every changed candidate to improve, so the cell
   failed and never reached the resume check. Its threshold of four metric calls
   also allowed a second mutation following rejection. Preserve this as a failed
   pilot with a two-cycle algorithm trace, not a successful one-cycle result.
2. The completion recipe uses a three-metric-call stop threshold, one validation
   item and one-item parent/child minibatches. This stops after the first mutation
   whether accepted or rejected. An offline test using actual pinned GEPA verified
   the rejected case: three metric calls, one reflection, one iteration, rejected
   candidate. Candidate validation can add a metric call within an iteration; the
   threshold is not a hard request count. The explicit evaluation ceiling is six.
3. Both first DSH cells failed during seed import, before live inference, because
   the tracked source contained symlinks. The explicit projection described above
   corrects import. Projection, escape rejection, and harness-method regressions:
   27 passed; Ruff and whitespace checks passed.

Original logs, candidate records, predictions, grades and available usage remain
in their source-bound Actions artifacts. Native usage fields not reported by the
adapter remain unknown. Total API request counts and dollar costs are not treated
as zero; a completed-resume dispatch check does not establish zero total cost.

## Interpretation and remaining coverage

The accepted cells establish that a configured real harness can execute a changed
candidate under the tested method, receive independent feedback, and resume after
completion without new dispatch records. Full recipes and acceptance logic are in
[`run.py`](run.py); reproduction steps are in [`README.md`](README.md).

This pilot does not validate ACE, ComBEE, Meta-Harness, CORAL or Codex through these
bridges, arbitrary cross-harness evolution actors, model-based private grading,
long-run convergence, throughput or crash recovery during a live paid call. Native
source mutation and GEPA text-policy mutation are different artifact protocols;
configuration does not make every method/harness/artifact combination compatible.

At the measured completion source `83b0ee8`, full CI
([40970465594](https://github.com/proteus-evolve/Proteus/actions/runs/40970465594)),
Harbor integration
([40970465563](https://github.com/proteus-evolve/Proteus/actions/runs/40970465563))
and Native source integration
([40970465562](https://github.com/proteus-evolve/Proteus/actions/runs/40970465562))
all passed. These are source-bound engineering checks, not release/tag acceptance
or matched research reproduction.
