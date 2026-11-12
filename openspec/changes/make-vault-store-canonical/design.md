## Context

Today a governed write commits Markdown files first and then updates each derived store separately: the lexical catalogue (`.lexical.sqlite`), the epistemic graph (`.graph.sqlite`), stable references, semantic units and deferred work. A kill, a lock timeout or an outside edit between those steps leaves the stores disagreeing, so each store carries freshness keys, drains, receipts, drift audits and repair. The Records and Planning move (`move-structured-collections-to-sqlite`) showed that a canonical SQLite store removes that class of problem for collections. This design applies the same move to knowledge pages and the graph.

The spike behind these numbers lives on the diagnostic branch `spike/single-store-pA9` (`scripts/spike_single_store/`). That branch is never merged.

## Goals / Non-Goals

The change ships in two stages. Stage 1 is store mode itself (D1 to D9). Stage 2 makes semantic units and the canonicity of originals first-class (D10, D11) on the same store. A separate package, task group 9 (D12), moves the vault to a flat root layout with readable filenames. It never gates the switch.

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

Pages join the database that already holds collections, rather than a new file. One file means one transaction can cover a page and a collection row, one backup unit and one migration framework (`collection_store/migration.py`, `export.py`). New tables hold page bytes and frontmatter (`store_pages`), the projection manifest and queue, held corrections, and the audit trail. The lexical catalogue and graph tables copy into the same file, so their readers and row builders are reused unchanged. Their owners' own DDL creates them, without the graph's file-mode publication-capture triggers, and they are not `STRICT`, because full-text tables cannot be. The collection schema's `STRICT` rule covers only the tables it creates. The spike proved the schemas coexist: no table names clash.

*Alternative rejected:* a second "page store" file beside the collection store. SQLite in WAL mode does not commit atomically across attached databases, so two files would put two transaction owners back between a page and the collection row that cites it.

**What the store owns.** Every text file a governed write commits: pages, navigation indexes, logs, compiled history and the governed schema overlay. Binary media stays on disk (D2). Dataset files that the structured-collections contract keeps file-canonical stay outside the store, and vault content outside the managed Knowledge Base stays a read-only input, as today.

**Store mode needs an adopted collection store.** Not every vault has one: the collection migration creates none when it has nothing to move, and the service starts no store thread without one. Store mode never creates a store as a side effect. Migration refuses with `VAULT_STORE_NEEDS_COLLECTION_STORE` when the vault has no collection store, and with the store's own admission refusal (for example `COLLECTION_STORE_LEASE_REQUIRED`) when the store exists but cannot be admitted.

**What joining the collection store changes.** Four things follow from one file, and the collection changes must say so (task 1.6):
- The collection store's in-vault replica keeps covering collection tables only. The Markdown projection is the in-vault copy of pages; republishing a 1.56 GB database every minute is not. The replica today comes from the SQLite online backup API, which copies the whole file, so it changes to a staging database filled from the collection tables inside one read transaction.
- Knowledge writes share the store's availability. When the store cannot accept writes (engine unsupported, sync pending, diverged or unknown custody), a store-mode knowledge write refuses with the store's reason. Today's collection contracts promise that knowledge writes continue in those states; that promise holds only in file mode.
- Page writes do not advance the collection chain. The store revision lives in `store_mode.revision`, so a page write never advances `txns.commit_seq` and never makes the replica republish. Takeover's adopt step and the reverse export both act on chain position, so each would replace or tombstone the store and drop its pages. Both refuse in store mode.
- Store-mode writes take the collection store's writer lease and transaction owner. Multi-host writer-lease takeover assumes every host receives the same canonical vault through file replication; in store mode the canonical pages live in the store, which file replication never copies. Store mode therefore refuses to start or activate while multi-host writer-lease coordination is configured, until a takeover path carries the store. The clauses in `move-structured-collections-to-sqlite` and `add-collection-query-engine` that keep knowledge out of the store and build the replica by whole-file backup, the Markdown-authority and replay requirements in `add-graph-traversal-queries`, and the replication assumption in `multi-host-writer-lease`, become file-mode clauses.

### D2. Derive from bytes, then commit everything in one transaction

The writer parses the incoming bytes with today's derivation (`find._parse_page`, `semantic_index.current_parent_index_state`, `epistemic_graph._structural_edges_for_page`, the lexical `_insert_page` row builder) before the transaction. It then runs one `BEGIN IMMEDIATE`. The transaction writes the files' bytes, deletes and inserts the lexical and graph rows, writes one audit row and one projection intent per file, and bumps the store revision. The write acknowledges after commit with `synchronous=FULL`. When a new title makes links on other pages resolve, the transaction also rewrites those pages' graph rows. A binary file the write preserves is written and made durable on disk before the transaction that records its path and hash commits, so a kill can leave an unreferenced file but never a reference to a missing one.

Two kinds of derived state stay outside that transaction. The registry and access-policy state that the schema overlay's pages define is re-derived by a background job after the commit that changes those pages. The store reports a visible `rebuilding` state with the target revision until the job finishes, as file mode rebuilds the graph today, and the write does not wait for it. The commit records its revision in `store_meta` when it changes a source of the overlay: a page under `_Schema/` or the access policy file. The job derives the stored pages again in path order, one bounded transaction per batch, and records the revision that the rows then reflect. Each batch commits with its cursor, so a kill leaves the rows behind the store and the next start resumes at the cursor. The job is one tick of the store thread's scheduler, so it adds no thread, and the tick decides on a plain read, so an idle store takes no mutation boundary. A pass over a store that records no reflected revision keeps the target it began with, so commits do not restart it. Until the readers read the store, the registries and the access policy load from disk, so the job waits while the projector holds a changed overlay file queued or held; that wait lapses when the loaders read the store. The governance catalog, due state, claims and embeddings are sidecars that refresh after the commit. A kill between the commit and a sidecar refresh leaves the sidecar behind the store, never the store behind a sidecar, and the next refresh closes the gap from the store's bytes. Governance state keeps its own files around the commit. A trash or a recovery of a page that a policy restricts writes its receipt intent and its tombstone or recovery marker under the governance folder before the transaction. After the transaction, a trash drops the vector rows of the trashed paths, and the receipt settles from the store's rows and the vector and CLIP sidecars. Reconcile settles a kill on either side of the transaction from the store, as file mode settles one around its rename. An active catalog publication is prepared before the writer's validation returns. The writer's log entry commits in the same transaction as its change, and the catalog publishes after that transaction. A companion backfill commits the companion page in one transaction between the arming of its journal and its terminal. A pass changes derived rows without a page commit, so each of its batches moves the store revision, and every cache keyed on the store revision sees those rows (task 3.3). A kept registry version under `_Schema/history/` is not an overlay source: no loader reads it.

In the spike, derivation took 391 ms and the transaction 1.23 s at p50 on the real-vault copy, including a re-index of the 1.3 MB `log.md` that almost every write rewrites.

### D3. Readers read the store

Every reader of page bytes or derived rows reads the store: lexical, graph, reference and semantic-unit readers, page reads, `find`'s parse cache (keyed by store revision instead of file mtime), the frontmatter reads that governance makes, and the registry overlay loaders that read the schema overlay's YAML files. Row readers keep their query code and point at the store's tables. A byte reader keeps its store-mode answer while the state manifest's stat signature holds, because activation and rollback run offline and each changes the manifest; the write refusal reads both mode records every time. In store mode they drop freshness witnesses, mtime checks and sync-on-first-use repair, because a committed write cannot leave those rows behind. A store-mode reader queries the store's rows with SQL at one snapshot per tool call; it does not list the corpus or parse a page that it does not return. While the registry pass runs (`rebuilding`) or its state is unknown, a reader still serves its snapshot's rows and attaches one notice and the `lag` figures (`vault_store.read.registry_notice`). It never refuses and never schedules a repair for that state. The notice names search, graph and link rows, and a change to the schema or the access policy, so every reader attaches the same sentence. Rows that a pass has not reached may still hold a page that the live access policy now excludes, and that page stays invisible. The store's recall rows withhold each page that the live policy excludes, cached under the policy's fingerprint, and `find`'s admitted set, the link resolver and a request's admission read those rows. A `recall_context` lane that reads its own pages (the task-context catalogue entries, an anchor's neighbourhood) checks them against the live policy. The `find` result cache keys on the derived-row revision as well as the store revision, so an answer cached during a pass is not served after it.

The graph readers share one chokepoint, `EpistemicGraphIndex._open_read_snapshot`. In store mode it returns a lend of the request's snapshot connection: a graph read and the request's search rows see one revision, and `close` ends only what the lends opened (the progress handler and the SQL functions that a reader registered, or a pooled snapshot outside a request), while `commit` and `rollback` raise. The snapshot revision and the registry state are the whole readiness model: no publication, sync checkpoint, read barrier or source proof is read, and `cache_token` is the store revision. `GraphView`'s re-derivation for a restricted reader, the correction context (`unit_corrections`, with the store checkpoint and the request's resolver) and the pack's page loads take a stored page's bytes and time from the store. The inbound-link index becomes two derived tables, `inbound_links` and `inbound_sources`, that each commit writes for its pages by the file index's own scan rule (`vault.inbound_rows`); migration derives them from the files and verification compares them. `list_inbound_links`, the inbound check of a move or delete and the commit's `linked_from` guard all read them, so a `[[Title]]` link no longer counts as a link to guard, as in file mode. A store that an older build set up lacks those tables. Its owner-object version in `store_meta` (`vault_store_owner_schema`) is behind, so the registry state is `unknown`; the registry job creates and records the tables in its batch, derives the unowned files' rows from disk once, derives the stored pages and then records the version. Until the tables exist, a link reader refuses with `VAULT_STORE_DERIVED_BEHIND`.

The lexical catalogue also holds rows for files that the store does not own: notes outside the Knowledge Base folder, files under dot-directories and the collection layers. In store mode nothing refreshes those rows. The vector sidecar holds the same kind of rows, and the store-mode semantic drain neither refreshes nor deletes them: it follows the store's audit trail, which names only owned pages, and its census touches only owned paths. Tasks 2.2 and 3.1 must give both kinds of row an owner before store mode serves reads. An edit, creation or deletion of such a file outside Exomem refreshes its lexical, graph and identity rows through the importer: the event or sweep notes the file, and one `StoreChange.refresh` commit rederives them from the file. The sweep finds a file that has no row yet through the lexical corpus walk that file mode and a cold rebuild make, so the corpus rule stays in one place; the sweep's report carries the walk's file count and seconds. Recall policy keeps a raw Records item out of that corpus, but file mode's reference scan gives it an identity row. The sweep therefore also walks the collection layers with the reference scan's rule, and judges a file that has only an identity row by that row's hash. At the end of each sweep that judged every such file, the importer records when it started in `store_meta`; a sweep that left one unjudged keeps the previous record. `store-mode status` shows it as `unowned_rows_as_of`, and a reader puts it in its diagnostics, not in its headline. Readers query these rows as they query a stored page's rows, so the reference readers and the duplicate-id check cover both. The importer does not touch the vector sidecar's rows.

### D4. Markdown is a projection

A projector thread writes each queued file after commit as a temporary file, makes it durable (`fsync`), renames it into place, then records hash, size, mtime and revision in the manifest and clears the intent. Durability before the manifest entry matters: a torn projection must never look like an outside edit. At startup the projector removes its own temporary files and drains the queue. Several queued revisions of one file coalesce into the latest. A queued page move is one intent under the new path that names the old path, so the projector can rename the file; a later change to either path turns the move into a delete of the old path and a write of the new one. When a move's source file is missing, or the manifest's hash for it differs from the page's, the projector writes the destination from the store instead of renaming. After a commit each touched path's queue row agrees with the store: a binary record, an on-disk put and an on-disk move clear the intents that their files already satisfy, and an on-disk move needs its source to be projected, else the commit conflicts. A page path is one store key, and the store refuses a spelling that a guarded read refuses (a backslash, `..` or an empty folder); two case variants of one path are still two keys, so workstreams A and C must fold case before store mode serves a vault on NTFS or APFS. Status reports queue depth and the oldest intent's age.

In the spike, projection cost 69 ms per file at p50 (1.15 s at p95 on a contended machine), and back-to-back writes outran it. With an fsync per file the projector wrote 7.5 files a second on the real-vault copy (1,421 files in 190 s of busy time), while the production peak is 5 writes a minute, about 30 files. A Cloud cell has no editor reading its Markdown, so it can skip per-write projection and export on demand.

### D5. Outside edits import behind a base-revision check

The watcher stays, but its job shrinks to one question: does a file's hash differ from the manifest? A watcher event hashes the touched file. A periodic sweep hashes every stored file, because an edit can keep a file's size and mtime. On a difference:

- If the store revision equals the manifest revision and no intent is queued, the file imports as a governed write (D2) with outside-editor provenance.
- Otherwise the outside bytes become a held correction, and neither version is overwritten. This generalises the collection views' edit-back to pages.
- An outside delete of a page becomes a held deletion. The hold clears when the file returns with the manifest's bytes or with bytes that Exomem wrote in the path's current life.
- A binary is disk-canonical: the store holds its path, hash and size, and no bytes. A hold would keep nothing, so an outside change, delete or rename of a binary imports as an audited record change. A binary is held only when an Exomem delete of it is queued and the file differs. After the importer records an imported or renamed media binary, it reconciles the binary on its own thread, including the hash of the whole file, and the sidecar page that media processing derives commits on the store thread with its lexical and graph rows; the sweep reconciles a binary whose derivation failed. An outside delete or rename leaves the old sidecar page in place, as file mode does, because the page may hold extracted text that a person edited. The media store's own eviction and restore of an original are not outside changes: after the renames pair by hash, the importer asks `media_store.managed_event` about each binary that still differs, so an eviction leaves the binary's record in place and a restore imports nothing. A missing stored original reads as evicted and keeps its record, as in file mode, because a person's delete of it looks the same; a person's rename of it pairs by hash first and moves the record. The media store's answer holds only for a missing file: a file that is there and differs from its record imports. A copy with an evicted original's bytes that arrives during the eviction therefore pairs as a rename and takes the record, and the restored original regains its record at the first sweep after the restore.
- A rename arrives as a delete plus a new file. When the new file's hash equals the deleted file's manifest hash in the same sweep, it imports as a governed move; otherwise both halves follow the rules above. A binary's rename pairs by its hash in the same way.
- A path whose store life has ended (a page that Exomem deleted or moved away, a binary that Exomem deleted) has no bytes of Exomem's: a file restored there is a person's new file and imports.

**Resolving a held item.** An agent lists held items with the review tool and resolves each one with the repair tool, on the person's request. The list shows the person's file as it is now and the store's current revision. It gives each item a version that covers both, and says whether keep-mine can still work: it cannot once the store page changed since the hold. The list leaves out, without a count, any item whose page the caller may not read, and one order decides each item. The owner with unrestricted access sees every item, a held binary too. Anyone else is judged on the stored page's bytes, because the store's copy is the page's authority: they see the item exactly when a read of that page would give them the page in full, whatever the person's file holds. The file on disk cannot decide it while a page is stored, since a held edit differs from the page and a held delete has no file. The path alone cannot decide it either, because it gives up on every scope that reads frontmatter, such as a tag. When the store holds no page, as when Exomem deleted or moved it while the person's edit stayed, the person's file is judged the same way. When neither exists, and for a binary, which has no Markdown to read, the path alone decides. A resolve names the choice, the item's version and a one-line reason of at most 500 characters, and it refuses with a conflict when the item, the file or the store page changed since the list. A resolve passes the connector boundary's write check that a governed write passes, on the bytes it commits (the person's file for keep-mine, the store's page for keep-store, a deletion when no page results), before it saves or commits anything. A held binary is listed as not yet resolvable with no choices, and a resolve refuses it: it stays held until a later release resolves binaries. A held file above 64 MiB is listed the same way with its own sentence, and a resolve refuses it with `VAULT_STORE_HELD_TOO_LARGE`, so the list never offers a choice that a resolve refuses.
- *Keep mine* makes the person's current file the stored page, as an outside-editor write that is checked against the store revision the hold saw and queues no projection. A held delete removes the page from the store the same way. A page that the store changed since the hold stays held, so keep-mine never overwrites a newer Exomem change.
- *Keep store* first saves the person's file, byte for byte and flushed to disk, under a dated folder in the vault's state folder, outside the vault, and never over an earlier copy. It then deletes the held record and queues the store's version for projection. The manifest takes the file's facts at that commit, so the projector does not hold the same file again.
- Each resolution adds one audit row. Its reason names the choice and the caller's reason, and for keep-store also the path and sha256 of the saved copy. The held record, the manifest facts and the page change commit in one transaction. While the service runs, the commit writer and the projector are the only code that writes the store's tables; the offline migration and revert write them with the service stopped. The resolve never reads the held record's stored body, because a very large held edit keeps no body.

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

**What the ablation does not cover.** It tests one mechanism: recall adds the one-hop neighbours of its hits to the ranked results, whether or not they fit the question. It does not test an agent that reads a page's links and chooses which to follow, and it scores retrieval, not task outcomes. One published study measured outcomes for a related shape, without isolating links. In Anthropic's study of agent memory ([The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory), 2026-11-06), the agent wrote and maintained subject pages that link to its other notes. On questions where more than half of the criteria needed several documents, the agent scored 87% when it consulted the subject pages and 73% with search alone. On a second document set, the subject pages alone scored 48% against 53% for search alone, and subject pages plus search reached 61%. The gain may come from the synthesis in the subject pages rather than from their links.

The graph has to earn its place through denser, typed links measured on outcomes, not through its current recall lane. Unit-level edges (D10) and the traversal capabilities above are where that density can come from. Before unit results ship, task 5.4 compares search alone with search plus agent-followed links, on tasks that span several vault pages, scored by outcome. The outcome evaluation (see Epistemics evaluation) carries the graph on/off comparison on agent outcomes for the switch.

### D7. Embeddings stay asynchronous

Vector rows stay derived and are computed after commit by the semantic drain, keyed by page hash and store revision. A reader that needs vectors for a page newer than its vectors falls back to lexical and graph lanes, as it does today when vectors lag.

### D8. Migration and rollback

A vault is in store mode when two records agree: the vault state manifest records the compatibility id `vault-store-v1`, and the store's `store_mode` row says `store`. Activation records the id before its commit, and rollback drops it after its own commit, so a crash between the two leaves file mode and both commands rerun cleanly. Every build supports the id, so a build that keeps the release capability off can still roll a vault back. The release capability of the same name stays off until the switch, and the managed upgrade migrator never switches a vault.

The manifest is machine-local, because each state root holds its own. A second service on another state root over the same vault, such as Windows and WSL sharing one coordinator, would read file mode. So activation records its claim, which sets the `store_meta` key `vault_store_mode`, and publishes it before the transaction that writes the `store_mode` row, and rollback deletes the key in the transaction that drops that row. The lease hand-back copies `store_meta` into the replica under the vault, which every sync carries, and a root forces that copy when its own decision is newer than the replica's, because store mode moves no collection head and a publication by head alone would skip it. **Decision register.** Each decision to enter or leave store mode takes `n`, one above the largest `n` that the deciding root sees under the lease in its own store and in the replica, and is stored with its id and mode as `vault_store_decision`; the larger `n` is the newer decision, a tie goes to the replica's copy, and `vault_store_mode` follows the newest decision, so a build that reads only that key still refuses. A takeover decides nothing: it merges the replica's decision into the store it starts, so the newest decision wins whichever root made it, it carries a replica decision that it cannot read as it stands, so that no publication erases it, and it keeps the root-local `vault_store_claim`, the id of this root's own claim: the replica's copy of the table holds the publisher's claim, and the takeover resets it. **Claim-first migrate.** `store-mode migrate` checks at its preflight and again inside the lease after it merges the replica, records its claim, publishes the replica and reads it back, and installs rows only when the replica carries the claim; a claim that no replica carried is withdrawn without a decision, so a failed publication leaves the root in file mode and the second of two roots to migrate refuses. **Release.** `exomem store-mode release --apply --claim ID --why TEXT` runs offline on a file-mode root while no service holds the lease and decides `file` over the named claim, so a refused root whose holder is gone has a remedy that does not need that root; a holder that returns merges the decision, reads displaced and cannot reassert its claim. A store key with no decision, which a build older than the register wrote, is the claim `legacy`. A displaced holder's rollback keeps its present behaviour (settled by the owner): it writes the store's versions of the files, and edits made on disk survive only in the rollback export (`--export-held`). A file-mode service reads the keys from the replica when it starts and refuses with `VAULT_STORE_ELSEWHERE`, which names the rollback and the release command with the claim id; `exomem store-mode status` shows the standing decision and whether this root holds, is displaced from or is refused by it, and the doctor shows the same. Only a readable replica that says store refuses, at whatever store schema it was written: a replica that is absent or unreadable starts the vault, because a synced vault is eventually consistent and a half-arrived copy is no sign of store mode, and a vault that never migrated has no key. A state root whose own store carries a newer decision than the replica's, or whose claim the replica carries, also starts, so a rollback whose hand-back failed never locks out the one root that can publish the replica that clears the key. A sync that restores an older replica saying store lets the root that already merged the rollback start, and that root's next publication meets the publisher's divergence guard: it republishes only through `exomem collections adopt-local`, as for any divergence. A decision that cannot be read, or that disagrees with `vault_store_mode`, is an order unknown: it refuses on a store key and forces no copy, and a takeover carries an unknown replica decision as it stands, so no publication drops it. The check runs at start and not while a service runs, because a service that holds the vault cannot see a later migration on another root; the one-service rule stays the operator's rule for a running pair. A build from before this check ignores the key, so every host must run a build with it before the switch.

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

For writes that Exomem commits, store mode does not need the modules that keep sidecars in step with files: `graph_sync` (4,153 lines), `graph_drain` (844), `derived_drain` (1,066), `derived_receipts` (3,408), `deferred_index` (2,733), `freshness` (2,468), `index_sync` (2,439), `pending_recall` (934), `graph_publication` (155), and the repair, witness and catch-up paths inside `lexstore` (12,289 lines in total) and `epistemic_graph` (14,880 in total). The watcher (2,564 lines) shrinks to the manifest importer. File mode keeps them until it retires; deleting them is the last phase. After the graph readers moved to the store (task 3.1), the sidecar read path (`_open_read_snapshot`'s file branch, `_open_published_read`, `graph_read_publication`, `graph_lag`) and the whole in-memory inbound-link index (its build, patch, catch-up and recovery functions) serve file mode only.

### D10. Semantic units are first-class rows

Stage 2. Pages stay the documents people read and edit, and they are composed of semantic units. Today each unit is stored twice, as a lexical `semantic_units` row and as a graph unit node, in two files that can disagree. In store mode each unit is one row with:
- its identity (`unit_ref`), under today's rule: the authored block anchor when the page carries one, otherwise the fingerprint-bound reference. The store derives it from the bytes and never writes anchors into a page;
- its page, ordinal and exact byte span in the page;
- category, kind and text;
- provenance (its sources);
- its verdict from the existing verdict set, its lifecycle and its canonicity (D11).

Typed edges run between units as well as between pages: a link to `[[Page#^anchor]]` resolves to the unit that carries that anchor. A claim-level operation, such as verifying, refuting or superseding one claim, is a governed write that changes the page bytes and the unit row in one transaction. Recall can therefore return units with their page context, and graph edges can point at the exact unit a claim lives in. That is new retrieval behaviour: it ships only after task 5.4 and the outcome evaluation's stage-2 run (task 7.3). The merged unit rows must still reproduce today's lexical unit rows and graph unit nodes for the same bytes.

*Option kept:* page bytes are the serialisation and unit rows derive from them in the same transaction. One source of truth holds because the two cannot diverge, and the Markdown projection stays complete for editors. *Option rejected for now:* unit-canonical storage, where a page is composed from stored segments. It offers no capability beyond the rows above and makes every outside edit a segmentation problem.

### D11. Originals record their canonicity

Stage 2. Every stored file, a text page or binary media, carries a canonicity record:
- **origin:** how it entered (upload, import source, capture tool, client);
- **content hash, size and format;**
- **derivation:** original, or derived from a named hash by a named transform, such as compression by a client, transcoding or text extraction.

When the true original of content already filed as an original arrives, a governed write supersedes the earlier record. The earlier file stays and becomes derived from the true original. Citations resolve to the original, and the audit trail records the change. This is the failure seen when compressed copies from a chat client were filed as originals and the true originals arrived later from a file store: store mode makes the correction one transaction instead of a hand repair across files. Task 5.3 maps the record onto the existing Evidence custody fields.

### D12. Root layout and readable names

D12 is a separate package, task group 9. It builds on the stage 1 write path and projector, and it never gates the switch (task 7.4).

**Why.** Every governed page lives under one wrapper folder, with up to seven folders below it. Filenames are kebab slugs of the title. Governed writes rewrite every resolvable link to a vault-rooted path (`normalize_body_wikilinks`), so Obsidian shows link text such as `Knowledge Base/Notes/Insights/some-slug`. On a 13.9k-page copy of a real vault, 19,743 of 20,437 links are path links, and 1,997 of those include the wrapper. The goal is a flat structure and filenames that equal titles. Store mode makes this affordable: a relayout is one governed move in one transaction, and the projector writes the files.

**Layout shapes.** A vault has one of two layouts:
- `nested`: the managed root is one folder inside a larger vault. This is today's layout. It stays for vaults that also hold content Exomem does not manage, and for every existing vault until its relayout.
- `root`: the managed root is the vault root. New vaults and new Cloud cells start here.

**Managed root.** The managed root is the folder that holds `_Schema/`, the marker that `_is_vault` already checks. Exomem resolves it once, when it resolves the vault. `EXOMEM_KB_DIRNAME` stays as a test override. Code reaches the managed root only through two helpers: one joins a managed-relative path, the other splits a vault path into its managed-relative rest. About 130 sites that build or compare the wrapper as a string move to these helpers. Path joins through `kb_root` need no change. Where D1 to D11 say "the Knowledge Base", they mean the managed root. In `root`, a path that names `Knowledge Base/Notes/<Area>/<rest>` for a compiled area means `<Area>/<rest>`, because the `Notes/` level drops. When the store records a layout, that layout decides the managed root. A leftover second `_Schema/` is reported by status and doctor and never refused, because both folders exist normally during the relayout's projection window and a refusal would leave a crashed relayout unresolvable. When no layout is recorded and both the vault root and a wrapper folder hold `_Schema/`, resolution refuses with an error that names both folders.

**Areas.** In `root`, the vault root holds the areas:
- one folder per compiled note type and per home, with no `Notes/` level and no wrapper folder, so a compiled note sits at `Insights/Some Title.md`;
- `Entities`, `Sources`, `Evidence`, `Records` and `Planning`;
- `index.md` and `log.md`.

The machinery folders (`_Schema`, `_trash`, `_archive`, `_attachments`, `_compiled`) keep their names. They are a closed product set that the reserved-path registry already holds. Registries name every area; code never spells one:
- A compiled type's `folder` is its area name. The `Notes/` parent exists only in `nested`. A `folder` value that starts with `Notes/` reads as the `nested` parent plus the area name, so an existing overlay stays valid. The relayout rewrites such overlay values to the bare area name in its one store change.
- The note types with role `source`, `evidence` and `entity` get a `folder` attribute. The attribute changes no entry's meaning.
- `Records` and `Planning` come from the collection profiles.

**Flat inside.** In `root`, the typed writer applies no partition (project, domain or medium) and no second taxonomy level. A group folder stays only where a contract needs it, and the group follows a closed role value or a collection profile, not an area name:
- Role `source` groups by kind (`Sources/<Kind>/`). Recap detection is a path test that must not read the page.
- Role `entity` groups by entity type (`Entities/<Type>/`). The entity-type registry declares this folder.
- A collection groups by its collection (`Records/<collection>/`, `Planning/<collection>/`). The collection contract fixes these folders.
- Role `evidence` groups by scope and then category (`Evidence/<scope>/<category>/`). Evidence keeps original filenames, and 70 to 82 of 3,114 names collide when flattened.
- Every other role, including `compiled`, has no group folder.

A page path then holds an area, at most one group folder and the name: `Area/Group/Name.md`. Evidence is the one exception, because it keeps both its scope and its category folder. A source's domain stays in its metadata, so a reclassification that changes only the domain moves no file in a `root` vault.

*Alternative rejected:* one folder for every note, with the type held in frontmatter. Three boundaries are path-based today: the compiled-type write gate, the append-only trees and the collection folders. Each would need a new rule.

**Readable names.** `root` makes `title` the default filename style. The setting from `add-readable-note-filenames` can still override it, and `nested` keeps `slug` as its default. The name rule is `sanitize_title_filename` with three corrections, which apply in both layouts:
- Remove `#`, `^`, `[` and `]`, because the wikilink grammar gives them meaning. 287 titles in the copy contain `#`. Define this set once, next to the wikilink parser, and import it into the sanitiser.
- Strip leading dots. A dot-named file is outside the store (ownership rule 2), and Obsidian hides it.
- Cap the whole name at 200 UTF-8 bytes as well as at the character cap. Cut only at a code-point boundary, because ext4 and APFS allow at most 255 bytes per name.

Types with `stem: dated` or `stem: started` keep their date prefix, which comes from the page's recorded date and never from the date of a write. That date is the `started` value for a started stem. For a dated stem it is the frontmatter `created` value, falling back to the page's first store audit row. Evidence keeps original filenames. Records and Planning keep their filename recipes.

**Uniqueness.** A new name derived under `title` style in a `root` vault is unique among the Markdown pages that Exomem owns, across the whole vault, compared under `vault_name_key` (NFC, then casefold). Uniqueness is vault-wide rather than per folder, so `[[Name]]` names one page in Exomem's stem lookup and in Obsidian's basename lookup. On a collision the writer appends ` (2)`, ` (3)` and so on, taking the lowest number that no page holds. The suffix counts toward the 200-byte cap, so the writer shortens the title part first. In store mode the check reads the store, never the projection. A slug name, and a `title` name in a `nested` vault, keep the `-2` collision contract of `add-readable-note-filenames`.

A name is pinned when a caller explicitly chooses it for a page of a title-named type, at create, move or rename time. In store mode the store records the pin with the page, in either layout. An editor rename that an outside-edit import (task 4.2) reads as a governed move counts as a chosen name. A move that keeps the basename, such as a reclassification or a Source-to-Evidence promotion, pins nothing.

The note type sets the class of every other name. A name is derived when the page's note type names its files from the page's frontmatter `title`: a title stem, with or without a date prefix. `title` means that frontmatter value only. An H1 or a filename stem never stands in for it, and a page with no frontmatter title keeps its name. Every other name is fixed: an Evidence original, a Records or Planning recipe name, a generated navigation page (`index.md`, `log.md`, `hub.md`), and any page whose type has no title rule or that sits outside every registry area. The class follows the page's type at write time, so a page whose type is later registered with a title rule takes its title form on its next governed write in a `root` vault.

In a `root` vault a pinned name never takes a suffix. A pin that collided would break vault-wide uniqueness, so a write that chooses a name another page holds is refused, and the refusal names that page. The refusal skips a move that keeps its basename, because Evidence names may collide by design. It also skips an outside-edit import, because the file already exists. A fixed name is never renamed after creation. A new write whose fixed name collides inside one folder keeps today's disambiguation (`unique_path`, and `add.py` for Evidence). A `nested` vault changes nothing: it keeps today's suffixing of an explicit name. A derived name yields to pinned and fixed names whatever the path order. The relayout resolves collisions among derived names in ascending order of the pages' current paths, so a second plan run gives the same names. In the copy, 1,505 pages need a suffix within their current folder and 1,651 need one across the whole vault.

**Links.** Writers emit the shortest trailing path of the target, cut at a folder boundary, that resolves to exactly one file for both Exomem and Obsidian. That path is `[[Name]]` when no other file Obsidian sees has that name. A grouped page needs a real path suffix. When `Scan` is not unique, a page at `Evidence/alpha/receipts/Scan.md` gets `[[receipts/Scan]]` if that is unique, then `[[alpha/receipts/Scan]]`. It never gets `[[Evidence/Scan]]`, which is a suffix of no page. Exomem's resolver matches full paths, managed-root-relative paths, stems and titles today, so task 9.5 adds trailing-path matching. Graph rows keep the resolved vault path, so derived rows keep their meaning. A link repoint changes only the link's target text, and the link still resolves to the same page. In store mode, append-only pages in `Sources/` and `Evidence/` accept link repoints and nothing else, each recorded with its own audit reason. Every other byte of those pages stays unchanged. File mode keeps today's refusal.

**Retitle.** In a `root` vault in store mode whose filename style is `title`, a governed write to a page renames a derived name that does not match the page's frontmatter title after the write. A name matches when it equals the naming rule's output for that title under the `title` or the `slug` style. A name derived under another style therefore counts as matching, and two writers with different settings never flip a name. The date prefix of a dated or started stem comes from the page's recorded date and never from the write date. Otherwise every write to a dated page in a later month would rename it and repoint its links. The page's own current name counts as free in the collision check, so a matching name stays put. One transaction moves the rows, repoints the inbound links that use the old name, re-derives those pages and records the old path as an alias. A pinned or fixed name never changes. An outside-edit import never renames a file, even when it changes the title. The next governed write to that page renames a derived name to match its current title, so a filename does not drift from its title. A derived name that carries a suffix returns to the plain name on such a write once no other page holds it. A `nested` vault, in either mode, renames no existing file on a write, exactly as today. A style change renames nothing in either layout. The relayout is the one point where an existing derived name takes the form of the vault's resolved style. File mode adds no rename on retitle, so a file-mode vault keeps its names after a retitle.

**Identity.** `store_pages.path` stays the key. Every derived table is keyed by path and reused unchanged (D1, D2). A page-id key would still rekey the derived rows on every move, and it would break the whole-table equivalence proof. The move-safe identity already exists:
- `exomem_id`, present on 91% of Markdown files in the copy, with no duplicates;
- the store's identities table, which every move updates.

A path alias table maps each old path to the current path. Every move updates it in the same transaction. The resolver reads it after exact paths and before stems.

**Relayout.** `exomem store-mode relayout` moves a store-mode vault from `nested` to `root`:
1. `--plan` computes every move, name, suffix, truncation and link repoint from the registries and the current pages. It reports counts and writes nothing.
2. `--apply` runs with the service stopped. It commits the plan as one store change: page moves, repointed pages, aliases, the layout record and the rewrite of overlay `folder` values that start with `Notes/`.
3. The projector writes the files.
4. `--verify` runs the proof below.

Preconditions: store mode is active, the service is stopped, no items are held, the projection queue is empty, a store backup exists, and free disk space is at least the size of the store. Each failed precondition refuses `--apply` and names itself. The managed upgrade migrator never runs a relayout.

Placement rules for pages that D12 does not place by area or group:
- A page in an exempt or unregistered folder keeps its path relative to the managed root, so only the wrapper segment drops. A `Notes/<X>` folder whose `<X>` names no registered area is unregistered. It keeps its `Notes/` level, which is intended, and `--plan` lists each such folder.
- A partition folder below the group level flattens into the group folder, or into the area when the area has no group folder.
- A pinned name and a fixed name keep their names. Every derived name takes the form of the vault's resolved style, which is `title` by default in `root`, with the date prefix from the page's recorded date. No vault holds a pin before this change ships, and a `nested` vault renames nothing on a write, so the first relayout renames every page whose name differs from that form, and `--plan` reports the count. Two pinned names that would take one path leave a page the plan cannot map, and `--plan` reports them.
- Markdown at the vault root outside the old wrapper keeps its path, name included, and becomes owned when the managed root becomes the vault root. `--plan` lists each such file.
- `--apply` refuses and names any page that the plan cannot map to exactly one new path.

**Cost of a move.** In file mode, a move renames one file, rewrites each linking page with its own guard and fan-out, and refuses when an append-only page links the target. A file-mode relayout of 14k pages is not practical. In store mode the relayout is one transaction that re-derives about 8k pages. That is close to a cold build: 82 s for the lexical index plus 50 s for the graph on the spike's copy. Projection follows. A single retitle re-derives the moved page and its linking pages in one transaction.

**Guarantees to the tool surface.**
- Each owned page keeps its `exomem://memory/<uuid>` reference across moves, retitles and the relayout.
- No path in a `root` vault contains the wrapper.
- A derived name identifies one page in a `root` vault. Where names collide (fixed names, outside-edit imports), the shortest trailing path identifies the page. A title does not identify a page: 1,998 pages in the copy share a title with another page, so a tool that shows titles must disambiguate them.
- An old path still resolves through the alias table.

**Rollback.** Rollback keeps the `root` layout. File mode can read it, because both modes share the layout code. D8's rollback step 3 also exports the alias table with the audit trail. Its existing precondition of an empty projection queue means that no leftover wrapper `_Schema/` can exist when it runs, so it exports no layout record. To get the old layout back, restore the store backup before any new write. No reverse relayout is built.

**Scope.** D12 says how a filename collision resolves and nothing more. It does not remove duplicate content, and it does not retitle recap pages. Separate dedup work owns both. Pages that share a title keep their titles and get distinct filenames.

**Schedule.** Group 9 starts after the weeks 2 and 3 workstreams merge. It never gates the switch (task 7.4). The relayout may share the switch's maintenance window when group 9 is ready. Otherwise it runs later, on a vault that is already in store mode.

**Order of work for the owner's vault.** The vault switches to store mode (7.4) with or without group 9. Once group 9 is ready:
1. Back up the store.
2. Run `exomem store-mode relayout --plan` and review the counts.
3. Stop the service, then run `exomem store-mode relayout --apply`.
4. Run `exomem store-mode relayout --verify`.
5. If `.obsidian` sits in the old managed folder, move it to the vault root.
6. Open the vault root in Obsidian, then start the service.

Expected result: Obsidian shows the areas at the root, filenames match titles, and every link resolves. Existing vaults stay `nested` until this run. New vaults use `init` in `root`. Cloud cells relayout after their own store migration (6.2), and new cells start in `root`.

Proof for `--verify`:
- Every owned page maps to exactly one new path, and the set of `exomem_id`s is unchanged.
- Every link occurrence resolves to the same page before and after, and every rewritten link passes Obsidian's basename-or-path rule.
- Page bytes equal the old bytes with the planned link edits applied, and binaries keep their hashes.
- A fresh derivation equals the stored rows. Graph edges are equal once paths are mapped to ids.

The rehearsal (task 9.13) adds two checks that `--verify` does not run. It compares the recall canary case by case with 95% intervals, and exact equality is not the claim, because link text and filename-derived titles change search tokens. It also runs the rollback drill on the relaid-out vault.

**Risks.**
- **Test corpus.** 10,465 wrapper literals in 895 test files stay on `nested`, so `root` needs its own test lane at the boundaries: ownership, resolver, recall scopes, write gate, governance, migrate and relayout.
- **Process-wide managed root.** This assumes one vault per process, which matches the service and the cells.
- **Collection store.** The `Records` and `Planning` views belong to the collection store, which must re-root its projected views during the relayout. Coordinate with that store's owner.
- **Projection volume.** The queue's move intent (operation `move` with the old path, store schema 16) lets the projector rename a page file instead of deleting and rewriting it. The relayout then writes about 8k rewritten pages, not about 36k file operations at the spike's 7.5 files per second. The rename needs task 4.1's optional addition. Without it, a move projects as a delete plus a write, which takes roughly 80 minutes.
- **Later homonyms.** A later fixed name or outside-edit import can add a second file with a name that an existing bare `[[Name]]` already uses, and the link becomes ambiguous. This is accepted debt, and the change adds no repoint rule for it.
- **Obsidian homonyms.** A bare `[[Name]]` may resolve in Obsidian to a homonym in `_trash` or `_archive`. Writers count every Obsidian-visible file when they choose the short form.
- **Old builds.** An older build pointed at a `root` vault could create a fresh wrapper folder. The vault-side marker (D8) refuses a build that carries the check; a build from before it ignores the marker, so each host must upgrade before the switch.

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

**Result.** These weeks assume the Records move's pace, which landed mostly one PR at a time. About 11 to 13 weeks in total. The smallest local release, the first six stage-1 rows, is 7 to 8 weeks. Embeddings, Cloud, Windows and deletion bring stage 1 to 9.5 to 10.5 weeks; stage 2 adds 1.5 to 2.5. No cut of this table fits one month at that pace.

**Parallel schedule.** With parallel workstreams, the smallest local release, plus embeddings keyed by store revision and an acceptance run on Windows, takes about four to five weeks of elapsed time:

1. Week 1, two workstreams: the store schema, migration with both verifications, rollback exports and the ownership rule; and, beside it, the availability, replica and contract-amendment work. This fixes the table contract and the governed write entry point that the later workstreams share.
2. Weeks 2 and 3, three workstreams on separate files: the write path; every reader, built against a migrated store so it does not wait for the write path; the projector and importer, then embeddings keyed by store revision.
3. Week 4: integration and acceptance on a real-vault copy and on a Windows runner.

Four weeks holds only at the lower ends of the package estimates. At the upper ends all three workstreams of weeks 2 and 3 fill their two weeks with no slack, and integration has no package of its own, so plan for five. This schedule defers stage 2, file-mode deletion and Cloud cells until after the switch, and file mode remains the rollback path. Any slip lands in the last week. The switch also waits on the outcome evaluation's store-mode run (task 7.3), which runs in its own session outside these weeks. Store mode can stay behind a per-vault setting while a release ships in file mode, which separates the switch from the release.

Task group 9 (D12) is outside these totals and has no estimate yet. It starts after weeks 2 and 3 merge and gates nothing in this schedule.

## Epistemics evaluation

The design's epistemics check is a separate outcome evaluation, run in its own session on case sets it owns. It plants stale or wrong notes, each with a governed correction (supersession, or a refuted or qualified verdict on a claim), and originals filed wrongly as derived or the reverse, in a disposable vault copy. It then measures case by case whether an agent using recall flags or corrects them, and runs the same tasks with the graph lane on and off. Each comparison carries a bootstrap 95% interval, blind judging and answer keys written by a separate agent. Store mode must keep every signal that evaluation finds recall relies on; the D1 schema stores pages verbatim, so it drops none. Its case sets run against any build that serves MCP, so the store-mode build is measured on the same cases before the switch.

## Risks / Trade-offs

- **One database is one failure point.** A corrupt store affects the whole vault. Mitigation: WAL with `synchronous=FULL`, the existing collection-store backups, and the projected Markdown as a complete second copy that rollback rebuilds from after the exports D8 names.
- **Outside-edit conflicts become visible work.** Concurrent edits become held corrections instead of a silent last-writer-wins. That is the intent, but it is a new review surface for Obsidian users.
- **Projection lag.** Under bursts, Markdown trails the store by seconds. Readers never use Markdown, but a person in Obsidian may briefly see the older text. Status shows the lag.
- **Two modes during migration.** Both modes run until the switch, which doubles the test matrix for that period. Keep it short.
- **Derivation seams.** Production code must pass bytes into four derivation entry points that read from disk today. If one resists, the write path keeps a narrow adapter, as the spike does, and the estimate's upper bound applies.
- **Root layout.** The risks of the layout change (test corpus, collection views, Obsidian homonyms, older builds) are listed in D12.

## Open Questions

- Whether append-only pages such as `log.md` should become rows that the projection renders, so a write appends one row instead of re-indexing a 1.3 MB page. Measure the transaction with and without that file first.
- Switch timing: before or after the public release. The owner decides from these numbers.
- Whether a Cloud cell projects Markdown at all, or exports only on request.
- Whether recall keeps the one-hop graph lane before task 5.4 or the outcome evaluation shows a gain. In the live logs of 2026-09-09 to 2026-11-07, 4.6% of returned hits came from that lane alone, and agents read them within 30 s at about a sixth of the rate of other hits at the same ranks.
