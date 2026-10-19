# API, schema and stored-run compatibility

Proteus has several independent version identities. A compatible Python import
surface does not authorize interpreting old experiment records with changed code.

| Identity | Meaning | Change rule |
| --- | --- | --- |
| `proteus.api.v1` / `API_VERSION=1` | Public researcher imports and documented extension behavior | Preserve documented source compatibility within this API; introduce an explicit new API version for incompatible behavior |
| `Contract.schema_version` | Stored framework record shape | Unknown versions/fields are rejected; incompatible record changes require an explicit format/migration policy |
| `PluginRef.version` | Plugin's declared interface/behavior generation | Authors increment for incompatible options, ports or semantics; never reuse a version to excuse changed implementation bytes |
| Plugin implementation hash and lock | Exact source, schemas and capabilities used by a run | Changed implementation requires a fresh lock and experiment; a version string alone is insufficient |
| `PluginState.payload_version` | Plugin-private checkpoint shape | Register and validate the exact method state schema; unsupported states cannot silently resume |
| Manifest/configuration/dependency hashes | Full immutable experiment identity | Resume requires the original resolved configuration and lock |

The `v1` import surface is proposed for the 1.0 release. Before that release,
changes on `dev/1.0.0` remain development changes and require regenerated locks for
new runs. A stable import does not promise that a plugin can mutate service
internals, that every backend supports every capability, or that any new method
can be implemented solely through configuration.

## Read and resume existing work

| Saved work | Read/report route | Resume route |
| --- | --- | --- |
| Existing native sweep/episode records | Existing `proteus report`, measurement and history tools | Existing `proteus run ... --on-existing resume`, retaining its original runtime/configuration constraints |
| Version-1 recipe manifest and journal | `proteus recipe export RUN --out NEW_DIRECTORY`; supported schema and evidence identities must validate | `proteus recipe resume CONFIG --lock LOCK --out RUN`, loading the original plugins/source/runtime |
| Native compatibility recipe | Recipe metadata, native receipts and reported usage; bounded minimal/HTTP call profiles additionally verify ledger coverage | The same recipe/lock and adapter recovery rules; finite profiles retain reservations and stop unresolved dispatches ([details](V1_NATIVE_INPUTS.md#finite-native-recipe-model-call-budgets)) |
| Unknown/incompatible record or plugin-state version | Use the original environment or an explicitly implemented offline reader | Finish with the original implementation or create a new run; no automatic in-place migration |

An existing native sweep is not converted into a recipe run by adding a manifest
or copying a journal. A native checkpoint can be imported as an artifact with
explicit provenance; that creates a new experiment and does not transfer hidden
state, historical budgets or an active session. Source files, raw results, locks
and old experiment histories are never rewritten as part of a read or upgrade.

Generic export validates saved identities and aggregation without loading method
plugins or rerunning graders. It supports the recorded version-1 shapes available
to that reader; it does not promise arbitrary future-schema compatibility. Domain
reports can impose stricter implementation locks and regrading requirements.
Non-journal/native export is explicitly limited to its latest invocation rather
than inventing cumulative coverage.

A plugin author may use explicit schema conversion helpers for a separate offline
migration, but that alone does not migrate a running experiment. Until a migration
path preserves the full configuration, artifact, operation, budget and external
session protocol and has its own acceptance checks, resume stays on the original
version. Keep the original checkout and environment for pending runs.

## Session and environment capability boundaries

Fresh actor calls, declared handoff context and native session continuation are
separate protocols. A runtime must advertise the requested capability; the framework
does not silently exchange them. Persistent research workers record native session
confirmation, and any fresh fallback is an explicit launch/continuity decision.
Likewise, independent trials, explicit sequence state and persistent worker
workspaces are different lifecycles. Harbor/Docker requirements are checked;
missing Docker is not a request to execute an untrusted candidate on the host.

The existing runtime release-smoke gates remain required. Public API examples and
installed-package checks supplement them; they do not replace five-family real
model results, real backend checks, or the final tag/commit's release acceptance.
The [native adapter input records](V1_NATIVE_INPUTS.md) cover the owned submission
boundary; opaque internal provider messages and aggregate native costs can remain
unavailable. [Release gates](RELEASING.md) bind the exact release tag/commit. Their
implementation does not imply that the actual release has passed.
