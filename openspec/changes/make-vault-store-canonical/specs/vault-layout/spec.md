## ADDED Requirements

### Requirement: A vault has one managed root
A vault SHALL have one managed root: the folder that holds `_Schema/`. In `root` layout the managed root SHALL be the vault root. In `nested` layout it SHALL be one folder inside the vault. Exomem SHALL resolve the managed root once per vault. A requirement that names `Knowledge Base/<rest>` SHALL mean `<managed root>/<rest>`, except that in `root` layout `Knowledge Base/Notes/<Area>/<rest>` SHALL mean `<Area>/<rest>` for a compiled area `<Area>`, because the `Notes/` level drops. When the store records a layout, that layout SHALL decide the managed root, and a leftover second `_Schema/` folder SHALL be reported by status and doctor, never refused. When no layout is recorded and both the vault root and a wrapper folder hold `_Schema/`, resolution SHALL refuse with an error that names both folders. An existing vault SHALL keep its layout until a relayout moves it, and an upgrade SHALL NOT move any file.

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

#### Scenario: A recorded layout decides between two roots
- **WHEN** the store records layout `root` and both the vault root and a wrapper folder hold `_Schema/`, as during the relayout's projection window
- **THEN** the vault root is the managed root
- **AND** status and doctor report the leftover `_Schema/` folder, and nothing refuses

#### Scenario: Two roots with no recorded layout are refused
- **WHEN** the store records no layout and both the vault root and a wrapper folder hold `_Schema/`
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
A new page name derived under `title` style in a `root` vault SHALL be unique among the Markdown pages that Exomem owns, across the whole vault, compared after NFC normalisation and casefolding. On a collision the writer SHALL append ` (2)`, ` (3)` and so on, taking the lowest number that no page holds. The suffix SHALL count toward the filename length cap. In a `root` vault, a pinned name SHALL never be suffixed, and a derived name SHALL yield to pinned and fixed names whatever the path order. A relayout SHALL resolve collisions among derived names in ascending order of the pages' current paths, so that the same plan input gives the same names. A slug name, and a `title` name in a `nested` vault, SHALL keep the collision contract of `add-readable-note-filenames`.

#### Scenario: Two pages with one title get distinct names
- **WHEN** a second page is created in a `root` vault with a title whose derived name an existing page already holds
- **THEN** the new page is named `<Name> (2).md`
- **AND** the existing page keeps its name

#### Scenario: Names that differ only by case collide
- **WHEN** a page is created in a `root` vault whose derived name differs from an existing page's name only by case or Unicode form
- **THEN** the new page gets a numbered suffix
- **AND** no existing page's bytes change

#### Scenario: A restore into a held name takes a suffix
- **WHEN** a page is restored from the trash and another page now holds its former name
- **THEN** the restored page takes a suffix by the collision rule above
- **AND** the page that holds the name keeps it

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

### Requirement: A name that a caller chooses is pinned
In store mode, a name SHALL be pinned when a caller explicitly chooses it for a page of a title-named type at create, move or rename time, and the store SHALL record the pin with the page, in either layout. An editor rename that an outside-edit import reads as a governed move counts as a chosen name. A move that keeps the page's basename, such as a reclassification or a Source-to-Evidence promotion, SHALL NOT pin the name.

A name SHALL be derived when the page's note type names its files from the page's frontmatter `title`, as a title stem with or without a date prefix, and no caller pinned it. `title` means the frontmatter value only: an H1 or a filename stem SHALL NOT stand in for it, and a page with no frontmatter title SHALL keep its name. Every other name SHALL be fixed: an Evidence original, a Records or Planning recipe name, a generated navigation page (`index.md`, `log.md`, `hub.md`), and any page whose type has no title rule or that sits outside every registry area. The class SHALL follow the page's type at write time, so a page whose type is later registered with a title rule takes its title form on its next governed write in a `root` vault.

In a `root` vault, a write that chooses a name another page holds SHALL be refused and name that page. The refusal SHALL NOT apply to a move that keeps its basename, because Evidence names may collide by design, or to an outside-edit import, because the file already exists. A fixed name SHALL never be renamed after creation. A new write whose fixed name collides inside one folder SHALL keep today's disambiguation. A `nested` vault SHALL keep the suffixing of an explicit name that `add-readable-note-filenames` defines.

#### Scenario: A chosen name is pinned
- **WHEN** a caller creates a page with an explicit filename in a store-mode vault
- **THEN** the store records the pin with the page

#### Scenario: An editor rename pins the new name
- **WHEN** a person renames a page file in an editor and the outside-edit import reads the rename as a governed move
- **THEN** the store records the new name as pinned

#### Scenario: A colliding chosen name is refused in a root vault
- **WHEN** a caller chooses a name that another page holds in a `root` vault
- **THEN** the write is refused and the error names that page
- **AND** no page changes

#### Scenario: A name is fixed when the type has no title rule
- **WHEN** a governed write creates an Evidence page or a generated navigation page
- **THEN** its name is fixed and no later governed write renames it

#### Scenario: A page without a frontmatter title keeps its name
- **WHEN** a governed write edits a page in a `root` vault whose frontmatter has no `title`, though its body starts with a heading
- **THEN** the page keeps its path

#### Scenario: A move that keeps its basename is neither pinned nor refused
- **WHEN** a source is promoted to Evidence in a `root` vault and another Evidence page already holds the same basename
- **THEN** the move proceeds
- **AND** the store records no pin

#### Scenario: A nested vault suffixes a chosen name
- **WHEN** a caller chooses a name that another page holds in a `nested` vault
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

### Requirement: A governed write in a root vault renames a derived name to match the title
In a `root` vault in store mode whose filename style is `title`, a governed write to a page SHALL rename the page when its name is derived and does not match the page's frontmatter title after the write. A name matches when it equals the name that the naming rule gives that title under the `title` style or the `slug` style, so a name derived under another style counts as matching and two writers with different settings never flip a name. The date prefix of a `dated` or `started` stem SHALL come from the page's recorded date, which is its `started` value for a `started` stem and, for a `dated` stem, its frontmatter `created` value, falling back to the page's first store audit row. It SHALL never come from the date of the write. The rename SHALL happen in the same transaction as the page's own change, and in the collision check the page's own current name SHALL count as free. One transaction SHALL move the page's rows, repoint every inbound link that uses the old name, re-derive the pages it repoints and record the old path as an alias. A pinned name and a fixed name SHALL NOT change. An outside-edit import SHALL NOT rename a file, even when it changes the title, and the next governed write to that page SHALL rename a derived name to match the page's current title. In a `nested` vault, in either mode, a governed write SHALL NOT rename an existing file. A change of filename style SHALL rename nothing in either layout. Only the relayout gives an existing derived name the form of the vault's resolved filename style.

#### Scenario: A derived name follows the title
- **WHEN** a governed write changes the title of a page with a derived name in a `root` vault in store mode
- **THEN** the page's file is renamed in the same transaction to the name derived from the new title
- **AND** every page that linked the old name now links the new name and still resolves to the page

#### Scenario: A suffixed derived name still follows the title
- **WHEN** a governed write changes the title of a page in a `root` vault in store mode whose derived name carries a ` (2)` suffix
- **THEN** the page is renamed to the name derived from the new title, with a suffix only if that name collides

#### Scenario: A pinned name never renames
- **WHEN** a caller created a page with an explicit filename in a `root` vault and a governed write later changes its title
- **THEN** the page keeps its path
- **AND** the store still records the pin

#### Scenario: An outside edit to a title keeps the file name
- **WHEN** a person edits a page's title in an editor and the outside-edit import records the edit as a governed write with outside-editor provenance
- **THEN** the page keeps its path

#### Scenario: The next governed write renames to match the outside edit
- **WHEN** a governed write touches a page with a derived name in a `root` vault in store mode after an outside edit changed its title
- **THEN** the page is renamed to the name derived from its current title
- **AND** every page that linked the old name now links the new name

#### Scenario: A dated page edited a month later keeps its path
- **WHEN** a governed write edits the body of a page with a `dated` stem and a derived name in a later month than its creation, without changing its title
- **THEN** the page keeps its path
- **AND** no link is repointed

#### Scenario: A retitle keeps the recorded date prefix
- **WHEN** a governed write changes the title of a page with a `dated` stem in a later month than its creation
- **THEN** the new name carries the page's recorded creation date as its prefix, not the date of the write

#### Scenario: A suffixed derived name returns to its plain name
- **WHEN** a governed write touches a page in a `root` vault in store mode whose derived name carries a ` (2)` suffix and the plain name is now free
- **THEN** the page is renamed to the plain name
- **AND** every page that linked the old name now links the plain name

#### Scenario: A matching name stays
- **WHEN** a governed write changes neither the title nor the name of a page whose derived name already matches its title, with or without a suffix that its own collision requires
- **THEN** the page keeps its path
- **AND** no link is repointed

#### Scenario: A nested vault never renames on a write
- **WHEN** a governed write edits a hand-named page in a `nested` store-mode vault, with or without a title change
- **THEN** its file name is unchanged

#### Scenario: A style change renames nothing
- **WHEN** the filename style of a vault of either layout changes and a governed write then edits a page whose name was derived under the earlier style
- **THEN** the page keeps its path
- **AND** no link is repointed

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
The relayout SHALL keep a page in an exempt or unregistered folder at its path relative to the managed root, so that only the wrapper segment drops. A `Notes/<X>` folder whose `<X>` names no registered area SHALL count as unregistered, so it keeps its `Notes/` level, and `--plan` SHALL list each such folder. Markdown at the vault root outside the old wrapper SHALL keep its path and become owned when the managed root becomes the vault root, and `--plan` SHALL list each such file. The relayout SHALL flatten a partition folder below the group level into the group folder, or into the area when the area has no group folder. It SHALL keep every pinned name and every fixed name, and SHALL give every derived name the form of the vault's resolved filename style, which is `title` by default in `root`, with the date prefix taken from the page's recorded date. `--plan` SHALL report two pinned names that would take one path. `--apply` SHALL refuse and name any page that the plan cannot map to exactly one new path, including those pages.

#### Scenario: The plan lists pages that become owned
- **WHEN** `--plan` runs on a vault with Markdown files at the vault root outside the old wrapper
- **THEN** the plan lists each of those files as becoming owned
- **AND** each file keeps its path
- **AND** the plan writes nothing

#### Scenario: A relayout keeps a pinned name and renames a derived one
- **WHEN** a relayout plans a page with a pinned name and a page whose slug name is derived
- **THEN** the pinned page keeps its filename
- **AND** the derived page takes the form of the resolved filename style, which is `title` by default

#### Scenario: An unregistered Notes folder keeps its path
- **WHEN** a relayout plans a page in `Notes/Scratch/` and no compiled type has the area `Scratch`
- **THEN** the page keeps the path `Notes/Scratch/<Name>.md` relative to the managed root
- **AND** `--plan` lists the `Notes/Scratch/` folder

#### Scenario: Pages outside the placement rules keep their folder
- **WHEN** a relayout plans a page in an exempt folder and a page with a partition folder below its group
- **THEN** the exempt page keeps its path relative to the managed root
- **AND** the partitioned page moves into its group folder

#### Scenario: A page the plan cannot map refuses the relayout
- **WHEN** the plan holds a page that it cannot map to exactly one new path, such as two pinned names that would meet at one path
- **THEN** `--plan` reports that page
- **AND** `--apply` refuses and names it, and no page moves
