## ADDED Requirements

### Requirement: The vault holds no two siblings with one portable name

No vault folder SHALL hold two children whose names are equal after Unicode NFC normalisation and casefolding, on any host filesystem. The vault, not the host, owns path identity. Path resolution SHALL re-spell a path to the existing sibling with the same key, and an exact spelling match SHALL win. A write that would create a folder whose key matches an existing sibling SHALL be refused with `PATH_VARIANT_EXISTS` naming the existing spelling. A write into a folder that already has a variant sibling SHALL succeed at the requested spelling and carry a warning.

#### Scenario: A capture names a differently-cased scope on a case-sensitive volume

- **WHEN** `Evidence/Food/` exists and a capture names the scope `food`
- **THEN** the artifact lands in `Evidence/Food/`
- **AND** no `Evidence/food/` folder is created

#### Scenario: A folder creation would add a variant

- **WHEN** a write bypasses re-spelling and asks to create a folder whose name matches an existing sibling by NFC and casefold
- **THEN** it is refused with `PATH_VARIANT_EXISTS` naming the existing spelling

#### Scenario: A split already exists

- **WHEN** a write targets a folder that already has a variant sibling
- **THEN** the write lands at the exact spelling requested
- **AND** the result warns about the split

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

When a vault cannot be exported because sibling paths collide after NFC and casefold, the export SHALL fail with the code `CASE_COLLISION` and a count of colliding groups. The result SHALL carry no path or name, and its reason SHALL tell the owner to run the `path_variants` audit in their own memory.

#### Scenario: Export of a vault with a split folder

- **WHEN** an export meets two colliding folder spellings
- **THEN** the result code is `CASE_COLLISION` with a group count
- **AND** the result contains no vault path
