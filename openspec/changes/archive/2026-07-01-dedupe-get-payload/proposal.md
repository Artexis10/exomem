## Why

`op_get` (`src/kb_mcp/commands.py`) currently returns `content` (the raw file text —
frontmatter delimiters + body), `body` (the markdown after the frontmatter), and the
parsed `frontmatter` dict on every read. That means the body — and effectively the
frontmatter — ships twice on every `get` call. For large pages this is a real per-call
token cost with no consumer: the edit two-writer drift guard round-trips
`content_hash`, a sha256 computed server-side over the raw file bytes
(`src/kb_mcp/vault.py::content_hash`), not `content` itself. No caller needs to
reconstruct the hashed bytes to use `edit(expected_hash=...)`.

## What Changes

**BREAKING (flagged prominently):** the default `get` response payload no longer
includes raw `content`.

- Default `get` response becomes `{path, frontmatter, body, content_hash, mtime}`
  (plus the existing optional `history`/`links` fields when requested). Raw `content`
  is dropped from the default shape.
- Add a new registry-level parameter `include_raw: bool = false` to `get`
  (`op_get` / `GetResult.as_dict`). When `true`, the response gains a `content` field
  containing the full raw file text, byte-identical to what is on disk.
- `content_hash` is unaffected: it is still always computed server-side over the raw
  file text inside `get_page()`, regardless of `include_raw`. `edit(expected_hash=...)`
  round-trips are untouched — callers never need to reconstruct the hashed bytes
  themselves.
- `frontmatter_only=true` is unaffected (it already excludes body/content).
- No deprecation window or dual-shape transition period — this is a single-user,
  personal-installation surface. The break ships as a clean cut, clearly flagged in
  the CHANGELOG, rather than maintaining two response shapes over time.

This change is isolated from `improve-find-latency-token-cost` on purpose (per
review): that change is explicitly results-identical latency/observability work for
`find` ("no change to default `find` return shape"). Bundling a `get` protocol break
into it would blur a results-identical change with a breaking one. This change is
small, focused, and reviewed on its own.

## Capabilities

### New Capabilities

- `get-payload-shape`: the shape of the `get` response payload — default exclusion of
  raw content, the `include_raw` opt-in, and the unaffected drift-guard hash contract.

### Modified Capabilities

- None. `command-surface` governs how the registry generates MCP/REST/CLI/OpenAPI
  surfaces from one leaf function; that mechanism is unchanged — `include_raw` is just
  a new parameter flowing through the existing generation path. No `command-surface`
  requirement needs to change.

## Impact

- Code: `src/kb_mcp/get_page.py` (`GetResult.as_dict`, ~lines 40-48), `src/kb_mcp/commands.py`
  (`op_get`, ~lines 847-928 including its docstring-documented return shape).
- Surfaces: `get` is a single registry entry with surfaces `{mcp, rest, cli}`
  (`src/kb_mcp/commands.py` `_SPEC`, `("get", op_get, 1, False, False, "path", _MCRC)`).
  Adding `include_raw` to `op_get`'s signature is sufficient — REST, CLI, and OpenAPI all
  derive from the same leaf and need no separate per-surface edits.
- Fixtures/docs: `tests/fixtures/mcp_tool_schemas.json` must be regenerated via
  `scripts/dump-tool-schemas.py` (the MCP schema-fidelity gate in
  `tests/test_mcp_schema_fidelity.py` pins `get`'s description/inputSchema
  byte-for-byte). `docs/capabilities.md` must be regenerated via
  `scripts/generate-capabilities.py` (its `get` row currently reads "Returns
  frontmatter + body + raw content.", generated straight from `op_get`'s docstring).
- CHANGELOG / release process: the repo uses Release Please + Conventional Commits
  (`docs/release.md`). Pre-1.0 policy calls for at least a minor bump for public
  MCP/REST/CLI behavior changes and requires breaking pre-1.0 changes to be called out
  in release notes even when represented as a minor bump. The implementing commit
  should use a `BREAKING CHANGE:` footer (or `feat!:`) so Release Please surfaces the
  break prominently in `CHANGELOG.md`.
- Docs/scaffold sweep for `content`: grepped `src/kb_mcp/_scaffold/_Schema/**`
  (`SKILL.md`, `references/*.md`) and `README.md`. `SKILL.md`'s operations table
  already documents `get` only in terms of `content_hash` + `mtime` ("Returns
  `content_hash` + `mtime` for the two-writer drift guard...") and never claims raw
  `content` is returned by default. Every other `content` hit in the scaffold refers
  to source-capture "raw content" (the `add`/`preserve` workflow), unrelated to
  `get`'s response shape. No scaffold or README edits are required by this change;
  `tests/test_scaffold_no_leak.py` is unaffected because no scaffold file changes.
- Tests: `tests/test_drift_guard.py`, `tests/test_multi_edit.py`, and
  `tests/test_edit_validate_only.py` call `get_page_module.get_page(...)` directly and
  read `.content_hash`/`.content` off the `GetResult` dataclass, not through
  `as_dict()` — they are unaffected because `GetResult` keeps its `content` field;
  only the dict returned by `as_dict()`/`op_get` changes shape.
  `tests/test_consolidated_tools.py::test_get_full_still_returns_body` only asserts
  `body` and `content_hash` are present and needs no change, but gains a sibling test
  asserting `content` is absent by default and present under `include_raw=true`.
