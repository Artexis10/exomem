## Why

Exomem keeps knowledge canonical in Markdown files and keeps several derived SQLite stores (lexical catalogue, epistemic graph, stable references, deferred work) in step with those files after each write. The files change outside any transaction, so every derived store needs its own freshness keys, drains, receipts, drift audits and repair paths. That machinery took a large share of the recent work:

- **Friction.** From 2026-10-07 to 2026-11-07, 825 PRs merged. By title, about 187 of them, roughly a fifth (98.8k changed lines, ±10%), were store-sync friction: catalogue rebuild, lag and repair (64), the Records SQLite move (41), locks and lease (36), graph convergence and snapshots (32), watcher self-echo (7), Windows sidecars (4), and Markdown, CRLF or frontmatter (3). Store mode targets the causes of the catalogue, graph, echo, sidecar and Markdown items (110 PRs), not the Records move or the lease work.
- **Write latency in production.** One production service's own logs, from 2026-11-06T01:24Z to 2026-11-07T08:36Z, show 358 governed writes. From receipt to return they took p50 4.8 s and p95 43 s. The Markdown commit inside them took p50 385 ms and p95 1.1 s.
- **Where the time goes.** In the spike, an offline copy of that 13.7k-page vault replayed 240 governed writes through today's path. The writes acknowledged at p50 8.8 s. The Markdown commit took p50 388 ms. The post-commit derived fan-out (lexical, graph, references, self-write registration, vocabulary) took p50 3.15 s and p95 5.24 s, and it runs after the canonical bytes are already committed. At acknowledgement, 42 of 220 checkable new graph edges were present.

The spike also built the alternative: one SQLite store, canonical for page bytes, lexical rows and graph rows, written in one transaction, with Markdown projected out of it. Fed the same bytes per write, its derived rows equal today's sidecars on whole tables, with zero differing rows. Its storage part of a write takes p50 1.65 s against today's 3.57 s, a mean of 2.09 s less per write (95% CI 1.95 to 2.24 s), of which up to about 0.7 s is fan-out work the prototype skipped and store mode still has to do. Every lexical row and every checkable graph edge is present at acknowledgement. On a native Windows runner with a synthetic vault the gap is wider: today's storage part took p50 13.1 s against the store's 0.38 s, of which about 2.8 s is skipped fan-out work, and today's search row was present at acknowledgement for 4 of 99 writes. None of 20 hard kills left it anything to repair. Today's path lost no data either, but after 2 of 12 random kills its search index stayed wrong after startup recovery; on two kills placed in that window, a reader's first search repaired it. The design records the full numbers, their intervals and their limits.

**Ruling reversed.** The owner has decided (2026-11-07) to reverse the ruling **knowledge stays Markdown** in `move-structured-collections-to-sqlite` (`proposal.md`, "The owner has decided"). The SQLite store becomes canonical for knowledge pages too, and Markdown becomes a projection of it. The graph stays a first-class part of the design. The owner decides from these numbers whether the switch happens before or after the public release.

## What Changes

- **Store mode.** A vault in store mode keeps one canonical SQLite database: the per-vault collection store database, extended with pages (exact bytes and frontmatter), the lexical catalogue tables, the graph tables, a projection manifest and the audit trail. Binary media stays on disk, referenced by path and hash.
- **One transaction per governed write.** Exomem derives the lexical and graph rows from the bytes being written, with the same derivation code as today. It then commits bytes, rows, audit and projection intent in one `BEGIN IMMEDIATE`. Readers see the write at acknowledgement, with no drain, watcher event or repair.
- **Markdown as a byte-exact projection.** A projector writes each committed file after commit, makes it durable, then records it in the manifest. Obsidian and other tools keep reading and editing Markdown.
- **Outside edits.** A file whose hash differs from the manifest is an outside edit. It imports as a governed write when the store has not moved; otherwise it is held as a correction for review. An outside delete is held, never applied silently.
- **Two stages.** Stage 1 is store mode with the same derived rows as today. Stage 2 adds semantic units and canonicity on the same store.
- **Semantic units as first-class rows (stage 2).** Each unit gets one row with today's identity rule, its provenance, verdict and canonicity. Typed edges can start or end at a unit, and a claim-level write changes the page and the unit together.
- **Canonicity of originals (stage 2).** Every stored file records its origin, hash, size, format and derivation. A file wrongly filed as an original can be superseded by the true original and kept as derived.
- **Migration and rollback.** Migration runs offline and verifies equal derived rows and byte-identical projection before activation. Rollback drains the projection, refuses while held edits are unexported, exports the audit trail and canonicity records, then rebuilds file mode from the Markdown.
- **What store mode retires.** For writes that Exomem commits, store mode needs no lexical freshness witnesses and catalogue repair, no graph sync checkpoints and drains, no derived receipts, no deferred full-refresh receipts and no self-write echo suppression. File mode keeps them until it is retired.
- **Root layout and readable names (D12).** A separate package, task group 9, drops the wrapper folder and the `Notes/` level, names pages after their titles with vault-wide unique names, and moves existing vaults through one verified relayout. It never gates the switch.
- **File mode stays** until the switch. Both modes share the derivation code and the readers.

## Capabilities

### New Capabilities

- `canonical-vault-store`: the per-vault canonical store, its one-transaction write contract, byte-exact Markdown projection, outside-edit import with base-revision checks, semantic units as first-class rows, canonicity of originals, and verified, reversible migration.
- `vault-layout`: the managed root, areas named by registries, flat page paths, unique readable names, short links, renames that keep derived names matching titles in `root` vaults, path aliases and the verified relayout.

### Modified Capabilities

- `epistemic-graph`: graph rows live in the canonical store in store mode and commit with their page; file mode is unchanged.
- `live-index-freshness`: in store mode, lexical rows commit with their page, and graph audit re-derives from store bytes instead of repairing from Markdown.
- `command-surface`: in store mode, a move repoints links in append-only `Sources/` and `Evidence/` pages and changes no other byte.
- `adoption-studio`: in a `root` vault, a classified item's Source directory is `Sources/<Kind>/`.

## Impact

- **Code.** The write path (`vault.batch_atomic_write`, `index_sync`), the collection store (`collection_store/`, reused as the canonical store, with its `migration.py` and `export.py`), the lexical store and graph writers (their row builders are reused unchanged), and the watcher (it becomes the outside-edit importer). In store mode the work removes most of `graph_sync`, `graph_drain`, `derived_drain`, `derived_receipts`, `deferred_index`, `freshness` and the repair paths in `lexstore` and `epistemic_graph`.
- **Related changes.** `add-graph-traversal-queries` already plans to move graph rows into the per-vault store while keeping Markdown authority. This change makes the store the authority, so that change's traversal work runs on canonical rows. `add-collection-query-engine` keeps its IR and executor over the same database. The user-defined collection types (P4 and P5 of `move-structured-collections-to-sqlite`) land on the collection store, which is the store that survives; that boundary was agreed with their owner on 2026-11-07. Store mode turns three contracts in those changes into file-mode clauses: knowledge stays out of the collection store, knowledge writes continue while the store is unavailable, and graph writes keep Markdown authority with replay. Task 1.6 amends them.
- **Users.** Obsidian users keep editing Markdown; their edits return through the import path. Cloud cells run the file watcher today; in store mode a cell can skip per-write projection and export Markdown on demand.
- **Operations.** One database per vault becomes the backup and restore unit. The in-vault Markdown stays a complete, readable copy, and the collection store's in-vault replica keeps covering collection tables only.
