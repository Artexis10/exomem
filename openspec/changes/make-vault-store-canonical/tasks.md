## 1. Store schema, migration and rollback

- [ ] 1.1 Add `store_pages`, the projection manifest and queue, held corrections, unit rows, canonicity records and the audit trail to the per-vault collection store database, beside the lexical catalogue and graph tables.
- [ ] 1.2 Extend `collection_store/migration.py` with an offline page migration that loads exact bytes and current derived rows.
- [ ] 1.3 Verify before activation: a fresh derivation equals the migrated rows on whole tables, and a full projection reproduces every file byte for byte.
- [ ] 1.4 Add a rollback drill: stop store mode, rebuild file-mode sidecars from the projection, and compare search and graph answers.

## 2. Write path

- [ ] 2.1 Give the four derivation entry points (page parse, parent state, recall-candidate gate, lexical row builder) a bytes input, replacing the spike's overrides.
- [ ] 2.2 Commit every file of a governed write with its lexical rows, graph rows, audit rows and projection intents in one `BEGIN IMMEDIATE`, through the collection store's existing transaction owner.
- [ ] 2.3 Re-resolve dependent pages whose links start resolving in the same transaction.
- [ ] 2.4 Prove with hard-kill trials that no kill leaves repair state, and with a whole-table diff that rows equal file mode for the same bytes.

## 3. Readers

- [ ] 3.1 Point lexical, graph, reference and semantic-unit readers at the store tables in store mode.
- [ ] 3.2 Remove freshness witnesses, mtime checks and sync-on-first-use repair from the store-mode read path.

## 4. Projection and outside edits

- [ ] 4.1 Add the projector: durable write, then manifest; startup sweep of temporary files; coalescing; queue depth and oldest-intent age in status.
- [ ] 4.2 Turn the watcher into the manifest importer: size and mtime first, hash on difference.
- [ ] 4.3 Import clean outside edits behind the base-revision check; hold concurrent edits and outside deletes as corrections for review.

## 5. Semantic units and canonicity

- [ ] 5.1 Merge the lexical unit rows and graph unit nodes into one unit row set with stable identities carried as anchors in the page bytes.
- [ ] 5.2 Resolve links to `[[Page#^anchor]]` as unit-level edges; make claim-level writes change the page and the unit in one transaction.
- [ ] 5.3 Record origin, hash, size, format and derivation for every stored file; add the governed write that supersedes a false original and keeps the copy as derived.

## 6. Embeddings, Cloud and Windows

- [ ] 6.1 Key vector rows by page hash and store revision; keep the semantic drain asynchronous.
- [ ] 6.2 Let a Cloud cell run without per-write projection and export Markdown on request.
- [ ] 6.3 Run the acceptance set on native Windows/NTFS.

## 7. Acceptance and switch

- [ ] 7.1 Rehearse on a disposable copy of a real vault: the replayed write stream, hard kills, the recall canary cases and the graph-lane cases.
- [ ] 7.2 Run the installed-wheel product journey in store mode.
- [ ] 7.3 Owner decision on switch timing; then migrate the owner's vault, then Cloud cells.

## 8. Retire file-mode sync machinery

- [ ] 8.1 After the switch, delete the modules and paths listed in design D9 that only file mode uses, and the requirements that only describe them.
