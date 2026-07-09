# Tasks — Unified command surface (single source of truth)

## 1. Fidelity baseline FIRST (so every later step is checked)
- [x] 1.1 Add `tests/test_mcp_schema_fidelity.py` + a committed fixture
      `tests/fixtures/mcp_tool_schemas.json` capturing every CURRENT MCP tool's `name`,
      `inputSchema`, and `description` (introspect the built server). This is the immovable baseline.
- [x] 1.2 The test asserts the live server's tools match the fixture byte-for-byte — green NOW
      (pre-refactor), and the gate every later task must keep green.

## 2. Registry + bind_vault (TDD)
- [x] 2.1 Add `src/kb_mcp/commands.py`: `Param` + `Command` dataclasses; `bind_vault(leaf, vault_root)`
      returning a callable with `__signature__` = leaf minus `vault_root`, `__doc__` = description,
      calling `leaf(vault_root, **kwargs)`. `COMMANDS` tuple + `HAND_REGISTERED_EXCEPTIONS`.
- [x] 2.2 Registry well-formedness is enforced by `test_mcp_schema_fidelity` (names unique via the
      byte-identical tool set; ≤1 positional per command via `_SPEC`) + `test_cli_ops` (param coercion);
      `bind_vault` fidelity is the byte-identical snapshot itself.

## 3. Generate MCP tools from the registry
- [x] 3.1 In `server.py`, register MCP tools by looping `COMMANDS` with `mcp` through
      `mcp.tool(bind_vault(cmd.leaf, vault_root))` (honoring `tier`/`KB_MCP_DISABLE_TIER2`); keep the
      `HAND_REGISTERED_EXCEPTIONS` (`note`, `mint_*`) hand-wired.
- [x] 3.2 `test_mcp_schema_fidelity` stays byte-identical green; exceptions list is explicit
      (`test_hand_registered_exceptions_are_explicit` asserts no silent skips).

## 4. Shared envelope + arg coercion
- [x] 4.1 Add `src/kb_mcp/cli_ops.py`: `envelope(success, data=None, error=None)`;
      `coerce(params, raw) -> kwargs` (str/int/bool/list[str]/dict from JSON or CLI strings; reject
      unknown keys; preserve the binary-blob guard); `OpError(code, message, remediation)`.
- [x] 4.2 Tests `tests/test_cli_ops.py`: coercion per type, unknown-key rejection, blob guard,
      envelope shape.

## 5. REST + OpenAPI from the registry
- [x] 5.1 Refactor `server.py` REST section: replace the 9 hand-wired `/api/*` blocks with a loop over
      `COMMANDS` (rest) → generic handler (gate → body → coerce → threadpool leaf → envelope).
- [x] 5.2 Generate `/api/openapi.json` from the registry param specs; delete the `post_tools` list.
- [x] 5.3 Tests `tests/test_rest_registry.py`: the original 9 routes exist + call the same leaves
      (back-compat pin); a previously-unexposed op (replace) now has a route; success → envelope,
      validation error → `{success:false,error:{code,...}}`; OpenAPI lists real params; blob guard.

## 6. CLI from the registry (reads + writes)
- [x] 6.1 `[project.scripts]`: `kb` and `exomem` → `kb_mcp.__main__:main`. `python -m kb_mcp` still works.
- [x] 6.2 In `__main__.py`, generate a subparser per `COMMANDS` (cli) op (positional for positional
      params, `--flags` for the rest; `note`'s type-specific args via `--field key=value`); global
      `--json`; dispatch → resolve vault → coerce → `cmd.leaf(vault_root, **kwargs)` → human or
      envelope output; exit codes 0/1/2. Keep existing admin subcommands unchanged + additive.
- [x] 6.3 Tests `tests/test_cli_core_ops.py`: `kb find`/`get`/`audit` against a temp vault; a write
      (`kb note …`); `--json` envelope vs human; missing-arg → exit 2; op error → exit 1 + code.

## 7. Docs
- [x] 7.1 Document the unified surface (one op = MCP tool + REST `/api/<name>` + `kb <name>` CLI) in
      `README.md`; show the `--json` envelope + a `curl` and a `kb` example.
      `src/kb_mcp/_scaffold/**` + canonical `_Schema/` out of scope.

## 8. Verify
- [x] 8.1 `PYTHONPATH=src KB_MCP_DISABLE_EMBEDDINGS=1 python -m pytest -q` green (785 passed) —
      **including `test_mcp_schema_fidelity` byte-identical** (the core safety gate) — no regression.
- [x] 8.2 `ruff check` clean on all touched files (project-wide lint is advisory with a non-curated
      baseline; no new errors introduced).
- [x] 8.3 Desk-side smoke: `kb find "<known term>" --json` returns hits; `kb audit --json` runs;
      `kb note … --json` writes; `--field` escape works; missing-arg → exit 2; op error → exit 1.
      Live REST `curl …/api/replace` + `/api/openapi.json` per-op params verified via tests (TestClient);
      a real running service curl only verifies desk-side.
- [x] 8.4 `openspec validate unify-command-surface --strict` passes.

## 9. Follow-ups (post-merge, non-blocking — from code review, all LOW)
- [ ] 9.1 CLI `edit --value <plain string>` requires JSON quoting (the genuine union → `json` tag);
      fall back to the raw string in CLI context when `json.loads` fails. (ergonomics papercut)
- [ ] 9.2 Malformed `--field KEY=VALUE` exits 1, not 2 — route through `parser.error` for a
      consistent usage exit code.
- [ ] 9.3 REST `/api/edit` doesn't blob-guard nested `edits[].new_string` (only top-level). NOT a
      regression (old REST had no guard + no `edits`), but mirror the MCP middleware for consistency.
- [ ] 9.4 `kb <tier2-op>` with `KB_MCP_DISABLE_TIER2` falls through to the serve parser — emit a
      clear "op unavailable" instead of an argparse error.
- [ ] 9.5 (separate change — needs a fixture regen, breaks byte-identical) genericize the `"Yolo"` /
      `"Mother Cancer"` example tokens in the `preserve`/`note` descriptions for the public release.
