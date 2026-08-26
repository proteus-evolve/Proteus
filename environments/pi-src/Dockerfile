# pi, built from its own source, bootable from an agent-edited copy of that source.
# Build context = a pi-mono checkout at the pinned tag; this file and boot.sh are copied
# into the context by the pinned build recipe in environments/pi-src/README.md.
FROM node:24-slim@sha256:a9f5f7c91a432850b2a8a7797adf5eadb6c733ceed61167806cee7ea7fbc29df
COPY . /opt/src
WORKDIR /opt/src
COPY .proteus-boot.sh /usr/local/bin/pi-boot
COPY .proteus-source-ref /opt/pi-source-ref
RUN chmod +x /usr/local/bin/pi-boot
# Model data is restored from the checked-in bundle by prepare-context.sh; no catalog
# endpoint is consulted. The current catalog removes Cloudflare's completions models,
# exposing a v0.84.2 inference bug. Apply an erased generic annotation only for compile,
# then restore and compare the upstream source before creating the staged source tar.
RUN cp packages/ai/src/providers/cloudflare-ai-gateway.ts /tmp/cloudflare-original.ts \
 && sed -i 's/return createProvider({/return createProvider<"anthropic-messages" | "openai-completions" | "openai-responses">({/' \
        packages/ai/src/providers/cloudflare-ai-gateway.ts \
 && npm ci --no-audit --no-fund \
 && npm run build:offline \
 && cp /tmp/cloudflare-original.ts packages/ai/src/providers/cloudflare-ai-gateway.ts \
 && cmp /tmp/cloudflare-original.ts packages/ai/src/providers/cloudflare-ai-gateway.ts \
 && rm -rf /opt/src/.git \
 && tar -cf /opt/pi-source.tar --exclude=node_modules --exclude='packages/*/dist' \
        --exclude='packages/*/*/dist' -C /opt src \
 && tar -tf /opt/pi-source.tar | sed 's|^src/||' | grep -v '/$' | grep -v '^$' \
        > /opt/source-manifest.txt \
 && mkdir /tmp/pristine && tar -xf /opt/pi-source.tar --strip-components=1 -C /tmp/pristine \
 && pi-boot --proteus-tree-hash /tmp/pristine > /opt/pristine-hash \
 && rm -rf /tmp/pristine
# the boot wrapper syncs and rebuilds inside /opt/src as whatever uid the host runs
# the container with — the tree must be writable by any user
RUN chmod -R a+rwX /opt/src && chmod a+r /opt/pristine-hash /opt/source-manifest.txt \
 && chmod a+r /opt/*.tar
WORKDIR /workspace
ENTRYPOINT ["pi-boot"]
