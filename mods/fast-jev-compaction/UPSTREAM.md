# Upstream

This mod is a copy of [fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction/tree/e3f262a7f4d42bd8dd32ced30d26176f7cb545b0)
by tamaratran, at commit `e3f262a7f4d42bd8dd32ced30d26176f7cb545b0`. It is licensed under the MIT License; see
`LICENSE` in this folder. Only the files the plugin runs are included: the
demo, examples, tests, npm packaging, and bundled type declarations are left out.

Changes from upstream:

- `hooks/fast-jev.ts`: the `turn.complete` hook does not request compaction
  when no TypeSafe API key is configured. Upstream requests it at
  `compactAtPercent` (60% by default) even without a key, and the compaction
  then falls back to Claude Code's built-in summary, so sessions compacted
  earlier than they otherwise would.

- `hooks/fast-jev.ts`: UI logs and notifications redact every occurrence of the
  resolved TypeSafe API key and strip control/format characters, including when
  remote response text appears in a compaction error.

Before you install it:

- It needs a TypeSafe API key (`TYPESAFE_API_KEY`, or the plugin's `apiKey`
  option), which is a paid third-party service.
- At each compaction it sends the conversation's text and tool inputs (tool
  results are replaced by short notes; about 25k tokens per request) to
  `https://api.typesafe.ai`.
- It requests compaction at 60% of the context window by default. AgentFleet
  compacts at about 80% on 1M-token models; set `compactAtPercent` to 80 to
  match.

AgentFleet lists it in its plugin catalog but does not install it. To use it:

    claude plugin install fast-jev-compaction@agentfleet
