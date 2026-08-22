# Tasks — find speaker filter

- [x] 1. `Page.speakers` accessor in `find.py` (reads the `speakers:` frontmatter list; non-list → []).
- [x] 2. `speakers` param on `find()` threaded through every ranker path to `_passes_filters`
      (default `None` = no scoping; mirrors `tags`). Case-insensitive intersection match in
      `_passes_filters`.
- [x] 3. Expose on the `find` command in `commands.py` (param + docstring); regenerate the MCP
      schema-fidelity fixture (`tests/fixtures/mcp_tool_schemas.json` — only `find`'s `speakers`
      property added).
- [x] 4. Scaffold `SKILL.md` find-knobs note (generic — leak-guarded).
- [x] 5. Tests in `tests/test_find.py`: `Page.speakers` accessor, `_passes_filters` speaker
      match/miss, and a find() integration test.
- [x] 6. Full suite green (851 passed, 1 skipped); leak guard green; `openspec validate --strict`.
