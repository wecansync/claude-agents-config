# fleet-status

An AgentFleet band above the prompt shows actions worth taking: restart after an
update, inspect a failed update, or review missing models and pending model changes
with `/fleet-setup`. Healthy sessions show no band. **Hide** dismisses the current
notice set for this session; changed notices show again. Surveys take precedence.

`/fleet-status` prints a fresh version, profile, auto-update and model-proposal
summary. The band refreshes every 60 seconds.

The mod derives the Claude directory from its installed location and reads only:

- `.claude-agents-config-manifest.json`
- `agentfleet/active`
- `agentfleet/auto-update.json`
- `fleet-model-proposal.json`

Missing or malformed sources are treated as absent. No settings, profiles,
secrets, environment variables, processes or network are accessed. Nothing is
added to the model context unless you run `/fleet-status`.

Disable it with `claude plugin disable fleet-status@agentfleet`.
