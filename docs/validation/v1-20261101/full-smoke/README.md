# Completed preparation release smoke

[Run 41398194611](https://github.com/proteus-evolve/Proteus/actions/runs/41398194611),
attempt 1, completed successfully on November 2, 2026 at 09:32:53 UTC. Its exact
source is `1adaa8ffa729771b80ad0a29b407fb014e64a14c`, with production tree
`0e73363387123ad067df3cfeff684af7d913fed2`. All six required jobs and their steps
passed in this one run; no required job was skipped and no other run supplies a cell.

| Job | Job ID | Verified outcome |
| --- | --- | --- |
| Offline | 125124037226 | Ruff passed; 1,842 tests passed / 91 skipped |
| Minimal | 125134436754 | Two episodes and independent benchmark execution completed |
| LLM | 125134436828 | Live HTTP adapter smoke passed |
| Pi | 125134436808 | Ordinary sweep and required edit/activation/rejection checks passed |
| DSH | 125134436868 | Ordinary sweep and required edit/activation/rejection checks passed |
| Codex | 125134436734 | Two episodes, edited binary activation, bad-source rejection/restoration passed |

The [independent acceptance](independent-final-smoke-acceptance.json) closes the
preparation-scoped installation, regression and preparation-complete items. It
rechecked the six original job logs, saved GitHub metadata and original diagnostic
ZIPs without models, Docker, workflow dispatches or changes to the originals.
[File integrity](original-file-integrity.json), [strict log readback](strict-log-readback.json)
and the coordinator's [earlier readback](full-smoke-readback.json) are retained.
The coordinator's earlier `independent_review: pending` is its original observation;
the subsequent independent acceptance above supersedes that pending status.

## Original artifacts and readback boundary

| Original artifact | GitHub artifact ID | Bytes | SHA-256 |
| --- | --- | --- | --- |
| `pi-original.zip` | 13122400389 | 225412 | `def4d918454097c6d5f94f620f220d3f8180e3e68e380b15ac04e223c978c4ca` |
| `dsh-original.zip` | 13122935125 | 695631 | `6553dc986244d73bc0971da510918227b3328c0f073a685153664ebd74b0e28a` |

The original ZIPs match the saved GitHub digests and contain 121/133 members,
respectively. All 120/132 exported files were verified. The reviewer recomputed
76 input/outcome pairs, including evaluator receipts; each required-edit run's
eight phase input/outcome/activation links bind the pinned boot script and expected
source hash. Each harness has six episodes: two ordinary arms × two episodes,
plus one required-edit arm × two episodes. The projected entry file changes only
by the instructed marker insertion before episode 2.

The ZIPs are sanitized, bounded diagnostic copies, not complete resumable source
or Git snapshots. Full checkpoint source identity and compiler-error refusal are
established by the fixed-source strict live checker. Six missing checkpoint/pending
files and 20/24 opaque usage sources are explicitly indexed. Pi's 20 native phase
outcomes and 20 of DSH's 24 returned 137 under controller caps; these incomplete
usage streams remain incomplete. Passing the declared bounded smoke does not
establish complete model conversations, provider-call totals or USD billing.

Codex's original log SHA-256 is
`66fb2be01fa3fa3c0504c318e2e8b7c7d3463727d0ffeccc7ccb3e1045e121cc`.
Its strict checker confirms a source edit before the last episode, the final
episode using the validated binary pair for that edited checkpoint, planted
compile-error refusal and restoration. This workflow exports no Codex raw trace,
snapshot or binary publication archive; independent reconstruction of that full
state is not claimed. Its 18/27 normalized tool events are not provider requests.

## Timing and retained history

Codex image building took 6,930 seconds (1h55m30s); the release Cargo profile
reported 89m44s and the test profile 18m08s. The two-episode workflow step took
2,867 seconds (47m47s), followed by 357 seconds (5m57s) of strict checks. These are
observed step durations; there is no complete phase/model/network/build breakdown.
The [read-only performance investigation](codex-performance-readonly.json) finds
source-hash keyed binary publication reuse and no measured current cache defect.
New Cargo intermediates are not retained across different candidate hashes in
disposable verification containers; this is a potential cost, not a quantified
cause. Suggested instrumentation was not implemented or made a release condition.

The first failed full workflow, 41390453875, remains failed due to DSH's missing
source edit. Its later successful Codex job is retained separately in
[historical diagnostics](../first-smoke-codex/codex-diagnostic-readback.json).
The original path/diagnostic fixes and metadata-amplification failure remain in
[repair evidence](../dsh-smoke-repair/). The initial coordinator helper's mistaken
arm label and correction are retained as `.py.txt` and a failure log; they changed
only readback logic, never the original ZIPs or runtime.

The accepted Native-source inputs are unchanged from `9807a41`; full-smoke runtime,
workflow, environment and checker inputs are unchanged from `1adaa8f` to the
reviewed `c1401fc`. Final distributions retain the separately accepted `bad3c55`
identity after the README metadata update. Main PR CI/Harbor/review and future
exact-tag CI/Harbor/Native-source/full-smoke are separate requirements. This
preparation evidence authorizes neither merge nor publication.
