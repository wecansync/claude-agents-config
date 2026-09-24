# AgentFleet

AgentFleet installs a fleet of native Claude Code subagents ("lanes") under
`~/.claude/agents/fleet-*.md` and maps each lane to whatever models your
provider offers: a Claude subscription, an Anthropic API key, or any
Anthropic-compatible gateway whose models are discovered from `GET /v1/models`.

It also installs a routing hook, a model-sync startup hook, a context-budget
hook, a privacy-safe status line, a global `CLAUDE.md`, and the `/fleet-setup`
skill. Everything lives under your home directory; no root or admin access is
required.

Version: **2.0.5** — MIT license. See [LICENSE](LICENSE).

> AgentFleet is an independent project by WeCanSync. It is not affiliated with
> or endorsed by Anthropic. Claude and Claude Code are trademarks of Anthropic.

---

## Requirements

- Claude Code
- Python 3.10+
- Node.js 18+

---

## Install

**macOS / Linux:**

```bash
curl -fsSL https://agentfleet.wecansync.com/install.sh | sh
```

**Windows PowerShell:**

```powershell
irm https://agentfleet.wecansync.com/install.ps1 | iex
```

The download script fetches `releases/latest.json`, downloads the pinned
release archive, verifies its SHA-256, extracts it to
`~/.local/share/agentfleet/releases/<version>` (Windows:
`%LOCALAPPDATA%\AgentFleet\releases\<version>`), then runs `bin/install.py`.

Pin a version with `AGENTFLEET_VERSION=2.0.0`. Override the host with
`AGENTFLEET_BASE_URL` (https only; http is accepted only for localhost).

From a clone, `./install.sh` (or `install.ps1` / `install.cmd`) does the same.

### First-run wizard

On a fresh interactive install the wizard asks "How should Claude Code reach
its models?":

1. **Claude subscription** (default) — uses Claude Code's own login; lanes map
   to `opus`, `sonnet`, `haiku`.
2. **Anthropic API key** — reads `ANTHROPIC_API_KEY` from the environment.
3. **Custom gateway** — asks for an https URL and the name of the environment
   variable that holds the token (default `AGENTFLEET_GATEWAY_TOKEN`); if
   unset, prompts with hidden input. It then discovers models, shows each
   model's tier, and offers to adjust tiers before installing.

Updates never run the wizard; they keep the installed profile.

### Automatic updates

Each time Claude Code starts, a quick hook checks at most once a day whether a
newer AgentFleet release exists. A newer release with the same major version
(for example 2.0.3 → 2.1.0) is installed in the background, with the same
checksum verification and backup as a manual update, so new sessions pick it
up. The next session mentions it in one line. A new major version is never
installed automatically; you get a one-time notice to run `agentfleet update`.
The hook never delays startup, and it is skipped for installs made from a git
clone.

```bash
agentfleet auto-update status   # enabled?, installed and latest versions, last result
agentfleet auto-update off      # stop automatic updates
agentfleet auto-update on       # turn them back on
```

`AGENTFLEET_AUTO_UPDATE=0` also turns them off (useful in CI). The state lives in
`~/.claude/agentfleet/auto-update.json` and each run's output in
`~/.claude/agentfleet/auto-update.log`.

### Non-interactive flags

```bash
# Gateway example (set the token first, outside shell history):
export MY_GATEWAY_TOKEN=…
curl -fsSL https://agentfleet.wecansync.com/install.sh | \
  sh -s -- --provider gateway \
            --gateway-url https://gateway.example.com \
            --gateway-token-env MY_GATEWAY_TOKEN
```

| Flag | Effect |
|---|---|
| `--provider native\|anthropic-api\|gateway` | Set the provider without the wizard. `gateway` also turns on startup model discovery. |
| `--gateway-url URL` | Gateway base URL (https required outside loopback). |
| `--gateway-token-env NAME` | Environment variable that holds the gateway token. |
| `--enable-model-discovery` | Turn on startup model discovery for a bare `--gateway-url` install (implied by `--provider gateway`). |
| `--no-wizard` | Never prompt; keep or infer the provider. |
| `--wizard` | Force the wizard even on an existing install. |
| `--dry-run` | Show changes without writing anything. |
| `--check` | Run the bundle doctor without changing files. |
| `--uninstall --apply` | Remove setup-owned files. |
| `--rollback --apply [--backup DIR]` | Restore the newest (or a named) backup. |
| `--home`, `--config-home`, `--prefix` | Override path roots. |

`AGENTFLEET_NONINTERACTIVE=1` disables all prompts.

---

## The 19 lanes

### Task lanes (12)

| Lane | Role | Writable |
|---|---|---|
| `fleet-plan` | Planning and architecture | No |
| `fleet-implement` | General implementation | Yes |
| `fleet-review` | Code review | No |
| `fleet-tests` | Test writing | Yes |
| `fleet-ui` | UI/frontend work | Yes |
| `fleet-docs` | Documentation | Yes |
| `fleet-security-review` | Security review | No |
| `fleet-diagnose-static` | Static diagnosis | No |
| `fleet-explore-narrow` | Bounded exploration | No |
| `fleet-triage-static` | Issue triage | No |
| `fleet-research-codebase` | Repository research (no web tools) | No |
| `fleet-research-web` | Web research (`WebSearch`, `WebFetch`) | No |

### Variant lanes (7)

Variants are named for a capability, never a model, so names survive provider
changes.

| Lane | Capability |
|---|---|
| `fleet-plan-alt` | Alternate planning model |
| `fleet-implement-alt` | Alternate implementation model |
| `fleet-implement-fast` | Low-latency implementation |
| `fleet-implement-deep` | Reasoning-capable implementation |
| `fleet-implement-cheap` | Budget or free-tier implementation |
| `fleet-review-alt` | Independent review model |
| `fleet-review-deep` | Deep review (security, migration, concurrency, data-loss) |

Read-only lanes receive no `Bash`, `Edit`, or `Write` tools, so they cannot
change files, commit, or push. All lanes are background agents with
`omitClaudeMd: true`, and none has the `Agent` tool, so none can spawn nested
agents. Writable lanes are instructed never to commit, push, deploy, or take
other outward-facing actions (a prompt rule, not a tool restriction). The main
agent owns integration, final gates, and all external actions.

---

## How lanes get models

Every discovered model gets a **capability tier**: `cheap`, `fast`, `balanced`,
or `deep`. The tier comes from, in priority order:

1. An explicit label set by the user.
2. Keywords in the model ID (e.g. `haiku`/`flash`/`mini` → fast;
   `opus`/`pro`/`max` → deep; `free`/`auto` → cheap).
3. Keywords in the catalog description.
4. `balanced` as the default.

Catalogs report context size but not price or speed, which is why explicit
labels exist.

Each lane has a required tier. At startup the reconcile step ranks all live
models per lane:

- Tier fit first; then reasoning support and 1M-context models rank higher.
- Staying on the current model is preferred, so a settled fleet does not
  reshuffle on every startup.
- Equally good models are spread across lanes; an `-alt` lane never shares its
  sibling's model.

The next-best models become each lane's fallbacks, so every live model can
serve somewhere regardless of lane count. A lane with an explicit `preferred`
list respects the user's order; its remaining fallback slots are still filled
by tier ranking, so a lane pinned to one model keeps somewhere to go. Models
you exclude never serve any lane (they stay in Claude Code's `/model` picker).

With a Claude subscription, lanes use `opus` / `sonnet` / `haiku` aliases.

---

## Tuning

```bash
# Pin a lane to a specific model (or list of fallbacks):
claude-fleet-setup --prefer fleet-implement=my-model-id
# Clear a pin:
claude-fleet-setup --prefer fleet-implement=

# Label a model's tier:
claude-fleet-setup --tier my-model-id=fast
# Clear a label:
claude-fleet-setup --tier my-model-id=

# Keep a model out of every lane, and undo it:
claude-fleet-setup --exclude my-model-id
claude-fleet-setup --include my-model-id

# Re-rank now:
claude-fleet-setup --reconcile
```

`/fleet-setup` inside Claude Code runs an interactive wizard over all of this:
stale-setting repair, profile switching, tier labels, and agent and skill
curation. Its **Assign models to lanes** option suggests a complete fleet from
each model's description, tier, and context size. You can apply it as is, or go
model by model: for each model it proposes the best-fitting lanes, and you pick
lanes, skip the model (auto-ranking decides), or exclude it. It shows the final
fleet for approval, or you can start over, and it pins only the lanes whose choice
differs from auto-ranking.

---

## Switching providers (profiles)

Profiles let you switch providers without reinstalling, and you can keep as
many as you like: one per gateway, plus `native` for your Claude login. Lanes
keep their names; only the models behind them change.

```bash
agentfleet profiles                  # list saved profiles (* = active)
agentfleet use native                # switch to your Claude login
agentfleet use <name>                # switch to a saved gateway profile
agentfleet save <name>               # name the current setup
agentfleet add <name> --gateway-url URL [--token-env VAR] [--use]
agentfleet remove <name> [--yes]     # delete a saved profile and its token
```

Switching first saves the current setup, so switching back restores it exactly.
Restart Claude Code after switching.

Each profile keeps its own:

- **provider policy** (`~/.config/delegate-skills/provider-policy.json`: the
  model families it approves, and the gateway's naming conventions),
- **lane pins**, **excluded models**, and **tier labels**.

A pin or exclusion you set on one provider does not follow you to another,
whose models have different names.

Gateway tokens are stored at `~/.claude/agentfleet/secrets/<name>.token`
(mode 0600, directory mode 0700). Profiles are stored under
`~/.claude/agentfleet/profiles/`.

To switch to `native`, AgentFleet removes `ANTHROPIC_BASE_URL`,
`ANTHROPIC_AUTH_TOKEN`, and the `ANTHROPIC_DEFAULT_*_MODEL` overrides, then
maps lanes to the `opus`/`sonnet`/`haiku` aliases. Gateway profiles set
`ANTHROPIC_API_KEY` to an empty string in `settings.json`, because Claude Code
would otherwise send an API key exported in your shell instead of the gateway
token. A non-empty key already in `settings.json` is yours and is left alone,
with a warning.

### Adding a provider

`agentfleet add` saves a new gateway profile without reinstalling:

```bash
export OPENROUTER_TOKEN=...          # or leave --token-env out to be prompted
agentfleet add openrouter --gateway-url https://openrouter.ai/api --token-env OPENROUTER_TOKEN
agentfleet use openrouter            # or pass --use to add
```

It lists the gateway's models first, and saves nothing if the token is
rejected or no model is available. Every lane is assigned one of that
gateway's models, under a generic policy that approves all of its models. The
token is read from the named environment variable, or typed at a hidden
prompt when you leave `--token-env` out; there is no flag that takes the token
itself, so it never lands in your shell history. `--force` replaces a saved
profile of the same name. `http://` is accepted only for localhost, with
`--allow-insecure-http`.

The gateway must accept Anthropic Messages requests and list its models at
`/v1/models`.

| Provider | `--gateway-url` | Notes |
|---|---|---|
| OpenRouter | `https://openrouter.ai/api` | Namespaced model ids (`anthropic/claude-sonnet-4.5`); the model list includes context sizes. |
| LiteLLM proxy | your proxy, e.g. `http://localhost:4000` | Its model list has no context sizes. |
| Vercel AI Gateway | `https://ai-gateway.vercel.sh/claude-code` | Claude Code support is documented; the model list endpoint is not, so `add` may be unable to list models. |
| OpenCode Zen | `https://opencode.ai/zen` | Claude models only on its Anthropic endpoint; the base URL is not documented. |
| OmniRoute or another Anthropic-compatible gateway | your URL | |

Re-running the installer with a different `--gateway-url` also switches
providers, but it cannot keep the old setup: it uses the policy and choices of
a saved profile for that endpoint if there is one, and otherwise the generic
policy without the old provider's pins and exclusions. Prefer
`agentfleet add`.

---

## Commands

| Command | Effect |
|---|---|
| `agentfleet profiles` | List saved profiles with each one's provider policy (`*` = active). |
| `agentfleet use <name>` | Switch to a saved profile, or `native` for your Claude login. `--force` re-applies the active one. |
| `agentfleet save <name>` | Save the current setup as a profile. |
| `agentfleet add <name> --gateway-url URL` | Add a gateway profile without reinstalling (`--token-env VAR`, `--use`, `--force`, `--allow-insecure-http`). |
| `agentfleet remove <name> [--yes]` | Delete a saved profile and its token. The active profile and `native` cannot be removed. |
| `agentfleet status` | Show the active profile and run the audit (live models, stale settings, hook health, agents and skills with token cost). |
| `agentfleet doctor` | Verify the installation (`claude-agents-doctor --check`). |
| `agentfleet sync` | Regenerate fleet agents from `~/.claude/fleet.json`; pass `--check` to verify without writing. |
| `agentfleet update` | Re-run the download script to install the latest release. |
| `agentfleet auto-update [on\|off\|status]` | Turn automatic background updates on or off, or show their status. |
| `agentfleet uninstall` | Remove AgentFleet and restore earlier settings. |
| `agentfleet rollback [--backup DIR]` | Restore the files and settings from before the last install or update (the newest backup, or a named one). |
| `agentfleet version` | Print the installed version. |
| `claude-fleet-setup --audit` | Scan models, settings, hooks, agents, and skills for health and bloat. |
| `claude-fleet-setup --archive-agents <categories>` | Archive agent files by category. |
| `claude-fleet-setup --restore-agents <categories>` | Restore archived agents. |
| `claude-fleet-setup --archive-skills` / `--restore-skills` | Archive or restore skills. |

---

## What gets installed

- `~/.claude/agents/fleet-*.md` — one generated agent per fleet lane, each with
  an optional fallback model chain (at most three entries, for provider
  unavailability or overload only).
- `~/.claude/settings.json` — merged settings. Unrelated permissions, hooks,
  plugins, and environment values are preserved. Mode 0600 because it can
  contain a gateway token.
- `~/.claude/CLAUDE.md` — the global delegation policy. AgentFleet manages this
  file and rewrites it on update; keep your own instructions in
  `~/.claude/rules/*.md`, which Claude Code loads in every session and
  AgentFleet never touches.
- `~/.claude/route-to-fleet.py` — UserPromptSubmit routing hook.
- `~/.claude/subagent-statusline.py` — native subagent status rendering.
- `~/.claude/sync-provider-models.mjs` — startup hook for gateway model
  discovery and fleet reconciliation.
- `~/.claude/sync-model-context.py` — PostModelSwitch/SessionStart hook that
  reads the active model's `context_length`, applies 90% headroom to
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, and keeps `autoCompactWindow` paired with
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW`. Never inflates a lower user budget. Fail-open.
- `~/.claude/provider_catalog.py` and `fleet-reconcile.py` — provider identity,
  cache, and approved-family reconciliation helpers.
- `~/.claude/fleet-model-drift.py` — fail-open drift detector that writes a
  mode-0600 proposal and emits bounded startup context.
- `~/.claude/claude-fleet-setup.py` — the setup/reconfigure tool. Prompts
  only in a TTY; hooks never invoke it interactively.
- `~/.claude/fleet.json` — the canonical fleet map.
  `~/.config/delegate-skills/config.json` is a legacy mirror kept in sync.
- `~/.config/delegate-skills/provider-policy.json` — provider policy. Generic
  (one catch-all family) on a fresh install; an existing install keeps its own
  provider-specific families and aliases on update, unless `--gateway-url`
  names a different gateway. `agentfleet use` swaps in each profile's own
  policy.
- Command wrappers in `~/.local/bin/` (POSIX) or equivalent (Windows).
- `~/.claude/.claude-agents-config-manifest.json` — installed-tree verification
  data, so the doctor does not require the source clone.

Machine-local marketplace source paths are not copied (they cannot be
portable). Remote marketplace entries, enabled plugin selections, permissions,
and model picker descriptions are preserved. `agent-brain` hooks are guarded
with `command -v` and are inert when that optional tool is absent.

---

## Context compaction

The context-budget hook keeps `autoCompactWindow` and
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` paired and bounded. At SessionStart and
PostModelSwitch it reads the active model's reported context, applies 90%
headroom (e.g. 272K context → 244800 budget), and writes both controls to the
same value. An existing lower user budget is never inflated. These are
startup-only settings; the hook emits a restart notice when they change.

---

## Privacy and routing policy

The routing hook writes only to a mode-0600 JSONL log. Routed entries contain
the lane, reason, timestamp, and character count — never the prompt text. Only
a `no-match` entry keeps a maximum 160-character excerpt, with
credential-shaped values redacted. Text excerpts are removed after 14 days;
entries after 90 days. Purging runs at most daily. The log rotates at
2,000,000 bytes. The hook is fail-open: logging failure does not block a
prompt. The doctor checks these values in the packaged and installed hook.

---

## Safety guarantees

- **Backups before every apply.** All managed files are copied to a mode-0700
  snapshot under `~/.claude/backups/claude-agents-config/` before any write.
- **Rollback.** `agentfleet rollback` (or `install.py --rollback --apply
  [--backup DIR]` from a clone) restores the newest pre-change snapshot;
  `agentfleet rollback --backup DIR` restores a named one. The backup is kept
  until you remove it.
- **Idempotent.** Repeated installs produce the same configuration and
  regenerate no duplicate hooks.
- **Non-destructive.** Uninstall removes only files and hook entries owned by
  this bundle. Unrelated top-level settings and plugin selections are not
  touched.
- **No accidental overwrites.** Existing files without the bundle's ownership
  marker are never overwritten without `--force-owned`.
- **Credential safety.** The scanner checks the bundle for credential-shaped
  literals. The gateway token is the only secret normally needed; keep it in a
  protected environment variable, not in version control, shell history, or
  logs.

---

## Upgrading from 1.0.0

The installer migrates automatically. It renames lanes that used to encode
vendor model names:

| Old lane name | New lane name |
|---|---|
| `implement-02-gemini-flash` | `implement-fast` |
| `implement-03-agy-opus` | `implement-deep` |
| `implement-04-free-1m` | `implement-cheap` |
| `implement-05-agy-sonnet` | `implement-alt` |
| `review-02-opus` | `review-alt` |
| `review-06-astra` | `review-deep` |

Other numbered lanes are retired. The old `sync-omniroute-models.mjs` hook
script is removed and replaced by `sync-provider-models.mjs`. The model cache
is now `~/.claude/cache/provider-models-cache.json`. All of this happens inside
the same backup/rollback transaction, so the upgrade is reversible.

---

## Development

```bash
# Run the test suite:
python3 -m unittest discover tests

# After editing any bundle file, update the manifest
# (the installer refuses a bundle whose manifest/checksums do not match):
python3 packaging/update-manifest.py

# Build a reproducible release from a git commit, then publish it:
python3 packaging/build-release.py --ref <commit>
# Produces: dist/releases/<version>/agentfleet-<version>.tar.gz, .zip,
#           SHA256SUMS, and dist/releases/latest.json
export AGENTFLEET_DEPLOY_HOST=user@server
sh packaging/deploy.sh --dry-run   # list every change and deletion first
sh packaging/deploy.sh             # upload site/, then archives, then latest.json
```

`deploy.sh` mirrors `site/` with `rsync --delete`, so it only syncs into a web
root that is empty or already carries the `.agentfleet-docroot` marker from an
earlier deploy. Published release archives are never deleted.

`site/`, `packaging/`, `dist/`, `.github/`, and a local `.claude/` are
repository-only and are never part of an installed bundle.

If the `agent-brain` CLI is on PATH at install time, agents receive its memory
tools and hooks. Otherwise they are omitted with no functional change.

---

## Contributing and roadmap

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, project rules, tests, and the
pull request process. The next phase is support for other agent CLIs besides
Claude Code (OpenAI Codex CLI, OpenCode, Kilo Code, Cline); open an issue to
discuss the design before sending code.
