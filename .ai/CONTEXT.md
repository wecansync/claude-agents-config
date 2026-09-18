# Portable Claude Agents Configuration — Project Context

## Purpose

This repository packages the portable parts of a global Claude Code setup so a user can clone the repository into any local directory and apply the configuration without copying machine-specific paths or secrets.

## Bundle contents

- 29 generated `fleet-*` agent definitions.
- A portable delegation policy and fleet map.
- Sanitized Claude settings template and model-picker entries.
- Automatic prompt-to-fleet routing hook with privacy-safe retention.
- Subagent status-line helper.
- Optional gateway model synchronizer.
- Authoritative Python installer plus POSIX, PowerShell, and CMD dispatchers.
- Update, uninstall, rollback, and doctor/check commands.
- `VERSION`, `manifest.json`, and `checksums.sha256` integrity metadata.

The local marketplace/plugin source that existed only on the original machine is deliberately excluded. Credentials, bearer tokens, private connection strings, and machine-local paths must never be bundled.

## Required behavior

1. **Relocatable:** installation must work from any clone directory, including paths with spaces, Unicode, and symlinked entrypoints. The installer infers the bundle root from its own resolved file path.
2. **Cross-platform:** Python is authoritative. Linux/macOS use the shell dispatcher or Python; native Windows uses PowerShell/CMD dispatchers; Git Bash and WSL have explicit, honest support notes.
3. **Safe direct mode:** no-gateway apply selects only first-party Claude model IDs, does not install gateway-only automatic routing, and leaves model discovery disabled.
4. **Explicit gateway mode:** gateway URL and non-empty token are required together. HTTPS is required except loopback HTTP with an explicit insecure opt-in. Discovery requires explicit enablement and an environment opt-in.
5. **No unsafe defaults:** normal installation does not enable `bypassPermissions` or skip dangerous-mode prompts.
6. **Transactional apply:** integrity/runtime preflight completes before target writes; writes are staged and rollback restores the exact prior state, including paths that did not exist on a fresh install.
7. **Reversible ownership:** settings, permissions, hooks, gateway values, and generated files are journaled. Uninstall restores a prior value only when the current value is still the bundle-installed value, preserving later user edits. Hook changes are merged at inner-command granularity.
8. **Integrity:** manifest, checksums, inventory, version, and expected logical modes are validated before writes. Installed doctor validates the installed tree from local verification data and does not require the source clone.
9. **Consistent roots:** `--home`, `--prefix`, `--config-home`, and XDG paths are honored consistently by installer, doctor, fleet sync, generator, and model sync.
10. **Secret-safe model sync:** disabled by default; explicit opt-in only; atomic mode-0600 writes, locking, and re-read verification.

## Verification contract

Run and report exact exit codes for:

- Python and Node syntax checks plus `git diff --check`.
- Source doctor/checksum/inventory validation.
- Dry-run from a different cwd, through a symlink, and from a path containing spaces.
- Direct apply, installed doctor, fleet sync, repeat apply, uninstall, and fresh-install rollback.
- Missing gateway token refusal, piped-token refusal, non-loopback HTTP rejection, loopback HTTP explicit opt-in, and explicit HTTPS gateway/discovery.
- XDG config-home selection, tampered-bundle preflight before target writes, unrelated settings/hooks/permissions preservation, and partial-write recovery.
- Static PowerShell/CMD wrapper checks.

Native Windows runtime execution is not available in the current Linux environment; it must never be reported as actually run. Static wrapper/path checks are the available evidence.

## Current repository state

The portability-hardening diff is intentionally uncommitted on `main` after commit `1aba2d7`. Preserve the working tree; do not reset, commit, or push as part of context recovery. The active implementation lane is finishing validation and any remaining fixes before final integrity regeneration.

Known historical issue: after installer/doctor edits, manifest and checksum entries became stale and a generated `bin/__pycache__` appeared once. Final source changes must be complete before regenerating integrity metadata, and generated caches must be removed afterward.

## Scope boundary

This repository owns only the portable configuration bundle. Do not copy or restore unrelated application source, application handoffs, application decisions, or application logs from another project.
