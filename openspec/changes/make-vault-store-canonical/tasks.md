## 1. Store schema, migration and rollback

- [x] 1.1 Add `store_pages`, binary records, the projection manifest and queue, held corrections and the audit trail to the per-vault collection store database, beside the lexical catalogue and graph tables. Unit rows move to 5.1 and canonicity records to 5.3.
- [x] 1.2 Define one ownership rule for the files the store owns (D1), shared by migration, the reconcile sweep and the projector, so none of them treats another's files as created or deleted.
- [x] 1.3 Extend `collection_store/migration.py` with an offline page migration that loads exact bytes and current derived rows.
- [x] 1.4 Verify before activation: a fresh derivation equals the migrated rows on whole tables, and a full projection reproduces every file byte for byte.
- [x] 1.5 Add rollback with its preconditions (drain projection, refuse on unexported held items, export audit and canonicity records) and a drill that rebuilds file mode from the projection and compares search and graph answers.
- [x] 1.6 Scope the store: keep page, lexical and graph tables out of the collection store's in-vault replica; make store-mode knowledge writes refuse with the store's unavailability reason; build the replica from a staging database filled in one read transaction; refuse to start or activate store mode while multi-host writer-lease coordination is configured; amend to file mode the knowledge-stays-out, knowledge-writes-continue and whole-file replica clauses in `move-structured-collections-to-sqlite` and `add-collection-query-engine`, the Markdown-authority and replay requirements in `add-graph-traversal-queries`, and the replication assumption in `multi-host-writer-lease`.

## 2. Write path

- [ ] 2.1 Give the four derivation entry points (page parse, parent state, recall-candidate gate, lexical row builder) a bytes input, replacing the spike's overrides.
- [ ] 2.2 Commit every file of a governed write with its lexical rows, graph rows, audit rows and projection intents in one `BEGIN IMMEDIATE`, through the collection store's existing writer lease and transaction owner; make preserved binary files durable before that commit. Give `unique_path` and `respell_new_path` a store form that lists folder contents from the store, not the disk; store mode needs this for any title-style name, with or without D12.
- [ ] 2.3 Re-resolve dependent pages whose links start resolving in the same transaction.
- [ ] 2.4 Prove with hard-kill trials that no kill leaves repair state, and with a whole-table diff that rows equal file mode for the same bytes.

## 3. Readers

- [ ] 3.1 Point every reader of page bytes or derived rows at the store in store mode: lexical, graph, reference and semantic-unit readers, page reads, `find`'s parse cache (keyed by store revision), governance frontmatter reads and the registry overlay loaders.
- [ ] 3.2 Remove freshness witnesses, mtime checks and sync-on-first-use repair from the store-mode read path.

## 4. Projection and outside edits

- [ ] 4.1 Add the projector: durable write, then manifest; startup sweep of temporary files; coalescing; queue depth and oldest-intent age in status. Before replacing a file, compare it with the manifest and hold on a difference; never project a held path. Optional, for D12: project a store move as a rename, through the queue's move intent, when the file on disk matches the manifest.
- [ ] 4.2 Turn the watcher into the manifest importer: hash the touched file on each event and every stored file on a periodic sweep; import a rename whose bytes match as a governed move.
- [ ] 4.3 Import clean outside edits behind the base-revision check; hold concurrent edits and outside deletes as corrections for review. Record each held item once per path.

## 5. Stage 2: semantic units and canonicity

- [ ] 5.1 Add the unit row table (moved from 1.1). Merge the lexical unit rows and graph unit nodes into one unit row set under today's identity rule (authored anchor, else fingerprint-bound reference), and prove it reproduces both for the same bytes.
- [ ] 5.2 Resolve links to `[[Page#^anchor]]` as unit-level edges; make claim-level writes change the page and the unit in one transaction.
- [ ] 5.3 Add the canonicity table (moved from 1.1). Record origin, hash, size, format and derivation for every stored file, mapped onto the existing Evidence custody fields; add the governed write that supersedes a false original and keeps the copy as derived.
- [ ] 5.4 Before unit results ship, compare search alone with search plus agent-followed unit-level links on tasks that span several pages, scored by outcome with the outcome evaluation's method, case by case with 95% intervals; rerun the graph-lane ablation with unit-level edges beside it.

## 6. Embeddings, Cloud and Windows

- [ ] 6.1 Key vector rows by page hash and store revision; keep the semantic drain asynchronous.
- [ ] 6.2 Let a Cloud cell run without per-write projection and export Markdown on request.
- [ ] 6.3 Run the acceptance set on native Windows/NTFS.

## 7. Acceptance and switch

- [ ] 7.1 Rehearse on a disposable copy of a real vault: the replayed write stream, hard kills, the recall canary cases and the graph-lane cases.
- [ ] 7.2 Run the installed-wheel product journey in store mode.
- [ ] 7.3 Run the outcome evaluation's planted-error, canonicity and graph on/off cases against the store-mode build before the switch, and again before stage 2 ships, comparing case by case with the file-mode build.
- [ ] 7.4 Owner decision on switch timing; then migrate the owner's vault, then Cloud cells.

## 8. Retire file-mode sync machinery

- [ ] 8.1 After the switch, delete the modules and paths listed in design D9 that only file mode uses, and the requirements that only describe them.
- [ ] 8.2 Before archiving, refresh each MODIFIED requirement against later changes to the same requirement, such as `fix-startup-retrieval-liveness` on Lexical Index Synchronization.

## 9. Root layout and readable names (D12)

Group 9 starts after the weeks 2 and 3 workstreams merge. It never gates 7.4. The relayout may share the switch's maintenance window when group 9 is done.

- [ ] 9.1 Resolve the managed root once from the `_Schema/` marker, letting a recorded layout decide when two `_Schema/` folders exist and reporting the leftover in status and doctor. Route the ~130 wrapper-shaped sites through two helpers. In `root`, keep `_Imports/<id>` stages outside the store.
- [ ] 9.2 Name the areas in registries: an area `folder` on compiled, source, evidence and entity types. Derive `init`'s folders from them. Remove `init._FOLDERS` and the area-root literals.
- [ ] 9.3 In `root`, place pages flat inside areas, keeping only the group folders D12 names. Make the compiled-type write gate read the area from the registry.
- [ ] 9.4 Give `root` vaults title names by default: a date prefix for dated and started stems taken from the page's recorded date, vault-wide case-insensitive uniqueness for title names read from the store, and deterministic ` (2)`, ` (3)` suffixes. In a `root` vault, never suffix a pinned name, and never rename a fixed name after creation; a fixed name that collides inside one folder keeps today's disambiguation.
- [ ] 9.5 Make every writer emit the shortest trailing path, cut at a folder boundary, that resolves to exactly one file for both Exomem and Obsidian (`[[Name]]` when the name is unique), including the index and log generators. Add trailing-path matching to `WikilinkResolver`. Keep graph rows on resolved paths.
- [ ] 9.6 Add the path alias table, its resolver step and its rollback export. Update it on every move. Record as accepted debt that a later fixed name or outside-edit import can make an existing bare `[[Name]]` ambiguous, and add no repoint rule for it.
- [ ] 9.7 Record the pin. In store mode, in either layout, record a pin with the page when a caller explicitly chooses a name for a page of a title-named type at create, move or rename time, as a store column or an audit entry. Count an editor rename that an outside-edit import (4.2) reads as a governed move as a chosen name. Let a move that keeps the basename, and an outside-edit import, neither pin nor meet the refusal. In a `root` vault, refuse a chosen name that another page holds, and name that page. A `nested` vault keeps its suffixing of an explicit name.
- [ ] 9.8 Add the rename rule. Take the class from the note type: a name is derived when the type names its files from the frontmatter `title`, and every other name is fixed (Evidence originals, Records and Planning recipes, `index.md`, `log.md`, `hub.md`, types with no title rule, pages outside every registry area). Read no H1 or filename stem as a title. In a `root` vault in store mode whose style is `title`, rename a derived name that does not match its page's frontmatter title, in the same write as any governed write, with its inbound link repoints. A name matches when it equals the naming rule's output under the `title` or the `slug` style. Take the date prefix from the page's recorded date, never the write date: `started`, or the frontmatter `created` value falling back to the first store audit row. Never rename a pinned or fixed name, and never rename on an outside-edit import, so the next governed write renames. In a `nested` vault, in either mode, and on any style change, no governed write renames an existing file.
- [ ] 9.9 Accept link-only repoints in append-only pages in store mode, each with its own audit reason. Reconcile it with two active changes so that each reads as file mode only or names this repoint as the one allowed byte change: `make-source-reclassification-reversible`, whose reclassification refuses on append-only path links and whose domain-only reclassification no longer relocates a page in a `root` vault, because the path holds no domain; and `add-governed-vault-consolidation`, whose import says append-only originals SHALL NOT be overwritten or body-rewritten.
- [ ] 9.10 Add `store-mode relayout` with plan, apply and verify, and enforce its preconditions. Place pages by D12's rules: keep pinned and fixed names, give every derived name the form of the vault's resolved style with the recorded date prefix, and keep unregistered `Notes/<X>` folders and foreign root Markdown at their paths, with `--plan` listing each.
- [ ] 9.11 Start `exomem init` and Cloud cells in `root`. Remove paths taught in the scaffold skill and references, and keep the scaffold generic.
- [ ] 9.12 In Cloud import, map `nested` trees to `root` through the relayout planner. Hold its link repoints to the rule of `add-governed-vault-consolidation` that append-only originals are not overwritten or body-rewritten, as 9.9 reconciles it. Make Cloud export emit the cell's own layout.
- [ ] 9.13 Rehearse on a disposable real-vault copy: plan counts, apply, verify, recall canary, rollback drill. Repeat on native Windows.
- [ ] 9.14 Reopen `add-readable-note-filenames` and correct its `vault-file-naming` requirements. Add the wikilink characters, leading dots and the 200-byte cap to Filesystem-Safe Title Names. Make `title` the default style in `root`. Make collision detection vault-wide with the ` (2)` suffix for `title` names in a `root` vault, and keep the `-2` contract for slug names and for `nested` vaults. Keep No Existing File Is Renamed true for `nested` vaults and for any style change in either layout, which rename nothing. Add only that `root` vaults in store mode with the `title` style follow titles by design: a governed write there renames a derived name (9.8), and the relayout renames existing derived names.
- [ ] 9.15 Reopen `add-note-type-registry` and correct its `Notes/<Name>` folder rule in its spec, its `semantic-write-contract` delta and its design. The folder becomes an area name, with the parent supplied by the layout. Let source, evidence and entity types declare `folder` too (9.2). Read a `folder` value that starts with `Notes/` as the `nested` parent plus the area name, and have the relayout rewrite such overlay values to the bare area name.
