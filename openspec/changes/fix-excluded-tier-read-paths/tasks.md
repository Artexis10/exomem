# Tasks: fix-excluded-tier-read-paths

## 1. Shared enforcement helper

- [ ] 1.1 Add `access.refuse_if_excluded(vault_root, rel_path)` (or reuse
      `is_indexable`) returning a uniform decision, beside the existing
      `writable_reason` (`access.py:186`). Red-first unit test in
      `tests/test_access_read_paths.py` for tier resolution on each surface's
      path form.

## 2. get_page / read_memory

- [ ] 2.1 Red test `test_get_page_refuses_excluded_path`: seed `_access.yaml`
      per the `test_access.py:59` fixture pattern; assert an excluded read is
      byte-identical (code + shape + text) to a NOT_FOUND read and does not echo
      the path.
- [ ] 2.2 Enforce in `get_page.get_page` after path normalization
      (`get_page.py:103-117`, beside the reserved-name guard `:81`), raising the
      existing missing-path error.
- [ ] 2.3 Verify `op_get`/`op_read_memory` at the command layer inherit the
      refusal (unit-read path `semantic_unit_read` too).

## 3. overview / browse_memory

- [ ] 3.1 Red test `test_overview_hides_excluded_tree` (+ counts exclude them).
- [ ] 3.2 Dir-prune in the `os.walk` loop (`overview.py:133-144`) and per-file
      skip (`:154`) via `access.access_tier` on the vault-relative path; no
      "hidden N" marker.
- [ ] 3.3 Audit `tests/test_overview.py` fixtures/goldens for count shifts.

## 4. query_dataset / read_media

- [ ] 4.1 Red tests `test_query_dataset_refuses_excluded`,
      `test_read_media_refuses_excluded`.
- [ ] 4.2 Enforce in `query_data.query_data` after `resolve_under_vault`
      (`query_data.py:466-468`) and in `video_frames.get_frames` (`:95`) after
      its resolve, using the missing-path contract.

## 5. Graph lane

- [ ] 5.1 Red test `test_graph_context_never_seeds_or_returns_excluded`
      (seed-by-path, seed-by-query, and neighbour cases).
- [ ] 5.2 Filter seeds, node materialization, and edge endpoints in
      `epistemic_graph.graph_context` (`:825-990`) via `access.is_indexable`,
      following the `review_context.py:73/:243` pattern.

## 6. Sweep + gates

- [ ] 6.1 Parametrized command-layer test asserting `read_memory`,
      `browse_memory`, `query_dataset`, `read_media`, and
      `connect_memory(graph-context)` all refuse an excluded path.
- [ ] 6.2 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q
      tests/test_access_read_paths.py tests/test_overview.py
      tests/test_epistemic_graph.py` green.
- [ ] 6.3 `uv run python -m pytest tests/test_latency_gate.py -q` green
      (thresholds untouched).
- [ ] 6.4 `uvx ruff check` clean on changed files; `openspec validate
      fix-excluded-tier-read-paths --strict` green.
