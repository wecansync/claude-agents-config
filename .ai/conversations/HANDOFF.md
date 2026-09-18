# Handoff — Portable Claude Agents Configuration

## Active work

- **Portability hardening is in progress.** The repository contains a large uncommitted diff on `main` after `1aba2d7`. Preserve it; do not reset, commit, or push during recovery.
- The work targets only this bundle: Python installer/doctor, POSIX/PowerShell/CMD launchers, settings/fleet templates, routing/model-sync scripts, README, `.gitattributes`, manifest, and checksums.
- Prior implementation added a direct-Anthropic profile, gateway URL/token validation, HTTPS and loopback-HTTP rules, XDG/config-home handling, transactional snapshots, reversible ownership journals, installed-tree verification, and native Windows dispatchers.
- The remaining owner task is to inspect the current tree, finish any incomplete fixes, run the acceptance matrix, remove generated caches, and regenerate integrity metadata only after the final source is correct.

## Acceptance gates

1. `python3 -m py_compile bin/install.py bin/claude-agents-doctor`.
2. Node syntax checks for every JavaScript file and `git diff --check`.
3. Source doctor/checksum/manifest validation.
4. Dry-run from a different cwd, a symlink, and a path with spaces/Unicode.
5. Direct-profile sandbox apply → installed doctor → fleet sync check → repeat apply → uninstall.
6. Fresh-install failure/rollback removes files it created.
7. Missing gateway token and noninteractive token prompt refuse before target writes.
8. Non-loopback HTTP rejects; loopback HTTP passes only with explicit opt-in; explicit HTTPS gateway/discovery passes.
9. XDG/config-home selection, tampered-bundle preflight, unrelated settings/hooks/permissions preservation, and partial-write recovery.
10. Static PowerShell/CMD wrapper checks; do not claim native Windows runtime execution on Linux.

## Important limitations

A destination device still needs Claude Code itself, any desired remote plugins, and optional gateway/agent-brain services. The bundle does not contain credentials and cannot validate remote service reachability. Native Windows runtime validation remains unavailable in this environment.
