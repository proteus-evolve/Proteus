#!/bin/bash
set -euo pipefail

WORKSPACE=/workspace
SOURCE="$WORKSPACE/src"
STATE=/state
OUTPUT=/output
PUBLISHED=/opt/proteus-bin
IMG_CODEX=/opt/codex-target/release/codex
IMG_HOST=/opt/codex-target/release/codex-code-mode-host
PRIS_HASH=/opt/codex-source.sha256

source_hash() {
  (cd "$1" && find . \
      \( -type d \( -name .git -o -name target \) -prune \) -o \
      \( -type f -o -type l \) -print0 | LC_ALL=C sort -z | \
      while IFS= read -r -d '' path; do
        if [ -L "$path" ]; then
          printf 'L\0%s\0' "$path"
          readlink -z "$path"
        else
          printf 'F\0%s\0' "$path"
          sha256sum "$path" | cut -d' ' -f1 | tr -d '\n'
          printf '\0'
        fi
      done | sha256sum | cut -d' ' -f1)
}

if [ "${1:-}" = "--proteus-source-hash" ]; then
  source_hash "$2"
  exit 0
fi

if [ ! -d "$SOURCE/codex-rs" ]; then
  echo "Proteus Codex source surface missing: $SOURCE/codex-rs" >&2
  exit 96
fi

SOURCE_HASH="$(source_hash "$SOURCE")"

if [ "${1:-}" = "--proteus-validate" ]; then
  EXPECTED_HASH="${2:-}"
  if [ -z "$EXPECTED_HASH" ] || [ "$SOURCE_HASH" != "$EXPECTED_HASH" ]; then
    echo "Proteus Codex source hash mismatch during boundary validation" >&2
    exit 94
  fi
  if [ "$(id -u)" != 0 ] || [ ! -d "$OUTPUT" ]; then
    echo "Proteus Codex boundary validation needs root and a dedicated /output mount" >&2
    exit 95
  fi

  export HOME=/tmp/proteus-validation-home
  export CARGO_HOME=/usr/local/cargo
  export CARGO_TARGET_DIR=/opt/codex-target
  export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-1}"
  export CARGO_NET_OFFLINE=true
  export RUSTY_V8_ARCHIVE=/opt/rusty-v8-archive.a.gz
  export RUSTY_V8_SRC_BINDING_PATH=/opt/rusty-v8-binding.rs
  mkdir -p "$HOME"

  if [ "$SOURCE_HASH" = "$(cat "$PRIS_HASH" 2>/dev/null || true)" ] \
     && [ -x "$IMG_CODEX" ] && [ -x "$IMG_HOST" ]; then
    true
  else
    # The candidate is read-only. Its build.rs files and proc macros run as root only in
    # this disposable, networkless container; their only host-persistent writable mount is
    # the dedicated output directory. The baked source/Cargo paths preserve fingerprints.
    rsync -rlp --checksum --delete --exclude .git --exclude target "$SOURCE/" /opt/src/

    TEST_LOG="$OUTPUT/last-test-build.log"
    if ! (cd /opt/src/codex-rs \
          && cargo test --locked -p codex-tui -p codex-core -p codex-cli \
               --lib --no-run) >"$TEST_LOG" 2>&1; then
      echo "Codex candidate tests do not compile" >&2
      tail -n 80 "$TEST_LOG" >&2 || true
      exit 98
    fi

    BUILD_LOG="$OUTPUT/build.log"
    if ! (cd /opt/src/codex-rs \
          && cargo build --locked -p codex-cli -p codex-code-mode-host --release) \
          >"$BUILD_LOG" 2>&1; then
      echo "Codex candidate build failed" >&2
      tail -n 120 "$BUILD_LOG" >&2 || true
      exit 97
    fi
  fi

  cp "$IMG_CODEX" "$OUTPUT/codex"
  cp "$IMG_HOST" "$OUTPUT/codex-code-mode-host"
  chmod 755 "$OUTPUT/codex" "$OUTPUT/codex-code-mode-host"
  printf '%s\n' "$SOURCE_HASH" >"$OUTPUT/source.sha256"
  if ! CODEX_HOME="$HOME" "$OUTPUT/codex" --version >/dev/null; then
    echo "Codex candidate binaries were built but failed the version probe" >&2
    exit 96
  fi
  exit 0
fi

# Runtime receives only the controller-selected hash directory at this read-only path.
# Authentication, sessions, aliases, and other mutable app-server state live separately.
CODEX_HOME="$STATE/codex-home"
RUNTIME_BIN="$STATE/runtime-bin"
BIN="$PUBLISHED/codex"
HOST="$PUBLISHED/codex-code-mode-host"
HASH_FILE="$PUBLISHED/source.sha256"
mkdir -p "$CODEX_HOME" "$RUNTIME_BIN"
export HOME="$CODEX_HOME"
export CODEX_HOME
export PATH="$RUNTIME_BIN:$PATH"

if [ "$(cat "$HASH_FILE" 2>/dev/null || true)" != "$SOURCE_HASH" ] \
   || [ ! -x "$BIN" ] || [ ! -x "$HOST" ]; then
  echo "Proteus Codex: no validated binary publication matches the active source" >&2
  exit 95
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
