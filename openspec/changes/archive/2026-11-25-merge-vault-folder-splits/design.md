## Context

The vault owns path identity: two sibling names that are equal after NFC normalisation and casefolding are one name (`vault_name_key`). `respell_new_path` gives a missing destination component an existing sibling's spelling, and the held filesystem refuses to create a new variant folder. An existing split is left alone by design, and it is the remaining source of growth: a component that exists exactly is accepted after one `lstat`, so a write naming `food/` lands in `food/` while `Food/` exists.

The path is a projection of a resolved identity, not a second ontology. Exact equivalence resolves deterministically before the destination binds, and the registered identity wins.

## Decisions

### D1. One canonical spelling per split, chosen from data

`canonical_variant(vault_root, parent, names)` decides which spelling of a split folder writes go to. It applies three rules in order:

1. The project registry names one of the spellings as a project folder.
2. The spelling whose folder holds the most direct entries.
3. The first spelling in code-point order.

The registry is data, so no vocabulary enters code. Rules 2 and 3 are deterministic and stable under the merge: moving files into the canonical folder only grows it. The destination writers and the merge call the same function, so a merge's moves never get steered back.

Alternative considered: keep each write on its requested spelling and rely on the merge. That leaves the split growing on every client until the owner merges, which is the defect.

### D2. Steering only where a write adds something

The rule applies to a directory component of a destination that exists and has a variant sibling. A destination whose full remaining path already exists at the requested spelling keeps it, so an update never forks a file. The walk lists a folder only at a directory component, never the destination folder itself. A write into a large folder therefore costs no extra listing of that folder. Reads, deletes and index keys keep the host's spelling, as before.

### D3. The merge moves through `move_file`, in batches bounded by count and time

Moving a file safely involves lineage, media bindings, originals, the store and link rewrites, and `move_file` already does all of it. A curation plan would add crash-safe resume, but a curation `move` step accepts only one unpaired Markdown page, and a split is mostly captured files with their pages. So the apply calls `move_file` once per file: the page of a captured pair carries its bytes.

A plan resolves destinations only until its batch is full and previews no move, so its cost follows the batch, not the size of the split. The plan id binds the batch's moves and the empty folders. A move's preview scans the vault for the pages that link to the file, so it grows with the vault. The preview therefore previews the batch only while the request budget allows, and marks the rest unpreviewed. The apply previews each move and commits it with that preview's digest, which `move_file` checks against the referrers it finds at commit. It skips and lists a move that `move_file` refuses, and it stops when the budget runs short and points to a new preview. Measured on a store-mode vault with 800 pages on each side, on one laptop under memory pressure: preview 22.4 s to 15.5 s, and an apply of 16 moves 53.8 s to 32.2 s.

The merge is one-way: no operation moves the files back. `record_repoints` keeps the owner-only bytes of each append-only page a move repoints.

### D4. Empty variant folders are removed by the preview's own apply

No existing operation removes an empty folder inside an append-only tree, and a merge leaves its emptied spellings behind. `maintain_memory(mode="path-variants", apply=true, plan_id, why)` removes exactly the empty variant folders that its preview listed. The plan id binds those folders. Removal is a held `rmdir`, which fails on a folder that gained an entry, and the folder must still have its variant sibling. An empty folder holds no content, so the append-only rule does not apply to it, and the removal needs no undo beyond its log entry.

### D5. The owner sees splits without the export naming paths

The export result stays path-free, because the cell manager must not see vault paths. `path_variants` moves into the default audit union, and its finding points at the preview. An owner who audits sees the split before an export fails.

## Risks

- A vault with a large split needs several batches. The owner approves the merge once, and each batch is its own apply; the preview counts what remains.
- A move whose link rewrite `move_file` refuses (an append-only page that cannot prove the link still names the file) is listed as blocked with the refusal. It is not moved without its links.
