# Shared knowledge from persistent workers

P09d gives declared workers an authenticated interface to shared notes, skills
and formal attempt records. Workers can inspect a captured snapshot, fetch an
immutable revision and publish or delete notes/skills with a required expected
version when replacing an existing entry. The controller remains responsible for
creating formal attempts from released evaluation evidence.

## Explicit grants

`GatewaySpec.knowledge_access` defaults to `none`. Set it to `read` or `read_write`
and identify a `knowledge_space` to grant worker access. The gateway's existing
member/role and evidence-policy checks still apply. Merely configuring automatic
shared attempts does not grant worker read or write access.

The interface uses the local gateway URL and bearer token supplied to the worker
runtime. Worker ID and role come from that authenticated member, and cannot be
overridden in a request body. Members of the shared space can read each other's
notes, skills and released formal attempts. Entries are shared research records,
not per-worker private files.

| Endpoint | Request | Result |
| --- | --- | --- |
| `POST /v1/knowledge/snapshots` | `operation_id` | Recorded snapshot and its reference |
| `GET /v1/knowledge/revisions/<digest>` | Revision content hash | Immutable revision and file bytes |
| `POST /v1/knowledge/writes` | `KnowledgeSubmission` | Published revision or conflict receipt |

`KnowledgeSubmission` contains an operation ID, `publish` or `delete`, `notes`
or `skills`, a logical key, files, optional expected revision and source references.
Skills require `SKILL.md`. Deletes need an expected revision and no files. The
gateway's upload-size and file-count limits also apply to knowledge requests.
Formal attempts cannot be authored, overwritten or deleted through this interface.
Sources must be existing revisions in this space or released evidence within its
run/configuration/lock and disclosure policy; private evaluation values and
cross-run references are rejected.

## Client and CLI

The client uses the same environment as the evaluation submission client:

```python
from proteus.artifacts.codecs import FileWrite
from proteus.gateway.knowledge import KnowledgeSubmission
from proteus.knowledge.client import KnowledgeClient

client = KnowledgeClient()
snapshot = client.capture("orientation-001")
outcome = client.write(KnowledgeSubmission(
    operation_id="note-001", action="publish", kind="notes", key="experiment-idea",
    files=(FileWrite.from_bytes("note.md", b"Try a different parent selector."),),
))
```

```sh
python -m proteus.knowledge.client capture --operation orientation-001
python -m proteus.knowledge.client read REVISION_HASH
python -m proteus.knowledge.client publish notes experiment-idea ./note \
  --operation note-001
python -m proteus.knowledge.client publish notes experiment-idea ./revised-note \
  --operation note-002 --expected REVISION_HASH --source REVISION_HASH
python -m proteus.knowledge.client delete notes experiment-idea \
  --operation note-003 --expected REVISED_REVISION_HASH
```

CLI hashes are the 64 hex characters of a revision content hash without the
`sha256:` prefix. The Python API accepts typed references and also supports a
released EvidenceView as a source. Note text remains a worker's assertion;
referencing a formal attempt does not turn arbitrary prose into a verified score.

## Concurrent writes and controller recovery

The knowledge store commits compare-and-swap writes, conflicts and captured
reads atomically, independently of the method journal. HTTP handlers do not
dispatch method operations or modify `RunServices.Values`. A worker can therefore
read or write while the controller is evaluating a candidate or replaying earlier
method operations.

An identical repeated write returns its original result. A repeated conflict
returns the revision that conflicted at that time, even after later updates.
Reuse of an operation ID with changed input is rejected. Autonomous writes have
a separate operation namespace from controller-generated formal attempts.

Repeated capture calls with one operation ID return the original snapshot.
Use a new operation ID to observe later publications. Revision reads continue to
work after later edits and deletion. A lost HTTP response can be recovered by
resending the identical request; the CLI leaves retry policy with its caller.
An initialized but missing database is rejected instead of recreating an empty
history. Existing journal snapshots never rewind live shared heads.

This remains a trusted local worker interface. It inherits the loopback transport
and same-host trust boundary described in [the Gateway documentation](V1_EVALUATION_GATEWAY.md).
It does not add remote/container worker connectivity or an OS permission sandbox.

## Cooperative executable example

```sh
python -m proteus.examples.worker_knowledge --out knowledge-study
proteus recipe lock knowledge-study/recipe.json \
  --plugin proteus.examples.worker_knowledge:register \
  --source "proteus=$PWD" --out knowledge-study/lock.json
proteus recipe run knowledge-study/recipe.json \
  --plugin proteus.examples.worker_knowledge:register \
  --lock knowledge-study/lock.json --artifact-store knowledge-study/artifact-store \
  --out knowledge-study/run
```

Worker A publishes a note containing a candidate strategy. Worker B reads that
note and uses its contents to construct the candidate it submits for formal
evaluation. After receiving feedback, B publishes a skill citing A's note and its
own formal attempt. Two local subprocesses produce one shared note, one skill and
two controller-generated attempts. The proposed strategy is a deterministic
fixture, not a model research result.

Tests verify actual subprocess exchange, cross-worker CAS conflicts, historical
read/write receipts, permission and provenance checks, and controller hard exits
after a publication or capture is committed but before its HTTP response. The
installed package check exercises the public CLI outside the repository.
CORAL reflect/consolidate/pivot policies, the complete CORAL recipe and real
single-worker/multi-worker experiments remain subsequent work.
