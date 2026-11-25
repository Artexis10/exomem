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

### D3. The merge moves through `move_file`, in bounded batches

Moving a file safely involves lineage, media bindings, originals, the store and link rewrites, and `move_file` already does all of it. A curation plan would add crash-safe resume, but a curation `move` step accepts only one unpaired Markdown page, and a split is mostly captured files with their pages. So the apply calls `move_file` once per file: the page of a captured pair carries its bytes. Each move previews first, and the plan id binds the batch's moves and their digests. A move after the first in a batch takes a fresh digest, because an earlier move may have rewritten a page that it rewrites too. Each move is atomic on its own, so a failure stops the batch and reports the files moved so far. `record_repoints` makes each move undoable. A batch is bounded, so one call stays short; the preview names the next batch until none is left.

### D4. Empty variant folders are removed by the preview's own apply

No existing operation removes an empty folder inside an append-only tree, and a merge leaves its emptied spellings behind. `maintain_memory(mode="path-variants", apply=true, plan_id, why)` removes exactly the empty variant folders that its preview listed. The plan id binds those folders. Removal is a held `rmdir`, which fails on a folder that gained an entry, and the folder must still have its variant sibling. An empty folder holds no content, so the append-only rule does not apply to it, and the removal needs no undo beyond its log entry.

### D5. The owner sees splits without the export naming paths

The export result stays path-free, because the cell manager must not see vault paths. `path_variants` moves into the default audit union, and its finding points at the preview. An owner who audits sees the split before an export fails.

## Risks

- A vault with a large split needs several batches. The owner approves the merge once, and each batch is its own apply; the preview counts what remains.
- A move whose link rewrite `move_file` refuses (an append-only page that cannot prove the link still names the file) is listed as blocked with the refusal. It is not moved without its links.
