#!/usr/bin/env bash
# Build the stylesheet with the Tailwind standalone CLI.
#
# The binary is pinned and downloaded on demand into .tailwind/ (gitignored).
# Pinning matters: an unpinned "latest" download makes the committed CSS
# depend on the day it was built.
#
# Usage: scripts/build-css.sh [output-path]

set -euo pipefail

TAILWIND_VERSION="v4.3.3"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${ROOT}/.tailwind"
BIN="${BIN_DIR}/tailwindcss-${TAILWIND_VERSION}"

INPUT="${ROOT}/src/promptheus/static/input.css"
OUTPUT="${1:-${ROOT}/src/promptheus/static/app.css}"

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)  ASSET="tailwindcss-macos-arm64" ;;
  Darwin-x86_64) ASSET="tailwindcss-macos-x64" ;;
  Linux-aarch64) ASSET="tailwindcss-linux-arm64" ;;
  Linux-x86_64)  ASSET="tailwindcss-linux-x64" ;;
  *)
    echo "No Tailwind standalone build for $(uname -s)-$(uname -m)" >&2
    exit 1
    ;;
esac

if [ ! -x "${BIN}" ]; then
  echo "Downloading Tailwind ${TAILWIND_VERSION} (${ASSET})…" >&2
  mkdir -p "${BIN_DIR}"
  curl -fsSL -o "${BIN}" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${ASSET}"
  chmod +x "${BIN}"
fi

"${BIN}" --input "${INPUT}" --output "${OUTPUT}" --minify
