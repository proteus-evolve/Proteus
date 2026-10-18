# Native adapter input evidence

Core episode prompts and final adapter submissions are different records. The
existing `native_prompts` array records core rendering, including the default
protocol, goal and authorized feedback. Adapters now persist their final inputs
immediately before invoking the policy, HTTP transport or harness process.

| Adapter | Recorded boundary | Scope |
| --- | --- | --- |
| minimal | Exact lazy-policy prompt, including the live budget header | `policy_prompt`; no model request is implied |
| llm | Complete chat body: model, system/user messages, current harness state, temperature and output limit | `chat_request` |
| Pi / DSH / Codex | Exact argv passed to the harness sandbox, including the final phase prompt and live budget header | `harness_cli` |
| Aki | Supervisor invocation and episode number | `opaque_supervisor`; internal prompts and actual model remain unavailable |

The CLI boundary does not expose every provider request made inside a coding
harness. Its system prompts, tool schemas, file instructions and later context
construction remain owned by that pinned harness and its native logs. Aki builds
phase prompts internally and still does not consume Proteus's phase overrides.
Custom adapters must add their own boundary instrumentation; an empty record list
is not proof of zero model calls or complete prompt coverage.

## Durable records and failure meaning

For a native run at `RUN`, input records are stored at
`RUN.parent/.proteus-records/RUN.name/native-inputs/`, outside the native subject
workspace and candidate snapshots. Each attempt has a fresh random ID and two
separate JSON files:

- `ID.input.json`: version, adapter, episode/phase, scope, requested model where
  known, protocol version, preparation timestamp, exact input value and hashes.
- `ID.outcome.json`: the input hash, completion timestamp, transport outcome and
  versioned usage evidence for records written with format version 2.

Files are atomically published and fsynced before dispatch. Inputs are never
replaced by a retry. Ordinary exceptions write a `raised` outcome containing the
exception type; a hard exit can leave an input without an outcome. The reader
reports that completion as `unknown`, including the ambiguity between a crash
just before dispatch and a crash after remote work. It never resumes or reissues
work based on these records. Existing native recovery rules remain authoritative.
A `returned` outcome means the boundary returned; a nonzero process return code
is still failure. Boundary counts are not model-call or token accounting.

Inputs and outcomes carry checked content hashes. Hashes detect changed evidence
but are not signatures against a party able to rewrite all controller records.
Input/outcome metadata excludes environment mappings, authentication headers and
exception text. Recorded model input itself can contain task data, so these files
remain controller records rather than automatically shared public reports. The
separate Codex raw usage source retains CLI output, including model/tool text or
provider errors; exports never copy those bytes.

## Recipe runs

`execution-NNNN.json` now includes `native_inputs`, the cumulative validated input
records under that recipe's native workspace. Its existing `native_prompts` stays
specific to the latest invocation. Resuming an already completed recipe adds no
new inputs and preserves the old files. An interrupted attempt remains visible
alongside any later retry, even if the process exited before it could write the
recipe execution file.

Use `proteus.core.native_inputs.read_native_inputs(native_root)` for checked
inspection. The helper and raw storage layout are implementation interfaces,
not additions to `proteus.api.v1`. Generic result exports keep native usage
separate from managed ledger accounting and its budget enforcement.

## Reported native usage

New input/outcome pairs use format version 2. Version 1 pairs remain readable with
unknown usage; they are not rewritten on resume. Each version 2 outcome binds a
whitelisted usage projection to its input hash, including on ordinary failure.
Missing outcomes retain unknown usage. Export verifies receipts against the
latest cumulative snapshot and checks that earlier snapshots have not lost their
recorded attempts. A completed resume adds neither calls nor zero-filled entries.

| Boundary | Reported model/token usage | Monetary cost |
| --- | --- | --- |
| Built-in minimal mock policy | Zero; only the exact bundled local policy qualifies | Zero inference cost |
| OpenAI-compatible chat | One received response; provider `prompt_tokens` / `completion_tokens` when valid | Unknown |
| Codex native `exec --json --ephemeral` | Completed fresh-thread token receipt; model-call count remains unknown | Unknown |
| Custom minimal policy, Pi, DSH, Aki | Unknown; no inference from turns, tools or process exits | Unknown |

Chat token counts must be nonnegative integers; missing, negative, Boolean,
fractional or string values become unknown. Cache and reasoning subcounts are
already included in the provider's totals and are not added again. A response
with invalid assistant content can still report valid usage; transport errors
without such evidence remain unknown. Arbitrary provider metadata and cost
extensions are excluded. Episode counter aggregation also preserves explicit
unknowns across episodes and resume, but durable receipts additionally retain
attempts that never reached an episode checkpoint.

For Codex, the adapter captures the phase JSONL stream (or transport stdout when
the container did not tee it) before publishing its outcome. `ID.usage-source`
retains those exact bytes privately, so a retry overwriting the ordinary phase
log cannot erase earlier accounting evidence. The outcome binds its hash, size,
confirmed thread ID, parse status and normalized counts. Readback hashes and
re-parses this source with the same strict stream parser used by persistent
Codex workers. Missing, modified or symlinked source files fail validation.

The native adapter always requests a new ephemeral thread, so the completed
thread's cumulative token totals have a zero baseline. Resume-shaped commands
and duplicate thread IDs across distinct fresh attempts are rejected. Cached
input tokens are already included and are not added again. Nonzero exits,
timeouts, malformed/truncated streams and missing terminal events keep token
totals unknown, even if a partial log contains a completed receipt. Model-call
counts and monetary cost remain unknown. This reports the harness's recorded
tokens, not independently audited billing or usage of unreported internal agents.

`proteus recipe export` exposes `native_accounting` and `native-usage.csv` with
counts, source and completion status, without copying prompts or provider output.
Unknown components keep `reported` totals `null`; `known_subtotal` is explicitly
incomplete. An empty record list means unavailable coverage. Receipt source hashes
under `.proteus-records/` appear only with `--include-private`.

`wall_seconds` measures elapsed time around each instrumented boundary, including
failed boundaries. It is not end-to-end runtime or provider inference time. These
totals cover recorded boundaries only: native evaluators, custom adapters and
internal harness work can remain outside their coverage. Generic finite native
cost/model-call caps remain rejected; reporting creates no new enforcement claim.

## Validation and remaining acceptance

Offline tests compare records with an actual local HTTP server's request body,
including state changed by an earlier phase. Sandbox fixtures check exact Pi,
DSH and Codex argv, transport failures, and DSH's resumed live-budget header.
Subprocess hard-exit tests prove input survival, unknown completion and separate
retry identity. Installed wheel and sdist checks execute the native recipe and
resume without new evidence, and read back a captured Codex protocol fixture.
`tests/check_codex_cli.py` additionally exercises the real selected CLI against a
local Responses stub: persistent-worker continuation plus two fresh native phases.
It verifies actual token receipts and excludes previous-phase model history.
These checks use no paid inference and do not validate container isolation.

Actual live backend release-smoke gates remain required. Internal provider-message
coverage for opaque harnesses, Pi/DSH usage reconciliation and complete native
aggregate cost coverage remain separate work.
