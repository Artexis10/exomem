## Context

`respell_new_path(vault_root, rel, replacing=None)` resolves the destination of create file, create directory, move, note placement, Evidence capture (`preserve._evidence_segments`), client artifacts and the split merge (`path_variants._destination`). In store mode it delegates to `_respell_in_store`, which reads names from the store view and from the disk. Both walk the destination one component at a time and list each folder on the way. For a component that does not exist as spelled, `_sibling_spelling` returns a sibling with the same `vault_name_key` (NFC, then casefold), excluding the path a move vacates. A folder that already has a variant takes `canonical_variant`: NFC first, then a spelling the layout declares or the project registry names, then most entries, then code point. At the vault top and the managed root, an undeclared split has no canonical spelling.

Three looser keys exist today:

- The `path_variants` audit keeps letters and digits only (NFKC). It reports matches as candidates, because `C++` and `C` share that key.
- `vocabulary_fold.fold_term` maps separators to one `-`, removes one plural and keeps an English exception list. It does not match `garden-tools` with `gardentools`: they fold to `garden-tool` and `gardentool`.
- Domain folders resolve through `vocabulary_resolution._existing_projection_spelling` (normalize, registry aliases, NFKC casefold). It refuses when two equivalents exist.

## Goals / Non-Goals

**Goals:** a governed write cannot create a folder that differs from an existing sibling folder only by case or separators. An existing separator pair can be merged with the owner's approval. File mode and store mode behave the same.

**Non-Goals:**
- Plurals and other inflections (`Garden`, `Gardens`). A plural folder can be another collection, and nothing in the vault tells the two apart.
- File names. `a-b.md` and `ab.md` can be different pages.
- Source and reclassify folders, which the source taxonomy and domain registry already resolve.

## Decisions

1. **One folder key, separators only.** `folder_lookalike_key(name)` removes every character in the Unicode categories Zs, Pd and Pc from `vault_name_key(name)`, but keeps a leading run of Pc. A category test is a closed set fixed by the Unicode standard, not a word list. The leading underscore marks system folders (`_Schema`, `_Governance`, `_Sources`), so it stays part of the name. `fold_term` is not reused: it misses the live pair, it folds plurals, and it carries an English word list that would decide folder identity.
2. **Steer only a folder the walk would create.** The rule runs where `_sibling_spelling` finds no case match, for a component followed by more path or for a leaf the caller marks as a folder. Its targets are sibling folders with the same key, excluding the path a move vacates. One target: take its spelling. Several: take `canonical_variant` over them, so no third spelling appears.
3. **Declared and top-level spellings are never steered.** At the vault top and the managed root, code reads folders by exact spelling, so the write keeps its spelling there. The same holds for a spelling that `layout.declared_folders` or the project registry names. Such a write can make a new pair. The audit then reports it and the merge prefers the declared spelling.
4. **Callers say when the leaf is a folder.** `respell_new_path` gains `leaf_is_folder: bool = False`. Evidence capture, note placement, client artifact folders and create_directory pass `True`.
5. **Tags follow the folder.** An Evidence capture computes `family_tags` from the scope and category segments it landed in. Otherwise the tags would split where the folders no longer do.
6. **One key for planning, merge and audit.** `project_keys.reuse_folder_spelling` matches by the folder key. `path_variants._splits` groups siblings by the folder key, and the emptied-folder check uses it. The audit's defect class uses the folder key. Its candidate class keeps the alphanumeric key for groups that only that key joins. Export keeps the case key: separator pairs are not a portability failure.
7. **The path in the result is the report.** Writers already return the path they wrote. An agent that named `gardentools` sees `Evidence/garden-tools/...` in the result.

Source: "Writing effective tools for agents" (Anthropic engineering): resolve a near-miss argument inside the tool instead of relying on the agent to spell it.

## Risks / Trade-offs

- A caller that meant a new folder whose name differs from an existing one only by spaces, dashes or underscores gets the existing folder. A move undoes it. A missed steer costs a collection split across two folders. That still exports, but captures, tags and later writes divide between the two.
- A merge plan now can move files between separator variants. The owner approves each plan, as for case splits.
- The second pass reads names that the walk already listed, plus one folder check per candidate. A write makes no extra listing.
