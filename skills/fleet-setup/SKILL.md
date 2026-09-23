---
name: fleet-setup
description: Interactive AgentFleet wizard: audit live models and settings, repair stale models, validate hooks, switch provider profiles, assign models to lanes model by model, tune model tiers, and curate agents/skills.
---

# AgentFleet Setup Wizard

You are running **/fleet-setup**. Every number, model name, and count you show comes from the live audit; never quote examples from this file as facts.

## Rules
1. **Ask before changing anything.** Use `AskUserQuestion` for every decision. Put the recommended option first and add "(Recommended)" to its label.
2. **Keep output small.** Show a 4–6 line status, then questions. No raw JSON, no long tables.
3. **Curate, don't purge.** Keep broadly useful engineering agents. Suggest archiving only whole domains the user doesn't work in (game dev/3D, regional platforms). Never archive `fleet-*` lanes or the `fleet-setup` skill.
4. **Write through the tools, never by hand-editing JSON.** Use `claude-fleet-setup` and `agentfleet`; they hold the shared lock and keep both fleet mirrors in sync.
5. **Finish on gates, not self-report.** `claude-fleet-sync --check` and `claude-agents-doctor --check` must pass.

## Phase 1: Audit (silent)
Run `claude-fleet-setup --audit --json` and `agentfleet profiles`. From them, note:
- **Profile:** the active profile, and whether it is `native` (Claude login) or a gateway.
- **Live models:** from `live_models.models`: each model's `runtime_id` (the id lanes use; always pass this one to commands), `description`, `tier` (cheap / fast / balanced / deep), `context_length`, `reasoning`, `excluded`, and the lanes it serves (`primary_for`, `fallback_for`). Native profiles have no catalog; use `native_models` (`opus`, `sonnet`, `haiku`).
- **Lanes:** from `lanes`: each lane's `tier`, `read_only`, `alt_of`, `description`, current `model`, `fallbacks`, and `preferred` pins. Excluded models are in `excluded_models`.
- **Settings health:** stale `model`, `advisorModel`, or `env.ANTHROPIC_*_MODEL` values, and the compaction window.
- **Hooks:** any command whose executable or script is missing.
- **Fleet:** lane count, which lanes carry an explicit `preferred` list, and how many live models serve as a primary or fallback somewhere.
- **Agents and skills:** non-fleet agents grouped by category with their prompt-token cost; skills grouped by category.

## Phase 2: Status, then the wizard
Print the status, filling every value from the audit:
```
Profile <name> (<native | gateway host>) · <N> live models (<deep> deep, <balanced> balanced, <fast> fast, <cheap> cheap)
Settings <OK | stale: key=value, …> · compaction <value>
Hooks <n>/<n> healthy
Fleet <lanes> lanes · <used>/<N> models in use
Agents <non-fleet count> extra (~<tokens> tokens/turn) · Skills <count>
```

Then ask only the questions that apply, in this order.

**Step 1: Stale settings** (only if the audit found any). For each stale key, offer up to three live models of the matching tier (`advisorModel` and `ANTHROPIC_DEFAULT_OPUS_MODEL` → deep, `model` and `ANTHROPIC_DEFAULT_SONNET_MODEL` → balanced, haiku/small-fast → fast), plus "Keep unchanged". Apply with `claude-fleet-setup --fix-settings` or `--fix-settings --advisor <model>`.

**Step 2: Provider profile.** Ask whether to stay on the current profile or switch. Options come from `agentfleet profiles`; always include `native` ("Your Claude subscription or API key login").
- To switch, run `agentfleet use <name>`, then tell the user to restart Claude Code.
- To keep the current gateway for later, run `agentfleet save <name>`.

**Step 3: Fleet tuning.** Options:
- **Auto-rank all lanes (Recommended):** `claude-fleet-setup --reconcile`. Each lane gets the best live model for its tier, and the next best become its fallbacks.
- **Assign models to lanes:** suggest a full fleet, then walk through the models one by one. See below.
- **Fix model tier labels:** see below.
- **Keep current assignments.**

*Assign models to lanes.* Build the fleet with the user in four moves.
1. **Suggest a fleet.** For every lane, pick the best live, non-excluded model using the model's `description` first, then its `tier`, `reasoning`, and `context_length`, matched against the lane's `description` and `tier`. Cues: "strongest", "reasoning", "frontier" fit deep lanes (`plan`, `review`, `review-deep`, `security-review`, `diagnose-static`); "fastest", "cheapest", "mini", "flash" fit `explore-narrow`, `triage-static`, `implement-fast`; "free" or "budget" fit `implement-cheap`; coding or everyday models fit `implement`, `tests`, `ui`, `docs`; a context of 800K or more suits `research-codebase`. Read-only lanes favor reasoning models. An `-alt` lane must not share its sibling's model. Spread strong models so one outage does not take out every lane. Show the suggestion as one compact table: lane, suggested model, current model (mark rows that change).
2. **Ask:** "Apply this fleet (Recommended)" (run the apply command from move 4 right away; the table was the confirmation), "Go model by model", or "Cancel".
3. **Go model by model.** Order models deep → balanced → fast → cheap. Ask about up to 4 models per `AskUserQuestion` call, one question each, `multiSelect: true`. Question text: `<runtime_id>: <description> · <tier> · <context>`; header: a short model name (max 12 characters). Options, in order:
   - the two lanes that fit it best, the first marked "(Recommended)", each with a one-line reason;
   - "Skip": no pin; auto-ranking may still use the model as a primary or fallback;
   - "Don't use this model": exclude it from every lane.
   The user can type other lane names through "Other". A model may serve several lanes. Keep going until every model is answered.
4. **Confirm.** Show the resulting fleet: each lane with its chosen model(s), in the order they were assigned, or "auto" when nothing was chosen, plus the excluded models. Ask: "Apply (Recommended)", "Start over" (back to move 1), or "Cancel". To apply, run ONE command so the fleet reconciles once:
   `claude-fleet-setup --include <m> … --exclude <m> … --prefer <lane>=<model>[,<model>] …`
   - `--prefer` only lanes whose choice differs from their current `model`; lanes whose suggestion already matches stay on auto-ranking, so the fleet keeps adapting when models change.
   - Add `--prefer <lane>=` for lanes marked "auto" that currently have a `preferred` pin.
   - Add `--include` for previously excluded models the user assigned to a lane, and `--exclude` for "Don't use this model" answers.
   - Always use `runtime_id` values. On the native profile, only `opus`, `sonnet`, and `haiku` can be pinned or excluded.
   Pinned lanes still get fallbacks: after the user's own models, free fallback slots are filled by tier ranking.

`--prefer <lane>=` clears a pin so the lane goes back to tier ranking. `--exclude <model>` keeps a model out of every lane (it stays in Claude Code's `/model` picker); `--include <model>` undoes it.

*Tier labels:* the `-fast`, `-deep`, and `-cheap` lanes depend on them. Catalogs report context size but not price or speed, so tiers are guessed from names. Show the guessed tier of each live model, ask which to correct, and apply with `claude-fleet-setup --tier <model>=<tier>`.

**Step 4: Agent curation** (only if non-fleet agents exist). Show categories with counts and token cost. Options:
- **Keep engineering, archive unused domains (Recommended):** `claude-fleet-setup --archive-agents game_dev,niche_ops`.
- **Keep everything.**
- **Archive one category:** `--archive-agents <category>`.
- **Restore archived agents:** `--restore-agents <category|all>`.

**Step 5: Skills and new agents.** Options:
- **Archive legacy `*-delegate` wrappers:** `claude-fleet-setup --archive-skills delegates`. The native fleet lanes replace them.
- **Create a custom lane:** ask for a name (`fleet-<name>`), what it does, whether it is read-only, and its tier. Add the lane to `~/.claude/fleet.json` with `implementer: "claude"`, `tier`, `effort`, `readOnly`, and `description`. Then run `claude-fleet-setup --reconcile`, which picks its model and regenerates its agent.
- **Nothing else.**

## Phase 3: Apply, verify, summarize
Run the chosen commands, then:
1. `claude-fleet-sync --check`
2. `claude-agents-doctor --check`

If either fails, show its message and fix the cause before continuing. Finish with at most 4 lines: what changed, what was left alone, and whether both gates passed. Say "Restart Claude Code to apply" if a profile or settings changed.
