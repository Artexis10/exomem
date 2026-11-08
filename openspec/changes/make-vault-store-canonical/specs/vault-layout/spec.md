## ADDED Requirements

### Requirement: A vault has one managed root
A vault SHALL have one managed root: the folder that holds `_Schema/`. In `root` layout the managed root SHALL be the vault root. In `nested` layout it SHALL be one folder inside the vault. Exomem SHALL resolve the managed root once per vault. A requirement that names `Knowledge Base/<rest>` SHALL mean `<managed root>/<rest>`, except that in `root` layout `Knowledge Base/Notes/<Area>/<rest>` SHALL mean `<Area>/<rest>` for a compiled area `<Area>`, because the `Notes/` level drops. When both the vault root and a wrapper folder hold `_Schema/`, resolution SHALL refuse with an error that names both folders. An existing vault SHALL keep its layout until a relayout moves it, and an upgrade SHALL NOT move any file.

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

#### Scenario: A nested path maps to a root path
- **WHEN** a requirement names `Knowledge Base/Notes/Insights/<Name>.md` and the vault's layout is `root`
- **THEN** it means `Insights/<Name>.md`

#### Scenario: Two managed roots are refused
- **WHEN** both the vault root and a wrapper folder hold `_Schema/`
- **THEN** resolution refuses and the error names both folders
- **AND** no file changes

### Requirement: Areas come from registries
Area folder names SHALL come from the note-type registry and the collection profiles, not from a list in code. A compiled type's `folder` SHALL be its area name. A note type with role `source`, `evidence` or `entity` SHALL declare its area through the same `folder` attribute. In `nested` layout a compiled area SHALL sit under the `Notes/` folder. In `root` layout no `Notes/` level SHALL sit above a compiled area. A `folder` value that starts with `Notes/` SHALL be read as the `nested` parent plus the area name.

#### Scenario: A vault-defined compiled type names its own area
- **WHEN** an owner saves a compiled note type with the folder `Playbooks`
- **THEN** a note of that type in a `root` vault is written to `Playbooks/<Name>.md`
- **AND** the same note in a `nested` vault is written to `Notes/Playbooks/<Name>.md`

#### Scenario: A folder value with the nested parent still names its area
- **WHEN** a `root` vault's note-type overlay holds a compiled type with the folder `Notes/Playbooks`
- **THEN** a note of that type is written to `Playbooks/<Name>.md`

#### Scenario: Initialisation creates a vault-defined area
- **WHEN** `exomem init` runs for a `root` vault that already defines a compiled type with the folder `Playbooks`
- **THEN** `Playbooks/` exists at the vault root

### Requirement: Pages sit flat inside areas
In `root` layout, a page path SHALL hold an area, the group folders that the page's role or collection profile declares, and the name. A page of role `source` SHALL group by kind, of role `entity` by entity type, and of role `evidence` by scope and then category. A collection's pages SHALL sit in its profile's area, grouped by collection. A page of any other role SHALL have no group folder. A writer SHALL NOT add a project, domain or medium folder or any other taxonomy level.

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

#### Scenario: A domain-only reclassification moves no file
- **WHEN** a source in a `root` vault is reclassified to another domain and keeps its kind
- **THEN** the source's path does not change
- **AND** its metadata records the new domain

### Requirement: Derived names are unique across the vault
A new page name derived under `title` style in a `root` vault SHALL be unique among the Markdown pages that Exomem owns, across the whole vault, compared after NFC normalisation and casefolding. On a collision the writer SHALL append ` (2)`, ` (3)` and so on, taking the lowest number that no page holds. The suffix SHALL count toward the filename length cap. A name that is not derived SHALL never be renamed or suffixed: that covers an Evidence original, a Records or Planning recipe name and a name the caller pinned. A derived name SHALL yield to such a name whatever the path order. A relayout SHALL resolve collisions among derived names in ascending order of the pages' current paths, so that the same plan input gives the same names. A slug name, and a `title` name in a `nested` vault, SHALL keep the collision contract of `add-readable-note-filenames`.

#### Scenario: Two pages with one title get distinct names
- **WHEN** a second page is created in a `root` vault with a title whose derived name an existing page already holds
- **THEN** the new page is named `<Name> (2).md`
- **AND** the existing page keeps its name

#### Scenario: Names that differ only by case collide
- **WHEN** a page is created in a `root` vault whose derived name differs from an existing page's name only by case or Unicode form
- **THEN** the new page gets a numbered suffix
- **AND** no existing page's bytes change

#### Scenario: A derived name yields to an original
- **WHEN** a relayout plans a derived name that an Evidence original already holds, and the derived page's path sorts before the original's
- **THEN** the derived page takes the suffix
- **AND** the Evidence original keeps its filename

#### Scenario: A relayout plan is repeatable
- **WHEN** `exomem store-mode relayout --plan` runs twice over the same pages
- **THEN** both runs assign the same name and suffix to every page

#### Scenario: A slug name in a nested vault keeps its contract
- **WHEN** a page with a colliding slug name is created in a `nested` vault
- **THEN** the writer disambiguates it as `add-readable-note-filenames` defines

### Requirement: Links use the shortest trailing path that resolves to one file
A writer SHALL emit the shortest trailing path of the target page, cut at a folder boundary, that resolves to exactly one file for both Exomem and Obsidian. That path SHALL be `[[Name]]` when no other file that Obsidian sees has that name. Graph rows SHALL keep the resolved vault path of the target. A link repoint SHALL change only the link's target text, and the repointed link SHALL resolve to the page it resolved to before.

#### Scenario: A unique name gives a bare link
- **WHEN** a governed write links to a page whose name no other file holds
- **THEN** the body carries `[[Name]]`
- **AND** the graph edge ends at the target's vault path

#### Scenario: A homonym outside the owned pages forces a longer path
- **WHEN** a governed write links to `Insights/<Name>.md` and a file in `_trash` has the same name
- **THEN** the body carries `[[Insights/<Name>]]`

#### Scenario: A grouped page keeps a real path suffix
- **WHEN** a governed write links to `Evidence/alpha/receipts/Scan.md` and `Evidence/beta/receipts/Scan.md` also exists
- **THEN** the body carries `[[alpha/receipts/Scan]]`
- **AND** the body does not carry `[[Evidence/Scan]]`, which is not a path suffix of either page

### Requirement: A retitle renames a derived name
In store mode, when a governed write changes a page's title and its filename was derived from the old title, the same write SHALL rename the page to the name derived from the new title. A filename is derived from the old title when it equals what the naming rule produces for that title, including any suffix or truncation. One transaction SHALL move the page's rows, repoint every inbound link that uses the old name, re-derive the pages it repoints and record the old path as an alias. A filename that is not derived from the old title, including one the caller pinned, SHALL NOT change. An outside-edit import that changes a title SHALL NOT rename the file.

#### Scenario: A derived name follows the title
- **WHEN** a governed write changes the title of a page whose name was derived from its old title
- **THEN** the page's file is renamed in the same transaction
- **AND** every page that linked the old name now links the new name and still resolves to the page

#### Scenario: A pinned name stays
- **WHEN** a governed write changes the title of a page whose filename differs from the name derived from its old title
- **THEN** the page keeps its path

#### Scenario: A suffixed derived name follows the title
- **WHEN** a governed write changes the title of a page whose name is the derived name plus ` (2)`
- **THEN** the page is renamed to the name derived from the new title, with a suffix only if that name collides

#### Scenario: An outside edit to a title keeps the file name
- **WHEN** a person edits a page's title in an editor and the edit imports as a governed write with outside-editor provenance
- **THEN** the page keeps its path

### Requirement: Old paths keep resolving
In store mode, a path alias table SHALL map each old path of a page to its current path. Every move SHALL update the table in the same transaction. The resolver SHALL read it after exact paths and before stems. `store_pages.path` SHALL remain the store key, and the page's `exomem_id` and the store's identities table SHALL carry its move-safe identity.

#### Scenario: An old path still resolves after a move
- **WHEN** a page moves in a store-mode vault and a link or call names its old path
- **THEN** the link or call resolves to the page at its current path

#### Scenario: A reference survives a move
- **WHEN** a page moves or is retitled in a store-mode vault
- **THEN** its `exomem://memory/<uuid>` reference still names the same page

### Requirement: Relayout is one verified governed move
`exomem store-mode relayout` SHALL move a store-mode vault from `nested` to `root` in one store change that holds the page moves, the repointed pages, the aliases, the layout record and the rewrite of each note-type overlay `folder` value that starts with `Notes/` to its bare area name. `--plan` SHALL compute every move, name, suffix, truncation and link repoint, and SHALL write nothing. `--apply` SHALL refuse unless store mode is active, the service is stopped, no item is held, the projection queue is empty, a store backup exists and free disk space is at least the size of the store. `--verify` SHALL prove that every owned page maps to one new path with its `exomem_id` unchanged, that every link occurrence resolves to the same page as before, that page bytes equal the old bytes apart from the planned link edits, and that a fresh derivation equals the stored rows. The managed upgrade migrator SHALL NOT run a relayout.

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

#### Scenario: A running service refuses the relayout
- **WHEN** the service is running and `--apply` runs
- **THEN** the relayout refuses
- **AND** no page moves

#### Scenario: Inactive store mode refuses the relayout
- **WHEN** the vault is in file mode and `--apply` runs
- **THEN** the relayout refuses
- **AND** no page moves

#### Scenario: A missing backup refuses the relayout
- **WHEN** no store backup exists and `--apply` runs
- **THEN** the relayout refuses
- **AND** no page moves

#### Scenario: Too little free disk refuses the relayout
- **WHEN** free disk space is smaller than the size of the store and `--apply` runs
- **THEN** the relayout refuses
- **AND** no page moves

### Requirement: Relayout places every page by rule
The relayout SHALL keep a page in an exempt or unregistered folder at its path relative to the managed root, so that only the wrapper segment drops. It SHALL flatten a partition folder below the group level into the group folder, or into the area when the area has no group folder. It SHALL keep every name that is not derived. `--plan` SHALL list each Markdown file at the vault root outside the old wrapper that becomes owned when the managed root becomes the vault root. `--apply` SHALL refuse and name any page that the plan cannot map to exactly one new path.

#### Scenario: The plan lists pages that become owned
- **WHEN** `--plan` runs on a vault with Markdown files at the vault root outside the old wrapper
- **THEN** the plan lists each of those files as becoming owned
- **AND** the plan writes nothing

#### Scenario: Pages outside the placement rules keep their folder
- **WHEN** a relayout plans a page in an exempt folder and a page with a partition folder below its group
- **THEN** the exempt page keeps its path relative to the managed root
- **AND** the partitioned page moves into its group folder

#### Scenario: A page the plan cannot map refuses the relayout
- **WHEN** the plan holds a page that it cannot map to exactly one new path, such as two non-derived names that would meet at one path, and `--apply` runs
- **THEN** the relayout refuses and names that page
- **AND** no page moves
