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

A type is in scope when `note_types.titled_name` holds for it and it is not a captured Source. Sources keep their dated capture names. Episodes, Evidence and Records are outside that scope. No type key is listed in code. A compiled page changes folder only when a relocation names a destination project. An entity is renamed where it is. Navigation pages keep their names.

### 3. One vault item, anchored on the top index

A page-level family would put thousands of items in the queue. One item stands for the vault. It is anchored on the vault's top `index.md`, which no rename moves, so its reference survives the renames it causes. Its `signal_version` is a hash of the drifted paths and their planned names, so a decision binds to the set that was shown and the item reopens when the set changes. The item joins the default review listing last, after the backlog queues, so a dated obligation ranks first.

### 4. The listing serves a stored row; the reads stay bounded

The due-state projection stores one row for the family, on the anchor, with the counts the item is built from. It does not store the drifted set: thousands of rows would be rewritten on every write delta. The default review listing rebuilds the item from that row and scans nothing. A restricted caller, and a vault whose projection holds no row, audit the family as before. The row is recompute-only: a reconcile and the end of a renaming curation run (decision 7) write it, and a page write does not. `inspect_name_drift` makes one pass over the admitted corpus through the cached frontmatter reads and returns one page of rows. A stopped pass returns `complete: false` and no total.

### 5. The write suggestion plans from the new title in memory

A write whose title differs from the page's previous title judges that one page through `relocate.planned_path`. It scans no other page. Only a changed title asks, and the suggestion carries the call with `pages=[ref]` and no destination project. It asks nothing: the user's edit is the approval.

### 6. The batch resolver

`name_drift.pages_for_ref` resolves any reference of the item, earlier ones included, to the next batch: the first drifted pages in path order from a fresh pass. It never returns a page with no usable title or one the caller cannot rename. `curation.relocate` reaches it through `attention.pages_of_item` when its `review_ref` names the item. A batch too large to seal is halved, and the pages it leaves out go to the next batch.

### 7. A renaming run refreshes the row

When a curation run with a move or unmove step ends, curation schedules `due_state.refresh_name_drift`. The refresh rebuilds the one row from a fresh pass, so the next listing shows the remaining backlog with the same reference. It runs off the request path.

## Risks / Trade-offs

- The stored row lags a rename or a retitle made outside a curation run until the next reconcile or renaming run. The retitle's own response carries the rename call, and `inspect_name_drift` and the batch resolver always read the vault again.
- A move that the semantic write contract refuses (a page without semantic units, or a referrer without a relation disposition) stays drifted. A batch excludes up to three such pages, so a page that the contract keeps refusing returns at the head of every batch until the page is repaired.
- Each move step of a relocation prepares its own semantic preflight. A large batch on a large vault therefore costs minutes, not seconds. The halving bounds the plan size, not the preparation time.
- A vault without a top `index.md` has no anchor, so the item carries no reference until the index exists.
- A page created with an explicit `slug` keeps a name the caller chose, and the detector reports it as drift. A page does not record that choice, so the owner can dismiss the item, or the batch preview shows the names.
