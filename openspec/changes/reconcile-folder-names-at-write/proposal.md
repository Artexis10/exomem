## Why

A write that names a folder in a slightly different spelling creates a second folder beside the first. Since the merge-vault-folder-splits change, a write steers a case variant into the existing folder: `Notes/garden/` lands in `Notes/Garden/`. A spelling that differs only by separators still makes a new folder. For example, `Evidence/garden-tools/` exists and a capture names the scope `gardentools`, or `garden_tools`. One owner vault holds such a pair today. The `path_variants` audit lists it as a candidate for a person to judge, and no tool can merge it.

Such a pair still exports, because export compares names after case folding only. The cost is one collection in two folders: captures, searches and tags split between them, and every later write can pick either side.

A folder name is vocabulary: agents pass scope, category, placement and project names to writes. The fix belongs where writes resolve a folder, so that an agent cannot create the variant. An existing pair gets merged through the existing preview and apply, which the owner approves.

## What Changes

- One folder identity key, `vault.folder_lookalike_key`. It is the NFC casefold (`vault_name_key`) with every Unicode separator removed: the space separators (Zs), the dash punctuation (Pd) and the connector punctuation (Pc, such as `_`). A leading run of connector punctuation stays, so `_Schema` and `Schema` keep different keys. Letters, digits and every other character also stay, so `C++` and `C` keep different keys.
- `respell_new_path` and its store-mode counterpart take a new argument that says whether the leaf is a folder. Every caller that passes a folder as the leaf sets it: Evidence capture, note placement, client artifacts and create_directory.
- When a write would create a folder, and an existing sibling folder has the same key, the write lands in that folder. When several do, it lands in their canonical spelling (`canonical_variant`), so it never adds a third spelling. A file name is never steered. At the top of the vault and of the managed root, and for a spelling that the layout declares or the project registry names, the write keeps its spelling.
- An Evidence capture that lands in a steered folder takes its family tags from the folder spellings it landed in.
- Project-key planning reuses an existing folder by the same key, so it refuses to register `Garden Tools` beside `Garden-Tools/` as it already refuses a case variant.
- The `path-variants` preview groups folders by the same key, so its owner-approved apply merges a separator pair as it merges a case split. The audit reports such a group as a defect whose fix is that preview. A group equal only under the looser key that keeps letters and digits stays a candidate.

## Capabilities

### Modified Capabilities

- `hosted-vault-portability`: new requirements for steering a new folder into its look-alike and for merging separator groups; the audit and preview requirements now use the folder key.

## Impact

- Code: `vault.py` (key, `respell_new_path`, `_respell_in_store`, `_sibling_spelling`), the leaf-folder argument at `preserve.py`, `note.py`, `client_artifacts.py` and `create_directory.py`, `preserve.family_tags` callers, `project_keys.reuse_folder_spelling`, `path_variants.py` grouping and emptied-folder check, `audit.py` path_variants classes.
- Not covered: Source and reclassify destinations take their folders from the source taxonomy and domain registry, which already reconcile names (`vocabulary_resolution`). The held filesystem's create check stays on the case key.
- Cost: no extra system call. The rule reads the folder listing that the walk already reads.
- No migration. An existing pair changes only through an owner-approved merge plan.
