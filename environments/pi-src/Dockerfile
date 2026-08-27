# pi, built from its own source, bootable from an agent-edited copy of that source.
# Build context = a pi-mono checkout at the pinned tag; this file and boot.sh are copied
# into the context by the pinned build recipe in environments/pi-src/README.md.
FROM node:24-slim
COPY . /opt/src
WORKDIR /opt/src
COPY .proteus-boot.sh /usr/local/bin/pi-boot
RUN chmod +x /usr/local/bin/pi-boot
# bake dependencies, pinned model data (the one network fetch), and the pristine build
# model data is hydrated in the build context beforehand (one network fetch, pinned by
# the context checkout) — hydrating inside the build hits overlayfs EXDEV on rename
RUN npm ci --no-audit --no-fund \
 && npm run build:offline \
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
