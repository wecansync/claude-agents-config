---
name: fleet-setup
description: Comprehensive interactive wizard: scan models & settings, repair stale models, validate hooks, curate agents/skills, and optimize the 30-lane fleet.
---

# Fleet Setup & System Optimization Wizard

You are running the **/fleet-setup** skill.

## Core Directives
1. **Interactive Multi-Step Wizard**: Use `AskUserQuestion` to collect user preferences and confirm recommendations before making any changes.
2. **Compact & Clean Output**: Print only a concise diagnostic summary (4-6 lines) and structured question prompts. Never dump raw JSON or sprawling tables.
3. **Smart Agent Curation (Do NOT Archive Everything)**: Separate valuable engineering tools (Database Optimizer, MCP Builder, Security Auditor, Accessibility Auditor, Data Remediation) from specialized domains (Game Dev, 3D, regional ops). Recommend keeping engineering tools active while archiving unneeded niche domains to reclaim prompt cache tokens.
4. **Deterministic Health & Stale Model Repair**: Identify and repair any model in `settings.json` or `env` that no longer exists in the provider gateway (e.g. `advisorModel: codex-sol-max[1m]`).
5. **Deterministic Verification Gates**: Conclude by synchronizing agents via `claude-fleet-sync` and verifying with `claude-fleet-sync --check` and `claude-agents-doctor --check`.

---

## Execution Flow

### Phase 1: Silent Diagnostic Audit
Run `claude-fleet-setup --audit --json` silently using `Bash`. Parse the JSON to inspect:
1. **Live Provider Models**: Total count, context lengths (1M vs 128K/256K), and active models.
2. **Settings.json Health**:
   - Check `advisorModel` and `model` against live catalog.
   - Check `env` model keys (`ANTHROPIC_DEFAULT_*_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`).
   - Check compaction window (`CLAUDE_CODE_AUTO_COMPACT_WINDOW` / `autoCompactWindow` vs `800000`).
3. **Hooks Integrity**: Verify all 11 hook commands and statusline scripts point to existing, executable binaries on disk.
4. **Installed Agents**:
   - 30 managed `fleet-*.md` lanes.
   - Non-fleet agents grouped into:
     - `engineering` (15 tools: Database Optimizer, MCP Builder, Security, Accessibility, Data Remediation, etc.)
     - `game_dev` (25 agents: Unity, Unreal, Godot, Roblox, Blender, XR)
     - `niche_ops` (2 agents: WeChat, Feishu)
     - `other` (6 agents: UX, Jira, Terminal, Voice AI, etc.)
5. **Installed Skills**: Active count by category (`core`, `delegates`, `engineering`, `other`).
6. **Fleet Coverage**: Percentage of live gateway models mapped into the 30 fleet lanes.

---

### Phase 2: Diagnostic Status & Interactive Wizard

Print a compact, high-signal diagnostic status (4-6 lines):
```markdown
### Fleet & System Audit
- **Gateway Models**: 13 online (1M context: Opus 5, Sonnet 5, Gemini Flash/Pro, Codex 5.5, Free 1M; 128K: Qwen 3.8).
- **Settings Health**: Stale model detected (`advisorModel: codex-sol-max[1m]`); Compaction at 235,929 (recommend 800,000 for 1M context).
- **Hooks Integrity**: 11/11 commands verified (all executables and script targets present on disk).
- **Installed Agents**: 30 Fleet lanes active; 15 Engineering tools (Database Optimizer, MCP Builder, Security, Accessibility, etc.); 25 Game Dev/3D agents; 2 Niche ops.
- **Fleet Coverage**: 30 lanes active · 100% gateway model coverage achievable.
```

Follow with the interactive wizard steps using `AskUserQuestion`:

#### Wizard Step 1: Settings Health & Stale Model Repair
If stale models are detected in `advisorModel`, `model`, or `env`, prompt the user:
- `header`: "Settings"
- `question`: "Stale model `codex-sol-max[1m]` detected in `advisorModel`. Which live model should replace it?"
- `options`:
  - `label`: "Replace with claude-opus-5[1m] (Recommended)"
    `description`: "Live 1M context Opus 5 for deep architectural guidance and advice."
  - `label`: "Replace with codex-5.5"
    `description`: "Live 272K context GPT-5.5 for high-precision reasoning."
  - `label`: "Replace with agy-claude-opus[1m]"
    `description`: "Live 1M context Opus 4.6 via Antigravity gateway."
  - `label`: "Keep Unchanged"
    `description`: "Leave existing settings.json configuration untouched."

*(Note: Claude will also align `CLAUDE_CODE_AUTO_COMPACT_WINDOW` and `autoCompactWindow` to 800,000 tokens for 1M context efficiency).*

#### Wizard Step 2: Fleet 30-Lane Architecture & Reconciliation
Prompt the user regarding fleet lane model assignments:
- `header`: "Fleet Mode"
- `question`: "How would you like to configure the 30-lane delegate fleet?"
- `options`:
  - `label`: "Auto-Reconcile 100% Coverage (Recommended)"
    `description`: "Map all 13 live models into the 30 lanes matching task shapes and context sizes."
  - `label`: "Interview Core Roles"
    `description`: "Interactively choose preferred models for the 5 core archetypes."
  - `label`: "Keep Current Fleet Assignments"
    `description`: "Preserve existing ~/.claude/fleet.json lane assignments."

**If the user selects "Interview Core Roles"**, prompt through the 5 archetypes:
1. **Main Orchestrator / Planner** (`fleet-plan`, `fleet-plan-alt`):
   - `claude-opus-5[1m] (Recommended)` (1M context deep reasoning), `agy-claude-opus[1m]`, `agy-gemini-pro[1m]`
2. **Lead Implementer** (`fleet-implement`):
   - `claude-sonnet-5[1m] (Recommended)` (1M coding), `agy-gemini-flash[1m]`, `agy-claude-sonnet[1m]`
3. **Code Reviewer** (`fleet-review`):
   - `claude-opus-5[1m] (Recommended)` (1M defect analysis), `codex-5.5`, `claude-sonnet-5[1m]`
4. **Decision Maker & Triage** (`fleet-triage-static`):
   - `qwen-3.8-128k-ctx (Recommended)` (Fast 128K triage), `claude-haiku`, `custom-auto`
5. **Researcher** (`fleet-research-codebase`, `fleet-research-web`):
   - `agy-gemini-pro[1m] (Recommended)` (1M broad sweep), `claude-sonnet-5[1m]`

#### Wizard Step 3: Categorized Agent Curation (Do NOT Archive Everything)
Explain that 15 Engineering tools provide immense utility for web, backend, database, and system development (Database Optimizer, MCP Builder, Security Auditor, Accessibility Auditor, Data Remediation, etc.). However, 25 Game Dev/3D agents (Unity, Unreal, Godot, Roblox, Blender) and 2 Niche ops (WeChat, Feishu) consume ~11,000 prompt tokens per turn if not working in those domains.

Prompt the user:
- `header`: "Agent Curation"
- `question`: "How should installed non-fleet agents be curated?"
- `options`:
  - `label`: "Keep Engineering & Archive Game/Niche (Recommended)"
    `description`: "Keep all 15 dev tools + 30 fleet lanes active; archive 27 game/niche agents to reclaim ~11K prompt tokens."
  - `label`: "Keep All 48 Agents Active"
    `description`: "Preserve all installed agents in session prompt without archiving."
  - `label`: "Archive Only Game Dev (25 agents)"
    `description`: "Move Unity, Unreal, Godot, Roblox, Blender, and XR agents to archive; keep everything else."
  - `label`: "Archive All Non-Fleet Agents"
    `description`: "Move all 48 non-fleet agents to archive, leaving only the 30 managed fleet lanes."

#### Wizard Step 4: Skills & Extension Strategy
Prompt the user regarding skills and custom agents:
- `header`: "Skills & Agents"
- `question`: "Would you like to adjust skills or generate a new custom delegate agent?"
- `options`:
  - `label`: "Apply All Approved Optimizations (Recommended)"
    `description`: "Execute the approved settings, fleet reconciliation, and agent curation choices."
  - `label`: "Archive Legacy Delegate Skills"
    `description`: "Archive 14 legacy *-delegate wrappers from before native fleet subagents."
  - `label`: "Suggest / Generate New Custom Agent"
    `description`: "Define and generate a new custom delegate agent with bounded tools."

**If "Suggest / Generate New Custom Agent" is selected**:
Prompt for agent name, role brief, and preferred model from the live catalog, then generate `~/.claude/agents/<agent-name>.md` with bounded tools and register it in `~/.claude/fleet.json`.

---

### Phase 3: Execution, Verification & Clean Summary

Execute the approved operations:
1. **Fix Settings**: Run `claude-fleet-setup --fix-settings --advisor <chosen_model>` (if repair was approved).
2. **Fleet Reconciliation**: Run `claude-fleet-setup --reconcile` (or write core role selections to `~/.claude/fleet.json`).
3. **Agent Curation**: Run `claude-fleet-setup --archive-agents <chosen_categories>` (e.g. `game_dev,niche_ops`).
4. **Skills Archiving**: Run `claude-fleet-setup --archive-skills delegates` (if chosen).
5. **Synchronization**: Run `claude-fleet-sync`.
6. **Deterministic Verification Gates**:
   - Run `claude-fleet-sync --check`
   - Run `claude-agents-doctor --check`

Output a crisp 3-4 line summary of actions taken and confirm all verification gates passed cleanly:
```markdown
### Fleet Setup Complete
- **Settings**: Repaired stale `advisorModel` to `claude-opus-5[1m]` and optimized compaction window to 800,000.
- **Fleet Lanes**: 30 lanes synchronized with 100% gateway model coverage.
- **Agent Curation**: Retained 15 high-value engineering tools; archived 27 specialized agents, saving ~11,000 prompt tokens/turn.
- **Verification**: `claude-fleet-sync --check` and `claude-agents-doctor --check` verified 100% healthy.
```
