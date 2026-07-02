## Why

The project's public identity is **exomem** — GitHub repo, PyPI package (`name = "exomem"`),
CLI (`exomem`, `kb` alias), the MCP server identity (`FastMCP("exomem")`), and README all say
so since the 2026-07-01 rename. But the internals still say the old name: the Python import
package is `kb_mcp`, every environment variable is `KB_MCP_*`, and docs/scripts mix the two.
The split identity has already produced real operational confusion (a deploy failed because
`restart.ps1` defaults to the new service name while the installed service still carries the
old one). The maintainer decision (2026-07-02) is to make exomem identifiable all the way
down rather than keep the internal/external split.

This change completes the rename with **zero breakage for existing installs**: the old import
name and the old environment variables keep working through explicit, tested compatibility
layers.

## What Changes

- **Python package rename**: `src/kb_mcp/` → `src/exomem/`. All internal (relative) imports
  are unaffected; docs and entrypoints move to `python -m exomem`.
- **`kb_mcp` becomes a compatibility shim**, shipped in the same wheel: a meta-path alias so
  `import kb_mcp` / `import kb_mcp.find` / `from kb_mcp import embeddings` resolve to the
  *same module objects* as their `exomem` counterparts (no duplicated module state), emit a
  one-time `DeprecationWarning`, and `python -m kb_mcp` still starts the CLI/server.
- **Environment variables rename**: every `KB_MCP_*` variable becomes `EXOMEM_*` (same
  suffixes). Internal code reads only `EXOMEM_*`. A promotion layer
  (`exomem.env_compat.promote_legacy()`) runs at package import and copies any `KB_MCP_X`
  value to `EXOMEM_X` when the new name is unset — so every existing `.env`/service config
  keeps working unchanged. New names win on conflict. When legacy names are detected, one
  log line recommends the new prefix. The promotion is re-runnable for late env loading.
- **Docs/scripts sweep**: README env table, deployment/setup/contributing docs, the shipped
  skill scaffold (`_Schema/`), helper scripts, and `openspec/config.yaml` all reference
  `EXOMEM_*` and `python -m exomem`, with a short legacy-compat note in the README.
- **Packaging**: wheel ships both `src/exomem` and the `src/kb_mcp` shim.
- Out of band (box ops, not in this change): re-register the Windows service as `exomem`
  and rename the deploy checkout; both are documented follow-ups.

Out of scope: renaming on-disk artifacts that are name-neutral (`.embeddings.sqlite`,
`.clip.sqlite`), vault content, and the claude.ai connector title (decided separately to
stay "Knowledge Base", identifying as exomem-powered).

## Capabilities

### New Capabilities
- `exomem-identity`: the exomem name is canonical across package, env vars, CLI, docs, and
  scripts, with permanent tested compatibility for `kb_mcp` imports and `KB_MCP_*` env vars —
  no reasoning-model involvement (pure naming/packaging), no new dependency.

## Impact

- Code: `src/kb_mcp/*` → `src/exomem/*` (git mv; relative imports unchanged); new
  `src/exomem/env_compat.py`; new `src/kb_mcp/` shim package (`__init__.py`, `__main__.py`);
  `pyproject.toml` wheel packages; every `KB_MCP_` string in `src/` becomes `EXOMEM_`.
- Tests: import + env sweep across the suite (the suite itself becomes the proof that the
  new names work); new `tests/test_rename_compat.py` proves the legacy surfaces.
- Deps: none added.
- Existing installs: no action required — legacy env names and imports keep working. The
  README documents the preferred new names.
- Release: breaking-adjacent rename shipped with compatibility, so a `feat!`-style bump is
  appropriate (rides the pending 0.3.0 release train).
