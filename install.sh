#!/usr/bin/env bash
set -euo pipefail

# Bash is only a compatibility dispatcher. Resolve the actual script with
# Python so a symlinked clone works on systems without readlink -f (including
# macOS), and so all installation behavior remains in the cross-platform Python
# implementation.
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
else
  printf '%s\n' 'claude-agents-config: Python 3.10 or newer is required.' >&2
  exit 1
fi
INSTALLER="$($PYTHON_BIN -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parent / "bin" / "install.py")' "${BASH_SOURCE[0]}")"
exec "$PYTHON_BIN" "$INSTALLER" "$@"
