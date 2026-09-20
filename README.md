# Portable Claude Code Fleet Bundle

Version `1.0.0` packages the portable parts of the global Claude Code setup: the
30-lane delegate fleet, generated native agents, routing hook, privacy-safe status
line, global delegation policy, model picker, conservative permissions/plugins
defaults, provider-aware discovery, and explicit setup policy command. The
directory can be copied or cloned anywhere and run from its new location; the
canonical build path is not required at install time.

## Platform support and prerequisites

| Platform / launcher | Status | Prerequisites and notes |
|---|---|---|
| Native Ubuntu/Linux | Supported | Python 3.10+; Node.js 18+ is required for fleet sync or explicit model discovery; Bash launchers are optional. |
| macOS | Supported | Python 3.10+; Node.js 18+ is required for fleet sync or explicit model discovery; use `install.sh` or Python. |
| Native Windows PowerShell | Supported | Python 3.10+; Node.js 18+ for fleet sync/discovery; use `install.ps1`, `update.ps1`, or `uninstall.ps1`. |
| Native Windows CMD | Supported | Python 3.10+ (`py -3` or `python`); Node.js 18+ for sync/discovery; use the matching `.cmd` wrapper. |
| Git Bash on Windows | Supported as POSIX compatibility mode | Requires Python 3.10+ and Node.js 18+ on PATH; native PowerShell/CMD entrypoints are preferred. |
| WSL | Supported as Linux | Uses the Linux installation and Linux home/config paths; native Windows and WSL targets are separate. |

The native Windows and POSIX entrypoints dispatch to the same Python installer. The
bundle root is inferred from the resolved installer file, so cloning into a path
with spaces or invoking through a symlink is supported. Python is authoritative;
Bash, PowerShell, and CMD files are only quoting-safe dispatchers. No Windows
runtime was available for execution in this validation environment, so the
PowerShell/CMD claims are backed by static syntax-safe entrypoints and path
construction tests, not a native Windows run.

## One-command installation

The installer never changes a machine without an explicit action. Preview first:

```bash
./install.sh --dry-run --home /tmp/claude-test --prefix /tmp/claude-test
```

Apply to a normal user account without a gateway. This selects the explicit
`direct-anthropic` profile: all 30 lanes use only first-party Claude model IDs,
gateway-only automatic routing is not installed, and model discovery remains
disabled. This mode is usable with a normal Anthropic login:

```bash
./install.sh --apply
```

For Linux/macOS, `XDG_CONFIG_HOME` is honored; use `--config-home DIR` when an
explicit configuration root is needed. A `--prefix DIR` sandbox is self-contained
and uses `DIR/.config` unless `--config-home` is supplied.

Enable the gateway only when both URL and a non-empty token are supplied. The
recommended non-interactive form reads the token from an environment variable and
never prints its value:

```bash
export MY_CLAUDE_GATEWAY_TOKEN='set this outside shell history when possible'
./install.sh --apply \
  --gateway-url 'https://gateway.example.invalid' \
  --gateway-token-env MY_CLAUDE_GATEWAY_TOKEN
```

If `--gateway-url` is supplied without `--gateway-token-env`, the installer asks
for the token with hidden input only when both stdin and stderr are real TTYs. A
non-interactive invocation fails rather than reading piped data or writing an
enabled gateway without a token. A token environment variable without a URL is
rejected. Gateway URLs must use HTTPS, may not contain embedded credentials, and
HTTP is accepted only for loopback (`localhost`, `127.0.0.1`, or `::1`) with the
explicit `--allow-insecure-http` flag.

Model discovery is not installed as a SessionStart hook by default. To opt in,
provide gateway credentials and `--enable-model-discovery`; the script itself also
requires `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`. Once the environment
value is enabled in installed settings that also carry a gateway URL and token,
either by this installer or by the user directly, subsequent installs and
updates preserve it in every profile; a fresh install still defaults to
disabled. For the same reason, the direct profile's first-party model
enforcement applies only when the installed settings carry no gateway
credentials. Discovery fetches and validates through the shared Python
provider catalog, scopes caches to endpoint/account, accepts successful empty
catalogs, and refuses incomplete pagination or stale caches. The ordered hook
then serializes drift reporting and fleet reconciliation under the shared lock.
Use `claude-fleet-setup --show` to inspect policy or pass a reviewed
`provider-policy-decisions.v1` JSON file to approve families/discovery; startup
never prompts. Discovery writes cache and settings files atomically with mode
0600 and re-reads under an exclusive lock.

For a disposable sandbox, `--prefix DIR` is a complete target home when `--home`
is omitted: configuration goes under `DIR/.claude` and `DIR/.config`, and command
wrappers under `DIR/bin`. When both are supplied, `--home` owns configuration and
`--prefix/bin` owns command wrappers. Relative paths are resolved from the current
working directory.

### Context compaction safety

The bundle keeps the top-level `autoCompactWindow` and the
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` environment setting paired and bounded. The
initial direct/gateway template uses `800000` as a conservative budget for the
advertised 872K route, below the observed 876273-token request envelope. At
SessionStart and PostModelSwitch, discovery reports the active model's supported
context and applies 90% headroom (for example, 272K becomes 244800); both paired
controls are written to the same value. An existing lower user budget is never
inflated. These are startup-only settings, so the hook emits a restart notice.

Claude Code still exposes one global auto-compact window rather than a native
model-conditional expression. The hooks therefore maintain a conservative paired
budget for the active verified route; they do not claim to change a running
session's limit. Unsupported or stale provider context leaves existing settings
unchanged, and later user-owned values remain authoritative through installer
updates.

The requested canonical destination is checked when this bundle is built or copied.
If the destination's parent is unavailable or unwritable, creation fails clearly;
the installer itself remains relocatable and does not require that canonical path.

## Actions

| Command | Effect |
|---|---|
| `./install.sh --dry-run` | Show creates, updates, conflicts, and gateway state; writes nothing and never prompts for a token. |
| `./install.sh --apply` | Install/update the bundle, taking a backup snapshot first. |
| `./update.sh --apply` | Explicit update alias; forwards installer options. |
| `./install.sh --check` | Run the doctor without changing files. |
| `./bin/claude-agents-doctor --check` | Check the bundle and the default target home. |
| `./uninstall.sh --dry-run` | Preview removal of setup-owned files and settings entries. |
| `./uninstall.sh --apply` | Remove setup-owned files while preserving unrelated settings. |
| `./install.sh --rollback --dry-run` | Preview the newest backup restore. |
| `./install.sh --rollback --apply` | Restore the newest backup, or pass `--backup DIR`. |

Existing files without the bundle's ownership marker are not overwritten. Use
`--force-owned` only after inspecting the dry run; all existing managed files are
copied to a mode-0700 backup directory before an apply, including unchanged files.
Repeated installs produce the same configuration and regenerate no duplicate hooks.
Backups are stored under `<home>/.claude/backups/claude-agents-config/`.

## What is installed

* `<home>/.claude/settings.json` — merged settings. Unknown top-level settings,
  unrelated permissions, hooks, plugins, and environment values survive. The fleet
  model picker and setup-owned hook entries are refreshed by stable ownership markers.
  The installed settings file is mode 0600 because it can contain a gateway token.
* `<home>/.claude/CLAUDE.md` — the global delegation policy. Generated agents omit
  this file intentionally so each brief must carry the applicable project rules.
* `<home>/.claude/route-to-fleet.py` — automatic UserPromptSubmit routing.
* `<home>/.claude/subagent-statusline.py` — native subagent status rendering.
* `<home>/.claude/sync-omniroute-models.mjs` — optional gateway model discovery. It
  is disabled by default and does nothing without both a gateway URL and token;
  when enabled, it delegates exact-ID normalization, pagination, cache scoping,
  and schema validation to `provider_catalog.py`, preserves every model required
  by the fleet, and runs drift detection/reconciliation in one ordered SessionStart
  transaction.
* `<home>/.claude/provider_catalog.py` and `fleet-reconcile.py` — shared provider
  identity/cache and approved-family reconciliation helpers. Reconciliation keeps
  custom lanes and unrelated picker rows, installs at most three fallback candidates,
  and reports pending decisions instead of silently approving a new family.
* `<home>/.claude/claude-fleet-setup.py` — explicit setup/reconfigure interview.
  It may prompt only in a TTY; hooks never invoke an interactive interview. Use
  `claude-fleet-setup --show` or a reviewed decision JSON for automation.
* `<home>/.claude/fleet-model-drift.py` — fail-open drift detector that writes a
  mode-0600 proposal and emits bounded SessionStart context. Approved families may
  be reconciled automatically when live and policy-approved; new families and
  proposal remaps require an explicit `claude-fleet-setup` approve/reject/supersede
  decision. It never silently escalates trust or cost policy.
* `<home>/.claude/sync-model-context.py` — PostModelSwitch/SessionStart hook that
  verifies the selected gateway model's real `context_length`, applies 90% headroom
  to `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, and keeps `autoCompactWindow` paired with
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW`. It never inflates a lower user budget, emits
  restart guidance for startup-only settings, never touches `claude-*` IDs, and is
  fail-open.
* `<home>/.claude/agents/fleet-*.md` — one generated agent per fleet lane. Each
  agent carries an optional native `fallbackModel` chain of at most three entries;
  it is intended for provider unavailability/overload only, not authentication,
  billing, rate-limit, request-size, transport, or policy-denial failures.
* `<config-home>/delegate-skills/config.json` and
  `generate-claude-agents.mjs` — the fleet map and portable generator. The
  config root is `XDG_CONFIG_HOME` on Linux/macOS, `<home>/.config` otherwise,
  or the explicit `--config-home` value.
* `<prefix>/bin/claude-fleet-sync` and `claude-agents-doctor` — POSIX wrappers,
  plus `.ps1` and `.cmd` native Windows entrypoints.
* `<home>/.claude/.claude-agents-config-manifest.json` — stable installed-tree
  verification data, so the installed doctor does not need the source clone.

Machine-local marketplace source paths are intentionally not copied because they
cannot be portable. Remote marketplace entries, enabled plugin selections,
permissions, model picker descriptions, and optional agent-brain hook stages are
retained when applicable.
Remote marketplace entries, enabled plugin selections, permissions, model picker descriptions, and optional
agent-brain hook stages are retained. Agent-brain hooks are guarded with
`command -v`; they are inert when that optional tool is not installed.

## Fleet behavior and model constraints

The fleet currently has 30 lanes. Counts are derived from the fleet map rather
than hardcoded in the installer/doctor. `fleet-plan` and `fleet-plan-alt` are read-only planning;
`fleet-implement` and numbered implement lanes are writable; `fleet-review`, static
diagnosis, security, repository research, and triage are read-only. `fleet-research-web`
has `WebSearch` and `WebFetch`; `fleet-research-codebase` is deliberately repository-only
and has neither web tool. Generated agents are background agents with
`omitClaudeMd: true`, cannot spawn nested agents, and must not commit, push, merge,
deploy, publish, or take other outward-facing actions. The main agent owns
integration, final gates, commits, releases, and deployments.

Every lane model must remain a member of `settings.json`'s `modelPicker.options`.
The doctor checks this invariant and the generator refuses to proceed if it drifts.
If model discovery runs, it preserves missing lane models as fallback picker rows. The fleet map may also declare an ordered `fallbacks` list; `claude-fleet-sync` uses the first live picker candidate without editing the map. `fleet-review` uses Codex Sol first, Claude Opus 5 second, and `fleet-review-06-astra` is reserved for very hard reviews. Approved families may reconcile automatically when live; a new family or explicit proposal remap remains pending until `claude-fleet-setup` records an approve/reject/supersede decision.
After changing the fleet map or picker, run:

```bash
claude-fleet-sync --check
claude-agents-doctor --check
```

Restart Claude Code or reload its agent registry after installing or changing agent
files. The generator's stable marker permits it to replace generated files while
refusing to overwrite hand-authored `fleet-*.md` files.

## Privacy and routing policy

The routing hook writes only to a mode-0600 JSONL log. Routed entries contain the
lane, reason, timestamp, and character count, never the prompt text. Only a
`no-match` entry keeps a maximum 160-character excerpt, after credential-shaped
values are redacted. Text excerpts are removed after 14 days; entries are removed
after 90 days. Purging runs at most daily. The active log rotates at 2,000,000 bytes
and the hook is fail-open: logging failure does not block a prompt. The doctor checks
these values in the packaged and installed hook.

## Prerequisites and trust

Python 3.10+ is required on every platform. Node.js 18+ is required before
fleet sync or explicit model discovery; it is not required for a default direct
profile apply when the generated agents are already present in the bundle. No
project npm or Composer dependency is used. Python's standard library and Node
built-ins are the only runtime libraries. Review `manifest.json`, the scripts, and
the generated agent frontmatter before applying on a new device. The generator,
routing hook, statusline, and installer are executable text files; no binaries are
bundled.

The gateway token is the only secret this setup normally needs. Keep it in a protected
environment or provide it through hidden prompt input. Do not put it in this bundle,
version control, shell history, command output, logs, or a README. Existing settings
may already contain credentials; the bundle's scanner intentionally scans the bundle,
not an installed settings file that the user deliberately configured.

## Checks and maintenance

The doctor validates JSON, executable bits, 30-lane fleet shape, model-picker
membership, generated-agent synchronization, frontmatter, research tool boundaries,
privacy constants, source-machine path leakage, and credential-shaped literals. It
also refuses an installed gateway URL without a token. `manifest.json` records SHA-256
checksums for every bundle file except the manifest itself; `checksums.sha256` is a
human-friendly copy of those records.

A safe update sequence is:

```bash
./install.sh --dry-run --home "$HOME"
./install.sh --apply --home "$HOME"
./bin/claude-agents-doctor --check --home "$HOME"
```

Rollback restores the most recent pre-change snapshot. Uninstall removes only files
and hook/permission entries owned by this bundle; it does not delete unrelated top-level
settings or plugin selections. A rollback backup is deliberately retained until the
operator removes it.
