---
name: fleet-setup
description: Analyze current fleet against available provider models, show decisions, and prompt user with clear recommendations.
---

# Fleet Setup & Reconciliation

You are running the **/fleet-setup** skill.

## Core Directives
1. **Output must be very small**: Output ONLY the key decisions, recommendation, and a quick question via `AskUserQuestion`. Never print large tables, raw JSON, long logs, or verbose markdown.
2. **Strict & Actionable**: Tell the user exactly what will change so they know what to decide.
3. **Full Model Utilization**: The fleet must contain and benefit from all available gateway models across its 30 lanes.
4. **Interactive Decision**: Prompt the user with concrete options using `AskUserQuestion` before making changes.
5. **Immediate Execution**: Apply the user's decision, verify with `claude-fleet-sync --check`, and return a 1-2 line confirmation.

## Execution Flow

### Step 1: Analyze Fleet & Live Models (Silent)
1. Read live provider models from `~/.claude/cache/omniroute-models-cache.json` (or run `claude-fleet-setup --status`).
2. Read current fleet from `~/.config/delegate-skills/config.json`.
3. Compare:
   - Identify lanes currently on fallback models where higher-performing preferred models are live.
   - Check if any live gateway models are unused by any lane.
   - Check if any removed models need pruning.

### Step 2: Minimal Decision Summary & Prompt
Print a concise summary (max 4-5 lines):
- **Status**: e.g., "Current fleet: 30 lanes active. Gateway catalog: X live models."
- **Key Decisions**:
  - Promotions: Which primary lanes will upgrade to preferred models (e.g. `implement` -> `codex-luna[1m]`, `review` -> `codex-sol[1m]`).
  - Model coverage: All live models are mapped across primary and alternate lanes.
- **Recommendation**: State clear recommendation (e.g., "Recommended: Apply updates to maximize model performance and coverage.").

Immediately invoke `AskUserQuestion`:
- `header`: "Fleet Action"
- `question`: "Update fleet to recommended configuration or keep current?"
- `options`:
  - `label`: "Apply Updates (Recommended)"
    `description`: "Promote lanes to live preferred models and sync all 30 subagents."
  - `label`: "Keep Current Fleet"
    `description`: "Leave all current model assignments unchanged."
  - `label`: "Customize Specific Lane"
    `description`: "Manually adjust a specific lane assignment."

### Step 3: Execute Choice
- **Apply Updates**:
  1. Run `claude-fleet-setup --reconcile`.
  2. Run `claude-fleet-sync --check`.
  3. Output: "Fleet updated and synchronized: all 30 agents are ready."
- **Keep Current Fleet**:
  Output: "Fleet preserved unchanged."
- **Customize Specific Lane**:
  Ask which lane to edit, update `~/.config/delegate-skills/config.json`, run `claude-fleet-sync`, and confirm.
