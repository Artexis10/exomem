## 1. Part A — Self-discovering, markdown-first skill (prose)

- [x] 1.1 SKILL.md "## Vault layout": add framing that the tree is the `Knowledge Base/` layer only, and instruct running `overview` on first engagement; treat outside-KB as read-only. Optionally list `_access.yaml` in the tree.
- [x] 1.2 SKILL.md "### Assessing a vault you didn't build": promote to the canonical first-engagement step, tied to write-scope.
- [x] 1.3 Reword "Obsidian vault" → "markdown vault (Obsidian optional)" across SKILL.md (3, 10, 14, 120, 173, 336), index.md (3), references/{frontmatter(5,128),supersession(53-58,77),operations(188),write-scope(44),audit-checks(20),page-types(483)}.md; keep wikilinks+frontmatter required, mark Dataview/callouts/Sync optional.
- [x] 1.4 write-scope.md: add "## Per-subtree access overrides (`_access.yaml`)" section (readonly/excluded schema, hot-reload, hard refusal, absent-by-default, resolution order) with generic example paths.
- [x] 1.5 SKILL.md read-only bullet (~240-242): name `_access.yaml`; correct to Tier 1 AND Tier 2, hard refusal.
- [x] 1.6 Confirm `tests/test_scaffold_no_leak.py` still passes (all new prose generic).

## 2. Part B — `personalize` command + tests

- [x] 2.1 New `src/exomem/personalize.py`: constants + `PersonalizeError`, `FolderProposal`/`PersonalizeReport` dataclasses.
- [x] 2.2 Pure core: `classify_siblings(scan, existing)` (measured-signal heuristic) and `merge_access_yaml(existing_text, add_readonly, add_excluded)` (YAML round-trip, byte-stable, preserves unknown keys).
- [x] 2.3 I/O + converger: `scan_and_classify` (NO_KB error), `write_access_yaml`, `personalize`, `run_personalize`, `personalize_main` with injectable `overview_fn`/`input_fn`/`print_fn`.
- [x] 2.4 Wire `exomem personalize` dispatch + docstring in `src/exomem/__main__.py`.
- [x] 2.5 Add idempotent personalize step (3b, after init) to `setup_wizard.run_setup`.
- [x] 2.6 `tests/test_personalize.py`: classifier (synthetic overview dicts), merge byte-stability/idempotency, tmp-vault integration asserting `access.access_tier` honors the emitted file, CLI dispatch + `--yes`/decline, `--yes`-without-`--vault` usage error, cap-omitted surfaced.
- [x] 2.7 Extend `tests/test_setup_wizard.py`: sibling folder → personalize line + generated `_access.yaml`; re-run converges to skip.

## 3. Part C — Retire the GENERIC-marker canonical

- [x] 3.1 Relocate `LEAK_PATTERNS` (7 structural regexes) verbatim into `tests/test_scaffold_no_leak.py` as a module constant; drop the `importlib` path-import shim; update its docstring.
- [x] 3.2 Delete `scripts/genericize-schema.py`.
- [x] 3.3 Delete `tests/test_schema_markers.py`.
- [x] 3.4 Repoint `scripts/rebuild-schema-zip.py`: read SKILL.md + references from `src/exomem/_scaffold/_Schema/`; overlay `project-keys.yaml` from `--vault` when given (else scaffold's); remove `strip_markers_keep_real`; make `--vault` optional (+ `--out`/repo-local default). Update `.ps1`/`.sh` wrapper comments.
- [x] 3.5 Docs: rewrite CLAUDE.md "Editing the skill scaffold" (single-source, zip-from-scaffold); update CONTRIBUTING.md maintainer section; tidy `.gitignore` line 21 and orphaned `scripts/generic/project-keys.yaml`.

## 4. Verify + ship

- [x] 4.1 `uv run pytest -q` green; confirm no collection error from the deleted marker test.
- [x] 4.2 `personalize` end-to-end smoke: markdown `Reference/`→readonly, binary `Photos/`→excluded, re-run no changes.
- [x] 4.3 `setup` step + `install-skill` read generic/markdown-first; `grep genericize-schema|GENERIC-START` returns only archived openspec docs; `rebuild-schema-zip.py` builds from scaffold.
- [x] 4.4 Commit Parts A/B/C (separate commits ok) on the worktree branch and update PR #114 (or open a follow-up PR).
