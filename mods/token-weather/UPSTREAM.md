# Upstream

This mod is a copy of `token-weather` from Anthropic's
[claude-code-playground](https://github.com/anthropics/claude-code-playground/tree/e9ab132d4575390ecbadfc54649712432b1a3351/claude-code/mods/token-weather)
repository, at commit `e9ab132d4575390ecbadfc54649712432b1a3351`. Anthropic shares it as-is, without support.

It is licensed under the Apache License 2.0; see `LICENSE` in this folder.

Changes from upstream:

- `README.md`: the screenshot links point to the upstream repository, because
  AgentFleet does not ship the screenshots.

AgentFleet lists it in its plugin catalog but does not install it. To use it:

    claude plugin install token-weather@agentfleet
