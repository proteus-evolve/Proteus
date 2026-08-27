#!/bin/sh
set -eu

if [ -z "${AKI_HARNESS_SRC:-}" ]; then
    echo "AKI_HARNESS_SRC must name the Aki 0.1.0 checkout" >&2
    exit 2
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_DIR=$(CDPATH= cd -- "$AKI_HARNESS_SRC" && pwd)
IMAGE=proteus-env-aki-src:0.1.0
BUILD_ROOT=$(mktemp -d)
CONTEXT="$BUILD_ROOT/context"
trap 'rm -rf "$BUILD_ROOT"' EXIT HUP INT TERM

test -f "$SOURCE_DIR/pyproject.toml"
test -f "$SOURCE_DIR/uv.lock"
case "$(uv --version)" in
    "uv 0.11.2 "*) ;;
    *) echo "building the Aki image requires host uv 0.11.2" >&2; exit 2 ;;
esac
mkdir -p "$CONTEXT/aki-source" "$CONTEXT/proteus-worker"

uv export \
    --project "$SOURCE_DIR" \
    --frozen \
    --no-dev \
    --no-emit-project \
    --no-hashes \
    --no-header \
    --quiet \
    --output-file "$CONTEXT/requirements.txt"

tar -C "$SOURCE_DIR" \
    --exclude='./.git' \
    --exclude='./.env' \
    --exclude='./.env.*' \
    --exclude='./.aki' \
    --exclude='./.claude' \
    --exclude='./.venv' \
    --exclude='./venv' \
    --exclude='./.pytest_cache' \
    --exclude='./.mypy_cache' \
    --exclude='./.ruff_cache' \
    --exclude='./outputs' \
    --exclude='./output' \
    --exclude='./runs' \
    --exclude='./Aki-experiments-data' \
    --exclude='./proteus' \
    --exclude='*/.git' \
    --exclude='*/.env' \
    --exclude='*/.env.*' \
    --exclude='*/.venv' \
    --exclude='*/venv' \
    --exclude='*/outputs' \
    --exclude='*/output' \
    --exclude='*/runs' \
    --exclude='*/__pycache__' \
    --exclude='*/.pytest_cache' \
    --exclude='*/.mypy_cache' \
    --exclude='*/.ruff_cache' \
    --exclude='*.pyc' \
    -cf - . | tar -C "$CONTEXT/aki-source" -xf -

if ! CACHE_DIRECTORIES=$(find "$CONTEXT/aki-source" -type d \
    \( -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -print); then
    echo "could not enumerate the scrubbed Aki build context" >&2
    exit 2
fi
if [ -n "$CACHE_DIRECTORIES" ]; then
    echo "scrubbed Aki build context still contains a tool cache" >&2
    exit 2
fi

cp "$SCRIPT_DIR/Dockerfile" "$CONTEXT/Dockerfile"
cp "$SCRIPT_DIR/boot.sh" "$CONTEXT/boot.sh"
cp "$SCRIPT_DIR/controller.patch" "$CONTEXT/controller.patch"
cp "$SCRIPT_DIR/../../proteus/adapters/aki_container_worker.py" \
    "$CONTEXT/proteus-worker/aki_container_worker.py"

docker build -t "$IMAGE" "$CONTEXT"
