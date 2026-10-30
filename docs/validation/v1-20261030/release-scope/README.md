# October 30 release-scope receipt readback

[evidence.json](evidence.json) is a sanitized local readback produced by
[readback.py](readback.py). It supports the
[acceptance disposition](../../../V1_RELEASE_EVIDENCE_MAP.md), not a new experiment.

Inputs are the independent review receipt for merge `3518095`, the original local
CORAL v5.1 acceptance/audit receipts, the local relay SQLite database, and the frozen
archive in this repository. The two final CORAL receipt hashes are pinned in the
script. The database is opened with `mode=ro` only to inspect its schema. No remote
host, provider or candidate code is invoked. Original request/response bodies are
not added to the repository.

With the original local evidence mounted and the dev environment installed:

```sh
python docs/validation/v1-20261030/release-scope/readback.py \
  --receipts /path/to/aws-access-20261020 \
  --review /path/to/proteus-v1-review-3518095/release-review.json \
  --relay /path/to/private-bridge/relay-receipts.sqlite3 \
  --out /tmp/release-scope-readback.json
```

This checks source identity, receipt hashes, publication/submission/generation
counts, recorded own-feedback windows, missing Combee continuous timing, and the
100-name proposed API inventory. It does not independently reconstruct the original
remote run, authenticate an unprovided later request body, or establish actual
cross-worker use/intervention application from counters. The checked-out runtime
must match the reviewed runtime tree. The historical source remains separate.

Documentation validation on this change:

- Frozen archive inspection: **15 profiles / 16 recipes / 182 files verified**,
  zero provider calls and no frozen-file changes.
- Receipt readback completed; the 40 historical physical calls are not new calls.
- All local Markdown file links in the changed documents resolve; Ruff on the
  readback script and `git diff --check` pass.
- No new pytest or paid benchmark run was needed for this documentation change.
  The PR109 targeted test outcomes and original failures remain in
  [the repair record](../parallel-graders/validation.json).

PR109 head CI and Harbor passed. At this documentation readback, merge `3518095`
[CI](https://github.com/proteus-evolve/Proteus/actions/runs/41111143253) and
[Harbor](https://github.com/proteus-evolve/Proteus/actions/runs/41111143243) were still
running. Those are branch checks, not future release-tag gates.
