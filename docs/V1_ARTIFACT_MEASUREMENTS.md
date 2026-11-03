# Artifact and lineage measurements

Saved-run export can compute content changes without running a candidate, model,
method or third-party codec. Supply a separate measurement specification:

```sh
proteus recipe export /path/to/run --out /path/to/new-export \
  --measurements /path/to/measurements.json
```

```json
{
  "schema_version": 1,
  "file_surfaces": [
    {"name": "code", "subdir": "main.py", "unit": "top_level_def"},
    {"name": "skills", "subdir": "skills", "unit": "directory"}
  ],
  "lineage_paths": {}
}
```

Use the paths and surface units appropriate to the actual candidate artifact.
`subdir` is a portable relative artifact path; `.` selects the entire tree.
It never identifies a host path. Surface names must be unique. The Python
contracts `MeasurementSpec` and `MeasurementSurface` are available through
`proteus.api.v1`; pass a spec to `report_run(..., measurement_spec=spec)` or
`export_run(..., measurement_spec=spec)`.

The ordinary export remains unchanged when no spec is supplied. With a spec,
`report.json` gains `measurements`, and three additional tables are written:

- `artifact-measurements.csv`: candidate/artifact identity, unit type, counts,
  unit-set hash and explicit availability.
- `lineage-edges.csv`: a separate comparison for every recorded parent→child
  edge, including rejected or failed candidates present in the saved lineage.
- `lineage-paths.csv`: totals only for explicitly named parent chains.

## Supported units

| Codec | Unit | Change definition |
| --- | --- | --- |
| `proteus.text_components@1` | Named text component | Exact decoded text changes under the same component key |
| `proteus.structured_records@1` | Named record | Canonical JSON content changes under the same record ID |
| `proteus.file_tree@1` | Declared `file`, `directory` or `top_level_def` surface | Existing native `proteus.measure.distance` units, independently for each surface |

File trees require explicit surface declarations. Unknown codecs and codec
versions are reported as unsupported; their plugin code is never loaded.
Cross-codec edges have no distance. Custom playbooks, opaque native checkpoint
identities and other representations do not acquire a structural metric merely
because they can be serialized.

File surfaces reuse the existing native UTF-8 text instrument. Directory units
group each top-level directory and cover its files recursively; root-level files
are individual units. `top_level_def` counts Python definitions/classes and
module-level content. A Python syntax error uses the legacy whole-file fallback,
and `syntax_fallback_files` records that change in granularity. Non-Python files
in a definition surface, or non-UTF-8 files anywhere in a selected surface, make
that surface unavailable. No binary replacement decoding is used for this API.
The legacy instrument itself remains unchanged.

The native instrument normalizes text newlines and excludes `__pycache__`.
Executable mode changes are not measured. Empty or absent declared surfaces have
zero units and expose `present: false`; their zero distance says nothing about
undeclared parts of an artifact. Overlapping surfaces are reported separately and
are never summed into an overall artifact score. Source artifacts are measured,
not build outputs or a live worker's uncommitted filesystem.

## Edges, branches and paths

For one compatible pair, the report counts added, dropped, revised and unchanged
units. Distance is `(added + dropped + revised) / union`, or zero when both unit
sets are empty. Churn is `(dropped + revised) / (added + dropped + revised)`, or
zero when no unit moved. Renames count as a removal and an addition; this is not
semantic matching, edit distance, model quality or an efficiency score.

A root has no fabricated parent comparison. A merge has one row per real parent
per surface. Candidate creation order is not interpreted as a path through a
branching graph. To request a path, use real candidate IDs from `candidates.csv`:

```json
"lineage_paths": {"via-branch-a": ["root", "branch-a", "merged"]}
```

Every consecutive pair must be a recorded parent edge. Duplicate IDs, missing
parents, cycles and paths that jump between siblings fail validation. A valid
path reports per-unit-type count totals and `edge_distance_sum`, the sum of its
edge distances. This can exceed one and is not endpoint displacement. A missing
or incompatible edge makes the corresponding path result null; the exporter
does not sum only the available steps. No aggregate across paths, surfaces or
codec types is defined.

## Evidence and comparison boundaries

The report verifies artifact manifests and bytes before measuring them. It binds
the spec, relevant instrument source files and Python version; each metric ID
also includes the codec, unit definition and surface. Different metric IDs must
not be pooled. All source hashes remain in the saved-run evidence map, and the
report hash covers the derived measurements. Measurements can be recomputed
after moving the saved run; they do not mutate its store, manifest or journal.
Hidden grade visibility follows the ordinary export policy.

Native behavioral trace distance, crystallization probes and audit instruments
remain available through their existing APIs. An artifact-only export has no
new behavioral or causal claim. Real-model recipe comparisons and final release
acceptance remain separate requirements.
