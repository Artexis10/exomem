## 1. Tests First

- [x] 1.1 Add `find` timing tests proving `include_timings=True` returns `total_ms`, stage entries, cache status, and the same ordered paths as an untimed request.
- [x] 1.2 Add default-compatibility tests proving existing `find` calls still return the current list shape with excerpts and no timing envelope.
- [x] 1.3 Add compact-result tests proving `detail="compact"` preserves ordered paths while omitting `excerpt` and detailed `signals`.
- [x] 1.4 Add hot-cache tests for repeated identical requests, cache-hit timing visibility, parameter-key separation, caller-mutation safety, and `find.clear_cache()` clearing the hot cache.
- [x] 1.5 Add cache invalidation tests for in-scope markdown mtime/count changes and embedding/CLIP sidecar freshness changes.
- [x] 1.6 Add watcher tests proving Exomem self-write upsert/delete events are suppressed, ordinary external modify/delete events still dispatch, and a later same-path external edit is not hidden.
- [x] 1.7 Add command-surface tests or fixture expectations for the new `find` parameters and return envelope behavior through the registry/MCP schema.

## 2. Find Timing and Result Surface

- [x] 2.1 Add lightweight timing helpers in `find.py` for stage spans, skipped/unavailable lanes, total elapsed time, and cache status without storing excerpts, bodies, vectors, or chunks in diagnostics.
- [x] 2.2 Instrument the current keyword, vector, CLIP, BM25, graph, temporal, fusion, hit filtering, rerank, auto-widen, and date-filter stages without changing ranking order.
- [x] 2.3 Add compact hit serialization alongside the existing `Hit.as_dict()` full serialization.
- [x] 2.4 Update `op_find` to accept `detail="full" | "compact"` and `include_timings=False`, preserving the existing default return shape and adding a `timings` sibling only when requested.
- [x] 2.5 Preserve `pack=true` behavior while allowing `pack`, compact/full hit detail, and optional timing diagnostics to compose predictably.

## 3. Hot Find Cache

- [x] 3.1 Add a bounded in-process LRU cache for base `find` hit lists, default size 32 and disabled when configured to size 0.
- [x] 3.2 Build request cache keys from all ranking/filtering parameters, resolved vault root, scope/mode knobs, date/preference options, and active ranking-config identity.
- [x] 3.3 Build freshness keys from the relevant markdown count/max-mtime scope plus embedding and CLIP sidecar mtimes when those sidecars can affect the request.
- [x] 3.4 Return copied cached hits so downstream mutation cannot poison the cache.
- [x] 3.5 Extend `find.clear_cache()` to clear parsed-page, resolver, and hot-query cache state needed by tests.

## 4. Watcher Self-Write Suppression

- [x] 4.1 Add a bounded self-write suppression registry in `file_watcher.py`, keyed by resolved vault root and vault-relative path.
- [x] 4.2 For create/modify events, match suppression by path plus file signature such as mtime_ns and size; for delete events, use a short TTL entry.
- [x] 4.3 Have `FileWatcher._record()` drop only matching self-authored events before they enter the debounce queues.
- [x] 4.4 Register self-authored writes from `batch_atomic_write()` after successful replacement and before or alongside the existing embedding upsert.
- [x] 4.5 Register self-authored delete/move events from Exomem paths that already call `embeddings.delete_after_remove`.
- [x] 4.6 Update the watcher module docstring to replace the current "echo is harmless" rationale with the suppression contract.

## 5. Command Surface, Logging, and Docs

- [x] 5.1 Update the unified `find` command registry docstring and parameters for `detail` and `include_timings`.
- [x] 5.2 Extend `query_log.log_find_call()` to accept optional timing summary fields while continuing to log only query metadata, paths, types, and signals.
- [x] 5.3 Regenerate `tests/fixtures/mcp_tool_schemas.json` and any generated capabilities docs affected by the command surface.
- [x] 5.4 Update scaffold/user-facing find guidance only if needed to mention compact recall and timing diagnostics generically, keeping leak-guarded scaffold content generic.

## 6. Validation

- [x] 6.1 Run targeted tests for `find`, file watcher, query logging, REST/CLI registry behavior, and MCP schema fidelity.
- [x] 6.2 Run the repo validation command from OpenSpec context: `uv run python -m pytest -q` with `KB_MCP_DISABLE_EMBEDDINGS=1`.
- [x] 6.3 Run `ruff check`.
- [x] 6.4 Confirm no LSH, ANN/vector database migration, or retrieval architecture rewrite was introduced in this change.
