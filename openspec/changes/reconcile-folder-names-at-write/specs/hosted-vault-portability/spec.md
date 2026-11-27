## ADDED Requirements

### Requirement: A new folder that differs from an existing one only by separators lands in it

Folder identity for writes and merges SHALL be the folder key: the name after NFC and casefold, with every Unicode space separator, dash and connector punctuation character removed, except a leading run of connector punctuation. When a governed write would create a folder and a sibling folder with the same folder key exists, the write SHALL land in that folder, or in the canonical spelling of the group when several exist. File mode and store mode SHALL apply the same rule.

#### Scenario: A capture names a scope without its hyphen

- **WHEN** `Evidence/garden-tools/` exists and a capture names the scope `gardentools`
- **THEN** the artifact lands in `Evidence/garden-tools/`
- **AND** no `Evidence/gardentools/` folder is created
- **AND** the page's family tags name `garden-tools`

#### Scenario: A capture names a category with an underscore

- **WHEN** `Evidence/Garden/shared-docs/` exists and a capture names the category `shared_docs`
- **THEN** the artifact lands in `Evidence/Garden/shared-docs/`

#### Scenario: A note placement folder is steered

- **WHEN** `Notes/garden-tools/` exists and a note's placement folder is `Notes/garden tools`
- **THEN** the note lands in `Notes/garden-tools/`

#### Scenario: Other punctuation keeps two names apart

- **WHEN** `Notes/C/` exists and a write names `Notes/C++/intro.md`
- **THEN** the page lands at `Notes/C++/intro.md`

#### Scenario: A leading underscore keeps a system folder apart

- **WHEN** the managed root holds `_Schema/` and a write names `Notes/Schema/plan.md` beside `Notes/_Schema/`
- **THEN** the page lands at `Notes/Schema/plan.md`

#### Scenario: A group never gains a third spelling

- **WHEN** `Notes/Garden-Tools/` holds three entries, `Notes/garden_tools/` holds one, and a write names `Notes/gardentools/plan.md`
- **THEN** the page lands at `Notes/Garden-Tools/plan.md`

### Requirement: A write keeps the caller's spelling where steering would be wrong

A file name SHALL keep the caller's spelling. A folder at the top of the vault or of the managed root, and a spelling that the layout declares or the project registry names, SHALL keep the caller's spelling. A move SHALL NOT treat the folder it vacates as a target.

#### Scenario: A file name is not steered

- **WHEN** `Notes/garden-tools.md` exists and a write names `Notes/gardentools.md`
- **THEN** the page lands at `Notes/gardentools.md`

#### Scenario: A move renames a folder to its own look-alike

- **WHEN** a move renames `Notes/garden_tools/` to `Notes/garden-tools/`
- **THEN** the folder is renamed

### Requirement: The preview merges separator variants like case splits

The `path-variants` preview SHALL group sibling folders by the folder key, so a group whose names differ only by case, Unicode form or separators is one split with one canonical spelling. The audit SHALL report such a group as a defect whose fix is the preview. A group equal only after NFKC folding and keeping letters and digits SHALL stay a candidate. Export SHALL keep refusing only on names equal after NFC and casefold.

#### Scenario: The preview lists a separator pair

- **WHEN** `Evidence/garden-tools/` holds three files and `Evidence/gardentools/` holds one
- **THEN** the preview lists one split with canonical spelling `garden-tools` and one move
- **AND** its apply moves the file into `Evidence/garden-tools/` and removes the emptied folder

#### Scenario: A looser look-alike stays a candidate

- **WHEN** a folder holds `C` and `C++`
- **THEN** the audit reports no defect for them
- **AND** the preview lists no split for them
