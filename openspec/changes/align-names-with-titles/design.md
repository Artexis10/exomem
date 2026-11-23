## Context

Two facts make names drift. Pages written before 0.186.0 carry slug names. A retitle changes `title` and keeps the file name. `adopt-root-vault-layout` deferred renames after a retitle (its owner choice O5) for the relayout, which renames derived names once and offline. This change delivers that deferred work in file mode, as detection, listing and suggestions. `curation.relocate` applies the renames with the same naming rule.

## Goals / Non-Goals

**Goals:**
- Find every page whose name differs from its title's name, with no second naming rule.
- Tell the connected agent once, with the exact call that fixes a batch.
- Ask for a rename right after the user's own retitle.

**Non-Goals:**
- No rename here. `curation.relocate` renames, with its preview and undo.
- No store-mode relayout. That stays with `adopt-root-vault-layout`.

## Decisions

### 1. Relocate's rule is the rule

`relocate.planned_path` is the one function that says where a page goes. A relocation derives its moves with it, and the detector asks it whether a rename-only relocation would move a page. A page drifts exactly when that answer is a new path, so the detector cannot report a page that relocate would leave, or miss one that it would move. The name is `vault.resolve_filename_slug` applied to the title, after the date prefix that a dated type keeps. A title that no file name can hold leaves the page's stem, and the detector reports the page as having no usable title. A name taken by a file, a claim in the same derivation or another live page's title gets the writer's collision number. The page's own name stays free for it, so a page that already holds its numbered name stays. A name that differs only in case is the same file on a case-blind volume, so it stays too.

### 2. Scope comes from the registry attribute

A type is in scope when `note_types.titled_name` holds for it and it is not a captured Source. Sources keep their dated capture names. Episodes, Evidence and Records are outside that scope. No type key is listed in code. A compiled page changes folder only when a relocation names a destination project. An entity is renamed where it is. Navigation pages keep their names whatever their type. `relocate.renamed_by_title` holds this whole scope, so a relocation that names pages follows it as the detector does.

### 3. One vault item, anchored on the top index

A page-level family would put thousands of items in the queue. One item stands for the vault. It is anchored on the vault's top `index.md`, which no rename moves, so its reference survives the renames it causes. A vault without a top index has no item and no item reference, because the due-state block serves no row whose page is missing. Its `signal_version` is a hash of the drifted paths and their planned names, so a decision binds to the set that was shown and the item reopens when the set changes. The item joins the default review listing last, after the backlog queues, so a dated obligation ranks first.

### 4. The listing serves a stored row; the reads stay bounded

The due-state projection stores one row for the family, on the anchor, with the counts the item is built from and the time of the scan that took them. It does not store the drifted set: thousands of rows would be rewritten on every write delta. The default review listing rebuilds the item from that row and scans nothing. The item's text states that time, because the counts are as old as the row. A restricted caller, and a vault whose projection holds no row, audit the family as before. The row is recompute-only: a reconcile and the end of a renaming curation run (decision 7) write it, and a page write does not. `inspect_name_drift` makes one pass over the admitted corpus through the cached frontmatter reads and returns one page of rows. A stopped pass returns `complete: false` and no total.

### 5. The write suggestion plans from the new title in memory

A write whose title differs from the page's previous title judges that one page through `relocate.planned_path`. It scans no other page. Only a changed title asks, and the suggestion carries the call with `pages=[ref]` and no destination project. It asks nothing: the user's edit is the approval.

### 6. The batch resolver

`name_drift.pages_for_ref` resolves any reference of the item, earlier ones included, to the next batch: the first drifted pages in path order from a fresh pass. It never returns a page with no usable title, one the caller cannot rename, or one that is refused (decision 8). `curation.relocate` reaches it through `attention.pages_of_item` when its `review_ref` names the item. The server chose that batch, so the relocation may shrink it. It sizes the batch before it prepares it (decision 9). A batch still too large to seal is halved, and the pages it leaves out go to the next batch. A page whose move or referrer rewrite the move contract refuses is left out and listed with its code, with no limit on the count. Each pass drops at least one page, so the loop ends. A repoint step's refusal names the moved pages that the refused referrer links to (`CurationError.pages`). Pages a caller names keep the refusal.

### 7. A renaming run refreshes the row

When a curation run with a move or unmove step ends, curation schedules `due_state.refresh_name_drift`. The refresh rebuilds the one row from a fresh pass, so the next listing shows the remaining backlog with the same reference. It runs off the request path.

### 8. Refused pages are remembered beside the projection

A rename-only relocation by the item's `review_ref` reports each page it left out through `attention.record_refused`. `name_drift` writes them to `name-drift-refusals.json` in the vault's state directory, beside the due-state projection. The file keeps them under the caller's key (`relocate.reader_key`: the audience, the authorization session, the purpose and the connector ceiling), and a scan reads only its own caller's entries, so a refusal under one ceiling never hides a page from a caller under another. The file keeps a bounded number of the most recently recorded keys (`REFUSAL_READERS`), because each configuration edit gives every caller a new key. Each entry keeps the page's code, the SHA-256 of its exact bytes and that of each page that links to it then. Both hashes come from the corpus read that the scan uses. The scan reads the file and compares those hashes with the corpus hashes it already holds, so it prepares no move. A matching page counts as refused, with its code, and leaves the drifted set. An edit to the page, or an edit, move or deletion of one of those referrers, lets the page back in. The entry is the caller's own, so a recorded referrer that its scan no longer holds has changed. The row is refreshed after each record, so the item closes when every remaining drifted page is refused.

The row is recompute-only: a reconcile or a refresh rewrites it from a scan, so it cannot hold the refusals. A separate file outlives both. Losing it costs one refused preparation per page, which records the refusal again. An entry keeps no reason text, because a reason can name pages that another audience may not see.

### 9. The batch is sized before it is prepared

Halving found a batch too large only at the seal, after every step of it was prepared, and each halving prepared the kept half's link rewrites again. `relocate._fitting` now sizes the batch first, from one read of the links to its pages. It adds an estimate per moved page, per referring page and per link, plus each link line twice, because a rewrite seals the old and new line. It keeps the pages from the head of the batch that fit 75% of the sealed-plan bound, at least one. The rest go to the next batch. Halving stays as the fallback for a batch that the estimate under-counts.

Measured on 2026-11-23 on the laptop, corpus cache on, with the fixture vault plus 3,000 generated research notes in 12 folders, nine in ten of them slug-named. The request budget is 50 s, and the load average was 13 to 16 on 20 cores during the runs.

| Measure | 6 links per note | 12 links per note |
|---|---|---|
| Pages sized into the batch, of 200 | 105 | 65 |
| Sealed: moves, steps | 97, 117 | 57, 80 |
| Left out as refused (fixture notes without semantic units) | 8 | 8 |
| Step preparations | 125: each sealed step once, each refused move once | 88, the same |
| Halvings | 0 | 0 |
| Relocate calls to the seal | 1, 37.6 s | 1, 28.4 s |
| Sealed plan | 700 KB, 67% of the bound | 706 KB, 67% of the bound |
| Estimate over sealed size | 1.08 | 1.07 |

A move seals about 920 bytes plus six copies of its old and new paths, 1.7 to 2.0 KB with its summary row. A referring page seals about 600 bytes, and a link about 300. The page estimate is set at 2.2 KB, so paths longer than the measured ones still fit. With the 75% share, a batch seals unhalved until the estimate under-counts by a third.

## Risks / Trade-offs

- The stored row lags a rename or a retitle made outside a curation run until the next reconcile or renaming run. The retitle's own response carries the rename call, and `inspect_name_drift` and the batch resolver always read the vault again.
- A move that the semantic write contract refuses (a page without semantic units, or a referrer without a relation disposition) stays drifted and is counted as refused. Each refused page costs one preparation pass when it is first tried.
- A later batch's link rewrite changes the bytes of a refused page that links to a renamed page, or of one of its referrers. That page is tried once more and refused again, which costs one pass.
- A refusal does not bind a referrer that names the page only in prose, one outside the Knowledge Base root, or one the scanning caller cannot read. The scan reads none of them, so it cannot tell a deletion from an absence. An edit to such a referrer does not let the page back in; an edit to the page does.
- Each move step of a relocation prepares its own semantic preflight, so a batch of about 100 pages took 28 to 38 s to prepare on a 3,000-page vault (decision 9). A slower vault prepares over several calls, each answering `preparing`, and one approval still covers the sealed plan.
- A vault without a top `index.md` has no anchor, so it has no item until the index exists. `inspect_name_drift` still lists the drift.
- A page created with an explicit `slug` keeps a name the caller chose, and the detector reports it as drift. A page does not record that choice, so the owner can dismiss the item, or the batch preview shows the names.
