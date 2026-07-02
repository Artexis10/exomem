# add-setup-wizard

## Why

The documented local onboarding is seven manual steps (~20–30 min) across two
docs, and it assumes a fresh vault — a real first-time user (an existing
daily-notes vault, non-expert) found it "too much," and nothing in the flow
explains what exomem will and won't do with pre-existing vault content. Most of
the steps are mechanical and already have library functions (`init_vault`,
`doctor`, `install_skill`, `install_hook`); they just aren't composed.

## What Changes

- New CLI-only subcommand **`exomem setup`**: one interactive, idempotent,
  re-runnable wizard collapsing SETUP-LOCAL steps 2–7 — vault path → pre-init
  structure scan (via the `overview` core) with an explicit "exomem writes only
  under `Knowledge Base/`" contract for vaults with existing content → `init` →
  lean/hybrid profile → `doctor` preflight → Claude Code MCP registration
  (`claude mcp add`, or a printed `.mcp.json` snippet when the CLI is absent) →
  `install-skill` → optional `install-hook` → summary + next steps.
- Every step is a converger (`[done]` / `[skipped: already …]` / `[failed]`);
  re-running is always safe and ends `[skipped]` across the board.
- Non-interactive mode (`--yes` + flags) for scripted installs; a failed
  `doctor` aborts non-interactive runs (exit 1) instead of silently proceeding.
- Docs: README quickstart becomes three lines ending in `setup`; SETUP-LOCAL.md
  gains a "One command" section on top with the existing steps retitled as the
  manual path.

Not an MCP/REST op: the wizard mutates host config (`~/.claude`), spawns
subprocesses, and prompts — it stays off the registry by design. No new
dependencies; no model involvement (pure orchestration of existing local
functions).

## Capabilities

### New Capabilities
- `guided-setup`: one-command interactive local onboarding that is idempotent,
  scriptable, and safe on vaults with pre-existing content.

### Modified Capabilities

(none — `init`, `doctor`, `install-skill`, `install-hook` keep their contracts;
the wizard composes them)

## Impact

- New `src/kb_mcp/setup_wizard.py`; dispatch line + docstring entry in
  `src/kb_mcp/__main__.py`.
- Reuses `init.init_vault`, `overview.overview`, `doctor.doctor`/`render_human`,
  `install_skill.install_skill`, `install_hook.install_hook`.
- Docs: `README.md`, `SETUP-LOCAL.md`.
- Tests: new `tests/test_setup_wizard.py` with injected seams (input, subprocess
  runner, `which`, home dir) — no test touches the real `~/.claude` or spawns
  `claude`.
