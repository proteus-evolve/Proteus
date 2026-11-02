# CORAL directed qualification evidence

This directory records a new, bounded current-source protocol qualification.
It does not replace the frozen one-worker and two-worker circle-packing studies.
The [recipe](../../../../recipes/validation/coral-directed/README.md) describes the
sequence, reproduction commands, result, and claim boundaries.

- Source: `cebc1a9e808a25e8c7ee0144f525138325008253`.
- Production tree: `0e73363387123ad067df3cfeff684af7d913fed2`.
- Live plan: `bded2fa9e62422e5d224d610b6e252264d847a5585bb7922bfc42c744e611dca`.
- [Fixture workflow 41394652342](https://github.com/proteus-evolve/Proteus/actions/runs/41394652342): successful, zero provider calls.
- [Single live workflow 41394999162](https://github.com/proteus-evolve/Proteus/actions/runs/41394999162): successful, exactly three completed provider requests; 1,141 input and 113 output tokens, no reported USD charge.

## Original archives and readback

| File | Contents / boundary |
| --- | --- |
| [fixture-41394652342.zip](fixture-41394652342.zip) | Original GitHub artifact, 510,390 bytes; SHA256 `5aeebbea5fcb5becd3f23355478bd2d64c339140713f87efe72e0c672fdf5a93`. |
| [live-41394999162.zip](live-41394999162.zip) | Original GitHub artifact, 1,523,952 bytes; SHA256 `13f28f94be0918f1e49604a3ce9536fed55bc8c1abaf88c293f923c812100eb4`. Includes separate fixture and live records from that single workflow. |
| [live-usage-and-acceptance.json](live-usage-and-acceptance.json) | Per-request token/transport readback; actual shared content, applied intervention, controller fault and no duplicate settlement. |
| [live-handoff.json](live-handoff.json) | Source-bound run, original local record paths and complete result handoff. |
| [live-artifact-author-readback.json](live-artifact-author-readback.json) | Author's saved-record audits of both exported runs; all original file bytes unchanged. |
| [report-path-hash-boundary.json](report-path-hash-boundary.json) | Derived native report identity includes its source path; relocating records changes that derived hash, not the original request evidence. |

Artifacts contain exact model request/response bytes and native records. The
export omits the expired broker permit and reconstructs the gateway database with
an empty credentials table while preserving its noncredential tables. These are
non-resumable audit copies, not backups for restarting work. Saved-record audit
makes no provider calls; it may create empty advisory lock files locally.

## Review and retained failures

The initial [P2 review](earlier-pre-live-review.json) found that a fixture could be
relabeled by changing its mode flag. The source/plan/transport binding was fixed;
[source review](source-0c749a7-review.json) and
[independent actual readback](p2-readback-0c749a7.json) retain the diagnosis and six
rejected tamper probes. The [wrapper review](independent-wrapper-review.json)
checks the explicit manual CI path. The [final pre-live acceptance](independent-prelive-acceptance.json)
binds one execution to the exact source/plan and three physical slots.
The [final independent paid-record acceptance](independent-live-acceptance.json)
closes the remaining I11/R05 delivery/intervention/controller-recovery chains and
the corresponding I14 reporting requirement. [Integrity and ledger readback](independent-live-integrity.json)
rechecks all archived files, actual HTTPS receipts and fifteen unique settlements.

The first direct dispatch of the new workflow returned HTTP 404 because GitHub
had not registered it on the default branch; [the attempt](initial-workflow-dispatch-404.json)
and [original error](initial-workflow-dispatch-404.stderr) are retained. No job or
provider call resulted. The existing CI workflow now has explicit manual CORAL
fixture/live entrypoints. Ordinary push/PR/tag CI retains its previous job bodies;
manual CORAL success does not count as a complete release CI gate.

The combined integration suite passed [37 tests](combined-qualification-tests.log),
and the unchanged publication gate passed [74 tests](publish-gate-recheck.log).
These are separate from the frozen-source 22-case qualification checks and must
not be pooled into a claim of additional independent experiments.

The live result establishes directed request delivery and controller-crash recovery.
It does not establish quality gains, historical v5.1 sharing, worker-crash recovery,
native model-session continuation, or simultaneous provider dispatches. The single
resume adds the declared final confirmation evaluation, while preserving all three
already settled evaluation identities and issuing no additional model requests.
