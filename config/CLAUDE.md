<!-- claude-agents-config:managed -->

# Global Delegation Policy

Delegate substantive work to the matching `fleet-*` subagent as a standing default. Read the request, match it to a lane, and dispatch — the user never names a lane. Say which lane you dispatched and why in one line.

## Route by task shape

| The request is | Lane |
| --- | --- |
| Design or approach before code exists | `fleet-plan` |
| Build a feature, fix a bug, refactor, migrate | `fleet-implement` |
| Check a diff, branch, PR, plan, or spec for defects | `fleet-review` |
| Write, fix, or investigate tests | `fleet-tests` |
| Change user-facing interface code | `fleet-ui` |
| Write or update documentation | `fleet-docs` |
| Find a symbol, file, config, or call site | `fleet-explore-narrow` |
| Examine auth, secrets, validation, or trust boundaries | `fleet-security-review` |
| Root-cause a failure from source | `fleet-diagnose-static` |
| Sweep many files to understand a system | `fleet-research-codebase` |
| Evaluate an external tool, library, or service, or check current docs | `fleet-research-web` |
| Decide ownership, scope, and next action for an issue | `fleet-triage-static` |

Do the work inline when it is a single obvious edit, a question already answered by context, or a command whose output you need for your next step. Dispatch when the task spans several files, needs real digging, or benefits from an independent model.

Chain lanes when the work has stages: plan, then implement, then review. Run a second reviewer on a risky change. Numbered lanes (`fleet-implement-04-*`, `fleet-review-03-*`) are alternates — reach for one when the user names it, when a primary lane has failed twice, or when a genuinely independent model improves the check; state the reason.

## Own the outcome

You own decomposition, briefs, integration, independent verification, commits, pushes, pull requests, releases, deployments, and every other outward-facing action.

Each brief states the goal, scope, exclusions, applicable project instructions, verification commands, and report contract. Fleet agents omit CLAUDE.md to keep their context bounded, so the brief is their entire instruction boundary. They report touched files, commands with exit codes, measured outcomes, and unresolved risks — treat every report as a claim until you inspect the tree and rerun the checks yourself.

Parallel writable agents need disjoint file scopes or isolated worktrees. Static reviewers run in parallel freely. Fleet agents never spawn nested agents.

## Keep the fleet honest

`~/.config/delegate-skills/config.json` is canonical; `~/.claude/agents/fleet-*.md` is generated from it. Run `claude-fleet-sync` after editing the fleet, and `claude-fleet-sync --check` must pass before dispatch. Reserve `claude-delegate` for work that needs a separate durable CLI session.
