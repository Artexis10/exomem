## Why

A vault folder can hold two children whose names differ only by case or Unicode form (`Food/` and `food/`). A case-sensitive host accepts both. Windows and default macOS hold them as one entry, so an export of that vault fails with `CASE_COLLISION` and the owner cannot see which paths cause it. The writers that take a model-given scope or category (`preserve`, client artifacts, project-key folders) join the text as typed, so a lower-case spelling next to an existing capitalised folder creates the split.

## What Changes

- Path identity belongs to the vault, not the host filesystem. One key function (NFC normalisation, then casefold) defines when two names are one name.
- `canonical_vault_rel` re-spells to an existing sibling on every platform. The earlier rule that a case-sensitive volume is never redirected is reversed. An exact match still wins.
- `preserve`, client artifacts, and project-key folder planning re-spell their destinations before identity binds.
- The folder creator refuses a new folder whose key matches an existing sibling (`PATH_VARIANT_EXISTS`). An existing split never blocks a write: the write goes to the requested spelling and carries a warning.
- Project-key auto-registration reuses an existing folder spelling and refuses an explicit folder that collides with one.
- `maintain_memory(mode="audit")` gains the opt-in category `path_variants`: NFC-casefold groups are defects; NFKC or separator-only groups are candidates. It is read-only.
- The cell export reports `CASE_COLLISION` as its own code with a group count and no path, and points the owner to the audit.
- The skill scaffold tells agents to reuse an existing folder spelling, and one contradiction about the experiment folder spelling is removed.

## Impact

- Capability amended: `hosted-vault-portability` (the export and restore case-collision rules gain a vault-side invariant and a diagnosable export code).
- Code: `vault.py`, `preserve.py`, `client_artifacts.py`, `project_keys.py`, `note.py`, `audit.py`, `vault_portability.py`, `cloud_export.py`, `audit_fix.py`, `vocabulary_resolution.py`, the scaffold and its derived skills and plugin.
- No data migration. No automatic merge of an existing split.
