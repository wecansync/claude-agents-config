#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then
    exec "$SCRIPT_DIR/install.sh" --uninstall "$@"
  fi
done
exec "$SCRIPT_DIR/install.sh" --uninstall --apply "$@"
