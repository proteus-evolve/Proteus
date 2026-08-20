# dsh, from source

Same design as [`environments/pi-src/`](../pi-src/README.md): the image bakes a
deepseek-harness monorepo checkout at the pinned tag (dependencies installed, `build:lib`
run once), and `boot.sh` syncs the agent's copy from `/workspace/src` over the baked
tree, rebuilds with the project's own toolchain when the source hash changes (outputs
cached on `/state`), and execs the built CLI (`apps/cli/lib/bin.js`). The source tar the
adapter extracts is produced by `git archive`, so the seed's `src/` is exactly the
tracked source of the build it boots.

Rebuild:

```bash
DSH_BUILD_ROOT="$(mktemp -d)"
DSH_CONTEXT="$DSH_BUILD_ROOT/deepseek-harness"
git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness "$DSH_CONTEXT"
git -C "$DSH_CONTEXT" fetch --depth 1 origin tag dsh-v0.1.0-rc.8
git -C "$DSH_CONTEXT" checkout dsh-v0.1.0-rc.8
cp environments/dsh-src/boot.sh "$DSH_CONTEXT/.proteus-boot.sh"
# --network host: the default bridge goes through vpnkit NAT on macOS, whose connections
# exhaust under pnpm's parallel registry fetches and kill the install
docker build --network host -f environments/dsh-src/Dockerfile \
    -t proteus-env-dsh-src:0.1.0-rc.8 "$DSH_CONTEXT"
```

An untouched source takes the pristine fast path without copying or rebuilding. A changed
source is exact-synced and pays one in-container `build:lib` per distinct source hash;
subsequent boots of that source state reuse `/state` while still syncing runtime-read files
such as `config/`.

Attribution: deepseek-harness (github.com/deepseek-ai/deepseek-harness), MIT.

Boot semantics (exact tree): tracked files deleted or renamed by the agent are removed from the baked tree before the overlay (the image carries the `git archive` manifest); the source hash covers paths as well as contents, so renames and empty files always re-key the build; the overlay excludes an agent-installed `node_modules`; and a rebuild first removes every build output and `.tsbuildinfo`, so artifacts are derived from the current source and a deleted entry point cannot boot from a stale bundle. An untouched copy boots via the pristine fast path with no copying at all.
