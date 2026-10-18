# 1.0 development: candidate source builds

P04c binds a file-tree candidate to a generated Docker build, an immutable build
receipt, and the image actually used by its Harbor agent trial. The verifier
continues to use the original pinned task image in a separate container.

## Configuration

Add `source_build` to the `proteus.harbor` environment plugin's options. Its
`SourceBuildSpec` contains a nonempty list of shell `commands`, an optional pinned
`builder_image`, `platform` (default `linux/amd64`), `network` (`none` by default,
or explicitly `default`), `timeout_seconds` (default 600), and `reuse` (default
true). Set `EnvironmentSpec.build_hash` to `content_hash(spec)`. Resolution rejects
a mismatched hash. Both runtime and builder bases must use `repository@sha256:…`
pins; mutable tags and bare local image IDs are not portable build inputs.

Commands execute under `/opt/proteus/build` with the complete candidate tree. A
generated multistage Dockerfile copies only this directory into a fresh runtime
base. Put compiled programs, local dependencies or a virtual environment under
that prefix; global package installations in the build stage are not retained.
The base image must already provide Python 3 for the adapter. The subject still
uses a Python entrypoint emitting `PythonReply`; that wrapper may invoke another
compiled program, and the build may generate the entrypoint itself.

The final image also contains a fresh, unmodified source tree under
`/opt/proteus/source`. Before executing candidate code, isolated Python checks its
bytes and executable bits against the artifact manifest. The controller checks
the container's image ID against the build receipt. The verifier uses neither the
build output nor this source tree. Trial artifacts reach it through Harbor's
declared artifact handoff as before.

The first implementation requires the local, single-node Docker Buildx `docker`
driver named `default`; remote builders and custom Dockerfiles are not supported.
Build-time networking is separate from trial networking. `network=none` disables
network access for build commands, while Docker still needs to resolve/pull the
pinned base images. Trial CPU/memory limits apply to Harbor containers, not the
BuildKit daemon. Build commands receive no subject credentials, SSH/secret mounts
or host proxy build arguments; commands should use public pinned dependencies.

## Receipts, cache and accounting

The cache key covers the full artifact, complete build specification, base image,
generated Dockerfile, Docker server version/platform and Buildx/BuildKit identity.
Before reuse, Proteus checks the local image ID, source/build labels and platform.
A missing image triggers a new build; corrupted receipts or mismatched provenance
fail explicitly. Source changes, deletions and renames all change the key.

Receipts and logs live under `private/builds/attempts/`; successful immutable
receipts also live under `private/builds/cache/`. Only images are reused. Each
evaluation still creates a fresh agent and verifier trial; trial files, learned
state and scores are not cached. The cache is scoped to one run. Image garbage
collection and cross-run caches are not implemented; no daemon-wide prune runs.

Image reuse means executing the previously recorded image. It does not assert
bitwise reproducibility of a fresh build with network access, clocks or random
inputs. Set `reuse=false` to bypass both receipt reuse and Docker layer caching.
Failures and interrupted builds never publish reusable receipts. The client
process group is terminated on timeout; uncertain daemon work is not declared a
successful build and generic crash recovery is still pending.

Builds reserve and settle their own ledger entries, including failed attempts.
Arbitrary build scripts have unknown model/token/cost usage; cache inspection has
zero model usage. Build elapsed time appears once in the evaluation stage total,
separately from subject and verifier phase time. A failed build dispatches neither
the subject nor verifier and produces no valid score. A compiler-command failure
is an invalid candidate; Docker/transport errors and build timeouts are infrastructure
errors. Completed identities are never implicitly reissued as retries.

## Acceptance study

```bash
python -m proteus.examples.harbor_source_build \
  --python /tmp/proteus-harbor-runtime/bin/python --out /tmp/harbor-build-study
proteus recipe lock /tmp/harbor-build-study/recipe.json --source proteus=. \
  --out /tmp/harbor-build-study/lock.json
proteus recipe run /tmp/harbor-build-study/recipe.json \
  --lock /tmp/harbor-build-study/lock.json \
  --artifact-store /tmp/harbor-build-study/artifact-store --out /tmp/harbor-build-study/run
```

The fixture generates an executable from source data: A returns 41; B modifies and
renames the data file, deletes an obsolete file and returns 42; restored A reuses
its image and returns 41. A fourth candidate fails compilation and never starts a
trial. The verifier checks that neither source nor build outputs nor global build
stage mutations reached its environment. The Harbor CI job runs this study and
uploads build receipts plus actual agent/verifier image and container identities.
Local unit tests alone do not satisfy this real Docker gate.
