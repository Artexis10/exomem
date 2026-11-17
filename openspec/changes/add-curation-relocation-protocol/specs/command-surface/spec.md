## MODIFIED Requirements

### Requirement: Append-Only Tree Relocation

A move that stays within one append-only tree SHALL be permitted. It SHALL carry the body and every field verbatim, and the page's lineage entry SHALL be its only byte change. A move from `Sources/` into `Evidence/` SHALL be permitted as a promotion when the caller supplies a promotion reason, and the reason MUST be recorded in the activity log. Every other boundary crossing MUST be refused, including any move out of `Evidence/` and any move into an append-only tree from a non-append-only location. Promotion MUST NOT change file content; its only byte change SHALL be the lineage entry.

A move that updates wikilinks SHALL repoint each link inside a page in `Sources/` or `Evidence/` through the governed pointer write when the moved page carries an `exomem_id`. When the moved page carries no `exomem_id`, a move that would rewrite such a link SHALL be refused with `APPEND_ONLY`, as before. A move SHALL also be refused with `APPEND_ONLY` when its change to such a page is not a pointer change. A page withheld from a restricted mover stays unchanged instead, and the move commits. A generated `index.md` in those trees is not captured content, and the move SHALL rewrite it. A path in an `Evidence/` page's `ingested_into` field is a pointer field, and the move SHALL repoint it through the same governed pointer write. The body outside the repointed spans and every other field of that page SHALL keep their bytes.

Every governed move SHALL follow this rule, in file mode and in store mode. A reclassification is a governed move: it SHALL repoint its append-only referrers in the same way, and its revert SHALL restore from their pointer-write records the ones its caller may see and write. Lineage SHALL stay the safety net for every reference that a move does not write.

#### Scenario: Source becomes case-relevant

- **WHEN** a caller moves a page from `Sources/` to `Evidence/<scope>/` with a promotion reason
- **THEN** the file is relocated with its body and fields unchanged and one lineage entry added
- **AND** the activity log records the promotion together with the supplied reason

#### Scenario: Promotion without a stated reason

- **WHEN** a caller moves a page from `Sources/` to `Evidence/<scope>/` without a promotion reason
- **THEN** the move is refused
- **AND** the refusal names the missing reason rather than the append-only rule

#### Scenario: Evidence is never demoted

- **WHEN** a caller moves a page out of `Evidence/` to any destination
- **THEN** the move is refused regardless of any reason supplied
- **AND** the refusal states that a case scope must remain complete

#### Scenario: Outside content still lands through the capture writers

- **WHEN** a caller moves a page from a non-append-only location into `Sources/` or `Evidence/`
- **THEN** the move is refused
- **AND** the refusal directs the caller to `add` or `preserve`

#### Scenario: Evidence names the moved note as ingested

- **WHEN** a caller previews and then moves a note with `update_wikilinks=true` and the preview digest, and an `Evidence/` page names that note only in its `ingested_into` field
- **THEN** the move commits and the `ingested_into` entry names the new path
- **AND** the page's body and other fields keep their bytes

#### Scenario: A link in Evidence content is repointed

- **WHEN** an `Evidence/` page links the moved note from its body, the note carries an `exomem_id`, and the caller passes the preview digest
- **THEN** the move commits and the link names the new path
- **AND** every other byte of the `Evidence/` page is unchanged, and the pointer-write record holds the old link

#### Scenario: A store-mode move repoints a link in Evidence content

- **WHEN** a caller moves a note that carries an `exomem_id` in a store-mode vault with the preview digest, and an `Evidence/` page links the note from its body
- **THEN** the move commits and the link names the note's new location and still resolves to the note
- **AND** the page's bytes differ from before only in that link's target text
- **AND** the pointer-write record holds the repoint with its reason

#### Scenario: A link in Evidence content still blocks the move

- **WHEN** an `Evidence/` page links the moved note from its body and the note carries no `exomem_id`
- **THEN** the move is refused with `APPEND_ONLY`
- **AND** no page is changed

## ADDED Requirements

### Requirement: A moved page records its prior paths

Each move of a Markdown page that has a frontmatter block SHALL append one
entry naming the old path to the page's `moved_from` list, oldest first, in the
same commit as the move. A reclassification entry SHALL also carry the prior
classification keys. A legacy `reclassified_from` value SHALL read as lineage
entries older than every `moved_from` entry, and no writer SHALL rewrite it to
rename the key. A page without a frontmatter block SHALL move without lineage.

The entry of an undoable move, such as a reclassification, SHALL name the
move's operation id, whether or not the move repointed a page, and SHALL name
no pointer-write record. Owner-only state SHALL map the operation id to the
pointer-write record of each page the move repointed, and the undo SHALL find
the records through that map. A reader of the page therefore learns nothing
about how many pages the move repointed. Prior paths SHALL come only from the
entries' paths and the relayout record, never from that map.

An undo SHALL remove the lineage entry that its forward move appended. A
reclassification revert and a curation compensation are undos. An undo whose
caller is not restricted SHALL append no entry, and it SHALL restore every
page that its forward move repointed to that page's exact before bytes. An
undo whose caller is restricted SHALL also append an entry naming the path
that it vacates, whatever pages link that path. It SHALL restore from their
records only the pages that the caller may see and write. Its answer and the
moved page's bytes then do not depend on any page withheld from the caller. A
withheld page that still links the vacated path resolves through the lineage,
and its pointer-write record stays valid. A plain move back to an old path is
a move and appends an entry.

Only a move, its undo and a reclassification SHALL write `moved_from` or
`reclassified_from`. Every other writer SHALL refuse a caller-supplied change
to either key with `LINEAGE_PROTECTED`, as it refuses one to `updated`. This
covers creates, frontmatter patches, full rewrites, supersede successors and
a curation `create-note` step. A forged entry would otherwise claim another
page's old path and take its links.

A move SHALL append its entry after every earlier byte of the `moved_from`
list, so the list's entries, comments and blank lines keep their bytes. When
the moved page is in `Sources/` or `Evidence/`, the move SHALL prove that its
lineage entry is the only change it makes beyond the caller's declared change,
and SHALL record that pointer write under the move's operation.

#### Scenario: A note moves twice

- **WHEN** a note moves from path A to path B and then to path C
- **THEN** the note at C lists A and then B in `moved_from`
- **AND** links to A and to B resolve to the note at C

#### Scenario: A reclassified Source moves again

- **WHEN** a Source that records a legacy `reclassified_from` entry moves
- **THEN** its `reclassified_from` value keeps its bytes and the new entry goes
  to `moved_from`

#### Scenario: A writer supplies a lineage entry

- **WHEN** a restricted caller creates a note whose `moved_from` names a page
  withheld from it
- **THEN** the create refuses with `LINEAGE_PROTECTED`
- **AND** links to the withheld page's old path still resolve to that page

#### Scenario: A moved Source keeps its list's comments

- **WHEN** a Source whose `moved_from` list is followed by a comment and a
  blank line moves, and a curation plan then corrects its `project`
- **THEN** the comment and the blank line keep their bytes after each write
- **AND** the move's pointer-write record holds only the appended entry

#### Scenario: A revert removes its own entry

- **WHEN** the owner reverts a reclassification
- **THEN** the Source no longer lists the path it returned from, and no entry
  names the path it left

#### Scenario: A lineage entry does not count repoints

- **WHEN** a reclassification repoints two `Evidence/` pages that link the
  Source by path
- **THEN** the Source's new lineage entry holds the same keys as the entry of a
  reclassification that repointed none, and its operation id is the only value
  that differs
- **AND** a revert finds both records through the operation id and restores
  each page's exact bytes

#### Scenario: A restricted revert leaves a withheld referrer resolving

- **WHEN** the owner's reclassification repointed an `Evidence/` page that is
  withheld from a restricted caller, and that caller reverts the
  reclassification
- **THEN** the Source returns to its earlier path and its lineage names the path
  it vacated
- **AND** the withheld page keeps the link that its pointer-write record holds,
  and that link resolves to the Source
- **AND** the caller's answer and the Source's bytes are the same as in a vault
  without the withheld page

### Requirement: Links resolve through a page's prior paths

A prior path SHALL come from one of two durable lineage sources:

- the page's own lineage: its `moved_from` entries and any legacy
  `reclassified_from` entries;
- a row of the relayout record that `adopt-root-vault-layout` keeps in the
  vault. Each row names an `exomem_id` or none, an old path and a new path.

A relayout row with an `exomem_id` SHALL claim its old path for the live page
that holds that `exomem_id`. A row without one SHALL claim its old path for the
live page whose current path or lineage holds the row's new path. A bulk
relayout writes no `moved_from`. A prior path from either source SHALL follow
the same resolution, visibility, reserved-path and squatter-guard rules. A row
that a relayout revert reverses SHALL claim nothing.

Link resolution SHALL try, in order: the exact path, the wrapper-prefixed path,
a prior path, a retired collection path, the stem, the title, and a prior stem.
A prior path or prior stem SHALL resolve only when exactly one live page that
the caller may see claims it. The visibility filter SHALL run before claimants
are counted, so a prior path that only a withheld page claims SHALL answer as
missing. A lineage entry equal to its page's current path SHALL claim nothing.
A prior stem SHALL never win over a live name.

Read commands, link rendering inside written content, the graph, the audit and
inbound-link checks SHALL use this resolution. The target path argument of a
write command SHALL stay literal and SHALL NOT follow lineage. Inbound-link
checks for a delete or a move SHALL read the target's lineage from its own
bytes. The prior-path lookup SHALL read an indexed derived table. The identity
writers SHALL refill that table from page bytes and the relayout record, and a
rebuild from those two sources alone SHALL give the same rows. No read or write
SHALL scan the vault for lineage. The audit SHALL report each prior path that
two live pages claim as `prior_path_conflict`.

#### Scenario: An old link resolves after a move

- **WHEN** a Source keeps a link to a note's old path after the note moves
- **THEN** reading the Source's links resolves that link to the note's new path
- **AND** the graph holds the edge to the note at its new path

#### Scenario: An old link resolves after a relayout

- **WHEN** a relayout moves a note without writing `moved_from`, its record
  holds the note's `exomem_id`, old path and new path, and the note later moves
  again
- **THEN** a link to the relayout's old path resolves to the note at its
  current path
- **AND** a rebuild of the prior-path index gives the same resolution

#### Scenario: A withheld page claims the prior path

- **WHEN** a restricted caller follows a link whose prior path only a page
  withheld from that caller claims
- **THEN** the link answers as missing, exactly as a link to a path no page
  claims

#### Scenario: Two pages claim one prior path

- **WHEN** two live pages list the same prior path
- **THEN** a link to that path stays unresolved
- **AND** the audit reports both claimants as `prior_path_conflict`

#### Scenario: An edit names an old path

- **WHEN** a caller edits a page by a path that only a lineage entry names
- **THEN** the edit acts on that literal path and does not follow the lineage
  to the moved page

### Requirement: A prior path stays reserved while a live page claims it

`unique_path` and every create-only check SHALL treat a prior path that a live
page claims as taken. A move of a page to a path in its own lineage SHALL be
permitted. When the claimant is withheld from the caller, the refusal SHALL be
`WRITE_REFUSED` and SHALL NOT name the claimant, its current path or its
content. The refusal, and the different name that `unique_path` then picks,
SHALL still disclose one fact: that some live page claims that exact path.
The reservation SHALL hold anyway, because without it the caller's page would
win exact-path resolution and capture the claimant's old links. The disclosed
path is in a folder the caller may write, where the claimant was visible
before it moved.

A file without an `exomem_id` at a prior path that a live page claims SHALL
NOT take that address: links to the path SHALL keep resolving to the lineage
claimant, and the audit SHALL report the file as `reserved_path_occupied`.
A page with a valid `exomem_id` at such a path wins exact-path resolution, so
the audit SHALL report it to the owner as `reserved_path_identified`. A
restricted caller's audit SHALL show a prior-path finding only when the caller
may see every path it names, and SHALL NOT show `reserved_path_identified`.

#### Scenario: A create lands on a reserved path

- **WHEN** a caller creates a page at a path that a moved page lists in
  `moved_from`
- **THEN** the create refuses and names the claimant to a caller that may see it

#### Scenario: A create lands on a relayout's old path

- **WHEN** a caller creates a page at an old path that a relayout record row
  claims for a live page
- **THEN** the create refuses exactly as for a path in a page's `moved_from`

#### Scenario: A page moves back

- **WHEN** a page moves back to a path in its own lineage
- **THEN** the move commits

#### Scenario: An editor creates an empty file at the old path

- **WHEN** an editor creates a file without an `exomem_id` at a moved page's
  old path
- **THEN** links to that path still resolve to the moved page
- **AND** the audit reports the file as `reserved_path_occupied`

#### Scenario: An identified page sits at the old path

- **WHEN** a page with its own `exomem_id` arrives at a moved page's old path
  through a sync or a create that raced the move
- **THEN** the owner's audit reports it as `reserved_path_identified` and names
  the moved page
- **AND** a restricted caller's audit does not report it

#### Scenario: A withheld page holds the reserved path

- **WHEN** a restricted caller creates a page at a prior path that only a
  withheld page claims
- **THEN** the create refuses with `WRITE_REFUSED`, byte for byte as for any
  unavailable target

#### Scenario: A withheld claim is disclosed only as taken

- **WHEN** a restricted caller creates a page at a prior path that only a
  withheld page claims, in one vault and in a twin vault without that claim
- **THEN** the create refuses in the first vault and commits in the twin
- **AND** neither answer names the claimant, its current path or its content

### Requirement: Append-only pages change only in pointer spans

After capture, every write to a page in `Sources/` or `Evidence/` SHALL go
through the governed pointer write. The capture writers that finish a page's
processing record or assign its `exomem_id` SHALL keep their own rules. Each
caller of the pointer write SHALL declare the spans it intends to change. The
write SHALL admit a span only when it is one of these:

- a wikilink target whose old and new forms resolve to the same `exomem_id`,
  outside an origin carrier;
- the value of a frontmatter field that the `page-fields` registry declares
  with `role: pointer`;
- through a move, the page's own folder and one lineage entry appended after
  every earlier byte of `moved_from`. No declaration SHALL admit a removal
  from either lineage key or any change to `reclassified_from`; an undo's
  removal is checked against the entry its forward move appended.

The write SHALL parse the page and compare its before and after spans. A
collection value's span SHALL end at its last item's line, so a comment or
blank line after it lies outside every span. Every byte outside an admitted,
declared span MUST be identical, and any other difference SHALL refuse with
`APPEND_ONLY`. A page whose frontmatter uses YAML anchors or aliases SHALL
refuse with `APPEND_ONLY`, because an alias lets an admitted change alter
another field's parsed value. Original media, body text and provenance MUST
NOT change.

A call SHALL approve the pointer writes to the page it names: a direct move,
promotion or reclassification of that page SHALL need no digest, and a curation
plan approval SHALL approve its in-place pointer corrections. A pointer write to a page that the call did not name, such as
a referrer repoint, SHALL be approved either by a curation plan approval or by
a commit bound to the digest of the preview that listed the change. A commit
without that digest, or with a digest whose bindings no longer match, SHALL
refuse with `POINTER_PREVIEW_REQUIRED`, change nothing, and name the preview
call.

Each pointer write SHALL create one create-only, owner-only record. The record
SHALL name the page, the write's reason, its before and after content hashes,
and each changed span's location, old bytes and new bytes. A revert SHALL apply
only while the page holds the recorded after hash, and its result SHALL hash to
the recorded before hash.

#### Scenario: A move repoints a Source's link

- **WHEN** a pointer write repoints a Source's link to a moved note
- **THEN** only the declared link span differs
- **AND** the Source's body outside that span, its provenance fields and its
  original media keep their hashes

#### Scenario: A crafted repoint names another page

- **WHEN** a declared link span's new target resolves to a page with a
  different `exomem_id`
- **THEN** the write refuses with `APPEND_ONLY` and nothing changes

#### Scenario: A change outside the declared spans

- **WHEN** a pointer write's result differs from the page in a field the
  registry does not declare as a pointer, or in body text outside a link target
- **THEN** the write refuses with `APPEND_ONLY` and nothing changes

#### Scenario: A pointer write is reverted

- **WHEN** the owner reverts a pointer write while the page holds the recorded
  after hash
- **THEN** the page's bytes equal its bytes before the write

#### Scenario: A Source's frontmatter uses an alias

- **WHEN** a pointer write targets a Source whose frontmatter uses a YAML
  anchor or alias
- **THEN** the write refuses with `APPEND_ONLY` and nothing changes

#### Scenario: A revert after later work

- **WHEN** the page no longer holds the recorded after hash
- **THEN** the revert refuses and the page is unchanged

### Requirement: A pointer field on an append-only page is corrected through a curation plan

A pointer field of a page in `Sources/` or `Evidence/` SHALL be corrected in
place only by a curation `edit` step, whose exact plan the user confirms and
whose compensation reverts it. The correction SHALL run as a governed pointer
write and SHALL NOT move the page. It SHALL change only that field's value
span. It SHALL NOT set `updated` or any other field, and SHALL keep every other
byte, line endings included. The new value SHALL pass the same field
validation as on any other page, and the correction SHALL create a
pointer-write record. A direct `edit_memory` `patch_frontmatter` operation on
such a page SHALL keep refusing with `APPEND_ONLY`; for a pointer field, the
refusal SHALL name the curation route.

A pointer field whose registry entry names a `writer` SHALL change on such a
page only through that operation, and the `edit` step SHALL refuse it with
`POINTER_FIELD_OWNED`, naming that operation. `reclassify` owns the
classification fields, because it records the classification history and
relocates the source. `custody` owns the raw-file pointers, because its repair
checks the file's recorded identity. `closure` owns `ingested_into`, because a
compiled page's `sources:` citation writes that back-reference.

#### Scenario: A Source's project is corrected

- **WHEN** the user confirms a curation plan whose `edit` step corrects a
  Source's `project` field
- **THEN** only the `project` value span differs
- **AND** the Source's `updated` field, body and original media keep their
  bytes, and the pointer-write record holds the old value

#### Scenario: A direct correction names the curation route

- **WHEN** a caller applies a `patch_frontmatter` operation to a Source's
  `project` field through `edit_memory`
- **THEN** the write refuses with `APPEND_ONLY`, names the curation `edit`
  step, and nothing changes

#### Scenario: A field that is not a pointer stays immutable

- **WHEN** a caller applies a `patch_frontmatter` operation to a Source's
  `title` field
- **THEN** the write refuses with `APPEND_ONLY` and nothing changes

#### Scenario: A classification field is corrected through reclassify

- **WHEN** a curation `edit` step patches a Source's `source_type` field
- **THEN** proposal refuses with `POINTER_FIELD_OWNED` and names
  reclassification

#### Scenario: A correction is reverted

- **WHEN** the owner approves the compensation of a `project` correction while
  the Source holds the recorded after hash
- **THEN** the Source's bytes equal its bytes before the correction

### Requirement: A move previews one outcome per referrer

A move preview SHALL give each referrer exactly one outcome: `rewritten`,
`pointer-repointed (append-only page)`, `unchanged: resolves via prior path`,
`unchanged: keeps the old path`, or `refused` with its cause. A Records row or
a Planning item that names the moved path SHALL be `rewritten`, as any page
the mover may write is. A reference that the move does not write, such as a
link in a page the mover may not write or a prose path in an append-only page,
SHALL be `unchanged: resolves via prior path` when the moved page records the
move in its lineage. It SHALL be `unchanged: keeps the old path` when the moved
file records no lineage, such as a binary file or a page without a frontmatter
block; that reference dangles, and the owner's audit reports it. A restricted mover's preview SHALL list
only pages it may see, and its answer SHALL match a vault without the withheld
pages. The preview SHALL return a digest of its outcomes and bindings. A move
that repoints an append-only page SHALL commit only with that digest, under
the approval rule of the governed pointer write.

#### Scenario: The preview names every outcome

- **WHEN** a caller previews moving a note that a writable note, a Source and a
  Records row reference by path
- **THEN** the preview lists the writable note and the Records row as
  `rewritten` and the Source as `pointer-repointed (append-only page)`

#### Scenario: A move rewrites collection rows

- **WHEN** a caller moves a note that a Records row and a Planning item link by
  path
- **THEN** the row and the item name the note's new path

#### Scenario: A committed move reports its repoints

- **WHEN** a move that repoints a Source commits
- **THEN** its receipt names the Source by title in `pointer_repointed` and
  its summary counts the repointed append-only pages
- **AND** the Source's path appears only under `diagnostics.operational`

#### Scenario: A repoint without the preview digest

- **WHEN** a caller moves a note that a Source links by path without passing
  the preview digest
- **THEN** the move refuses with `POINTER_PREVIEW_REQUIRED` and nothing changes
