---
name: fleet-setup
description: Set up, reconcile, and synchronize the delegate fleet based on provider models and proposals.
---

# Fleet Setup & Reconciliation

You are running the **fleet-setup** skill. Use this skill to inspect gateway provider models, review model drift proposals, reconcile delegate fleet lanes, and synchronize subagent definitions.

## When to Run This Skill
- A pending model proposal exists (`~/.claude/fleet-model-proposal.json`).
- Provider gateway models changed (models added, removed, or restored).
- The user requests to configure, review, or reconcile the 30 delegate fleet lanes.
- You need to verify agent synchronization or test provider connectivity.

## Workflow

### 1. Inspect Current Fleet & Proposals
Run the fleet inspection commands and read current state:
1. Check fleet status:
   ```bash
   claude-fleet-setup --status
   ```
2. Read the pending proposal if present (`~/.claude/fleet-model-proposal.json`):
   - Check if new models were discovered from the gateway.
   - Check if any models were removed or restored.
   - Check proposed lane adjustments (e.g. promoting `implement` to `codex-luna[1m]` or `review` to `codex-sol[1m]`).
3. Check current fleet configuration in `~/.config/delegate-skills/config.json`.

### 2. Present Summary & Review Options
Present a clear, formatted comparison table to the user:
- **Available Models**: Models currently live on the gateway.
- **Lane Changes**:
  - `Lane`: Name of the delegate fleet lane (e.g., `implement`, `plan`, `review`).
  - `Current Model`: Active model currently assigned.
  - `Target / Preferred Model`: Canonical preferred model or proposed upgrade/fallback.
  - `Status`: Auto-promoted, downgraded, or pending approval.
- **Pending Decisions**: Any unapproved model families or removed candidate models.

If the user has specific preferences (e.g. assigning a particular model to a lane or approving a family), adjust the plan accordingly.

### 3. Apply Reconciliation & Synchronize Agents
Execute the reconciliation:
1. Run live reconciliation:
   ```bash
   claude-fleet-setup --reconcile
   ```
   This will:
   - Fetch the latest live models from the provider gateway.
   - Evaluate persistent `preferred` hierarchies across all 30 lanes.
   - Auto-promote returning preferred models and downgrade removed models.
   - Prune stale models from `modelPicker` while preserving first-party/custom models.
   - Synchronize all 30 agent definition files (`~/.claude/agents/fleet-*.md`).

2. Verify agent synchronization:
   ```bash
   claude-fleet-sync --check
   ```

3. Verify fleet health:
   ```bash
   claude-agents-doctor --check
   ```

### 4. Report Final Status
Summarize the resulting active model assignments for primary workflows:
- `fleet-plan`: Planning & Architecture
- `fleet-implement`: Feature Implementation & Bug Fixing
- `fleet-review`: Code Review & Defect Auditing
- `fleet-tests`: Test Creation & Investigation
- `fleet-ui`: Interface & Layout
- `fleet-security-review`: Security & Authentication Review

Confirm that all 30 subagent definitions are updated, in sync, and ready for use.
