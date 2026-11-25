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

`maintain_memory(mode="audit")` SHALL run the category `path_variants` by default, over the names an export keeps. It SHALL report sibling groups equal after NFC and casefold as defects, and groups equal only after NFKC folding and dropping separators as candidates. A defect finding for a folder group SHALL name the `path-variants` preview as its fix. The audit SHALL NOT change the vault.

#### Scenario: The audit finds a case group and an NFC group

- **WHEN** a folder holds `Food` and `food`, and another holds a precomposed and a decomposed spelling of one name
- **THEN** the audit reports one defect group for each

#### Scenario: A default audit shows a split

- **WHEN** an owner runs `maintain_memory(mode="audit")` without naming categories, and a folder holds `Food` and `food`
- **THEN** the report holds a `path_variants` defect for that group
- **AND** the finding names the `path-variants` preview

## ADDED Requirements

### Requirement: A write into a split folder lands under its canonical spelling

When a destination passes through, or ends at, a folder that has a variant sibling (a split), the write SHALL land under the split's canonical spelling, whichever spelling the caller named. A destination whose path already exists as a file at the requested spelling SHALL keep it. The write SHALL still warn about the split and name the `path_variants` audit.

#### Scenario: A capture names the smaller side of a split

- **WHEN** `Evidence/Garden/` holds three files, `Evidence/garden/` holds one, and a capture names the scope `garden`
- **THEN** the artifact lands in `Evidence/Garden/`
- **AND** the result warns about the split

#### Scenario: A capture names the smaller side of a split category folder

- **WHEN** `Evidence/Garden/Shared/` holds three files, `Evidence/Garden/shared/` holds one, and a capture names the category `shared`
- **THEN** the artifact lands in `Evidence/Garden/Shared/`

#### Scenario: An existing path keeps its spelling

- **WHEN** a split exists and the destination file already exists under the non-canonical spelling
- **THEN** the destination keeps the requested spelling

### Requirement: A split's canonical spelling comes from data

The canonical spelling of a split SHALL be, in order: the spelling in NFC form; a spelling the vault's layout declares (the managed root, the scaffold's folders, a folder a registry gives a type or placement) or the project registry names; the folder with the most direct entries; the first in code-point order. At the top of the vault or of the managed root, a split with no declared spelling SHALL have none. Writers and the merge SHALL use this one rule.

#### Scenario: The registry names the canonical spelling

- **WHEN** the project registry names the folder `Garden`, `Evidence/garden/` holds more files than `Evidence/Garden/`, and a write names `Evidence/garden/new.png`
- **THEN** the file lands at `Evidence/Garden/new.png`

#### Scenario: A stray folder does not take a declared folder's writes

- **WHEN** a stray `notes/` holds more entries than the layout's `Notes/`, and a write names `notes/Research/idea.md`
- **THEN** the page lands at `Notes/Research/idea.md`

#### Scenario: The NFC spelling wins a Unicode split

- **WHEN** a folder's decomposed spelling holds more entries than its precomposed spelling
- **THEN** a write into either spelling lands under the precomposed one

### Requirement: A preview lists every folder split

`maintain_memory(mode="path-variants")`, a dry run by default, SHALL list without changing the vault every split among the names an export keeps, with its canonical spelling, every move into it, each file it can tell will not move and why, the other spellings it removes once empty, and a `plan_id` that binds the moves and those folders. A split with a file among its names, outside the managed root, or without a canonical spelling SHALL be listed as not mergeable.

#### Scenario: A split is previewed

- **WHEN** `Evidence/Garden/` holds three files and `Evidence/garden/` holds one
- **THEN** the preview names `Evidence/Garden` as canonical and one move
- **AND** the vault is unchanged

### Requirement: The preview's apply merges every split into its canonical spelling

`maintain_memory(mode="path-variants", dry_run=false, plan_id, why)` SHALL move every file the preview with that `plan_id` listed into the canonical spelling within its own tree, through the ordinary file move with link rewrites. A plan that changed since the preview SHALL be refused before anything moves. A move the file move refuses SHALL be skipped and listed with its reason.

#### Scenario: A split is merged

- **WHEN** `Evidence/Garden/` and `Evidence/garden/` each hold files, a page links to a file under `Evidence/garden/`, and the owner applies the preview's `plan_id`
- **THEN** every file is under `Evidence/Garden/`
- **AND** the link names the moved file

#### Scenario: A file arrives after the preview

- **WHEN** a file is written under a listed spelling before the apply
- **THEN** the apply refuses the stale plan and changes nothing

### Requirement: A remote merge runs as a maintenance job

A remote owner's preview and apply SHALL each run as a maintenance job. The apply job SHALL report its progress and stop between moves when cancelled. Each job's receipt SHALL list every row of its plan, and every move the file move refused with its reason.

#### Scenario: A remote owner merges as a job

- **WHEN** the owner previews and applies over a remote surface
- **THEN** each call returns a maintenance job
- **AND** the apply job's receipt lists each moved file and each refused move with its reason

### Requirement: A split merge is one-way

Each move of a split merge SHALL keep the file's bytes, rewrite the links to it, and record its old path in the moved page's lineage. No operation SHALL move the files back, and the merge's guidance SHALL say so before an owner applies it.

#### Scenario: A merged file keeps its bytes and names its old path

- **WHEN** the owner applies a plan that moves `Evidence/garden/seed.txt` into `Evidence/Garden/`
- **THEN** `Evidence/Garden/seed.txt` holds the same bytes
- **AND** its page's lineage names the page's old path under `Evidence/garden/`

### Requirement: The preview's apply removes emptied variant folders

After its moves, the apply SHALL remove each other spelling that the plan listed, holds no file, is writable and is still a split, also inside an append-only tree, and record the removal in the vault log. A spelling that still holds a file SHALL be kept and listed.

#### Scenario: Emptied variant folders are removed

- **WHEN** the owner applies a plan whose moves empty `Evidence/garden/`
- **THEN** `Evidence/garden/` no longer exists
- **AND** an export of the vault passes the case collision check

#### Scenario: A file that cannot move keeps its folder

- **WHEN** one file under `Evidence/garden/` cannot move
- **THEN** `Evidence/garden/` is kept and listed with its reason
