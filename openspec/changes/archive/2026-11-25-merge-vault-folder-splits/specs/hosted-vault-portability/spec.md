## MODIFIED Requirements

### Requirement: The vault holds no two siblings with one portable name

No vault folder SHALL hold two children whose names are equal after Unicode NFC normalisation and casefolding, on any host filesystem. The vault, not the host, owns path identity.

A writer that creates a path SHALL re-spell each missing component to an existing sibling with the same key before the destination binds, and an exact spelling SHALL win. `create_file`, `move_file` destinations, `create_directory`, note creation, evidence capture, client artifact destinations and project-key folder planning SHALL apply this rule.

A recorded identity SHALL keep the spelling that the host stores. Reads, deletes and index keys SHALL NOT be re-spelled, so each page of an existing split keeps its own identity.

The folder creator of the held filesystem SHALL refuse a new folder whose key matches an existing sibling with `PATH_VARIANT_EXISTS`, and the refusal SHALL name the existing spelling. A move whose re-spelled destination is its own source SHALL be refused with the same code, because only a move of the folder changes the folder's spelling.

A write into a folder that already has a variant sibling SHALL follow the requirement "A write into a split folder lands under its canonical spelling". A note or source destination that a domain projects SHALL keep the existing refusal `AMBIGUOUS_DOMAIN_DESTINATION` while two equivalent domain folders exist.

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
- **THEN** the write lands under the split's canonical spelling
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

`maintain_memory(mode="audit")` SHALL run the category `path_variants` by default. It SHALL report sibling groups equal after NFC and casefold as defects, and groups equal only after NFKC folding and dropping separators as candidates. A defect finding for a folder group SHALL name the `path-variants` preview as its fix. The audit SHALL NOT change the vault.

#### Scenario: The audit finds a case group and an NFC group

- **WHEN** a folder holds `Food` and `food`, and another holds a precomposed and a decomposed spelling of one name
- **THEN** the audit reports one defect group for each

#### Scenario: A default audit shows a split

- **WHEN** an owner runs `maintain_memory(mode="audit")` without naming categories, and a folder holds `Food` and `food`
- **THEN** the report holds a `path_variants` defect for that group
- **AND** the finding names the `path-variants` preview

## ADDED Requirements

### Requirement: A write into a split folder lands under its canonical spelling

When a destination passes through a folder that has a variant sibling (a split), the write SHALL land under the split's canonical spelling, whichever spelling the caller named. A destination whose path already exists at the requested spelling SHALL keep it. The write SHALL still warn about the split and name the `path_variants` audit.

#### Scenario: A capture names the smaller side of a split

- **WHEN** `Evidence/Garden/` holds three files, `Evidence/garden/` holds one, and a capture names the scope `garden`
- **THEN** the artifact lands in `Evidence/Garden/`
- **AND** the result warns about the split

#### Scenario: An existing path keeps its spelling

- **WHEN** a split exists and the destination path already exists under the non-canonical spelling
- **THEN** the destination keeps the requested spelling

### Requirement: A split's canonical spelling comes from data

The canonical spelling of a split SHALL be the spelling that the project registry names as a project folder; otherwise the spelling whose folder holds the most direct entries; otherwise the first spelling in code-point order. Writers and the merge SHALL use this one rule.

#### Scenario: The registry names the canonical spelling

- **WHEN** the project registry names the folder `Garden`, `Evidence/garden/` holds more files than `Evidence/Garden/`, and a write names `Evidence/garden/new.png`
- **THEN** the file lands at `Evidence/Garden/new.png`

### Requirement: A preview lists every folder split

`maintain_memory(mode="path-variants")` SHALL list, without changing the vault, every folder split with its canonical spelling, the files still under each other spelling, the variant folders that hold no entry, and a `plan_id`. A split of two files SHALL be listed as not mergeable.

#### Scenario: A split is previewed

- **WHEN** `Evidence/Garden/` holds three files and `Evidence/garden/` holds one
- **THEN** the preview names `Evidence/Garden` as canonical and one file to move
- **AND** the vault is unchanged

### Requirement: The preview's apply moves a batch into the canonical spelling

`maintain_memory(mode="path-variants", apply=true, plan_id, why)` SHALL move the batch that the preview with that `plan_id` listed, from the other spellings into the canonical spelling within their own tree, through the ordinary file move with link rewrites. The merge SHALL be one-way: each move keeps the file's bytes, rewrites the links to it, and records its old path in the moved page's lineage, and no operation moves the files back. A file whose move or link rewrite would be refused SHALL be listed as blocked, not moved.

#### Scenario: A split is merged

- **WHEN** `Evidence/Garden/` and `Evidence/garden/` each hold files, a page links to a file under `Evidence/garden/`, and the owner applies the preview's `plan_id`
- **THEN** every file is under `Evidence/Garden/`
- **AND** the link names the moved file
- **AND** the next preview lists `Evidence/garden/` as an empty variant folder

### Requirement: The preview's apply removes empty variant folders

`maintain_memory(mode="path-variants", apply=true, plan_id, why)` SHALL remove exactly the empty variant folders that the preview with that `plan_id` listed, also inside an append-only tree, and record each removal in the vault log. When a listed folder gained an entry or lost its variant sibling, the apply SHALL refuse as stale and remove nothing.

#### Scenario: Empty variant folders are removed

- **WHEN** the preview lists `Evidence/garden/` as empty and the owner applies its `plan_id`
- **THEN** `Evidence/garden/` no longer exists
- **AND** an export of the vault passes the case collision check

#### Scenario: A folder gained a file after the preview

- **WHEN** a file is written into a listed empty variant folder before the apply
- **THEN** the apply refuses as stale and removes nothing
