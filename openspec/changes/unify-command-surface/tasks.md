# Tasks — Unified command surface (single source of truth)

## 1. Fidelity baseline FIRST (so every later step is checked)
- [ ] 1.1 Add `tests/test_mcp_schema_fidelity.py` + a committed fixture
      `tests/fixtures/mcp_tool_schemas.json` capturing every CURRENT MCP tool's `name`,
      `inputSchema`, and `description` (introspect the built server). This is the immovable baseline.
- [ ] 1.2 The test asserts the live server's tools match the fixture byte-for-byte — green NOW
      (pre-refactor), and the gate every later task must keep green.

## 2. Registry + bind_vault (TDD)
- [ ] 2.1 Add `src/kb_mcp/commands.py`: `Param` + `Command` dataclasses; `bind_vault(leaf, vault_root)`
      returning a callable with `__signature__` = leaf minus `vault_root`, `__doc__` = description,
      calling `leaf(vault_root, **kwargs)`. `COMMANDS` tuple + `HAND_REGISTERED_EXCEPTIONS`.
- [ ] 2.2 Tests `tests/test_commands_registry.py`: `bind_vault` produces the expected signature +
      docstring + delegates correctly; leaves callable; param specs well-formed; names unique;
      ≤1 positional per command.

## 3. Generate MCP tools from the registry
- [ ] 3.1 In `server.py`, register MCP tools by looping `COMMANDS` with `mcp` through
      `mcp.tool(bind_vault(cmd.leaf, vault_root))` (honoring `tier`/`KB_MCP_DISABLE_TIER2`); keep the
      `HAND_REGISTERED_EXCEPTIONS` (e.g. `note`) hand-wired as today.
- [ ] 3.2 `test_mcp_schema_fidelity` MUST stay byte-identical green. Iterate the registry descriptions
      + param specs until every non-exception tool matches; add any stubborn tool to the exceptions
      list. The exceptions list must be explicit (test asserts no silent skips).

## 4. Shared envelope + arg coercion
- [ ] 4.1 Add `src/kb_mcp/cli_ops.py`: `envelope(success, data=None, error=None)`;
      `coerce(params, raw) -> kwargs` (str/int/bool/list[str]/dict from JSON or CLI strings; reject
      unknown keys; preserve the binary-blob guard); `OpError(code, message, remediation)`.
- [ ] 4.2 Tests `tests/test_cli_ops.py`: coercion per type, unknown-key rejection, blob guard,
      envelope shape.

## 5. REST + OpenAPI from the registry
- [ ] 5.1 Refactor `server.py` REST section: replace the 9 hand-wired `/api/*` blocks with a loop over
      `COMMANDS` (rest) → generic handler (gate → body → coerce → threadpool leaf → envelope).
- [ ] 5.2 Generate `/api/openapi.json` from the registry param specs; delete the `post_tools` list.
- [ ] 5.3 Tests `tests/test_rest_registry.py`: the original 9 routes exist + call the same leaves
      (back-compat pin); a previously-unexposed op (e.g. replace) now has a route; success → envelope,
      validation error → `{success:false,error:{code,...}}`; OpenAPI lists real params; blob guard.

## 6. CLI from the registry (reads + writes)
- [ ] 6.1 `[project.scripts]`: `kb` and `kb-mcp` → `kb_mcp.__main__:main`. `python -m kb_mcp` still works.
- [ ] 6.2 In `__main__.py`, generate a subparser per `COMMANDS` (cli) op (positional for positional
      params, `--flags` for the rest; `note`'s type-specific args via `--field key=value`); global
      `--json`; dispatch → resolve vault → coerce → `cmd.leaf(vault_root, **kwargs)` → human or
      envelope output; exit codes 0/1/2. Keep existing admin subcommands unchanged + additive.
- [ ] 6.3 Tests `tests/test_cli_core_ops.py`: `kb find`/`get`/`audit` against a temp vault; a write
      (`kb note …`); `--json` envelope vs human; missing-arg → exit 2; op error → exit 1 + code.

## 7. Docs
- [ ] 7.1 Document the unified surface (one op = MCP tool + REST `/api/<name>` + `kb <name>` CLI) in
      `README.md` / `docs/deployment.md`; show the `--json` envelope + a `curl` and a `kb` example.
      `src/kb_mcp/_scaffold/**` + canonical `_Schema/` out of scope.

## 8. Verify
- [ ] 8.1 `PYTHONPATH=src KB_MCP_DISABLE_EMBEDDINGS=1 python -m pytest -q` green — **including
      `test_mcp_schema_fidelity` byte-identical** (the core safety gate) — no regression.
- [ ] 8.2 `ruff check` clean (no new errors).
- [ ] 8.3 Desk-side smoke: `kb find "<known term>" --json` returns hits; `kb-mcp audit` runs; with
      `KB_MCP_REST_API_KEY` set, `curl …/api/replace` (newly exposed) works + `/api/openapi.json`
      lists per-op params.
- [ ] 8.4 `openspec validate unify-command-surface --strict` passes.
