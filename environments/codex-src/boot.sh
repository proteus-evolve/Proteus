#!/bin/bash
set -euo pipefail

WORKSPACE=/workspace
SOURCE="$WORKSPACE/src"
STATE=/state
BIN_DIR="$STATE/bin"
HASH_FILE="$STATE/source.sha256"
CODEX_HOME="$STATE/codex-home"
IMG_CODEX=/opt/codex-target/release/codex
IMG_HOST=/opt/codex-target/release/codex-code-mode-host
PRIS_HASH=/opt/codex-source.sha256

# /state is a run-private bind mount. Both dirs must stay writable by the host-user model
# phases even when a root boundary boot created them first: codex keeps runtime state
# (auth refreshes, PATH aliases, in-process app-server helpers) next to its binary, under
# CODEX_HOME, and under $HOME (the container default HOME=/root is not writable by the
# host-user phases and makes codex fail with EACCES during app-server init).
mkdir -p "$STATE" "$CODEX_HOME" "$BIN_DIR"
chmod 777 "$CODEX_HOME" "$BIN_DIR" 2>/dev/null || true
export HOME="$CODEX_HOME"
export CODEX_HOME="$STATE/codex-home"

if [ ! -d "$SOURCE/codex-rs" ]; then
  echo "Proteus Codex source surface missing: $SOURCE/codex-rs" >&2
  exit 96
fi

# Content hash of the mounted source; the recipe matches the image's pristine hash so an
# untouched seed extracts and hashes identically to the baked /opt/src.
SOURCE_HASH="$(cd "$SOURCE" && { find . -type f \
    ! -path '*/.git/*' ! -path '*/target/*' -print0 | sort -z | \
    xargs -0 sha256sum 2>/dev/null || true; } | sha256sum | awk '{print $1}')"
OLD_HASH="$(cat "$HASH_FILE" 2>/dev/null || true)"
BIN="$BIN_DIR/codex"
HOST="$BIN_DIR/codex-code-mode-host"

is_root=0
[ "$(id -u)" = 0 ] && is_root=1

needs_gate=0
if [ "$SOURCE_HASH" != "$OLD_HASH" ] || [ ! -x "$BIN" ] || [ ! -x "$HOST" ]; then
  needs_gate=1
fi

# install_bin atomically publishes a validated (codex, codex-code-mode-host) pair into the
# run-private /state/bin and records the source hash that produced them.
install_bin() {
  local d
  d="$BIN_DIR/.install.$$"
  rm -rf "$d"
  mkdir -p "$d"
  if ! cp "$1" "$d/codex" || ! cp "$2" "$d/codex-code-mode-host"; then
    rm -rf "$d"
    return 1
  fi
  chmod 755 "$d/codex" "$d/codex-code-mode-host"
  mv -f "$d/codex" "$BIN_DIR/codex"
  mv -f "$d/codex-code-mode-host" "$BIN_DIR/codex-code-mode-host"
  rm -rf "$d"
  printf '%s\n' "$SOURCE_HASH" > "$HASH_FILE"
  chmod 644 "$HASH_FILE" 2>/dev/null || true
}

if [ "$needs_gate" = 1 ]; then
  # Pristine candidate: the mounted source is byte-identical to the baked source, so the
  # image's own prebuilt release binaries are exactly right and only need copying — no
  # compilation. Model phases never install (they only exec the last published pair).
  if [ "$SOURCE_HASH" = "$(cat "$PRIS_HASH" 2>/dev/null || true)" ] \
     && [ -x "$IMG_CODEX" ] && [ -x "$IMG_HOST" ]; then
    install_bin "$IMG_CODEX" "$IMG_HOST" \
      || { echo "could not install pristine Codex binaries" >&2; exit 96; }
    needs_gate=0
  fi
fi

if [ "$needs_gate" = 1 ]; then
  if [ "$is_root" != 1 ]; then
    echo "Proteus Codex: candidate changed or binaries missing, but this container is not root." >&2
    echo "Boundary validation (runs as container root) must publish the candidate first." >&2
    exit 95
  fi

  # Changed candidate. Overlay it onto the image's baked /opt/src *in place* and compile
  # with CARGO_HOME=/usr/local/cargo + CARGO_TARGET_DIR=/opt/codex-target, i.e. the exact
  # paths the image cache was built with, so Cargo's fingerprints stay valid and only
  # files whose contents really changed (rsync --checksum, no timestamp trust) recompile.
  # The overlay lives in this disposable container layer (root-owned), so the image and
  # the host are never modified.
  rsync -rlp --checksum --delete --exclude .git --exclude target "$SOURCE/" /opt/src/
  export CARGO_HOME=/usr/local/cargo
  export CARGO_TARGET_DIR=/opt/codex-target
  export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-1}"
  export CARGO_NET_OFFLINE=true
  # Baked into the image at build time: point Cargo at the pinned prebuilt V8 rather
  # than letting the `v8` crate's own build script try (and fail) to fetch one itself.
  export RUSTY_V8_ARCHIVE=/opt/rusty-v8-archive.a.gz
  export RUSTY_V8_SRC_BINDING_PATH=/opt/rusty-v8-binding.rs

  # Gate 1: the candidate's own tests must at least compile. A release build skips
  # #[cfg(test)] code, so without this step a candidate that breaks its test modules
  # would pass validation and only fail later, when tests are actually run. This compiles
  # lib test harnesses (codex-tui / codex-core / codex-cli) but does not execute tests;
  # behavioural validation still belongs to the experiment's own tests.
  TEST_LOG="$STATE/last-test-build.log"
  if ! (cd /opt/src/codex-rs \
        && cargo test --locked -p codex-tui -p codex-core -p codex-cli \
             --lib --no-run) >"$TEST_LOG" 2>&1; then
    echo "Codex candidate tests do not compile" >&2
    tail -n 80 "$TEST_LOG" >&2 || true
    exit 98
  fi

  # Gate 2: release binaries. codex-code-mode-host is a second binary this Codex build
  # routes all tool/shell execution through; codex fails every tool call closed without
  # it, so it is always built alongside codex-cli, not treated as optional.
  BUILD_LOG="$STATE/build.log"
  if ! (cd /opt/src/codex-rs \
        && cargo build --locked -p codex-cli -p codex-code-mode-host --release) \
        >"$BUILD_LOG" 2>&1; then
    echo "Codex candidate build failed" >&2
    tail -n 120 "$BUILD_LOG" >&2 || true
    exit 97
  fi

  if ! install_bin "$IMG_CODEX" "$IMG_HOST"; then
    echo "Codex candidate build succeeded but binaries could not be installed" >&2
    exit 96
  fi
fi

# Proteus mode: preserve Codex' native JSONL in a state file while also keeping stdout.
if [ "${1:-}" = "--proteus-json-log" ]; then
  LOG_PATH="$2"
  shift 2
  mkdir -p "$(dirname "$LOG_PATH")"
  set +e
  "$BIN" "$@" | tee "$LOG_PATH"
  CODEX_RC=${PIPESTATUS[0]}
  set -e
  exit "$CODEX_RC"
fi

exec "$BIN" "$@"
