## ADDED Requirements

### Requirement: A vault has a single managed root
A vault SHALL have a single managed root: the folder that holds `_Schema/`. In `root` layout the managed root SHALL be the vault root. In `nested` layout it SHALL be a folder inside the vault. A record at `_Schema/vault-layout.yaml` in the managed root SHALL state the layout, `nested` or `root`, and in `root` the top-level entries that Exomem does not manage. `exomem init` and the relayout SHALL write the record, and in store mode it SHALL be a stored page. Exomem SHALL resolve the layout once per vault, and SHALL NOT infer `root` from where a folder sits. Resolution SHALL read the record in the vault root's `_Schema/` first and then the record in the wrapper's `_Schema/`, and in store mode it SHALL read the stored page when the file is absent. The first record found decides. Resolution has five cases:
- A record that says `root` SHALL give `root`.
- A record that says `nested` SHALL give `nested`.
- No record and `_Schema/` only in the wrapper folder, or nowhere, SHALL give `nested`, which covers every existing vault.
- No record and `_Schema/` at the vault root only SHALL refuse with an error that names the missing record.
- No record and `_Schema/` in both the vault root and a wrapper folder SHALL refuse with an error that names both folders.

Each refusal SHALL name the remedy, and the record that fits depends on the refusal. `_Schema/` only at the vault root means the vault was laid out as `root`, so the remedy is a `root` record there. `_Schema/` in both places with no record means an interrupted relayout or a stray folder, so the remedy is the record for the layout that the pages are in, which is `nested` when the pages sit under the wrapper, because a wrong record hides every page. While a record exists, a leftover second `_Schema/` folder SHALL be reported by status and doctor and SHALL NOT be refused, because both folders exist during the relayout's projection window. A requirement that names `Knowledge Base/<rest>` SHALL mean `<managed root>/<rest>`, except that in `root` layout `Knowledge Base/Notes/<Area>/<rest>` SHALL mean `<Area>/<rest>` for a compiled area `<Area>`, because the `Notes/` level drops. An existing vault SHALL keep its layout until a relayout moves it, and an upgrade SHALL NOT move any file.

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
- **AND** `_Schema/vault-layout.yaml` records the layout

#### Scenario: A nested path maps to a root path
- **WHEN** a requirement names `Knowledge Base/Notes/Insights/<Name>.md` and the vault's layout is `root`
- **THEN** it means `Insights/<Name>.md`

#### Scenario: A record that says root decides
- **WHEN** `_Schema/vault-layout.yaml` at the vault root says `root`
- **THEN** the vault root is the managed root
- **AND** every path is read without a wrapper segment

#### Scenario: A record that says nested decides
- **WHEN** `_Schema/vault-layout.yaml` in the wrapper folder says `nested` and the vault root holds no record
- **THEN** the wrapper folder is the managed root
- **AND** every path keeps its wrapper segment

#### Scenario: A stored record decides when its file is absent
- **WHEN** a store-mode vault holds the layout record as a stored page and the projected file is absent
- **THEN** resolution reads the stored page and gives its layout

#### Scenario: A vault with only a wrapper folder is nested
- **WHEN** no layout record exists and only the wrapper folder holds `_Schema/`
- **THEN** the vault's layout is `nested`

#### Scenario: A folder with no schema folder is nested
- **WHEN** no layout record exists and no `_Schema/` folder exists in the vault root or in a wrapper folder
- **THEN** the vault's layout is `nested`

#### Scenario: A root-shaped vault without a record is refused
- **WHEN** no layout record exists and `_Schema/` sits at the vault root with no wrapper folder
- **THEN** resolution refuses and the error names the missing record
- **AND** the error names the remedy: write a `root` record in the vault root's `_Schema/`
- **AND** no file changes

#### Scenario: A recorded layout decides between two roots
- **WHEN** a layout record says `root` and both the vault root and a wrapper folder hold `_Schema/`, as during the relayout's projection window
- **THEN** the vault root is the managed root
- **AND** status and doctor report the leftover `_Schema/` folder, and nothing refuses

#### Scenario: A nested record decides while a root-level schema folder is left over
- **WHEN** the wrapper folder's layout record says `nested` and the vault root also holds `_Schema/` without a record
- **THEN** the wrapper folder is the managed root
- **AND** status and doctor report the leftover `_Schema/` folder, and nothing refuses

#### Scenario: Two roots with no record are refused
- **WHEN** no layout record exists and both the vault root and a wrapper folder hold `_Schema/`
- **THEN** resolution refuses and the error names both folders
- **AND** the error names the remedy: write the record for the layout that the pages are in, which is `nested` when they sit under the wrapper folder
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

### Requirement: Path selectors are read through the layout
Every reader of a path selector SHALL read it through a single layout translation. The selectors are note-type and home `folder` values, governance `paths`, `exclude_paths` and `refs`, `_access.yaml` tiers, vocabulary instance bindings and folder topic carriers. In `root` layout the translation SHALL drop a leading wrapper segment and SHALL read `Notes/<Area>/...` as `<Area>/...` for a registered compiled area or home. The translation SHALL NOT rewrite any governance file, `_access.yaml` or overlay value. A path ref or filename glob that names a page that a relayout renames SHALL NOT be translated.

#### Scenario: A wrapper-form path scope still selects in a root vault
- **WHEN** a governance scope selects `Knowledge Base/Notes/Insights/**` and the vault's layout is `root`
- **THEN** the scope selects the pages under `Insights/`
- **AND** the governance file is unchanged

#### Scenario: A restricted caller does not see a page that a path scope withholds
- **WHEN** a governance scope selects `Insights/**` in a `root` vault and a caller lacks that scope
- **THEN** the caller does not see `Insights/<Name>.md`

#### Scenario: An access tier keeps its meaning
- **WHEN** an `_access.yaml` tier names the append-only trees by wrapper-relative prefix and the vault's layout is `root`
- **THEN** the tier covers the same trees without the wrapper

### Requirement: Pages sit flat inside areas
In `root` layout, a page path SHALL hold an area, the group folders that the page's role or collection profile declares, and the name. A page of role `source` SHALL group by kind, of role `entity` by entity type, and of role `evidence` by scope and then category. A collection's pages SHALL sit in its profile's area, grouped by collection. A page of any other role SHALL have no group folder. These group folders SHALL be the only levels below an area. A writer SHALL NOT add a project, domain or medium folder or any other taxonomy level, and the relayout SHALL flatten the partition folders that an existing vault holds below an area.

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
A page name derived under `title` style in a `root` vault, whether a writer gives it to a new page or the relayout gives it to an existing page, SHALL be unique among the Markdown pages that Exomem owns, across the whole vault and not per folder, compared after NFC normalisation and casefolding. On a collision the writer SHALL append ` (2)`, ` (3)` and so on, taking the lowest number that no page holds. The suffix SHALL count toward the filename length cap. A derived name SHALL yield to fixed names whatever the path order. A relayout SHALL resolve collisions among derived names in ascending order of the pages' current paths, so that the same plan input gives the same names. A slug name, and a `title` name in a `nested` vault, SHALL keep the collision contract of the `vault-file-naming` requirement "Case-Insensitive Collision Detection".

#### Scenario: Two pages with the same title get distinct names
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
- **THEN** the writer disambiguates it as the `vault-file-naming` capability defines

### Requirement: A page name is derived or fixed
A name SHALL be derived when the page's note type names its files from the page's frontmatter `title`, as a title stem with or without a date prefix. `title` means the frontmatter value only: an H1 or a filename stem SHALL NOT stand in for it, and a page with no frontmatter title SHALL keep its name. Every other name SHALL be fixed: an Evidence original, a Records or Planning recipe name, a generated navigation page (`index.md`, `log.md`, `hub.md`), a page named after the file it describes, and any page whose type has no title rule or that sits outside every registry area. The class SHALL follow the page's note type. A fixed name SHALL never be renamed after creation, and a new write whose fixed name collides inside a single folder SHALL keep today's disambiguation.

Whether a retitle or a style change renames a file is stated once, in the `vault-file-naming` requirement "No Existing File Is Renamed": neither does, and only the relayout gives an existing derived name the form of the vault's resolved filename style. No record tells a name that a caller chose from a derived name, so the relayout treats every title-named page as derived.

#### Scenario: A name is fixed when the type has no title rule
- **WHEN** a governed write creates an Evidence page or a generated navigation page
- **THEN** its name is fixed
- **AND** no later governed write and no relayout renames it

#### Scenario: A page without a frontmatter title keeps its name
- **WHEN** a relayout plans a page whose frontmatter has no `title`, though its body starts with a heading
- **THEN** the page keeps its filename

### Requirement: Links use the shortest trailing path that resolves to a single file
In a `root` vault, a writer SHALL emit the shortest trailing path of the target page, cut at a folder boundary, that resolves to exactly a single file for both Exomem and Obsidian. That path SHALL be `[[Name]]` when no other file that Obsidian sees has that name. When no trailing path singles the target out, the writer SHALL emit the target's full vault path. Graph rows SHALL keep the resolved vault path of the target. A link repoint SHALL change only the link's target text, and the repointed link SHALL resolve to the page it resolved to before. A writer in a `nested` vault SHALL keep today's link form.

In both layouts, Exomem SHALL resolve a link the way Obsidian does. After the steps that resolve links today, it SHALL compare the link text with the basename and the trailing path of each file in the link index, under the NFC-casefold key, and SHALL resolve the link only when exactly a single file matches. A link text that two files satisfy SHALL stay unresolved. A link that resolves today SHALL keep its result. Machinery folders, such as `_Schema/`, SHALL stay out of the link index, so a link into them stays unresolved in Exomem, although a writer SHALL still count the files in them when it picks a short form.

#### Scenario: A unique name gives a bare link
- **WHEN** a governed write in a `root` vault links to a page whose name no other file holds
- **THEN** the body carries `[[Name]]`
- **AND** the graph edge ends at the target's vault path

#### Scenario: A homonym outside the owned pages forces a longer path
- **WHEN** a governed write in a `root` vault links to `Insights/<Name>.md` and a file in `_trash` has the same name
- **THEN** the body carries `[[Insights/<Name>]]`

#### Scenario: A grouped page keeps a real path suffix
- **WHEN** a governed write in a `root` vault links to `Evidence/alpha/receipts/Scan.md` and `Evidence/beta/receipts/Scan.md` also exists
- **THEN** the body carries `[[alpha/receipts/Scan]]`
- **AND** the body does not carry `[[Evidence/Scan]]`, which is not a path suffix of either page

#### Scenario: A page that no trailing path singles out keeps its full path
- **WHEN** a governed write in a `root` vault links to `Evidence/Scan.md` and `Archive/Evidence/Scan.md` also exists
- **THEN** the body carries `[[Evidence/Scan]]`, the full vault path of the target
- **AND** a link to `Archive/Evidence/Scan.md` carries `[[Archive/Evidence/Scan]]`

#### Scenario: A nested vault keeps today's link form
- **WHEN** a governed write links to a page in a `nested` vault
- **THEN** the body carries the link form that the writer emits today
- **AND** no link takes the trailing-path form

#### Scenario: A link that differs only in letter case resolves
- **WHEN** a page in either layout links `[[budget]]` and a single file is named `Budget.md`
- **THEN** the link resolves to `Budget.md`
- **AND** the graph edge ends at that page's vault path

#### Scenario: A partial path resolves in a nested vault
- **WHEN** a page in a `nested` vault links `[[receipts/Scan]]` and a single page's path ends in `receipts/Scan.md`
- **THEN** the link resolves to that page

#### Scenario: A link text that two files satisfy stays unresolved
- **WHEN** a page links `[[receipts/Scan]]` and two pages' paths end in `receipts/Scan.md`
- **THEN** the link stays unresolved

#### Scenario: A link into a machinery folder stays unresolved
- **WHEN** a page links `[[_Schema/Guide]]`, a file that Obsidian opens
- **THEN** the link stays unresolved in Exomem
- **AND** the graph holds no edge for it

#### Scenario: A link that resolves today keeps its result
- **WHEN** a link resolves through today's steps and a second file matches it only by letter case
- **THEN** the link still resolves to the same page

### Requirement: Old paths keep resolving
The relayout SHALL write a durable record in the vault with a row for each moved file: its identity (or none), its old path and its new path. The record SHALL be `_Schema/relayout-record.yaml` under the managed root, with `schema_version: 1` and a `relayouts` mapping from a relayout id to `reverted` (true for an entry that a revert undid) and `rows`, a list of `exomem_id` (an id or null), `old_path` and `new_path`. In store mode the record SHALL be a stored page. The revert SHALL write its own entry in the record. The relayout SHALL NOT write `moved_from` into any page. The prior-path index of `add-curation-relocation-protocol` SHALL read the relayout record as it reads a page's `moved_from`. It SHALL apply resolution, visibility, reserved paths and its guard against a new page at a prior path to a relayout row exactly as to a `moved_from` row. The managed-relative form that the hosted protected-tree guard and the executors share SHALL agree with the resolver's prior-path step. A prior path to a page that the caller may not see SHALL answer exactly as a missing page. `store_pages.path` SHALL remain the store key, and the page's `exomem_id` and the store's identities table SHALL carry its move-safe identity.

#### Scenario: An old path still resolves after the relayout
- **WHEN** the relayout moves a page and a link or call names its old path
- **THEN** the link or call resolves to the page at its current path

#### Scenario: The relayout records moves in a record and not in pages
- **WHEN** the relayout moves a page
- **THEN** `_Schema/relayout-record.yaml` holds a row with the page's `exomem_id`, old path and new path
- **AND** the page's frontmatter gains no `moved_from` field

#### Scenario: A page without an identity still has a row
- **WHEN** the relayout moves a file that has no `exomem_id`
- **THEN** its row holds a null `exomem_id`, its old path and its new path

#### Scenario: A reference survives a move
- **WHEN** a page moves or is retitled in a store-mode vault
- **THEN** its `exomem://memory/<uuid>` reference still names the same page

#### Scenario: The guard and the resolver read an old path the same way
- **WHEN** the hosted protected-tree guard and the wikilink resolver each read the old path of a relaid-out page
- **THEN** both read it as the same current path

#### Scenario: An old path to a page the caller may not see answers as missing
- **WHEN** a restricted caller names the old path of a relaid-out page that the caller may not see
- **THEN** the answer equals the answer for a path that never existed
- **AND** the answer names neither the current path nor the page

#### Scenario: A revert keeps root-layout paths resolving
- **WHEN** `--revert` returns a page to its nested path
- **THEN** the revert's own entry in the relayout record holds a row from the page's root-layout path to its nested path
- **AND** a link or call that names the page's root-layout path resolves to the page at its nested path

### Requirement: Relayout is a single verified governed move
`exomem store-mode relayout` SHALL move a store-mode vault from `nested` to `root` in a single store change that holds the page moves, the repointed pages, the relayout record and the layout record. It SHALL rewrite no governance file, no `_access.yaml` and no note-type overlay `folder` value, because those are read through the layout translation.

`--plan` SHALL work in file mode and in store mode, SHALL compute every move, name, suffix, truncation, link repoint and pointer-field repoint, and SHALL write nothing. The relayout SHALL repoint every pointer field that names a moved page or file, including `evidence_file`, `data_file`, `parent_media`, `governance_companion.artifact_path`, `raw_protection.artifact_path` and the `parent_path`, `parent_page` and `artifact_path` of a document container member's binding. In an append-only `Sources/` or `Evidence/` page the relayout SHALL repoint links and pointer fields only through the governed pointer write of `add-curation-relocation-protocol`. For every governance scope and access tier, `--plan` SHALL compute the member set by identity before and after the move, SHALL count the files each instance binding holds before and after, and SHALL list each path ref and filename glob that names a page the relayout renames, which an operator converts to an id ref.

`--apply` SHALL refuse unless store mode is active, the service is stopped, no item is held, the projection queue is empty, a store backup exists and free disk space is at least the size of the store. It SHALL also refuse while any page cannot map to a single new path, while any member set differs, while a path ref to a renamed page stays unconverted, and while a top-level entry outside the old wrapper has the name of an area, `index.md` or `log.md`, until the owner renames or removes that entry. The apply SHALL write a `relayout_pending` marker first, then commit the store change, then project every queued intent offline, then move the disk-canonical files (`_Governance/`, `_access.yaml` and the collection replica), and then clear the marker. While the marker stands, the service start and every CLI read SHALL refuse with an error that names `--apply` to resume and `--revert`. Governance root resolution SHALL refuse when the root-layout `_Governance/` is missing and the wrapper `_Governance/` exists. A copy of the collection replica SHALL stay at the old path, so that a build from before the layout record refuses with `VAULT_STORE_ELSEWHERE`, and doctor SHALL report that copy and remove it on request.

`--verify` SHALL prove that every owned page maps to a single new path with its `exomem_id` unchanged, that every member set is equal by identity, that every link occurrence resolves to the same page as before, that page bytes equal the old bytes apart from the planned link and pointer-field edits, that binary files keep their content hashes, and that a fresh derivation equals the stored rows.

`--revert` SHALL plan back to `nested` with the same planner. The relayout record SHALL hold each page's old path, and the relayout SHALL record each link span's old text in its audit. The revert SHALL return every page that existed at the relayout to its recorded path, SHALL restore the old text of every recorded link span that is still byte-identical, and SHALL give a page created after the relayout nested placement by rule and today's link form. In the same store change it SHALL write a `nested` layout record in the wrapper's `_Schema/`, remove the record in the vault root and write a relayout record of its own moves. The managed upgrade migrator SHALL NOT run a relayout.

#### Scenario: The plan writes nothing
- **WHEN** `--plan` runs on a store-mode vault
- **THEN** it reports counts of moves, suffixes, truncations and link repoints
- **AND** the store and the vault files are byte-identical before and after

#### Scenario: Every link resolves to the same page after the relayout
- **WHEN** `--verify` runs after `--apply`
- **THEN** each link occurrence resolves to the page it resolved to before
- **AND** each rewritten link passes Obsidian's basename-or-path rule

#### Scenario: Only link spans and pointer fields change in page bytes
- **WHEN** `--verify` compares each page after the relayout with its bytes before
- **THEN** the two differ only in the planned link spans and pointer-field edits
- **AND** every binary file keeps its content hash

#### Scenario: An append-only page changes only in its pointers
- **WHEN** the relayout moves a note that an `Evidence/` page links from its body
- **THEN** the page's bytes differ from before only in that link's target text
- **AND** the governed pointer write records the edit in the audit trail

#### Scenario: A scope that would gain or lose a member refuses the relayout
- **WHEN** a governance scope selects a page by path before the relayout and would not select it after
- **THEN** `--plan` reports the scope with its counts and sample ids
- **AND** `--apply` refuses and no page moves

#### Scenario: A path ref to a renamed page must become an id ref
- **WHEN** a governance scope names a page by path ref and the relayout renames that page
- **THEN** `--plan` lists the ref as needing an id ref
- **AND** `--apply` refuses until the operator converts it

#### Scenario: A moved video keeps its frames' scopes
- **WHEN** the relayout moves a video and its scene frames, and each frame's `parent_media` names the video
- **THEN** each frame's `parent_media` names the moved video
- **AND** each frame still inherits the video's scopes

#### Scenario: A top-level name collision refuses the relayout
- **WHEN** a foreign top-level folder outside the old wrapper has the name of an area and `--apply` runs
- **THEN** the relayout refuses and names the folder
- **AND** the error says that renaming or removing that entry lifts the refusal
- **AND** no page moves

#### Scenario: Renaming the clashing entry lifts the refusal
- **WHEN** the owner renames or removes the clashing top-level entry and `--apply` runs again
- **THEN** the relayout no longer refuses for that entry

#### Scenario: A crash leaves a vault that resumes or reverts
- **WHEN** the process stops after the store change commits and before the marker clears
- **THEN** the service start and every CLI read refuse and name `--apply` and `--revert`
- **AND** `--apply` completes the remaining steps and clears the marker

#### Scenario: A missing root-layout governance folder refuses
- **WHEN** governance root resolution finds no root-layout `_Governance/` while the wrapper `_Governance/` exists
- **THEN** resolution refuses
- **AND** no page is treated as covered by an empty policy

#### Scenario: A build from before the layout record refuses the vault
- **WHEN** a build from before the layout record opens a relaid-out vault
- **THEN** it finds the replica copy at the old path and refuses with `VAULT_STORE_ELSEWHERE`
- **AND** it does not start in file mode

#### Scenario: A revert restores old paths and old link text
- **WHEN** `--revert` runs after a relayout
- **THEN** every page that existed at the relayout returns to its recorded path
- **AND** every recorded link span that is still byte-identical has its old text again

#### Scenario: A revert records the nested layout
- **WHEN** `--revert` runs after a relayout
- **THEN** the wrapper's `_Schema/vault-layout.yaml` says `nested` and the vault root's record is removed in the same store change
- **AND** resolution gives `nested`

#### Scenario: A revert keeps a link that a person edited
- **WHEN** a person edited a recorded link span after the relayout and `--revert` runs
- **THEN** that span keeps its current text

#### Scenario: A revert places a later page by rule
- **WHEN** a page was created after the relayout and `--revert` runs
- **THEN** the page takes nested placement by rule
- **AND** its links take today's link form

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
The relayout SHALL keep a page in an exempt or unregistered folder at its path relative to the managed root, so that only the wrapper segment drops. A `Notes/<X>` folder whose `<X>` names no registered area SHALL count as unregistered, so it keeps its `Notes/` level, and `--plan` SHALL list each such folder. The relayout SHALL flatten each partition folder below the group level into the group folder, or into the area when the area has no group folder. It SHALL keep every fixed name, and SHALL give every existing derived name the form of the vault's resolved filename style, which is `title` by default in `root`, with the date prefix taken from the page's recorded date, unique across the vault. A page named after the file it describes SHALL keep its name: the page `<file name>.md` of any file, and the page `<stem>-notes.md` of an unprotected Markdown original. A Markdown original that carries its own type and title SHALL keep its name together with its `<file name>.md` page. `--plan` SHALL report two fixed names that would take a single path. `--apply` SHALL refuse and name any page that the plan cannot map to exactly a single new path, including those pages.

Content outside the old wrapper SHALL stay at its path and stay unmanaged: Exomem SHALL index it for search where it indexes it today and SHALL NOT write it. The layout record SHALL list each such top-level entry under `unmanaged`. `--plan` SHALL list each of them with its links into the managed tree, which the relayout cannot repoint. A top-level entry whose name is an area's, `index.md` or `log.md` SHALL refuse `--apply` until the owner renames or removes it.

#### Scenario: The plan lists outside content as unmanaged
- **WHEN** `--plan` runs on a vault with files and folders at the vault root outside the old wrapper
- **THEN** the plan lists each of them as unmanaged, with its links into the managed tree
- **AND** each keeps its path
- **AND** the plan writes nothing

#### Scenario: Outside content is never written
- **WHEN** a relayout applies and a Markdown file outside the old wrapper links to a page that moved
- **THEN** that file's bytes are unchanged
- **AND** the layout record lists its top-level entry under `unmanaged`

#### Scenario: A relayout flattens a partition folder
- **WHEN** a relayout plans the page `Notes/Research/Alpha/Churn drivers.md` below the wrapper, and `Research` is a registered compiled area
- **THEN** the page moves to `Research/Churn drivers.md`
- **AND** no project folder remains

#### Scenario: A relayout keeps a fixed name and renames a derived name
- **WHEN** a relayout plans an Evidence original and a page whose slug name is derived
- **THEN** the Evidence original keeps its filename
- **AND** the derived page takes the form of the resolved filename style, which is `title` by default

#### Scenario: An unregistered Notes folder keeps its path
- **WHEN** a relayout plans a page in `Notes/Scratch/` and no compiled type has the area `Scratch`
- **THEN** the page keeps the path `Notes/Scratch/<Name>.md` relative to the managed root
- **AND** `--plan` lists the `Notes/Scratch/` folder

#### Scenario: Pages outside the placement rules keep their folder
- **WHEN** a relayout plans a page in an exempt folder and a page with a partition folder below its group
- **THEN** the exempt page keeps its path relative to the managed root
- **AND** the partitioned page moves into its group folder

#### Scenario: A sidecar page keeps the name that binds it to its file
- **WHEN** the relayout plans a page named `<file name>.md` beside the file it describes, such as the page of a video or of a scene frame
- **THEN** the page keeps its name even when its title would rename it
- **AND** the file, the page, the folder `<file name>.frames/` and the frame pages keep their names, and only the managed folder prefix drops

#### Scenario: A companion page keeps the name that binds it to its original
- **WHEN** the relayout plans the page `<stem>-notes.md` beside an unprotected Markdown original `<stem>.md`
- **THEN** both keep their names, and only the managed folder prefix drops

#### Scenario: A Markdown original with its own title keeps its pair
- **WHEN** the relayout plans a Markdown original that carries its own type and title, beside its page `<file name>.md.md`
- **THEN** the original keeps its name and the page keeps `<file name>.md.md`

#### Scenario: A page the plan cannot map refuses the relayout
- **WHEN** the plan holds a page that it cannot map to exactly a single new path, such as two fixed names that would meet at a single path
- **THEN** `--plan` reports that page
- **AND** `--apply` refuses and names it, and no page moves
