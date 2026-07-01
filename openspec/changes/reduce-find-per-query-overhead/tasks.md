## 1. Tests First

- [ ] 1.1 Add snapshot-equivalence tests with monkeypatched walk counters proving a single hybrid
      `find(scope="kb")` call performs the KB markdown stat-walk at most once and the wikilink
      resolver stat-walk at most once, and that `scope="kb-only"` never performs a vault-wide
      stat-walk.
- [ ] 1.2 Add staleness tests proving a write to a markdown file in scope is seen by the next `find`
      call for both BM25 (KB and vault scope) and the wikilink resolver.
- [ ] 1.3 Add freshness-key tests for deletes, renames (mtime preserved), and backdated replacements
      (new file with an older mtime than the file it replaced), proving the BM25 index and the
      wikilink resolver rebuild in every case, where they would not have rebuilt under today's
      `(count, max_mtime)` key.
- [ ] 1.4 Add `ParsedPage` derived-text tests proving `body_norm`/`title_norm`/`stem_set` are
      computed once per page revision (not recomputed on repeated access) and are recomputed after
      an edit changes the file's mtime.
- [ ] 1.5 Add a combined-multiplier equivalence-grid test comparing the new single-pass output
      against the old three-pass (`_apply_type_boost` → `_apply_status_demotion` →
      `_apply_temporal_boost`) output, covering `prefer_compiled` × `prefer_active` × temporal
      on/off, the media-sidecar source-penalty exemption, and the all-off no-resort case — run in
      both fused-score mode and post-rerank `rerank_score` mode.
- [ ] 1.6 Add `InboundLinkIndex` tests proving output and ordering are identical to the current
      brute-force `find_inbound_wikilinks`, covering the basename-ambiguity gate, self-exclusion,
      and a move/delete safety check performed after a prior cached call (including a pure rename
      that preserves mtime).
- [ ] 1.7 Add a `find(pack=true)` regression test proving neighborhood assembly (including ordering)
      is unchanged when backed by the cached inbound-link index.
- [ ] 1.8 Add warm-up tests proving `warm_caches` populates the BM25 (KB and vault scope), resolver,
      and page caches; that each stage soft-fails independently without raising; and that
      `KB_MCP_DISABLE_WARMUP` skips warm-up entirely.

## 2. Per-Request Freshness Snapshot

- [ ] 2.1 Add a `FreshnessSnapshot` type with lazy, memoized `.kb()` and `.vault()` accessors that
      walk `find._walk_md(vault_root / "Knowledge Base")` and `vault.walk_vault_md(vault_root)`
      respectively, each computed at most once per instance.
- [ ] 2.2 Thread an optional `freshness` keyword through `bm25.BM25Index.search`/`bm25.search` so a
      caller-supplied snapshot's walk result is reused instead of `_current_max_mtime` re-walking;
      preserve today's own-walk behavior when no snapshot is passed.
- [ ] 2.3 Thread the same optional `freshness` keyword through `_get_query_resolver`.
- [ ] 2.4 Upgrade the BM25 cache comparison (`current_max > cached[0]`) and the resolver's
      `(count, latest)` cache key to `(count, max_mtime_ns, digest-of-sorted-rel-paths)`.
- [ ] 2.5 Create exactly one `FreshnessSnapshot` per `find()` call and pass it to every `bm25.search`
      call (both the `scope="kb"` call and auto-widen's `scope="vault"` call) and to
      `_get_query_resolver` made during that call.

## 3. Per-Page Derived-Text Memoization

- [ ] 3.1 Add `body_norm`, `title_norm`, and `stem_set` as `functools.cached_property` members on
      `ParsedPage`, matching `_make_excerpt`'s current normalization exactly and lazy-importing
      `bm25.tokenize` for `stem_set`.
- [ ] 3.2 Update `_make_excerpt`, `_keyword_match_paths` (and the keyword-mode all-tokens-present
      gate), `_stem_tokens_present`, and `_any_stem_present` to read the cached properties instead of
      recomputing normalized/stemmed text per call.
- [ ] 3.3 Document on `ParsedPage`/`FrontmatterCache` that derived-text invalidation relies on
      `FrontmatterCache.get()` replacing the whole instance on mtime change, so no separate cache
      hook is needed.

## 4. Single-Pass Post-RRF Multipliers And Per-Request Page Memo

- [ ] 4.1 Add `_apply_post_rrf_multipliers` computing the combined type/status/temporal multiplier
      once per candidate, preserving the media-sidecar source-penalty exemption and returning the
      input unchanged when no stage is active.
- [ ] 4.2 Give it a fused-score mode (operating on `(path, score)` RRF pairs) and a rerank-score mode
      (operating on `Hit.rerank_score`), sharing one multiplier computation; rerank mode excludes the
      temporal multiplier exactly as today.
- [ ] 4.3 Replace the three sequential fused-score calls with the new combined pass, and replace the
      post-rerank re-apply block with the rerank-mode call.
- [ ] 4.4 Add a per-request `dict[str, ParsedPage | None]` page memo shared by the multiplier pass,
      the fused-candidate-to-`Hit` resolution loop, and auto-widen's strong/weak partition.
- [ ] 4.5 Remove `_apply_type_boost`, `_apply_status_demotion`, and `_apply_temporal_boost` once the
      combined pass covers their behavior, after confirming no other call site depends on them.

## 5. InboundLinkIndex

- [ ] 5.1 Add a module-level, process-cached inbound-link index in `vault.py`, built in one read pass
      over `walk_vault_md`: `normalized_target -> ordered (seq, path, line_number, context,
      raw_target)` entries, plus a `basename -> count` map for the uniqueness gate.
- [ ] 5.2 Freshness-key the index by a digest of sorted `(rel_path, mtime_ns)` pairs, not a
      `(count, max_mtime)` pair.
- [ ] 5.3 Reimplement `find_inbound_wikilinks` to look up the cached index, preserving its exact
      signature, self-exclusion, basename-uniqueness gate, and seq-merged output ordering.
- [ ] 5.4 Wire a reset hook into `find.clear_cache()` or add `vault.clear_link_index()`, and use it
      wherever tests already rely on `find.clear_cache()` to reset freshness-keyed state.
- [ ] 5.5 Verify `move_file`/`delete_file`'s existing inbound-link safety checks observe the new
      cache correctly after a rename.

## 6. Startup Warm-Up

- [ ] 6.1 Add `bm25.warm(vault_root, scope)`, a thin explicit wrapper that primes the module-level
      `BM25Index` cache for the given scope.
- [ ] 6.2 Add `src/kb_mcp/warmup.py` with `warm_caches(vault_root)`: walk the KB through `find._CACHE`,
      call `bm25.warm(vault_root, scope)` for both `scope="kb"` and `scope="vault"`, build the
      resolver via `_get_query_resolver`, and instantiate `EmbeddingIndex` when embeddings are
      enabled.
- [ ] 6.3 Call `warm_caches(vault_root)` synchronously in `build_server`, immediately after the
      existing model preload block, gated by `KB_MCP_DISABLE_WARMUP`.
- [ ] 6.4 Give each warm-up stage its own soft-fail `try/except` with a warning log and a duration
      log, matching the existing preload block's pattern.

## 7. Validation

- [ ] 7.1 Run targeted tests for `find`, hybrid search, context packs, inbound links, and warm-up.
- [ ] 7.2 Run the full suite: `KB_MCP_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q`.
- [ ] 7.3 Run `uv run ruff check`.
- [ ] 7.4 Confirm no `find`/command-registry parameter, MCP schema, or default `find` behavior
      changed, other than the freshness-key fix's correction of delete/rename/backdated-replacement
      staleness.
