# Portable Claude Code Fleet Bundle

Version `1.0.0` packages the portable parts of the global Claude Code setup: the
29-lane delegate fleet, generated native agents, routing hook, privacy-safe status
line, global delegation policy, model picker, permissions, plugins, and optional
model discovery. The canonical build location is
`/var/www/html/claude-agents-config/`; the directory can be copied elsewhere and
run from its new location.

## One-command installation

The installer never changes a machine without an explicit action. Preview first:

```bash
./install.sh --dry-run --home /tmp/claude-test --prefix /tmp/claude-test
```

Apply to a normal user account, with no gateway enabled:

```bash
./install.sh --apply
```

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
for the token with hidden input on a TTY. A non-interactive invocation fails rather
than writing an enabled gateway without a token. A token environment variable
without a URL is rejected. Gateway URLs may not contain embedded credentials.

For a disposable sandbox, `--prefix DIR` is a complete target home when `--home`
is omitted: configuration goes under `DIR/.claude` and `DIR/.config`, and command
wrappers under `DIR/bin`. When both are supplied, `--home` owns configuration and
`--prefix/bin` owns command wrappers. Relative paths are resolved from the current
working directory.

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
  when enabled, it never prints the token and preserves every model required by the fleet.
* `<home>/.claude/agents/fleet-*.md` — exactly 29 generated agents.
* `<home>/.config/delegate-skills/config.json` and
  `generate-claude-agents.mjs` — the fleet map and portable generator.
* `<prefix>/bin/claude-fleet-sync` and `claude-agents-doctor` — command wrappers.

The local `kai-research` marketplace and plugin entries from the source machine are
intentionally not copied because the marketplace source is a machine-local path.
Remote marketplace entries, enabled plugin selections, permissions, model picker descriptions, and optional
agent-brain hook stages are retained. Agent-brain hooks are guarded with
`command -v`; they are inert when that optional tool is not installed.

## Fleet behavior and model constraints

The fleet has 29 lanes. `fleet-plan` and `fleet-plan-alt` are read-only planning;
`fleet-implement` and numbered implement lanes are writable; `fleet-review`, static
diagnosis, security, repository research, and triage are read-only. `fleet-research-web`
has `WebSearch` and `WebFetch`; `fleet-research-codebase` is deliberately repository-only
and has neither web tool. Generated agents are background agents with
`omitClaudeMd: true`, cannot spawn nested agents, and must not commit, push, merge,
deploy, publish, or take other outward-facing actions. The main agent owns
integration, final gates, commits, releases, and deployments.

Every lane model must remain a member of `settings.json`'s `modelPicker.options`.
The doctor checks this invariant and the generator refuses to proceed if it drifts.
If model discovery runs, it preserves missing lane models as fallback picker rows.
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

Only ordinary Linux Bash, Python 3, and Node.js are required. No project npm or
Composer dependency is used. Python's standard library and Node built-ins are the
only runtime libraries. Review `manifest.json`, the scripts, and the generated agent
frontmatter before applying on a new device. The generator, routing hook, statusline,
and installer are executable text files; no binaries are bundled.

The gateway token is the only secret this setup normally needs. Keep it in a protected
environment or provide it through hidden prompt input. Do not put it in this bundle,
version control, shell history, command output, logs, or a README. Existing settings
may already contain credentials; the bundle's scanner intentionally scans the bundle,
not an installed settings file that the user deliberately configured.

## Checks and maintenance

The doctor validates JSON, executable bits, 29-lane fleet shape, model-picker
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
