# dsh, built from its own source, bootable from an agent-edited copy of that source.
# Build context = a deepseek-harness checkout at the pinned tag WITH its .git (the
# source tar is produced by `git archive`, so it is exactly the tracked files).
FROM node:24-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY . /opt/src
WORKDIR /opt/src
# NOTE: bake with `docker build --network host` on macOS — the default bridge goes
# through vpnkit NAT, whose connections exhaust under pnpm's parallel registry fetches
# (the same reason runtime containers use host networking).
# Keep both Corepack's package-manager cache and pnpm's content store outside /root.
# Runtime validation runs as the host uid, so a cache under root's HOME would appear
# empty and Corepack would try to download pnpm again during an offline cold start.
ENV COREPACK_HOME=/opt/corepack
RUN corepack enable && pnpm install --frozen-lockfile --store-dir /opt/pnpm-store \
    --config.trust-lockfile=true
# Candidates may use only the package-manager version baked for the pinned seed. A
# packageManager change is a dependency change too and must fail closed, not reach out.
ENV COREPACK_ENABLE_NETWORK=0
RUN npm run build:lib
COPY .proteus-boot.sh /usr/local/bin/dsh-boot
RUN chmod +x /usr/local/bin/dsh-boot
RUN git archive --format=tar -o /opt/dsh-source.tar HEAD \
 && rm -rf /opt/src/.git \
 && tar -tf /opt/dsh-source.tar | grep -v '/$' > /opt/source-manifest.txt \
 && mkdir /tmp/pristine && tar -xf /opt/dsh-source.tar -C /tmp/pristine \
 && dsh-boot --proteus-tree-hash /tmp/pristine > /opt/pristine-hash \
 && dsh-boot --proteus-dependency-hash /tmp/pristine > /opt/pristine-dependency-hash \
 && rm -rf /tmp/pristine
ENV DSH_HOME=/state
ENV DSH_TELEMETRY_MODE=DISABLED
# the boot wrapper syncs and rebuilds inside /opt/src as whatever uid the host runs
# the container with — the tree must be writable by any user
RUN chmod -R a+rwX /opt/src /opt/corepack /opt/pnpm-store \
 && chmod a+r /opt/pristine-hash /opt/pristine-dependency-hash /opt/source-manifest.txt \
 && chmod a+r /opt/*.tar
WORKDIR /workspace
ENTRYPOINT ["dsh-boot"]
