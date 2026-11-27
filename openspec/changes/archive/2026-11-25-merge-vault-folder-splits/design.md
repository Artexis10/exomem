## Context

The vault owns path identity: two sibling names that are equal after NFC normalisation and casefolding are one name (`vault_name_key`). `respell_new_path` gives a missing destination component an existing sibling's spelling, and the held filesystem refuses to create a new variant folder. An existing split is left alone by design, and it is the remaining source of growth: a component that exists exactly is accepted after one `lstat`, so a write naming `food/` lands in `food/` while `Food/` exists.

The path is a projection of a resolved identity, not a second ontology. Exact equivalence resolves deterministically before the destination binds, and the registered identity wins.

## Decisions

### D1. One canonical spelling per split, chosen from data

`canonical_variant(vault_root, parent, spellings, entries)` decides which spelling of a split folder writes go to. It applies four rules in order:

1. The spelling in NFC form. An export refuses any other form, so a write steered elsewhere would add to an export failure.
2. A spelling the vault declares: the managed root, the scaffold's folders, a folder a registry gives a type or placement (`layout.declared_folders`), or a project folder the project registry names. Code reads these folders by their exact spelling.
3. The spelling whose folder holds the most direct entries.
4. The first spelling in code-point order.

The registries and the scaffold are data, so no vocabulary enters code. A count alone would let a stray folder of loose files take every structured write. Rules 3 and 4 are deterministic and stable under the merge: moving files into the canonical folder only grows it. The destination writers and the merge call the same function, so a merge's moves never get steered back.

Alternative considered: keep each write on its requested spelling and rely on the merge. That leaves the split growing on every client until the owner merges, which is the defect.

### D2. Steering only where a write adds something

The rule applies to every folder of a destination that exists and has a variant sibling, the destination's own folder included. A destination that already exists as a file at the requested spelling keeps it, so an update never forks a file; its folder is then not listed. Reads, deletes and index keys keep the host's spelling, as before.

### D3. The merge moves through `move_file`, as one maintenance job

Moving a file safely involves lineage, media bindings, originals, the store and link rewrites, and `move_file` already does all of it. A curation plan would add crash-safe resume, but a curation `move` step accepts only one unpaired Markdown page, and a split is mostly captured files with their pages. So the apply calls `move_file` once per file: the page of a captured pair carries its bytes.

Each move scans the vault for the pages that link to the file, so a merge costs the vault's size times the files it moves. A request-bound apply would move about one file per call on a large vault. `path-variants` is therefore a maintenance job kind, like `backfill-ids`: a remote owner's preview and apply each run as a job that no request bounds. The preview plans every move, and its plan id binds them and the other spellings. The apply plans again and refuses a changed plan before it moves anything. It then previews and commits each move with that preview's digest, holding the mutation boundary only while the move commits. The job reports its progress, stops between moves when cancelled, and lists every moved file in its receipt. A move that `move_file` refuses is skipped and listed.

The merge is one-way: no operation moves the files back. `record_repoints` keeps the owner-only bytes of each append-only page a move repoints.

### D4. Emptied variant folders are removed by the same apply

No existing operation removes an empty folder inside an append-only tree, and a merge leaves its emptied spellings behind. After its moves, the apply removes each other spelling that the plan listed, that holds no file, that is writable and that is still a split. Removal is a held `rmdir`, under the mutation boundary, deepest folder first. A spelling that still holds a file is kept and listed. An empty folder holds no content, so the append-only rule does not apply to it, and the removal needs no undo beyond its log entry.

### D5. The owner sees splits without the export naming paths

The export result stays path-free, because the cell manager must not see vault paths. `path_variants` moves into the default audit union, and its finding points at the preview. An owner who audits sees the split before an export fails.

## Risks

- A merge of many files on a large vault runs for a long time, because each move scans the vault for its referrers. It runs as a job with progress and cancel; the per-move scan is the file move's own cost.
- A move whose link rewrite `move_file` refuses (an append-only page that cannot prove the link still names the file) is listed as blocked with the refusal. It is not moved without its links.
