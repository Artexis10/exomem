## Context

Today a governed write commits Markdown files first and then updates each derived store separately: the lexical catalogue (`.lexical.sqlite`), the epistemic graph (`.graph.sqlite`), stable references, semantic units and deferred work. A kill, a lock timeout or an outside edit between those steps leaves the stores disagreeing, so each store carries freshness keys, drains, receipts, drift audits and repair. The Records and Planning move (`move-structured-collections-to-sqlite`) showed that a canonical SQLite store removes that class of problem for collections. This design applies the same move to knowledge pages and the graph.

The spike behind these numbers lives on the diagnostic branch `spike/single-store-pA9` (`scripts/spike_single_store/`). That branch is never merged.

## Goals / Non-Goals

The change ships in two stages. Stage 1 is store mode itself (D1 to D9). Stage 2 makes semantic units and the canonicity of originals first-class (D10, D11) on the same store.

**Goals**
- One canonical store per vault; one transaction per governed write; derived rows visible at acknowledgement.
- No repair state after a hard kill at any point of a write.
- Markdown stays a complete, byte-exact, editable copy, and outside edits return safely.
- In stage 1, the same derived rows as today, so recall, graph context and every reader behave the same.
- In stage 2, unit-level rows and edges, and a canonicity record for every stored file.
- A verified, reversible migration.

**Non-Goals**
- No change to governance, vocabulary, the public tool surface, semantic authoring or the unit identity rule.
- No graph database service. Neo4j is not needed.
- No new retrieval behaviour in stage 1: it changes where rows live and when they commit, not how readers rank. Stage 2's unit results and unit-level edges are new behaviour with their own acceptance (D10).
- Embeddings stay derived and asynchronous; this change does not make vectors transactional.

## What the spike measured

All numbers come from run files on the spike branch's harness. "Today" is the file-canonical path driven through the governed commands (`create_note`, `edit_note`) with the local runtime running (watcher, drains, catalogue recovery). "Store" is the prototype fed the exact files each governed write committed. Embeddings are off in both arms. One governed write committed 6.0 files on average: the page, `log.md`, about 2.6 navigation `index.md` files, a relation review and sometimes compiled history.

Paired run on a copy of a 13.7k-page vault (ext4; 240 writes, 237 accepted by both arms). For p50 and p95 the difference column is the difference of the two arms' percentiles; for the mean it is the mean paired difference. Intervals are bootstrap 95% intervals over the paired writes. Proportions carry Wilson 95% intervals, which treat writes as independent; they are not, so read them as describing this stream.

| Measure | Today | Store | Today minus store (95% CI) |
| --- | ---: | ---: | ---: |
| Whole governed write, p50 | 8.8 s | — | governance is common to both |
| Storage part of the write, p50 | 3.57 s | 1.65 s | 1.91 s (1.76 to 2.05) |
| Storage part of the write, p95 | 5.65 s | 1.83 s | 3.81 s (3.2 to about 5) |
| Storage part of the write, mean | | | 2.09 s (1.95 to 2.24) |
| Markdown commit alone, p50 | 388 ms | — | |
| Derived fan-out after commit, p50 / p95 | 3.15 s / 5.24 s | none: inside the transaction | |
| Store: derivation / transaction, p50 | — | 391 ms / 1.23 s | |
| Lexical row present at acknowledgement | 229 of 237 | 237 of 237 | |
| New graph edge present at acknowledgement | 42 of 220 (14.4–24.8%) | 220 of 220 (98.3–100%) | |
| Graph edge still absent 60 s after acknowledgement | 167 of 220 | none | |
| Projection lag after acknowledgement, p50 / p95 | — | 59 ms / 379 ms | |
| Projection cost per file written, p50 / p95 | — | 69 ms / 1.15 s | |
| Random hard kills that left work for recovery | 2 of 12 | 20 of 20 (queued projections only) | |
| Random hard kills still wrong after startup recovery (330 s) | 2 of 12 (4.7–45%) | 0 of 20 (0–16%) | |
| Kills placed between Markdown commit and fan-out, still wrong after startup recovery | 2 of 2 | not applicable: no such window | |
| Cold build or migration | 82 s lexical + 50 s graph | 12.3 s migration, 1.56 GB file | |

Graph counts cover the 220 writes whose new link could be checked: of the other 17, 15 had link text that the spike's check could not match (non-ASCII characters; fixed after the run), and 2 link to a title that two pages share, which resolves in neither arm. Today's misses are not spread evenly. In the paired run, edges were present at acknowledgement for 19 of writes 0 to 25, for none of writes 26 to 188, and again for writes 189 to 192 and 213 to 232. The earlier run showed the same long stall, from write 70 to its end. A graph rebuild was still open at the end of the paired run. The edges arrived after the stream ended: the whole-table diff at the end shows no missing rows. Today's 8 lexical misses were all in the first 9 writes, a start-up wait rather than a steady rate. Projection wrote 1,421 files for 505 distinct paths, because `log.md` and the navigation indexes are rewritten by almost every write. A separate earlier run of today's path on the same vault (237 writes, less machine contention) gave p50 7.9 s for the whole write and 3.33 s for its storage part.

**What a kill leaves behind.** No kill tore a page in either arm. Every today's-path kill that left work left the same thing: the new page missing from the lexical index and five navigation pages stale. Startup recovery caught up the graph but not those lexical rows; a reader's first search then repaired them (measured on the two placed kills). So today's path loses no data on a kill, but its search index stays wrong until something reads it. Every store kill left only queued projections, and recovery wrote all of them with every file byte-identical to the store. Random kills landed within 60 s of the start for today's path and within about 8 s for the store, because the store's writes are faster.

**Outside-edit rehearsal.** A fresh copy of the real vault, which was only read, was migrated (19,631 stored files), and 28 editor-style changes to real pages ran through the prototype's sweep: 16 clean edits (an appended line, an inline word, a frontmatter property, a whole file rewritten with CRLF line endings), 2 timestamp-only touches, 2 edits that kept size and mtime, 3 edits staged against a queued store write, 2 deletes, 2 renames and 1 new file. All 28 ended as D5 says. The 12 body edits and the new file imported and were searchable; the 4 property edits were checked by their stored bytes only. Touches imported nothing. The staged edits were held, and the sweep overwrote neither version; the projector was stopped, so a live race was not tested. The prototype's projector does not yet compare a file with the manifest before replacing it, so in a live race it would overwrite the person's edit (task 4.1). Deletes were held. Renames paired by hash. The two edits that kept size and mtime were missed by the size-and-mtime sweep and caught by the full-hash sweep. A full-hash sweep of all 19,680 files took 0.5 to 0.84 s with the files in the page cache; a cold-cache sweep was not measured. The first sweep also exposed a scope mismatch in the prototype: its migration stored 2 vault-root files outside the managed Knowledge Base and skipped 52 files under dot-directories inside it, so the sweep reported them as deletes and creates. Migration, sweep and projection must share one ownership rule (task 1.2).

Today's lexical rows also arrive late on smaller vaults, where its graph edges do not: on a 2,000-page synthetic vault, 17 of 30 lexical rows arrived after acknowledgement (p50 10.7 s) and 5 not within 60 s.

**Native Windows/NTFS.** A GitHub `windows-latest` runner (a small VM, not a desktop) ran a 13,000-page synthetic vault with a 100-write stream; 99 writes are paired. Today's storage part took p50 13.1 s and p95 28.9 s, almost all of it fan-out (p50 12.4 s). The store's took p50 0.38 s and p95 0.64 s, a mean paired difference of 14.9 s (13.6 to 16.2). As on ext4, the prototype skipped fan-out work that store mode still has to do; on Windows it averaged 2.8 s, so the gap stays above about 12 s. At acknowledgement today's lexical row was present for 4 of 99 writes and its new graph edge for none; 60 s later 94 lexical rows and all 99 edges were still absent. The store had both for all 99. After 15 minutes of startup recovery, today's catalogue still lacked 22 pages, 14 of them new notes. Once a reader's first search repaired them, the whole-table diff showed zero differing rows (13,070 pages, 26,405 semantic units, 50,730 graph edges). Projection cost p50 31 ms and p95 87 ms per file, once the projector retried a Windows sharing refusal on rename as Exomem's own writer does; without that retry it stalled. Export lag is not reported for Windows: projection often finished before the store's commit call returned (p50 −0.18 s), which points to work inside the commit after the data became visible, most likely SQLite's automatic checkpoint (not verified). An earlier Windows run with hard kills matched the ext4 pattern: 1 of 6 random kills of today's path left the same lexical leftovers after recovery, and 0 of 10 store kills needed repair. All six kills of today's path landed before its first write acknowledged, so that sample is weak.

**Equivalence.** Fed the same bytes per write, the store's derived rows equal today's sidecars on whole tables, apart from write-time and write-order columns. The diff compares whole rows by key and excludes only the page modification time and write-order columns. On the real-vault copy it covered 13,089 lexical pages, 63,170 semantic units, 76,249 graph nodes, 79,628 graph edges, 21,318 graph dependencies and 12,595 parent references, with zero differing rows. It also held on an 800-page and a 2,000-page synthetic vault. Readers that only query these rows therefore return the same answers in both modes. D3 changes where every reader reads from, so the rollback drill and the acceptance run still compare answers (tasks 1.5 and 7.1).

**What the store's transaction contains.** The transaction re-derives and re-indexes every Markdown file the write committed. Almost every governed write rewrites `log.md`, a 1.3 MB page, so both arms re-index it on every write. How much of the 1.23 s that costs was not measured.

**Production calibration.** One production service's logs over 31 hours show 358 governed writes at p50 4.8 s and p95 43 s from receipt to return, with the Markdown commit at p50 385 ms and p95 1.1 s. The spike's Markdown commit matches production (388 ms). Its whole write ran slower (p50 8.8 s against 4.8 s) on a shared, loaded laptop.

**Spike limits.**
- The prototype skips re-resolution of other pages whose links start resolving when a new title appears. The replayed stream links only to existing pages, so this did not affect the equivalence result.
- The prototype feeds bytes to Exomem's derivation through temporary overrides of the parse cache, the parent-state read and the recall-candidate gate. Production code needs real parameters instead (task 2.1).
- Embeddings were off in both arms.
- The arms ran one after the other, on a laptop shared with other sessions, at different load: the 1-minute load average was 14.1 when today's arm started and 7.0 when the store arm started. Pairing by write does not remove that.
- The store arm derives only lexical and graph rows. Today's fan-out also delivers vocabulary (389 ms mean), semantic states (220 ms), memory references (73 ms) and path custody (51 ms). Store mode still has to do that work, inside or after the transaction, so up to about 0.7 s of the 2.09 s mean saving may not survive production.
- The 12.3 s migration covers loading bytes and copying rows. It excludes both verifications of D8; a fresh derivation alone costs about a cold build.

## Decisions

### D1. The canonical store is the per-vault collection store database

Pages join the database that already holds collections, rather than a new file. One file means one transaction can cover a page and a collection row, one backup unit and one migration framework (`collection_store/migration.py`, `export.py`). New tables hold page bytes and frontmatter (`store_pages`), the projection manifest and queue, held corrections, and the audit trail. The lexical catalogue and graph tables keep their current schemas inside the same file, so their readers and row builders are reused unchanged. The spike proved the schemas coexist: no table names clash.

*Alternative rejected:* a second "page store" file beside the collection store. SQLite in WAL mode does not commit atomically across attached databases, so two files would put two transaction owners back between a page and the collection row that cites it.

**What the store owns.** Every text file a governed write commits: pages, navigation indexes, logs, compiled history and the governed schema overlay. Binary media stays on disk (D2). Dataset files that the structured-collections contract keeps file-canonical stay outside the store, and vault content outside the managed Knowledge Base stays a read-only input, as today.

**What joining the collection store changes.** Three things follow from one file, and the collection changes must say so (task 1.6):
- The collection store's in-vault replica keeps covering collection tables only. The Markdown projection is the in-vault copy of pages; republishing a 1.56 GB database every minute is not. The replica today comes from the SQLite online backup API, which copies the whole file, so it changes to a staging database filled from the collection tables inside one read transaction.
- Knowledge writes share the store's availability. When the store cannot accept writes (engine unsupported, sync pending, diverged or unknown custody), a store-mode knowledge write refuses with the store's reason. Today's collection contracts promise that knowledge writes continue in those states; that promise holds only in file mode.
- Store-mode writes take the collection store's writer lease and transaction owner. Multi-host writer-lease takeover assumes every host receives the same canonical vault through file replication; in store mode the canonical pages live in the store, which file replication never copies. Store mode therefore refuses to start or activate while multi-host writer-lease coordination is configured, until a takeover path carries the store. The clauses in `move-structured-collections-to-sqlite` and `add-collection-query-engine` that keep knowledge out of the store and build the replica by whole-file backup, the Markdown-authority and replay requirements in `add-graph-traversal-queries`, and the replication assumption in `multi-host-writer-lease`, become file-mode clauses.

### D2. Derive from bytes, then commit everything in one transaction

The writer parses the incoming bytes with today's derivation (`find._parse_page`, `semantic_index.current_parent_index_state`, `epistemic_graph._structural_edges_for_page`, the lexical `_insert_page` row builder) before the transaction. It then runs one `BEGIN IMMEDIATE`. The transaction writes the files' bytes, deletes and inserts the lexical and graph rows, writes one audit row and one projection intent per file, and bumps the store revision. The write acknowledges after commit with `synchronous=FULL`. When a new title makes links on other pages resolve, the transaction also rewrites those pages' graph rows. A binary file the write preserves is written and made durable on disk before the transaction that records its path and hash commits, so a kill can leave an unreferenced file but never a reference to a missing one.

In the spike, derivation took 391 ms and the transaction 1.23 s at p50 on the real-vault copy, including a re-index of the 1.3 MB `log.md` that almost every write rewrites.

### D3. Readers read the store

Every reader of page bytes or derived rows reads the store: lexical, graph, reference and semantic-unit readers, page reads, `find`'s parse cache (keyed by store revision instead of file mtime), the frontmatter reads that governance makes, and the registry overlay loaders that read the schema overlay's YAML files. Row readers keep their query code and point at the store's tables. In store mode they drop freshness witnesses, mtime checks and sync-on-first-use repair, because a committed write cannot leave those rows behind.

### D4. Markdown is a projection

A projector thread writes each queued file after commit as a temporary file, makes it durable (`fsync`), renames it into place, then records hash, size, mtime and revision in the manifest and clears the intent. Durability before the manifest entry matters: a torn projection must never look like an outside edit. At startup the projector removes its own temporary files and drains the queue. Several queued revisions of one file coalesce into the latest. Status reports queue depth and the oldest intent's age.

In the spike, projection cost 69 ms per file at p50 (1.15 s at p95 on a contended machine), and back-to-back writes outran it. With an fsync per file the projector wrote 7.5 files a second on the real-vault copy (1,421 files in 190 s of busy time), while the production peak is 5 writes a minute, about 30 files. A Cloud cell has no editor reading its Markdown, so it can skip per-write projection and export on demand.

### D5. Outside edits import behind a base-revision check

The watcher stays, but its job shrinks to one question: does a file's hash differ from the manifest? A watcher event hashes the touched file. A periodic sweep hashes every stored file, because an edit can keep a file's size and mtime. On a difference:

- If the store revision equals the manifest revision and no intent is queued, the file imports as a governed write (D2) with outside-editor provenance.
- Otherwise the outside bytes become a held correction, and neither version is overwritten. This generalises the collection views' edit-back to pages.
- An outside delete becomes a held deletion.
- A rename arrives as a delete plus a new file. When the new file's hash equals the deleted file's manifest hash in the same sweep, it imports as a governed move; otherwise both halves follow the rules above.

Exomem's own projections match the manifest, so self-write echo suppression disappears. The spike's reconcile pass implements this check. Over today's production logs, edits from outside Exomem cannot be counted, because the watcher records them only in memory and at debug level. Store mode makes them auditable.

### D6. The graph stays, on the same rows

The graph keeps its tables, relation registry and derivation. Traversal runs as recursive SQL with the visibility check at every hop, as `add-graph-traversal-queries` already plans. Whole-graph algorithms (PageRank-style centrality, components, communities) run in memory on demand and are cached by store revision. Measured on the migrated real-vault store (79,628 edges; 13,087 pages with 10,942 page-to-page links; 80 seed pages; one shared laptop):

| Capability | Cost on the store | Serves |
| --- | ---: | --- |
| 1-hop neighbourhood (recursive SQL, per-hop node check) | p50 0.04 ms, p95 0.10 ms | context expansion, "what links here" |
| 2- and 3-hop neighbourhood | p95 0.23 ms and 0.66 ms | evidence chains, impact review |
| Lexical hits expanded one hop, in one statement | p50 1.2 ms, p95 11.6 ms | graph lane of recall, only possible in one store |
| Load page graph into memory | 1.9 s, once per store revision | prerequisite for algorithms |
| PageRank, 20 iterations | 42 ms | hub detection, curation priority |
| Connected components / isolated pages | 7 ms | orphan curation |
| Label-propagation communities, 10 rounds | 58 ms | topic maps, consolidation candidates |
| Shortest path (BFS) | p50 0.5 ms, p95 1.3 ms | "how are these connected" explanations |

None of these is expensive. What limits them is the data: 8,835 of the 13,087 pages (68%) have no page-to-page link, and a 3-hop neighbourhood reaches one page at p50 and 64 at p95.

**Why the graph is limited today.** The machinery that keeps a separate graph sidecar consistent with Markdown has taken the effort: 32 graph-convergence PRs in the month measured. The existing walker supports bounded neighbourhoods; paths, patterns and evidence chains wait in `add-graph-traversal-queries` (0 of 39 tasks done). Store mode removes the convergence cost. Every capability above is a query or a short in-memory pass over rows that already exist, so building them is ordinary query work, not storage work.

**Does the graph justify itself for recall? Not yet.** An ablation ran the recall cases of the real-vault copy with the graph lane on and off, on the same cases. Labels are pages that a governed write cited within 2 hours of the query, mined from the live service's logs of 2026-09-09 to 2026-11-07. Offline runs have no vectors, so the graph lane seeds from lexical hits only.

| Case set | Cases | hit@10, on minus off (95% CI) | Reciprocal rank, on minus off (95% CI) | Reciprocal rank wins / losses |
| --- | ---: | ---: | ---: | ---: |
| Cited pages the live system surfaced | 106 | +0.019 (0 to 0.047) | −0.003 (−0.015 to 0.005) | 2 / 3 |
| Cited pages, sampled without regard to surfacing | 401 | 0 (0 to 0) | 0 (0 to 0) | 0 / 0 |

Every interval includes zero: no change. These offline sets have little power: the cited page reached the top 10 in only 25 of the 106 cases and 6 of the 401, so they can rule out only a large effect. The live service's own query and read logs (vectors on) carry more weight, and they agree:

- 4.6% (4.5 to 4.8) of returned hits came from the graph lane alone.
- Agents read 0.57% (0.40 to 0.82) of those within 30 s, about a sixth of the rate for other hits at the same ranks.
- Of 99 hits that a write cited within 2 hours, none came from the graph lane alone (0 to 3.7%).

The graph has to earn its place through denser, typed links rather than through its current recall lane. Unit-level edges (D10) and the traversal capabilities above are where that density can come from. The ablation reruns after D10 (task 5.4). Its effect on agent outcomes, not only on retrieval, belongs to the outcome evaluation (see Epistemics evaluation).

### D7. Embeddings stay asynchronous

Vector rows stay derived and are computed after commit by the semantic drain, keyed by page hash and store revision. A reader that needs vectors for a page newer than its vectors falls back to lexical and graph lanes, as it does today when vectors lag.

### D8. Migration and rollback

Migration runs offline:

1. Load every text file's exact bytes and frontmatter.
2. Copy the current lexical and graph rows.
3. Verify that a fresh derivation from the loaded bytes equals the copied rows.
4. Verify that a full projection reproduces every file byte for byte.

In the spike, loading and copying the real-vault copy (19,563 text files) took 12.3 s and produced a 1.56 GB database; the two verifications add about a cold build.

Some data exists only in the store: the audit trail, queued projection intents, held corrections, held deletions and, in stage 2, canonicity records. Rollback therefore runs in this order:

1. Drain every queued projection; a cell with projection off projects in full first.
2. Refuse while held corrections or held deletions remain, unless the operator exports them for review. On export, Exomem moves the person's held bytes into the export and projects the store's version of each held file, so the rebuild starts from the store's state and the held edits stay reviewable instead of being applied by the rebuild.
3. Export the audit trail and canonicity records as files beside the vault.
4. Stop store mode and let file mode rebuild its sidecars from the projected Markdown.

### D9. What store mode retires

For writes that Exomem commits, store mode does not need the modules that keep sidecars in step with files: `graph_sync` (4,153 lines), `graph_drain` (844), `derived_drain` (1,066), `derived_receipts` (3,408), `deferred_index` (2,733), `freshness` (2,468), `index_sync` (2,439), `pending_recall` (934), and the repair, witness and catch-up paths inside `lexstore` (12,289 lines in total) and `epistemic_graph` (14,880 in total). The watcher (2,564 lines) shrinks to the manifest importer. File mode keeps them until it retires; deleting them is the last phase.

### D10. Semantic units are first-class rows

Stage 2. Pages stay the documents people read and edit, and they are composed of semantic units. Today each unit is stored twice, as a lexical `semantic_units` row and as a graph unit node, in two files that can disagree. In store mode each unit is one row with:
- its identity (`unit_ref`), under today's rule: the authored block anchor when the page carries one, otherwise the fingerprint-bound reference. The store derives it from the bytes and never writes anchors into a page;
- its page, ordinal and exact byte span in the page;
- category, kind and text;
- provenance (its sources);
- its verdict from the existing verdict set, its lifecycle and its canonicity (D11).

Typed edges run between units as well as between pages: a link to `[[Page#^anchor]]` resolves to the unit that carries that anchor. A claim-level operation, such as verifying, refuting or superseding one claim, is a governed write that changes the page bytes and the unit row in one transaction. Recall can therefore return units with their page context, and graph edges can point at the exact unit a claim lives in. That is new retrieval behaviour: it ships only after the graph ablation reruns on unit-level edges and the outcome evaluation runs on the stage-2 build. The merged unit rows must still reproduce today's lexical unit rows and graph unit nodes for the same bytes.

*Option kept:* page bytes are the serialisation and unit rows derive from them in the same transaction. One source of truth holds because the two cannot diverge, and the Markdown projection stays complete for editors. *Option rejected for now:* unit-canonical storage, where a page is composed from stored segments. It offers no capability beyond the rows above and makes every outside edit a segmentation problem.

### D11. Originals record their canonicity

Stage 2. Every stored file, a text page or binary media, carries a canonicity record:
- **origin:** how it entered (upload, import source, capture tool, client);
- **content hash, size and format;**
- **derivation:** original, or derived from a named hash by a named transform, such as compression by a client, transcoding or text extraction.

When the true original of content already filed as an original arrives, a governed write supersedes the earlier record. The earlier file stays and becomes derived from the true original. Citations resolve to the original, and the audit trail records the change. This is the failure seen when compressed copies from a chat client were filed as originals and the true originals arrived later from a file store: store mode makes the correction one transaction instead of a hand repair across files. Task 5.3 maps the record onto the existing Evidence custody fields.

## Estimate

**Evidence.**
- The Records and Planning move is the closest precedent. Its proposal dates from 2026-10-01, and its first store commit landed on 2026-10-04. It then had 107 commits to `collection_store/` on 26 working days, and GA closure was still open on 2026-11-07. Five weeks bought one store for collections, with about 36k lines in `collection_store/`.
- This change reuses that store, its migration and export framework, its backups and its edit-back.
- The spike shows that today's row builders run unchanged against the store. The riskiest technical unknown, equal derived rows, is already answered.

**Work packages**, each sized against the Records move's pace:

| Stage | Package | Weeks |
| --- | --- | ---: |
| 1 | Store schema, offline migration with both verifications, rollback with its exports | 1 |
| 1 | Write path: real byte-input parameters in the four derivation seams; one transaction per governed write; dependent-page re-resolution | 1.5–2 |
| 1 | Every reader on the store; store-mode freshness removal | 1.5–2 |
| 1 | Projector, manifest, outside-edit importer, held corrections | 1.5 |
| 1 | Store availability, replica scope and the contract amendments in other changes | 0.5 |
| 1 | Acceptance on a real-vault copy: kill trials, recall canary, installed-wheel journey | 1 |
| 1 | Embeddings keyed by store revision | 0.5 |
| 1 | Cloud cells (projection off, export on demand) and Windows/NTFS | 1 |
| 1 | Delete the file-mode sync machinery | 1 |
| 2 | Semantic units as one row set, unit-level edges and claim-level writes (D10) | 1–1.5 |
| 2 | Canonicity records and the supersede-original write (D11) | 0.5–1 |

**Result.** These are single-author weeks. About 11 to 13 weeks in total. The smallest local release, the first six stage-1 rows, is 7 to 8 weeks. Embeddings, Cloud, Windows and deletion bring stage 1 to 9.5 to 10.5 weeks; stage 2 adds 1.5 to 2.5. No cut of this table fits one month for one author.

**Parallel schedule.** The smallest local release fits about four weeks of elapsed time with three parallel lanes:

1. Week 1, one lane: the store schema, migration with both verifications, rollback exports, the ownership rule, and the availability and replica work. This fixes the table contract the later lanes share.
2. Weeks 2 and 3, three lanes on separate files: the write path; every reader, built against a migrated store so it does not wait for the write path; the projector and importer, then embeddings keyed by store revision.
3. Week 4: integration and acceptance on a real-vault copy and on a Windows runner.

This schedule defers stage 2, file-mode deletion and Cloud cells until after the switch, and file mode remains the rollback path. Integration risk falls entirely in week 4, so any slip lands there. Store mode can stay behind a per-vault setting while a release ships in file mode, which separates the switch from the release.

## Epistemics evaluation

The design's epistemics check is a separate outcome evaluation, run in its own session on case sets it owns. It plants stale or wrong notes, each with a governed correction (supersession, or a refuted or qualified verdict on a claim), and originals filed wrongly as derived or the reverse, in a disposable vault copy. It then measures case by case whether an agent using recall flags or corrects them, and runs the same tasks with the graph lane on and off. Each comparison carries a bootstrap 95% interval, blind judging and answer keys written by a separate lane. Store mode must keep every signal that evaluation finds recall relies on; the D1 schema stores pages verbatim, so it drops none. Its case sets run against any build that serves MCP, so the store-mode build is measured on the same cases before the switch.

## Risks / Trade-offs

- **One database is one failure point.** A corrupt store affects the whole vault. Mitigation: WAL with `synchronous=FULL`, the existing collection-store backups, and the projected Markdown as a complete second copy that rollback rebuilds from after the exports D8 names.
- **Outside-edit conflicts become visible work.** Concurrent edits become held corrections instead of a silent last-writer-wins. That is the intent, but it is a new review surface for Obsidian users.
- **Projection lag.** Under bursts, Markdown trails the store by seconds. Readers never use Markdown, but a person in Obsidian may briefly see the older text. Status shows the lag.
- **Two modes during migration.** Both modes run until the switch, which doubles the test matrix for that period. Keep it short.
- **Derivation seams.** Production code must pass bytes into four derivation entry points that read from disk today. If one resists, the write path keeps a narrow adapter, as the spike does, and the estimate's upper bound applies.

## Open Questions

- Whether append-only pages such as `log.md` should become rows that the projection renders, so a write appends one row instead of re-indexing a 1.3 MB page. Measure the transaction with and without that file first.
- Switch timing: before or after the public release. The owner decides from these numbers.
- Whether a Cloud cell projects Markdown at all, or exports only on request.
