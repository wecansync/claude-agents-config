#!/usr/bin/env bash
set -euo pipefail
BUNDLE_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ ! -d "$BUNDLE_DIR" ]]; then
  printf 'claude-agents-config: bundle directory is unavailable: %s\n' "$BUNDLE_DIR" >&2
  exit 1
fi
if [[ ! -w "$(dirname -- "$BUNDLE_DIR")" && ! -w "$BUNDLE_DIR" ]]; then
  printf 'claude-agents-config: bundle directory is unavailable or unwritable: %s\n' "$BUNDLE_DIR" >&2
  exit 1
fi
exec python3 "$BUNDLE_DIR/bin/install.py" --bundle "$BUNDLE_DIR" "$@"
