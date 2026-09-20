#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ACTION="--apply"
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" || "$arg" == "--check" ]]; then
    ACTION=""
  fi
done
if [[ "$ACTION" == "" ]]; then
  exec "$SCRIPT_DIR/install.sh" --uninstall "$@"
fi
exec "$SCRIPT_DIR/install.sh" --uninstall "$ACTION" "$@"
