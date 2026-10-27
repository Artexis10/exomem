## ADDED Requirements

### Requirement: The vault holds no two siblings with one portable name

No vault folder SHALL hold two children whose names are equal after Unicode NFC normalisation and casefolding, on any host filesystem. The vault, not the host, owns path identity.

A writer that creates a path SHALL re-spell each missing component to an existing sibling with the same key before the destination binds, and an exact spelling SHALL win. `create_file`, `move_file` destinations, `create_directory`, note creation, evidence capture, client artifact destinations and project-key folder planning SHALL apply this rule.

A recorded identity SHALL keep the spelling that the host stores. Reads, deletes and index keys SHALL NOT be re-spelled, so each page of an existing split keeps its own identity.

The folder creator of the held filesystem SHALL refuse a new folder whose key matches an existing sibling with `PATH_VARIANT_EXISTS`, and the refusal SHALL name the existing spelling. A move whose re-spelled destination is its own source SHALL be refused with the same code, because only a move of the folder changes the folder's spelling.

A write into a folder that already has a variant sibling SHALL succeed at the requested spelling. `create_file`, `move_file`, `create_directory`, note creation and evidence capture SHALL then return a warning that names the existing spelling and the `path_variants` audit. A note or source destination that a domain projects SHALL keep the existing refusal `AMBIGUOUS_DOMAIN_DESTINATION` while two equivalent domain folders exist.

#### Scenario: A capture names a differently-cased scope on a case-sensitive volume

- **WHEN** `Evidence/Food/` exists and a capture names the scope `food`
- **THEN** the artifact lands in `Evidence/Food/`
- **AND** no `Evidence/food/` folder is created

#### Scenario: An explicit path names a differently-cased folder

- **WHEN** `Notes/Garden/` exists and `create_file` names `Notes/garden/plan.md`, or `move_file` names it as the destination
- **THEN** the page lands at `Notes/Garden/plan.md`
- **AND** no `Notes/garden/` folder is created

#### Scenario: A move names a variant of its own folder

- **WHEN** only `Notes/food/` exists and a caller moves `Notes/food/trial.md` to `Notes/Food/trial.md`
- **THEN** the move is refused with `PATH_VARIANT_EXISTS` naming `Notes/food`
- **AND** the page stays where it is

#### Scenario: A folder creation would add a variant

- **WHEN** a write bypasses re-spelling and asks to create a folder whose name matches an existing sibling by NFC and casefold
- **THEN** it is refused with `PATH_VARIANT_EXISTS` naming the existing spelling

#### Scenario: A split already exists

- **WHEN** a write targets a folder that already has a variant sibling
- **THEN** the write lands at the exact spelling requested
- **AND** the result warns about the split

#### Scenario: Deleting one side of a split keeps the other page's identity

- **WHEN** `Notes/Food/a.md` and `Notes/food/a.md` both exist and the second is deleted
- **THEN** the index removes only the deleted page's row
- **AND** `Notes/Food/a.md` keeps its row

#### Scenario: A project key reuses an existing folder

- **WHEN** a new project key derives a folder name that matches an existing research folder by NFC and casefold
- **THEN** the registration uses the existing spelling
- **AND** an explicit folder that collides with a different existing spelling is refused

### Requirement: A read-only audit lists path variants

`maintain_memory(mode="audit")` SHALL offer the opt-in category `path_variants`. It SHALL report sibling groups equal after NFC and casefold as defects, and groups equal only after NFKC folding and dropping separators as candidates. It SHALL NOT change the vault.

#### Scenario: The audit finds a case group and an NFC group

- **WHEN** a folder holds `Food` and `food`, and another holds a precomposed and a decomposed spelling of one name
- **THEN** the audit reports one defect group for each

### Requirement: Export reports a path collision without naming paths

When a vault cannot be exported because sibling paths collide after NFC and casefold, the export SHALL fail with the code `CASE_COLLISION`. The result SHALL carry no path, name or count. The cell manager SHALL record `CASE_COLLISION` as the export job's failure code. The owner lists the colliding paths with the `path_variants` audit in their own memory.

#### Scenario: Export of a vault with a split folder

- **WHEN** an export meets two colliding folder spellings
- **THEN** the result code is `CASE_COLLISION`
- **AND** the result contains no vault path
- **AND** the cell manager records `CASE_COLLISION` for the job
