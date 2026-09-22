---
name: fleet-setup
description: Analyze live models, audit agents/skills/plugins for token bloat, optimize config, interview core roles, or suggest new agents.
---

# Fleet Setup & Context Optimization

You are running the **/fleet-setup** skill.

## Core Directives
1. **Output must be very small**: Output ONLY concise diagnostic status (3-5 lines), clear recommendations, and an interactive prompt via `AskUserQuestion`. Never dump large tables, raw JSON, long logs, or verbose markdown.
2. **Context & Token Economy**: Extra non-fleet agents, redundant skills, and bloated plugins inject massive prompt overhead into every session. Audit them and suggest pruning.
3. **Full Model Utilization**: The fleet must contain and benefit from all available gateway models across its 30 lanes, matching task shapes to context lengths and strengths.
4. **Interactive Decision Before Acting**: Always present recommendations and ask before mutating files or pruning configurations.
5. **Immediate Execution & Verification**: Apply choices, synchronize agents via `claude-fleet-sync`, and verify with `claude-fleet-sync --check`.

## Execution Flow

### Step 1: Deep System & Context Audit (Silent)
Perform the following diagnostics silently before printing output:
1. **Live Provider Models**:
   - Read `~/.claude/cache/omniroute-models-cache.json` (or inspect `/v1/models` via gateway endpoint in `~/.claude/settings.json`).
   - Extract model IDs, descriptions, and context lengths (e.g. 1M vs 128K vs 256K).
2. **Fleet Configuration**:
   - Read `~/.claude/fleet.json` (fallback: `~/.config/delegate-skills/config.json`).
   - Identify lanes on fallback models that can be promoted to optimal live models.
   - Verify 100% gateway model utilization across all 30 lanes.
3. **Context & Token Overhead Audit**:
   - **Agents (`~/.claude/agents/`)**: Count total agents vs. managed `fleet-*.md` agents. Non-fleet agents inject full names and descriptions into the session prompt on every turn, consuming prompt tokens and degrading cache.
   - **Skills (`~/.claude/skills/`)**: Identify user-defined skills and detect stale or unused ones.
   - **Plugins / MCP**: Check `~/.claude/settings.json` for active plugins or MCP servers adding tool schema overhead.
   - **Compaction & Limits**: Verify `CLAUDE_CODE_AUTO_COMPACT_WINDOW` in `~/.claude/settings.json` is configured appropriately for active context sizes (e.g. `800000` for 1M context models).

### Step 2: Minimal Diagnostic Summary & Prompt
Print a compact, high-signal summary (max 5-6 lines):
- **Gateway Catalog**: e.g., "13 live models detected (1M context: Opus 5, Sonnet 5, Codex Luna, Codex Sol, Gemini Pro/Flash; 128K: Qwen 3.8)."
- **Fleet Status**: e.g., "30 lanes active. 100% model coverage achievable."
- **Context Overhead**: e.g., "Detected X non-fleet agents and Y custom skills injecting ~Z tokens into every session start."
- **Recommended Action**: e.g., "Recommended: Auto-optimize fleet model assignments and prune unused context bloat."

Prompt the user immediately using `AskUserQuestion`:
- `header`: "Fleet Action"
- `question`: "Select an action for fleet setup and context optimization:"
- `options`:
  - `label`: "Apply Full Optimization (Recommended)"
    `description`: "Reconcile all 30 lanes to optimal live models, sync agents, and ensure optimal compaction config."
  - `label`: "Interview Core Roles"
    `description`: "Interactively choose preferred models for the 5 core archetypes based on live models and context."
  - `label`: "Audit & Prune Token Bloat"
    `description`: "Review and remove or archive non-fleet agents/skills/plugins that inject unnecessary context."
  - `label`: "Suggest / New Agent"
    `description`: "Define and generate a new custom delegate agent with bounded tools."
  - `label`: "Keep Current Configuration"
    `description`: "Leave all current assignments, agents, and settings untouched."

### Step 3: Handle Selected Action

#### A. Apply Full Optimization:
1. Run `claude-fleet-setup --reconcile`.
2. Ensure `~/.claude/settings.json` has optimal compaction (`CLAUDE_CODE_AUTO_COMPACT_WINDOW` = `800000`).
3. Run `claude-fleet-sync --check`.
4. Output 2 lines:
   - "Fleet reconciled to live provider models with 100% model coverage."
   - "All 30 delegate agents synchronized and verified."

#### B. Interview Core Roles:
Ask the user sequentially using `AskUserQuestion` populated with the live models, showing model name, context length, and role fit:
1. **Main Orchestrator / Planner** (`fleet-plan`, `fleet-plan-alt`):
   - Options: `claude-opus-5[1m]` (Recommended, 1M context deep reasoning), `codex-sol[1m]` (1M architecture), `agy-claude-opus[1m]`
2. **Lead Implementer** (`fleet-implement`):
   - Options: `codex-luna[1m]` (Recommended, 1M precision coding), `claude-sonnet-5[1m]` (1M coding), `agy-gemini-flash[1m]` (Fast 1M)
3. **Code Reviewer** (`fleet-review`):
   - Options: `codex-sol[1m]` (Recommended, 1M defect analysis), `claude-opus-5[1m]`, `codex-astra[1m]`
4. **Decision Maker & Triage** (`fleet-triage-static`):
   - Options: `qwen-3.8-128k-ctx` (Recommended, fast 128K triage), `claude-haiku`, `custom-auto`
5. **Researcher** (`fleet-research-codebase`, `fleet-research-web`):
   - Options: `agy-gemini-pro[1m]` (Recommended, 1M web & broad repo analysis), `claude-sonnet-5[1m]`

After selections:
- Update `~/.claude/fleet.json` and mirror `~/.config/delegate-skills/config.json`.
- Run `claude-fleet-sync`.
- Run `claude-fleet-sync --check`.
- Output: "Core roles updated, fleet synchronized, and verified."

#### C. Audit & Prune Token Bloat:
1. List non-fleet agents in `~/.claude/agents/` (excluding `fleet-*.md`).
2. Calculate estimated prompt token overhead.
3. Prompt via `AskUserQuestion`:
   - "Archive All Non-Fleet Agents": Move unused third-party agents to `~/.claude/agents-archived/` so they stop polluting session context.
   - "Selectively Prune": Choose specific agents to delete or archive.
   - "Keep All": Preserve existing agent files.
4. After user confirmation, execute the action and output:
   - "Context pruned: removed X extra agents from session prompt overhead."

#### D. Suggest / New Agent:
1. Prompt for:
   - Agent Name (e.g. `fleet-db-optimizer`, `fleet-api-tester`)
   - Role Brief & Scope (Read-only vs Writable)
   - Preferred live model (from gateway catalog)
2. Create `~/.claude/agents/<agent-name>.md` with minimal, bounded frontmatter:
   ```yaml
   ---
   name: <agent-name>
   description: <description>
   model: <model>
   effort: high
   tools: <bounded-tools>
   omitClaudeMd: true
   ---
   ```
3. Register the lane in `~/.claude/fleet.json` and run `claude-fleet-sync`.
4. Output: "New agent <agent-name> registered and ready for delegation."

#### E. Keep Current Configuration:
Output: "Configuration preserved unchanged."

