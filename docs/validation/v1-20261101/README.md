# November 1 preparation evidence

This directory contains new preparation evidence. It does not rewrite the frozen
October model results or qualify an unpublished release tag.

- `api-freeze.json`: 100 resolving v1 names and 64 public Contract schema hashes.
- `installed-distributions.json`: 1.0.0 wheel/sdist artifact hashes and source
  verification, plus 22 installed checks outside the checkout. The artifacts were
  built from clean `9807a41`; the byte check occurred at `81ede83` after only
  acceptance-runner/test changes. All 261 Python package files match. Four named
  extras (`llm`, `sandbox`, `pi`, `dsh`) installed; optional fixed GEPA is separately
  required in CI, and real runtimes have their dedicated gates.
- `combee-shared-trace.json`: compact comparison and archive identity for the
  source-bound `5794378` eleven-sample four-arm run, followed by read-only verification.
- `combee-shared-trace-5794378.tar.gz`: 331 original fixture files sufficient for
  independent readback, including actual request/response records, manifest/lock,
  datasets, artifact bytes and invocation receipts. No provider was called. It is
  a verification bundle, not a resumable run: private recovery journals are omitted.
- `earlier-*.json`: original independent review and source/config/task drift probes
  against `9807a41`. The probes really passed the old reader incorrectly. Later
  identity checks reject those changes; these failures are not deleted.
- `initial-focused.log`, `trace-final-tests.log`: initial 10 focused cases and the
  final 10 trace-specific cases respectively. The sets overlap; do not sum them.
- `archive-verify.json`: unchanged 15 profiles, 16 recipes and 182 frozen files.
- `initial-build-missing-dependency.log`, `twine.log`: the initial local setup lacked
  the `build` package; a fresh build-tool environment produced both successful
  distributions. This was not a runtime failure.

To verify the original common-trace records without executing methods or models:

```sh
# Use the repository with commit 5794378 available and the unchanged reader.
# A shallow clone may need: git fetch origin 57943787369aff43fb180818af9eba5b6a68b1c6
mkdir -p /tmp/proteus-trace-readback
tar -xzf docs/validation/v1-20261101/combee-shared-trace-5794378.tar.gz \
  -C /tmp/proteus-trace-readback
python recipes/validation/combee-shared-trace/run.py \
  --out /tmp/proteus-trace-readback/combee-shared-trace --verify
```

The archive preserves original absolute path strings inside locked records to
retain their hashes. The reader reads the supplied extraction directory; those
strings are provenance, not commands to execute. The archive was extracted into a
different local directory and successfully verified before commit. Run a fresh
comparison with the [recipe instructions](../../../recipes/validation/combee-shared-trace/README.md).

The broader [preparation record](../../V1_FINAL_ACCEPTANCE.md) tracks accepted
CORAL qualification and outstanding required workflows separately. Prepared package version
1.0.0 is not evidence of a published release.

The final `independent-review.json`, `independent-trace-probes.json`,
`independent-package-readback.json` and `independent-trace-tests.log` close R04
and the trace-reader P2. They accept API/package evidence and the bounded I09
contract while explicitly leaving CORAL and remaining release gates open.

`native-source/readback.json` binds successful preparation run 41390451600 to both
required source jobs and the original artifact ZIP hashes. Per-backend original
activation/repair evidence and distinct controller process records are retained.
These are real source-built Docker harnesses with a deterministic provider, not
the separate paid full-smoke gate.

`native-source/independent-readback.json` records the separate reviewer's rehash
of both original ZIPs and four successful source/repair verifier invocations.
The review reconstructed archived Git checkpoints without Docker dispatch or
provider calls; it accepts this preparation source gate only.

The [CORAL evidence index](coral-directed/README.md) contains the original fixture
and single paid-workflow ZIPs, the frozen recipe, three actual request receipts,
usage, retained initial audit/dispatch failures, and independent acceptance.
Its current-source directed protocol closes I11/R05 and the corresponding I14
reporting requirement; it is separate from the original circle-packing quality runs.

`release-smoke-41390453875-dsh-failed.log` preserves the first preparation DSH
self-edit failure. The normal sweep and two self-edit episodes completed, but
no source change existed before episode two. The workflow did not archive the
raw DSH trace, so the cause is unproven; the complete release-smoke gate remains
unaccepted while that failure is investigated.

`i14-output/i14-generic-disposition.json` and its saved-record readback close the
two generic output requirements: five runs, 92 task rows and 30 candidates, with
no omitted rollout. All nine report files per run were reconstructed without
models, evaluators or mutations to the source copies. Unknown costs/timing remain
unknown; historical reference/adapted results and common-condition records stay
separate. Fourteen historical method profiles are adapted; the fifteenth is the
Native mechanism profile. This review does not close the full-smoke gate.
