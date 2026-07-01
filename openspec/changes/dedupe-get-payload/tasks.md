## 1. Tests First

- [ ] 1.1 Add a test proving the default `get` response (no `include_raw`) has no
      `content` key and keeps `body`, `content_hash`, and `mtime`
      (`tests/test_consolidated_tools.py`, sibling to
      `test_get_full_still_returns_body`).
- [ ] 1.2 Add a test proving `get(..., include_raw=True)` returns `content`
      byte-identical to the file's contents on disk.
- [ ] 1.3 Add/confirm a test that `edit(expected_hash=get(path=...)["content_hash"])`
      round-trips unaffected at the `op_get`/dict-returning level (the existing
      `tests/test_drift_guard.py` coverage exercises the `GetResult` dataclass
      directly; add one test that goes through `op_get`'s dict output to pin the
      caller-facing contract).
- [ ] 1.4 Run the existing `frontmatter_only=true` tests
      (`tests/test_consolidated_tools.py::test_get_frontmatter_only_routes`) and
      confirm they remain green with no changes needed.
- [ ] 1.5 Run the existing `links`/`history` composition tests and confirm they remain
      green with no changes needed.
- [ ] 1.6 Add or confirm a command-surface/fixture expectation that `include_raw`
      appears as a `get` parameter on the MCP/REST/CLI/OpenAPI surfaces generated from
      the registry.

## 2. Implementation

- [ ] 2.1 Add `include_raw: bool = False` to `GetResult.as_dict()` in
      `src/kb_mcp/get_page.py` (~lines 40-48); only include the `content` key in the
      returned dict when `include_raw` is true. Leave `GetResult`'s dataclass fields
      (including `content`) and `get_page()`'s hashing behavior unchanged.
- [ ] 2.2 Add `include_raw: bool = False` to `op_get`'s signature in
      `src/kb_mcp/commands.py` (~lines 847-928) and thread it to
      `get_page_module.get_page(...).as_dict(include_raw=include_raw)` in the
      non-`frontmatter_only` branch.
- [ ] 2.3 Update `op_get`'s docstring (Args + Returns) to document the new default
      shape `{path, frontmatter, body, content_hash, mtime}`, the `include_raw`
      parameter, and that `content_hash` is always computed over the raw file text
      regardless of `include_raw`.
- [ ] 2.4 Confirm no change is needed to the `frontmatter_only` branch
      (`get_frontmatter_module.get_frontmatter`) — it already excludes body/content.

## 3. Command Surface, Fixtures, and Docs

- [ ] 3.1 Regenerate `tests/fixtures/mcp_tool_schemas.json` via
      `PYTHONPATH=src python scripts/dump-tool-schemas.py`; review the diff and
      confirm it touches only the `get` tool's schema/description.
- [ ] 3.2 Regenerate `docs/capabilities.md` via `scripts/generate-capabilities.py`
      (without `--check`); review the diff and confirm it touches only the `get` row.
- [ ] 3.3 Re-sweep `src/kb_mcp/_scaffold/_Schema/**` and `README.md` for `content`
      references tied to `get`'s response shape; update only if the implementation
      diff introduces new drift versus the sweep already recorded in `design.md`
      (none found as of this change).
- [ ] 3.4 When implementing, use a `BREAKING CHANGE:` footer (or `feat!:`) on the
      commit per `docs/release.md`'s Commit convention, so Release Please surfaces the
      break prominently in `CHANGELOG.md` even under the pre-1.0 minor-bump policy.

## 4. Validation

- [ ] 4.1 `KB_MCP_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q`
- [ ] 4.2 `uv run ruff check`
