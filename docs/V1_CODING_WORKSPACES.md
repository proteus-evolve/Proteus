# Coding actor workspaces (P07d)

`proteus.workspace` is a reusable stage for file-based coding actors. Its typed
inputs are `prompt: proteus.text@1`, `seed: proteus.artifact_ref@1` and
`history: proteus.evidence.view@1`. Its output is `response:
proteus.actor_result@1`, whose `workspace_artifact` points to the captured file
tree. A method can interpret that tree as one or multiple proposals, attach
candidate parents, and evaluate them through existing services. The core does
not impose a parent selector, candidate count or proposal manifest format.

## Input and output ownership

`ActorRequest.workspace` contains a `WorkspaceInput`: the exact seed artifact,
history view and output byte/file limits. The service verifies both inputs were
supplied to the requesting stage. The history must match that run, stage, role
and declared evidence policy; hidden evaluation records cannot be granted.

Each actor dispatch receives a fresh task tree:

```text
task/
  input/     copied seed artifact
  history/   disclosed historical files and view.json provenance
  output/    deliverables to capture
  scratch/   disposable work, excluded from the returned artifact
```

The input manifest remains outside the task mount. Inputs are copies, never
writable links into the artifact/evidence stores. Container adapters mount
`input` and `history` read-only, and the service verifies them again after the
actor returns. Trusted local plugins can implement the same protocol, but the
filesystem layout alone is not an operating-system sandbox.

On successful completion the service reads only `output`, rejects symlinks and
special files, enforces the declared aggregate byte/file limits, and publishes
an immutable artifact before settling the actor call. Empty successful outputs,
modified inputs and runtime-authored artifact references fail validation. A
failed actor has no published output artifact; its disposable workspace remains
available for diagnostics. The method decides how to validate and accept each
proposed candidate. Capturing files does not establish that they execute or solve
the task.

## Coding runtimes

The first providers are `proteus.workspace.dsh`, `proteus.workspace.pi` and
`proteus.workspace.codex`. They reuse the existing container coding adapters,
with a fresh session and a frozen controlling harness for each dispatch. Their
root instructions describe the task workspace instead of inheriting the native
self-evolution protocol. The method's prompt is passed as the single `act`
request; a model's final prose is not used as a substitute for its output files.

These providers require an explicit digest-pinned image, `none` or `bridge`
networking and a finite phase timeout. For example, after preparing a recipe,
replace its fixture proposer with a provider appropriate to an installed,
source-compatible image:

```json
{
  "runtime": {
    "plugin": {"name": "proteus.workspace.pi", "version": "1"},
    "options": {
      "image": "YOUR_PI_IMAGE@sha256:YOUR_IMAGE_DIGEST",
      "network": "bridge",
      "phase_timeout_seconds": 600,
      "max_turns": 40
    }
  },
  "model": "YOUR_CONFIGURED_MODEL"
}
```

Supply credentials using the selected adapter's existing configuration. The
proposer runtime can contact its model provider; this is a separate permission
boundary from the offline, credential-free
[Harbor candidate-code provider](V1_HARBOR_CODE.md). Coding workspaces currently
use the native adapters' Docker lifecycle, not a Harbor actor provider.

Native harness accounting has no defensible hard model-call bound. Finite
run/role/stage model-call caps are rejected for these providers, and missing
usage remains unknown. `max_turns` and the phase timeout are runtime controls,
not promises of a precise number or cost of model calls. This batch does not
establish live provider performance or new hard-exit cleanup guarantees for
the native adapters.

Generic coding actors now consume the same immutable usage receipts as native
recipes. Available Codex, Pi and DSH token counts reach the role/stage actor
ledger record and [result export](V1_RESULT_EXPORT.md), even when legacy episode
counters omit tokens. Reported receipts take precedence over those counters;
missing or incomplete source usage remains unknown. Raw receipts are stored
outside the disposable task workspace and hash-bound to the settled actor result.
Completed recovery verifies them without rerunning the coding agent. Calls and
cost remain unknown wherever the underlying source cannot establish them.

The DSH workspace provider binds the requested model through the pinned
[`dsh-v0.1.0-rc.7` `agent-default-model` settings section](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.0-rc.7/packages/core/agent-default-model/src/index.ts), mounted read-only at
`/state/settings.yaml`. It uses the `deepseek-official` provider route and the
existing DeepSeek credential input; arbitrary provider routing is not supported
by this adapter. Legacy native DSH recipes retain their existing profile
selection. Pi and Codex pass the requested model through their CLI arguments.
These are requested-model settings; they do not certify which model a remote
provider ultimately serves.

## Recovery

Workspace inputs are part of the durable actor request identity. Recovery can
redo partial input copying only while the entry is still `reserved`, before
runtime dispatch. A settled actor result uses its immutable output artifact;
the mutable task directory can be deleted without requiring another model call.
Replay and completed-run restore verify that artifact again. Missing or corrupt
output bytes fail closed.

A started actor without a settled result requires reconciliation and is not
automatically repeated. A file left in `output` is not proof that the actor
completed. Thus a hard exit after generation but before settlement remains
uncertain; after settlement but before the enclosing stage commits, recovery
reuses the settled artifact. No external call is inferred from a workspace file.

## Runnable fixture and validation

```sh
python -m proteus.examples.coding_workspace --out /tmp/file-proposer-study
proteus recipe lock /tmp/file-proposer-study/recipe.json \
  --plugin proteus.examples.coding_workspace:register \
  --source "proteus=$PWD" --out /tmp/file-proposer-study/lock.json
proteus recipe run /tmp/file-proposer-study/recipe.json \
  --plugin proteus.examples.coding_workspace:register \
  --lock /tmp/file-proposer-study/lock.json \
  --artifact-store /tmp/file-proposer-study/artifact-store --out /tmp/file-proposer-run
```

The explicitly deterministic actor reads two distinct historical code versions
from files and writes three independent proposals. All five candidates and
twelve predictions enter the ordinary artifact/evaluation records. The hidden
test results of the initial two versions are excluded from the proposer view.
Installed wheel/sdist checks run this example outside the repository.

Tests cover real candidate execution, input grants, output limits, symlink and
input-tampering rejection, all three adapters' mount wiring, missing artifacts,
and process hard exits before dispatch, after dispatch and after settlement.
The Linux Docker gate executes the same workspace bridge with a deterministic
container process, checks actual read-only mount behavior and host-file/env
exclusion, then captures/evaluates three proposals and resumes. This is a
container contract test, not live DSH/Pi/Codex model inference.

Meta-Harness integration still needs the upstream proposer instructions/output
adapter, domain data, offline/online learning and validation/final-selection
control flow. Its published proposer uses Claude Code; choosing one of these
other runtimes is an explicit adaptation, not a claim of an exact model/runtime
reproduction. Full recipe and real benchmark acceptance remain open.
