# Proteus 1.0 release preparation: review closure and validation

This records the October 28, 2026 review of `e1c4d0e` and the repair in
[`3e10b16`](https://github.com/proteus-evolve/Proteus/commit/3e10b16f03752d99bdf56a06996c2fe675f18898).
[PR #106](https://github.com/proteus-evolve/Proteus/pull/106) targets `dev/1.0.0`.
It does not publish 1.0, change the package version or restart historical studies.
The review's baseline results are preserved as baseline evidence, not relabeled as
validation of the repair.

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
change those components. Workloads were measured independently, not as a new ACE
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

Local validation covers all **1,778 collected cases: 1,695 passed and 83 skipped**
after the explicit rechecks below. The [per-case outcome record](validation/v1-20261028/test-outcomes.json)
checks its node-ID set against the final collection. It retains the [original broad
sweep](validation/v1-20261028/broad-sweep.json) and [six initial failures](validation/v1-20261028/earlier-lock-failures.log):
one GEPA resume and five knowledge subprocess cases encountered source/schema locks
that changed while Native metadata was being repaired. All six passed with source
held fixed. An additional 90-case Native/export check and the final 44-pass/3-skip
fault/configuration check overlap the sweep; their totals are not added as new cases.
The skips require optional pinned reference sources or live Docker; the GitHub
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

Release remains blocked until the current preparation evidence is complete and the
remaining research acceptance is either satisfied or explicitly scoped by maintainers.
After that, main integration, version/tag creation and the four successful tag-triggered
workflows remain mandatory under [RELEASING.md](RELEASING.md). A branch gate is not a
release-tag gate. No release or PyPI publication is implied by this document.
