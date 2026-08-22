## Context

The v0.29.2 retrieval repair moved ordinary warm keyword and unit recall onto bounded index candidates. Structured-filter eligibility remains a separate prerequisite path: `_eligible_filter_paths` walks and parses every Markdown parent before candidate search. On a 2,410-page real vault, a cold exact category request spent about 7.8 seconds there; the equivalent indexed category lookup was 0.35 ms and targeted hydration 2.85 ms.

The rebuildable lexical sidecar also overloads `None` to mean disabled, stale, unsupported, or failed; any SQLite exception sets process-lifetime `_failed`; and `PRAGMA journal_mode=WAL` runs before the busy timeout on every connection. A transient lock can therefore disable unit recall until restart. When FTS5 is absent, auto mode has no maintained exact category candidate route and can return a false empty.

Correctness and bounded foreground work must be reconciled explicitly. If a maintained category catalog cannot prove completeness and no bounded event delta can repair it, Exomem cannot promise exact results without a corpus scan. This design returns an observable warming/unavailable outcome instead of lying with an empty result or violating the latency boundary.

## Goals / Non-Goals

**Goals:**

- Make safe category/kind filters O(index candidates), not O(corpus pages).
- Preserve canonical result semantics by post-evaluating hydrated candidates.
- Maintain exact category/kind metadata independently of FTS5 availability.
- Recover from transient SQLite failures on a later call without restart.
- Heal only provably complete bounded deltas in the foreground.
- Expose incomplete index state so it cannot be cached or mistaken for no matches.

**Non-Goals:**

- A new vector database, replacement query language, ranking change, or category boost.
- Treating derived SQLite state as authoritative knowledge.
- A full foreground rebuild/walk for ordinary exact category/kind requests.
- Making every arbitrary structured-filter expression indexable in this slice.

## Decisions

### 1. A maintained semantic catalog is independent of FTS capability

Split normal-table semantic metadata setup from virtual FTS table setup. The `pages` and `semantic_units` tables, their category/kind indexes, and a catalog checkpoint form the `semantic_catalog` capability and are created/maintained even when FTS5 or trigram tokenization is unavailable. FTS tables remain optional text-ranking acceleration in the same disposable sidecar. Startup warm-up/readiness seeds the catalog through the existing bounded/background index path.

The catalog is complete only when both halves of its compound identity match: (1) its stored freshness checkpoint exactly matches a live snapshot or an atomically applied complete delta, and (2) its semantic projection identity matches the current catalog schema version, semantic-unit parser version, core category/authoring-contract identity, and extension semantic-language registry content hash. A mismatch in either half is rebuildable stale state. This prevents a parser or category-registry upgrade from omitting candidates even when no note Markdown changed. A missing, corrupt, or unverifiably stale catalog is not an empty index.

Alternative considered: use the current semantic index as an independent durable oracle. Rejected because it is a parser/projection API, not a separately maintained complete lookup substrate.

### 2. Candidate algebra distinguishes complete empty from unsupported

The filter planner returns either `complete(paths)`—where `paths` may be an empty set—or `unsupported`. Exact category/kind equality and membership predicates provide positive complete seeds.

- For `AND`, any positive complete seed may narrow candidates; multiple seeds intersect. Page predicates, `NOT`, and unsupported predicates are evaluated after hydration and do not invalidate that safe narrowing.
- For `OR`, every branch must provide a complete seed; paths are unioned. One branch without a seed makes the whole OR unsupported.
- A top-level `NOT` or page-only expression is unsupported.
- A complete empty intersection returns no candidates without falling back to a scan.

Candidate parents are still evaluated by the canonical access policy and `structured_filters.page_matches`, preserving aliases, page/lifecycle predicates, scene-frame emission, and future filter behavior. Unsupported plans retain the existing full-scan correctness oracle; indexed and scan results are parity-tested.

### 3. Empty-query recall consumes finite eligibility directly

When eligibility is `complete(paths)` and the keyword query is empty, `_find_keyword` iterates those paths. A non-empty keyword request intersects text candidates with eligible paths before hydration. Neither case walks the scope merely to rediscover category candidates.

### 4. Explicit outcomes cross the command boundary on incomplete recall

Internal catalog/lexical calls return `available`, `stale`, `unsupported`, `transient_failure`, or `fatal_failure`, with `complete`, backend, and bounded remediation metadata. `available` may contain zero candidates and is authoritative only when `complete=true`.

For an exact category/kind request whose candidate plan is safe but catalog completeness cannot be established, `find` raises a typed `RETRIEVAL_INDEX_WARMING` operation outcome carrying `complete=false`, `status` (`warming` or `temporarily_unavailable`), and bounded `retry_after_ms`. MCP/REST/CLI translate it through the shared error envelope. It is never cached as an empty result. This is preferable to either a false empty or an unbounded foreground scan.

FTS5 absence is not this error when the semantic catalog is complete: metadata-only category/kind lookup remains exact. FTS affects only content ranking.

### 5. Freshness deltas are atomic snapshots, not count guesses

Each scope registry has a process-instance ID and monotonic generation. A non-destructive consumer checkpoint names `{instance_id, generation, triple}`. `delta_since` atomically returns `{from, to, complete, changed, deleted}` for one snapshot; rename is deleted-old plus changed-new. The two path sets are mutually disjoint and coalesced to target state: a path present at `to` appears only in `changed`, while a path absent at `to` appears only in `deleted`, regardless of intermediate event order. Multiple consumers do not consume history. Restart, reconciliation mismatch, overflow, or a checkpoint outside retained history yields `complete=false` with no partial suffix presented as complete.

Foreground repair accepts at most 32 changed-plus-deleted paths from a complete delta. In one SQLite transaction it applies upserts/deletes and stores the exact `to` checkpoint. Events after the snapshot increment generation and remain visible to the next request. Unknown or larger deltas schedule one background atomic rebuild and return `RETRIEVAL_INDEX_WARMING` for safe exact category plans.

### 6. SQLite classification is narrow and rebuilds are atomic

`SQLITE_BUSY`, `SQLITE_LOCKED`, `SQLITE_INTERRUPT`, and their canonical locked/busy messages are transient for the current operation and never set a sticky process flag. `SQLITE_CORRUPT` and `SQLITE_NOTADB` are fatal sidecar states. Schema/version mismatch is rebuildable stale state, not fatal. Other operational failures degrade the current call without permanent retirement unless a fatal code is proven.

Ordinary connections set bounded busy/synchronous policy but do not negotiate journal mode. WAL negotiation occurs only during setup/rebuild and soft-fails. A background rebuild captures a start freshness checkpoint and semantic projection identity, builds a temporary sidecar, then replays a complete delta through an exact target checkpoint before atomic publication. Events after that target remain detectable; overflow or semantic-identity change discards/retries the temporary build rather than publishing it as complete. The target checkpoint and semantic identity are stored in the temp database before publication. Foreground code performs no VACUUM, whole-corpus rebuild, or large-file quarantine move.

### 7. Performance evidence is reproducible and private

The workstation category lane runs in a live service process with the semantic catalog and OS file cache warm, but clears the in-process result cache and parsed-page cache for each cold sample. It uses `scope=kb-only`, `mode=keyword`, empty query, graph/rerank/pack off, two indexed candidates, 30 samples, and nearest-rank p95; connector RTT and startup/catalog construction are excluded. Hot samples repeat an unchanged request with the result cache live. Page and unit paths expose an equivalent `filter_eligibility` stage.

Reports use synthetic category labels, anonymous run IDs, corpus-size buckets rounded to 500, candidate-count buckets, and latency distributions only—never exact category frequencies, paths, excerpts, or private query text. CI relies on operation-count scaling tests; workstation thresholds are release evidence.

## Risks / Trade-offs

- [First request sees warming] → Seed catalog during startup/readiness and provide bounded retry metadata instead of false results.
- [Conservative planner scans complex expressions] → Keep canonical parity and add operators only with completeness proofs.
- [Catalog and FTS share a file] → Separate capability/schema readiness; fatal file loss yields explicit warming while background atomic rebuild replaces disposable state.
- [Transient failures repeat] → Rate-limit diagnostics per vault/status while retrying later calls normally.
- [Scene-frame grouping loses candidates] → Test child and emitted-parent identities through the common evaluator.

## Migration Plan

1. Add red algebra, no-walk, compound-identity completeness, transient recovery, no-FTS catalog, coalesced delta, concurrent rebuild, and privacy tests.
2. Split semantic catalog setup/readiness from optional FTS setup and introduce explicit outcomes.
3. Add atomic freshness checkpoints/deltas and bounded repair.
4. Route safe filters through candidate hydration and preserve scan fallback for unsupported plans.
5. Verify synthetic scaling, full lean behavior, and the precisely defined real-vault release lane.
6. Only then activate `teach-portable-category-core` in the coordinated release.

Existing sidecars are upgraded or rebuilt as disposable state. Markdown is never migrated. Rollback restores prior acceleration behavior without touching notes.

## Open Questions

None for this slice.
