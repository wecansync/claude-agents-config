# Project Activity Log

## 2026-09-18 — Initial portable bundle

Built a relocatable Claude Code configuration bundle containing 29 generated fleet agents, the delegation policy and fleet map, sanitized settings, generator, routing/privacy hook, statusline, optional model synchronizer, installer/update/uninstall/rollback actions, doctor, README, version, manifest, and SHA-256 checksums. Credentials and machine-local marketplace/plugin paths were omitted. Default model discovery remains disabled and gateway setup requires credentials.

Initial validation included a no-write dry-run, sandbox apply, installed doctor, fleet synchronization, repeat-install preservation, missing-token refusal, routing-log privacy checks, uninstall/rollback, syntax checks, checksum verification, and secret/path scanning.

## 2026-09-18 — Portability review and hardening

A follow-up review identified and addressed the main portability and safety gaps: native Windows entrypoints, bundle-root inference, platform home/config discovery, direct first-party model selection, unsafe permission defaults, transactional rollback, reversible settings/hook ownership, gateway URL/token policy, disabled-by-default model discovery, integrity preflight, installed-tree verification, XDG consistency, and LF/mode metadata.

The current hardening work remains uncommitted. Final validation must be rerun against the actual tree, and manifest/checksum files must be regenerated only after all source edits are complete. Native Windows cannot be executed on the current Linux host, so only static wrapper/path validation can be claimed there.
