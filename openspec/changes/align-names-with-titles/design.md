## Context

Two facts make names drift. Pages written before 0.186.0 carry slug names. A retitle changes `title` and keeps the file name. `adopt-root-vault-layout` deferred renames after a retitle (its owner choice O5) for the relayout, which renames derived names once and offline. This change delivers that deferred work in file mode, as detection, listing and suggestions. `curation.relocate` applies the renames.

## Goals / Non-Goals

**Goals:**
- Find every page whose name differs from its title's name, with no second naming rule.
- Tell the connected agent once, with the exact call that fixes a batch.
- Ask for a rename right after the user's own retitle.

**Non-Goals:**
- No rename here. `curation.relocate` renames, with its preview and undo.
- No store-mode relayout. That stays with `adopt-root-vault-layout`.

## Decisions

### 1. The creation path is the rule

The planned name is `vault.resolve_filename_slug` applied to the frontmatter title, so a change to the naming rule changes drift with it. A title that no file name can hold (`corrected_title_name` is empty, or no bytes fit) has no usable title in every style. The name differs only by a collision number when it equals the planned name numbered as `vault.numbered_stem` numbers it (`-n`) or as `layout.allocate` numbers it (` (n)`); both forms exist on disk.

### 2. Scope comes from the registry attribute

A type is in scope when `note_types.titled_name` holds for it and it is not a captured Source. Sources keep their dated capture names. Evidence and Records are outside `titled_name`. No type key is listed in code. A dated type keeps the date prefix that its current name carries, as `layout.place` reads it.

### 3. One vault item, anchored on the first drifted page

A page-level family would put thousands of items in the queue. One item stands for the vault. Its `signal_version` is a hash of the drifted paths and their planned names, so a decision binds to the set that was shown and the item reopens when the set changes. The anchor is the first drifted path, so the item's identity moves when that page is renamed; the fingerprint moves with the set either way. The item joins the default review listing last, after the backlog queues, so a dated obligation ranks first.

### 4. The listing is a bounded read, not a stored list

`inspect_name_drift` makes one pass over the admitted corpus through the cached frontmatter reads, as the audit does, and returns one page of rows. A stopped scan returns `complete: false` and no total. The projection does not store the drifted set: 2,500 rows would be rewritten on every write delta.

### 5. The write suggestion plans from the new title in memory

A write whose title differs from the page's previous title judges that one page. It reads no other page. Only a changed title asks, and the suggestion carries the call with `pages=[ref]` and no destination project. It asks nothing: the user's edit is the approval.

### 6. The batch resolver

`next_batch` returns the next N drifted pages in path order and never a page with no usable title. `curation.relocate` calls it when its `review_ref` names this item.

## Risks / Trade-offs

- The listing costs one corpus pass per call. A vault of tens of thousands of pages pays that on each page of the listing; the cached reads make it a stat per file.
- The review item reads the corpus each time the default review listing runs, as every audit-backed family does. The due-state projection is not extended: its rows are per page and per audience, and this item is one row about the whole vault.
- A page created with an explicit `slug` keeps a name the caller chose, and the detector reports it as drift. A page does not record that choice, so the owner can dismiss the item or the batch preview shows the names.
