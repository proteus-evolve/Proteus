# 1.0 development: ACE playbook artifacts

P06a adds the opt-in `proteus.ace_playbook@1` codec and versioned counter/curator
deltas. They use selected pure functions from
[ace-agent/ace](https://github.com/ace-agent/ace/tree/82709de050e1db6e6ef2f07bcb0393560b94992a),
revision `82709de050e1db6e6ef2f07bcb0393560b94992a`. The source license and provenance
ship with the package; no upstream model SDK or embedding dependency is required.

This completes the artifact/update foundation of P06. Generator, Reflector,
Curator, reference prompts, offline/online control flow, task adapters and real
baseline experiments remain necessary for R02. These artifact checks do not count
as an ACE recipe or a reproduction of the paper's results. Core APIs are unchanged.

## Use the codec

```python
from proteus.artifacts.store import ArtifactStore
from proteus.methods.ace.playbook import CODEC, Playbook, PlaybookDelta, register
from proteus.recipes.builtins import builtin_registry

registry = builtin_registry()
register(registry)
store = ArtifactStore("runs/ace-artifacts", registry)
initial = store.put(CODEC, Playbook.from_text())
added = store.apply_delta(initial, PlaybookDelta(
    base_hash=initial.content_hash, kind="curate",
    records=({"type": "ADD", "section": "OTHERS", "content": "Check the units."},),
))
counted = store.apply_delta(added, PlaybookDelta(
    base_hash=added.content_hash, kind="counters",
    records=({"id": "misc-00001", "tag": "helpful"},),
))
playbook = store.get(counted)
assert playbook.next_id == 2 and playbook.bullets[0].helpful == 1
assert store.get(initial).bullets == ()
```

A method uses the same delta with `services.fork_candidate(parent, delta=...)`.
This records the complete proposed delta, parent relationship and resulting
artifact through the ordinary candidate journal. Register the codec through
`--plugin proteus.methods.ace.playbook:register` when a recipe uses it; registration
alone does not supply an ACE method. `services.checkpoint(...)` retains the method
cursor separately from the candidate's playbook.

## State and update contract

Each artifact contains two non-executable files:

- `playbook.txt`: exact UTF-8 text, including section order, blank lines and
  continuation lines. It is the text consumed by role prompts.
- `index.json`: canonical, schema-versioned metadata containing the pinned source
  revision, next global ID and ordered parsed bullet records (including line
  numbers). Every decode rebuilds and verifies the index against the text.

The index's bullet content is the first parsed line, as in upstream. Multiline
content is retained in the text and is not silently folded into a different prompt
or reflector input. `Playbook.stats()` and `extract(ids)` use the upstream functions;
extraction follows playbook order rather than the order of the requested IDs.

`next_id` is explicit and participates in artifact identity. `from_text(text)`
infers the next unused numeric suffix on first import. Passing `next_id=...` can
retain a higher previously allocated counter; loading never recomputes it downward.
The codec requires a sectioned playbook, unique parsed bullet IDs and a next ID
greater than every existing numeric suffix. It rejects ambiguous state and updates
whose resulting text violates those constraints before publishing any bytes.
This is a declared input restriction around upstream, not an upstream repair.

`PlaybookDelta` has `base_hash`, `kind` (`counters` or `curate`) and an ordered array
of JSON `records`. It retains duplicate/ignored records and extra fields for audit:

- Counter tags use `id`, with the upstream `bullet` alias as fallback. The last tag
  for a repeated ID wins. Helpful/harmful increment once; other tags are neutral.
  Non-object tags and missing IDs are ignored. A nonempty identifier must be a
  string. Unknown IDs do not create bullets.
- The upstream counter function returns the original text when no tag ID exists;
  otherwise it strips outer whitespace and reformats matching bullet lines even
  for a neutral tag. The codec retains these observable differences.
- Curator records require a string `type`. Only `ADD` executes. Missing ADD section
  and content use upstream defaults (`general`, empty text); present values must
  be strings. Later role-response validation must additionally apply Curator's
  required-field rules before submitting a delta.
- UPDATE, MERGE, DELETE, CREATE_META and unknown operations do not mutate entries
  or allocate IDs. `delta.ignored_operations` exposes their zero-based positions;
  their raw records remain in the journal. A curator call containing only ignored
  operations may still strip outer whitespace, as the upstream function does.
- ADD preserves reference section normalization, slug construction, fallback and
  placement. For example, `STRATEGIES & INSIGHTS` produces `sai-00001`, not `fin`;
  an absent `general` section falls immediately after the exact `## OTHERS` header.
  No automatic sorting, deduplication, compression or second model rewrite occurs.

The wrapper converts upstream update errors to `ContractError`, preserving the
parent. It does not fix problematic upstream section slugs. The method must decide
how to report a rejected update under its declared failure protocol. Token-budget
behavior, raw model-response parsing and optional BulletpointAnalyzer behavior are
not implemented by this codec and must not be inferred from its acceptance checks.

## Verification

`tests/check_ace_reference.py --source /path/to/pinned/ace` verifies SHA-256 hashes
of the actual reference files, the ASTs of all eight copied functions (normalizing
only trailing docstring whitespace), the initial
template and the bundled license. It executes only those verified pure function
ASTs, so importing upstream's SDK-heavy `utils.py` is unnecessary. Across 48 fixed
seeds it checks 768 ordered updates and 2,304 extractions against the reference,
including section fallbacks, ignored operations, duplicate tags and multiline text.
Every Python CI version fetches the pinned source and runs this comparison.

`test_v1_ace_playbook.py` also checks explicit edge-case expectations, corrupted
indexes, ID reuse, immutable parent preservation and stale-base rejection. A
fixture method drives candidate services with fixed deltas and exits the process
after candidate publication or after journal commit. Resume must retain the same
IDs, counters, operation records and parent chain; a second resume does no work.
It makes zero model calls and performs no benchmark evaluation.

Wheel and sdist checks run `check_ace_package.py` outside the repository, verifying
the installed codec, deterministic updates, materialization and license files.
