## Decision

Path identity belongs to the vault. Two sibling names with equal `vault_name_key` (NFC, then casefold) are one name, on every host. Only paths that a writer is about to create are re-spelled; a path that already exists keeps the spelling the host stores.

## Why the vault, not the host

The vault travels between hosts. A rule that depends on the running volume produces a vault whose contents differ by where the write happened, and the difference surfaces only at export or on another machine. The exporter and `unique_path` already apply the key to files. The gap is folders and the writers that never consult siblings.

## Why destinations only

Re-spelling every resolved path would merge the two identities of an existing split on a case-sensitive volume. A delete of `food/a.md` would then be recorded as `Food/a.md`, and the index would drop the surviving page's row. Identities therefore keep the host-faithful `canonical_vault_rel`, and only destinations take the re-spell.

## Shape

1. `vault_name_key(name, form="NFC")` and `name_variants` live in `held_fs.py`, the lowest layer that needs them. `vault.py` re-exports the key. The exporter, `audit_fix`, `unique_path`, project keys and vocabulary folding import it. `form="NFKC"` serves the looser comparisons.
2. `respell_new_path` walks a destination: an existing component costs one `lstat`, and a missing one lists its folder once. A missing component that matches a sibling's key takes that spelling; an exact match always wins. The first component that matches nothing ends the walk. A move passes its source as `replacing`, so a move that changes only the case of its own name still renames.
3. Writers re-spell before the destination binds: `create_file`, `move_file`, `create_directory`, note creation, `evidence_folder`, `client_artifacts._destination` and project-key planning. A move whose re-spelled destination is its own source is refused, because only a move of the folder changes the folder's spelling.
4. The held filesystem's `parent(create=True)` is the backstop. It refuses a missing folder whose key matches an existing sibling with `PATH_VARIANT_EXISTS` and names that spelling. Governed writes that create a folder reach it. It lists a folder only when it is about to create a child there, so the write path gains no corpus walk. Batch scratch workspaces skip it, because they carry random names and are removed again. Windows folds case when it opens a folder, so it has no backstop; the writers' re-spell still covers a Unicode-form variant there. A wrong fire costs one corrected call that names the existing spelling. An existing exact folder never reaches the check, so old drift never blocks.
5. A write into a folder of an existing split warns with `folder_split_warnings`, which names the other spelling and the `path_variants` audit. Domain-projected note and source folders keep their existing `AMBIGUOUS_DOMAIN_DESTINATION` refusal while two equivalent domain folders exist.
6. `plan_project_keys` derives a folder with `_title_case_slug`, which reuses an existing spelling first. An explicit folder that collides with a different existing spelling is refused with `PATH_VARIANT_EXISTS`.
7. The `path_variants` audit reads names only. Defects are NFC-casefold groups. Candidates are NFKC groups after dropping non-alphanumeric characters (a Unicode character class, not a word list). It is opt-in, so a vault with old drift does not gain findings on every sweep.
8. `CASE_COLLISION` gets its own export code. The export result carries only codes, so it reveals no path or count. cellctl lists the code among the job failure codes, so the job records it instead of a generic failure.
9. A move that rewrites inbound links asks, for an Evidence page, whether every rewritten link sits in `ingested_into`. If so, the move patches that field with `splice_markdown_frontmatter`, the same parser-placed splice that it already uses for an append-only page's pointer fields. The body and every other field keep their bytes. Any other rewritten link keeps the `APPEND_ONLY` refusal.

## Not done

No automatic merge of split folders, and no rename on read. A merge moves content and belongs to the owner. A Sources page keeps the `APPEND_ONLY` refusal for its `ingested_into` field.
