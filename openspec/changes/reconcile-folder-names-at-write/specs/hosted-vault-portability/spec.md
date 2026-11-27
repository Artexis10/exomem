## ADDED Requirements

### Requirement: Folder identity is the folder key

The folder key SHALL be the name after NFC and casefold, with every Unicode space separator, dash and connector punctuation character removed. A leading run of connector punctuation SHALL stay, and a run between two digits SHALL become one `-`. The separator rule SHALL join sibling folders with one folder key below the vault top and the managed root, outside the Sources tree.

#### Scenario: Other punctuation keeps two names apart

- **WHEN** `Notes/C/` exists and a write names `Notes/C++/intro.md`
- **THEN** the page lands at `Notes/C++/intro.md`

#### Scenario: A leading underscore keeps two names apart

- **WHEN** `Notes/_Drafts/` exists and a write names `Notes/Drafts/plan.md`
- **THEN** the page lands at `Notes/Drafts/plan.md`

#### Scenario: A separator between two numbers keeps two folders apart

- **WHEN** `Notes/Course/Week 1-2/` exists and a write names `Notes/Course/Week 12/notes.md`
- **THEN** the page lands at `Notes/Course/Week 12/notes.md`

### Requirement: A new folder that differs from an existing one only by separators lands in it

When a governed write would create a folder where the separator rule applies, and a sibling folder with the same folder key exists, the write SHALL land in that folder, or in the canonical spelling of the group when several exist. File mode and store mode SHALL apply the same rule.

#### Scenario: A capture names a scope without its hyphen

- **WHEN** `Evidence/garden-tools/` exists and a capture names the scope `gardentools`
- **THEN** the artifact lands in `Evidence/garden-tools/`
- **AND** no `Evidence/gardentools/` folder is created
- **AND** the page's family tags name `garden-tools`

#### Scenario: A capture names a category with an underscore

- **WHEN** `Evidence/Garden/shared-docs/` exists and a capture names the category `shared_docs`
- **THEN** the artifact lands in `Evidence/Garden/shared-docs/`

#### Scenario: A capture names a scope with a project's folder spelling

- **WHEN** the project registry names the folder `Garden Tools`, `Evidence/garden-tools/` exists, and a capture names the scope `Garden Tools`
- **THEN** the artifact lands in `Evidence/garden-tools/`

#### Scenario: A note placement folder is steered

- **WHEN** `Notes/garden-tools/` exists and a note's placement folder is `Notes/garden tools`
- **THEN** the note lands in `Notes/garden-tools/`

#### Scenario: A group never gains a third spelling

- **WHEN** `Notes/garden_tools/` holds three entries, `Notes/Garden-Tools/` holds one, and a write names `Notes/gardentools/plan.md`
- **THEN** the page lands at `Notes/garden_tools/plan.md`

### Requirement: The separator rule keeps the caller's spelling where code reads a folder by its exact spelling

The separator rule SHALL NOT change a file name, a folder that the layout declares, or a project folder that the registry names in the research folder. These exceptions SHALL apply only to the separator rule. Case and Unicode-form steering SHALL stay as "The vault holds no two siblings with one portable name" requires.

#### Scenario: A file name is not steered

- **WHEN** `Notes/garden-tools.md` exists and a write names `Notes/gardentools.md`
- **THEN** the page lands at `Notes/gardentools.md`

#### Scenario: A top-level folder keeps its spelling

- **WHEN** the vault top holds `Shared-Docs/` and a write names `SharedDocs/plan.md`
- **THEN** the page lands at `SharedDocs/plan.md`

#### Scenario: A Sources folder keeps its taxonomy spelling

- **WHEN** `Sources/Field-Notes/` holds three pages, `Sources/Field Notes/` holds one, and a write names `Sources/Field Notes/new.md`
- **THEN** the page lands at `Sources/Field Notes/new.md`
- **AND** the preview lists no split for them

#### Scenario: A registered project folder keeps its spelling

- **WHEN** the project registry names the folder `Garden Tools`, the research folder holds `Garden-Tools/`, and a write names `Notes/Research/Garden Tools/plan.md`
- **THEN** the page lands at `Notes/Research/Garden Tools/plan.md`

### Requirement: The preview merges separator variants like case splits

A split SHALL be a group of sibling names equal after NFC and casefold, or, where the separator rule applies, a group of sibling folders equal under the folder key. The `path-variants` preview SHALL list both kinds, each with one canonical spelling. Export SHALL keep refusing only on names equal after NFC and casefold.

#### Scenario: The preview lists a separator pair

- **WHEN** `Evidence/garden-tools/` holds three files and `Evidence/gardentools/` holds one
- **THEN** the preview lists one split with canonical spelling `garden-tools` and one move
- **AND** its apply moves the file into `Evidence/garden-tools/` and removes the emptied folder

#### Scenario: Two pages that differ only by a hyphen are no split

- **WHEN** a folder holds the pages `garden-tools.md` and `gardentools.md`
- **THEN** the preview lists no split for them

## MODIFIED Requirements

### Requirement: A read-only audit lists path variants

`maintain_memory(mode="audit")` SHALL run the category `path_variants` by default, over the names an export keeps. Its defects SHALL be the sibling groups equal after NFC and casefold, and the folder groups that the separator rule joins. Its candidates SHALL be the groups equal only after NFKC folding with only letters and digits kept. A defect for a folder group SHALL name the `path-variants` preview as its fix. The audit SHALL NOT change the vault.

#### Scenario: The audit finds a case group and an NFC group

- **WHEN** a folder holds `Food` and `food`, and another holds a precomposed and a decomposed spelling of one name
- **THEN** the audit reports one defect group for each

#### Scenario: A default audit shows a split

- **WHEN** an owner runs `maintain_memory(mode="audit")` without naming categories, and a folder holds `Food` and `food`
- **THEN** the report holds a `path_variants` defect for that group
- **AND** the finding names the `path-variants` preview

#### Scenario: The audit reports a separator pair

- **WHEN** a folder holds the folders `garden-tools` and `gardentools`
- **THEN** the audit reports one defect group for them
- **AND** the finding names the `path-variants` preview

#### Scenario: A looser look-alike stays a candidate

- **WHEN** a folder holds the folders `C` and `C++`
- **THEN** the audit reports them as a candidate, not a defect
- **AND** the preview lists no split for them

### Requirement: A write into a split folder lands under its canonical spelling

When a destination passes through, or ends at, a folder that has a variant sibling (a split), the write SHALL land under the split's canonical spelling, whichever spelling the caller named. A variant sibling is a name equal after NFC and casefold, or, where the separator rule applies, a folder equal under the folder key. A destination whose path already exists as a file at the requested spelling SHALL keep it. The write SHALL still warn about the split and name the `path_variants` audit.

#### Scenario: A capture names the smaller side of a split

- **WHEN** `Evidence/Garden/` holds three files, `Evidence/garden/` holds one, and a capture names the scope `garden`
- **THEN** the artifact lands in `Evidence/Garden/`
- **AND** the result warns about the split

#### Scenario: A capture names the smaller side of a split category folder

- **WHEN** `Evidence/Garden/Shared/` holds three files, `Evidence/Garden/shared/` holds one, and a capture names the category `shared`
- **THEN** the artifact lands in `Evidence/Garden/Shared/`

#### Scenario: A write names the smaller side of a separator pair

- **WHEN** `Notes/garden-tools/` holds three pages, `Notes/gardentools/` holds one, and a write names `Notes/gardentools/plan.md`
- **THEN** the page lands at `Notes/garden-tools/plan.md`
- **AND** the result warns about the split

#### Scenario: An existing path keeps its spelling

- **WHEN** a split exists and the destination file already exists under the non-canonical spelling
- **THEN** the destination keeps the requested spelling
