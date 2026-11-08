## ADDED Requirements

### Requirement: A vault has one managed root
A vault SHALL have one managed root: the folder that holds `_Schema/`. In `root` layout the managed root SHALL be the vault root. In `nested` layout it SHALL be one folder inside the vault. Exomem SHALL resolve the managed root once per vault. A requirement that names `Knowledge Base/<rest>` SHALL mean `<managed root>/<rest>`. An existing vault SHALL keep its layout until a relayout moves it, and an upgrade SHALL NOT move any file.

#### Scenario: A governed write in a root vault has no wrapper segment
- **WHEN** a governed write creates a compiled note in a vault whose layout is `root`
- **THEN** the page path starts with the note type's area name
- **AND** no path segment is the wrapper folder

#### Scenario: A nested vault keeps its layout on upgrade
- **WHEN** Exomem upgrades a vault whose layout is `nested`
- **THEN** every page keeps its path
- **AND** the vault stays `nested`

#### Scenario: A new vault starts in root
- **WHEN** `exomem init` creates a vault
- **THEN** the vault's layout is `root` and `_Schema/` sits at the vault root

### Requirement: Areas come from registries
Area folder names SHALL come from the note-type registry and the collection profiles, not from a list in code. A compiled type's `folder` SHALL be its area name. A note type with role `source`, `evidence` or `entity` SHALL declare its area through the same `folder` attribute. In `nested` layout a compiled area SHALL sit under the `Notes/` folder. In `root` layout no `Notes/` level SHALL sit above a compiled area.

#### Scenario: A vault-defined compiled type names its own area
- **WHEN** an owner saves a compiled note type with the folder `Playbooks`
- **THEN** a note of that type in a `root` vault is written to `Playbooks/<Name>.md`
- **AND** the same note in a `nested` vault is written to `Notes/Playbooks/<Name>.md`

#### Scenario: Initialisation creates the registered areas
- **WHEN** `exomem init` creates a `root` vault
- **THEN** it creates the area folders that the registries declare
- **AND** no area name comes from a list in code

### Requirement: Pages sit flat inside areas
In `root` layout, a page path SHALL hold an area, at most one registry-declared group folder and the name. A writer SHALL NOT add a project, domain or medium folder or any other taxonomy level. The group folders SHALL be `Sources/<Kind>/`, `Entities/<Type>/`, `Records/<collection>/` and `Planning/<collection>/`. `Evidence/<scope>/<category>/` SHALL remain the one area with two group folders.

#### Scenario: A partitioned type is written flat
- **WHEN** a note of a type that `nested` layout partitions by project is written to a `root` vault
- **THEN** the page path is `<Area>/<Name>.md`
- **AND** no project folder appears

#### Scenario: A source keeps its kind folder
- **WHEN** a classified source is written to a `root` vault
- **THEN** the page path is `Sources/<Kind>/<Name>.md`
- **AND** the domain is recorded in the page's metadata, not in a folder

#### Scenario: Evidence keeps its scope and category folders
- **WHEN** an artifact is preserved to a `root` vault
- **THEN** its path is `Evidence/<scope>/<category>/<original filename>`

### Requirement: Derived names are unique across the vault
A new derived page name SHALL be unique among the Markdown pages that Exomem owns, across the whole vault, compared after NFC normalisation and casefolding. On a collision the writer SHALL append ` (2)`, ` (3)` and so on, taking the lowest number that no page holds. The suffix SHALL count toward the filename length cap. A relayout SHALL resolve collisions in ascending order of the pages' current paths, so that the same plan input gives the same names.

#### Scenario: Two pages with one title get distinct names
- **WHEN** a second page is created with a title whose derived name an existing page already holds
- **THEN** the new page is named `<Name> (2).md`
- **AND** the existing page keeps its name

#### Scenario: Names that differ only by case collide
- **WHEN** a page is created whose derived name differs from an existing page's name only by case or Unicode form
- **THEN** the new page gets a numbered suffix
- **AND** no existing page's bytes change

#### Scenario: A relayout plan is repeatable
- **WHEN** `exomem store-mode relayout --plan` runs twice over the same pages
- **THEN** both runs assign the same name and suffix to every page

### Requirement: Links use the shortest form that resolves to one page
A writer SHALL emit `[[Name]]` when no other file that Obsidian sees has that name, and `[[Area/Name]]` otherwise. Graph rows SHALL keep the resolved vault path of the target. A link repoint SHALL change only the link's target text, and the repointed link SHALL resolve to the page it resolved to before.

#### Scenario: A unique name gives a bare link
- **WHEN** a governed write links to a page whose name no other file holds
- **THEN** the body carries `[[Name]]`
- **AND** the graph edge ends at the target's vault path

#### Scenario: A homonym outside the owned pages forces the path form
- **WHEN** a governed write links to a page and a file in `_trash` has the same name
- **THEN** the body carries `[[Area/Name]]`

### Requirement: A retitle renames a derived name
In store mode, when a page's title changes and its filename was derived from the old title, the same governed write SHALL rename the page to the name derived from the new title. One transaction SHALL move the page's rows, repoint every inbound link that uses the old name, re-derive the pages it repoints and record the old path as an alias. A filename that the caller pinned SHALL NOT change.

#### Scenario: A derived name follows the title
- **WHEN** a governed write changes the title of a page whose name was derived from its old title
- **THEN** the page's file is renamed in the same transaction
- **AND** every page that linked the old name now links the new name and still resolves to the page

#### Scenario: A pinned name stays
- **WHEN** a governed write changes the title of a page whose filename differs from the name derived from its old title
- **THEN** the page keeps its path

### Requirement: Old paths keep resolving
A path alias table SHALL map each old path of a page to its current path. Every move SHALL update the table in the same transaction. The resolver SHALL read it after exact paths and before stems. `store_pages.path` SHALL remain the store key, and the page's `exomem_id` and the store's identities table SHALL carry its move-safe identity.

#### Scenario: An old path still resolves after a move
- **WHEN** a page moves and a link or call names its old path
- **THEN** the link or call resolves to the page at its current path

#### Scenario: A reference survives a move
- **WHEN** a page moves or is retitled
- **THEN** its `exomem://memory/<uuid>` reference still names the same page

### Requirement: Relayout is one verified governed move
`exomem store-mode relayout` SHALL move a store-mode vault from `nested` to `root` in one store change that holds the page moves, the repointed pages, the aliases and the layout record. `--plan` SHALL compute every move, name, suffix, truncation and link repoint, and SHALL write nothing. `--apply` SHALL refuse unless store mode is active, no item is held, the projection queue is empty, a store backup exists and free disk space is at least the size of the store. `--verify` SHALL prove that every owned page maps to one new path with its `exomem_id` unchanged, that every link occurrence resolves to the same page as before, and that page bytes equal the old bytes apart from the planned link edits. The managed upgrade migrator SHALL NOT run a relayout.

#### Scenario: The plan writes nothing
- **WHEN** `--plan` runs on a store-mode vault
- **THEN** it reports counts of moves, suffixes, truncations and link repoints
- **AND** the store and the vault files are byte-identical before and after

#### Scenario: Every link resolves to the same page after the relayout
- **WHEN** `--verify` runs after `--apply`
- **THEN** each link occurrence resolves to the page it resolved to before
- **AND** each rewritten link passes Obsidian's basename-or-path rule

#### Scenario: Only link spans change in page bytes
- **WHEN** `--verify` compares each page after the relayout with its bytes before
- **THEN** the two differ only in the planned link spans
- **AND** every binary file keeps its content hash

#### Scenario: A held item refuses the relayout
- **WHEN** a held correction or held deletion exists and `--apply` runs
- **THEN** the relayout refuses and names the held items
- **AND** no page moves

#### Scenario: A busy projection queue refuses the relayout
- **WHEN** projection intents are still queued and `--apply` runs
- **THEN** the relayout refuses
- **AND** no page moves
