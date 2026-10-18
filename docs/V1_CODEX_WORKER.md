# Persistent Codex worker bridge

`proteus.workers.codex.codex_worker` builds options for the shared
`proteus.worker.command@1` runtime. Any method declaring a worker can use these
options, including the CORAL controller. This is a local trusted runtime;
container worker isolation and real CORAL benchmark acceptance remain unfinished.

## Declare a provider

```python
import hashlib
from pathlib import Path
from proteus.workers.codex import codex_worker

executable = Path('/absolute/path/to/codex')
options = codex_worker(
    (str(executable),),
    command_files={str(executable): 'sha256:' + hashlib.sha256(executable.read_bytes()).hexdigest()},
    model='your-explicit-model',
    api_base_url='https://api.openai.com/v1',
    api_key_env='CODEX_API_KEY',
    credential_env=('CODEX_API_KEY',),
    network_access=False,
)
```

The helper does not read credentials or call a model. Store `options.to_dict()`
in a `WorkerSpec` runtime `PluginSpec(plugin=PluginRef(name='proteus.worker.command',
version='1'), options=...)`; roles, gateway membership, budget and method options remain normal
recipe declarations. Pin any wrapper scripts in `command_files` as well. Keys
stay in the named process environment, outside recipe/lock/launch records.

`api_base_url` and `api_key_env` explicitly configure a Responses provider after
the CLI `exec` subcommand. Without a URL, the CLI's default provider applies.
Using global CLI prefix flags to select the provider was ineffective in the
tested desktop build; the local API check caught this before real inference.
URLs containing credentials, query strings or fragments are rejected.

## Continuation and accounting

The bridge uses `--no-daemon`, `--ignore-user-config`, an explicit model, and
workspace-specific `.proteus-codex` state. It never resumes `--last` or copies a
personal auth directory. It verifies the exact UUID from `thread.started` and
passes that UUID to `exec resume`. Provider output is wrapped before entering the
worker protocol so an agent message cannot impersonate a completion receipt.

The tested CLI reports **cumulative session** token usage in `turn.completed`.
The supervisor keeps those totals in a typed completion record. The next native
launch receives that record's totals, and the bridge charges only the difference.
If a pause/kill loses the preceding terminal totals, the next generation's delta
is unknown; its newly confirmed totals can anchor subsequent generations. A
decreasing counter fails rather than silently charging a negative or zero delta.
Fresh fallback starts a distinct session and resets the baseline explicitly.

Model-call counts and money remain unknown: one CLI turn can contain several
requests and the CLI event does not establish their count or prices. Generation
timeouts and shared concurrency apply; this runtime cannot enforce hard model-call
or monetary caps. The common worker service rejects unsupported finite limits.

## Execution boundary

The declared CLI profile uses `workspace-write`, `approval_policy="never"`,
explicit shell network access, and disabled nested agents. Model-generated shell
commands receive a named allowlist of basic environment variables and the scoped
evaluation gateway URL/token; provider key variables are excluded from that
shell environment. These settings are not an OS-level private-grader read
boundary. The local process and CLI state remain trusted. Use an isolated worker
backend before claiming containment of untrusted research agents.

## Validation and supported scope

Validated locally with **codex-cli 0.159.2**. Binary content pins are required;
other CLI versions need compatibility verification. Official JSON-event and
resume semantics: [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

The following check uses the actual selected CLI and a local Responses stub,
with a synthetic key. It checks native session IDs, preserved history, two actual
HTTP requests, and incremental token accounting without paid inference:

```sh
PYTHONPATH=. python tests/check_codex_cli.py --executable /absolute/path/to/codex
```

The same check also makes two additional local requests through the native Codex
adapter, verifies distinct fresh ephemeral sessions and immutable raw usage
receipts, and confirms that cached-token subsets are not counted twice. Native
reporting and its remaining coverage limits are documented in
[V1_NATIVE_INPUTS.md](V1_NATIVE_INPUTS.md#reported-native-usage).

Ordinary subprocess tests also cover cooperative and forced pause, partial output
on interruption, protocol spoofing, missing terminal events, malformed usage,
controller reconstruction and explicit fallback. The installed wheel/sdist gate
uses a deterministic provider executable to verify packaging and three resumed
generations outside the checkout. These checks establish protocol integration;
they do not establish research quality, real provider credentials, container
isolation or a completed CORAL reproduction.
