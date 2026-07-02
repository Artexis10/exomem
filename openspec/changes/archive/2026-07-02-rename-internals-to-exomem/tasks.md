# Tasks — rename internals to exomem

## 1. Package move + shim
- [x] 1.1 `git mv src/kb_mcp src/exomem`; sweep remaining `kb_mcp` strings inside `src/`
      (docstrings, CLI prog/messages, `python -m kb_mcp` hints) to `exomem`.
- [x] 1.2 New `src/kb_mcp/__init__.py`: meta-path alias finder (kb_mcp[.X] → same
      `exomem[.X]` module objects), one-time `DeprecationWarning`; `src/kb_mcp/__main__.py`
      delegating to `exomem.__main__.main()`.
- [x] 1.3 `pyproject.toml`: wheel `packages = ["src/exomem", "src/kb_mcp"]`.

## 2. Env identity
- [x] 2.1 New `src/exomem/env_compat.py`: `promote_legacy()` (KB_MCP_X → EXOMEM_X when
      unset; returns promoted names; one log line when any promoted), called first thing in
      `exomem/__init__.py`.
- [x] 2.2 Sweep `KB_MCP_` → `EXOMEM_` across `src/exomem/` including `_scaffold/_Schema`
      docs; verify zero `KB_MCP_` remains outside `env_compat.py` + the shim.

## 3. Tests
- [x] 3.1 Sweep `tests/`: `kb_mcp` imports/attr paths → `exomem`; `KB_MCP_` env names →
      `EXOMEM_`; conftest.
- [x] 3.2 New `tests/test_rename_compat.py`: module-identity via shim + DeprecationWarning;
      `python -m kb_mcp --help` subprocess; `promote_legacy()` semantics (legacy-only,
      no-clobber, re-run).

## 4. Docs/scripts
- [x] 4.1 README (env table → EXOMEM_* + legacy note), SETUP-LOCAL.md, CONTRIBUTING.md,
      docs/deployment.md, docs/remote-checklist.md, docs/release.md, docs/capabilities.md.
- [x] 4.2 Repo CLAUDE.md path refs; `openspec/config.yaml` test command;
      `openspec/specs/**` current-truth mentions.
- [x] 4.3 `scripts/*` env/PYTHONPATH/`python -m` refs; service unit files sanity
      (already exomem-named).

## 5. Verify
- [x] 5.1 Full suite green (`PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 python -m pytest -q`).
- [x] 5.2 `ruff check` — no net-new findings vs origin/main; leak guard green.
- [x] 5.3 `openspec validate rename-internals-to-exomem --strict` passes.
- [x] 5.4 `uv build` — wheel contains both `exomem/` and `kb_mcp/`.
- [ ] 5.5 Box ops after merge (Hugo/desk-side): re-register service as `exomem`
      (`install-service.ps1`), rename deploy checkout, optionally migrate its `.env` keys.

## 6. Follow-ups (non-blocking)
- [ ] 6.1 Consider a far-future removal window for the shim/promotion (or keep forever —
      cost is near zero).
