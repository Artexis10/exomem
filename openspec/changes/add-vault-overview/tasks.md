# add-vault-overview — tasks

## 1. Core module (test-first)

- [ ] 1.1 Write `tests/test_overview.py` against the core function: messy tmp
      vault (nested dirs, md with/without frontmatter, binary, zero-byte file,
      `note 2.md` beside `note.md`, oversized md) — assert exact totals,
      frontmatter %, junk detection, `kb.present` false pre-init, POSIX paths,
      determinism, and cap behavior (depth/breadth/list caps with exact totals).
- [ ] 1.2 Implement `src/kb_mcp/overview.py`: single `os.walk`, own skip-set
      (design §3), markdown-only capped content reads, shape-bucketed naming
      patterns, junk/largest/oldest summaries, bounded tree assembly.

## 2. Registry wiring

- [ ] 2.1 Add `op_overview` leaf to `src/kb_mcp/commands.py` (Google-style
      docstring — it IS the MCP/CLI/REST contract) and the `_SPEC` row
      `("overview", op_overview, 1, False, False, "path", _MCRC)`.
- [ ] 2.2 CLI-door test (`kb overview --json` pattern) + REST route smoke via the
      registry test machinery; confirm Tier 2-disabled exposure.
- [ ] 2.3 Regenerate `tests/fixtures/mcp_tool_schemas.json` via
      `scripts/dump-tool-schemas.py` (after the docstring is final).

## 3. Docs and skill guidance

- [ ] 3.1 Scaffold `SKILL.md`: Tier 1 table row, phrasing mappings, "Assessing a
      vault you didn't build" block (generic wording; run
      `tests/test_scaffold_no_leak.py`).
- [ ] 3.2 README core-tools table row for `overview`.

## 4. Verification

- [ ] 4.1 `uv run pytest -q` and `ruff check` green; run `kb overview` once
      against a scratch vault shaped like a daily-notes vault and eyeball the
      report.
