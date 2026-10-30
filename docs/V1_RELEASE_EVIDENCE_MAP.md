# 1.0 acceptance evidence and remaining decisions

This October 30, 2026 mapping reconciles the implementation plan with existing
results. It does not change the plan's mandatory scope, frozen experiments,
catalog validation labels or package version. The reviewed development merge is
`3518095dab1e6223bb40d95237e11a3d8e11a0c3`; its runtime tree is
`f9a0040634c388483f66882cf4a45dddc9d8636b`. Historical model runs retain their own
[source/configuration bindings](V1_EXPERIMENT_ACCEPTANCE.md).
The subsequent review of merge `58910e1181d4e7fc0b4a91d9e6a5317f71dd6565`
confirmed that runtime is unchanged and supplied the online Combee supplement
below. The earlier local readback remains preserved separately.

“Accepted historical profile” below means the specified adapted experiment has
audited results. It does not mean paper parity, current-source benchmark validation,
or completion of all release requirements. The independent review found no new
engineering blocker on this merge; **1.0 publication remains unapproved**.

## Requirement-by-requirement disposition

| Requirement | Evidence that can be accepted now | Still open / permitted claim |
| --- | --- | --- |
| I03: real runtimes and role configuration | Real-model Pi/DSH Native and GEPA cycles (4 cells), additional Pi roles (4), and DSH/Codex ACE, Combee-ACE, Meta and bounded CORAL roles (8). See the [three pilot reports](V1_HARNESS_ROLES.md#capability-and-evidence-matrix). | Accept these 16 bounded cells. No all-method/all-harness matrix, Codex Native/GEPA, Combee-GEPA live pilot, or native-session multiworker claim follows. |
| R01: GEPA | [AIME seed 0](../recipes/gepa/aime/README.md): 45 train / 45 validation / 30 private, budget 500 metric calls, minibatch 3; H0 **23/30**, selected **24/30**. Pinned upstream mechanism, recovery and reference checks are mapped separately. | Historical single-mutation/full-validation/no-merge/no-cache adapted profile accepted. No original-model performance-parity or single-seed significance claim. No full rerun is required merely because the release SHA changed. |
| R02: ACE | [Formula](../recipes/ace/formula/README.md): 500/300/200, one epoch, three refinement rounds. Offline **141 → 173/200**, selected after 200 training examples; online **141 → 163/200** on predictions made before each learning window. | Accept these historical Formula profiles. Offline selected is not last; online stream is not final-playbook held-out accuracy. FiNER/AppWorld and unimplemented delta extensions are outside validated scope. |
| R03: Meta-Harness | [Three MCE domains](../recipes/meta-harness/mce/README.md): 450 learning callbacks and 260 visible predictions per system over 130 distinct validation tasks (30 USPTO + 50 Symptom2Disease + 50 LawBench, each evaluated twice); 412 private tasks per selected system. Archived proposer makes one JSON call producing three programs. Current Pi/DSH/Codex coding proposers have minimal live-cycle evidence. | Accept the historical JSON-proposer result only for that adapted profile. Current coding-search benchmark acceptance is pending; claiming it requires a complete audited declared-domain run. There is no paper-length-search or improvement threshold requirement. |
| R04: Combee | Both ACE/GEPA combinations have mechanism checks. The [eight Formula conditions](../recipes/combee/formula/README.md) supply historical quality and API cost upper bounds; the four online conditions also have study-level elapsed receipts, mapped below. | Online observation availability is closed. Review the fixed-trace mechanism coverage and declared adapted comparison against R04 before marking the whole requirement complete. Offline continuous-time gaps do not imply missing online elapsed or automatically require a new paid run. Neither positive speedup nor paper-matched numbers are acceptance prerequisites; any scope deferral still needs an explicit maintainer decision. |
| R05 / I11: CORAL | [26-circle public task](../recipes/coral/circle-packing/README.md): one worker × 20 proposals versus two × 10, one evaluation worker; actual model requests, submitted code, own-worker measured feedback, publications and fresh-fallback generations are recorded. | Accept the two historical optimization arms and their declared fresh-context/persistent-workspace boundary. Actual cross-worker prompt use, applied interventions and interrupted recovery still require the evidence closure below. One-worker harness pilots do not establish current native-session multiworker support. |
| R00: Native | Historical Pi integration, current activation/repair/recovery mechanisms, pinned Pi/DSH source gates and bounded live role pilots are distinct evidence. | Final preparation and exact-tag gates must bind the declared adapter inputs, activation, invalid-candidate repair and interruption/resume to the release source. Historical exit-137 phases and completed resume do not replace them. |
| I14: comparable output | Frozen task metrics, selection, costs and failures are available, including all four Combee online study elapsed observations; offline continuous timing remains unknown. Current export/study mechanisms preserve missing values. | Review the declared Combee comparison conditions and CORAL sharing/intervention evidence. Existing observations do not establish isolated speedup or controlled cross-method ranking. |

The five general benchmark catalog entries can still say `model_results_pending`:
that label belongs to their current builders, not to the separately archived
historical profiles. This mapping does not promote those builders automatically.

## Combee: online observations, offline gaps and acceptance scope

The post-merge review recovered online elapsed records omitted from the first
mapping. The [online receipt readback](validation/v1-20261030/release-scope/combee-online-evidence.json)
binds all four observations to their frozen configuration, lock and historical
source. These scores are **pre-learning stream accuracy** over 200 tasks; each arm
has its own measured empty-playbook baseline. They are not final-playbook held-out
accuracy. API costs are recorded upper bounds, not invoices; canonical cost remains
unknown.

| Historical online condition | H0 → stream correct / 200 | API calls | Known API upper USD | Study elapsed seconds | Core |
| --- | ---: | ---: | ---: | ---: | --- |
| ACE | 139 → 160 | 1,255 | 1.737708312 | 16,636.682 | `d0a9dc848e89` |
| Naive parallel | 137 → 157 | 1,071 | 0.705109362 | 10,114.331 | `21205d1dcdcf` |
| Combee fixed | 140 → 159 | 1,116 | 0.771608514 | 10,808.138 | `21205d1dcdcf` |
| Combee dynamic | 137 → 162 | 1,115 | 0.742123308 | 10,409.919 | `21205d1dcdcf` |

All four use seed 0, the same H0 artifact and the same generator/reflector/curator
settings: `deepseek-flash`, temperature 0, thinking disabled, max tokens 4096,
fresh sessions. Their frozen run ceilings are 2,400 model calls and 1,400 evaluations;
ACE concurrency is 1, the parallel arms 8. Online windows contain at most 15 tasks,
with three refinement rounds; fixed/naive batching is configured at 50 and respects
the window boundary. Dynamic batching uses bounds 1–100 and slope threshold 0.016.
The full per-arm options and budgets remain in the sanitized readback.

The saved auditor sources map both `wall_seconds` and `engine_wall_seconds` to
the original report's `runs[].elapsed.wall_seconds`, not sums of actor durations.
This measurement includes baseline, learning, evaluation and persistence within
study execution, excluding preparation and report auditing; it is not paper
training latency. The ACE full original report was reopened locally, hash matched,
and has `interrupted=false`. For the other three arms, this readback checks the
historical independent receipts and auditor source; their complete original
reports were **not** reopened. The auditor files were inspected as text/AST and
never executed. Their original report hashes remain recorded.

Shared-host contention and the different ACE versus parallel runtime cores limit
isolated speedup attribution. The archived equality of four method/operator file
hashes is a scientific-source witness, not evidence of identical runtime I/O or
performance. The table is a complete single-seed online observation table with
these limitations, not a causal speedup or significance result.

### Offline timing remains a separate gap

The frozen [results](../recipes/experiments/single-seed-20261020/results.json)
contain the following offline observations. All private accuracy denominators are
200; cost is a known upper bound from recorded API usage, not an invoice or a filled
canonical cost. All four `continuous_run_seconds` fields are `null`.

| Historical condition | H0 → selected correct | API calls | Known API upper USD | Controller seconds across attempts | Core |
| --- | ---: | ---: | ---: | ---: | --- |
| ACE | 136 → 174 | 2,784 | 5.503515372 | 74,777.108 | `d0a9dc848e89` |
| Naive parallel | 139 → 168 | 2,344 | 1.497942420 | 37,958.899 | `21205d1dcdcf` |
| Combee fixed | 139 → 170 | 2,404 | 1.632558642 | 44,556.233 | `21205d1dcdcf` |
| Combee dynamic | 138 → 174 | 2,695 | 2.975843886 | 76,016.384 | `21205d1dcdcf` |

These offline controller intervals include recovery CPU and come from a shared
host and different historical cores. They are not uninterrupted runtime or summed
worker latency, and should not be converted into a speedup chart. Missing time is
not zero. This offline gap does not erase the online elapsed observations above.

### Fixed-trace and comparison evidence mapping

The following existing checks identify mechanism coverage; they are fixtures, not
new quality/time observations. This documentation change does not rerun them.

| Plan element | Existing evidence and its boundary |
| --- | --- |
| Sequential ACE order/update semantics | [ACE method tests](../tests/test_v1_ace_method.py), `test_full_loop_matches_actual_upstream`: fixed responses and the actual pinned upstream control flow. |
| Naive versus Combee on common reflections | [Baseline tests](../tests/test_v1_combee_baselines.py): original sample order, one curator pass without reducer, identical reflector prompts in naive versus Combee, distinct reducer calls, completed-resume reuse. |
| Fixed Combee aggregation and learning | [Aggregation tests](../tests/test_v1_combee_aggregation.py) compare ACE/GEPA prompts, outputs, grouping/RNG and occurrence provenance with pinned upstream; [Combee-ACE method tests](../tests/test_v1_combee_ace_method.py) cover three phases, window boundaries and reference controller behavior. |
| Dynamic scheduling | [Dynamic tests](../tests/test_v1_dynamic_batching.py) cover recorded ordinary-batch timing, pinned plateau/sampler correspondence, online window caps, task coverage and recovery without extra learning. Simulated delays are not measured model speedup. |
| Four-arm protocol and reporting | [Formula study tests](../tests/test_v1_combee_formula.py), `test_all_arms_share_protocol_and_budgets_with_complete_private_results`, plus timing/recovery checks; these use a local HTTP fixture and do not make the historical runtime cores identical. |

This maps the existing component checks; it does not claim that all four methods
were replayed on one identical frozen trace sequence by a single test. The R04
fixed-trace checkbox and acceptance of the explicitly declared historical
comparison still need review. The requirement is at least one complete
quality/cost/elapsed comparison with documented conditions, not a positive speedup
or reproduction of paper numbers. The online table can support that review without
additional inference. Do not schedule a paid rerun solely because the offline
continuous-time fields are null. Any proven missing evidence or scope decision
must be stated specifically; the existing requirements are not waived.

## CORAL: completed arms and the exact evidence gap

The [sanitized readback](validation/v1-20261030/release-scope/evidence.json) records
hashes of the existing local acceptance receipts and a read-only database schema
inspection. It rechecks those receipts, not the original remote run. Raw requests,
private responses and credentials are not included in this documentation change.

| Evidence | One worker | Two workers | Interpretation |
| --- | --- | --- | --- |
| Proposals / physical API calls | 20 / 20 | 10 + 10 / 20 | Real optimization work already completed. |
| Succeeded / timed out proposals | 12 / 8 | 11 / 9 | Failed or missing outcomes are retained, not replaced by zero-quality observations. |
| H0 → selected same-task normalized score | 0.364102 → 0.928722 | 0.364102 → 0.900208 | Original public task; fresh confirmation seed is not a held-out task. |
| Knowledge publications | 20 | 10 + 10 | Publication does not prove another worker consumed it. |
| Worker generations | 12 | 6 + 7 | Persistent workspace with fresh model contexts. |
| Paused / fresh-fallback generations | 11 / 11 | (5 + 6) / (5 + 6) | Actual pause and fallback records; not native model-session continuation. |
| Reported restarts | 0 | 0 + 0 | No real crash-restart witness is established by these counts. |

Both arms' recorded prompts include their own previous measured feedback (up to
five earlier proposals). The two-worker receipt binds all 20 paid responses to
submitted source. The archived worker code also implements shared-knowledge reads
and saves prompt rendering/source references. That implementation is evidence of
the protocol, not evidence that a particular cross-worker revision was used.

The local final receipts do not include later shared-reference rendering or
intervention-to-request bindings. The local relay database keeps request hashes
and compressed responses, but no request bodies. The inspected first single-worker
request has no shared references, as expected; it says nothing about later requests.
Therefore the local audit cannot establish or refute later sharing or intervention
use. The missing evidence may still exist in the original worker workspaces.

The expanded local search found nonempty shared references in an older v4.1
single-worker record, an intervention incident from an aborted v1 run, and an unpaid
delayed-response fixture. Those belong to different profiles or synthetic checks;
they cannot be combined into evidence of final v5.1 two-worker sharing or successful
live crash/restart. The accepted v5.1 optimization arms remain unchanged.

To close I11/R05 without unnecessarily rerunning optimization:

1. Recover the original worker request, rendering and intent receipts. Match their
   hashes to the paid dispatch receipts, and match rendered shared source references
   to published revisions authored by the *other* worker. Report availability,
   actual prompt inclusion and downstream response separately; none proves a causal
   quality benefit by itself.
2. Bind each claimed intervention decision to its worker generation/launch input
   and actual subsequent request. Configuring a heartbeat or recording a pause is
   insufficient proof that the requested intervention reached the model.
3. Bind the declared interrupted-recovery boundary to before/after worker,
   evaluation and budget receipts, verifying no duplicate dispatch/settlement.
   Current fault-injection fixtures and historical completed-resume checks retain
   their own scope; do not relabel them as a historical live crash/restart.

If those original receipts are unavailable or do not cover a promised behavior,
record the proven gap, then obtain authorization for targeted additional evidence
or an explicit release-scope decision. “Experimental” is not a waiver of the
existing sharing, heartbeat and recovery acceptance requirements.

## Release preparation boundary

The independent review verified 100 unique resolving exports in
`proteus.api.v1.__all__`. The proposed freeze covers these aliases, the
[documented service operations](V1_PUBLIC_API.md#service-contracts-used-by-extensions)
and [schema/lock compatibility](V1_COMPATIBILITY.md), not private attributes, SQL,
recovery journals, method internals or worker transports. Maintainer freeze and
final-version installed-distribution validation remain pending.

Earlier preparation commit `325ab5edb056f592209e23116e6ffabcb87ec9e8` did pass
[CI](https://github.com/proteus-evolve/Proteus/actions/runs/40836739047),
[Harbor](https://github.com/proteus-evolve/Proteus/actions/runs/40836739160),
[Native source](https://github.com/proteus-evolve/Proteus/actions/runs/40836739357)
and [full release-smoke](https://github.com/proteus-evolve/Proteus/actions/runs/40836737206).
Its runtime tree was `f98892e02a77b3130f2c6beed97e0ce78c791e8f`, different from
the current runtime. Those are genuine historical successes, not final-source or
release-tag qualification. Development-version wheel/sdist CI also passed at PR110
head; final-version distribution checks remain a separate step.

After research scope/evidence is resolved, follow [RELEASING.md](RELEASING.md):
final notes and version, build/twine and wheel/sdist checks outside the checkout,
preparation-branch Native-source and full live smoke, accepted main integration,
then authorized tag/publication. Each of CI, Harbor, Native-source and release-smoke
needs one complete successful run at the exact tag/SHA. Identical source trees and
successful earlier branch jobs do not satisfy those tag gates. Unknown external
usage stays unknown, with capability/budget limitations explicit.

No release-scope waiver, new paid experiment, AWS start, version change, tag or
publication is authorized by this mapping. Existing paused monitors stay paused.
