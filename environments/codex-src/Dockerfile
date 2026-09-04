# Source-evolving OpenAI Codex environment for Proteus.
# Pin by immutable SHA for reproducible experiments.
FROM rust:1.95-bookworm

ARG CODEX_REF=a7b86b6201d35200280e3d24ca5ebccd8122d6ca
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates pkg-config libssl-dev libcap-dev clang cmake python3 jq rsync zstd curl \
    && rm -rf /var/lib/apt/lists/*

# GitHub clones on some build hosts are flaky (TLS resets, mid-transfer EOF); retry
# rather than fail the whole image build on a transient network blip.
RUN for i in 1 2 3 4 5; do \
      rm -rf /opt/src && git clone https://github.com/openai/codex.git /opt/src && break; \
      echo "clone attempt $i failed, retrying..." >&2; sleep 5; \
    done \
    && cd /opt/src \
    && git checkout --detach "$CODEX_REF" \
    && git submodule update --init --recursive

# codex-code-mode-host is a separate binary this Codex build routes ALL tool/shell
# execution through - without it every tool call fails closed (verified: the agent
# cannot run commands or edit files at all, only produce text), so it is not optional.
# It links `v8`, whose crate build script fetches gn/ninja over a raw socket that
# ignores http_proxy/https_proxy and 404s against upstream denoland/rusty_v8 for this
# sandboxed variant/target combo besides. openai/codex publishes its own prebuilt
# archive+binding for exactly this purpose (see codex-rs/../third_party/v8/README.md,
# scripts/codex_package/v8.py) - fetch and checksum-verify those instead of building
# V8 from source, and point Cargo at them via RUSTY_V8_ARCHIVE/RUSTY_V8_SRC_BINDING_PATH
# so the crate's own downloader never runs.
# Override CARGO_BUILD_JOBS on memory-constrained build hosts (rustc's LTO codegen for
# the largest crates can OOM at full `-j nproc` parallelism on small boxes).
ARG CARGO_BUILD_JOBS=
RUN cd /opt/src/codex-rs \
    && V8_VERSION=$(awk '/^name = "v8"$/{f=1} f && /^version = /{gsub(/"/,"",$3); print $3; exit}' Cargo.lock) \
    && V8_TARGET=x86_64-unknown-linux-gnu \
    && V8_PROFILE=ptrcomp_sandbox_release \
    && V8_URL="https://github.com/openai/codex/releases/download/rusty-v8-v${V8_VERSION}" \
    && V8_DIR=/opt/rusty-v8 && mkdir -p "$V8_DIR" \
    && ARCHIVE="librusty_v8_${V8_PROFILE}_${V8_TARGET}.a.gz" \
    && BINDING="src_binding_${V8_PROFILE}_${V8_TARGET}.rs" \
    && CHECKSUMS="rusty_v8_${V8_PROFILE}_${V8_TARGET}.sha256" \
    && curl -fsSL --retry 8 --retry-all-errors --retry-delay 5 --connect-timeout 30 \
          "$V8_URL/$CHECKSUMS" -o "$V8_DIR/$CHECKSUMS" \
    && curl -fsSL --retry 8 --retry-all-errors --retry-delay 5 --connect-timeout 30 \
          "$V8_URL/$ARCHIVE" -o "$V8_DIR/$ARCHIVE" \
    && curl -fsSL --retry 8 --retry-all-errors --retry-delay 5 --connect-timeout 30 \
          "$V8_URL/$BINDING" -o "$V8_DIR/$BINDING" \
    && (cd "$V8_DIR" && sha256sum -c --ignore-missing "$CHECKSUMS") \
    && cp "$V8_DIR/$ARCHIVE" /opt/rusty-v8-archive.a.gz \
    && cp "$V8_DIR/$BINDING" /opt/rusty-v8-binding.rs \
    && export RUSTY_V8_ARCHIVE="$V8_DIR/$ARCHIVE" RUSTY_V8_SRC_BINDING_PATH="$V8_DIR/$BINDING" \
    && export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-1}" \
    && for i in 1 2 3 4 5; do \
         if CARGO_TARGET_DIR=/opt/codex-target cargo fetch --locked; then FETCH_OK=1; break; fi; \
         echo "fetch attempt $i failed, retrying..." >&2; sleep 10; \
       done \
    && [ -n "${FETCH_OK:-}" ] \
    && CARGO_TARGET_DIR=/opt/codex-target \
       cargo build --locked -p codex-cli -p codex-code-mode-host --release

# Prewarm the candidate gate's test profile. boot.sh runs the exact same `cargo test
# --lib --no-run` (offline, against the /state copy of this target dir) before the release
# build at every changed-candidate boundary, so the debug artifacts and dev-dependencies
# for codex-tui/codex-core/codex-cli must already be baked here: network is available at
# image build time but not at boot. Env/flags must stay identical to boot.sh's gate 1.
RUN cd /opt/src/codex-rs \
    && if [ -n "${CARGO_BUILD_JOBS:-}" ]; then export CARGO_BUILD_JOBS; else export CARGO_BUILD_JOBS=1; fi \
    && export RUSTY_V8_ARCHIVE=/opt/rusty-v8-archive.a.gz \
              RUSTY_V8_SRC_BINDING_PATH=/opt/rusty-v8-binding.rs \
    && CARGO_TARGET_DIR=/opt/codex-target \
       cargo test --locked -p codex-tui -p codex-core -p codex-cli --lib --no-run

# Seed tar is exactly the source used above; generated build state and .git are excluded.
# The hash uses the same recipe as boot.sh's SOURCE_HASH so a first boot's freshly
# extracted /workspace/src hashes identically and can reuse /opt/codex-target as-is.
RUN tar --exclude=.git --exclude=target -cf /opt/codex-source.tar -C /opt/src . \
    && cp -a /usr/local/cargo /opt/cargo-home-seed \
    && (cd /opt/src && { find . -type f ! -path '*/.git/*' ! -path '*/target/*' -print0 | \
         sort -z | xargs -0 sha256sum; } | sha256sum | awk '{print $1}') > /opt/codex-source.sha256 \
    && chmod -R a+rX /opt/cargo-home-seed /opt/codex-target /opt/src /opt/codex-source.tar \
                      /opt/codex-source.sha256 /opt/rusty-v8-archive.a.gz /opt/rusty-v8-binding.rs

COPY boot.sh /usr/local/bin/proteus-codex
RUN chmod +x /usr/local/bin/proteus-codex
ENTRYPOINT ["/usr/local/bin/proteus-codex"]
