---
name: fleet-setup
description: Analyze current fleet against available provider models, show decisions, interview core roles, or suggest new agents.
---

# Fleet Setup & Reconciliation

You are running the **/fleet-setup** skill.

## Core Directives
1. **Output must be very small**: Output ONLY the key decisions, recommendation, and a quick question via `AskUserQuestion`. Never print large tables, raw JSON, long logs, or verbose markdown.
2. **Strict & Actionable**: Tell the user exactly what will change so they know what to decide.
3. **Full Model Utilization**: The fleet must contain and benefit from all available gateway models across its 30 lanes.
4. **Interactive Decision**: Prompt the user with concrete options using `AskUserQuestion` before making changes.
5. **Immediate Execution**: Apply choices, synchronize agents via `claude-fleet-sync`, and verify with `claude-fleet-sync --check`.

## Execution Flow

### Step 1: Analyze Fleet & Live Models (Silent)
1. Read live provider models from `~/.claude/cache/omniroute-models-cache.json` (or run `claude-fleet-setup --show`).
2. Read current fleet configuration from `~/.claude/fleet.json` (or `~/.config/delegate-skills/config.json`).
3. Identify:
   - Available models on provider gateway.
   - Lanes on fallbacks that can be promoted to preferred models.
   - Ensure all live models are utilized across primary or alternate lanes.

### Step 2: Minimal Decision Summary & Prompt
Print a concise summary (max 4-5 lines):
- **Status**: e.g., "Current fleet: 30 lanes active. Gateway catalog: 13 live models."
- **Key Decisions**:
  - Promotions/Mappings: Primary lanes aligned with optimal live models.
  - Coverage: 100% gateway model utilization across all 30 lanes.
- **Recommendation**: State clear recommendation (e.g., "Recommended: Apply updates or customize core roles.").

Immediately invoke `AskUserQuestion`:
- `header`: "Fleet Action"
- `question`: "Select an action for the fleet configuration:"
- `options`:
  - `label`: "Apply Recommendations (Recommended)"
    `description`: "Auto-reconcile all 30 lanes to optimal live models and sync agents."
  - `label`: "Interview Core Roles"
    `description`: "Interactively pick models for the 5 core roles (Planner, Implementer, Reviewer, Triage, Researcher)."
  - `label`: "Suggest / New Agent"
    `description`: "Define and generate a new custom delegate agent."
  - `label`: "Keep Current Fleet"
    `description`: "Leave all current model assignments unchanged."

### Step 3: Handle Selected Action

#### A. Apply Recommendations:
1. Run `claude-fleet-setup --reconcile`.
2. Run `claude-fleet-sync --check`.
3. Output 1 line: "Fleet updated and synchronized: all 30 agents are ready."

#### B. Interview Core Roles:
Ask the user sequentially using `AskUserQuestion` with options derived from live available models:
1. **Main Orchestrator / Planner** (`fleet-plan`, `fleet-plan-alt`):
   - Options: `claude-opus-5[1m]` (Recommended), `codex-sol[1m]`, `agy-claude-opus[1m]`
2. **Lead Implementer** (`fleet-implement`):
   - Options: `codex-luna[1m]` (Recommended), `claude-sonnet-5[1m]`, `agy-gemini-flash[1m]`
3. **Code Reviewer** (`fleet-review`):
   - Options: `codex-sol[1m]` (Recommended), `claude-opus-5[1m]`, `codex-astra[1m]`
4. **Decision Maker & Triage** (`fleet-triage-static`):
   - Options: `qwen-3.8-128k-ctx` (Recommended), `claude-haiku`, `custom-auto`
5. **Researcher** (`fleet-research-codebase`, `fleet-research-web`):
   - Options: `agy-gemini-pro[1m]` (Recommended), `claude-sonnet-5[1m]`

After selections:
- Update the corresponding lane models in `~/.claude/fleet.json` (and `~/.config/delegate-skills/config.json`).
- Run `claude-fleet-sync`.
- Run `claude-fleet-sync --check`.
- Output: "Core roles updated and all 30 agents synchronized."

#### C. Suggest / New Agent:
1. Ask for:
   - Agent Name (e.g. `fleet-db-optimizer`, `fleet-mobile-qa`)
   - Role / Task Brief
   - Model (select from live models)
   - Scope (Read-only vs Writable)
2. Generate the agent definition at `~/.claude/agents/<agent-name>.md` with frontmatter:
   ```yaml
   ---
   name: <agent-name>
   description: <description>
   model: <model>
   effort: high
   tools: <tools-list>
   omitClaudeMd: true
   ---
   ```
3. Add the lane entry to `~/.claude/fleet.json` and sync.
4. Output: "New agent <agent-name> created and ready for delegation."

#### D. Keep Current Fleet:
Output: "Fleet preserved unchanged."
