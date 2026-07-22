## Context

The source of truth remains Markdown, but several foreground paths reconstruct derived corpus state from that source on every request or after any corpus change:

- `semantic_writes.preflight_existing()` calls `semantic_contract.build_corpus_context()` for every validation. Its cache key is a full `(path, size, mtime_ns)` census, so Syncthing, media sidecars, or any unrelated Markdown write causes another whole-vault rebuild.
- mixed/unit recall walks every eligible Markdown file and calls `semantic_index.current_parent_index_state()` before querying the semantic-unit sidecars.
- `scope="kb"` auto-widen calls vault-scope BM25 and can rebuild the entire outside-KB corpus under ordinary churn.
- validate and commit are separate stateless calls, so they both repeat preflight work.
- the managed service installer upgrades its private venv, while a separately installed `uv tool` command can remain years behind with a lean capability set.

Live 0.29.1 evidence isolates these costs without model or reranker work: keyword `ask_memory` took 55.1 seconds, including 12.2 seconds in semantic units and 41.3 seconds outside KB; `kb-only` still took 13.1 seconds, including 11.6 seconds in semantic units. A guarded `observe_memory` validation took about 29 seconds and its commit about 16 seconds. Prior measured warm production edits were 32-48 ms and note creation was about 212 ms.

The system already has the needed primitives: atomic canonical writes, process-shared writer fencing, watcher-driven freshness state, a WAL lexical sidecar containing page and semantic-unit generations, and per-parent semantic parses emitted by the write coordinator. The repair makes those event-maintained artifacts the foreground substrate instead of treating them as optional caches behind a new corpus walk.

## Goals / Non-Goals

**Goals:**

- Keep warm persistent-server keyword reads and governed semantic-unit validate/commit operations sub-second at realistic vault sizes.
- Eliminate corpus-wide Markdown I/O from optimized foreground lanes and keep cached-metadata reconciliation inside explicit sub-second ceilings.
- Preserve exact freshness, relation governance, stable identities, writer fencing, read-your-write behavior, and crash recovery.
- Keep cold parsing off normal foreground requests by starting semantic-corpus warm-up with the service and retaining its result in the persistent process.
- Make CLI and service release/profile identity explicit and reconcile managed installations automatically.
- Add structural gates against corpus-wide Markdown parsing plus wall-clock and scaling ceilings for both validation and commit.

**Non-Goals:**

- Changing the authored semantic language, Markdown source-of-truth rule, ranking model, or MCP mutation schema.
- Adding a server-side reasoning model or changing any epistemic judgment.
- Replacing SQLite, BM25, embedding, CLIP, or graph ranking architecture.
- Requiring optional ML/media dependencies in lean installations.
- Making arbitrary one-shot Python startup the performance baseline; managed CLI calls should use the warm service runtime.

## Decisions

### 1. Maintain one warm semantic corpus context per vault

Keep the existing bounded process cache as the rebuildable semantic snapshot. Its hot key is the freshness event token plus the small registry/config census, so a normal request does not stat every Markdown page. Canonical writes and watcher batches patch exact changed/deleted Markdown paths in that cached context. A full census remains the correctness fallback when no event token exists or continuity is uncertain, but a Markdown-only mismatch reparses only the changed paths.

Cold startup primes the context in the existing background warm-up. If Markdown changes during that build, the builder absorbs a bounded number of exact census deltas rather than throwing away the completed parse. Registry/schema changes still invalidate the context and use a cold rebuild because their meaning is corpus-wide.

Alternative considered: add a durable semantic snapshot and delta journal in this repair. Deferred because it expands the storage/recovery protocol substantially; the observed regression was in a persistent service whose in-process cache was invalidated and rebuilt on foreground requests. The current change fixes that measured path while keeping Markdown canonical.

### 2. Incrementally evaluate common same-page transitions

`SemanticCorpusContext.with_candidate()` gains a stable-topology replacement path. When path, title, stable identity, eligibility, and resolver keys are unchanged (the normal `observe_memory` update), it reparses only the candidate page and reuses the cached corpus pages/resolver inputs. It may rederive relation and activation maps from cached facts, but it does not reopen or reparse unchanged Markdown. Structural changes that affect resolver keys retain the full correctness oracle.

If the event token cannot prove currency, Exomem checks the existing exact census. It applies Markdown-only differences incrementally and uses the full build oracle only for a cold cache or corpus-wide configuration change.

### 3. Query semantic-unit and outside-KB rows before hydrating Markdown

Semantic-unit recall asks the lexical/vector sidecars for matching current-generation rows first. Category and kind constraints are pushed into the sidecars before ranking; remaining structured predicates are evaluated while hydrating a bounded candidate window. If that window cannot satisfy the requested result count, the response reports `semantic_units_candidate_window` instead of silently claiming complete recall. The optimized unit lane no longer builds an all-unit in-memory dictionary as a prerequisite to searching; filtered page-level/mixed eligibility outside this lane is not covered by that claim.

Outside-KB widening uses the WAL lexical sidecar’s vault-scope FTS rows directly rather than reconstructing an in-memory vault BM25 corpus after every freshness change. The existing relaxed any-stem gate and reserved result slots remain unchanged.

Fallbacks remain correctness-first but bounded: a missing/stale sidecar starts or joins background repair and reports the affected lane as warming/unavailable. A foreground request never silently falls back to an unbounded whole-vault scan.

### 4. Reuse already-parsed state on the guarded commit path

Validation already constructs the current corpus and resolver inputs. Before the guarded commit, seed the shared wikilink resolver from those exact entries so graph/index fanout does not immediately reopen and parse the entire vault. Canonical transaction, writer fencing, read-your-write behavior, and existing optional-worker boundaries remain unchanged by this repair.

### 5. Gate both latency and scaling

Add a persistent-process, model-free benchmark over deterministic 2,000- and 8,000-page generated vaults. It measures cold semantic warm-up plus repeated `observe_memory(validate)` and guarded commit. Structural retrieval/cache tests separately prove that an unrelated change or keyword recall does not reopen every parent.

Acceptance on the reference lane is: local validate median below 500 ms and p95 below 1 second, commit median below 750 ms and p95 below 1.5 seconds at each size, and both 8k medians must remain below `2 * their 2k median + 200 ms`. Structural tests—not the noisy wall-clock ratio—assert that no full Markdown parse occurs on warm/event-maintained paths. Thresholds exclude cold semantic warm-up, optional model download, and one-shot interpreter startup.

### 6. Treat managed CLI/service identity as one install contract

Add a cheap `exomem --version` / `exomem install-info --json` path that imports no optional ML stack and reports distribution version, interpreter, install source, selected profile, and managed-service target without secrets or vault paths.

Windows and Unix upgrade scripts write a non-secret managed-install manifest after verifying the live service release, then resolve every `exomem`/`kb` command visible on PATH. In `auto` mode an existing uv-tool install is upgraded to that exact release; `always` may install it and `never` opts out. Verification fails with a concrete repair command if any visible executable remains stale. The command environment stays lean; parity means the same Exomem release, not duplicate media/ML dependencies.

The historical `find` command becomes a compatibility alias for current `ask` semantics. Before loading command leaves, a lean direct CLI detects an absent local model stack and disables embedding, reranking, and CLIP lanes for that invocation. Once release-aligned it therefore uses the bounded lexical sidecars without the obsolete CLI's raw missing-ML warnings; structured diagnostics still describe explicitly requested unavailable capabilities.

## Risks / Trade-offs

- **Watcher loss could leave the cache stale** -> compare the exact existing corpus census whenever the freshness event token cannot prove currency; apply Markdown-only differences as bounded deltas and retain the full-build oracle for cold/config-wide changes.
- **Incremental resolver updates are subtle** -> store raw-target dependencies, test rename/title/alias ambiguity transitions, and retain a full-rebuild oracle for equivalence tests.
- **Process restart loses the warm corpus** -> start a background semantic warm-up immediately; never treat the cache as canonical.
- **Wall-clock tests can be noisy** -> pair generous sub-second ceilings with deterministic no-walk/no-full-parse assertions and scaling ratios; run connector p95 on the dedicated performance lane.
- **Automatic CLI reconciliation may touch an independently managed tool** -> limit it to commands that resolve to the Exomem distribution, show planned/current/target provenance, and support an explicit opt-out while doctor remains actionable.

## Migration Plan

1. Ship the in-process cache/event maintenance and bounded sidecar recall changes without a storage migration.
2. Keep the full semantic build as a cold/config-change correctness oracle and prove incremental equivalence in tests.
3. Update upgrade scripts to record verified service provenance and reconcile existing uv-tool CLIs.
4. Deploy to the follower first, verify readiness and aggregate latency, then upgrade the writer and allow normal lease election. Roll back by installing the previous wheel; Markdown remains canonical and every cache/sidecar is rebuildable.

## Open Questions

- A durable semantic snapshot and authenticated warm loopback CLI remain possible follow-up changes, but neither is required to fix the measured persistent-service regression or the stale executable split.
