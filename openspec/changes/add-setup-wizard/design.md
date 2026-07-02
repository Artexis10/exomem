# add-setup-wizard — design

## Context

SETUP-LOCAL.md steps 2–7 are mechanical compositions of existing library
functions. The failure mode observed in the field is not broken tooling but
onboarding weight and a missing "existing vault" moment: the user is never told
that exomem leaves their current files alone.

## Goals / Non-Goals

**Goals:**
- One command from `uv sync` to a working Claude Code integration.
- Idempotent convergers; a full re-run is a no-op ending in `[skipped]`s.
- The "Yusuke moment": before `init`, show what's in the vault (via
  `overview()`) and state the write contract explicitly.
- Fully testable without touching `~/.claude` or spawning real subprocesses.

**Non-Goals:**
- Remote/mobile tier setup (OAuth, tunnels) — stays in docs/deployment.md.
- MCP/REST exposure — host-config mutation is CLI-only.
- Replacing the individual subcommands — they remain the manual path.

## Decisions

1. **CLI-only subcommand dispatched in `__main__.py`**, module
   `src/kb_mcp/setup_wizard.py` (not `setup.py` — avoids setuptools-shadowing
   confusion). The registry's leaves are `leaf(vault_root, **kwargs)` wired to
   REST; a prompting, subprocess-spawning wizard doesn't fit that contract.

2. **Injected seams**: `run_setup(..., input_fn, run_fn, which_fn, home,
   print_fn)`. `home` redirects the skill target, hook dir, and settings path;
   `which_fn`/`run_fn` isolate the `claude mcp add` interaction; `input_fn`
   scripts prompts. Defaults are the real stdlib functions.

3. **Converger model.** Each step detects current state first:
   `init_vault` → catch `FileExistsError` → `[skipped]`; `install_skill` →
   on `FileExistsError`, read the target `SKILL.md` for `name: knowledge-base`
   to distinguish "ours → offer refresh (`force=True`)" from "foreign → warn +
   skip"; `claude mcp add` exit≠0 with "already exists" in output → interactive
   offer remove+re-add, non-interactive `[skipped]`. The wizard never passes
   `force` to `init_vault`.

4. **Registration command is argv-list, never `shell=True`.** Locate the CLI
   with `which_fn("claude")` (resolves `.cmd`/`.exe` shims on Windows). Server
   command: repo checkout (`pyproject.toml` at the package's repo root, `uv` on
   PATH) → `uv --directory <repo> run python -m kb_mcp --transport stdio`;
   otherwise (wheel install) → `sys.executable -m kb_mcp --transport stdio`.
   Default `--scope user` (a `local`-scope registration silently applies only
   to the cwd — a foot-gun for a user-level memory tool). No CLI → print the
   `.mcp.json` snippet via `json.dumps` (correct Windows backslash escaping for
   free).

5. **Profile detection**: `--lean`/`--hybrid` override; otherwise
   `importlib.util.find_spec("sentence_transformers")` decides the offered
   default (present → hybrid, absent → lean with a one-line upgrade note).
   Lean adds `KB_MCP_DISABLE_EMBEDDINGS=1` to the registration env.

6. **Doctor gate**: run after `init` (schema files must exist). On FAIL:
   interactive → show `render_human` + ask continue/abort (default abort);
   `--yes` → abort exit 1. Scripts must not proceed past a failed preflight.

7. **subprocess text handling**: `encoding="utf-8", errors="replace"` on every
   `run_fn` call — Windows-native Python otherwise decodes pipes as cp1252 and
   multibyte output crashes the reader thread.

## Risks / Trade-offs

- [`claude mcp add` CLI surface changes across versions] → single call site,
  errors reported verbatim with the manual `.mcp.json` snippet as fallback.
- [Wheel installs have no repo for `uv --directory`] → `sys.executable`
  fallback decided here, not improvised at implementation time.
- [Interactive prompts under PowerShell/Git Bash] → plain `input()`; no TTY
  tricks, no ANSI requirements.
- [User's existing `~/.claude/skills/knowledge-base` is a custom skill] → the
  frontmatter check refuses to force-overwrite anything not identifying as the
  bundled skill.

## Open Questions

(none)
