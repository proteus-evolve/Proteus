# pi, from source

The image this directory builds is what makes pi's self-evolution operate on its **real
TypeScript source**: `/opt/src` is a pi-mono checkout with dependencies and a pristine
build baked in, and the entrypoint (`boot.sh`) syncs the agent's copy from
`/workspace/src` over that tree, rebuilds with the project's own toolchain when the
source hash changes (build outputs cached on `/state`), and execs the built CLI. An
untouched copy boots in seconds via the pristine-hash fast path; a broken edit exits 97
with the build log tail — that is the adapter's viability gate.

Rebuild (pin the tag you mean to study):

```bash
PI_BUILD_ROOT="$(mktemp -d)"
PI_CONTEXT="$PI_BUILD_ROOT/pi-mono"
git clone --depth 1 --branch v0.84.2 \
    https://github.com/badlogic/pi-mono "$PI_CONTEXT"
# restore model data from the same-version official npm package, then validate it
docker run --rm -v "$PI_CONTEXT:/opt/src" -w /opt/src \
    -v "$PWD/environments/pi-src/hydrate-model-data.sh:/hydrate-model-data.sh:ro" \
    node:24-slim sh /hydrate-model-data.sh
cp environments/pi-src/boot.sh "$PI_CONTEXT/.proteus-boot.sh"
docker build -f environments/pi-src/Dockerfile \
    -t proteus-env-pi-src:0.84.2 "$PI_CONTEXT"
```

Attribution: pi-mono (github.com/badlogic/pi-mono), MIT.

The model values are ignored in the upstream checkout. Pinned builds recover them
from the matching published `pi-ai` package and run upstream's `check:model-data`
before building. Package identity and archive integrity are checked; a missing or
incompatible catalog fails the build. Fetching live catalogs with
`hydrate:model-data` can break older pins when providers or models disappear.

Boot semantics (exact tree): tracked files deleted or renamed by the agent are removed from the baked tree before the overlay (the image carries the `git archive` manifest); the source hash covers paths as well as contents, so renames and empty files always re-key the build; the overlay excludes an agent-installed `node_modules`; and a rebuild first removes every build output and `.tsbuildinfo`, so artifacts are derived from the current source and a deleted entry point cannot boot from a stale bundle. An untouched copy boots via the pristine fast path with no copying at all.
