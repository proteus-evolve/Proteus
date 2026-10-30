# 1.0 acceptance evidence and remaining decisions

This October 30, 2026 mapping reconciles the implementation plan with existing
results. It does not change the plan's mandatory scope, frozen experiments,
catalog validation labels or package version. The reviewed development merge is
`3518095dab1e6223bb40d95237e11a3d8e11a0c3`; its runtime tree is
`f9a0040634c388483f66882cf4a45dddc9d8636b`. Historical model runs retain their own
[source/configuration bindings](V1_EXPERIMENT_ACCEPTANCE.md).

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
| R03: Meta-Harness | [Three MCE domains](../recipes/meta-harness/mce/README.md): 450 learning callbacks, 260 visible tasks, 412 private tasks per selected system. Archived proposer makes one JSON call producing three programs. Current Pi/DSH/Codex coding proposers have minimal live-cycle evidence. | Accept the historical JSON-proposer result only for that adapted profile. Current coding-search benchmark acceptance is pending; claiming it requires a complete audited declared-domain run. There is no paper-length-search or improvement threshold requirement. |
| R04: Combee | Both ACE/GEPA combinations have mechanism checks. The [eight Formula conditions](../recipes/combee/formula/README.md) supply historical quality and known API cost upper bounds. | **Mandatory complete quality/cost/elapsed tradeoff remains open.** Shared-host intervals and missing continuous elapsed coverage do not close it. At least one combination needs a complete comparison; the plan does not demand a separate full paid benchmark for both. Deferral requires an explicit maintainer scope revision. |
| R05 / I11: CORAL | [26-circle public task](../recipes/coral/circle-packing/README.md): one worker × 20 proposals versus two × 10, one evaluation worker; actual model requests, submitted code, own-worker measured feedback, publications and fresh-fallback generations are recorded. | Accept the two historical optimization arms and their declared fresh-context/persistent-workspace boundary. Actual cross-worker prompt use, applied interventions and interrupted recovery still require the evidence closure below. One-worker harness pilots do not establish current native-session multiworker support. |
| R00: Native | Historical Pi integration, current activation/repair/recovery mechanisms, pinned Pi/DSH source gates and bounded live role pilots are distinct evidence. | Final preparation and exact-tag gates must bind the declared adapter inputs, activation, invalid-candidate repair and interruption/resume to the release source. Historical exit-137 phases and completed resume do not replace them. |
| I14: comparable output | Frozen task metrics, selection, costs, failures and partial timing are available; current export/study mechanisms preserve missing values. | Combee elapsed comparison and CORAL sharing/intervention interpretation remain incomplete. These historical conditions do not constitute a controlled cross-method ranking. |

The five general benchmark catalog entries can still say `model_results_pending`:
that label belongs to their current builders, not to the separately archived
historical profiles. This mapping does not promote those builders automatically.

## Combee: why elapsed acceptance remains open

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

These controller intervals include recovery CPU and come from a shared host and
different historical cores. They are not uninterrupted runtime or summed worker
latency, and should not be converted into a speedup chart. A complete comparison
must declare compatible source/environment, workloads, budget and start/end scope,
account for recovery and profiling, and report quality, cost coverage and elapsed
coverage together. Missing time is not zero. New paid runs require separate
authorization; this document does not launch or defer them.

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

After research scope/evidence is resolved, follow [RELEASING.md](RELEASING.md):
final notes and version, build/twine and wheel/sdist checks outside the checkout,
preparation-branch Native-source and full live smoke, accepted main integration,
then authorized tag/publication. Each of CI, Harbor, Native-source and release-smoke
needs one complete successful run at the exact tag/SHA. Identical source trees and
successful earlier branch jobs do not satisfy those tag gates. Unknown external
usage stays unknown, with capability/budget limitations explicit.

No release-scope waiver, new paid experiment, AWS start, version change, tag or
publication is authorized by this mapping. Existing paused monitors stay paused.
