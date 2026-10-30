# Proteus 1.0 release preparation: review closure and validation

This records the October 28, 2026 review of `e1c4d0e` and the repair in
[`3e10b16`](https://github.com/proteus-evolve/Proteus/commit/3e10b16f03752d99bdf56a06996c2fe675f18898).
[PR #106](https://github.com/proteus-evolve/Proteus/pull/106) targets `dev/1.0.0`.
It does not publish 1.0, change the package version or restart historical studies.
The review's baseline results are preserved as baseline evidence, not relabeled as
validation of the repair.

## October 30 independent re-review and parallel grader repair

The independent re-review of merged `67aa54a9d01fbce8145189561cb6b510514ae658`
confirmed the original P1/configuration fixes, but found two additional P2 release
blockers. Both were independently reproduced on that merge before this repair.
The original [concurrency failures](validation/v1-20261030/parallel-graders/before-concurrency.json)
and [invalid-evidence resume](validation/v1-20261030/parallel-graders/before-recovery.json)
are retained; they are not benchmark outcomes.

| Finding | Repair | Targeted acceptance |
| --- | --- | --- |
| Parallel model grading ignored the judge's concurrency cap and `actor.parallel` capability. | After-prediction grading requires the capability at preflight; actual trial width also respects the grading role's cap. After-batch and hidden grading remain serial. | Judge cap 1 completes with peak 1; cap 2 permits peak 2 when the run cap permits; run cap 1 remains serial. Unsupported parallel actors are rejected before dispatch, while serial finalization remains supported. |
| Completed parallel result recovery did not verify private model-grading provenance. | Before joining, reuse `verify_grade` for private input/outcome hashes, role/task/candidate binding and native usage receipts. Invalid evidence raises `RecoveryRequired`, keeping the cohort recoverable. | Interrupt after all branch results but before join. Corrupt or remove private input, outcome or native usage evidence: repeated resumes reject without new calls or ledger changes. Restore the original bytes: resume and export succeed, and completed resume adds no calls. |

The [repair validation record](validation/v1-20261030/parallel-graders/validation.json)
contains **175 passed / 8 skipped** across disjoint affected cases, including
**14 new regression cases**. The eight skips require live Docker/Harbor; they are
not passes. The separate no-pytest runner passed all 145 checks. Ruff and whitespace
checks passed. This is targeted repair validation, not a new full-suite sweep or a
coverage percentage. Concurrency actors and HTTP responses are local fixtures;
no paid provider calls or AWS work occurred. The native-receipt tests explicitly
register a local parallel fixture rather than broadening the bundled LLM harness's
declared capabilities.

The first implementation correctly rejected damaged evidence but terminally
settled the cohort. Its [six failed recovery regressions](validation/v1-20261030/parallel-graders/after-tests.log)
are retained. Using `RecoveryRequired` at this boundary preserves the original
reservation and permits evidence reconciliation; the final regressions validate
both rejection and subsequent recovery.

**Release readiness remains unapproved.** This patch needs independent re-review
and checks against its own source. Earlier CI, Harbor, native-source and live-smoke
successes do not qualify this changed runtime or a future release tag. The
[experiment acceptance](V1_EXPERIMENT_ACCEPTANCE.md), final API/support scope,
version/release notes and all same-tag/SHA gates in [RELEASING.md](RELEASING.md)
remain separate requirements. Frozen experiments and their claims are unchanged.

## Findings and decisions

| Review finding | Confirmed and addressed | Current validation |
| --- | --- | --- |
| P1: unknown execution settled before reconciliation | Confirmed. Actor, subject and integrated verifier reservations survive `RecoveryRequired` and interruption. Ordinary terminal failures still preserve measured/unknown usage. Native input recording, harness actor wrapping, builds and stage maps follow the same uncertainty rule. | `tests/test_v1_release_recovery.py`: repeated serial/map resume without a second dispatch; subject reconciliation in serial/parallel; Native receipt and build interruption; existing code/workflow/gateway recovery suites. |
| P1: Native checkpoint publication window | Confirmed. Prepare the Git commit, persist the complete receipt, then publish HEAD. Resume rolls forward the receipt, including rejected-candidate selection/counters and repair pointers. | Fault injection before publication, after publication and after history replacement, for accepted and rejected candidates; original staged source-repair tests and native-source integration. |
| P1: unbounded Docker cleanup | Confirmed. Removal and pipe draining share a 10-second cleanup deadline. Keyboard interruption also cleans up. An unverified cleanup records a private marker and blocks that root until explicit reconciliation. | Deadline-aware substitute process tests, hung-pipe tests and timeout/Ctrl-C cases. Real daemon failure is not inferred from substitute tests; the real Docker/native gates validate supported normal operations. |
| P2: journal/ledger scale | Confirmed. Journal receipts now reference shared immutable JSON subtrees; object transactions commit before receipt publication. Ledger schema 2 maintains transactionally updated scope totals and an operation index. Schema-1 accounting migrates once; export independently audits current totals. | Snapshot integrity/aliasing and missing-object tests; ledger migration, concurrent admission, rollback, Decimal/unknown accounting tests; measured scale acceptance below. |
| P2: constructor/schema mismatch | Confirmed. Enforce maximum, exclusive maximum, maximum length and maximum item count alongside lower bounds. | Constructor/JSON-reader/JSON-Schema parity tests and the actual Combee slope-threshold counterexample. |
| P2: paired study seed mismatch | Confirmed. Validate execution and task-suite seed mappings within each paired replicate, independently of variation masks. Output retains the mapping. | Reject execution seeds 11 vs 999 and split seeds 42 vs 999; accept matching mappings where outer seed, execution seed and split seed differ. |
| P2: DSH model ownership | Confirmed interface ambiguity; preserve intentional legacy behavior. Add explicit framework/runtime ownership. Record owner/requested/applied model and leave unobserved resolution unknown. DSH receipts no longer claim an ignored model was applied. | Six legacy/framework/runtime × generic/workspace cases and existing actual adapter-input/settings-mount fixtures. |

`RecoveryRequired` is the runtime's explicit report that work may still be active.
Ordinary runtime exceptions report terminal failure; usage can still be unknown.
A custom executor must not report an unknown external outcome as an ordinary failure.
These contracts are conservative local recovery, not a provider-side exactly-once API.
An unknown actor/build without a supported reconciliation protocol remains blocked;
there is no automatic refund or forced redispatch.

## Compatibility and unused code

- Removed private, unreferenced `distance._sim` and Aki's write-only `_pending_root`.
- Removed the unused `EpisodeSpec.extra_env` field. Unsupported input now fails
  instead of appearing to configure the environment. Use documented sandbox options.
- Kept `_repair_feedback` as an explicit former-private compatibility helper during
  the development transition. It is outside the proposed stable API; removal is a
  separate compatibility decision.
- Kept upstream reference branches/parameters and dynamic callbacks: static
  reachability does not establish that they are dead. Source conformance remains required.
- `web/server.py` serves `/run` and `web/static/run.html`; these are retained as an
  experimental localhost/trusted-network backend. The public static site does not
  use it. The existing [web guide](../web/README.md) already states that boundary.
- Added an explicit package initializer for installed scaffold templates, addressing
  the setuptools package-discovery warning. Distribution smoke checks remain required.

Old source/configuration locks are not rewritten to make old runs resume under new
code. Existing full-snapshot journals and schema-1 ledger reports remain readable;
opening a ledger for execution transactionally creates its accounting index. A
legacy Native commit whose evaluator history was already lost has no new receipt
from which to recover; restore original evidence rather than inventing scores.
See [compatibility](V1_COMPATIBILITY.md) and [recovery](V1_RECOVERY.md).

## Scale acceptance

Run the committed offline benchmark outside the checkout:

```sh
python scripts/benchmark_recovery_scale.py \
  --out /tmp/proteus-recovery-scale \
  --examples 80 160 500 --ledger-entries 5000 --prompt-bytes 8192
```

Each example performs generator/reflector/curator managed stages and a checkpoint.
Prompts are unique synthetic inputs of approximately 8 KiB; actors are explicitly
model-free fixtures. The separate ledger workload reserves/starts/settles/checks
5,000 synthetic entries across run/role scopes, including unknown actual usage,
and independently scans the final totals. Synthetic model/cost units are accounting
test data, not API spend. The script deletes only its own work fixtures after saving
its JSON summary.

The [raw scale record](validation/v1-20261028/scale.json) records these results on
macOS/Apple Silicon with Python 3.12.13. Storage/recovery code is the committed
`3e10b16` implementation; the later Native reporting compatibility repair does not
change those components. The subsequent hot-journal repair below changes SQLite
connection handling, so these timings retain their original source binding. Its
[smaller capacity recheck](validation/v1-20261028/hot-journal-scale.json) is separate
from the original 500-example and 1,000-task measurements. Workloads were measured independently, not as a new ACE
or Meta-Harness quality experiment.

| Offline workload | Run time | Completed resume | Journal / database bytes |
| --- | ---: | ---: | ---: |
| 80 examples / 240 managed stages | 13.35 s | 0.88 s | 9,190,140 journal |
| 160 examples / 480 managed stages | 47.63 s | 1.82 s | 20,236,519 journal |
| 500 examples / 1,500 managed stages | 433.23 s | 6.47 s | 70,421,365 journal |
| 500 train / 300 validation / 200 hidden test tasks, 8 workers | 161.21 s | 2.61 s | 8,068,694 journal |
| 5,000 synthetic ledger entries, including unknown usage | 15.81 s | independent audit 1.28 s | 89,202,688 database |

All completed resumes returned the identical result with zero additional dispatches.
The evaluation workload settled 2,003 entries and both ledger workloads matched an
independent entry scan. Peak measured process RSS was 348 MB for the stage/ledger
run and 384 MB for the evaluation run. No provider calls occurred.

The qualified envelope is these bounded workloads: up to 1,500 managed stages,
1,000 evaluation tasks in 500/300/200 cohorts at 8 workers, and 5,000 ledger entries
with approximately 8 KiB inputs. Larger traces, more workers, more calls or different
filesystems require separate capacity validation; this is not an enforced global
limit or a claim that every frozen method's maximum workload was rerun. Journal
storage grew by about 2.2× when the 80-example workload doubled, while CPU traversal
still grows faster than linearly. The 7-minute 500-example run is retained as a
limitation, not described as constant-time checkpointing. Traversal and final export still grow with retained run state;
this implementation does not claim constant-time snapshots or unbounded histories.
It also does not model long provider latency, arbitrary trace sizes, remote filesystems
or multiple writers. Existing immutable operation receipts are not compacted away.

## Experiment and release evidence

The [15-profile correspondence](V1_EXPERIMENT_ACCEPTANCE.md) separates historical
results, runtime patches and current implementation checks. It preserves the
[original paper/repository citations](METHOD_REFERENCES.md). Current mechanism or
container checks do not replace matched-quality reruns, multiple seeds or a complete
reproduction of an original paper.

Before the hot-journal repair below, local validation covered all **1,778 collected cases: 1,695 passed and 83 skipped**
after the explicit rechecks below. The [per-case outcome record](validation/v1-20261028/test-outcomes.json)
checks its node-ID set against the final collection. It retains the [original broad
sweep](validation/v1-20261028/broad-sweep.json) and [six initial failures](validation/v1-20261028/earlier-lock-failures.log):
one GEPA resume and five knowledge subprocess cases encountered source/schema locks
that changed while Native metadata was being repaired. All six passed with source
held fixed. An additional 90-case Native/export check and the final 44-pass/3-skip
fault/configuration check overlap the sweep; their totals are not added as new cases.
The hot-journal repair adds two cases, bringing the current collection to **1,780**;
its affected-suite outcomes are recorded separately below, rather than relabeling
the earlier full sweep as a fresh run of the new source. The skips require optional pinned reference sources or live Docker; the GitHub
matrix fetches those references and the Harbor/native gates supply the real runtimes.
No test-coverage percentage was measured.

| Local gate | Evidence |
| --- | --- |
| Pinned Ruff 0.14.14; whitespace | Pass |
| No-pytest offline runner | 145 passed / 0 failed |
| Visualization JavaScript helpers | 8 passed / 0 failed |
| Frozen archive | 15 profiles / 16 recipes / 182 files verified; no payload changes |
| Wheel + sdist build and `twine check` | Pass; template package-discovery warning removed |
| Installed distributions outside checkout | [16 smoke checks passed](validation/v1-20261028/installed-smoke.json), covering templates, Native input/budget, accounting, export, studies and parallel/workflow execution |
| Storage/accounting scale | [Stage, split-cohort and ledger records](validation/v1-20261028/scale.json); no provider calls |

Native model ownership is execution/report metadata rather than a new field in
`proteus.native_summary@1`; a regression checks the exact legacy serialized value
and hash identity. Docker marker-write failure also remains `RecoveryRequired`
rather than downgrading to an ordinary terminal failure. Real timeout/Ctrl-C
container-removal checks are included in the Harbor workflow; daemon/persistence
failure injection remains a substitute-process test.

The current exact-commit branch CI, Harbor and native-source job links are visible
on [PR #106](https://github.com/proteus-evolve/Proteus/pull/106). Earlier successful
runs are preserved but do not replace the final head. The branch release-smoke run
and required tag gates remain separate acceptance items.

The first branch live-smoke attempt, [40828273530](https://github.com/proteus-evolve/Proteus/actions/runs/40828273530),
failed in its offline prerequisite: the workflow installed the development extras but
omitted the exact optional GEPA reference required by three dynamic-batching cases.
It recorded **3 failed / 1,587 passed / 188 skipped**; every downstream live job was
skipped, so no provider call was launched by that attempt. The
[original failure log](validation/v1-20261028/release-smoke-40828273530-failed.log)
is retained. The workflow now installs the same pinned GEPA commit as the main CI
matrix and sets `PROTEUS_REQUIRE_GEPA=1`; the
[isolated regression record](validation/v1-20261028/release-smoke-dependency-recheck.json)
separates the reproduced missing-dependency failures from the post-install checks.
This changes workflow preparation only; the Proteus runtime tree and frozen results
remain unchanged. A new complete live-smoke run is still required.

### Additional gate finding: interrupted SQLite snapshot transaction

The next branch attempt, [40831787772](https://github.com/proteus-evolve/Proteus/actions/runs/40831787772),
passed GEPA setup but failed the worker-knowledge capture hard-exit/resume case:
**1 failed / 1,686 passed / 91 skipped**. All downstream live jobs were skipped.
The [raw failure log](validation/v1-20261028/release-smoke-40831787772-failed.log)
is retained. The same head's full CI, Harbor and native-source workflows passed;
that does not waive the failed independent smoke prerequisite.

A new deterministic subprocess test forces uncommitted snapshot pages to disk and
exits without cleanup. Before the repair, it reproduces the same `attempt to write
a readonly database` error. SQLite must
[roll back a hot journal before reading](https://www.sqlite.org/lockingv3.html#dealing_with_hot_journals).
Snapshot loading now opens only an existing database in read/write mode and enables
[`query_only`](https://www.sqlite.org/pragma.html#pragma_query_only), permitting
SQLite's recovery while forbidding application data writes. It never creates an
empty replacement for a missing database. Save/load connections are explicitly
closed; save transactions still commit before journal receipt publication.

The new test verifies the last committed snapshot, discard of uncommitted objects,
database integrity, repeated load and a subsequent successful save. A second test
checks that a missing database stays missing. The
[repair acceptance record](validation/v1-20261028/hot-journal-recheck.json)
records the before/after fault check, affected worker/workflow/recovery suites,
installed distribution checks and exact wheel source identity. These checks are
**151 passed / 5 environment skips**, plus **16 installed wheel/sdist checks**,
145 no-pytest offline checks, build/twine and Ruff. All 255 wheel Python files match
the repaired checkout byte for byte. The first local package-check staging omitted
three fixture/helper inputs; its eight failures are retained in the record, and all
16 checks passed after copying the CI fixture inputs, without another runtime change.
These outcomes are separate from the earlier 1,778-case local sweep. New complete workflows on the
repaired head remain required; successful jobs from older runs are not pooled.

Release remains blocked until the current preparation evidence is complete and the
remaining research acceptance is either satisfied or explicitly scoped by maintainers.
After that, main integration, version/tag creation and the four successful tag-triggered
workflows remain mandatory under [RELEASING.md](RELEASING.md). A branch gate is not a
release-tag gate. No release or PyPI publication is implied by this document.
