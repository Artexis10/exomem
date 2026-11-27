## Context

`respell_new_path(vault_root, rel, replacing=None)` resolves the destination of create file, create directory, move, note placement, Evidence capture (`preserve._evidence_segments`), client artifacts and the split merge (`path_variants._destination`). In store mode it delegates to `_respell_in_store`, which reads names from the store view and from the disk. Both walk the destination one component at a time and list each folder on the way.

- For a component that does not exist as spelled, `_sibling_spelling` returns a sibling with the same `vault_name_key` (NFC, then casefold), excluding the path a move vacates.
- A folder that exists and has a variant takes `canonical_variant`: NFC first, then a spelling the layout declares or the project registry names, then most entries, then code point. At the vault top and the managed root, an undeclared split has no canonical spelling.
- `folder_split_warnings` warns about each folder on the way that has a case variant.

Three looser keys exist today:

- The `path_variants` audit keeps letters and digits only (NFKC). It reports matches as candidates, because `C++` and `C` share that key.
- `vocabulary_fold.fold_term` maps separators to one `-`, removes one plural and keeps an English exception list. It does not match `garden-tools` with `gardentools`: they fold to `garden-tool` and `gardentool`.
- Domain folders resolve through `vocabulary_resolution._existing_projection_spelling` (normalize, registry aliases, NFKC casefold). It refuses when two equivalents exist.

## Goals / Non-Goals

**Goals:**
- A governed write that resolves its destination through `respell_new_path` cannot create a folder that differs from an existing sibling folder only by case or separators. The exceptions are the places where code reads a folder by its exact spelling (Decision 3).
- A write into an existing separator pair lands in its canonical spelling and warns, as a write into a case split does.
- The owner can merge an existing separator pair with an approved plan.
- File mode and store mode behave the same.

**Non-Goals:**
- Plurals and other inflections (`Garden`, `Gardens`). A plural folder can be another collection, and nothing in the vault tells the two apart.
- File names. `a-b.md` and `ab.md` can be different pages.
- Source and reclassify folders, which the source taxonomy and domain registry already resolve.
- Writers that do not call the resolver yet. The one-write-pipeline change records that the episode store and the import run are among them, and its names step adopts this rule.

## Decisions

1. **One folder key, separators only.** `folder_lookalike_key(name)` removes every character in the Unicode categories Zs, Pd and Pc from `vault_name_key(name)`, but keeps a leading run of Pc. A category test is a closed set fixed by the Unicode standard, not a word list. A leading underscore marks system folders (`_Schema`, `_Governance`, `_Sources`), so it stays part of the name. `fold_term` is not reused: it misses the live pair, it folds plurals, and it carries an English word list that would decide folder identity.
2. **A split is a case group or a folder-key group.** Where `_sibling_spelling` finds no case match, a folder the walk would create takes the spelling of a sibling folder with the same folder key. With several such folders it takes `canonical_variant` over them, so no third spelling appears. A folder that exists and has a folder-key variant takes the split's canonical spelling, as a case split does, except at the vault top and the managed root. `folder_split_warnings` names both kinds.
3. **Some spellings are never steered.** The separator rule keeps the caller's spelling in three places: at the vault top and the managed root, for a folder that `layout.declared_folders` names, and for a project folder that the registry names in `Notes/Research`. Code reads those folders by exact spelling. Elsewhere a registered project spelling is steered like any other name, because nothing reads `Evidence/<project folder>` by exact spelling. A write at an exempt place can make a new pair. The audit then reports it, and the merge prefers the declared spelling.
4. **Callers say when the leaf is a folder.** `respell_new_path` gains `leaf_is_folder: bool = False`. Evidence capture, note placement, client artifact folders and create_directory pass `True`. A leaf without the flag is treated as a file. The one-write-pipeline names step takes the flag from each surface's plan.
5. **Tags follow the folder.** An Evidence capture computes `family_tags` from the scope and category folders it landed in. Its activity line and log entry name those folders too.
6. **One key for planning, merge and audit.** `project_keys.reuse_folder_spelling` matches by the folder key. `path_variants.sibling_groups` groups siblings for the preview, the emptied-folder check and the audit. The audit reports a folder-key group as a defect. Its candidate class keeps the letters-and-digits key for groups that only that key joins. Export keeps the case key, because a separator pair is not a portability failure.
7. **The path in the result is the report.** Writers already return the path they wrote. An agent that named `gardentools` sees `Evidence/garden-tools/...` in the result.

Source: "Building effective agents" (Anthropic, 2024), on tool design: change a tool's arguments so that mistakes are harder to make. The fix resolves a near-miss folder name inside the tool instead of relying on the agent to spell it.

## Risks / Trade-offs

- A caller that meant a new folder whose name differs from an existing one only by spaces, dashes or underscores gets the existing folder. No governed tool renames a folder, so the owner cannot undo that steer through one. Case steering has the same limit today. Two folders that differ only by a separator are rarely two collections, and a missed steer costs one collection split across two folders.
- A merge plan now can move files between separator variants. The owner approves each plan, as for case splits.
- Existing pairs start converging at the release. A new write that names the smaller side of a pair lands in the canonical spelling and warns, with no approval, as for a case split. That includes a rename inside the smaller side, and a pair someone kept on purpose. Only the owner's merge moves the files already there.
- The merge repoints a moved Evidence page, but it does not add the canonical folder's family tags. After a merge of `gardentools` into `garden-tools`, the moved pages still carry the tag `gardentools`, so a tag filter still divides the merged collection. A follow-up can append the canonical tags in the move, as `evidence_reuse` appends a family's tags today. This change leaves it out to keep the merge's writes as they are.
- Cost. A write whose folders exist without a variant, or whose new folder has no look-alike, adds no system call. The key is computed over the listing that the walk already reads. A steered folder costs one listing per look-alike candidate and one read of the layout and the project registry. The warning reads each folder on the way once, as before.
