# CORAL circle-packing reference

The unmodified `seed.reference.py`, `grader.reference.py` and
`task.reference.yaml` come from Human-Agent-Society/CORAL commit
`0123dfb939b35228cf2c1fde224cd0561e727408`. Their source paths and SHA-256
digests are recorded in `reference.json`; the Apache-2.0 license is included.

The domain packs 26 circles in a unit square and scores the actual sum of
radii divided by 2.635977. The reference's 1e-6 geometry tolerance and ignored
claimed sum are retained. This is an optimization task without model training.

Proteus changes execution, not the declared optimization objective: candidate
code exports geometry, and a separate private verifier checks it without
importing candidate code or sharing the candidate's Python process. The verifier
uses Python float arithmetic instead of NumPy reductions, so last-bit score
differences are possible. Non-finite or malformed JSON, and geometry files over
256,000 bytes, are rejected. Private diagnostics remain private. Candidate
execution seeds Python and NumPy explicitly; the original grader does not.

NumPy 2.2.6 and SciPy 1.15.3 Linux amd64 CPython 3.12 wheels are pinned by
published PyPI hashes in `reference.json`. These are declared environment
choices; the reference task merely requests NumPy and SciPy without versions.
No real-model reproduction result is implied by preparing or testing this task.
