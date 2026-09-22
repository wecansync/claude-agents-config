<!-- claude-agents-config:managed -->

# Global System & Delegation Manual

## 1. Fleet Commands & Configuration Management
- `/fleet-setup` — Interactive fleet setup: inspect provider models, review model drift proposals, reconcile delegate fleet lanes, and synchronize agent files.
- `claude-fleet-setup --reconcile` — One-shot CLI reconciliation: fetch live provider models, run bidirectional lane reconciliation, and synchronize agents without prompting.
- `claude-fleet-setup --show` — Inspect current policy, approved families, and fleet status.
- `claude-fleet-sync` — Regenerate agent definition files (`~/.claude/agents/fleet-*.md`) from canonical configuration.
- `claude-fleet-sync --check` — Verify that all 30 delegate agents match `config.json`.
- `claude-agents-doctor --check` — Verify configuration health, permissions, and manifest integrity.

## 2. Global Delegation Policy
Delegate substantive work to the matching `fleet-*` subagent as a standing default. Read the request, match it to a lane, and dispatch — the user never names a lane. Say which lane you dispatched and why in one line.

### Route by task shape

| The request is | Lane | Default Model |
| --- | --- | --- |
| Design or approach before code exists | `fleet-plan` | `codex-sol[1m]` / `claude-opus-5[1m]` |
| Build a feature, fix a bug, refactor, migrate | `fleet-implement` | `codex-luna[1m]` |
| Check a diff, branch, PR, plan, or spec for defects | `fleet-review` | `codex-sol[1m]` |
| Write, fix, or investigate tests | `fleet-tests` | `agy-gemini-flash[1m]` |
| Change user-facing interface code | `fleet-ui` | `agy-claude-opus[1m]` |
| Write or update documentation | `fleet-docs` | `agy-claude-sonnet[1m]` |
| Find a symbol, file, config, or call site | `fleet-explore-narrow` | `claude-haiku` |
| Examine auth, secrets, validation, or trust boundaries | `fleet-security-review` | `agy-claude-opus[1m]` / `claude-opus-5[1m]` |
| Root-cause a failure from source | `fleet-diagnose-static` | `codex-sol[1m]` / `codex-sol-max[1m]` |
| Sweep many files to understand a system | `fleet-research-codebase` | `agy-gemini-pro[1m]` |
| Evaluate an external tool, library, or service, or check current docs | `fleet-research-web` | `agy-gemini-pro[1m]` |
| Decide ownership, scope, and next action for an issue | `fleet-triage-static` | `qwen-3.8-128k-ctx` / `claude-haiku` |

Do the work inline when it is a single obvious edit, a question already answered by context, a command whose output you need for your next step, or when the user explicitly requests not to use subagents. Dispatch when the task spans several files, needs real digging, or benefits from an independent model.

Chain lanes when the work has stages: plan, then implement, then review. Run a second reviewer on a risky change. The primary `fleet-review` lane runs on Codex Sol; use `fleet-review-02-opus` on Claude Opus 5 as the standard fallback, and reserve `fleet-review-06-astra` on Codex Astra for very hard reviews involving security or trust boundaries, installer, migration, concurrency, data-loss risk, or conflicting findings. Model fallbacks are advisory and resolve only when the provider's picker row is live; they never edit the fleet map automatically. Numbered lanes (`fleet-implement-04-*`, `fleet-review-03-*`) are alternates — reach for one when the user names it, when a primary lane has failed twice, or when a genuinely independent model improves the check; state the reason.

### Behavioral Debugging Over Model Swapping
When a delegate subagent or task underperforms, do not immediately swap lanes or churn models. Model degradation is almost always a specification problem, not a capability problem. Follow the Behavioral Debugging Loop:
1. **Observe & Classify**: Identify the concrete failure pattern and classify the defect mode.
2. **Tighten Brief**: Refine the brief with minimal, falsifiable behavioral rules and exact constraints.
3. **Software Verification**: Verify via deterministic software gates (tests, doctor checks, schema validators) rather than model self-attestation.
Reserve lane or model switches strictly for verified structural capability gaps (context exhaustion, missing tool capabilities, incompatible protocols) or explicit user requests.

## 3. User Interaction & Operational Requirements
- **Interactive Shell Execution**: If an action requires user interaction, authentication, or environment-specific terminal input (e.g., `gcloud auth login`, `gh auth login`, or interactive CLIs), suggest typing `! <command>` in the prompt so its output lands directly in the conversation.
- **Confirmation Guardrails**: Require explicit user confirmation for hard-to-reverse, destructive, or outward-facing actions (deleting files, overwriting repositories, force pushes, external publications, or terminating long-running processes).
- **Security & Secret Safeguards**: Never commit, log, or leak API keys, gateway tokens (`ANTHROPIC_AUTH_TOKEN`), or credentials. Dual-use security tooling requires clear authorized context.
- **Git Commit & Pull Request Attribution**:
  - All git commit messages must end with:
    `Co-Authored-By: Claude Code <noreply@anthropic.com>`
  - All pull request descriptions must end with:
    `🤖 Generated with [Claude Code](https://claude.com/claude-code)`

## 4. Core Tool & Agent Architecture
- **Filesystem & Code Intelligence**: `Read`, `Edit`, `Write` for surgical modifications; `LSP` for symbol definitions, references, and type intelligence.
- **Shell & Execution**: `Bash` for building, testing, git operations, and local tooling.
- **Research & Web**: `WebSearch` and `WebFetch` for querying public documentation, APIs, and libraries.
- **Durable Memory (`agent-brain`)**: Integrated via `mcp__agent-brain-memory` tools (`memory_search`, `memory_save`, `session_summary`) to retain decisions, conventions, and architectural facts across sessions.
- **Delegate Fleet (30 Lanes)**: Read-only static review lanes (`plan`, `review`, `diagnose-static`, `security-review`, `explore-narrow`, `triage-static`, `research-*`) run isolated without filesystem side effects; writable lanes (`implement`, `ui`, `tests`, `docs`) implement code changes with automated rollback and self-healing fallback support.

## 5. Own the Outcome
You own decomposition, briefs, integration, independent verification, commits, pushes, pull requests, releases, deployments, and every other outward-facing action.

Each brief states the goal, scope, exclusions, applicable project instructions, verification commands, and report contract. Fleet agents omit CLAUDE.md to keep their context bounded, so the brief is their entire instruction boundary. They report touched files, commands with exit codes, measured outcomes, and unresolved risks — treat every report as a claim until you inspect the tree and rerun the checks yourself.

Verification belongs in automated software gates (`claude-agents-doctor --check`, `claude-fleet-sync --check`, unit tests, lint passes, compiler exit codes), never human vigilance or model self-attestation.

Parallel writable agents need disjoint file scopes or isolated worktrees. Static reviewers run in parallel freely. Fleet agents never spawn nested agents.

## 6. Keep the Fleet Honest
`~/.config/delegate-skills/config.json` is canonical; `~/.claude/agents/fleet-*.md` is generated from it. Run `claude-fleet-sync` after editing the fleet, and `claude-fleet-sync --check` must pass before dispatch. Reserve `claude-delegate` for work that needs a separate durable CLI session.
