# Contributing to AgentFleet

AgentFleet is maintained by WeCanSync and is MIT licensed. Contributions are
welcome. Read this guide before opening a PR.

- Public repo: https://github.com/wecansync/claude-agents-config
- Site and docs: https://agentfleet.wecansync.com / https://agentfleet.wecansync.com/docs/

---

## Requirements

- Python 3.10+
- Node.js 18+
- git

All runtime code uses only the Python standard library and Node.js built-ins.
Do not add third-party runtime dependencies.

---

## Set up for development

```bash
git clone https://github.com/wecansync/claude-agents-config.git
cd claude-agents-config
```

No build step is needed. The installer discovers its bundle from its own
location, so you can run scripts directly from the clone.

---

## Layout

```
bin/          install.py (installer), agentfleet, claude-fleet-setup,
              claude-agents-doctor (CLIs)
scripts/      provider_catalog.py, fleet-reconcile.py,
              generate-claude-agents.mjs, hook scripts
config/       delegate-fleet.json (fleet map), provider-policy.json,
              settings.template.json, CLAUDE.md (installed into ~/.claude/)
agents/       generated lane templates
skills/       /fleet-setup skill
tests/        test suite (tests/test_*.py)
packaging/    release tooling — never part of an installed bundle
site/         docs site — never part of an installed bundle
dist/         build output — never part of an installed bundle
.github/      CI config, when present — never part of an installed bundle
.claude/      local Claude Code state (settings, worktrees) — git-ignored, never bundled
install.*     one-command install wrappers (sh / ps1 / cmd)
uninstall.*   uninstall wrappers
update.*      update wrappers
VERSION       current version string
```

---

## Running the tests

```bash
python3 -m unittest discover tests
```

All tests must pass. Some tests require Node.js or a git repository and skip
when those are absent — a skip is not a pass.

When adding a test for new behavior or a bug fix, confirm the test fails
without your change. The easiest way is to stash or temporarily revert the
change, run the test, and verify it fails before restoring.

---

## Bundle integrity

The installer refuses a bundle whose `manifest.json` and `checksums.sha256` do
not match the files on disk. After editing any bundle file (anything outside
`site/`, `packaging/`, `dist/`, `.github/`, and `.claude/`), regenerate the manifest:

```bash
python3 packaging/update-manifest.py
```

The test file is itself part of the bundle, so run the manifest update before
running tests after any file change.

---

## Rules (read these carefully)

These rules are enforced by the doctor, the installer, or the test suite. PRs
that break them will not be merged.

1. **Lane names describe a role, never a model or vendor.** Use
   `fleet-implement-fast`, not `fleet-implement-gemini-flash`. Nothing in the
   bundle may hard-code a provider's model names as defaults.

2. **Stay provider-agnostic.** Model choice comes from tier ranking against the
   live catalog, explicit user preferences, or tier labels. Do not assume any
   specific provider.

3. **Read-only lanes must never receive `Bash`, `Edit`, `Write`, or `Agent`
   tools.** The doctor enforces this and will flag violations.

4. **Do not ship personal preferences in `config/CLAUDE.md` or
   `config/settings.template.json`.** Users keep their own instructions in
   `~/.claude/rules/`, which AgentFleet never touches.

5. **Never commit secrets or real tokens.** The installer scans the bundle for
   credential-shaped strings. Use obvious fixtures (e.g. `FAKE_TOKEN`,
   `TEST_KEY`) in tests.

6. **Installs must be safe: backup before every write, rollback-able,
   idempotent, and non-destructive.** The installer backs up all managed files
   before any write and never overwrites files it does not own. Changes to what
   the installer writes need a migration path for existing installs. See
   `LANE_RENAMES` and `RETIRED_LANES` in `bin/install.py` and
   `SHIPPED_1_0_PREFERRED` in `scripts/fleet-reconcile.py` for examples of
   how old state is carried forward or cleaned up.

7. **User-facing changes must update the docs.** Update `README.md` and the
   docs page `site/docs/index.html` (component markup lives in
   `packaging/site-components.md`). When changing `site/assets/site.css` or
   `site/assets/site.js`, bump the `?v=` query string wherever those files are
   referenced — Cloudflare caches assets aggressively.

---

## Versioning

The version is in `VERSION` and follows semantic versioning. Any change to a
bundle file requires a version bump in the PR because published releases are
immutable. Maintainers build and deploy releases; contributors do not:

```bash
# For reference only — maintainers run these, not contributors:
python3 packaging/build-release.py --ref <merge-commit>
export AGENTFLEET_DEPLOY_HOST=user@server
sh packaging/deploy.sh --dry-run
sh packaging/deploy.sh
```

---

## Git workflow

- Fork or branch from `main`.
- Keep each PR to one focused change.
- Use Conventional Commits with an optional scope:
  - `feat(agentfleet): ...`
  - `fix(install): ...`
  - `feat(site): ...`
  - `chore: ...`
- Include a short summary and a test plan in the PR description.

---

## Security

Report vulnerabilities privately through GitHub's Security tab ("Report a
vulnerability"). Do not open a public issue for a security report.

---

## Roadmap

The next phase is support for agent CLIs beyond Claude Code — OpenAI Codex CLI,
OpenCode, Kilo Code, and Cline — with lanes rendered in each tool's own agent
or mode format while keeping the same provider-agnostic lane model.

No design decisions have been made yet. If you are interested in contributing
to this area, open an issue to discuss the design before writing any code.

---

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
