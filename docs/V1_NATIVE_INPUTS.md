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
separate CLI raw usage sources retain model/tool text, summaries or provider
errors; exports never copy those bytes.

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
| Pi native fresh v3 sessions | Positive reported token totals, including caches, tools and summaries; ambiguous zeros remain unknown | Unknown |
| DSH native fresh format-0 sessions | Latest per-step tokens and summary receipts, excluding fork seeds; auxiliary/retry gaps remain unknown | Unknown |
| Custom minimal policy, Aki | Unknown; no inference from turns, tools or process exits | Unknown |

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

For Pi, the adapter freezes newly created v3 session logs into a private
`ID.usage-source` bundle before publishing its outcome. Readback validates the
bundle hash and re-parses every captured file. Changes to pre-existing sessions,
inherited histories, duplicate identities, unsupported entries, truncated logs,
unfinished assistant turns and failed transports cannot produce complete totals.
Fresh attempts cannot reuse a session ID. A retry cannot replace an earlier
attempt's captured source.

The projection follows the [Pi v0.84.2 usage fold](https://github.com/earendil-works/pi/blob/914cf1472e715297caa30db4b9535d534a9eb718/packages/coding-agent/src/core/usage-totals.ts):
assistant messages, tool results with usage, compaction and branch summaries.
Input is `input + cacheRead + cacheWrite`; output is `output`. Reasoning and
one-hour cache-write subsets are not added again. Missing summary usage remains
unknown, even though upstream's display skips it. Pi providers can initialize
usage to zero and retain it when a response omits usage, so a zero aggregate in
either dimension is also unknown. Each dimension is validated independently.
These are recorded-session totals; unreported extension/subagent work can remain
outside their coverage. Summary entries may combine several requests, and the
runtime's estimated prices are not billed amounts, so model-call counts and
monetary costs remain unknown.

For DSH, `ID.usage-source` retains newly created compressed session files with
their relative paths. Every zstd frame must be complete; live-budget polling's
tolerance for truncated tails does not apply to accounting. The decoder expands
the pinned text/reasoning/tool-call storage rows and checks contiguous event
sequences. Python 3.10–3.13 requires the existing optional `dsh` extra
(`zstandard>=0.21`); Python 3.14 can use its standard-library decoder.

The [pinned DSH token projection](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/token-meter/src/usage-projection.ts)
replaces a step's streaming sample with its final sample. Proteus follows that
rule, adds uncached input plus cache read/write tokens, and includes separate
compaction-summary receipts. Reasoning is already part of output. Unlike Pi's
SDK defaults, explicit valid DSH zero counts are accepted. `header.seedLength`
excludes a new child file's inherited fork history; later process-lifecycle
markers do not discard work already recorded during the same phase. Each new
session is counted once, including captured child sessions.

DSH's main-loop projection alone does not cover all calls. Failed retries,
title/search requests without usage and missing/failed summary receipts keep
totals unknown. The normalized fields retain known token subtotals and unknown
sample counts for inspection. Unsupported logs, changed old files, incomplete
turns and failed transports cannot assert complete totals. Child or extension
work that does not persist into the captured directory remains outside coverage.
Calls and cost stay unknown; these receipts do not introduce finite-cost gating.

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
`tests/check_native_pi_package.py` exercises the installed Pi adapter with a
deterministic transport that writes fresh v3 logs. The optional
`tests/check_pi_usage_reference.py --source /path/to/usage-totals.ts` runs the
hash-verified pinned upstream fold using a Node version with TypeScript stripping;
four positive-token profiles agree, while ambiguous zero defaults deliberately
remain unknown. Neither Pi check runs a real Pi CLI or proves live provider usage.
`tests/check_native_dsh_package.py` checks the actual installed DSH adapter with
a deterministic compressed-session transport. `tests/check_dsh_usage_reference.py`
compares three storage shapes and three main-loop usage profiles with the
hash-verified pinned upstream pure functions. Its Node harness supplies only
schema/brand imports and rejects execution of the unrelated surface projection;
it does not run the DSH CLI or validate provider billing. Auxiliary coverage,
fork exclusion, failed transports and private export/resume are tested separately.
These checks use no paid inference and do not validate container isolation.

Actual live backend release-smoke gates remain required. Internal provider-message
coverage for opaque harnesses, unreported auxiliary work and complete native
aggregate cost coverage remain separate work.
