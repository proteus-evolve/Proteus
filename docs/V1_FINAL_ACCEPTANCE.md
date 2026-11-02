# Proteus 1.0.0 final preparation acceptance

This is the November 1 release-preparation record, not an announcement or a
published tag. It reconciles the development plan against observable, source-bound
evidence. Historical experiments retain their original source and protocol; current
mechanism checks do not turn them into current-source or paper-matched replications.

The reviewed development base is `dc6ea2f14f391989c3944df6fc873d3786be0a99`.
The first preparation commit is `9807a4133ee06ef48dea54fe8c4b5fba482bd74e`:
its production tree is `0e73363387123ad067df3cfeff684af7d913fed2`. Relative to
the accepted base, production changes are the package version (`1.0.0`) and the
API module's freeze docstring. The Combee acceptance runner/tests are additional
validation code. Subsequent validation-only fixes retain that production tree.

## Requirement disposition

The table describes the accepted **supported contracts** and the remaining
qualification boundaries. It does not convert unknown usage into complete billing,
require backends to expose internals they do not own, or silently relax research
requirements. The older implementation-plan checkboxes are implementation history;
this record supplies their current per-requirement disposition.

| Requirement | Current evidence / supported behavior | Remaining boundary |
| --- | --- | --- |
| I01 data contracts | Strict public records, schemas, identity validation; 100 exports and 64 contract schema identities frozen. | Incompatible records require explicit versions; no automatic migration. |
| I02 methods and stages | Actual Native compatibility path and typed method/stage plugins; external stage template runs from installed distributions. Final owned adapter input is recorded. | Aki internal prompts and coding-harness internal provider messages remain opaque; the API does not promise their capture. |
| I03 role runtimes | Capability-checked roles, separate optimizer/subject/grader actors; 16 bounded Pi/DSH/Codex live pilot cells. | Not every method × harness × role combination; persistent native session and fresh fallback stay distinct. |
| I04 artifacts | Immutable file/text/record artifacts, checked deltas/materialization, preserved failed candidates and lineage; source/image identity gates. | Executability and quality require the declared validator/evaluator. |
| I05 evaluation | Pinned tasks/splits/state, per-task outcomes, private grader isolation, before-learning prediction ordering; recovered parallel grader provenance verified. | Missing/infrastructure-failed scores remain unavailable, not zero. |
| I06 environments | Pinned Harbor runtime, independent verifier, source build/cache witness, bounded cleanup and recovery. | Only declared providers/lifecycles/resources are supported; final preparation Harbor check runs on the main PR. |
| I07 search | Parent/archive/final policies, seeded decisions, exact candidate lineage; installed external parent selector changes the actual child. | Selection sees only eligible declared evidence. |
| I08 knowledge | Versioned sourced shared knowledge, CAS publication, role-scoped evidence; source/worker provenance preserved. | CORAL's real-worker delivery is a separate qualification below. |
| I09 usage and recovery | Managed calls reserve/settle once; native receipts retain reported tokens and known subtotals; uncertainty retains reservations. Settled results recover without reissue. | Opaque model-call/cost totals remain unknown. Unsupported finite caps fail preflight. Lost external results stop for reconciliation, rather than promising automatic recovery. See the detailed disposition below. |
| I10 parallelism | Shared concurrency caps, ordered joins, copy-aware grouping, reducer lineage, production dynamic controller. | Controlled clock observations in the common-trace check are not model speedup. |
| I11 persistent workers | Managed generations, real worker processes, knowledge/evaluation gateway and events; historical fresh fallback supported. | Current real two-worker sharing/intervention/interruption qualification is pending. |
| I12 recipes | Immutable resolved manifests, exact source/dependency/plugin locks, profile and deviation metadata; changed conditions cannot masquerade as resume. | Frozen historical builders and current catalog builders have separate validation labels. |
| I13 researcher entrypoints | Five external plugin templates, six dependency-free catalog examples, export and study comparison execute outside the checkout in both distributions. | Optional GEPA reference and real runtimes have their own CI/gates. |
| I14 output | Historical 15 profiles, baseline/selected/last separation, task tables, cost bounds and missing values; online Combee elapsed table accepted. Common-trace comparison adds controlled mechanism evidence. | CORAL real sharing/intervention/recovery qualification is pending. Offline historical continuous timing stays unknown; no cross-method causal ranking. |
| I15 release | API/schema/lock freeze, unreleased 1.0.0 metadata/notes, wheel+sdist build and twine; 22 installed checks, 261 Python files byte-matched. | Complete prepare full smoke and main PR CI/Harbor; later exact-tag gates and publication require separate authorization. |

### I09: disposition of the three earlier unchecked items

1. **Actual role usage aggregation:** complete for managed operations at their
   declared observable boundary, with native receipts and independently validated
   known subtotals. An opaque call count or monetary cost remains `null`; this is
   an explicit coverage limit, not a successful complete-accounting claim.
   [Protocol](V1_NATIVE_INPUTS.md#reported-native-usage),
   [receipt regressions](../tests/test_native_usage.py), and
   [installed readback evidence](validation/v1-20261101/installed-distributions.json).
2. **Concurrent reservation and settlement:** supported run/role scopes share one
   ledger. Built-in minimal and single-request HTTP Native recipes additionally
   enforce finite call caps. Backends that cannot bound calls/cost/time reject those
   finite caps before dispatch. Unknown actual usage retains the corresponding
   reserved amount in budget accounting (or remains unknown if unbounded); a
   terminal operation may settle and release its concurrency slot. An uncertain
   external outcome remains unsettled with its reservation/slot intact until
   reconciliation. Unknown usage is never silently refunded or converted to zero.
   [Finite Native budgets](V1_NATIVE_INPUTS.md#finite-native-recipe-model-call-budgets),
   [ledger behavior](../tests/test_v1_ledger.py), [Native budget checks](../tests/test_native_budget.py),
   and [installed package evidence](validation/v1-20261101/installed-distributions.json).
3. **Recovery:** committed operations and original settled receipts are reused.
   Harbor can reconcile its independently completed worker; an active worker or
   lost/unconfirmed external result remains unresolved. Automatic resubmission of
   uncertain work is deliberately unsupported. Source/config/lock changes require
   a new run. Docker cleanup and SQLite hot-journal faults have dedicated retained
   before/after evidence. [Recovery protocol](V1_RECOVERY.md),
   [journal tests](../tests/test_v1_recovery.py),
   [release-fault tests](../tests/test_v1_release_recovery.py), and
   [retained hot-journal evidence](validation/v1-20261028/hot-journal-recheck.json).

These are the documented capability boundaries in `V1_NATIVE_INPUTS.md`,
`V1_RECOVERY.md` and the prior release acceptance record. The final live gates
validate supported adapters; they do not claim access to hidden provider billing.

## Method acceptance

| Family | Accepted result / mechanism | Claim boundary |
| --- | --- | --- |
| GEPA | Frozen AIME seed 0, 45/45/30, H0 23/30 → selected 24/30; pinned single mutation/reference mechanism. | Adapted, no merge/cache; not original-model parity or significance. |
| ACE | Formula 500/300/200; offline 141 → selected 173/200, online 141 → pre-learning stream 163/200. | Selected is not last; online is not final-playbook held-out accuracy. |
| Meta-Harness | Three historical MCE domains, 450 learn callbacks, 260 visible predictions over 130 distinct tasks, 412 private tasks; JSON proposer protocol. | Current Pi/DSH/Codex coding proposers have bounded integration pilots, not a full coding-search benchmark claim. That stronger claim would require its own audited full run. |
| Combee | Eight historical Formula conditions; four online quality/API-upper/study-elapsed rows. Additional common frozen trace across four actual ACE variants. | Model-free common trace checks mechanism only; no isolated speedup, paper parity or multi-seed claim. |
| CORAL | Historical one worker ×20 versus two ×10, own measured feedback, publications and fresh-fallback generations. | Historical cross-worker delivery/intervention/interruption chains were not recovered locally. New qualification must remain separately labeled and is pending. |
| Native | Compatibility transaction engine plus common Native evolution with last-valid activation and independent external evaluation; real role pilots and pinned source gates. | No outer-score selection in the common evolution profile; no all-role/runtime matrix or native-session multiworker claim. |

## Preparation evidence

- Public API: frozen export names, defining modules and schema hashes in
  `validation/v1-20261101/api-freeze.json`; service behavior and compatibility rules
  remain documented separately.
- Common-trace validation: the production four-arm recipe replays eleven original
  samples; fixed/naive batches are 3/3/3/2 and dynamic is 1/2/4/4. All arms share
  H0, seed, original task/prediction/feedback/reflection bytes. Copy occurrences,
  curation/reducer calls and resulting lineage remain separate. Readback verifies
  source/config/lock/dataset/actual request identities, not only task labels.
- Distribution source: built from clean `9807a41`, then byte-checked against the
  unchanged production tree. Both wheel and sdist contain exactly 261 matching
  Python source files. Both pass installed template/catalog/Native/role/accounting/
  export/study/parallel/workflow checks outside the checkout (22 checks including
  metadata/extras/API). Initial missing local `build` dependency is retained as a
  setup failure, followed by a fresh build-tool environment and success.
- Prepare Native source workflow: [run 41390451600](https://github.com/proteus-evolve/Proteus/actions/runs/41390451600)
  passed both required pinned Pi and DSH source jobs with `live_role_pilot=false`.
  The [artifact readback](validation/v1-20261101/native-source/readback.json) verifies
  source/image bindings, actual read/write witnesses, changed compiled activation,
  broken-source rejection and three distinct controller processes for repair/resume.
  Provider responses in this gate are deterministic fixtures. The optional Codex
  role pilot was disabled by input; no required source job was skipped. The
  [independent readback](validation/v1-20261101/native-source/independent-readback.json)
  rehashed both original ZIPs and passed all four source/repair verifiers using
  reconstructed archived Git checkpoints, without new Docker or provider dispatch.
- Prepare complete release-smoke: run `41390453875`, same `9807a41` source;
  required offline/minimal/LLM/Pi/DSH/Codex outcomes remain pending.

The [independent review](validation/v1-20261101/independent-review.json) accepts
R04 common-trace closure, the API/package evidence and the scoped I09 disposition.
The reviewer found a real P2 in the first trace reader: source/config/task metadata
could drift while `--verify` still passed. Original failing probes and the first
9807a41 run are retained. Follow-up validation commits bind these identities and
add negative regressions, including unavailable Git objects in a shallow checkout.
No historical experiment or production optimization algorithm was changed to make
this validation pass.

## Main PR and publication

Open the PR into `main` when the three preparation items and their review are
complete. CI/Harbor are triggered by that PR; their checks must pass before merge.
Keep the PR reviewable and unmerged until the user authorizes acceptance.

A separately authorized annotated release tag must point to the accepted main
commit. CI, Harbor, Native-source and full release-smoke must each have one complete
successful workflow on that **exact tag/SHA**, without combining successful jobs
from different failed runs. Branch checks, identical production trees, installed
preparation packages and historical successes do not replace that rule. GitHub
Release/PyPI publication and the public announcement remain separate actions.
