# Shared research knowledge (P09a)

`proteus.knowledge.SharedKnowledgeStore` stores research notes, reusable skill
files and released evaluation evidence. It is separate from candidate artifacts,
actor handoffs and private evaluation records. This is storage infrastructure for
CORAL and other methods; persistent workers and a complete CORAL recipe are still
being implemented.

## Publication and concurrency

Each logical entry has a kind (`notes`, `skills` or `attempts`), a portable key,
an immutable file-tree artifact and numbered revisions. A publication records its
writer, operation ID, expected previous revision and source references. Sources
can identify an earlier knowledge revision or an already released EvidenceView.

Creating an entry requires `expected=None`. Updating or deleting it requires the
exact revision previously read. Two workers editing the same revision cannot
silently overwrite each other: one commits and the other receives a
`KnowledgeConflict` carrying the current reference. The worker can inspect that
version and explicitly reconcile its own edits. No content merge is inferred.

SQLite commits the revision, current head and successful operation receipt in one
transaction. Conflict responses are also committed under the request identity;
a retry reports the same observed conflicting revision even after later writes.
Use a new operation ID when resolving that conflict with a new request.
Immutable file bytes are published first. A crash can leave unused
blobs but cannot publish a partial entry. Retrying the same successful writer /
operation identity returns its original receipt, even after subsequent edits;
reusing that identity with different inputs is rejected.

Notes and skills support deletion tombstones and explicit restoration. Historical
revisions remain readable. Keys that overlap or alias on common filesystems are
rejected, including case-folding and Unicode-normalization aliases. A skill
contains `SKILL.md` and may include executable scripts or other files.

## Evidence and permissions

The store's immutable `KnowledgeIdentity` binds run, configuration, lock and the
shared evidence disclosure policy. Evidence is denied by default. When enabled,
the source view's evaluation IDs, splits and kinds must fit that policy, and the
view must belong to the same run. Reopening the store cannot expand the policy.

Workers publish notes and skills. `record_attempt` is a controller operation: it
copies a released view containing one candidate and evaluation evidence. It does
not accept a worker-supplied numeric score. Formal attempts are append-only and
cannot be replaced or deleted through the note APIs. A note's prose remains the
writer's claim; a source citation does not automatically validate that claim.

The controller owns the database and artifact store. Writer IDs are supplied by
the authenticated orchestration layer; this local Python API is not a process
authentication mechanism. Future worker/gateway integration must expose bounded
operations and exported views, not mount the private store into agent workspaces.

## Reading and snapshots

`head`, `history`, `get` and `read` resolve verified revisions. `snapshot` records
an atomic capture of all current heads, including tombstones. `get_snapshot`
retrieves that capture by content reference. A fabricated mixture of independently
read revisions cannot be exported as a recorded snapshot.

`materialize` exports a previously captured snapshot into a fresh directory:

```text
knowledge.json
notes/<key>/<files>
skills/<key>/SKILL.md
skills/<key>/<supporting files>
attempts/<key>/view.json
attempts/<key>/evidence/<released files>
```

Exports contain copies, not links to mutable working files or private storage.
Editing a copied file does not alter its recorded source. Deleted entries are
omitted from current exports but remain in historical snapshots.

## Recipe and method integration

`RecipeConfig.knowledge` declares named spaces, allowed actor roles and a shared
evidence policy. The configuration hash locks those declarations; the dependency
lock includes the file-tree codec. Hidden evaluation pools cannot be granted.
Changing a space or its permissions requires a new run, not a resumed old run.

Methods use `services.knowledge(space_id)`. `publish`, `delete` and
`record_attempt` accept an explicit role, writer and operation ID and return a
typed `KnowledgeOutcome` (`published` or `conflict`). `capture` records a read
under its role, reader and operation IDs and returns a typed snapshot reference.
Evidence sources must be committed, stage-bound views in the current run.

The method journal records each operation. The store separately commits writes,
conflicts and named reads so an exit between storage commit and journal commit
does not change their outcomes. Restoring method state verifies recorded revisions
and snapshots but does not rewind shared heads or discard other workers' updates.

A stage must declare the space in `StageSpec.knowledge` and receive its snapshot
as an explicit input. `context.knowledge_entries`, `read_knowledge` and
`materialize_knowledge` enforce that binding. A stage cannot use these APIs to read
an unsupplied snapshot or a revision absent from that snapshot.

The offline example performs real local candidate evaluation, publishes only
released validation evidence, records two logical writers' notes and a skill,
and demonstrates a stale-write conflict. It is a service integration example,
not a persistent multi-agent CORAL run:

```sh
python -m proteus.examples.shared_knowledge --out /tmp/shared-knowledge-study
python -m proteus.cli recipe lock /tmp/shared-knowledge-study/recipe.json \
  --plugin proteus.examples.shared_knowledge:register \
  --source proteus=/absolute/path/to/Proteus --out /tmp/shared-knowledge-lock.json
python -m proteus.cli recipe run /tmp/shared-knowledge-study/recipe.json \
  --plugin proteus.examples.shared_knowledge:register \
  --lock /tmp/shared-knowledge-lock.json --out /tmp/shared-knowledge-run \
  --artifact-store /tmp/shared-knowledge-study/artifact-store
python -m proteus.cli recipe resume /tmp/shared-knowledge-study/recipe.json \
  --plugin proteus.examples.shared_knowledge:register \
  --lock /tmp/shared-knowledge-lock.json --out /tmp/shared-knowledge-run
```

The 1.0 API is still in development. As with other locked recipes, runs created
against earlier source/configuration versions must resume with their original
version. This change does not migrate previous experimental run records.

## Validation and remaining work

Storage checks cover two independent processes racing on the same revision,
hard exits before and after transaction commit, idempotent retries, immutable
historical exports, source/disclosure checks and corrupt-content rejection. Method
checks force exits after storage commits and after journal commits, introduce a
later worker publication, and verify that old reads/conflicts stay unchanged
while subsequent captures include the new knowledge.

The installed-distribution gate runs the example through the public CLI outside
the checkout and inspects historical exports and evaluation coverage after
resume. Worker lifecycle, authenticated EvaluationGateway and event policies
remain subsequent CORAL work; this store does not substitute for those features.

The reference under inspection is [CORAL at
`0123dfb939b35228cf2c1fde224cd0561e727408`](https://github.com/Human-Agent-Society/CORAL/tree/0123dfb939b35228cf2c1fde224cd0561e727408).
Its notes/skills/attempts organization informs the first consumer. Proteus adds
explicit revision checks and durable operation receipts as a framework adaptation;
these are not claims of source-level CORAL conformance.
