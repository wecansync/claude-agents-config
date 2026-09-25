# Changelog

All notable changes to AgentFleet are documented here. Dates are UTC. This
file follows the [Keep a Changelog](https://keepachangelog.com/) format.

## [2.0.6] - 2026-09-25

Windows fixes: the one-command install works on the PowerShell built into
Windows, and updates repair hooks an older install left behind.

### Fixed
- The Windows one-command install (`irm https://agentfleet.wecansync.com/install.ps1 | iex`) works on Windows PowerShell 5.1, the version built into Windows. It stopped with an error when the Microsoft Store `python3` placeholder was on PATH, and it always reported Node.js 18+ as missing because PowerShell 5.1 drops the quotes in the version check. This also fixes `agentfleet update` and automatic updates on Windows machines without PowerShell 7.
- The Windows installer finds a Python installed with `uv python install` even when it is not on PATH. It never picks a project's virtual environment.
- On Windows, `agentfleet`, `claude-agents-doctor`, and `claude-fleet-setup` run with the Python the installer used, as the hooks already did, instead of whatever `py` or `python` finds on PATH. They had failed with "Python was not found" on machines whose only Python is not on PATH.
- An update refreshes managed hooks whose commands differ from the recorded install only in quoting or comment style, instead of treating them as your edits and never updating them again.
- An update no longer leaves a hook pointing at a script it has just removed. An edited hook that still runs a retired script, such as `sync-omniroute-models.mjs`, is replaced with the current hook and the installer says so. Your previous `settings.json` is in the backup snapshot.

## [2.0.5] - 2026-09-24

Any number of provider profiles: add and remove gateways without
reinstalling, and give each profile its own settings.

### Added
- `agentfleet add NAME --gateway-url URL [--token-env VAR] [--use] [--force] [--allow-insecure-http]` adds a gateway profile: it lists the gateway's models first and saves nothing on a bad token or an empty catalog.
- `agentfleet remove NAME [--yes]` removes a saved profile.
- Each profile now keeps its own provider policy, model pins, exclusions, and tier labels, instead of sharing one policy across every profile.
- Gateway setups set `ANTHROPIC_API_KEY` to an empty value, so an API key exported in your shell is not sent instead of the gateway token. A key you set yourself in `settings.json` is kept, with a warning.
- Re-running the installer with a `--gateway-url` for another gateway uses the settings saved in an `agentfleet` profile for it, or a generic policy, instead of carrying over the previous provider's model families and pins.
- `/fleet-setup` can add and remove providers without handling tokens in chat.
- README and docs cover the new commands and a table of supported gateways (OpenRouter, LiteLLM, Vercel AI Gateway, OpenCode Zen).

### Fixed
- Gateway tokens are handled more safely: model-list requests no longer follow redirects to another endpoint with the token attached, the install journal stores token digests instead of plaintext, a dry run with a new URL never sends the live token to it, and `--token-env` errors never echo the token value.
- A stale active-profile marker can no longer cause one gateway's setup to be saved over another gateway's profile.
- The installer decides the gateway URL and token together, so it never pairs one provider's URL with another's token. Re-running the original install command keeps a gateway you switched to with `agentfleet`.
- Uninstall keeps the API-key mask for a gateway you switched to, restores a pre-install gateway exactly, and no longer leaves a token behind without its URL.
- `agentfleet update` (including automatic updates) now succeeds after `agentfleet use native`, keeps model discovery on, and keeps your excluded models.
- The model-sync hook is always wired up and defers instead of failing if the provider changes while it waits.
- Cached model catalogs are only reused for the same endpoint and token.
- Installs from a group-writable git checkout work.
- Default profile names no longer collide with reserved Windows device names.

## [2.0.4] - 2026-09-23

`/fleet-setup` now interviews you model by model, and updates can install
themselves automatically.

### Added
- `/fleet-setup` suggests a complete fleet from each model's description, tier, reasoning support, and context size, or walks you through it model by model: pick lanes, skip the model, or exclude it entirely.
- `claude-fleet-setup --exclude/--include MODEL` keeps a model out of every lane while leaving it available in the `/model` picker.
- Automatic updates: a new release with the same major version installs itself in the background at most once a day, without delaying startup; a new major version gets a one-time notice instead. Turn it off with `agentfleet auto-update off` or `AGENTFLEET_AUTO_UPDATE=0`; check status with `agentfleet auto-update status`.
- `CONTRIBUTING.md` with setup, project rules, tests, versioning, security reporting, and the roadmap.

### Fixed
- A local `.claude/` directory no longer breaks installs made from a git clone.

## [2.0.3] - 2026-09-23

A redesigned website with full documentation, plus a missing CLI command.

### Added
- Redesigned landing page: OS-tabbed install commands, a wizard preview, and sections on why AgentFleet exists, features, how it works, the 19 lanes, providers and profiles, safety and privacy, requirements, and FAQ. Light and dark themes; works without JavaScript.
- Full documentation site at `/docs/`: install, the first-run wizard, installer flags, lanes, tiers and ranking, tuning, custom lanes, profiles, the command reference, `/fleet-setup`, installed files, context compaction, privacy, safety, upgrading, uninstall and rollback, troubleshooting, FAQ, and development.
- `agentfleet rollback [--backup DIR]`, which the README already documented but which didn't exist yet.

### Fixed
- The README, landing page, and docs now correctly say that writable lanes are only instructed, not technically prevented, from committing, pushing, or deploying; only read-only lanes are tool-restricted, and no lane can spawn nested agents.

## [2.0.2] - 2026-09-23

### Fixed
- `agentfleet update` works again. It had failed for everyone on 2.0.0 and 2.0.1 because the download used the default Python user agent, which Cloudflare rejected before the request reached the server.
- An update now removes preference values (effort level, output style, TUI mode, push notifications) that AgentFleet stopped shipping in 2.0.1, but only if you never changed them yourself; a value you changed is kept either way.

## [2.0.1] - 2026-09-23

### Changed
- Updating from 1.0.0 no longer carries over the old shipped model pins as if you had chosen them yourself; lanes stay free to pick the best model unless you pinned one.
- `CLAUDE.md` and settings no longer include the maintainer's personal preferences (commit/PR footers, the agent-brain memory section, a `claude-delegate` pointer, and UI preferences such as effort level, output style, and push notifications). Put your own instructions in `~/.claude/rules/*.md` instead, which Claude Code loads every session and AgentFleet never touches.

### Fixed
- The read-only check (`agentfleet doctor`) now covers all 19 lanes, including `review-alt`, `review-deep`, and `research-web`, and flags any custom lane marked read-only.
- Several edge cases found in an independent review: the one-time pin cleanup runs only once, saved profiles record their AgentFleet version so old profiles don't restore old pins, permission defaults are correctly handed back on update, and a malformed lane entry no longer breaks the doctor check.

## [2.0.0] - 2026-09-23

The project becomes AgentFleet: any Anthropic-compatible provider, role-named
lanes, provider profiles, and a one-command install.

### Added
- Any provider: a Claude subscription, an Anthropic API key, or any gateway that serves `GET /v1/models`.
- Tier-based model assignment: every discovered model gets a tier (cheap, fast, balanced, or deep), and each lane is ranked against live models by tier, with automatic fallbacks.
- A first-run wizard that asks how Claude Code should reach its models, discovers the gateway's models, and lets you correct tiers. Updates never prompt.
- Profiles: `agentfleet use native|<name>` switches between a gateway and your Claude login without reinstalling; lane names stay the same, only the models behind them change.
- A one-command install (`curl | sh` on macOS/Linux, `irm | iex` on Windows) that verifies a SHA-256 checksum and rejects unsafe archive paths.
- A landing page and full site at `agentfleet.wecansync.com`, built from reproducible release archives.
- MIT license.

### Changed
- 19 role-named lanes (`fleet-plan`, `fleet-implement`, `fleet-review`, ...) replace the 30 lanes that used to be named after models. Lane names never contain a model name.
- Upgrading from 1.0.0 renames or retires old lanes, carries over your per-lane choices, and keeps any custom lanes you added through `/fleet-setup`.

## [1.0.0] - 2026-09-18

The portable Claude agents configuration, published as `claude-agents-config`,
before the AgentFleet name. It received many small, unversioned improvements
through 2026-09-22:

### Added
- Initial release: a portable Claude Code agents configuration installed entirely under `~/.claude` and `~/.local/bin`, with no root or admin access required.
- Provider-aware fleet repair: model discovery, advisory fallback models, and reconciliation so the fleet adapts automatically as the gateway's model list changes, including an announcement when a model the fleet was using disappears.
- A `/fleet-setup` skill and a clean startup banner for reviewing and adjusting lane-to-model assignments.
- Automatic context-window sync when switching models, with a restart notice after a model switch changes the context budget.

### Fixed
- Bidirectional model-context scaling: switching from a large-context model to a small one and back no longer traps the compaction window at the smaller size.
- A bytecode-guard preflight check, manifest/fleet-hash sync, and the gateway catalog's user agent, each of which could otherwise break installs or model discovery.
- Windows compatibility: UTF-8 output across scripts and binaries.

[2.0.5]: https://github.com/wecansync/claude-agents-config/pull/20
[2.0.4]: https://github.com/wecansync/claude-agents-config/pull/19
[2.0.3]: https://github.com/wecansync/claude-agents-config/pull/18
[2.0.2]: https://github.com/wecansync/claude-agents-config/pull/17
[2.0.1]: https://github.com/wecansync/claude-agents-config/pull/16
[2.0.0]: https://github.com/wecansync/claude-agents-config/pull/15
