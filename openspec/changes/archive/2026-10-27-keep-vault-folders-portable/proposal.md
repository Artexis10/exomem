## Why

A vault folder can hold two children whose names differ only by case or Unicode form (`Food/` and `food/`). A case-sensitive host accepts both. Windows and default macOS hold them as one entry, so an export of that vault fails with `CASE_COLLISION`, and the owner cannot see which paths cause it. Writers that take a model-given scope, category or path segment join the text as typed, so a lower-case spelling next to an existing capitalised folder creates the split.

Repairing a split also hits a second refusal. A move of a note that an Evidence page names in its `ingested_into` back-reference is refused with `APPEND_ONLY`, although that field is maintained by note writes and is not captured content.

## What Changes

- Path identity belongs to the vault, not the host filesystem. One key function (NFC normalisation, then casefold) defines when two names are one name.
- A new helper re-spells a destination to an existing sibling on every platform before the destination binds. An exact match wins. `create_file`, `move_file`, `create_directory`, note creation, `preserve`, client artifacts and project-key folder planning use it.
- A recorded identity keeps the host's spelling. `canonical_vault_rel` keeps its host-faithful rule, so reads, deletes and index keys of an existing split stay distinct.
- The held filesystem's folder creator refuses a new folder whose key matches an existing sibling (`PATH_VARIANT_EXISTS`). An existing split never blocks a write: the write goes to the requested spelling and carries a warning.
- Project-key auto-registration reuses an existing folder spelling and refuses an explicit folder that collides with one.
- `maintain_memory(mode="audit")` gains the opt-in category `path_variants`: NFC-casefold groups are defects, and NFKC or separator-only groups are candidates. It is read-only.
- The cell export reports `CASE_COLLISION` as its own code with no path or count, and the cell manager records it as the job's failure code.
- A move rewrites an Evidence page's `ingested_into` entry for the moved note when that field holds every rewritten link. Any other link in an append-only page still refuses the move.
- The skill scaffold tells agents to reuse an existing folder spelling, and one contradiction about the experiment folder spelling is removed.

## Impact

- Capabilities amended:
  - `hosted-vault-portability`: a vault-side invariant and a diagnosable export code.
  - `command-surface`: the append-only rule for inbound link rewrites, with the Evidence back-reference exception.
- Code: `held_fs.py`, `_held_fs_posix.py`, `_held_fs_windows.py`, `vault.py`, `create_file.py`, `move_file.py`, `create_directory.py`, `note.py`, `preserve.py`, `client_artifacts.py`, `semantic_writes.py`, `project_keys.py`, `audit.py`, `audit_fix.py`, `vocabulary_resolution.py`, `vault_portability.py`, `cloud_export.py`, cellctl `exports.py`, the scaffold and its derived skills and plugins.
- No data migration. No automatic merge of an existing split.
